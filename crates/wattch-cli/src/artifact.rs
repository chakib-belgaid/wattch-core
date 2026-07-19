//! Run artifacts: replayable raw traces plus their metadata.
//!
//! ```text
//! runs/<run-id>/
//!   manifest.json     RunManifest
//!   sources.json      SourceDescriptor list (also embedded in the manifest)
//!   raw.trace.wch     framed binary RawSampleBatch messages, replayable
//!   events.trace.wch  framed binary EventBatch messages, replayable
//!   command.json      optional shell-free command declaration
//!   command.lifecycle.jsonl  optional append-only command completion ledger
//!   trial.json        optional experiment-runner metadata
//!   quality.json      optional offline evidence-quality metadata
//!   checksums.json    optional artifact-integrity metadata
//! ```

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::{Component, Path, PathBuf};
use thiserror::Error;
use wattch_core::{
    validate_source_set, RawSampleBatch, RawStreamValidator, RunManifest, SourceDescriptor,
};
use wattch_protocol::frame::{decode_header, WireError, FRAME_HEADER_LEN, MAX_PAYLOAD_LEN};
use wattch_protocol::messages::{
    read_message, read_message_with_payload_limit, validate_record_event, validate_run_id,
    write_message, EventBatch, Message, RecordEvent,
};

pub const MANIFEST_FILE: &str = "manifest.json";
pub const SOURCES_FILE: &str = "sources.json";
pub const RAW_TRACE_FILE: &str = "raw.trace.wch";
pub const EVENTS_TRACE_FILE: &str = "events.trace.wch";
pub const COMMAND_FILE: &str = "command.json";
pub const COMMAND_LIFECYCLE_FILE: &str = "command.lifecycle.jsonl";
pub const COMMAND_LIFECYCLE_SCHEMA_VERSION: u32 = 1;
pub const TRIAL_FILE: &str = "trial.json";
pub const QUALITY_FILE: &str = "quality.json";
pub const CHECKSUMS_FILE: &str = "checksums.json";
pub const CLOCK_INSTANCE_ID_LABEL: &str = "wattch.clock_instance_id";
const MAX_METADATA_BYTES: u64 = 16 * 1024 * 1024;
/// Historical v0.1 writers did not all enforce today's live 16 MiB frame
/// bound. Offline replay accepts those raw batches up to this finite migration
/// limit; live socket decoding remains capped by `MAX_PAYLOAD_LEN`.
const MAX_LEGACY_TRACE_PAYLOAD_LEN: u32 = MAX_PAYLOAD_LEN * 16;

/// Known JSON sidecars kept next to immutable raw evidence.
///
/// Their schemas belong to experiment and offline-analysis layers. The CLI
/// provides safe, create-only JSON I/O without interpreting their contents.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JsonSidecar {
    Command,
    Trial,
    Quality,
    Checksums,
}

/// One append-only record in `command.lifecycle.jsonl`.
///
/// The runner writes `STARTED` before executing the command and exactly one
/// terminal record. A missing or malformed terminal record is never treated
/// as a complete command capture.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "SCREAMING_SNAKE_CASE")]
pub(crate) enum CommandLifecycleRecord {
    Started {
        schema_version: u32,
        run_id: String,
    },
    Complete {
        schema_version: u32,
        run_id: String,
        exit_code: i32,
        signal: Option<i32>,
        batches: u64,
        samples: u64,
    },
    Incomplete {
        schema_version: u32,
        run_id: String,
        reason: String,
    },
}

impl CommandLifecycleRecord {
    pub(crate) fn started(run_id: &str) -> Self {
        Self::Started {
            schema_version: COMMAND_LIFECYCLE_SCHEMA_VERSION,
            run_id: run_id.to_string(),
        }
    }

    pub(crate) fn complete(
        run_id: &str,
        exit_code: i32,
        signal: Option<i32>,
        batches: u64,
        samples: u64,
    ) -> Self {
        Self::Complete {
            schema_version: COMMAND_LIFECYCLE_SCHEMA_VERSION,
            run_id: run_id.to_string(),
            exit_code,
            signal,
            batches,
            samples,
        }
    }

