//! macOS acquisition through Apple's `/usr/bin/powermetrics` tool.
//!
//! `powermetrics` stays behind the daemon's privilege boundary because the
//! tool must run as root. It emits NUL-delimited property lists; this backend
//! preserves native interval-average milliwatt estimates as independent CPU,
//! GPU, and ANE sources, together with each frame's raw `elapsed_ns`. It
//! deliberately omits `combined_power`: aggregation and integration belong in
//! an offline benchmark or reporting layer, not the acquisition daemon.

use plist::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, BufRead, BufReader, Cursor, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use thiserror::Error;
use tracing::warn;
use wattch_core::{
    validate_source_set, EvidenceClass, RawValue, SamplingMode, SourceDescriptor, SourceKind,
    Temporality, Unit, INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1, INTERVAL_ANCHOR_LABEL,
    INTERVAL_DURATION_SOURCE_UID_LABEL, INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP,
    INTERVAL_TIMELINE_RECONSTRUCTION_LABEL,
};
use wattch_daemon::{AcquiredReading, SampleBackend};

const POWERMETRICS_PATH: &str = "/usr/bin/powermetrics";
const MIN_SAMPLE_INTERVAL_MS: u64 = 100;
const MAX_SAMPLE_INTERVAL_MS: u64 = 10_000;
const MAX_PLIST_FRAME_BYTES: usize = 16 * 1024 * 1024;
const MAX_STDERR_BYTES: usize = 8 * 1024;

const CPU_ID: u32 = 1;
const GPU_ID: u32 = 2;
const ANE_ID: u32 = 3;
const ELAPSED_NS_ID: u32 = 4;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum PowerDomain {
    Cpu,
    Gpu,
    Ane,
}

impl PowerDomain {
    fn source_id(self) -> u32 {
        match self {
            Self::Cpu => CPU_ID,
            Self::Gpu => GPU_ID,
            Self::Ane => ANE_ID,
        }
    }

    fn metric(self) -> &'static str {
        match self {
            Self::Cpu => "processor.cpu_power",
            Self::Gpu => "processor.gpu_power",
            Self::Ane => "processor.ane_power",
        }
    }

    fn source_path(self) -> &'static str {
        match self {
            Self::Cpu => "cpu/package/0/power",
            Self::Gpu => "gpu/0/power",
            Self::Ane => "accelerator/ane/0/power",
        }
    }

    fn source_kind(self) -> SourceKind {
        match self {
            Self::Cpu => SourceKind::CpuPower,
            Self::Gpu => SourceKind::GpuPower,
            Self::Ane => SourceKind::AnePower,
        }
    }
}

#[derive(Debug, Error)]
pub enum PowermetricsError {
    #[error("apple-powermetrics is only available on macOS")]
    UnsupportedPlatform,

