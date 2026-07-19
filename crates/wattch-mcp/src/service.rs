use crate::capture::{spawn_capture, CaptureControl, CaptureSpec, TailCoverage};
use crate::status::{self, CaptureTerminalRecord, ManagedCaptureStatus, MCP_CAPTURE_ID_LABEL};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use wattch_cli::artifact::{inspect_run, read_manifest, read_sources, replay_events, replay_trace};
use wattch_cli::client::WattchClient;
use wattch_core::{validate_source_set, SourceDescriptor};

const MAX_ACTIVE_CAPTURES_HARD: usize = 8;
const MAX_INTERVAL_MS: u64 = 60_000;
const MIN_INTERVAL_MS: u64 = 10;
const MAX_SOURCE_IDS: usize = 4096;
const MAX_LABELS: usize = 48;
const MAX_LABEL_KEY_BYTES: usize = 128;
const MAX_LABEL_VALUE_BYTES: usize = 1024;
const MAX_DESCRIPTOR_FILE_BYTES: u64 = 16 * 1024 * 1024;
const MAX_PATH_BYTES: usize = 4096;
const MAX_CAPTURE_ID_BYTES: usize = 256;
const MAX_COMPLETED_CAPTURES: usize = 32;
const CLOCK_INSTANCE_ID_LABEL: &str = "wattch.clock_instance_id";

#[derive(Clone, Debug)]
pub struct ServiceConfig {
    pub default_socket: Option<PathBuf>,
    pub runs_dir: PathBuf,
    pub workspace_root: PathBuf,
    pub max_active_captures: usize,
    pub max_capture_duration: Duration,
    pub startup_timeout: Duration,
}

impl ServiceConfig {
    pub fn resolve(mut self) -> Result<Self, ServiceError> {
        if self.max_active_captures == 0 || self.max_active_captures > MAX_ACTIVE_CAPTURES_HARD {
            return Err(ServiceError::new(
                "INVALID_CONFIG",
                format!("max_active_captures must be between 1 and {MAX_ACTIVE_CAPTURES_HARD}"),
            ));
        }
        if self.max_capture_duration.is_zero() {
            return Err(ServiceError::new(
                "INVALID_CONFIG",
                "max_capture_duration must be positive",
            ));
        }
        if self.startup_timeout.is_zero() {
            return Err(ServiceError::new(
                "INVALID_CONFIG",
                "startup_timeout must be positive",
            ));
        }

        self.workspace_root = fs::canonicalize(&self.workspace_root).map_err(|error| {
            ServiceError::new(
                "INVALID_CONFIG",
                format!(
                    "failed to resolve workspace root {}: {error}",
                    self.workspace_root.display()
                ),
            )
        })?;

        let runs = if self.runs_dir.is_absolute() {
            self.runs_dir.clone()
        } else {
            self.workspace_root.join(&self.runs_dir)
        };
        reject_parent_components(&runs)?;
        fs::create_dir_all(&runs).map_err(|error| {
            ServiceError::new(
                "INVALID_CONFIG",
                format!(
                    "failed to create runs directory {}: {error}",
                    runs.display()
                ),
            )
        })?;
        self.runs_dir = fs::canonicalize(&runs).map_err(|error| {
            ServiceError::new(
                "INVALID_CONFIG",
                format!(
                    "failed to resolve runs directory {}: {error}",
                    runs.display()
                ),
            )
        })?;
        Ok(self)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ServiceError {
    pub code: &'static str,
    pub message: String,
    pub data: Option<Value>,
}

impl ServiceError {
    pub(crate) fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            data: None,
        }
    }

    pub(crate) fn with_data(mut self, data: Value) -> Self {
        self.data = Some(data);
        self
    }
}

impl fmt::Display for ServiceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ServiceError {}

#[derive(Default)]
struct ServiceState {
    next_capture: u64,
    shutting_down: bool,
    active: BTreeMap<String, CaptureControl>,
    completed: BTreeMap<String, Result<Value, ServiceError>>,
    completed_order: VecDeque<String>,
}

pub struct WattchService {
    config: ServiceConfig,
    state: Mutex<ServiceState>,
}

impl WattchService {
    pub fn new(config: ServiceConfig) -> Result<Self, ServiceError> {
        Ok(Self {
            config: config.resolve()?,
            state: Mutex::new(ServiceState::default()),
        })
    }

    pub fn sources(&self, arguments: Value) -> Result<Value, ServiceError> {
        require_empty_arguments(&arguments)?;
        let socket = self.resolve_socket()?;
        let client = WattchClient::connect(&socket).map_err(client_error)?;
        let hello = client.hello();
        let warnings = evidence_warnings(&hello.sources, &hello.daemon_name);
        Ok(json!({
            "daemon": {
                "name": hello.daemon_name,
                "version": hello.daemon_version,
                "protocol_version": hello.protocol_version,
                "node_id": hello.node_id,
                "clock_instance_id": hello.clock_instance_id,
                "socket": socket,
            },
            "source_count": hello.sources.len(),
            "sources": hello.sources,
            "warnings": warnings,
        }))
    }

