use crate::backend::SampleBackend;
use crate::peer::{peer_credentials, PeerPolicy};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, DirBuilder, Permissions};
use std::io::{self, Read};
use std::net::Shutdown;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use thiserror::Error;
use tracing::{debug, info, warn};
use wattch_core::{validate_source_set, RawSample, RawSampleBatch, SourceDescriptor};
use wattch_protocol::frame::WireError;
use wattch_protocol::messages::{
    error_codes, read_message, validate_record_event, validate_run_id, write_message, ErrorMessage,
    Event, EventBatch, HelloResponse, Message, RecordEventAck, StartCaptureAck, StopCaptureAck,
};
use wattch_protocol::PROTOCOL_VERSION;

#[derive(Clone, Debug)]
pub struct DaemonIdentity {
    pub daemon_name: String,
    pub daemon_version: String,
    pub node_id: String,
}

/// One monotonic epoch shared by every connection served by a daemon
/// instance. The opaque id lets offline consumers reject accidental joins
/// across daemon restarts without assigning wall-clock meaning to samples.
#[derive(Debug)]
struct DaemonClock {
    origin: Instant,
    instance_id: String,
}

impl DaemonClock {
    fn new() -> Self {
        static INSTANCE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

        let unix_nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let sequence = INSTANCE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        Self {
            origin: Instant::now(),
            instance_id: format!(
                "wattch-clock-{:x}-{unix_nanos:x}-{sequence:x}",
                std::process::id()
            ),
        }
    }

    fn now_ns(&self) -> u64 {
        self.origin.elapsed().as_nanos().min(u64::MAX as u128) as u64
    }

    fn timestamp_ns(&self, acquired_at: Instant) -> u64 {
        acquired_at
            .checked_duration_since(self.origin)
            .unwrap_or_default()
            .as_nanos()
            .min(u64::MAX as u128) as u64
    }
}

/// Where the listening socket comes from.
#[derive(Debug)]
pub enum SocketSource {
    /// Bind `path` ourselves: create the parent directory (0750), clean a
    /// stale socket, bind, chmod 0660, and unlink on shutdown.
    Bind(PathBuf),
    /// Serve on a listener bound by someone else (systemd socket
    /// activation). Mode, ownership, and lifecycle of the socket file
    /// belong to the activator: no chmod, no unlink on shutdown.
    Activated(UnixListener),
}

/// Not `Clone`: `SocketSource::Activated` holds a `UnixListener`.
#[derive(Debug)]
pub struct ServerConfig {
    pub socket: SocketSource,
    pub identity: DaemonIdentity,
    pub peer_policy: PeerPolicy,
    pub limits: ServerLimits,
}

/// Bounded defaults for untrusted local client sessions.
#[derive(Clone, Debug)]
pub struct ServerLimits {
    pub max_connections: usize,
    pub max_connections_per_uid: usize,
    pub max_active_captures: usize,
    pub max_active_captures_per_uid: usize,
    pub hello_timeout: Duration,
    pub session_idle_timeout: Duration,
    pub max_capture_duration: Duration,
    pub write_timeout: Duration,
    pub min_interval_ms: u64,
}

impl Default for ServerLimits {
    fn default() -> Self {
        Self {
            max_connections: 64,
            max_connections_per_uid: 16,
            max_active_captures: 8,
            max_active_captures_per_uid: 2,
            hello_timeout: Duration::from_secs(5),
            session_idle_timeout: Duration::from_secs(5 * 60),
            max_capture_duration: Duration::from_secs(24 * 60 * 60),
            write_timeout: Duration::from_secs(5),
            min_interval_ms: 10,
        }
    }
}

