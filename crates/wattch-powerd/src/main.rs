use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use std::path::PathBuf;
use wattch_daemon::{DaemonIdentity, SampleBackend, ServerConfig};
use wattch_powerd::rapl::RaplBackend;
use wattch_powerd::synthetic::SyntheticBackend;

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
enum BackendChoice {
    /// Pick linux-rapl when a powercap tree exists, otherwise synthetic.
    Auto,
    /// Linux RAPL via the powercap sysfs interface (usually needs privilege).
    LinuxRapl,
    /// Deterministic synthetic laptop topology, no privilege required.
    Synthetic,
}

#[derive(Debug, Parser)]
#[command(name = "wattch-powerd")]
#[command(about = "Wattch powermeter daemon: privileged acquisition behind a local socket")]
struct Args {
    /// Unix socket path to serve the Wattch protocol on.
    #[arg(long, default_value = "/tmp/wattch-powerd.sock")]
    socket: PathBuf,

    #[arg(long, value_enum, default_value_t = BackendChoice::Auto)]
    backend: BackendChoice,

    /// Powercap sysfs root for the linux-rapl backend.
    #[arg(long, default_value = "/sys/class/powercap")]
    powercap_root: PathBuf,

    /// Node identity recorded in source descriptors.
    #[arg(long, default_value = "local-node")]
    node_id: String,

    /// Seed for the synthetic backend.
    #[arg(long, default_value_t = 42)]
    seed: u64,
}

fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let args = Args::parse();

    let backend = match args.backend {
        BackendChoice::Auto => {
            if args.powercap_root.is_dir()
                && RaplBackend::new(&args.powercap_root, &args.node_id).is_ok()
            {
                BackendChoice::LinuxRapl
            } else {
                tracing::warn!(
                    powercap_root = %args.powercap_root.display(),
                    "no usable RAPL zones; falling back to the synthetic backend"
                );
                BackendChoice::Synthetic
            }
        }
        explicit => explicit,
    };

    // Probe once so misconfiguration fails at startup, not per connection.
    let make_backend: Box<dyn Fn() -> Box<dyn SampleBackend> + Send + Sync> = match backend {
        BackendChoice::LinuxRapl => {
            RaplBackend::new(&args.powercap_root, &args.node_id)
                .context("linux-rapl backend unavailable")?;
            let root = args.powercap_root.clone();
            let node_id = args.node_id.clone();
            Box::new(move || {
                Box::new(
                    RaplBackend::new(&root, &node_id)
                        .expect("powercap tree disappeared after startup probe"),
                )
            })
        }
        BackendChoice::Synthetic => {
            let node_id = args.node_id.clone();
            let seed = args.seed;
            Box::new(move || Box::new(SyntheticBackend::new(&node_id, seed)))
        }
        BackendChoice::Auto => unreachable!("auto resolved above"),
    };

    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket_path: args.socket,
            identity: DaemonIdentity {
                daemon_name: "wattch-powerd".to_string(),
                daemon_version: env!("CARGO_PKG_VERSION").to_string(),
                node_id: args.node_id.clone(),
            },
        },
        move || make_backend(),
    )
    .context("failed to start wattch-powerd")?;

    println!(
        "wattch-powerd ({:?}) serving on {} (ctrl-c to stop)",
        backend,
        handle.socket_path().display()
    );
    handle.wait();
    Ok(())
}