    pub fn capture_start(&self, arguments: Value) -> Result<Value, ServiceError> {
        let args: CaptureStartArgs = parse_arguments(arguments)?;
        let interval_ms = args.interval_ms.unwrap_or(100);
        if !(MIN_INTERVAL_MS..=MAX_INTERVAL_MS).contains(&interval_ms) {
            return Err(ServiceError::new(
                "INVALID_ARGUMENT",
                format!("interval_ms must be between {MIN_INTERVAL_MS} and {MAX_INTERVAL_MS}"),
            ));
        }
        if args.source_ids.len() > MAX_SOURCE_IDS {
            return Err(ServiceError::new(
                "INVALID_ARGUMENT",
                format!("source_ids exceeds {MAX_SOURCE_IDS} entries"),
            ));
        }
        if args.source_ids.iter().collect::<BTreeSet<_>>().len() != args.source_ids.len() {
            return Err(ServiceError::new(
                "INVALID_ARGUMENT",
                "source_ids must not contain duplicates",
            ));
        }
        validate_labels(&args.labels)?;

        let out_root = self.resolve_output_root(args.out.as_deref())?;
        let socket = self.resolve_socket()?;
        let (capture_id, run_dir, ready) = {
            let mut state = self.lock_state();
            reap_finished_locked(&mut state);
            if state.shutting_down {
                return Err(ServiceError::new(
                    "SERVER_SHUTTING_DOWN",
                    "MCP capture service is shutting down",
                ));
            }
            if state.active.len() >= self.config.max_active_captures {
                return Err(ServiceError::new(
                    "CAPTURE_LIMIT",
                    format!(
                        "active capture limit {} reached",
                        self.config.max_active_captures
                    ),
                ));
            }
            state.next_capture = state.next_capture.saturating_add(1);
            let capture_id = minted_capture_id(state.next_capture);
            let run_id = args.run_id.unwrap_or_else(|| format!("mcp-{capture_id}"));
            validate_run_component(&run_id)?;
            if state.active.values().any(|active| active.run_id == run_id) {
                return Err(ServiceError::new(
                    "CAPTURE_EXISTS",
                    format!("run {run_id:?} is already active"),
                ));
            }
            let run_dir = out_root.join(&run_id);
            let spec = CaptureSpec {
                capture_id: capture_id.clone(),
                run_id: run_id.clone(),
                socket,
                out_root,
                interval_ms,
                source_ids: args.source_ids,
                labels: args.labels,
                max_duration: self.config.max_capture_duration,
            };
            let (control, ready) = spawn_capture(spec);
            state.active.insert(capture_id.clone(), control);
            (capture_id, run_dir, ready)
        };

        match ready.recv_timeout(self.config.startup_timeout) {
            Ok(Ok(mut value)) => {
                value["capture_id"] = Value::String(capture_id);
                Ok(value)
            }
            Ok(Err(error)) => {
                self.remove_and_join(&capture_id, false);
                Err(error)
            }
            Err(_) => {
                self.remove_and_join(&capture_id, true);
                Err(ServiceError::new(
                    "CAPTURE_START_TIMEOUT",
                    format!(
                        "capture did not become ready within {} ms",
                        self.config.startup_timeout.as_millis()
                    ),
                )
                .with_data(json!({"capture_id": capture_id, "run_dir": run_dir})))
            }
        }
    }

    pub fn capture_stop(&self, arguments: Value) -> Result<Value, ServiceError> {
        let args: CaptureStopArgs = parse_arguments(arguments)?;
        if args.capture_id.is_empty() || args.capture_id.len() > MAX_CAPTURE_ID_BYTES {
            return Err(ServiceError::new(
                "INVALID_ARGUMENT",
                format!("capture_id must be non-empty and at most {MAX_CAPTURE_ID_BYTES} bytes"),
            ));
        }
        let mut control = {
            let mut state = self.lock_state();
            reap_finished_locked(&mut state);
            if let Some(result) = take_completed_locked(&mut state, &args.capture_id) {
                return result;
            }
            state.active.remove(&args.capture_id).ok_or_else(|| {
                ServiceError::new(
                    "CAPTURE_NOT_FOUND",
                    format!("unknown active capture_id {:?}", args.capture_id),
                )
            })?
        };
        let _ = control.stop.send(());
        join_control(&mut control)
    }

    pub fn inspect(&self, arguments: Value) -> Result<Value, ServiceError> {
        let args: InspectArgs = parse_arguments(arguments)?;
        let run_dir = self.resolve_existing(&args.run)?;
        inspect_value(&run_dir)
    }

