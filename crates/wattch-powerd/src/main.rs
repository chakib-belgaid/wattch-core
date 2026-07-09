use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use std::path::PathBuf;
use wattch_daemon::{DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, SocketSource};
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
    ///
    /// Defaults to (in order of preference): /run/wattch/wattch-powerd.sock
    /// when running as root, $XDG_RUNTIME_DIR/wattch/wattch-powerd.sock, or
    /// a per-uid path under /tmp. See docs/powerd.md for the full model.
    #[arg(long)]
    socket: Option<PathBuf>,

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

    // Scan the powercap tree once so misconfiguration fails at startup, not
    // per connection, and so every connection shares the same probed zone
    // list (`RaplBackend` is a cheap `Clone` of already-read data) instead
    // of re-scanning powercap on each new connection. If the powercap tree
    // disappears later (module unload, permission change), that surfaces as
    // per-sample read errors on the existing skip-with-warn path in
    // `RaplBackend::sample`, not a re-scan and not a panic.
    let make_backend: Box<dyn Fn() -> Box<dyn SampleBackend> + Send + Sync> = match backend {
        BackendChoice::LinuxRapl => {
            let probed = RaplBackend::new(&args.powercap_root, &args.node_id)
                .context("linux-rapl backend unavailable")?;
            Box::new(move || Box::new(probed.clone()))
        }
        BackendChoice::Synthetic => {
            let node_id = args.node_id.clone();
            let seed = args.seed;
            Box::new(move || Box::new(SyntheticBackend::new(&node_id, seed)))
        }
        BackendChoice::Auto => unreachable!("auto resolved above"),
    };

    // systemd socket activation: if systemd handed us a pre-bound socket
    // (LISTEN_FDS), serve on it. The listenfd crate owns the only unsafe
    // raw-fd conversion; this crate stays zero-unsafe.
    let activated = listenfd::ListenFd::from_env()
        .take_unix_listener(0)
        .context("systemd-activated fd 0 is not a Unix stream listener")?;

    let socket =
        match activated {
            Some(listener) => {
                if let Some(socket) = &args.socket {
                    anyhow::bail!(
                        "--socket {} conflicts with a systemd-activated socket \
                     (LISTEN_FDS is set); configure the path in the .socket \
                     unit or drop the flag",
                        socket.display()
                    );
                }
                tracing::info!("using systemd-activated socket");
                SocketSource::Activated(listener)
            }
            None => SocketSource::Bind(args.socket.clone().unwrap_or_else(|| {
                wattch_protocol::socket::default_bind_path("wattch-powerd.sock")
            })),
        };

    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket,
            identity: DaemonIdentity {
                daemon_name: "wattch-powerd".to_string(),
                daemon_version: env!("CARGO_PKG_VERSION").to_string(),
                node_id: args.node_id.clone(),
            },
            peer_policy: PeerPolicy::with_grants(&args.allow_uid, &args.allow_gid),
        },
        move || make_backend(),
    )
    .context("failed to start wattch-powerd")?;

    println!(
        "wattch-powerd ({:?}) serving on {} (ctrl-c to stop)",
        backend,
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