    #[error(
        "powermetrics sample interval must be between {MIN_SAMPLE_INTERVAL_MS} and \
         {MAX_SAMPLE_INTERVAL_MS} ms, got {0}"
    )]
    InvalidSampleInterval(u64),

    #[error("failed to start {path}: {source}")]
    Spawn { path: PathBuf, source: io::Error },

    #[error("powermetrics child did not expose its {0} pipe")]
    MissingPipe(&'static str),

    #[error("powermetrics exited before producing a power sample: {0}")]
    ProcessExited(String),

    #[error("powermetrics produced no power sample within {0:?}: {1}")]
    StartupTimeout(Duration, String),

    #[error("invalid apple-powermetrics source descriptors: {0}")]
    InvalidSources(#[from] wattch_core::Error),
}

#[derive(Debug, Default)]
struct LatestSample {
    generation: u64,
    readings: Vec<(u32, RawValue)>,
    received_at: Option<Instant>,
}

#[derive(Debug, Clone, Copy)]
struct GenerationCursor {
    last_generation: u64,
}

impl GenerationCursor {
    fn at_current(latest: &Mutex<LatestSample>) -> Self {
        let latest = latest
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        Self {
            last_generation: latest.generation,
        }
    }

    fn reset_to_current(&mut self, latest: &Mutex<LatestSample>) {
        *self = Self::at_current(latest);
    }

    fn advance_to(&mut self, generation: u64) -> bool {
        if generation == self.last_generation {
            return false;
        }
        self.last_generation = generation;
        true
    }
}

/// Handle onto one daemon-wide `powermetrics` collector.
///
/// Clones share the child process and latest sample, while each clone tracks
/// the last collector generation it emitted. Multiple clients therefore do
/// not spawn multiple privileged tools or receive duplicated Apple samples.
pub struct PowermetricsBackend {
    descriptors: Arc<Vec<SourceDescriptor>>,
    advertised_ids: BTreeSet<u32>,
    latest: Arc<Mutex<LatestSample>>,
    cursor: GenerationCursor,
    _process: Arc<ProcessGuard>,
}

impl Clone for PowermetricsBackend {
    fn clone(&self) -> Self {
        let cursor = GenerationCursor::at_current(self.latest.as_ref());
        Self {
            descriptors: Arc::clone(&self.descriptors),
            advertised_ids: self.advertised_ids.clone(),
            latest: Arc::clone(&self.latest),
            cursor,
            _process: Arc::clone(&self._process),
        }
    }
}

impl PowermetricsBackend {
    /// Start one machine-wide Apple power collector.
    ///
    /// Startup waits for the first valid sample so permission failures and
    /// unsupported hardware are reported before clients can connect.
    pub fn new(node_id: &str, sample_interval_ms: u64) -> Result<Self, PowermetricsError> {
        if !cfg!(target_os = "macos") {
            return Err(PowermetricsError::UnsupportedPlatform);
        }
        Self::start_command(Path::new(POWERMETRICS_PATH), node_id, sample_interval_ms)
    }

    fn start_command(
        command_path: &Path,
        node_id: &str,
        sample_interval_ms: u64,
    ) -> Result<Self, PowermetricsError> {
        if !(MIN_SAMPLE_INTERVAL_MS..=MAX_SAMPLE_INTERVAL_MS).contains(&sample_interval_ms) {
            return Err(PowermetricsError::InvalidSampleInterval(sample_interval_ms));
        }

        let mut child = Command::new(command_path)
            .args([
                "--format",
                "plist",
                "--samplers",
                "cpu_power,gpu_power,ane_power",
                "--sample-rate",
                &sample_interval_ms.to_string(),
                "--sample-count",
                "-1",
                "--buffer-size",
                "0",
                "--poweravg",
                "0",
                "--handle-invalid-values",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|source| PowermetricsError::Spawn {
                path: command_path.to_path_buf(),
                source,
            })?;

        let Some(stdout) = child.stdout.take() else {
            stop_child(&mut child);
            return Err(PowermetricsError::MissingPipe("stdout"));
        };
        let Some(stderr) = child.stderr.take() else {
            stop_child(&mut child);
            return Err(PowermetricsError::MissingPipe("stderr"));
        };

        let latest = Arc::new(Mutex::new(LatestSample::default()));
        let stderr_bytes = Arc::new(Mutex::new(Vec::new()));
        let (startup_tx, startup_rx) = mpsc::sync_channel(1);

        let reader_latest = Arc::clone(&latest);
        let reader = thread::spawn(move || collect_stdout(stdout, reader_latest, startup_tx));
        let stderr_output = Arc::clone(&stderr_bytes);
        let stderr_reader = thread::spawn(move || drain_stderr(stderr, stderr_output));

        let process = Arc::new(ProcessGuard {
            child: Mutex::new(child),
            reader: Mutex::new(Some(reader)),
            stderr_reader: Mutex::new(Some(stderr_reader)),
            stderr_bytes,
        });

        let startup_timeout = Duration::from_millis(sample_interval_ms) + Duration::from_secs(5);
        let domains = match startup_rx.recv_timeout(startup_timeout) {
            Ok(Ok(domains)) => domains,
            Ok(Err(reason)) => {
                let detail = process.detail_or(reason);
                return Err(PowermetricsError::ProcessExited(detail));
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                return Err(PowermetricsError::StartupTimeout(
                    startup_timeout,
                    process.detail_or("no machine-readable power fields were emitted"),
                ));
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err(PowermetricsError::ProcessExited(
                    process.detail_or("collector thread stopped"),
                ));
            }
        };

        let descriptors = descriptors_for(node_id, command_path, sample_interval_ms, &domains);
        validate_source_set(&descriptors)?;
        let descriptors = Arc::new(descriptors);
        let advertised_ids = descriptors
            .iter()
            .map(|descriptor| descriptor.local_source_id)
            .collect();
        let cursor = GenerationCursor::at_current(latest.as_ref());

        Ok(Self {
            descriptors,
            advertised_ids,
            latest,
            cursor,
            _process: process,
        })
    }
}

impl SampleBackend for PowermetricsBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn begin_capture(&mut self) {
        self.cursor.reset_to_current(self.latest.as_ref());
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        take_latest_generation(&mut self.cursor, self.latest.as_ref(), &self.advertised_ids)
            .map(|(_, readings)| readings)
            .unwrap_or_default()
    }

    fn sample_acquired(&mut self) -> Vec<AcquiredReading> {
        let Some((received_at, readings)) =
            take_latest_generation(&mut self.cursor, self.latest.as_ref(), &self.advertised_ids)
        else {
            return Vec::new();
        };
        readings
            .into_iter()
            .map(|(local_source_id, value)| {
                AcquiredReading::new(local_source_id, value, received_at)
            })
            .collect()
    }
}

fn take_latest_generation(
    cursor: &mut GenerationCursor,
    latest: &Mutex<LatestSample>,
    advertised_ids: &BTreeSet<u32>,
) -> Option<(Instant, Vec<(u32, RawValue)>)> {
    let latest = latest
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if !cursor.advance_to(latest.generation) {
        return None;
    }
    let received_at = latest.received_at?;
    let readings = latest
        .readings
        .iter()
        .filter(|(source_id, _)| advertised_ids.contains(source_id))
        .cloned()
        .collect();
    Some((received_at, readings))
}

struct ProcessGuard {
    child: Mutex<Child>,
    reader: Mutex<Option<JoinHandle<()>>>,
    stderr_reader: Mutex<Option<JoinHandle<()>>>,
    stderr_bytes: Arc<Mutex<Vec<u8>>>,
}

fn stop_child(child: &mut Child) {
    if child.try_wait().ok().flatten().is_none() {
        let _ = child.kill();
    }
    let _ = child.wait();
}

impl ProcessGuard {
    fn detail_or(&self, fallback: impl Into<String>) -> String {
        let stderr = self
            .stderr_bytes
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let detail = String::from_utf8_lossy(&stderr).trim().to_string();
        if detail.is_empty() {
            fallback.into()
        } else {
            detail
        }
    }
}

impl Drop for ProcessGuard {
    fn drop(&mut self) {
        let child = self
            .child
            .get_mut()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        stop_child(child);

        if let Some(reader) = self
            .reader
            .get_mut()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take()
        {
            let _ = reader.join();
        }
        if let Some(reader) = self
            .stderr_reader
            .get_mut()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take()
        {
            let _ = reader.join();
        }
    }
}

fn collect_stdout(
    stdout: ChildStdout,
    latest: Arc<Mutex<LatestSample>>,
    startup_tx: mpsc::SyncSender<Result<BTreeSet<PowerDomain>, String>>,
) {
    let mut reader = BufReader::new(stdout);
    let mut startup_tx = Some(startup_tx);

    loop {
        let frame = match read_nul_frame(&mut reader, MAX_PLIST_FRAME_BYTES) {
            Ok(Some(frame)) => frame,
            Ok(None) => {
                if !send_startup_error(&mut startup_tx, "powermetrics stdout closed") {
                    warn!("powermetrics exited; future capture ticks will contain gaps");
                }
                return;
            }
            Err(error) => {
                send_startup_error(&mut startup_tx, error.to_string());
                warn!(error = %error, "stopping unreadable powermetrics stream");
                return;
            }
        };
        if frame.is_empty() {
            continue;
        }

        // Record receipt before plist parsing so later daemon polling retains
        // the age of this native collector generation instead of pretending
        // it was acquired at the client poll tick.
        let received_at = Instant::now();

        let parsed = match parse_power_frame(&frame) {
            Ok(parsed) if !parsed.readings.is_empty() => parsed,
            Ok(_) => continue,
            Err(error) => {
                warn!(error = %error, "skipping malformed powermetrics plist");
                continue;
            }
        };
        let (domains, readings) = into_raw_readings(parsed);

        {
            let mut state = latest
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            state.generation = state.generation.saturating_add(1);
            state.readings = readings;
            state.received_at = Some(received_at);
        }
        if let Some(sender) = startup_tx.take() {
            let _ = sender.send(Ok(domains));
        }
    }
}

fn into_raw_readings(parsed: ParsedPowerFrame) -> (BTreeSet<PowerDomain>, Vec<(u32, RawValue)>) {
    let domains = parsed.readings.iter().map(|(domain, _)| *domain).collect();
    let mut readings: Vec<_> = parsed
        .readings
        .into_iter()
        .map(|(domain, milliwatts)| (domain.source_id(), RawValue::F64(milliwatts)))
        .collect();
    readings.push((ELAPSED_NS_ID, RawValue::I64(parsed.elapsed_ns)));
    (domains, readings)
}

fn send_startup_error(
    sender: &mut Option<mpsc::SyncSender<Result<BTreeSet<PowerDomain>, String>>>,
    reason: impl Into<String>,
) -> bool {
    if let Some(sender) = sender.take() {
        let _ = sender.send(Err(reason.into()));
        true
    } else {
        false
    }
}

fn drain_stderr(mut stderr: ChildStderr, output: Arc<Mutex<Vec<u8>>>) {
    let mut chunk = [0u8; 1024];
    loop {
        match stderr.read(&mut chunk) {
            Ok(0) | Err(_) => return,
            Ok(read) => {
                let mut stored = output
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                let remaining = MAX_STDERR_BYTES.saturating_sub(stored.len());
                stored.extend_from_slice(&chunk[..read.min(remaining)]);
            }
        }
    }
}

fn read_nul_frame<R: BufRead>(reader: &mut R, max_len: usize) -> io::Result<Option<Vec<u8>>> {
    let mut frame = Vec::new();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if frame.is_empty() {
                Ok(None)
            } else {
                // The format is NUL-separated, not necessarily NUL-terminated.
                // In particular, powermetrics can close stdout after writing a
                // final complete plist without a trailing separator. Return it
                // to the plist parser, which still rejects genuinely truncated
                // XML instead of discarding a potentially valid last sample.
                Ok(Some(frame))
            };
        }

        let delimiter = available.iter().position(|byte| *byte == 0);
        let take = delimiter.unwrap_or(available.len());
        if frame.len().saturating_add(take) > max_len {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("powermetrics plist exceeds {max_len} bytes"),
            ));
        }
        frame.extend_from_slice(&available[..take]);
        reader.consume(take + usize::from(delimiter.is_some()));
        if delimiter.is_some() {
            return Ok(Some(frame));
        }
    }
}