    pub(crate) fn incomplete(run_id: &str, reason: String) -> Self {
        Self::Incomplete {
            schema_version: COMMAND_LIFECYCLE_SCHEMA_VERSION,
            run_id: run_id.to_string(),
            reason,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CommandLifecycleStatus {
    Complete,
    Incomplete,
}

/// Inspection result for a command-backed artifact.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct CommandLifecycleSummary {
    pub status: CommandLifecycleStatus,
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub batches: Option<u64>,
    pub samples: Option<u64>,
    pub reason: Option<String>,
}

impl JsonSidecar {
    pub const fn file_name(self) -> &'static str {
        match self {
            Self::Command => COMMAND_FILE,
            Self::Trial => TRIAL_FILE,
            Self::Quality => QUALITY_FILE,
            Self::Checksums => CHECKSUMS_FILE,
        }
    }
}

#[derive(Debug, Error)]
pub enum ArtifactError {
    #[error("io error on {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },

    #[error("invalid json in {path}: {source}")]
    Json {
        path: PathBuf,
        source: serde_json::Error,
    },

    #[error("invalid trace frame in {path}: {source}")]
    Trace { path: PathBuf, source: WireError },

    #[error("unexpected message kind in trace: {0}")]
    UnexpectedMessage(String),

    #[error("run directory {0} already exists")]
    AlreadyExists(PathBuf),

    #[error("invalid run artifact: {0}")]
    Integrity(#[from] wattch_core::Error),

    #[error("run metadata mismatch: {0}")]
    MetadataMismatch(String),
}

fn io_err(path: &Path) -> impl FnOnce(std::io::Error) -> ArtifactError + '_ {
    move |source| ArtifactError::Io {
        path: path.to_path_buf(),
        source,
    }
}

/// Streaming artifact writer: create once, append batches, then finish.
pub struct RunWriter {
    run_dir: PathBuf,
    raw_trace: BufWriter<File>,
    event_trace: BufWriter<File>,
    validator: RawStreamValidator,
    event_validator: EventStreamValidator,
}

impl RunWriter {
    pub fn create(out_root: &Path, manifest: &RunManifest) -> Result<Self, ArtifactError> {
        validate_artifact_run_id(&manifest.run_id)?;
        validate_manifest_clock(manifest)?;
        validate_source_set(&manifest.sources)?;
        let validator = RawStreamValidator::new(
            manifest.run_id.clone(),
            manifest.node_id.clone(),
            &manifest.sources,
        )?;
        let run_dir = out_root.join(&manifest.run_id);
        fs::create_dir_all(out_root).map_err(io_err(out_root))?;
        match fs::create_dir(&run_dir) {
            Ok(()) => {}
            Err(source) if source.kind() == std::io::ErrorKind::AlreadyExists => {
                return Err(ArtifactError::AlreadyExists(run_dir));
            }
            Err(source) => {
                return Err(ArtifactError::Io {
                    path: run_dir,
                    source,
                })
            }
        }

        let manifest_path = run_dir.join(MANIFEST_FILE);
        let manifest_json =
            serde_json::to_string_pretty(manifest).map_err(|source| ArtifactError::Json {
                path: manifest_path.clone(),
                source,
            })?;
        write_new_file(&manifest_path, manifest_json.as_bytes())?;

        let sources_path = run_dir.join(SOURCES_FILE);
        let sources_json = serde_json::to_string_pretty(&manifest.sources).map_err(|source| {
            ArtifactError::Json {
                path: sources_path.clone(),
                source,
            }
        })?;
        write_new_file(&sources_path, sources_json.as_bytes())?;

        let raw_trace_path = run_dir.join(RAW_TRACE_FILE);
        let raw_trace_file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&raw_trace_path)
            .map_err(io_err(&raw_trace_path))?;
        let event_trace_path = run_dir.join(EVENTS_TRACE_FILE);
        let event_trace_file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&event_trace_path)
            .map_err(io_err(&event_trace_path))?;
        Ok(Self {
            event_validator: EventStreamValidator::new(
                manifest.run_id.clone(),
                manifest.node_id.clone(),
                None,
            ),
            run_dir,
            raw_trace: BufWriter::new(raw_trace_file),
            event_trace: BufWriter::new(event_trace_file),
            validator,
        })
    }

    pub fn append_batch(&mut self, batch: &RawSampleBatch) -> Result<(), ArtifactError> {
        self.validator.validate(batch)?;
        write_message(&mut self.raw_trace, &Message::RawSampleBatch(batch.clone())).map_err(
            |source| ArtifactError::Trace {
                path: self.run_dir.join(RAW_TRACE_FILE),
                source,
            },
        )
    }

    pub fn append_event_batch(&mut self, batch: &EventBatch) -> Result<(), ArtifactError> {
        self.event_validator
            .validate(batch)
            .map_err(ArtifactError::MetadataMismatch)?;
        write_message(&mut self.event_trace, &Message::EventBatch(batch.clone())).map_err(
            |source| ArtifactError::Trace {
                path: self.run_dir.join(EVENTS_TRACE_FILE),
                source,
            },
        )
    }

    pub fn run_dir(&self) -> &Path {
        &self.run_dir
    }

    pub fn finish(self) -> Result<PathBuf, ArtifactError> {
        let Self {
            run_dir,
            raw_trace,
            event_trace,
            validator: _,
            event_validator: _,
        } = self;
        sync_trace(raw_trace, &run_dir.join(RAW_TRACE_FILE))?;
        sync_trace(event_trace, &run_dir.join(EVENTS_TRACE_FILE))?;
        Ok(run_dir)
    }
}