    pub fn validate(&self, arguments: Value) -> Result<Value, ServiceError> {
        let args: ValidateArgs = parse_arguments(arguments)?;
        let choices = usize::from(args.sources.is_some())
            + usize::from(args.input_path.is_some())
            + usize::from(args.run.is_some());
        if choices != 1 {
            return Err(ServiceError::new(
                "INVALID_ARGUMENT",
                "provide exactly one of sources, input_path, or run",
            ));
        }

        if let Some(run) = args.run {
            let run_dir = self.resolve_existing(&run)?;
            let manifest = read_manifest(&run_dir)
                .map_err(|error| ServiceError::new("INVALID_ARTIFACT", error.to_string()))?;
            let capture_status = capture_status(&run_dir, &manifest)?;
            validate_capture_status(&run_dir, &capture_status)?;
            let mut result = inspect_value(&run_dir)?;
            result["validation_kind"] = Value::String("run_artifact".to_string());
            result["valid"] = Value::Bool(true);
            return Ok(result);
        }

        let (sources, input) = if let Some(sources) = args.sources {
            (decode_sources(Value::Array(sources))?, "inline".to_string())
        } else {
            let path = self.resolve_existing(
                args.input_path
                    .as_deref()
                    .expect("validated one input choice"),
            )?;
            let raw = read_bounded_regular_file(&path, MAX_DESCRIPTOR_FILE_BYTES)?;
            let value: Value = serde_json::from_slice(&raw).map_err(|error| {
                ServiceError::new(
                    "INVALID_JSON",
                    format!("invalid JSON in {}: {error}", path.display()),
                )
            })?;
            let source_value = value.get("sources").cloned().ok_or_else(|| {
                ServiceError::new("INVALID_DESCRIPTOR_SET", "missing sources array")
            })?;
            (decode_sources(source_value)?, path.display().to_string())
        };
        if sources.len() > MAX_SOURCE_IDS {
            return Err(ServiceError::new(
                "INVALID_DESCRIPTOR_SET",
                format!("sources exceeds {MAX_SOURCE_IDS} entries"),
            ));
        }
        validate_source_set(&sources)
            .map_err(|error| ServiceError::new("INVALID_DESCRIPTOR_SET", error.to_string()))?;
        Ok(json!({
            "valid": true,
            "validation_kind": "source_descriptors",
            "input": input,
            "source_count": sources.len(),
        }))
    }

    pub fn shutdown_all(&self) {
        let controls = {
            let mut state = self.lock_state();
            reap_finished_locked(&mut state);
            state.shutting_down = true;
            std::mem::take(&mut state.active)
        };
        for control in controls.values() {
            let _ = control.stop.send(());
        }
        let completed = controls
            .into_iter()
            .map(|(capture_id, mut control)| (capture_id, join_control(&mut control)))
            .collect::<Vec<_>>();
        let mut state = self.lock_state();
        for (capture_id, result) in completed {
            cache_completed_locked(&mut state, capture_id, result);
        }
    }

    pub fn workspace_root(&self) -> &Path {
        &self.config.workspace_root
    }

    pub fn runs_dir(&self) -> &Path {
        &self.config.runs_dir
    }

    fn resolve_socket(&self) -> Result<PathBuf, ServiceError> {
        if let Some(socket) = &self.config.default_socket {
            return Ok(socket.clone());
        }
        let mut candidates =
            wattch_protocol::socket::default_connect_candidates("wattch-powerd.sock");
        candidates.extend(wattch_protocol::socket::default_connect_candidates(
            "wattch-testd.sock",
        ));
        candidates
            .iter()
            .find(|path| path.exists())
            .cloned()
            .ok_or_else(|| {
                ServiceError::new(
                    "DAEMON_NOT_FOUND",
                    format!(
                        "no Wattch daemon socket found; tried {}",
                        candidates
                            .iter()
                            .map(|path| path.display().to_string())
                            .collect::<Vec<_>>()
                            .join(", ")
                    ),
                )
            })
    }

    fn resolve_output_root(&self, requested: Option<&str>) -> Result<PathBuf, ServiceError> {
        let Some(requested) = requested else {
            return Ok(self.config.runs_dir.clone());
        };
        let path = self.absolute_requested(requested)?;
        self.require_safe_creation_path(&path)?;
        fs::create_dir_all(&path).map_err(|error| {
            ServiceError::new(
                "OUTPUT_IO",
                format!("failed to create output root {}: {error}", path.display()),
            )
        })?;
        let canonical = fs::canonicalize(&path).map_err(|error| {
            ServiceError::new(
                "OUTPUT_IO",
                format!("failed to resolve output root {}: {error}", path.display()),
            )
        })?;
        self.require_allowed(&canonical)?;
        Ok(canonical)
    }

    fn require_safe_creation_path(&self, path: &Path) -> Result<(), ServiceError> {
        // Check the lexical target before creating anything, then resolve the
        // deepest existing ancestor so an in-root symlink cannot redirect
        // create_dir_all outside the configured file boundary.
        self.require_allowed(path)?;
        let mut ancestor = path;
        while !ancestor.exists() {
            ancestor = ancestor.parent().ok_or_else(|| {
                ServiceError::new(
                    "INVALID_PATH",
                    format!("{} has no existing ancestor", path.display()),
                )
            })?;
        }
        let canonical = fs::canonicalize(ancestor).map_err(|error| {
            ServiceError::new(
                "OUTPUT_IO",
                format!(
                    "failed to resolve output ancestor {}: {error}",
                    ancestor.display()
                ),
            )
        })?;
        self.require_allowed(&canonical)
    }

    fn resolve_existing(&self, requested: &str) -> Result<PathBuf, ServiceError> {
        let path = self.absolute_requested(requested)?;
        let canonical = fs::canonicalize(&path).map_err(|error| {
            ServiceError::new(
                "INPUT_IO",
                format!("failed to resolve {}: {error}", path.display()),
            )
        })?;
        self.require_allowed(&canonical)?;
        Ok(canonical)
    }

