use crate::backend::SampleBackend;
use std::collections::BTreeMap;
use std::fs::{self, DirBuilder, Permissions};
use std::io;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};
use thiserror::Error;
use tracing::{debug, info, warn};
use wattch_core::{validate_source_set, RawSample, RawSampleBatch, SourceDescriptor};
use wattch_protocol::frame::WireError;
use wattch_protocol::messages::{
    error_codes, read_message, write_message, ErrorMessage, HelloResponse, Message,
    StartCaptureAck, StopCaptureAck,
};
use wattch_protocol::PROTOCOL_VERSION;

#[derive(Clone, Debug)]
pub struct DaemonIdentity {
    pub daemon_name: String,
    pub daemon_version: String,
    pub node_id: String,
}

#[derive(Clone, Debug)]
pub struct ServerConfig {
    pub socket_path: PathBuf,
    pub identity: DaemonIdentity,
}

#[derive(Debug, Error)]
pub enum ServerError {
    #[error("invalid backend source descriptors: {0}")]
    InvalidDescriptors(#[from] wattch_core::Error),

    #[error("io error: {0}")]
    Io(#[from] io::Error),
}

/// Handle to a running server. Dropping it does not stop the server;
/// call [`ServerHandle::shutdown`].
pub struct ServerHandle {
    socket_path: PathBuf,
    stop: Arc<AtomicBool>,
    accept_thread: thread::JoinHandle<()>,
}

impl ServerHandle {
    pub fn socket_path(&self) -> &Path {
        &self.socket_path
    }

    /// Stop accepting connections and join the accept loop. Connections in
    /// flight finish their current session on their own threads.
    pub fn shutdown(self) {
        self.stop.store(true, Ordering::SeqCst);
        // Wake the blocking accept() with a throwaway connection.
        let _ = UnixStream::connect(&self.socket_path);
        let _ = self.accept_thread.join();
        let _ = std::fs::remove_file(&self.socket_path);
    }

    /// Block until the server is shut down externally (for daemon mains).
    pub fn wait(self) {
        let _ = self.accept_thread.join();
        let _ = std::fs::remove_file(&self.socket_path);
    }
}

/// Validate the backend and start serving the Wattch protocol on a Unix
/// socket. Each connection gets its own backend instance from `factory`.
pub fn spawn<F>(config: ServerConfig, factory: F) -> Result<ServerHandle, ServerError>
where
    F: Fn() -> Box<dyn SampleBackend> + Send + Sync + 'static,
{
    let probe = factory();
    validate_source_set(probe.descriptors())?;
    drop(probe);

    ensure_socket_parent_dir(&config.socket_path)?;

    if config.socket_path.exists() {
        // A previous daemon may have crashed without unlinking its socket.
        // If nothing accepts connections there, the socket is stale.
        if UnixStream::connect(&config.socket_path).is_ok() {
            return Err(ServerError::Io(io::Error::new(
                io::ErrorKind::AddrInUse,
                format!(
                    "another daemon is already listening on {}",
                    config.socket_path.display()
                ),
            )));
        }
        warn!(path = %config.socket_path.display(), "removing stale socket");
        std::fs::remove_file(&config.socket_path)?;
    }

    let listener = UnixListener::bind(&config.socket_path)?;
    // energy_uj-derived samples are a documented side channel (PLATYPUS,
    // CVE-2020-8694); the socket must not be group/world writable, and the
    // owned parent directory (mode 0750, set above) is the TOCTOU mitigation
    // for the stale-socket probe below.
    fs::set_permissions(&config.socket_path, Permissions::from_mode(0o660))?;
    info!(path = %config.socket_path.display(), "listening");

    let stop = Arc::new(AtomicBool::new(false));
    let accept_stop = Arc::clone(&stop);
    let socket_path = config.socket_path.clone();
    let identity = config.identity;
    let factory = Arc::new(factory);

    let accept_thread = thread::spawn(move || {
        for connection in listener.incoming() {
            if accept_stop.load(Ordering::SeqCst) {
                break;
            }
            match connection {
                Ok(stream) => {
                    let identity = identity.clone();
                    let backend = factory();
                    thread::spawn(move || {
                        if let Err(err) = handle_connection(stream, &identity, backend) {
                            debug!(error = %err, "connection ended with error");
                        }
                    });
                }
                Err(err) => {
                    warn!(error = %err, "accept failed");
                }
            }
        }
    });

    Ok(ServerHandle {
        socket_path,
        stop,
        accept_thread,
    })
}

/// Create the socket's parent directory (mode 0750) if it doesn't exist yet.
///
/// Only the leaf directory is created (e.g. `wattch/` in `/run/wattch/`) —
/// this deliberately does not behave like `create_dir_all`. If the
/// grandparent is also missing, that's a misconfiguration (the runtime root
/// should already exist, e.g. via systemd's `RuntimeDirectory=`), so we fail
/// loudly instead of creating an unbounded ancestor chain with directory
/// modes we didn't choose.
fn ensure_socket_parent_dir(socket_path: &Path) -> Result<(), ServerError> {
    let parent = socket_path.parent().filter(|p| !p.as_os_str().is_empty());
    let Some(parent) = parent else {
        return Ok(());
    };
    if parent.exists() {
        return Ok(());
    }

    if let Some(grandparent) = parent.parent() {
        if !grandparent.as_os_str().is_empty() && !grandparent.exists() {
            return Err(ServerError::Io(io::Error::new(
                io::ErrorKind::NotFound,
                format!(
                    "cannot create socket directory {}: parent directory {} does not exist \
                     (only the immediate socket directory is created automatically)",
                    parent.display(),
                    grandparent.display()
                ),
            )));
        }
    }

    match DirBuilder::new().mode(0o750).create(parent) {
        Ok(()) => Ok(()),
        // Tolerates the TOCTOU race between the `exists()` check above and
        // this call (e.g. another daemon instance creating it concurrently).
        Err(err) if err.kind() == io::ErrorKind::AlreadyExists => Ok(()),
        Err(err) => Err(ServerError::Io(err)),
    }
}

fn send_error(stream: &mut UnixStream, code: u32, message: impl Into<String>) {
    let _ = write_message(
        stream,
        &Message::Error(ErrorMessage {
            code,
            message: message.into(),
        }),
    );
}

fn handle_connection(
    mut stream: UnixStream,
    identity: &DaemonIdentity,
    mut backend: Box<dyn SampleBackend>,
) -> Result<(), WireError> {
    let first = match read_message(&mut stream) {
        Ok(message) => message,
        Err(WireError::ConnectionClosed) => return Ok(()),
        Err(err @ (WireError::Frame(_) | WireError::InvalidPayload { .. })) => {
            send_error(
                &mut stream,
                error_codes::PROTOCOL_VIOLATION,
                err.to_string(),
            );
            return Ok(());
        }
        Err(err) => return Err(err),
    };
    let hello = match first {
        Message::HelloRequest(hello) => hello,
        other => {
            send_error(
                &mut stream,
                error_codes::PROTOCOL_VIOLATION,
                format!("expected HelloRequest, got {:?}", other.kind()),
            );
            return Ok(());
        }
    };

    if !compatible_version(&hello.protocol_version) {
        send_error(
            &mut stream,
            error_codes::UNSUPPORTED_VERSION,
            format!(
                "client protocol {} is not compatible with daemon protocol {}",
                hello.protocol_version, PROTOCOL_VERSION
            ),
        );
        return Ok(());
    }

    debug!(client = %hello.client_name, version = %hello.client_version, "client connected");

    let descriptors: Vec<SourceDescriptor> = backend.descriptors().to_vec();
    write_message(
        &mut stream,
        &Message::HelloResponse(HelloResponse {
            daemon_name: identity.daemon_name.clone(),
            daemon_version: identity.daemon_version.clone(),
            protocol_version: PROTOCOL_VERSION.to_string(),
            node_id: identity.node_id.clone(),
            sources: descriptors.clone(),
        }),
    )?;

    let session_start = Instant::now();
    let mut sequence_numbers: BTreeMap<u32, u64> =
        descriptors.iter().map(|d| (d.local_source_id, 0)).collect();
    let mut batch_sequence_no: u64 = 0;

    loop {
        let request = match read_message(&mut stream) {
            Ok(message) => message,
            Err(WireError::ConnectionClosed) => return Ok(()),
            Err(err @ (WireError::Frame(_) | WireError::InvalidPayload { .. })) => {
                send_error(
                    &mut stream,
                    error_codes::PROTOCOL_VIOLATION,
                    err.to_string(),
                );
                return Ok(());
            }
            Err(err) => return Err(err),
        };

        match request {
            Message::StartCapture(start) => {
                let all_ids: Vec<u32> = descriptors.iter().map(|d| d.local_source_id).collect();
                let selected: Vec<u32> = if start.source_ids.is_empty() {
                    all_ids.clone()
                } else {
                    start.source_ids.clone()
                };
                if let Some(unknown) = selected.iter().find(|id| !all_ids.contains(id)) {
                    send_error(
                        &mut stream,
                        error_codes::UNKNOWN_SOURCE_ID,
                        format!("unknown source id {unknown}"),
                    );
                    continue;
                }

                write_message(
                    &mut stream,
                    &Message::StartCaptureAck(StartCaptureAck {
                        run_id: start.run_id.clone(),
                        accepted_source_ids: selected.clone(),
                    }),
                )?;

                let summary = run_capture(
                    &mut stream,
                    backend.as_mut(),
                    identity,
                    &start.run_id,
                    Duration::from_millis(start.interval_ms.max(1)),
                    &selected,
                    session_start,
                    &mut sequence_numbers,
                    &mut batch_sequence_no,
                )?;

                match summary {
                    CaptureEnd::Stopped {
                        batches_sent,
                        samples_sent,
                    } => {
                        write_message(
                            &mut stream,
                            &Message::StopCaptureAck(StopCaptureAck {
                                run_id: start.run_id,
                                batches_sent,
                                samples_sent,
                            }),
                        )?;
                    }
                    CaptureEnd::Disconnected => return Ok(()),
                    CaptureEnd::ProtocolViolation(kind) => {
                        send_error(
                            &mut stream,
                            error_codes::PROTOCOL_VIOLATION,
                            format!("unexpected {kind:?} during capture"),
                        );
                        return Ok(());
                    }
                }
            }
            Message::StopCapture(_) => {
                send_error(
                    &mut stream,
                    error_codes::NO_ACTIVE_CAPTURE,
                    "no capture is active",
                );
            }
            other => {
                send_error(
                    &mut stream,
                    error_codes::PROTOCOL_VIOLATION,
                    format!("unexpected {:?}", other.kind()),
                );
                return Ok(());
            }
        }
    }
}

enum CaptureEnd {
    Stopped {
        batches_sent: u64,
        samples_sent: u64,
    },
    Disconnected,
    ProtocolViolation(wattch_protocol::frame::FrameKind),
}

#[allow(clippy::too_many_arguments)]
fn run_capture(
    stream: &mut UnixStream,
    backend: &mut dyn SampleBackend,
    identity: &DaemonIdentity,
    run_id: &str,
    interval: Duration,
    selected: &[u32],
    session_start: Instant,
    sequence_numbers: &mut BTreeMap<u32, u64>,
    batch_sequence_no: &mut u64,
) -> Result<CaptureEnd, WireError> {
    let stop_flag = AtomicBool::new(false);
    // Tracked outside the producer closure so a mid-capture write error or
    // panic can't discard counts for batches that were already sent: the
    // ack must report what actually went out, regardless of how the
    // producer thread exited.
    let batches_sent_counter = AtomicU64::new(0);
    let samples_sent_counter = AtomicU64::new(0);
    let mut writer = stream.try_clone()?;
    let mut reader = stream.try_clone()?;

    thread::scope(|scope| {
        let producer = scope.spawn(|| -> Result<(), WireError> {
            loop {
                if stop_flag.load(Ordering::SeqCst) {
                    return Ok(());
                }

                let monotonic_ns = session_start.elapsed().as_nanos() as u64;
                let readings = backend.sample();
                let samples: Vec<RawSample> = readings
                    .into_iter()
                    .filter(|(id, _)| selected.contains(id))
                    .map(|(source_id, value)| {
                        let next = sequence_numbers.entry(source_id).or_insert(0);
                        let sequence_no = *next;
                        *next += 1;
                        RawSample {
                            source_id,
                            sequence_no,
                            monotonic_ns,
                            value,
                        }
                    })
                    .collect();

                let batch = RawSampleBatch {
                    run_id: run_id.to_string(),
                    node_id: identity.node_id.clone(),
                    batch_sequence_no: *batch_sequence_no,
                    samples,
                };
                *batch_sequence_no += 1;
                samples_sent_counter.fetch_add(batch.samples.len() as u64, Ordering::SeqCst);
                batches_sent_counter.fetch_add(1, Ordering::SeqCst);
                write_message(&mut writer, &Message::RawSampleBatch(batch))?;

                // Sleep in small slices so StopCapture latency stays low
                // even with long sampling intervals.
                let mut remaining = interval;
                while !remaining.is_zero() {
                    if stop_flag.load(Ordering::SeqCst) {
                        return Ok(());
                    }
                    let slice = remaining.min(Duration::from_millis(20));
                    thread::sleep(slice);
                    remaining -= slice;
                }
            }
        });

        let end = match read_message(&mut reader) {
            Ok(Message::StopCapture(_)) => None,
            Ok(other) => Some(CaptureEnd::ProtocolViolation(other.kind())),
            Err(_) => Some(CaptureEnd::Disconnected),
        };
        stop_flag.store(true, Ordering::SeqCst);

        // The producer's own Ok/Err/panic outcome is intentionally not
        // propagated here (unchanged from before); only how the counts are
        // obtained has changed, so they survive a mid-capture failure.
        let _ = producer.join();
        let batches_sent = batches_sent_counter.load(Ordering::SeqCst);
        let samples_sent = samples_sent_counter.load(Ordering::SeqCst);

        Ok(match end {
            Some(end) => end,
            None => CaptureEnd::Stopped {
                batches_sent,
                samples_sent,
            },
        })
    })
}

fn compatible_version(client_version: &str) -> bool {
    let prefix = |v: &str| {
        let mut parts = v.split('.');
        let major = parts.next().unwrap_or("").to_string();
        let minor = parts.next().unwrap_or("").to_string();
        (major, minor)
    };
    prefix(client_version) == prefix(PROTOCOL_VERSION)
}

#[cfg(test)]
mod version_tests {
    use super::compatible_version;
    use wattch_protocol::PROTOCOL_VERSION;

    // PROTOCOL_VERSION is "0.1.0" today; these tests pin the semantics
    // (same major+minor accepted regardless of patch, anything else
    // rejected) rather than the literal string, so they keep meaning if it
    // changes.

    #[test]
    fn identical_version_is_compatible() {
        assert!(compatible_version(PROTOCOL_VERSION));
    }

    #[test]
    fn same_major_minor_different_patch_is_compatible() {
        assert!(compatible_version("0.1.0"));
        assert!(compatible_version("0.1.99"));
    }

    #[test]
    fn older_minor_is_incompatible() {
        assert!(!compatible_version("0.0.9"));
    }

    #[test]
    fn newer_minor_is_incompatible() {
        assert!(!compatible_version("0.2.0"));
    }

    #[test]
    fn different_major_is_incompatible() {
        assert!(!compatible_version("1.1.0"));
    }

    #[test]
    fn malformed_version_is_incompatible() {
        assert!(!compatible_version(""));
        assert!(!compatible_version("garbage"));
    }
}

#[cfg(test)]
mod permission_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use wattch_core::{
        EvidenceClass, SamplingMode, SourceDescriptor, SourceKind, Temporality, Unit,
    };

    struct NoopBackend {
        descriptors: Vec<SourceDescriptor>,
    }

    impl NoopBackend {
        fn new() -> Self {
            Self {
                descriptors: vec![SourceDescriptor {
                    local_source_id: 1,
                    source_uid: "test://node/cpu/package/0/energy".to_string(),
                    node_id: "node".to_string(),
                    backend: "noop".to_string(),
                    kind: SourceKind::CpuPackageEnergy,
                    unit: Unit::Joule,
                    temporality: Temporality::Cumulative,
                    evidence_class: EvidenceClass::RawHardwareCounter,
                    source_path: "cpu/package/0/energy".to_string(),
                    parent_source_uid: None,
                    clock_domain: "monotonic".to_string(),
                    sampling_mode: SamplingMode::Synthetic,
                    nominal_period_ns: Some(1_000_000),
                    max_value: None,
                    resolution: None,
                    labels: BTreeMap::new(),
                }],
            }
        }
    }

    impl SampleBackend for NoopBackend {
        fn descriptors(&self) -> &[SourceDescriptor] {
            &self.descriptors
        }

        fn sample(&mut self) -> Vec<(u32, wattch_core::RawValue)> {
            Vec::new()
        }
    }

    fn identity() -> DaemonIdentity {
        DaemonIdentity {
            daemon_name: "test".to_string(),
            daemon_version: "0.0.0".to_string(),
            node_id: "node".to_string(),
        }
    }

    /// Short (AF_UNIX `sun_path` is capped at ~104 bytes on macOS/BSD), unique
    /// per call so parallel tests in this process don't collide.
    fn unique_temp_dir(tag: &str) -> PathBuf {
        static COUNTER: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        std::env::temp_dir().join(format!("wperm-{tag}-{}-{n}", std::process::id() % 10_000))
    }

    #[test]
    fn bind_creates_missing_parent_dir_mode_0750_and_socket_mode_0660() {
        let base = unique_temp_dir("op");
        std::fs::create_dir_all(&base).unwrap();
        let socket_dir = base.join("wattch");
        let socket_path = socket_dir.join("test.sock");
        assert!(!socket_dir.exists(), "precondition: parent must not exist");

        let handle = spawn(
            ServerConfig {
                socket_path: socket_path.clone(),
                identity: identity(),
            },
            || Box::new(NoopBackend::new()),
        )
        .expect("server should start and create its own runtime dir");

        let dir_mode = std::fs::metadata(&socket_dir).unwrap().permissions().mode() & 0o777;
        assert_eq!(dir_mode, 0o750, "created parent dir should be mode 0750");

        let socket_mode = std::fs::metadata(&socket_path)
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(socket_mode, 0o660, "bound socket should be mode 0660");

        handle.shutdown();
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn bind_fails_clearly_when_grandparent_is_missing() {
        let base = unique_temp_dir("mg");
        // Do not create `base` at all: base/wattch/test.sock has a missing
        // grandparent (`base`), so only-create-the-leaf-dir must fail.
        let socket_path = base.join("wattch").join("test.sock");

        let result = spawn(
            ServerConfig {
                socket_path,
                identity: identity(),
            },
            || Box::new(NoopBackend::new()),
        );

        match result {
            Err(ServerError::Io(_)) => {}
            Ok(_) => panic!("spawn must not silently create ancestor directory chains"),
            Err(other) => panic!("expected ServerError::Io, got {other}"),
        }
    }
}
