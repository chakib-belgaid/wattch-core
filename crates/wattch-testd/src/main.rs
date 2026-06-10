use anyhow::{Context, Result};
use clap::Parser;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use wattch_core::{validate_source_set, RawSample, RawSampleBatch, RawValue, SourceDescriptor};

#[derive(Debug, Parser)]
struct Args {
    /// Deterministic scenario file.
    #[arg(long)]
    scenario: PathBuf,

    /// Unix socket path for the future daemon transport.
    #[arg(long, default_value = "/tmp/wattch-testd.sock")]
    socket: PathBuf,

    /// Print generated batches instead of binding the socket.
    #[arg(long)]
    dry_run: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Scenario {
    scenario_id: String,
    run_id: String,
    node_id: String,
    tick_ns: u64,
    ticks: u64,
    sources: Vec<SyntheticSource>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct SyntheticSource {
    #[serde(flatten)]
    descriptor: SourceDescriptor,
    behavior: LinearBehavior,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct LinearBehavior {
    start: f64,
    step_per_tick: f64,
}

fn main() -> Result<()> {
    tracing_subscriber::fmt().with_env_filter("info").init();

    let args = Args::parse();
    let scenario = load_scenario(&args.scenario)?;
    let descriptors = scenario
        .sources
        .iter()
        .map(|s| s.descriptor.clone())
        .collect::<Vec<_>>();

    validate_source_set(&descriptors).context("invalid scenario source descriptors")?;

    if args.dry_run {
        println!("# scenario: {}", scenario.scenario_id);
        println!("# node: {}", scenario.node_id);
        println!("# sources: {}", scenario.sources.len());
        for batch in generate_batches(&scenario) {
            println!("{}", serde_json::to_string(&batch)?);
        }
        return Ok(());
    }

    // The socket transport is intentionally left as the next vertical slice.
    // For now, dry-run output creates deterministic golden fixtures while the
    // protocol and source semantics stabilize.
    println!(
        "wattch-testd scenario is valid. Next slice: bind local socket at {}",
        args.socket.display()
    );

    Ok(())
}

fn load_scenario(path: &PathBuf) -> Result<Scenario> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read scenario {}", path.display()))?;
    serde_json::from_str(&raw)
        .with_context(|| format!("failed to parse scenario {}", path.display()))
}

fn generate_batches(scenario: &Scenario) -> impl Iterator<Item = RawSampleBatch> + '_ {
    (0..scenario.ticks).map(move |tick| {
        let samples = scenario
            .sources
            .iter()
            .map(|source| RawSample {
                source_id: source.descriptor.local_source_id,
                sequence_no: tick,
                monotonic_ns: tick * scenario.tick_ns,
                value: RawValue::F64(
                    source.behavior.start + source.behavior.step_per_tick * tick as f64,
                ),
            })
            .collect();

        RawSampleBatch {
            run_id: scenario.run_id.clone(),
            node_id: scenario.node_id.clone(),
            batch_sequence_no: tick,
            samples,
        }
    })
}
