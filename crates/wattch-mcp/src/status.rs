use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use thiserror::Error;

pub(crate) const MCP_CAPTURE_ID_LABEL: &str = "wattch.mcp.capture_id";
const START_FILE: &str = "mcp-capture.json";
const TERMINAL_FILE: &str = "mcp-capture-terminal.json";
const SCHEMA_VERSION: u32 = 1;
const MAX_STATUS_BYTES: u64 = 64 * 1024;
const MAX_ERROR_MESSAGE_BYTES: usize = 1024;

static NEXT_TEMP_FILE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CaptureStartRecord {
    schema_version: u32,
    state: CaptureStartState,
    pub capture_id: String,
    pub run_id: String,
    created_unix_ns: u64,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum CaptureStartState {
    Capturing,
}

impl CaptureStartRecord {
    pub(crate) fn new(capture_id: &str, run_id: &str) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            state: CaptureStartState::Capturing,
            capture_id: capture_id.to_string(),
            run_id: run_id.to_string(),
            created_unix_ns: unix_ns(),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum CaptureTerminalRecord {
    Complete {
        schema_version: u32,
        capture_id: String,
        run_id: String,
        termination_reason: String,
        interval_ms: u64,
        selected_source_ids: Vec<u32>,
        start_event_id: String,
        start_monotonic_ns: u64,
        end_event_id: String,
        end_monotonic_ns: u64,
        tail_complete: bool,
        tail_target_monotonic_ns: u64,
        tail_last_monotonic_ns: u64,
        tail_incomplete_source_ids: Vec<u32>,
        batches_sent: u64,
        samples_sent: u64,
        completed_unix_ns: u64,
    },
    Incomplete {
        schema_version: u32,
        capture_id: String,
        run_id: String,
        error_code: String,
        error_message: String,
        trace_sync_succeeded: bool,
        completed_unix_ns: u64,
    },
}

impl CaptureTerminalRecord {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn complete(
        capture_id: &str,
        run_id: &str,
        termination_reason: &str,
        interval_ms: u64,
        selected_source_ids: &[u32],
        start_event_id: &str,
        start_monotonic_ns: u64,
        end_event_id: &str,
        end_monotonic_ns: u64,
        tail_complete: bool,
        tail_target_monotonic_ns: u64,
        tail_last_monotonic_ns: u64,
        tail_incomplete_source_ids: Vec<u32>,
        batches_sent: u64,
        samples_sent: u64,
    ) -> Self {
        let mut selected_source_ids = selected_source_ids.to_vec();
        selected_source_ids.sort_unstable();
        Self::Complete {
            schema_version: SCHEMA_VERSION,
            capture_id: capture_id.to_string(),
            run_id: run_id.to_string(),
            termination_reason: termination_reason.to_string(),
            interval_ms,
            selected_source_ids,
            start_event_id: start_event_id.to_string(),
            start_monotonic_ns,
            end_event_id: end_event_id.to_string(),
            end_monotonic_ns,
            tail_complete,
            tail_target_monotonic_ns,
            tail_last_monotonic_ns,
            tail_incomplete_source_ids,
            batches_sent,
            samples_sent,
            completed_unix_ns: unix_ns(),
        }
    }

    pub(crate) fn incomplete(
        capture_id: &str,
        run_id: &str,
        error_code: &str,
        error_message: &str,
        trace_sync_succeeded: bool,
    ) -> Self {
        Self::Incomplete {
            schema_version: SCHEMA_VERSION,
            capture_id: capture_id.to_string(),
            run_id: run_id.to_string(),
            error_code: error_code.to_string(),
            error_message: truncate_utf8(error_message, MAX_ERROR_MESSAGE_BYTES),
            trace_sync_succeeded,
            completed_unix_ns: unix_ns(),
        }
    }

    fn identity(&self) -> (&str, &str) {
        match self {
            Self::Complete {
                capture_id, run_id, ..
            }
            | Self::Incomplete {
                capture_id, run_id, ..
            } => (capture_id, run_id),
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) enum ManagedCaptureStatus {
    Unmanaged,
    Capturing(CaptureStartRecord),
    Complete {
        start: CaptureStartRecord,
        terminal: CaptureTerminalRecord,
    },
    Incomplete {
        start: CaptureStartRecord,
        terminal: CaptureTerminalRecord,
    },
}

impl ManagedCaptureStatus {
    pub(crate) fn as_value(&self) -> Value {
        match self {
            Self::Unmanaged => json!({"managed": false, "status": "unmanaged"}),
            Self::Capturing(start) => {
                json!({"managed": true, "status": "capturing", "start": start})
            }
            Self::Complete { start, terminal } => json!({
                "managed": true,
                "status": "complete",
                "start": start,
                "terminal": terminal,
            }),
            Self::Incomplete { start, terminal } => json!({
                "managed": true,
                "status": "incomplete",
                "start": start,
                "terminal": terminal,
            }),
        }
    }

    pub(crate) fn is_lifecycle_valid(&self) -> bool {
        matches!(self, Self::Unmanaged | Self::Complete { .. })
    }
}

#[derive(Debug, Error)]
pub(crate) enum CaptureStatusError {
    #[error("capture status I/O failed at {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("capture status JSON failed at {path}: {source}")]
    Json {
        path: PathBuf,
        source: serde_json::Error,
    },
    #[error("capture status integrity error: {0}")]
    Integrity(String),
}

pub(crate) fn write_start(
    run_dir: &Path,
    record: &CaptureStartRecord,
) -> Result<(), CaptureStatusError> {
    write_new_json(run_dir, START_FILE, record)
}

pub(crate) fn write_terminal(
    run_dir: &Path,
    record: &CaptureTerminalRecord,
) -> Result<(), CaptureStatusError> {
    write_new_json(run_dir, TERMINAL_FILE, record)
}

pub(crate) fn read_status(
    run_dir: &Path,
    run_id: &str,
    manifest_capture_id: Option<&str>,
) -> Result<ManagedCaptureStatus, CaptureStatusError> {
    let start = read_optional_json::<CaptureStartRecord>(&run_dir.join(START_FILE))?;
    let terminal = read_optional_json::<CaptureTerminalRecord>(&run_dir.join(TERMINAL_FILE))?;

    let Some(expected_capture_id) = manifest_capture_id else {
        if start.is_some() || terminal.is_some() {
            return Err(CaptureStatusError::Integrity(
                "MCP capture sidecars exist without the reserved manifest binding".to_string(),
            ));
        }
        return Ok(ManagedCaptureStatus::Unmanaged);
    };
    let start = start.ok_or_else(|| {
        CaptureStatusError::Integrity(
            "MCP-labelled artifact is missing mcp-capture.json".to_string(),
        )
    })?;
    if start.schema_version != SCHEMA_VERSION
        || start.capture_id != expected_capture_id
        || start.run_id != run_id
    {
        return Err(CaptureStatusError::Integrity(
            "MCP capture start record is not bound to the manifest".to_string(),
        ));
    }
    let Some(terminal) = terminal else {
        return Ok(ManagedCaptureStatus::Capturing(start));
    };
    let (terminal_capture_id, terminal_run_id) = terminal.identity();
    let terminal_schema = match &terminal {
        CaptureTerminalRecord::Complete { schema_version, .. }
        | CaptureTerminalRecord::Incomplete { schema_version, .. } => *schema_version,
    };
    if terminal_schema != SCHEMA_VERSION
        || terminal_capture_id != expected_capture_id
        || terminal_run_id != run_id
    {
        return Err(CaptureStatusError::Integrity(
            "MCP capture terminal record is not bound to the manifest".to_string(),
        ));
    }
    Ok(match terminal {
        terminal @ CaptureTerminalRecord::Complete { .. } => {
            ManagedCaptureStatus::Complete { start, terminal }
        }
        terminal @ CaptureTerminalRecord::Incomplete { .. } => {
            ManagedCaptureStatus::Incomplete { start, terminal }
        }
    })
}

fn write_new_json<T: Serialize>(
    run_dir: &Path,
    file_name: &str,
    value: &T,
) -> Result<(), CaptureStatusError> {
    let bytes = serde_json::to_vec_pretty(value).map_err(|source| CaptureStatusError::Json {
        path: run_dir.join(file_name),
        source,
    })?;
    if bytes.len() as u64 > MAX_STATUS_BYTES {
        return Err(CaptureStatusError::Integrity(format!(
            "{file_name} exceeds {MAX_STATUS_BYTES} bytes"
        )));
    }
    let sequence = NEXT_TEMP_FILE.fetch_add(1, Ordering::SeqCst);
    let temp_path = run_dir.join(format!(
        ".{file_name}.tmp-{}-{sequence}",
        std::process::id()
    ));
    let final_path = run_dir.join(file_name);
    let mut temp = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temp_path)
        .map_err(|source| CaptureStatusError::Io {
            path: temp_path.clone(),
            source,
        })?;
    let write_result = (|| {
        temp.write_all(&bytes)
            .and_then(|()| temp.write_all(b"\n"))
            .and_then(|()| temp.sync_all())
            .map_err(|source| CaptureStatusError::Io {
                path: temp_path.clone(),
                source,
            })?;
        fs::hard_link(&temp_path, &final_path).map_err(|source| CaptureStatusError::Io {
            path: final_path.clone(),
            source,
        })?;
        File::open(run_dir)
            .and_then(|directory| directory.sync_all())
            .map_err(|source| CaptureStatusError::Io {
                path: run_dir.to_path_buf(),
                source,
            })
    })();
    drop(temp);
    let _ = fs::remove_file(&temp_path);
    write_result
}

fn read_optional_json<T: for<'de> Deserialize<'de>>(
    path: &Path,
) -> Result<Option<T>, CaptureStatusError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(source) => {
            return Err(CaptureStatusError::Io {
                path: path.to_path_buf(),
                source,
            })
        }
    };
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err(CaptureStatusError::Integrity(format!(
            "{} is not a regular status file",
            path.display()
        )));
    }
    if metadata.len() > MAX_STATUS_BYTES {
        return Err(CaptureStatusError::Integrity(format!(
            "{} exceeds {MAX_STATUS_BYTES} bytes",
            path.display()
        )));
    }
    let mut file = File::open(path).map_err(|source| CaptureStatusError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.read_to_end(&mut bytes)
        .map_err(|source| CaptureStatusError::Io {
            path: path.to_path_buf(),
            source,
        })?;
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|source| CaptureStatusError::Json {
            path: path.to_path_buf(),
            source,
        })
}

