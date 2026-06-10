use anyhow::{Context, Result};
use clap::Parser;
use std::path::PathBuf;
use wattch_core::validate_source_set;
use wattch_daemon::{DaemonIdentity, ServerConfig};
use wattch_testd::scenario::{generate_batches, load_scenario, DeterministicBackend};

#[derive(Debug, Parser)]
struct Args {
    /// Deterministic scenario file.
    #[arg(long)]
    scenario: PathBuf,

    /// Unix socket path to serve the Wattch protocol on.
    #[arg(long, default_value = "/tmp/wattch-testd.sock")]
    socket: PathBuf,

    /// Print generated batches instead of binding the socket.
    #[arg(long)]
    dry_run: bool,
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

    let identity = DaemonIdentity {
        daemon_name: "wattch-testd".to_string(),
        daemon_version: env!("CARGO_PKG_VERSION").to_string(),
        node_id: scenario.node_id.clone(),
    };

    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket_path: args.socket,
            identity,
        },
        move || Box::new(DeterministicBackend::new(&scenario)),
    )
    .context("failed to start wattch-testd server")?;

    println!(
        "wattch-testd serving on {} (ctrl-c to stop)",
        handle.socket_path().display()
    );
    handle.wait();
    Ok(())
}