fn sync_trace(trace: BufWriter<File>, path: &Path) -> Result<(), ArtifactError> {
    trace
        .into_inner()
        .map_err(|e| ArtifactError::Io {
            path: path.to_path_buf(),
            source: e.into_error(),
        })?
        .sync_all()
        .map_err(io_err(path))
}

/// Write one recognized JSON sidecar without overwriting prior evidence.
pub fn write_json_sidecar<T: Serialize>(
    run_dir: &Path,
    sidecar: JsonSidecar,
    value: &T,
) -> Result<PathBuf, ArtifactError> {
    // Bind the target to a valid artifact directory before creating anything.
    read_bound_metadata(run_dir)?;
    let path = run_dir.join(sidecar.file_name());
    let contents = serde_json::to_vec_pretty(value).map_err(|source| ArtifactError::Json {
        path: path.clone(),
        source,
    })?;
    if contents.len() as u64 > MAX_METADATA_BYTES {
        return Err(ArtifactError::MetadataMismatch(format!(
            "{} is {} bytes; metadata limit is {MAX_METADATA_BYTES}",
            path.display(),
            contents.len()
        )));
    }
    write_new_file(&path, &contents)?;
    Ok(path)
}

/// Read one recognized JSON sidecar with the same bound used for core
/// metadata. Missing optional sidecars surface as ordinary I/O errors.
pub fn read_json_sidecar<T: DeserializeOwned>(
    run_dir: &Path,
    sidecar: JsonSidecar,
) -> Result<T, ArtifactError> {
    read_bound_metadata(run_dir)?;
    let path = run_dir.join(sidecar.file_name());
    let raw = read_bounded_metadata(&path)?;
    serde_json::from_str(&raw).map_err(|source| ArtifactError::Json { path, source })
}

#[derive(Debug)]
pub(crate) struct EventStreamValidator {
    run_id: String,
    node_id: String,
    next_batch_sequence_no: Option<u64>,
    last_monotonic_ns: Option<u64>,
    seen_event_ids: std::collections::BTreeSet<String>,
}

impl EventStreamValidator {
    pub(crate) fn new(
        run_id: String,
        node_id: String,
        first_batch_sequence_no: Option<u64>,
    ) -> Self {
        Self {
            run_id,
            node_id,
            next_batch_sequence_no: first_batch_sequence_no,
            last_monotonic_ns: None,
            seen_event_ids: Default::default(),
        }
    }