    fn absolute_requested(&self, requested: &str) -> Result<PathBuf, ServiceError> {
        if requested.is_empty() {
            return Err(ServiceError::new("INVALID_PATH", "path must not be empty"));
        }
        if requested.len() > MAX_PATH_BYTES {
            return Err(ServiceError::new(
                "INVALID_PATH",
                format!("path exceeds {MAX_PATH_BYTES} bytes"),
            ));
        }
        let path = Path::new(requested);
        reject_parent_components(path)?;
        Ok(if path.is_absolute() {
            path.to_path_buf()
        } else {
            self.config.workspace_root.join(path)
        })
    }

    fn require_allowed(&self, path: &Path) -> Result<(), ServiceError> {
        if path.starts_with(&self.config.workspace_root) || path.starts_with(&self.config.runs_dir)
        {
            return Ok(());
        }
        Err(ServiceError::new(
            "PATH_OUTSIDE_ROOT",
            format!(
                "{} is outside configured workspace {} and runs directory {}",
                path.display(),
                self.config.workspace_root.display(),
                self.config.runs_dir.display()
            ),
        ))
    }

    fn remove_and_join(&self, capture_id: &str, request_stop: bool) {
        let control = {
            let mut state = self.lock_state();
            let control = state.active.remove(capture_id);
            let _ = take_completed_locked(&mut state, capture_id);
            control
        };
        if let Some(mut control) = control {
            if request_stop {
                let _ = control.stop.send(());
            }
            let _ = join_control(&mut control);
        }
    }

