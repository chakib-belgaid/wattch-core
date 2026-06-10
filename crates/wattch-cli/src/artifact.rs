//! Run artifacts: replayable raw traces plus their metadata.
//!
//! ```text
//! runs/<run-id>/
//!   manifest.json   RunManifest
//!   sources.json    SourceDescriptor list (also embedded in the manifest)
//!   raw.trace.wch   framed binary RawSampleBatch messages, replayable
//! ```

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufReader, BufWriter};
use std::path::{Path, PathBuf};
use thiserror::Error;
use wattch_core::{RawSampleBatch, RunManifest};
use wattch_protocol::frame::WireError;
use wattch_protocol::messages::{read_message, write_message, Message};

pub const MANIFEST_FILE: &str = "manifest.json";
pub const SOURCES_FILE: &str = "sources.json";
pub const RAW_TRACE_FILE: &str = "raw.trace.wch";

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
}

impl RunWriter {
    pub fn create(out_root: &Path, manifest: &RunManifest) -> Result<Self, ArtifactError> {
        let run_dir = out_root.join(&manifest.run_id);
        if run_dir.exists() {
            return Err(ArtifactError::AlreadyExists(run_dir));
        }
        fs::create_dir_all(&run_dir).map_err(io_err(&run_dir))?;

        let manifest_path = run_dir.join(MANIFEST_FILE);
        let manifest_json =
            serde_json::to_string_pretty(manifest).map_err(|source| ArtifactError::Json {
                path: manifest_path.clone(),
                source,
            })?;
        fs::write(&manifest_path, manifest_json).map_err(io_err(&manifest_path))?;

        let sources_path = run_dir.join(SOURCES_FILE);
        let sources_json = serde_json::to_string_pretty(&manifest.sources).map_err(|source| {
            ArtifactError::Json {
                path: sources_path.clone(),
                source,
            }
        })?;
        fs::write(&sources_path, sources_json).map_err(io_err(&sources_path))?;

        let trace_path = run_dir.join(RAW_TRACE_FILE);
        let trace = BufWriter::new(File::create(&trace_path).map_err(io_err(&trace_path))?);
        Ok(Self { run_dir, trace })
    }

    pub fn append_batch(&mut self, batch: &RawSampleBatch) -> Result<(), ArtifactError> {
        write_message(&mut self.trace, &Message::RawSampleBatch(batch.clone())).map_err(|source| {
            ArtifactError::Trace {
                path: self.run_dir.join(RAW_TRACE_FILE),
                source,
            }
        })
    }

    pub fn finish(self) -> Result<PathBuf, ArtifactError> {
        let Self { run_dir, trace } = self;
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

pub fn read_manifest(run_dir: &Path) -> Result<RunManifest, ArtifactError> {
    let path = run_dir.join(MANIFEST_FILE);
    let raw = fs::read_to_string(&path).map_err(io_err(&path))?;
    serde_json::from_str(&raw).map_err(|source| ArtifactError::Json { path, source })
}

/// Replay the raw trace from disk, calling `on_batch` per decoded batch.
pub fn replay_trace(
    run_dir: &Path,
    mut on_batch: impl FnMut(RawSampleBatch),
) -> Result<u64, ArtifactError> {
    let path = run_dir.join(RAW_TRACE_FILE);
    let mut reader = BufReader::new(File::open(&path).map_err(io_err(&path))?);
    let mut batches = 0u64;
    loop {
        match read_message(&mut reader) {
            Ok(Message::RawSampleBatch(batch)) => {
                batches += 1;
                on_batch(batch);
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
    let manifest = read_manifest(run_dir)?;
    let known: Vec<u32> = manifest.sources.iter().map(|s| s.local_source_id).collect();

    let mut report = InspectReport {
        run_id: manifest.run_id.clone(),
        node_id: manifest.node_id.clone(),
        ..Default::default()
    };

    let batches = replay_trace(run_dir, |batch| {
        report.samples += batch.samples.len() as u64;
        for sample in &batch.samples {
            if !known.contains(&sample.source_id) {
                report.unknown_source_samples += 1;
                continue;
            }
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
    })?;
    report.batches = batches;
    Ok(report)
}