    pub(crate) fn validate(&mut self, batch: &EventBatch) -> Result<(), String> {
        if batch.run_id != self.run_id {
            return Err(format!(
                "event batch run_id {:?} does not match {:?}",
                batch.run_id, self.run_id
            ));
        }
        if batch.node_id != self.node_id {
            return Err(format!(
                "event batch node_id {:?} does not match {:?}",
                batch.node_id, self.node_id
            ));
        }
        if batch.events.is_empty() {
            return Err("event batch must not be empty".to_string());
        }
        if let Some(expected) = self.next_batch_sequence_no {
            if batch.batch_sequence_no != expected {
                return Err(format!(
                    "event batch sequence {} does not match expected {expected}",
                    batch.batch_sequence_no
                ));
            }
        }

        let next_batch_sequence_no = batch
            .batch_sequence_no
            .checked_add(1)
            .ok_or_else(|| "event batch sequence overflow".to_string())?;
        let mut last_monotonic_ns = self.last_monotonic_ns;
        let mut new_event_ids = std::collections::BTreeSet::new();
        for event in &batch.events {
            if event.run_id != batch.run_id || event.node_id != batch.node_id {
                return Err(format!(
                    "event {:?} identity does not match its batch",
                    event.event_id
                ));
            }
            let request_shape = RecordEvent {
                run_id: event.run_id.clone(),
                event_id: event.event_id.clone(),
                kind: event.kind.clone(),
                labels: event.labels.clone(),
            };
            validate_record_event(&request_shape).map_err(str::to_string)?;
            if self.seen_event_ids.contains(&event.event_id)
                || !new_event_ids.insert(event.event_id.clone())
            {
                return Err(format!(
                    "duplicate event_id {:?} in event trace",
                    event.event_id
                ));
            }
            if last_monotonic_ns.is_some_and(|last| event.monotonic_ns < last) {
                return Err(format!(
                    "event {:?} timestamp moves backwards",
                    event.event_id
                ));
            }
            last_monotonic_ns = Some(event.monotonic_ns);
        }

        self.next_batch_sequence_no = Some(next_batch_sequence_no);
        self.last_monotonic_ns = last_monotonic_ns;
        self.seen_event_ids.extend(new_event_ids);
        Ok(())
    }

    pub(crate) fn events_seen(&self) -> u64 {
        self.seen_event_ids.len() as u64
    }
}

fn write_new_file(path: &Path, contents: &[u8]) -> Result<(), ArtifactError> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(io_err(path))?;
    file.write_all(contents).map_err(io_err(path))?;
    file.sync_all().map_err(io_err(path))
}

fn validate_artifact_run_id(run_id: &str) -> Result<(), ArtifactError> {
    validate_run_id(run_id)
        .map_err(|reason| ArtifactError::MetadataMismatch(reason.to_string()))?;
    let mut components = Path::new(run_id).components();
    let is_one_normal_component =
        matches!(components.next(), Some(Component::Normal(_))) && components.next().is_none();
    if !is_one_normal_component || run_id.contains('\\') {
        return Err(ArtifactError::MetadataMismatch(
            "run_id must be one portable filename component".to_string(),
        ));
    }
    Ok(())
}

fn validate_manifest_clock(manifest: &RunManifest) -> Result<(), ArtifactError> {
    if manifest.clock_instance_id.trim().is_empty() && !is_legacy_v0_1(&manifest.protocol_version) {
        return Err(ArtifactError::MetadataMismatch(
            "v0.2+ manifest.clock_instance_id must not be empty".to_string(),
        ));
    }
    if manifest.clock_instance_id.len() > 256
        || manifest.clock_instance_id.chars().any(char::is_control)
    {
        return Err(ArtifactError::MetadataMismatch(
            "manifest.clock_instance_id must be at most 256 bytes and contain no control characters"
                .to_string(),
        ));
    }
    if let Some(label_clock) = manifest.labels.get(CLOCK_INSTANCE_ID_LABEL) {
        if label_clock != &manifest.clock_instance_id {
            return Err(ArtifactError::MetadataMismatch(format!(
                "manifest label {CLOCK_INSTANCE_ID_LABEL:?} does not match manifest.clock_instance_id"
            )));
        }
    }
    Ok(())
}

pub fn read_manifest(run_dir: &Path) -> Result<RunManifest, ArtifactError> {
    let path = run_dir.join(MANIFEST_FILE);
    let raw = read_bounded_metadata(&path)?;
    serde_json::from_str(&raw).map_err(|source| ArtifactError::Json { path, source })
}

pub fn read_sources(run_dir: &Path) -> Result<Vec<SourceDescriptor>, ArtifactError> {
    let path = run_dir.join(SOURCES_FILE);
    let raw = read_bounded_metadata(&path)?;
    serde_json::from_str(&raw).map_err(|source| ArtifactError::Json { path, source })
}

