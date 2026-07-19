use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::ExitCode;
use std::str::FromStr;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use wattch_cli::artifact::{
    inspect_run, CommandLifecycleStatus, RunWriter, CLOCK_INSTANCE_ID_LABEL,
};
use wattch_cli::client::{CaptureFrame, WattchClient};
use wattch_cli::runner::{run_command, RunCommandOptions};
use wattch_core::{validate_source_set, RunManifest, SourceDescriptor};
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
        /// /run/wattch/wattch-powerd.sock or
        /// $XDG_RUNTIME_DIR/wattch/wattch-powerd.sock is used. Paths under
        /// /tmp require an explicit --socket because they are not trusted for
        /// automatic discovery.
        #[arg(long)]
        socket: Option<PathBuf>,

        /// Emit exactly one machine-readable discovery object on stdout.
        #[arg(long)]
        json: bool,
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

    /// Capture one shell-free command into a reproducible run artifact.
    Run {
        /// Unix socket to connect to. Same default resolution as `sources`.
        #[arg(long)]
        socket: Option<PathBuf>,

        /// Output root; the artifact is written to <out>/<run-id>/.
        #[arg(long, default_value = "runs")]
        out: PathBuf,

        #[arg(long, default_value_t = 100)]
        interval_ms: u64,

        /// Bound target wall time. On expiry Wattch terminates the target's
        /// dedicated process group, then finalizes the capture artifact.
        #[arg(long)]
        timeout_ms: Option<u64>,

        /// Explicit run id. Agent workflows should encode the trial condition.
        #[arg(long)]
        run_id: String,

        /// Repeatable condition metadata, for example `--label model=terra`.
        #[arg(long = "label", value_name = "KEY=VALUE")]
        labels: Vec<CliLabel>,

        /// Emit exactly one machine-readable summary object on stdout.
        #[arg(long)]
        json: bool,

        /// Program and arguments. `--` is required; no shell is inserted.
        #[arg(last = true, required = true, num_args = 1.., allow_hyphen_values = true)]
        program: Vec<String>,
    },

    /// Replay a run artifact offline and report per-source statistics.
    Inspect {
        #[arg(long)]
        run: PathBuf,

        /// Emit exactly one machine-readable inspection object on stdout.
        #[arg(long)]
        json: bool,
    },

    /// Validate source descriptors in a scenario or manifest file.
    Validate {
        #[arg(long)]
        input: PathBuf,
    },
}

