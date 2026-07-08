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
        #[arg(long, default_value = "/tmp/wattch-testd.sock")]
        socket: PathBuf,
    },

    /// Capture a raw trace artifact from a daemon.
    Capture {
        #[arg(long, default_value = "/tmp/wattch-testd.sock")]
        socket: PathBuf,

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

fn generated_run_id() -> String {
    let unix_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("run-{unix_ms}")
}