fn read_bounded_metadata(path: &Path) -> Result<String, ArtifactError> {
    let length = fs::metadata(path).map_err(io_err(path))?.len();
    if length > MAX_METADATA_BYTES {
        return Err(ArtifactError::MetadataMismatch(format!(
            "{} is {length} bytes; metadata limit is {MAX_METADATA_BYTES}",
            path.display()
        )));
    }
    fs::read_to_string(path).map_err(io_err(path))
}

fn read_bound_metadata(
    run_dir: &Path,
) -> Result<(RunManifest, Vec<SourceDescriptor>), ArtifactError> {
    let manifest = read_manifest(run_dir)?;
    validate_artifact_run_id(&manifest.run_id)?;
    validate_manifest_clock(&manifest)?;
    if run_dir.file_name() != Some(std::ffi::OsStr::new(&manifest.run_id)) {
        return Err(ArtifactError::MetadataMismatch(
            "run directory name does not match manifest.run_id".to_string(),
        ));
    }
    let sources = read_sources(run_dir)?;
    if sources != manifest.sources {
        return Err(ArtifactError::MetadataMismatch(
            "sources.json does not exactly match manifest.sources".to_string(),
        ));
    }
    validate_source_set(&sources)?;
    Ok((manifest, sources))
}

/// Replay the raw trace from disk, calling `on_batch` per decoded batch.
pub fn replay_trace(
    run_dir: &Path,
    mut on_batch: impl FnMut(RawSampleBatch),
) -> Result<u64, ArtifactError> {
    let (manifest, sources) = read_bound_metadata(run_dir)?;
    let allow_large_legacy_frames = is_legacy_v0_1(&manifest.protocol_version);
    let mut validator = RawStreamValidator::new(manifest.run_id, manifest.node_id, &sources)?;
    replay_trace_checked(run_dir, allow_large_legacy_frames, |batch| {
        validator.validate(&batch)?;
        on_batch(batch);
        Ok(())
    })
}

fn replay_trace_checked(
    run_dir: &Path,
    allow_large_legacy_frames: bool,
    mut on_batch: impl FnMut(RawSampleBatch) -> Result<(), ArtifactError>,
) -> Result<u64, ArtifactError> {
    let path = run_dir.join(RAW_TRACE_FILE);
    let mut reader = BufReader::new(File::open(&path).map_err(io_err(&path))?);
    let mut batches = 0u64;
    loop {
        let message = read_raw_trace_message(&mut reader, allow_large_legacy_frames);
        match message {
            Ok(Message::RawSampleBatch(batch)) => {
                batches += 1;
                on_batch(batch)?;
            }
            Ok(other) => {
                return Err(ArtifactError::UnexpectedMessage(format!(
                    "{:?}",
                    other.kind()
                )))
            }
            Err(WireError::ConnectionClosed) => return Ok(batches),
            Err(source) => return Err(ArtifactError::Trace { path, source }),
        }
    }
}

fn read_raw_trace_message(
    reader: &mut BufReader<File>,
    allow_large_legacy_frames: bool,
) -> Result<Message, WireError> {
    if allow_large_legacy_frames {
        let buffered = reader.fill_buf()?;
        if buffered.len() >= FRAME_HEADER_LEN
            && decode_header(&buffered[..FRAME_HEADER_LEN])
                .is_ok_and(|header| header.wire_version_minor == 1)
        {
            return read_message_with_payload_limit(reader, MAX_LEGACY_TRACE_PAYLOAD_LEN);
        }
    }
    read_message(reader)
}

/// Replay daemon-stamped benchmark events from disk.
///
/// A v0.1 artifact may legitimately lack `events.trace.wch`; that case is an
/// empty event stream. Newer artifacts must contain the file, even if empty.
pub fn replay_events(
    run_dir: &Path,
    mut on_batch: impl FnMut(EventBatch),
) -> Result<u64, ArtifactError> {
    let (manifest, _) = read_bound_metadata(run_dir)?;
    let mut validator = EventStreamValidator::new(manifest.run_id, manifest.node_id, None);
    replay_events_checked(
        run_dir,
        is_legacy_v0_1(&manifest.protocol_version),
        |batch| {
            validator
                .validate(&batch)
                .map_err(ArtifactError::MetadataMismatch)?;
            on_batch(batch);
            Ok(())
        },
    )
}