#[derive(Debug, Error)]
pub enum ServerError {
    #[error("invalid backend source descriptors: {0}")]
    InvalidDescriptors(#[from] wattch_core::Error),

    #[error("io error: {0}")]
    Io(#[from] io::Error),

    #[error("invalid server limits: {0}")]
    InvalidLimits(String),
}

#[derive(Debug, Default)]
struct PermitState {
    total: usize,
    per_uid: BTreeMap<u32, usize>,
}

#[derive(Debug)]
struct PermitPool {
    max_total: usize,
    max_per_uid: usize,
    state: Mutex<PermitState>,
}

impl PermitPool {
    fn new(max_total: usize, max_per_uid: usize) -> Arc<Self> {
        Arc::new(Self {
            max_total,
            max_per_uid,
            state: Mutex::new(PermitState::default()),
        })
    }

    fn try_acquire(self: &Arc<Self>, uid: Option<u32>) -> Option<Permit> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.total >= self.max_total {
            return None;
        }
        if let Some(uid) = uid {
            if state.per_uid.get(&uid).copied().unwrap_or(0) >= self.max_per_uid {
                return None;
            }
            *state.per_uid.entry(uid).or_default() += 1;
        }
        state.total += 1;
        drop(state);
        Some(Permit {
            pool: Arc::clone(self),
            uid,
        })
    }
}

#[derive(Debug)]
struct Permit {
    pool: Arc<PermitPool>,
    uid: Option<u32>,
}

impl Drop for Permit {
    fn drop(&mut self) {
        let mut state = self
            .pool
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.total = state.total.saturating_sub(1);
        if let Some(uid) = self.uid {
            if let Some(count) = state.per_uid.get_mut(&uid) {
                *count = count.saturating_sub(1);
                if *count == 0 {
                    state.per_uid.remove(&uid);
                }
            }
        }
    }
}

/// Handle to a running server. Dropping it does not stop the server;
/// call [`ServerHandle::shutdown`].
pub struct ServerHandle {
    socket_path: PathBuf,
    /// True when we bound the socket ourselves (`SocketSource::Bind`) and
    /// therefore own the socket file's lifecycle.
    owns_socket_file: bool,
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
        if self.owns_socket_file {
            let _ = std::fs::remove_file(&self.socket_path);
        }
    }

    /// Block until the server is shut down externally (for daemon mains).
    pub fn wait(self) {
        let _ = self.accept_thread.join();
        if self.owns_socket_file {
            let _ = std::fs::remove_file(&self.socket_path);
        }
    }
}