#[derive(Debug, Error)]
enum ParseError {
    #[error("invalid property list: {0}")]
    Plist(#[from] plist::Error),

    #[error("powermetrics property-list root is not a dictionary")]
    RootNotDictionary,

    #[error("powermetrics field processor.{0} is not a finite non-negative number")]
    InvalidPower(&'static str),

    #[error("powermetrics field elapsed_ns is missing or is not a positive signed 64-bit integer")]
    InvalidElapsedNs,
}

#[derive(Debug, PartialEq)]
struct ParsedPowerFrame {
    elapsed_ns: i64,
    readings: Vec<(PowerDomain, f64)>,
}

fn parse_power_frame(frame: &[u8]) -> Result<ParsedPowerFrame, ParseError> {
    let root = Value::from_reader(Cursor::new(frame))?;
    let root = root.as_dictionary().ok_or(ParseError::RootNotDictionary)?;
    let elapsed_ns = root
        .get("elapsed_ns")
        .and_then(plist_positive_i64)
        .ok_or(ParseError::InvalidElapsedNs)?;
    let Some(processor) = root.get("processor").and_then(Value::as_dictionary) else {
        return Ok(ParsedPowerFrame {
            elapsed_ns,
            readings: Vec::new(),
        });
    };
    if processor
        .get("invalid")
        .and_then(Value::as_boolean)
        .unwrap_or(false)
    {
        return Ok(ParsedPowerFrame {
            elapsed_ns,
            readings: Vec::new(),
        });
    }

    let mut readings = Vec::new();
    for (domain, key) in [
        (PowerDomain::Cpu, "cpu_power"),
        (PowerDomain::Gpu, "gpu_power"),
        (PowerDomain::Ane, "ane_power"),
    ] {
        let Some(value) = processor.get(key) else {
            continue;
        };
        let value = plist_number(value).ok_or(ParseError::InvalidPower(key))?;
        if !value.is_finite() || value < 0.0 {
            return Err(ParseError::InvalidPower(key));
        }
        readings.push((domain, value));
    }
    Ok(ParsedPowerFrame {
        elapsed_ns,
        readings,
    })
}

fn plist_number(value: &Value) -> Option<f64> {
    value
        .as_real()
        .or_else(|| value.as_signed_integer().map(|value| value as f64))
        .or_else(|| value.as_unsigned_integer().map(|value| value as f64))
}

fn plist_positive_i64(value: &Value) -> Option<i64> {
    let value = value.as_signed_integer().or_else(|| {
        value
            .as_unsigned_integer()
            .and_then(|value| value.try_into().ok())
    })?;
    (value > 0).then_some(value)
}

fn descriptors_for(
    node_id: &str,
    command_path: &Path,
    sample_interval_ms: u64,
    domains: &BTreeSet<PowerDomain>,
) -> Vec<SourceDescriptor> {
    let duration_uid = format!("powermetrics://{node_id}/frame/elapsed_ns");
    let mut descriptors: Vec<_> = domains
        .iter()
        .copied()
        .map(|domain| {
            let mut labels = BTreeMap::new();
            labels.insert("apple_metric".to_string(), domain.metric().to_string());
            labels.insert("native_unit".to_string(), "mW".to_string());
            labels.insert(
                "estimate_scope".to_string(),
                "same-device-energy-optimization".to_string(),
            );
            labels.insert("tool_path".to_string(), command_path.display().to_string());
            labels.insert(
                INTERVAL_DURATION_SOURCE_UID_LABEL.to_string(),
                duration_uid.clone(),
            );
            labels.insert(
                INTERVAL_ANCHOR_LABEL.to_string(),
                INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP.to_string(),
            );
            labels.insert(
                INTERVAL_TIMELINE_RECONSTRUCTION_LABEL.to_string(),
                INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1.to_string(),
            );
            SourceDescriptor {
                local_source_id: domain.source_id(),
                source_uid: format!("powermetrics://{node_id}/{}", domain.metric()),
                node_id: node_id.to_string(),
                backend: "apple-powermetrics".to_string(),
                kind: domain.source_kind(),
                unit: Unit::Milliwatt,
                temporality: Temporality::IntervalAverage,
                evidence_class: EvidenceClass::ToolDerivedEstimate,
                source_path: domain.source_path().to_string(),
                parent_source_uid: None,
                clock_domain: "monotonic".to_string(),
                sampling_mode: SamplingMode::Periodic,
                nominal_period_ns: Some(sample_interval_ms * 1_000_000),
                max_value: None,
                resolution: None,
                labels,
            }
        })
        .collect();
    let mut duration_labels = BTreeMap::new();
    duration_labels.insert("apple_metric".to_string(), "elapsed_ns".to_string());
    duration_labels.insert("native_unit".to_string(), "ns".to_string());
    duration_labels.insert("tool_path".to_string(), command_path.display().to_string());
    descriptors.push(SourceDescriptor {
        local_source_id: ELAPSED_NS_ID,
        source_uid: duration_uid,
        node_id: node_id.to_string(),
        backend: "apple-powermetrics".to_string(),
        kind: SourceKind::SampleIntervalDuration,
        unit: Unit::Nanosecond,
        temporality: Temporality::Delta,
        evidence_class: EvidenceClass::ToolDerivedEstimate,
        source_path: "collector/powermetrics/frame/elapsed_ns".to_string(),
        parent_source_uid: None,
        clock_domain: "monotonic".to_string(),
        sampling_mode: SamplingMode::Periodic,
        nominal_period_ns: Some(sample_interval_ms * 1_000_000),
        max_value: None,
        resolution: Some(1.0),
        labels: duration_labels,
    });
    descriptors
}

#[cfg(test)]
mod tests {
    use super::*;
    use wattch_core::validate_source_set;

    const SAMPLE: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>elapsed_ns</key><integer>1000000000</integer>
  <key>processor</key>
  <dict>
    <key>cpu_power</key><real>1234.5</real>
    <key>gpu_power</key><real>250</real>
    <key>ane_power</key><integer>75</integer>
    <key>combined_power</key><real>1559.5</real>
  </dict>
</dict>
</plist>"#;

    #[test]
    fn parses_native_power_fields_without_combined_power() {
        let frame = parse_power_frame(SAMPLE.as_bytes()).unwrap();
        assert_eq!(frame.elapsed_ns, 1_000_000_000);
        assert_eq!(
            frame.readings,
            vec![
                (PowerDomain::Cpu, 1234.5),
                (PowerDomain::Gpu, 250.0),
                (PowerDomain::Ane, 75.0),
            ]
        );
    }

    #[test]
    fn native_elapsed_interval_is_emitted_as_a_same_frame_raw_source() {
        let parsed = parse_power_frame(SAMPLE.as_bytes()).unwrap();
        let (domains, readings) = into_raw_readings(parsed);
        assert_eq!(domains.len(), 3);
        assert_eq!(
            readings.last(),
            Some(&(ELAPSED_NS_ID, RawValue::I64(1_000_000_000)))
        );
    }

    #[test]
    fn invalid_processor_sample_is_skipped() {
        let invalid = r#"<?xml version="1.0"?><plist version="1.0"><dict>
          <key>elapsed_ns</key><integer>1000000000</integer>
          <key>processor</key><dict><key>invalid</key><true/></dict>
        </dict></plist>"#;
        assert!(parse_power_frame(invalid.as_bytes())
            .unwrap()
            .readings
            .is_empty());
    }

    #[test]
    fn elapsed_interval_is_mandatory_raw_evidence() {
        let missing = r#"<?xml version="1.0"?><plist version="1.0"><dict>
          <key>processor</key><dict><key>cpu_power</key><real>10</real></dict>
        </dict></plist>"#;
        assert!(matches!(
            parse_power_frame(missing.as_bytes()),
            Err(ParseError::InvalidElapsedNs)
        ));

        let zero = r#"<?xml version="1.0"?><plist version="1.0"><dict>
          <key>elapsed_ns</key><integer>0</integer>
          <key>processor</key><dict><key>cpu_power</key><real>10</real></dict>
        </dict></plist>"#;
        assert!(matches!(
            parse_power_frame(zero.as_bytes()),
            Err(ParseError::InvalidElapsedNs)
        ));
    }

    #[test]
    fn descriptors_preserve_milliwatts_and_estimate_semantics() {
        let domains = [PowerDomain::Cpu, PowerDomain::Gpu, PowerDomain::Ane]
            .into_iter()
            .collect();
        let descriptors =
            descriptors_for("mac-node", Path::new(POWERMETRICS_PATH), 1_000, &domains);
        validate_source_set(&descriptors).unwrap();
        assert_eq!(descriptors.len(), 4);
        let power_sources = &descriptors[..3];
        assert!(power_sources.iter().all(|source| {
            source.unit == Unit::Milliwatt
                && source.temporality == Temporality::IntervalAverage
                && source.evidence_class == EvidenceClass::ToolDerivedEstimate
                && source.labels.get(INTERVAL_DURATION_SOURCE_UID_LABEL)
                    == Some(&descriptors[3].source_uid)
                && source.labels.get(INTERVAL_ANCHOR_LABEL).map(String::as_str)
                    == Some(INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP)
                && source
                    .labels
                    .get(INTERVAL_TIMELINE_RECONSTRUCTION_LABEL)
                    .map(String::as_str)
                    == Some(INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1)
        }));
        assert_eq!(descriptors[0].kind, SourceKind::CpuPower);
        assert_eq!(descriptors[1].kind, SourceKind::GpuPower);
        assert_eq!(descriptors[2].kind, SourceKind::AnePower);
        assert_eq!(descriptors[0].nominal_period_ns, Some(1_000_000_000));
        let duration = &descriptors[3];
        assert_eq!(duration.local_source_id, ELAPSED_NS_ID);
        assert_eq!(duration.kind, SourceKind::SampleIntervalDuration);
        assert_eq!(duration.unit, Unit::Nanosecond);
        assert_eq!(duration.temporality, Temporality::Delta);
    }

    #[test]
    fn reads_nul_separated_frames_and_an_unterminated_final_record() {
        let bytes = b"one\0two\0";
        let mut reader = BufReader::new(bytes.as_slice());
        assert_eq!(
            read_nul_frame(&mut reader, 10).unwrap(),
            Some(b"one".to_vec())
        );
        assert_eq!(
            read_nul_frame(&mut reader, 10).unwrap(),
            Some(b"two".to_vec())
        );
        assert_eq!(read_nul_frame(&mut reader, 10).unwrap(), None);

        let mut final_record = BufReader::new(SAMPLE.as_bytes());
        let frame = read_nul_frame(&mut final_record, SAMPLE.len() + 1)
            .unwrap()
            .expect("final non-NUL-terminated plist should be returned");
        assert_eq!(parse_power_frame(&frame).unwrap().readings.len(), 3);
        assert_eq!(
            read_nul_frame(&mut final_record, SAMPLE.len() + 1).unwrap(),
            None
        );

        let mut truncated = BufReader::new(b"<plist>".as_slice());
        let frame = read_nul_frame(&mut truncated, 10).unwrap().unwrap();
        assert!(parse_power_frame(&frame).is_err());
    }

    #[test]
    fn enforces_property_list_size_limit_before_copying_more_data() {
        let mut reader = BufReader::new(b"12345\0".as_slice());
        assert_eq!(
            read_nul_frame(&mut reader, 4).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn generation_cursor_requires_a_post_snapshot_generation() {
        let latest = Mutex::new(LatestSample {
            generation: 7,
            readings: vec![(CPU_ID, RawValue::F64(100.0))],
            received_at: Some(Instant::now()),
        });
        let mut cursor = GenerationCursor::at_current(&latest);

        assert!(!cursor.advance_to(7), "the cached generation is stale");
        assert!(cursor.advance_to(8), "the next generation is fresh");
        assert!(!cursor.advance_to(8), "one generation is emitted once");
    }

    #[test]
    fn capture_reset_discards_values_published_before_the_new_capture() {
        let latest = Mutex::new(LatestSample {
            generation: 3,
            readings: Vec::new(),
            received_at: Some(Instant::now()),
        });
        let mut cursor = GenerationCursor::at_current(&latest);
        assert!(cursor.advance_to(4));

        latest
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .generation = 5;
        cursor.reset_to_current(&latest);

        assert!(!cursor.advance_to(5));
        assert!(cursor.advance_to(6));
    }

    #[test]
    fn delayed_poll_retains_collector_receipt_instant_and_stale_generation_is_empty() {
        let received_at = Instant::now() - Duration::from_millis(75);
        let latest = Mutex::new(LatestSample {
            generation: 8,
            readings: vec![(CPU_ID, RawValue::F64(100.0))],
            received_at: Some(received_at),
        });
        let advertised_ids = [CPU_ID].into_iter().collect();
        let mut cursor = GenerationCursor { last_generation: 7 };

        let (observed_at, readings) =
            take_latest_generation(&mut cursor, &latest, &advertised_ids).unwrap();

        assert_eq!(observed_at, received_at);
        assert_eq!(readings, vec![(CPU_ID, RawValue::F64(100.0))]);
        assert!(take_latest_generation(&mut cursor, &latest, &advertised_ids).is_none());
    }
}
