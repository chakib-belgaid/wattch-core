use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use wattch_cli::artifact::{inspect_run, RunWriter};
use wattch_cli::client::WattchClient;
use wattch_core::{validate_batch, validate_source_set, RunManifest, SourceDescriptor};
use wattch_protocol::PROTOCOL_VERSION;

#[derive(Debug, Parser)]
#[command(name = "wattch")]
#[command(about = "Protocol-first energy and telemetry capture CLI")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Print protocol/client version.
    Version,

    /// List sources exposed by a daemon.
    Sources {
        /// Unix socket to connect to. When omitted, the first existing of
        /// /run/wattch/wattch-powerd.sock, $XDG_RUNTIME_DIR/wattch/wattch-powerd.sock,
        /// then a per-uid /tmp path is used; the command fails listing the
        /// paths it tried if none exist.
        #[arg(long)]
        socket: Option<PathBuf>,
    },

    /// Capture a raw trace artifact from a daemon.
    Capture {
        /// Unix socket to connect to. Same default resolution as `sources`.
        #[arg(long)]
        socket: Option<PathBuf>,

        /// Output root; the artifact is written to <out>/<run-id>/.
        #[arg(long, default_value = "runs")]
        out: PathBuf,

        #[arg(long, default_value_t = 5_000)]
        duration_ms: u64,

        #[arg(long, default_value_t = 100)]
        interval_ms: u64,

        /// Run id; generated from the wall clock when omitted.
        #[arg(long)]
        run_id: Option<String>,
    },

    /// Replay a run artifact offline and report per-source statistics.
    Inspect {
        #[arg(long)]
        run: PathBuf,
    },

    /// Validate source descriptors in a scenario or manifest file.
    Validate {
        #[arg(long)]
        input: PathBuf,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::Version => {
            println!("wattch protocol {PROTOCOL_VERSION}");
        }
        Command::Sources { socket } => {
            let socket = resolve_socket(socket)?;
            let client = WattchClient::connect(&socket)?;
            let hello = client.hello();
            println!(
                "daemon: {} {} (node {})",
                hello.daemon_name, hello.daemon_version, hello.node_id
            );
            println!(
                "{:<4} {:<40} {:<12} {:<12} kind",
                "id", "path", "unit", "temporality"
            );
            for source in client.sources() {
                println!(
                    "{:<4} {:<40} {:<12} {:<12} {:?}",
                    source.local_source_id,
                    source.source_path,
                    format!("{:?}", source.unit),
                    format!("{:?}", source.temporality),
                    source.kind
                );
            }
        }
        Command::Capture {
            socket,
            out,
            duration_ms,
            interval_ms,
            run_id,
        } => {
            let run_id = run_id.unwrap_or_else(generated_run_id);
            let socket = resolve_socket(socket)?;
            let mut client = WattchClient::connect(&socket)?;
            let hello = client.hello().clone();
            validate_source_set(&hello.sources)
                .context("daemon advertised an invalid source set")?;

            let manifest = RunManifest {
                run_id: run_id.clone(),
                node_id: hello.node_id.clone(),
                protocol_version: hello.protocol_version.clone(),
                wattch_version: env!("CARGO_PKG_VERSION").to_string(),
                created_unix_ns: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_nanos() as u64)
                    .unwrap_or(0),
                sources: hello.sources.clone(),
                labels: Default::default(),
            };

            let mut writer = RunWriter::create(&out, &manifest)?;
            let mut capture_error: Option<anyhow::Error> = None;
            let ack = client.capture(
                &run_id,
                interval_ms,
                Duration::from_millis(duration_ms),
                |batch| {
                    if capture_error.is_some() {
                        return;
                    }
                    if let Err(err) = validate_batch(&batch, &hello.sources) {
                        capture_error =
                            Some(anyhow::Error::new(err).context("daemon sent an invalid batch"));
                    } else if let Err(err) = writer.append_batch(&batch) {
                        capture_error = Some(err.into());
                    }
                },
            )?;
            if let Some(err) = capture_error {
                return Err(err);
            }
            let run_dir = writer.finish()?;

            println!(
                "captured {} batches / {} samples into {}",
                ack.batches_sent,
                ack.samples_sent,
                run_dir.display()
            );
        }
        Command::Inspect { run } => {
            let report = inspect_run(&run)?;
            println!("run: {} (node {})", report.run_id, report.node_id);
            println!("batches: {}  samples: {}", report.batches, report.samples);
            if report.unknown_source_samples > 0 {
                println!(
                    "WARNING: {} samples reference unknown source ids",
                    report.unknown_source_samples
                );
            }
            println!(
                "{:<4} {:<10} {:<12} {:<12} {:<8} span_ms",
                "id", "samples", "first_seq", "last_seq", "gaps"
            );
            for (source_id, stats) in &report.per_source {
                println!(
                    "{:<4} {:<10} {:<12} {:<12} {:<8} {:.1}",
                    source_id,
                    stats.samples,
                    stats.first_sequence_no,
                    stats.last_sequence_no,
                    stats.sequence_gaps,
                    (stats.last_monotonic_ns - stats.first_monotonic_ns) as f64 / 1e6
                );
            }
        }
        Command::Validate { input } => {
            let raw = std::fs::read_to_string(&input)
                .with_context(|| format!("failed to read {}", input.display()))?;
            let value: serde_json::Value = serde_json::from_str(&raw)
                .with_context(|| format!("invalid json in {}", input.display()))?;
            let Some(sources) = value.get("sources").and_then(|s| s.as_array()) else {
                bail!("{} has no `sources` array", input.display());
            };
            let descriptors: Vec<SourceDescriptor> = sources
                .iter()
                .map(|s| serde_json::from_value(s.clone()))
                .collect::<Result<_, _>>()
                .context("invalid source descriptor")?;
            validate_source_set(&descriptors)?;
            println!("{}: {} sources valid", input.display(), descriptors.len());
        }
    }

    Ok(())
}