/// Validate the backend and start serving the Wattch protocol on a Unix
/// socket. Each connection gets its own backend instance from `factory`.
pub fn spawn<F>(config: ServerConfig, factory: F) -> Result<ServerHandle, ServerError>
where
    F: Fn() -> Box<dyn SampleBackend> + Send + Sync + 'static,
{
    validate_limits(&config.limits)?;
    let probe = factory();
    validate_source_set(probe.descriptors())?;
    drop(probe);

    let ServerConfig {
        socket,
        identity,
        peer_policy,
        limits,
    } = config;
    let (listener, socket_path, owns_socket_file) = match socket {
        SocketSource::Bind(path) => {
            ensure_socket_parent_dir(&path)?;

            if path.exists() {
                // A previous daemon may have crashed without unlinking its
                // socket. If nothing accepts connections there, the socket
                // is stale.
                if UnixStream::connect(&path).is_ok() {
                    return Err(ServerError::Io(io::Error::new(
                        io::ErrorKind::AddrInUse,
                        format!("another daemon is already listening on {}", path.display()),
                    )));
                }
                warn!(path = %path.display(), "removing stale socket");
                std::fs::remove_file(&path)?;
            }

            let listener = UnixListener::bind(&path)?;
            // energy_uj-derived samples are a documented side channel
            // (PLATYPUS, CVE-2020-8694); the socket must not be group/world
            // writable, and the owned parent directory (mode 0750, set
            // above) is the TOCTOU mitigation for the stale-socket probe
            // above.
            fs::set_permissions(&path, Permissions::from_mode(0o660))?;
            (listener, path, true)
        }
        SocketSource::Activated(listener) => {
            // systemd owns the socket file: its mode/ownership come from
            // the .socket unit and it is removed by RemoveOnStop=, so no
            // chmod and no unlink here. The path is still needed for the
            // shutdown wake-up connect and for logging.
            let addr = listener.local_addr()?;
            let Some(path) = addr.as_pathname() else {
                return Err(ServerError::Io(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "activated listener has no filesystem path \
                     (unnamed/abstract sockets are unsupported)",
                )));
            };
            let path = path.to_path_buf();
            (listener, path, false)
        }
    };
    info!(path = %socket_path.display(), "listening");

    let stop = Arc::new(AtomicBool::new(false));
    let accept_stop = Arc::clone(&stop);
    let factory = Arc::new(factory);
    let connection_pool = PermitPool::new(limits.max_connections, limits.max_connections_per_uid);
    let capture_pool = PermitPool::new(
        limits.max_active_captures,
        limits.max_active_captures_per_uid,
    );
    let daemon_clock = Arc::new(DaemonClock::new());
    #[cfg(not(target_os = "linux"))]
    warn!(
        "peer authentication unavailable on this platform; \
         dev builds only — production target is Linux"
    );

    let accept_thread = thread::spawn(move || {
        for connection in listener.incoming() {
            if accept_stop.load(Ordering::SeqCst) {
                break;
            }
            match connection {
                Ok(mut stream) => {
                    if let Err(err) = stream.set_write_timeout(Some(limits.write_timeout)) {
                        warn!(error = %err, "rejecting peer: cannot set write deadline");
                        continue;
                    }
                    // Kernel-verified peer check before any protocol I/O.
                    // A credential-read failure is a deny, not an allow;
                    // Ok(None) means this platform has no credential
                    // facility (non-Linux dev builds — warned at startup).
                    let peer_uid = match peer_credentials(&stream) {
                        Ok(None) => None,
                        Ok(Some(creds)) if peer_policy.permits(creds.uid, creds.gid) => {
                            Some(creds.uid)
                        }
                        Ok(Some(creds)) => {
                            warn!(
                                uid = creds.uid,
                                gid = creds.gid,
                                pid = creds.pid,
                                "rejecting peer: uid/gid not in allowed lists"
                            );
                            send_error(&mut stream, error_codes::ACCESS_DENIED, "access denied");
                            continue;
                        }
                        Err(err) => {
                            warn!(error = %err, "rejecting peer: cannot read credentials");
                            send_error(&mut stream, error_codes::ACCESS_DENIED, "access denied");
                            continue;
                        }
                    };
                    let Some(connection_permit) = connection_pool.try_acquire(peer_uid) else {
                        warn!(uid = peer_uid, "rejecting peer: connection limit reached");
                        send_error(
                            &mut stream,
                            error_codes::RESOURCE_EXHAUSTED,
                            "connection limit reached",
                        );
                        continue;
                    };
                    if let Err(err) = stream.set_read_timeout(Some(limits.hello_timeout)) {
                        warn!(error = %err, "rejecting peer: cannot set handshake timeout");
                        continue;
                    }
                    let identity = identity.clone();
                    let factory = Arc::clone(&factory);
                    let limits = limits.clone();
                    let capture_pool = Arc::clone(&capture_pool);
                    let daemon_clock = Arc::clone(&daemon_clock);
                    thread::spawn(move || {
                        let _connection_permit = connection_permit;
                        if let Err(err) = handle_connection(
                            stream,
                            &identity,
                            factory.as_ref(),
                            &limits,
                            &capture_pool,
                            peer_uid,
                            daemon_clock.as_ref(),
                        ) {
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
        owns_socket_file,
        stop,
        accept_thread,
    })
}

fn validate_limits(limits: &ServerLimits) -> Result<(), ServerError> {
    let counts = [
        ("max_connections", limits.max_connections),
        ("max_connections_per_uid", limits.max_connections_per_uid),
        ("max_active_captures", limits.max_active_captures),
        (
            "max_active_captures_per_uid",
            limits.max_active_captures_per_uid,
        ),
    ];
    if let Some((name, _)) = counts.into_iter().find(|(_, value)| *value == 0) {
        return Err(ServerError::InvalidLimits(format!(
            "{name} must be non-zero"
        )));
    }
    let durations = [
        ("hello_timeout", limits.hello_timeout),
        ("session_idle_timeout", limits.session_idle_timeout),
        ("max_capture_duration", limits.max_capture_duration),
        ("write_timeout", limits.write_timeout),
    ];
    if let Some((name, _)) = durations
        .into_iter()
        .find(|(_, duration)| duration.is_zero())
    {
        return Err(ServerError::InvalidLimits(format!(
            "{name} must be non-zero"
        )));
    }
    if limits.min_interval_ms == 0 {
        return Err(ServerError::InvalidLimits(
            "min_interval_ms must be non-zero".to_string(),
        ));
    }
    Ok(())
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
    factory: &(dyn Fn() -> Box<dyn SampleBackend> + Send + Sync),
    limits: &ServerLimits,
    capture_pool: &Arc<PermitPool>,
    peer_uid: Option<u32>,
    daemon_clock: &DaemonClock,
) -> Result<(), WireError> {
    let first = match read_message_before(&mut stream, limits.hello_timeout) {
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
        Err(err) if is_timeout(&err) => {
            send_error(&mut stream, error_codes::TIMEOUT, "hello deadline exceeded");
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

    let mut backend = factory();
    let descriptors: Vec<SourceDescriptor> = backend.descriptors().to_vec();
    write_message(
        &mut stream,
        &Message::HelloResponse(HelloResponse {
            daemon_name: identity.daemon_name.clone(),
            daemon_version: identity.daemon_version.clone(),
            protocol_version: PROTOCOL_VERSION.to_string(),
            node_id: identity.node_id.clone(),
            clock_instance_id: daemon_clock.instance_id.clone(),
            sources: descriptors.clone(),
        }),
    )?;

    let mut sequence_numbers: BTreeMap<u32, u64> =
        descriptors.iter().map(|d| (d.local_source_id, 0)).collect();
    let mut batch_sequence_no: u64 = 0;
    let mut event_batch_sequence_no: u64 = 0;

    loop {
        let request = match read_message_before(&mut stream, limits.session_idle_timeout) {
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
            Err(err) if is_timeout(&err) => {
                send_error(
                    &mut stream,
                    error_codes::TIMEOUT,
                    "session idle deadline exceeded",
                );
                return Ok(());
            }
            Err(err) => return Err(err),
        };

        match request {
            Message::StartCapture(start) => {
                if let Err(reason) = validate_run_id(&start.run_id) {
                    send_error(&mut stream, error_codes::INVALID_REQUEST, reason);
                    continue;
                }
                if start.interval_ms < limits.min_interval_ms {
                    send_error(
                        &mut stream,
                        error_codes::INVALID_REQUEST,
                        format!("interval_ms must be at least {}", limits.min_interval_ms),
                    );
                    continue;
                }

                let all_ids: BTreeSet<u32> =
                    descriptors.iter().map(|d| d.local_source_id).collect();
                let unique = if start.source_ids.is_empty() {
                    all_ids.clone()
                } else {
                    if start.source_ids.len() > all_ids.len() {
                        send_error(
                            &mut stream,
                            error_codes::INVALID_REQUEST,
                            "source_ids contains duplicates or too many entries",
                        );
                        continue;
                    }
                    let unique: BTreeSet<u32> = start.source_ids.iter().copied().collect();
                    if unique.len() != start.source_ids.len() {
                        send_error(
                            &mut stream,
                            error_codes::INVALID_REQUEST,
                            "source_ids must not contain duplicates",
                        );
                        continue;
                    }
                    if let Some(unknown) = unique.iter().find(|id| !all_ids.contains(id)) {
                        send_error(
                            &mut stream,
                            error_codes::UNKNOWN_SOURCE_ID,
                            format!("unknown source id {unknown}"),
                        );
                        continue;
                    }
                    unique
                };
                let selected: Vec<u32> = unique.iter().copied().collect();
                let Some(_capture_permit) = capture_pool.try_acquire(peer_uid) else {
                    send_error(
                        &mut stream,
                        error_codes::RESOURCE_EXHAUSTED,
                        "active capture limit reached",
                    );
                    continue;
                };

                backend.begin_capture();
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
                    Duration::from_millis(start.interval_ms),
                    &unique,
                    daemon_clock,
                    &mut sequence_numbers,
                    &mut batch_sequence_no,
                    &mut event_batch_sequence_no,
                    limits.max_capture_duration,
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
                    CaptureEnd::TimedOut => {
                        send_error(
                            &mut stream,
                            error_codes::TIMEOUT,
                            "maximum capture duration exceeded",
                        );
                        return Ok(());
                    }
                    CaptureEnd::ProtocolViolation(reason) => {
                        send_error(&mut stream, error_codes::PROTOCOL_VIOLATION, reason);
                        return Ok(());
                    }
                }
            }
            Message::StopCapture(stop) => {
                if validate_run_id(&stop.run_id).is_err() {
                    send_error(
                        &mut stream,
                        error_codes::INVALID_REQUEST,
                        "invalid StopCapture run_id",
                    );
                } else {
                    send_error(
                        &mut stream,
                        error_codes::NO_ACTIVE_CAPTURE,
                        "no capture is active",
                    );
                }
            }
            Message::RecordEvent(event) => {
                if let Err(reason) = validate_record_event(&event) {
                    send_error(&mut stream, error_codes::INVALID_REQUEST, reason);
                } else {
                    send_error(
                        &mut stream,
                        error_codes::NO_ACTIVE_CAPTURE,
                        "no capture is active",
                    );
                }
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
    TimedOut,
    ProtocolViolation(String),
}

#[allow(clippy::too_many_arguments)]
fn run_capture(
    stream: &mut UnixStream,
    backend: &mut dyn SampleBackend,
    identity: &DaemonIdentity,
    run_id: &str,
    interval: Duration,
    selected: &BTreeSet<u32>,
    daemon_clock: &DaemonClock,
    sequence_numbers: &mut BTreeMap<u32, u64>,
    batch_sequence_no: &mut u64,
    event_batch_sequence_no: &mut u64,
    max_capture_duration: Duration,
) -> Result<CaptureEnd, WireError> {
    let stop_flag = AtomicBool::new(false);
    // Tracked outside the producer closure so a normal StopCapture ack reports
    // only batches whose writes completed. Producer failure tears down the
    // connection instead of emitting a misleading partial acknowledgement.
    let batches_sent_counter = AtomicU64::new(0);
    let samples_sent_counter = AtomicU64::new(0);
    let writer = Arc::new(Mutex::new(stream.try_clone()?));
    let mut reader = stream.try_clone()?;
    let capture_deadline = Instant::now()
        .checked_add(max_capture_duration)
        .ok_or_else(|| WireError::Io(io::Error::other("capture deadline is too large")))?;

    thread::scope(|scope| {
        let producer = scope.spawn(|| -> Result<(), WireError> {
            let writer = Arc::clone(&writer);
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(
                || -> Result<(), WireError> {
                    loop {
                        if stop_flag.load(Ordering::SeqCst) {
                            return Ok(());
                        }

                        let readings = backend.sample_acquired();
                        let samples: Vec<RawSample> = readings
                            .into_iter()
                            .filter(|reading| selected.contains(&reading.local_source_id))
                            .map(|reading| {
                                let source_id = reading.local_source_id;
                                let next = sequence_numbers.entry(source_id).or_insert(0);
                                let sequence_no = *next;
                                *next += 1;
                                RawSample {
                                    source_id,
                                    sequence_no,
                                    monotonic_ns: daemon_clock.timestamp_ns(reading.acquired_at),
                                    value: reading.value,
                                }
                            })
                            .collect();

                        let sample_count = samples.len() as u64;
                        let batch = RawSampleBatch {
                            run_id: run_id.to_string(),
                            node_id: identity.node_id.clone(),
                            batch_sequence_no: *batch_sequence_no,
                            samples,
                        };
                        *batch_sequence_no += 1;
                        if let Err(err) = write_capture_messages(
                            writer.as_ref(),
                            &[Message::RawSampleBatch(batch)],
                        ) {
                            shutdown_capture_writer(writer.as_ref());
                            return Err(err);
                        }
                        samples_sent_counter.fetch_add(sample_count, Ordering::SeqCst);
                        batches_sent_counter.fetch_add(1, Ordering::SeqCst);

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
                },
            ));
            match result {
                Ok(result) => result,
                Err(_) => {
                    shutdown_capture_writer(writer.as_ref());
                    Err(WireError::Io(io::Error::other(
                        "capture producer thread panicked",
                    )))
                }
            }
        });

        let mut seen_event_ids = BTreeSet::new();
        let mut capture_write_error = None;
        let end = loop {
            let Some(remaining) = capture_deadline.checked_duration_since(Instant::now()) else {
                break Some(CaptureEnd::TimedOut);
            };

            match read_message_before(&mut reader, remaining) {
                Ok(Message::RecordEvent(request)) => {
                    if let Err(reason) = validate_record_event(&request) {
                        if let Err(err) = write_capture_messages(
                            writer.as_ref(),
                            &[Message::Error(ErrorMessage {
                                code: error_codes::INVALID_REQUEST,
                                message: reason.to_string(),
                            })],
                        ) {
                            capture_write_error = Some(err);
                            break Some(CaptureEnd::Disconnected);
                        }
                        continue;
                    }
                    if request.run_id != run_id {
                        break Some(CaptureEnd::ProtocolViolation(
                            "RecordEvent run_id does not match the active run".to_string(),
                        ));
                    }
                    if seen_event_ids.contains(&request.event_id) {
                        if let Err(err) = write_capture_messages(
                            writer.as_ref(),
                            &[Message::Error(ErrorMessage {
                                code: error_codes::INVALID_REQUEST,
                                message: "event_id must be unique within a capture".to_string(),
                            })],
                        ) {
                            capture_write_error = Some(err);
                            break Some(CaptureEnd::Disconnected);
                        }
                        continue;
                    }

                    let event = Event {
                        event_id: request.event_id.clone(),
                        run_id: run_id.to_string(),
                        node_id: identity.node_id.clone(),
                        monotonic_ns: daemon_clock.now_ns(),
                        kind: request.kind,
                        labels: request.labels,
                    };
                    let event_batch = EventBatch {
                        run_id: run_id.to_string(),
                        node_id: identity.node_id.clone(),
                        batch_sequence_no: *event_batch_sequence_no,
                        events: vec![event.clone()],
                    };
                    let acknowledgement = RecordEventAck {
                        event: event.clone(),
                    };
                    if let Err(err) = write_capture_messages(
                        writer.as_ref(),
                        &[
                            Message::EventBatch(event_batch),
                            Message::RecordEventAck(acknowledgement),
                        ],
                    ) {
                        capture_write_error = Some(err);
                        break Some(CaptureEnd::Disconnected);
                    }
                    *event_batch_sequence_no += 1;
                    seen_event_ids.insert(event.event_id);
                }
                Ok(Message::StopCapture(stop)) if validate_run_id(&stop.run_id).is_err() => {
                    break Some(CaptureEnd::ProtocolViolation(
                        "invalid StopCapture run_id".to_string(),
                    ));
                }
                Ok(Message::StopCapture(stop)) if stop.run_id == run_id => break None,
                Ok(Message::StopCapture(_)) => {
                    break Some(CaptureEnd::ProtocolViolation(
                        "StopCapture run_id does not match the active run".to_string(),
                    ));
                }
                Ok(other) => {
                    break Some(CaptureEnd::ProtocolViolation(format!(
                        "unexpected {:?} during capture",
                        other.kind()
                    )));
                }
                Err(err) if is_timeout(&err) => break Some(CaptureEnd::TimedOut),
                Err(_) => break Some(CaptureEnd::Disconnected),
            }
        };
        stop_flag.store(true, Ordering::SeqCst);
        if capture_write_error.is_some() {
            shutdown_capture_writer(writer.as_ref());
        }

        let producer_result = producer
            .join()
            .map_err(|_| WireError::Io(io::Error::other("capture producer thread panicked")))?;
        if let Some(err) = capture_write_error {
            return Err(err);
        }
        producer_result?;
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

/// Serialize complete capture-phase frames. The raw-sample producer and the
/// request reader both write to the socket, so cloned stream handles alone
/// are not sufficient to prevent byte-level frame interleaving.
fn write_capture_messages(
    writer: &Mutex<UnixStream>,
    messages: &[Message],
) -> Result<(), WireError> {
    let mut writer = writer
        .lock()
        .map_err(|_| WireError::Io(io::Error::other("capture writer lock poisoned")))?;
    for message in messages {
        write_message(&mut *writer, message)?;
    }
    Ok(())
}

fn shutdown_capture_writer(writer: &Mutex<UnixStream>) {
    if let Ok(writer) = writer.lock() {
        let _ = writer.shutdown(Shutdown::Both);
    }
}

fn is_timeout(error: &WireError) -> bool {
    matches!(
        error,
        WireError::Io(source)
            if matches!(source.kind(), io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock)
    )
}

struct DeadlineReader<'a> {
    stream: &'a mut UnixStream,
    deadline: Instant,
}

impl Read for DeadlineReader<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let Some(remaining) = self.deadline.checked_duration_since(Instant::now()) else {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "read deadline exceeded",
            ));
        };
        self.stream
            .set_read_timeout(Some(remaining.max(Duration::from_millis(1))))?;
        self.stream.read(buffer)
    }
}

fn read_message_before(stream: &mut UnixStream, timeout: Duration) -> Result<Message, WireError> {
    let deadline = Instant::now()
        .checked_add(timeout)
        .ok_or_else(|| WireError::Io(io::Error::other("read deadline is too large")))?;
    read_message(&mut DeadlineReader { stream, deadline })
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

    // PROTOCOL_VERSION is "0.2.0" today; these tests pin the semantics
    // (same major+minor accepted regardless of patch, anything else
    // rejected) rather than the literal string, so they keep meaning if it
    // changes.

    #[test]
    fn identical_version_is_compatible() {
        assert!(compatible_version(PROTOCOL_VERSION));
    }

    #[test]
    fn same_major_minor_different_patch_is_compatible() {
        assert!(compatible_version("0.2.0"));
        assert!(compatible_version("0.2.99"));
    }

    #[test]
    fn older_minor_is_incompatible() {
        assert!(!compatible_version("0.1.9"));
    }

    #[test]
    fn newer_minor_is_incompatible() {
        assert!(!compatible_version("0.3.0"));
    }

    #[test]
    fn different_major_is_incompatible() {
        assert!(!compatible_version("1.2.0"));
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
                socket: SocketSource::Bind(socket_path.clone()),
                identity: identity(),
                peer_policy: PeerPolicy::with_grants(&[], &[]),
                limits: Default::default(),
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
                socket: SocketSource::Bind(socket_path),
                identity: identity(),
                peer_policy: PeerPolicy::with_grants(&[], &[]),
                limits: Default::default(),
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