fn main() -> Result<ExitCode> {
    let cli = Cli::parse();

    match cli.command {
        Command::Version => {
            println!("wattch protocol {PROTOCOL_VERSION}");
        }
        Command::Sources { socket, json } => {
            let socket = resolve_socket(socket)?;
            let client = WattchClient::connect(&socket)?;
            let hello = client.hello();
            if json {
                println!(
                    "{}",
                    serde_json::to_string(&SourcesJson {
                        daemon: DaemonIdentityJson {
                            name: &hello.daemon_name,
                            version: &hello.daemon_version,
                            node_id: &hello.node_id,
                        },
                        clock_instance_id: &hello.clock_instance_id,
                        protocol_version: &hello.protocol_version,
                        sources: &hello.sources,
                    })?
                );
            } else {
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

            let mut labels = std::collections::BTreeMap::new();
            labels.insert(
                CLOCK_INSTANCE_ID_LABEL.to_string(),
                hello.clock_instance_id.clone(),
            );
            let manifest = RunManifest {
                run_id: run_id.clone(),
                node_id: hello.node_id.clone(),
                protocol_version: hello.protocol_version.clone(),
                clock_instance_id: hello.clock_instance_id.clone(),
                wattch_version: env!("CARGO_PKG_VERSION").to_string(),
                created_unix_ns: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_nanos() as u64)
                    .unwrap_or(0),
                sources: hello.sources.clone(),
                labels,
            };

            let mut writer = RunWriter::create(&out, &manifest)?;
            let mut capture_error: Option<anyhow::Error> = None;
            let ack = client.capture_with_events(
                &run_id,
                interval_ms,
                Duration::from_millis(duration_ms),
                |frame| {
                    if capture_error.is_some() {
                        return;
                    }
                    let result = match frame {
                        CaptureFrame::RawSampleBatch(batch) => writer.append_batch(&batch),
                        CaptureFrame::EventBatch(batch) => writer.append_event_batch(&batch),
                        CaptureFrame::RecordEventAck(_) | CaptureFrame::StopCaptureAck(_) => Ok(()),
                    };
                    if let Err(err) = result {
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
        Command::Run {
            socket,
            out,
            interval_ms,
            timeout_ms,
            run_id,
            labels,
            json,
            program,
        } => {
            let socket = resolve_socket(socket)?;
            let result = run_command(RunCommandOptions {
                socket,
                out,
                interval_ms,
                timeout: timeout_ms.map(Duration::from_millis),
                run_id,
                labels: labels_to_map(labels)?,
                argv: program,
            })?;
            if json {
                println!("{}", serde_json::to_string(&result)?);
            } else {
                println!(
                    "captured {} batches / {} samples into {} (command exit {})",
                    result.batches,
                    result.samples,
                    result.run_dir.display(),
                    result.exit_code
                );
            }
            return Ok(ExitCode::from(result.wrapper_exit_code()));
        }
        Command::Inspect { run, json } => {
            let report = inspect_run(&run)?;
            if json {
                println!("{}", serde_json::to_string(&report)?);
            } else {
                println!("run: {} (node {})", report.run_id, report.node_id);
                println!(
                    "batches: {}  samples: {}  event_batches: {}  events: {}",
                    report.batches, report.samples, report.event_batches, report.events
                );
                if let Some(lifecycle) = &report.command_lifecycle {
                    match lifecycle.status {
                        CommandLifecycleStatus::Complete => println!(
                            "command: COMPLETE  exit_code: {}  signal: {}",
                            lifecycle.exit_code.unwrap_or(1),
                            lifecycle
                                .signal
                                .map_or_else(|| "none".to_string(), |signal| signal.to_string())
                        ),
                        CommandLifecycleStatus::Incomplete => println!(
                            "WARNING: command lifecycle INCOMPLETE: {}",
                            lifecycle.reason.as_deref().unwrap_or("unspecified reason")
                        ),
                    }
                }
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

    Ok(ExitCode::SUCCESS)
}

#[derive(Serialize)]
struct SourcesJson<'a> {
    daemon: DaemonIdentityJson<'a>,
    clock_instance_id: &'a str,
    protocol_version: &'a str,
    sources: &'a [SourceDescriptor],
}

#[derive(Serialize)]
struct DaemonIdentityJson<'a> {
    name: &'a str,
    version: &'a str,
    node_id: &'a str,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct CliLabel {
    key: String,
    value: String,
}

impl FromStr for CliLabel {
    type Err = String;

    fn from_str(raw: &str) -> std::result::Result<Self, Self::Err> {
        let (key, value) = raw
            .split_once('=')
            .ok_or_else(|| "label must use KEY=VALUE".to_string())?;
        if key.is_empty() {
            return Err("label key must not be empty".to_string());
        }
        Ok(Self {
            key: key.to_string(),
            value: value.to_string(),
        })
    }
}

fn labels_to_map(labels: Vec<CliLabel>) -> Result<BTreeMap<String, String>> {
    let mut parsed = BTreeMap::new();
    for label in labels {
        if matches!(
            label.key.as_str(),
            CLOCK_INSTANCE_ID_LABEL | "pid" | "returncode" | "signal" | "timed_out"
        ) {
            bail!("--label key {:?} is reserved", label.key);
        }
        if parsed.insert(label.key.clone(), label.value).is_some() {
            bail!("duplicate --label key {:?}", label.key);
        }
    }
    Ok(parsed)
}

/// Resolve the daemon socket to connect to. An explicit `--socket` always
/// wins; otherwise probe the shared default candidates (see
/// `wattch_protocol::socket`) and use the first one that exists, or fail
/// listing every path tried. `wattch-powerd` is preferred; the deterministic
/// `wattch-testd` sockets are probed last so a dev `wattch sources` against a
/// running testd works without `--socket`.
fn resolve_socket(explicit: Option<PathBuf>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return Ok(path);
    }
    let mut candidates = wattch_protocol::socket::default_connect_candidates("wattch-powerd.sock");
    candidates.extend(wattch_protocol::socket::default_connect_candidates(
        "wattch-testd.sock",
    ));
    resolve_from_candidates(candidates)
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

    #[test]
    fn run_parser_preserves_trailing_program_arguments() {
        let cli = Cli::try_parse_from([
            "wattch",
            "run",
            "--run-id",
            "agent-r01",
            "--label",
            "note=a=b",
            "--json",
            "--",
            "tool",
            "--json",
            "literal;argument",
        ])
        .unwrap();
        let Command::Run {
            run_id,
            labels,
            json,
            program,
            ..
        } = cli.command
        else {
            panic!("expected run command");
        };
        assert_eq!(run_id, "agent-r01");
        assert_eq!(labels, vec!["note=a=b".parse().unwrap()]);
        assert!(json);
        assert_eq!(program, ["tool", "--json", "literal;argument"]);
    }

    #[test]
    fn run_labels_reject_malformed_duplicate_and_reserved_keys() {
        assert!("missing-separator".parse::<CliLabel>().is_err());
        assert!("=value".parse::<CliLabel>().is_err());

        let duplicate = vec![
            "model=luna".parse().unwrap(),
            "model=terra".parse().unwrap(),
        ];
        assert!(labels_to_map(duplicate).is_err());
        assert!(labels_to_map(vec!["pid=12".parse().unwrap()]).is_err());
        assert!(labels_to_map(vec!["timed_out=true".parse().unwrap()]).is_err());
        assert!(
            labels_to_map(vec![format!("{CLOCK_INSTANCE_ID_LABEL}=clock")
                .parse()
                .unwrap()])
            .is_err()
        );
    }
}