fn replay_events_checked(
    run_dir: &Path,
    allow_missing: bool,
    mut on_batch: impl FnMut(EventBatch) -> Result<(), ArtifactError>,
) -> Result<u64, ArtifactError> {
    let path = run_dir.join(EVENTS_TRACE_FILE);
    let file = match File::open(&path) {
        Ok(file) => file,
        Err(source) if allow_missing && source.kind() == std::io::ErrorKind::NotFound => {
            return Ok(0);
        }
        Err(source) => return Err(ArtifactError::Io { path, source }),
    };
    let mut reader = BufReader::new(file);
    let mut batches = 0u64;
    loop {
        match read_message(&mut reader) {
            Ok(Message::EventBatch(batch)) => {
                batches += 1;
                on_batch(batch)?;
            }
            Ok(other) => {
                return Err(ArtifactError::UnexpectedMessage(format!(
                    "{:?} in {EVENTS_TRACE_FILE}",
                    other.kind()
                )))
            }
            Err(WireError::ConnectionClosed) => return Ok(batches),
            Err(source) => return Err(ArtifactError::Trace { path, source }),
        }
    }
}

fn is_legacy_v0_1(protocol_version: &str) -> bool {
    protocol_version == "0.1" || protocol_version.starts_with("0.1.")
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct SourceStats {
    pub samples: u64,
    pub first_sequence_no: u64,
    pub last_sequence_no: u64,
    pub sequence_gaps: u64,
    pub first_monotonic_ns: u64,
    pub last_monotonic_ns: u64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct InspectReport {
    pub run_id: String,
    pub node_id: String,
    pub batches: u64,
    pub samples: u64,
    pub event_batches: u64,
    pub events: u64,
    pub unknown_source_samples: u64,
    pub per_source: BTreeMap<u32, SourceStats>,
    pub per_event_kind: BTreeMap<String, u64>,
    pub command_lifecycle: Option<CommandLifecycleSummary>,
}

/// Replay a run artifact and compute per-source statistics and integrity
/// checks (sequence gaps, unknown source references).
pub fn inspect_run(run_dir: &Path) -> Result<InspectReport, ArtifactError> {
    let (manifest, sources) = read_bound_metadata(run_dir)?;
    let allow_large_legacy_frames = is_legacy_v0_1(&manifest.protocol_version);
    let mut validator =
        RawStreamValidator::new(manifest.run_id.clone(), manifest.node_id.clone(), &sources)?;

    let mut report = InspectReport {
        run_id: manifest.run_id.clone(),
        node_id: manifest.node_id.clone(),
        ..Default::default()
    };

    let batches = replay_trace_checked(run_dir, allow_large_legacy_frames, |batch| {
        validator.validate(&batch)?;
        report.samples += batch.samples.len() as u64;
        for sample in &batch.samples {
            let stats = report.per_source.entry(sample.source_id).or_default();
            if stats.samples == 0 {
                stats.first_sequence_no = sample.sequence_no;
                stats.first_monotonic_ns = sample.monotonic_ns;
            } else if sample.sequence_no != stats.last_sequence_no + 1 {
                stats.sequence_gaps += 1;
            }
            stats.last_sequence_no = sample.sequence_no;
            stats.last_monotonic_ns = sample.monotonic_ns;
            stats.samples += 1;
        }
        Ok(())
    })?;
    report.batches = batches;

    let mut event_validator =
        EventStreamValidator::new(manifest.run_id.clone(), manifest.node_id.clone(), None);
    let event_batches = replay_events_checked(
        run_dir,
        is_legacy_v0_1(&manifest.protocol_version),
        |batch| {
            event_validator
                .validate(&batch)
                .map_err(ArtifactError::MetadataMismatch)?;
            for event in batch.events {
                *report.per_event_kind.entry(event.kind).or_default() += 1;
            }
            Ok(())
        },
    )?;
    report.event_batches = event_batches;
    report.events = event_validator.events_seen();
    report.command_lifecycle = inspect_command_lifecycle(run_dir, &manifest.run_id, &report)?;
    Ok(report)
}

fn inspect_command_lifecycle(
    run_dir: &Path,
    run_id: &str,
    report: &InspectReport,
) -> Result<Option<CommandLifecycleSummary>, ArtifactError> {
    let command_path = run_dir.join(COMMAND_FILE);
    let lifecycle_path = run_dir.join(COMMAND_LIFECYCLE_FILE);
    let command_exists = command_path.try_exists().map_err(io_err(&command_path))?;
    let lifecycle_exists = lifecycle_path
        .try_exists()
        .map_err(io_err(&lifecycle_path))?;
    if !command_exists && !lifecycle_exists {
        return Ok(None);
    }
    if !lifecycle_exists {
        return Ok(Some(incomplete_lifecycle(
            "command lifecycle ledger is missing",
        )));
    }

    let raw = read_bounded_metadata(&lifecycle_path)?;
    let mut records = Vec::new();
    for line in raw.lines() {
        if line.trim().is_empty() {
            continue;
        }
        records.push(
            serde_json::from_str::<CommandLifecycleRecord>(line).map_err(|source| {
                ArtifactError::Json {
                    path: lifecycle_path.clone(),
                    source,
                }
            })?,
        );
    }
    if records.is_empty() {
        return Ok(Some(incomplete_lifecycle(
            "command lifecycle ledger has no STARTED record",
        )));
    }
    if records.len() > 2 {
        return Err(ArtifactError::MetadataMismatch(
            "command lifecycle ledger contains records after its terminal state".to_string(),
        ));
    }

    match &records[0] {
        CommandLifecycleRecord::Started {
            schema_version,
            run_id: record_run_id,
        } => validate_lifecycle_identity(*schema_version, record_run_id, run_id)?,
        _ => {
            return Err(ArtifactError::MetadataMismatch(
                "command lifecycle ledger must begin with STARTED".to_string(),
            ))
        }
    }
    let Some(terminal) = records.get(1) else {
        return Ok(Some(incomplete_lifecycle(
            "command lifecycle terminal record is missing",
        )));
    };

    match terminal {
        CommandLifecycleRecord::Complete {
            schema_version,
            run_id: record_run_id,
            exit_code,
            signal,
            batches,
            samples,
        } => {
            validate_lifecycle_identity(*schema_version, record_run_id, run_id)?;
            if !command_exists {
                return Err(ArtifactError::MetadataMismatch(
                    "COMPLETE command lifecycle has no command.json declaration".to_string(),
                ));
            }
            if *batches != report.batches || *samples != report.samples {
                return Err(ArtifactError::MetadataMismatch(format!(
                    "COMPLETE command lifecycle counts {batches}/{samples} do not match replayed {}/{}",
                    report.batches, report.samples
                )));
            }
            Ok(Some(CommandLifecycleSummary {
                status: CommandLifecycleStatus::Complete,
                exit_code: Some(*exit_code),
                signal: *signal,
                batches: Some(*batches),
                samples: Some(*samples),
                reason: None,
            }))
        }
        CommandLifecycleRecord::Incomplete {
            schema_version,
            run_id: record_run_id,
            reason,
        } => {
            validate_lifecycle_identity(*schema_version, record_run_id, run_id)?;
            Ok(Some(incomplete_lifecycle(reason)))
        }
        CommandLifecycleRecord::Started { .. } => Err(ArtifactError::MetadataMismatch(
            "command lifecycle ledger contains STARTED where a terminal record is required"
                .to_string(),
        )),
    }
}

fn validate_lifecycle_identity(
    schema_version: u32,
    record_run_id: &str,
    expected_run_id: &str,
) -> Result<(), ArtifactError> {
    if schema_version != COMMAND_LIFECYCLE_SCHEMA_VERSION {
        return Err(ArtifactError::MetadataMismatch(format!(
            "unsupported command lifecycle schema version {schema_version}"
        )));
    }
    if record_run_id != expected_run_id {
        return Err(ArtifactError::MetadataMismatch(format!(
            "command lifecycle run_id {record_run_id:?} does not match {expected_run_id:?}"
        )));
    }
    Ok(())
}

fn incomplete_lifecycle(reason: impl Into<String>) -> CommandLifecycleSummary {
    CommandLifecycleSummary {
        status: CommandLifecycleStatus::Incomplete,
        exit_code: None,
        signal: None,
        batches: None,
        samples: None,
        reason: Some(reason.into()),
    }
}