fn truncate_utf8(value: &str, limit: usize) -> String {
    if value.len() <= limit {
        return value.to_string();
    }
    let mut end = limit;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    value[..end].to_string()
}

fn unix_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_files_are_create_only_and_identity_bound() {
        let root = std::env::temp_dir().join(format!(
            "wattch-mcp-status-{}-{}",
            std::process::id(),
            NEXT_TEMP_FILE.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir_all(&root).unwrap();
        let start = CaptureStartRecord::new("capture-1", "run-1");
        write_start(&root, &start).unwrap();
        assert!(write_start(&root, &start).is_err());
        assert!(matches!(
            read_status(&root, "run-1", Some("capture-1")).unwrap(),
            ManagedCaptureStatus::Capturing(_)
        ));

        let terminal = CaptureTerminalRecord::incomplete(
            "capture-1",
            "run-1",
            "TEST_FAILURE",
            "expected",
            true,
        );
        write_terminal(&root, &terminal).unwrap();
        assert!(write_terminal(&root, &terminal).is_err());
        assert!(matches!(
            read_status(&root, "run-1", Some("capture-1")).unwrap(),
            ManagedCaptureStatus::Incomplete { .. }
        ));
        assert!(read_status(&root, "wrong-run", Some("capture-1")).is_err());

        let _ = fs::remove_dir_all(root);
    }
}