/// Resolve the daemon socket to connect to. An explicit `--socket` always
/// wins; otherwise probe the shared default candidates (see
/// `wattch_protocol::socket`) and use the first one that exists, or fail
/// listing every path tried.
fn resolve_socket(explicit: Option<PathBuf>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return Ok(path);
    }
    resolve_from_candidates(wattch_protocol::socket::default_connect_candidates(
        "wattch-powerd.sock",
    ))
}

fn resolve_from_candidates(candidates: Vec<PathBuf>) -> Result<PathBuf> {
    candidates
        .iter()
        .find(|path| path.exists())
        .cloned()
        .ok_or_else(|| {
            let tried = candidates
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>()
                .join(", ");
            anyhow::anyhow!("no wattch daemon socket found; tried: {tried}")
        })
}

fn generated_run_id() -> String {
    let unix_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("run-{unix_ms}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unique_temp_path(tag: &str) -> PathBuf {
        static COUNTER: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
        let n = COUNTER.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        std::env::temp_dir().join(format!(
            "wcli-{tag}-{}-{n}.sock",
            std::process::id() % 10_000
        ))
    }

    #[test]
    fn resolve_from_candidates_picks_first_existing() {
        let missing = unique_temp_path("a");
        let existing = unique_temp_path("b");
        std::fs::write(&existing, b"").unwrap();
        let other = unique_temp_path("c");

        let resolved =
            resolve_from_candidates(vec![missing.clone(), existing.clone(), other.clone()])
                .unwrap();
        assert_eq!(resolved, existing);

        let _ = std::fs::remove_file(&existing);
    }

    #[test]
    fn resolve_from_candidates_errors_listing_every_tried_path_when_none_exist() {
        let a = unique_temp_path("none-a");
        let b = unique_temp_path("none-b");

        let err = resolve_from_candidates(vec![a.clone(), b.clone()]).unwrap_err();
        let message = err.to_string();
        assert!(message.contains(&a.display().to_string()));
        assert!(message.contains(&b.display().to_string()));
    }

    #[test]
    fn resolve_socket_bypasses_candidate_probing_when_explicit() {
        // An explicit --socket is returned as-is even if it doesn't exist:
        // no behavior change vs. before this feature existed.
        let explicit = unique_temp_path("explicit-missing");
        assert_eq!(resolve_socket(Some(explicit.clone())).unwrap(), explicit);
    }
}
