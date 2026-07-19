use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use std::path::PathBuf;
use wattch_daemon::{DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, SocketSource};
use wattch_powerd::composite::{CloneSampleBackend, CompositeBackend};
use wattch_powerd::linux_os::{LinuxOsBackend, LinuxOsPaths};
use wattch_powerd::powermetrics::PowermetricsBackend;
use wattch_powerd::rapl::RaplBackend;
use wattch_powerd::synthetic::SyntheticBackend;

type BackendFactory = Box<dyn Fn() -> Box<dyn SampleBackend> + Send + Sync>;

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
enum BackendChoice {
    /// Pick apple-powermetrics on macOS, linux-rapl when available on Linux,
    /// otherwise synthetic.
    Auto,
    /// Linux RAPL via the powercap sysfs interface (usually needs privilege).
    LinuxRapl,
    /// Apple powermetrics CPU/GPU/ANE estimates (macOS, requires root).
    ApplePowermetrics,
    /// Deterministic synthetic laptop topology, no privilege required.
    Synthetic,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
enum OsTelemetryChoice {
    /// Include Linux OS telemetry when supported, otherwise continue without it.
    Auto,
    /// Require Linux OS telemetry and fail startup when no source is available.
    On,
    /// Expose only the selected power/energy backend.
    Off,
}

#[derive(Debug, Parser)]
#[command(name = "wattch-powerd")]
#[command(about = "Wattch powermeter daemon: privileged acquisition behind a local socket")]
struct Args {
    /// Unix socket path to serve the Wattch protocol on.
    ///
    /// Defaults to the platform root runtime directory when running as root,
    /// then $XDG_RUNTIME_DIR/wattch, then a per-uid path under /tmp. Clients
    /// do not auto-discover /tmp. See docs/powerd.md.
    #[arg(long)]
    socket: Option<PathBuf>,

    #[arg(long, value_enum, default_value_t = BackendChoice::Auto)]
    backend: BackendChoice,

    /// Powercap sysfs root for the linux-rapl backend.
    #[arg(long, default_value = "/sys/class/powercap")]
    powercap_root: PathBuf,

    /// Native powermetrics collection interval in milliseconds. Client
    /// intervals may be faster, but only fresh Apple samples are emitted.
    #[arg(long, default_value_t = 1_000)]
    powermetrics_interval_ms: u64,

    /// Node identity recorded in source descriptors.
    #[arg(long, default_value = "local-node")]
    node_id: String,

    /// Seed for the synthetic backend.
    #[arg(long, default_value_t = 42)]
    seed: u64,

    /// Compose raw Linux scheduler/CPU-state/thermal/wakeup sources with the powermeter.
    #[arg(long, value_enum, default_value_t = OsTelemetryChoice::Auto)]
    os_telemetry: OsTelemetryChoice,

    /// Linux proc-stat path (overridable for containers and tests).
    #[arg(long, default_value = "/proc/stat")]
    proc_stat: PathBuf,

    /// Linux CPU sysfs root (overridable for containers and tests).
    #[arg(long, default_value = "/sys/devices/system/cpu")]
    cpu_sysfs_root: PathBuf,

    /// Linux thermal sysfs root (overridable for containers and tests).
    #[arg(long, default_value = "/sys/class/thermal")]
    thermal_sysfs_root: PathBuf,

    /// Linux sysfs root containing per-source wakeup event counters.
    #[arg(long, default_value = "/sys/class/wakeup")]
    wakeup_sources_root: PathBuf,

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
            if cfg!(target_os = "macos") {
                BackendChoice::ApplePowermetrics
            } else if args.powercap_root.is_dir()
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

    let linux_os_paths = LinuxOsPaths {
        proc_stat: args.proc_stat.clone(),
        cpu_root: args.cpu_sysfs_root.clone(),
        thermal_root: args.thermal_sysfs_root.clone(),
        wakeup_root: args.wakeup_sources_root.clone(),
    };
    let os_telemetry = match args.os_telemetry {
        OsTelemetryChoice::Off => None,
        OsTelemetryChoice::On => Some(
            LinuxOsBackend::new(linux_os_paths, &args.node_id)
                .context("required Linux OS telemetry unavailable")?,
        ),
        OsTelemetryChoice::Auto => match LinuxOsBackend::new(linux_os_paths, &args.node_id) {
            Ok(backend) => Some(backend),
            Err(error) => {
                tracing::info!(error = %error, "Linux OS telemetry unavailable; continuing");
                None
            }
        },
    };

    // Probe and validate the full source composition once. Each connection
    // then receives a clone of that prototype, so invalid descriptors fail
    // startup instead of panicking inside the server's infallible factory.
    let make_backend: BackendFactory = match backend {
        BackendChoice::LinuxRapl => {
            let probed = RaplBackend::new(&args.powercap_root, &args.node_id)
                .context("linux-rapl backend unavailable")?;
            make_backend_factory(probed, os_telemetry.clone())?
        }
        BackendChoice::ApplePowermetrics => {
            let probed = PowermetricsBackend::new(&args.node_id, args.powermetrics_interval_ms)
                .context(
                    "apple-powermetrics backend unavailable; on macOS run wattch-powerd as root",
                )?;
            make_backend_factory(probed, os_telemetry.clone())?
        }
        BackendChoice::Synthetic => {
            let probed = SyntheticBackend::new(&args.node_id, args.seed);
            make_backend_factory(probed, os_telemetry.clone())?
        }
        BackendChoice::Auto => anyhow::bail!("automatic backend selection remained unresolved"),
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
            limits: Default::default(),
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

fn make_backend_factory<B>(
    powermeter: B,
    os_telemetry: Option<LinuxOsBackend>,
) -> Result<BackendFactory>
where
    B: SampleBackend + Clone + Sync + 'static,
{
    let Some(os_telemetry) = os_telemetry else {
        return Ok(Box::new(move || Box::new(powermeter.clone())));
    };
    let children: Vec<Box<dyn CloneSampleBackend>> =
        vec![Box::new(powermeter), Box::new(os_telemetry)];
    let prototype = CompositeBackend::new(children)
        .context("powermeter and OS telemetry descriptors cannot be composed")?;
    Ok(Box::new(move || Box::new(prototype.clone())))
}