    fn lock_state(&self) -> std::sync::MutexGuard<'_, ServiceState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

fn join_control(control: &mut CaptureControl) -> Result<Value, ServiceError> {
    control
        .join
        .take()
        .expect("active capture always owns its join handle")
        .join()
        .map_err(|_| ServiceError::new("CAPTURE_PANIC", "capture worker panicked"))?
}

fn reap_finished_locked(state: &mut ServiceState) {
    let finished = state
        .active
        .iter()
        .filter(|(_, control)| {
            control
                .join
                .as_ref()
                .is_some_and(std::thread::JoinHandle::is_finished)
        })
        .map(|(capture_id, _)| capture_id.clone())
        .collect::<Vec<_>>();
    for capture_id in finished {
        if let Some(mut control) = state.active.remove(&capture_id) {
            let result = join_control(&mut control);
            cache_completed_locked(state, capture_id, result);
        }
    }
}

fn cache_completed_locked(
    state: &mut ServiceState,
    capture_id: String,
    result: Result<Value, ServiceError>,
) {
    while state.completed.len() >= MAX_COMPLETED_CAPTURES {
        let Some(oldest) = state.completed_order.pop_front() else {
            break;
        };
        state.completed.remove(&oldest);
    }
    state.completed_order.push_back(capture_id.clone());
    state.completed.insert(capture_id, result);
}

fn take_completed_locked(
    state: &mut ServiceState,
    capture_id: &str,
) -> Option<Result<Value, ServiceError>> {
    let result = state.completed.remove(capture_id)?;
    if let Some(index) = state
        .completed_order
        .iter()
        .position(|candidate| candidate == capture_id)
    {
        state.completed_order.remove(index);
    }
    Some(result)
}

impl Drop for WattchService {
    fn drop(&mut self) {
        self.shutdown_all();
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CaptureStartArgs {
    #[serde(default)]
    run_id: Option<String>,
    #[serde(default)]
    out: Option<String>,
    #[serde(default)]
    interval_ms: Option<u64>,
    #[serde(default)]
    source_ids: Vec<u32>,
    #[serde(default)]
    labels: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CaptureStopArgs {
    capture_id: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct InspectArgs {
    run: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ValidateArgs {
    #[serde(default)]
    sources: Option<Vec<Value>>,
    #[serde(default)]
    input_path: Option<String>,
    #[serde(default)]
    run: Option<String>,
}

fn parse_arguments<T: for<'de> Deserialize<'de>>(value: Value) -> Result<T, ServiceError> {
    serde_json::from_value(value)
        .map_err(|error| ServiceError::new("INVALID_ARGUMENT", error.to_string()))
}

fn require_empty_arguments(value: &Value) -> Result<(), ServiceError> {
    match value {
        Value::Object(map) if map.is_empty() => Ok(()),
        _ => Err(ServiceError::new(
            "INVALID_ARGUMENT",
            "this tool accepts no arguments",
        )),
    }
}

fn decode_sources(value: Value) -> Result<Vec<SourceDescriptor>, ServiceError> {
    serde_json::from_value(value)
        .map_err(|error| ServiceError::new("INVALID_DESCRIPTOR_SET", error.to_string()))
}

fn validate_labels(labels: &BTreeMap<String, String>) -> Result<(), ServiceError> {
    if labels.len() > MAX_LABELS {
        return Err(ServiceError::new(
            "INVALID_ARGUMENT",
            format!("labels exceeds {MAX_LABELS} entries"),
        ));
    }
    for (key, value) in labels {
        if key.trim().is_empty()
            || key.len() > MAX_LABEL_KEY_BYTES
            || key.chars().any(char::is_control)
        {
            return Err(ServiceError::new(
                "INVALID_ARGUMENT",
                format!(
                    "label key must be non-empty, contain no controls, and be at most {MAX_LABEL_KEY_BYTES} bytes"
                ),
            ));
        }
        if value.len() > MAX_LABEL_VALUE_BYTES || value.chars().any(char::is_control) {
            return Err(ServiceError::new(
                "INVALID_ARGUMENT",
                format!(
                    "label value for {key:?} must contain no controls and be at most {MAX_LABEL_VALUE_BYTES} bytes"
                ),
            ));
        }
    }
    if labels.contains_key(CLOCK_INSTANCE_ID_LABEL) || labels.contains_key(MCP_CAPTURE_ID_LABEL) {
        return Err(ServiceError::new(
            "INVALID_ARGUMENT",
            format!("labels {CLOCK_INSTANCE_ID_LABEL:?} and {MCP_CAPTURE_ID_LABEL:?} are reserved"),
        ));
    }
    Ok(())
}

fn validate_run_component(run_id: &str) -> Result<(), ServiceError> {
    wattch_protocol::messages::validate_run_id(run_id)
        .map_err(|reason| ServiceError::new("INVALID_ARGUMENT", reason))?;
    let mut components = Path::new(run_id).components();
    let valid = matches!(components.next(), Some(Component::Normal(_)))
        && components.next().is_none()
        && !run_id.contains('\\');
    if !valid {
        return Err(ServiceError::new(
            "INVALID_ARGUMENT",
            "run_id must be one portable filename component",
        ));
    }
    Ok(())
}

fn reject_parent_components(path: &Path) -> Result<(), ServiceError> {
    if path
        .components()
        .any(|component| component == Component::ParentDir)
    {
        return Err(ServiceError::new(
            "INVALID_PATH",
            format!("{} contains a parent-directory component", path.display()),
        ));
    }
    Ok(())
}

fn read_bounded_regular_file(path: &Path, limit: u64) -> Result<Vec<u8>, ServiceError> {
    let initial_metadata = fs::symlink_metadata(path).map_err(|error| {
        ServiceError::new(
            "INPUT_IO",
            format!("failed to inspect {}: {error}", path.display()),
        )
    })?;
    if !initial_metadata.file_type().is_file() {
        return Err(ServiceError::new(
            "INVALID_INPUT_TYPE",
            format!("{} is not a regular file", path.display()),
        ));
    }
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW)
        .open(path)
        .map_err(|error| {
            ServiceError::new(
                "INPUT_IO",
                format!("failed to open {}: {error}", path.display()),
            )
        })?;
    let metadata = file.metadata().map_err(|error| {
        ServiceError::new(
            "INPUT_IO",
            format!("failed to inspect {}: {error}", path.display()),
        )
    })?;
    if !metadata.file_type().is_file() {
        return Err(ServiceError::new(
            "INVALID_INPUT_TYPE",
            format!("{} is not a regular file", path.display()),
        ));
    }
    if metadata.len() > limit {
        return Err(ServiceError::new(
            "INPUT_TOO_LARGE",
            format!(
                "{} is {} bytes; limit is {limit}",
                path.display(),
                metadata.len()
            ),
        ));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.by_ref()
        .take(limit.saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|error| {
            ServiceError::new(
                "INPUT_IO",
                format!("failed to read {}: {error}", path.display()),
            )
        })?;
    if bytes.len() as u64 > limit {
        return Err(ServiceError::new(
            "INPUT_TOO_LARGE",
            format!(
                "{} exceeded the {limit}-byte limit while reading",
                path.display()
            ),
        ));
    }
    Ok(bytes)
}

fn minted_capture_id(sequence: u64) -> String {
    let unix_ns = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    format!("cap-{unix_ns:x}-{:x}-{sequence:x}", std::process::id())
}

fn client_error(error: wattch_cli::client::ClientError) -> ServiceError {
    ServiceError::new("DAEMON_CLIENT", error.to_string())
}

pub(crate) fn inspect_value(run_dir: &Path) -> Result<Value, ServiceError> {
    let manifest = read_manifest(run_dir)
        .map_err(|error| ServiceError::new("INVALID_ARTIFACT", error.to_string()))?;
    let sources = read_sources(run_dir)
        .map_err(|error| ServiceError::new("INVALID_ARTIFACT", error.to_string()))?;
    let capture_status = capture_status(run_dir, &manifest)?;
    let report = inspect_run(run_dir).map_err(|error| {
        ServiceError::new("INVALID_ARTIFACT", error.to_string()).with_data(json!({
            "trace_valid": false,
            "capture_status": capture_status.as_value(),
        }))
    })?;
    let source_map = sources
        .iter()
        .map(|source| (source.local_source_id, source))
        .collect::<BTreeMap<_, _>>();
    let per_source = report
        .per_source
        .iter()
        .map(|(source_id, stats)| {
            let source = source_map.get(source_id);
            json!({
                "source_id": source_id,
                "source_uid": source.map(|value| value.source_uid.as_str()),
                "source_path": source.map(|value| value.source_path.as_str()),
                "backend": source.map(|value| value.backend.as_str()),
                "kind": source.map(|value| &value.kind),
                "unit": source.map(|value| &value.unit),
                "temporality": source.map(|value| &value.temporality),
                "evidence_class": source.map(|value| &value.evidence_class),
                "sampling_mode": source.map(|value| &value.sampling_mode),
                "samples": stats.samples,
                "first_sequence_no": stats.first_sequence_no,
                "last_sequence_no": stats.last_sequence_no,
                "sequence_gaps": stats.sequence_gaps,
                "first_monotonic_ns": stats.first_monotonic_ns,
                "last_monotonic_ns": stats.last_monotonic_ns,
                "span_ns": stats.last_monotonic_ns.saturating_sub(stats.first_monotonic_ns),
            })
        })
        .collect::<Vec<_>>();
    let mut warnings = evidence_warnings(&sources, "");
    if report
        .per_source
        .values()
        .any(|source| source.sequence_gaps > 0)
    {
        warnings.push(
            "sequence gaps are present; preserve them as evidence-quality signals".to_string(),
        );
    }
    match &capture_status {
        ManagedCaptureStatus::Capturing(_) => {
            warnings.push("MCP capture has no terminal record and is not finalized".to_string())
        }
        ManagedCaptureStatus::Incomplete { .. } => warnings.push(
            "MCP capture ended incomplete; integrity statistics are diagnostic only".to_string(),
        ),
        ManagedCaptureStatus::Complete {
            terminal:
                CaptureTerminalRecord::Complete {
                    tail_complete: false,
                    tail_incomplete_source_ids,
                    ..
                },
            ..
        } => warnings.push(format!(
            "post-END endpoint coverage is incomplete for source ids {tail_incomplete_source_ids:?}"
        )),
        _ => {}
    }
    let lifecycle_valid = capture_status.is_lifecycle_valid();
    Ok(json!({
        "valid": lifecycle_valid,
        "trace_valid": true,
        "capture_status": capture_status.as_value(),
        "run_dir": run_dir,
        "run_id": report.run_id,
        "node_id": report.node_id,
        "protocol_version": manifest.protocol_version,
        "clock_instance_id": manifest.clock_instance_id,
        "labels": manifest.labels,
        "batches": report.batches,
        "samples": report.samples,
        "event_batches": report.event_batches,
        "events": report.events,
        "per_event_kind": report.per_event_kind,
        "per_source": per_source,
        "warnings": warnings,
        "interpretation": "raw acquisition and integrity summary only; no joules, aggregation, attribution, or optimization claim",
    }))
}

fn capture_status(
    run_dir: &Path,
    manifest: &wattch_core::RunManifest,
) -> Result<ManagedCaptureStatus, ServiceError> {
    status::read_status(
        run_dir,
        &manifest.run_id,
        manifest
            .labels
            .get(MCP_CAPTURE_ID_LABEL)
            .map(String::as_str),
    )
    .map_err(|error| ServiceError::new("INVALID_ARTIFACT", error.to_string()))
}

fn validate_capture_status(
    run_dir: &Path,
    capture_status: &ManagedCaptureStatus,
) -> Result<(), ServiceError> {
    let terminal = match capture_status {
        ManagedCaptureStatus::Unmanaged => return Ok(()),
        ManagedCaptureStatus::Capturing(_) => {
            return Err(ServiceError::new(
                "CAPTURE_NOT_FINALIZED",
                "MCP capture has no terminal status record",
            ))
        }
        ManagedCaptureStatus::Incomplete { terminal, .. } => {
            return Err(ServiceError::new(
                "CAPTURE_INCOMPLETE",
                "MCP capture terminal status is incomplete",
            )
            .with_data(json!({"capture_status": terminal})))
        }
        ManagedCaptureStatus::Complete { terminal, .. } => terminal,
    };
    let CaptureTerminalRecord::Complete {
        termination_reason,
        interval_ms,
        selected_source_ids,
        start_event_id,
        start_monotonic_ns,
        end_event_id,
        end_monotonic_ns,
        tail_complete,
        tail_target_monotonic_ns,
        tail_last_monotonic_ns,
        tail_incomplete_source_ids,
        batches_sent,
        samples_sent,
        ..
    } = terminal
    else {
        return Err(ServiceError::new(
            "INVALID_ARTIFACT",
            "complete capture status contains a non-complete terminal record",
        ));
    };
    if !matches!(
        termination_reason.as_str(),
        "stopped" | "server_shutdown" | "max_duration_reached"
    ) || end_monotonic_ns < start_monotonic_ns
        || !(MIN_INTERVAL_MS..=MAX_INTERVAL_MS).contains(interval_ms)
        || selected_source_ids.is_empty()
    {
        return Err(ServiceError::new(
            "INVALID_ARTIFACT",
            "MCP capture terminal lifecycle fields are invalid",
        ));
    }

    let mut starts = Vec::new();
    let mut ends = Vec::new();
    replay_events(run_dir, |batch| {
        for event in batch.events {
            if event.kind == "WATTCH_WINDOW_START" {
                starts.push((event.event_id, event.monotonic_ns));
            } else if event.kind == "WATTCH_WINDOW_END" {
                ends.push((event.event_id, event.monotonic_ns));
            }
        }
    })
    .map_err(|error| ServiceError::new("INVALID_ARTIFACT", error.to_string()))?;
    if starts.as_slice() != [(start_event_id.clone(), *start_monotonic_ns)]
        || ends.as_slice() != [(end_event_id.clone(), *end_monotonic_ns)]
    {
        return Err(ServiceError::new(
            "INVALID_ARTIFACT",
            "MCP capture terminal record does not match exactly one START and END event",
        ));
    }

    let report = inspect_run(run_dir)
        .map_err(|error| ServiceError::new("INVALID_ARTIFACT", error.to_string()))?;
    if report.batches != *batches_sent || report.samples != *samples_sent {
        return Err(ServiceError::new(
            "INVALID_ARTIFACT",
            format!(
                "terminal daemon counts ({batches_sent} batches, {samples_sent} samples) do not match trace counts ({} batches, {} samples)",
                report.batches, report.samples
            ),
        ));
    }

    let selected_id_set = selected_source_ids.iter().copied().collect::<BTreeSet<_>>();
    if selected_id_set.len() != selected_source_ids.len() {
        return Err(ServiceError::new(
            "INVALID_ARTIFACT",
            "terminal selected_source_ids contains duplicates",
        ));
    }
    let sources = read_sources(run_dir)
        .map_err(|error| ServiceError::new("INVALID_ARTIFACT", error.to_string()))?;
    let source_map = sources
        .iter()
        .map(|source| (source.local_source_id, source))
        .collect::<BTreeMap<_, _>>();
    let selected = selected_source_ids
        .iter()
        .map(|source_id| {
            source_map.get(source_id).cloned().cloned().ok_or_else(|| {
                ServiceError::new(
                    "INVALID_ARTIFACT",
                    format!("terminal references unknown selected source id {source_id}"),
                )
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut coverage = TailCoverage::new(&selected, *interval_ms, *end_monotonic_ns);
    let mut unexpected_source_ids = BTreeSet::new();
    replay_trace(run_dir, |batch| {
        unexpected_source_ids.extend(
            batch
                .samples
                .iter()
                .map(|sample| sample.source_id)
                .filter(|source_id| !selected_id_set.contains(source_id)),
        );
        coverage.observe(&batch);
    })
    .map_err(|error| ServiceError::new("INVALID_ARTIFACT", error.to_string()))?;
    let recorded_incomplete = tail_incomplete_source_ids
        .iter()
        .copied()
        .collect::<BTreeSet<_>>();
    let expected_incomplete = coverage
        .incomplete_source_ids()
        .into_iter()
        .collect::<BTreeSet<_>>();
    if !unexpected_source_ids.is_empty()
        || recorded_incomplete.len() != tail_incomplete_source_ids.len()
        || recorded_incomplete != expected_incomplete
        || *tail_complete != coverage.complete()
        || *tail_target_monotonic_ns != coverage.target_ns()
        || *tail_last_monotonic_ns != coverage.last_ns()
        || *tail_last_monotonic_ns < *end_monotonic_ns
    {
        return Err(ServiceError::new(
            "INVALID_ARTIFACT",
            "terminal tail fields do not match descriptor-bound raw trace evidence",
        )
        .with_data(json!({
            "unexpected_source_ids": unexpected_source_ids,
            "expected_tail_complete": coverage.complete(),
            "expected_tail_target_monotonic_ns": coverage.target_ns(),
            "expected_tail_last_monotonic_ns": coverage.last_ns(),
            "expected_tail_incomplete_source_ids": expected_incomplete,
        })));
    }
    Ok(())
}

fn evidence_warnings(sources: &[SourceDescriptor], daemon_name: &str) -> Vec<String> {
    let mut warnings = Vec::new();
    if daemon_name == "wattch-testd"
        || sources
            .iter()
            .any(|source| source.backend == "deterministic")
    {
        warnings.push(
            "deterministic/testd telemetry validates plumbing only and does not measure workload energy"
                .to_string(),
        );
    }
    if sources.iter().any(|source| {
        matches!(
            source.temporality,
            wattch_core::Temporality::IntervalAverage
        )
    }) {
        warnings.push(
            "INTERVAL_AVERAGE evidence requires descriptor-linked offline interval reconstruction"
                .to_string(),
        );
    }
    warnings.push(
        "do not sum nested or independent sources unless an offline measurement boundary proves disjointness"
            .to_string(),
    );
    warnings
}

#[cfg(test)]
mod tests {
    use super::*;
    use wattch_cli::artifact::RunWriter;
    use wattch_core::{EvidenceClass, RunManifest, SamplingMode, SourceKind, Temporality, Unit};

    fn config(root: &Path) -> ServiceConfig {
        ServiceConfig {
            default_socket: None,
            runs_dir: root.join("runs"),
            workspace_root: root.to_path_buf(),
            max_active_captures: 1,
            max_capture_duration: Duration::from_secs(5),
            startup_timeout: Duration::from_secs(1),
        }
    }

    #[test]
    fn path_policy_rejects_parent_traversal() {
        let root = std::env::temp_dir().join(format!("wattch-mcp-path-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let service = WattchService::new(config(&root)).unwrap();
        let error = service.resolve_existing("../outside").unwrap_err();
        assert_eq!(error.code, "INVALID_PATH");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn output_policy_rejects_outside_and_symlink_targets_before_creation() {
        use std::os::unix::fs::symlink;

        let base =
            std::env::temp_dir().join(format!("wattch-mcp-output-policy-{}", std::process::id()));
        let workspace = base.join("workspace");
        let outside = base.join("outside");
        fs::create_dir_all(&workspace).unwrap();
        fs::create_dir_all(&outside).unwrap();
        let service = WattchService::new(config(&workspace)).unwrap();

        let direct = outside.join("must-not-exist");
        let error = service
            .resolve_output_root(Some(direct.to_str().unwrap()))
            .unwrap_err();
        assert_eq!(error.code, "PATH_OUTSIDE_ROOT");
        assert!(!direct.exists());

        let escape = workspace.join("escape");
        symlink(&outside, &escape).unwrap();
        let redirected = escape.join("must-not-exist");
        let error = service
            .resolve_output_root(Some(redirected.to_str().unwrap()))
            .unwrap_err();
        assert_eq!(error.code, "PATH_OUTSIDE_ROOT");
        assert!(!outside.join("must-not-exist").exists());

        drop(service);
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn labels_are_bounded_and_clock_label_is_reserved() {
        assert!(validate_labels(&BTreeMap::new()).is_ok());
        assert!(validate_labels(&BTreeMap::from([(
            CLOCK_INSTANCE_ID_LABEL.to_string(),
            "forged".to_string()
        )]))
        .is_err());
    }

    #[test]
    fn run_ids_are_portable_components() {
        assert!(validate_run_component("agent-luna-skill-r01").is_ok());
        assert!(validate_run_component("../escape").is_err());
        assert!(validate_run_component("nested/run").is_err());
    }

    #[test]
    fn inspect_reports_and_validate_rejects_nonterminal_and_incomplete_captures() {
        let root =
            std::env::temp_dir().join(format!("wattch-mcp-incomplete-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let service = WattchService::new(config(&root)).unwrap();
        let capture_id = "capture-incomplete";
        let run_id = "incomplete-run";
        let source = SourceDescriptor {
            local_source_id: 1,
            source_uid: "test://incomplete/cpu/energy".to_string(),
            node_id: "incomplete-node".to_string(),
            backend: "test".to_string(),
            kind: SourceKind::CpuPackageEnergy,
            unit: Unit::Microjoule,
            temporality: Temporality::Cumulative,
            evidence_class: EvidenceClass::RawHardwareCounter,
            source_path: "cpu/energy".to_string(),
            parent_source_uid: None,
            clock_domain: "monotonic".to_string(),
            sampling_mode: SamplingMode::Synthetic,
            nominal_period_ns: Some(10_000_000),
            max_value: None,
            resolution: None,
            labels: BTreeMap::new(),
        };
        let manifest = RunManifest {
            run_id: run_id.to_string(),
            node_id: "incomplete-node".to_string(),
            protocol_version: wattch_protocol::PROTOCOL_VERSION.to_string(),
            clock_instance_id: "incomplete-clock".to_string(),
            wattch_version: env!("CARGO_PKG_VERSION").to_string(),
            created_unix_ns: 1,
            sources: vec![source],
            labels: BTreeMap::from([
                (
                    CLOCK_INSTANCE_ID_LABEL.to_string(),
                    "incomplete-clock".to_string(),
                ),
                (MCP_CAPTURE_ID_LABEL.to_string(), capture_id.to_string()),
            ]),
        };
        let writer = RunWriter::create(service.runs_dir(), &manifest).unwrap();
        let run_dir = writer.finish().unwrap();
        status::write_start(
            &run_dir,
            &status::CaptureStartRecord::new(capture_id, run_id),
        )
        .unwrap();

        let capturing = service
            .inspect(json!({"run": "runs/incomplete-run"}))
            .unwrap();
        assert_eq!(capturing["valid"], false);
        assert_eq!(capturing["capture_status"]["status"], "capturing");
        assert_eq!(
            service
                .validate(json!({"run": "runs/incomplete-run"}))
                .unwrap_err()
                .code,
            "CAPTURE_NOT_FINALIZED"
        );

        status::write_terminal(
            &run_dir,
            &status::CaptureTerminalRecord::incomplete(
                capture_id,
                run_id,
                "TEST_FAILURE",
                "expected failure",
                true,
            ),
        )
        .unwrap();
        let incomplete = service
            .inspect(json!({"run": "runs/incomplete-run"}))
            .unwrap();
        assert_eq!(incomplete["valid"], false);
        assert_eq!(incomplete["capture_status"]["status"], "incomplete");
        assert_eq!(
            service
                .validate(json!({"run": "runs/incomplete-run"}))
                .unwrap_err()
                .code,
            "CAPTURE_INCOMPLETE"
        );

        drop(service);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn descriptor_reader_rejects_fifo_and_limit_plus_one_without_blocking() {
        use std::ffi::CString;
        use std::os::unix::ffi::OsStrExt;

        let root = std::env::temp_dir().join(format!(
            "wattch-mcp-descriptor-reader-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();

        let oversized = root.join("oversized.json");
        fs::write(&oversized, b"123456789").unwrap();
        assert_eq!(
            read_bounded_regular_file(&oversized, 8).unwrap_err().code,
            "INPUT_TOO_LARGE"
        );

        let fifo = root.join("descriptor.fifo");
        let fifo_path = CString::new(fifo.as_os_str().as_bytes()).unwrap();
        // SAFETY: fifo_path is a NUL-terminated path owned for this call, and
        // the test creates the FIFO only inside its private temporary root.
        assert_eq!(unsafe { libc::mkfifo(fifo_path.as_ptr(), 0o600) }, 0);
        assert_eq!(
            read_bounded_regular_file(&fifo, 8).unwrap_err().code,
            "INVALID_INPUT_TYPE"
        );

        let _ = fs::remove_dir_all(root);
    }
}
