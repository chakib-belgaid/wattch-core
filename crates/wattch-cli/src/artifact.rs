//! Run artifacts: replayable raw traces plus their metadata.
//!
//! ```text
//! runs/<run-id>/
//!   manifest.json   RunManifest
//!   sources.json    SourceDescriptor list (also embedded in the manifest)
//!   raw.trace.wch   framed binary RawSampleBatch messages, replayable
//! ```

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Write};
use std::path::{Component, Path, PathBuf};
use thiserror::Error;
use wattch_core::{
    validate_source_set, RawSampleBatch, RawStreamValidator, RunManifest, SourceDescriptor,
};
use wattch_protocol::frame::WireError;
use wattch_protocol::messages::validate_run_id;
use wattch_protocol::messages::{read_message, write_message, Message};

pub const MANIFEST_FILE: &str = "manifest.json";
pub const SOURCES_FILE: &str = "sources.json";
pub const RAW_TRACE_FILE: &str = "raw.trace.wch";
const MAX_METADATA_BYTES: u64 = 16 * 1024 * 1024;

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

    #[error("unexpected message kind in raw trace: {0}")]
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
    trace: BufWriter<File>,
    validator: RawStreamValidator,
}

impl RunWriter {
    pub fn create(out_root: &Path, manifest: &RunManifest) -> Result<Self, ArtifactError> {
        validate_artifact_run_id(&manifest.run_id)?;
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

        let trace_path = run_dir.join(RAW_TRACE_FILE);
        let trace_file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&trace_path)
            .map_err(io_err(&trace_path))?;
        let trace = BufWriter::new(trace_file);
        Ok(Self {
            run_dir,
            trace,
            validator,
        })
    }

    pub fn append_batch(&mut self, batch: &RawSampleBatch) -> Result<(), ArtifactError> {
        self.validator.validate(batch)?;
        write_message(&mut self.trace, &Message::RawSampleBatch(batch.clone())).map_err(|source| {
            ArtifactError::Trace {
                path: self.run_dir.join(RAW_TRACE_FILE),
                source,
            }
        })
    }

    pub fn finish(self) -> Result<PathBuf, ArtifactError> {
        let Self {
            run_dir,
            trace,
            validator: _,
        } = self;
        trace
            .into_inner()
            .map_err(|e| ArtifactError::Io {
                path: run_dir.join(RAW_TRACE_FILE),
                source: e.into_error(),
            })?
            .sync_all()
            .map_err(io_err(&run_dir.join(RAW_TRACE_FILE)))?;
        Ok(run_dir)
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
    let mut validator = RawStreamValidator::new(manifest.run_id, manifest.node_id, &sources)?;
    replay_trace_checked(run_dir, |batch| {
        validator.validate(&batch)?;
        on_batch(batch);
        Ok(())
    })
}

fn replay_trace_checked(
    run_dir: &Path,
    mut on_batch: impl FnMut(RawSampleBatch) -> Result<(), ArtifactError>,
) -> Result<u64, ArtifactError> {
    let path = run_dir.join(RAW_TRACE_FILE);
    let mut reader = BufReader::new(File::open(&path).map_err(io_err(&path))?);
    let mut batches = 0u64;
    loop {
        match read_message(&mut reader) {
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

#[derive(Clone, Debug, Default, PartialEq)]
pub struct SourceStats {
    pub samples: u64,
    pub first_sequence_no: u64,
    pub last_sequence_no: u64,
    pub sequence_gaps: u64,
    pub first_monotonic_ns: u64,
    pub last_monotonic_ns: u64,
}

#[derive(Clone, Debug, Default)]
pub struct InspectReport {
    pub run_id: String,
    pub node_id: String,
    pub batches: u64,
    pub samples: u64,
    pub unknown_source_samples: u64,
    pub per_source: BTreeMap<u32, SourceStats>,
}

/// Replay a run artifact and compute per-source statistics and integrity
/// checks (sequence gaps, unknown source references).
pub fn inspect_run(run_dir: &Path) -> Result<InspectReport, ArtifactError> {
    let (manifest, sources) = read_bound_metadata(run_dir)?;
    let mut validator =
        RawStreamValidator::new(manifest.run_id.clone(), manifest.node_id.clone(), &sources)?;

    let mut report = InspectReport {
        run_id: manifest.run_id.clone(),
        node_id: manifest.node_id.clone(),
        ..Default::default()
    };

    let batches = replay_trace_checked(run_dir, |batch| {
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
    Ok(report)
}
