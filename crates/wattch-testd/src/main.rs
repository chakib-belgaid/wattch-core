use anyhow::{Context, Result};
use clap::Parser;
use std::path::PathBuf;
use wattch_core::validate_source_set;
use wattch_daemon::{DaemonIdentity, PeerPolicy, ServerConfig, SocketSource};
use wattch_testd::scenario::{generate_batches, load_scenario, DeterministicBackend};

#[derive(Debug, Parser)]
struct Args {
    /// Deterministic scenario file.
    #[arg(long)]
    scenario: PathBuf,

    /// Unix socket path to serve the Wattch protocol on.
    ///
    /// Defaults to (in order of preference): /run/wattch/wattch-testd.sock
    /// when running as root, $XDG_RUNTIME_DIR/wattch/wattch-testd.sock, or a
    /// per-uid path under /tmp.
    #[arg(long)]
    socket: Option<PathBuf>,

    /// Print generated batches instead of binding the socket.
    #[arg(long)]
    dry_run: bool,

    /// Additional uid allowed to connect (repeatable). uid 0 and the
    /// daemon's own effective uid are always allowed.
    #[arg(long = "allow-uid", value_name = "UID")]
    allow_uid: Vec<u32>,

    /// Additional gid allowed to connect (repeatable). Matches the peer's
    /// EFFECTIVE gid only — supplementary group membership does not count;
    /// use the socket file's group permissions (or --allow-uid) for that.
    #[arg(long = "allow-gid", value_name = "GID")]
    allow_gid: Vec<u32>,
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

    let socket_path = args
        .socket
        .unwrap_or_else(|| wattch_protocol::socket::default_bind_path("wattch-testd.sock"));

    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path),
            identity,
            peer_policy: PeerPolicy::with_grants(&args.allow_uid, &args.allow_gid),
        },
        move || Box::new(DeterministicBackend::new(&scenario)),
    )
    .context("failed to start wattch-testd server")?;

    println!(
        "wattch-testd serving on {} (ctrl-c to stop)",
        handle.socket_path().display()
    );

    // `ServerHandle::shutdown` consumes `self` and unlinks the socket file;
    // `handle.wait()` alone never calls it, so Ctrl-C (or SIGTERM) would
    // leave a stale socket behind. Route the signal through a channel
    // instead of blocking on `wait()`, then shut down explicitly so the
    // socket is always removed on the way out.
    let (stop_tx, stop_rx) = std::sync::mpsc::channel();
    ctrlc::set_handler(move || {
        let _ = stop_tx.send(());
    })
    .context("failed to install signal handler")?;
    let _ = stop_rx.recv();
    handle.shutdown();
    Ok(())
}
