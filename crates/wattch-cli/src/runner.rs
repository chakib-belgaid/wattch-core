//! Shell-free command execution bound to one raw Wattch capture.
//!
//! This module records process boundaries and command metadata only. It does
//! not aggregate sources, integrate power, or interpret the captured values.

use crate::artifact::{
    write_json_sidecar, CommandLifecycleRecord, JsonSidecar, RunWriter, CLOCK_INSTANCE_ID_LABEL,
    COMMAND_LIFECYCLE_FILE,
};
use crate::client::{CaptureFrame, CaptureSession, ClientError, WattchClient};
use anyhow::{anyhow, bail, Context, Result};
use nix::errno::Errno;
use nix::sys::signal::{killpg, Signal};
use nix::unistd::Pid;
use serde::Serialize;
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::process::CommandExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use wattch_core::{RawSampleBatch, RunManifest, SamplingMode, SourceDescriptor, Temporality};
use wattch_protocol::frame::WireError;
use wattch_protocol::messages::{validate_record_event, RecordEvent, StopCaptureAck};

pub const COMMAND_SCHEMA_VERSION: u32 = 1;
pub const TARGET_STDOUT_FILE: &str = "target.stdout.log";
pub const TARGET_STDERR_FILE: &str = "target.stderr.log";

const TARGET_SPAWN: &str = "TARGET_SPAWN";
const TARGET_EXEC: &str = "TARGET_EXEC";
const TARGET_EXIT: &str = "TARGET_EXIT";
const CAPTURE_POLL: Duration = Duration::from_millis(25);
const STOP_GRACE: Duration = Duration::from_secs(5);
const PROCESS_TERM_GRACE: Duration = Duration::from_millis(500);
const PROCESS_KILL_GRACE: Duration = Duration::from_millis(500);
const PROCESS_POLL: Duration = Duration::from_millis(10);

#[derive(Debug)]
pub struct RunCommandOptions {
    pub socket: PathBuf,
    pub out: PathBuf,
    pub interval_ms: u64,
    pub run_id: String,
    pub labels: BTreeMap<String, String>,
    pub argv: Vec<String>,
}

/// Stable summary emitted by `wattch run --json`.
#[derive(Clone, Debug, Serialize)]
pub struct RunCommandResult {
    pub run_id: String,
    pub run_dir: PathBuf,
    /// Direct child status, or `128 + signal` for a Unix signal termination.
    pub exit_code: i32,
    pub signal: Option<i32>,
    pub batches: u64,
    pub samples: u64,
}

impl RunCommandResult {
    /// Process exit code for the `wattch` wrapper itself.
    ///
    /// Normal child exits pass through exactly. Unix signals use the familiar
    /// `128 + signal` convention. Values outside the portable byte range map
    /// to generic failure instead of wrapping.
    pub fn wrapper_exit_code(&self) -> u8 {
        self.exit_code.try_into().unwrap_or(1)
    }
}

#[derive(Debug, Serialize)]
struct CommandMetadata<'a> {
    schema_version: u32,
    run_id: &'a str,
    argv: &'a [String],
    cwd: &'a Path,
    environment: EnvironmentMetadata,
    stdio: StdioMetadata,
    process_group: &'static str,
    labels: &'a BTreeMap<String, String>,
}

#[derive(Debug, Serialize)]
struct EnvironmentMetadata {
    inherit: bool,
}

#[derive(Debug, Serialize)]
struct StdioMetadata {
    stdin: &'static str,
    stdout: &'static str,
    stderr: &'static str,
}

/// Run one direct command while continuously draining a raw capture.
///
/// `argv` is passed directly to [`Command`]; no shell is inserted. The child
/// inherits the caller's environment, runs in the recorded current directory,
/// receives a null stdin, and writes stdout/stderr into the run artifact.
pub fn run_command(options: RunCommandOptions) -> Result<RunCommandResult> {
    validate_options(&options)?;

    let cwd = std::env::current_dir().context("failed to resolve command working directory")?;
    let out = if options.out.is_absolute() {
        options.out.clone()
    } else {
        cwd.join(&options.out)
    };

    let mut client = WattchClient::connect(&options.socket)?;
    let hello = client.hello().clone();
    let tail_tracker = SampleBoundaryTracker::new(&hello.sources, options.interval_ms);
    let mut manifest_labels = options.labels.clone();
    manifest_labels.insert(
        CLOCK_INSTANCE_ID_LABEL.to_string(),
        hello.clock_instance_id.clone(),
    );
    let manifest = RunManifest {
        run_id: options.run_id.clone(),
        node_id: hello.node_id.clone(),
        protocol_version: hello.protocol_version.clone(),
        clock_instance_id: hello.clock_instance_id,
        wattch_version: env!("CARGO_PKG_VERSION").to_string(),
        created_unix_ns: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos() as u64)
            .unwrap_or(0),
        sources: hello.sources,
        labels: manifest_labels,
    };

    let mut writer = RunWriter::create(&out, &manifest)?;
    let mut lifecycle = CommandLifecycleWriter::create(writer.run_dir(), &options.run_id)?;
    let command_metadata = CommandMetadata {
        schema_version: COMMAND_SCHEMA_VERSION,
        run_id: &options.run_id,
        argv: &options.argv,
        cwd: &cwd,
        environment: EnvironmentMetadata { inherit: true },
        stdio: StdioMetadata {
            stdin: "null",
            stdout: TARGET_STDOUT_FILE,
            stderr: TARGET_STDERR_FILE,
        },
        process_group: "dedicated",
        labels: &options.labels,
    };
    write_json_sidecar(writer.run_dir(), JsonSidecar::Command, &command_metadata)?;

    let stdout = create_log(&writer.run_dir().join(TARGET_STDOUT_FILE))?;
    let stderr = create_log(&writer.run_dir().join(TARGET_STDERR_FILE))?;

    let mut session = match client.start_capture(&options.run_id, options.interval_ms) {
        Ok(session) => session,
        Err(error) => {
            let error = anyhow!(error).context("failed to start command capture");
            return Err(finish_after_error(writer, &mut lifecycle, error));
        }
    };

    let mut tail_tracker = tail_tracker;
    if let Err(error) = wait_for_pre_roll(&mut session, &mut writer, &mut tail_tracker) {
        let cleanup = stop_and_drain(&mut session, &mut writer);
        drop(session);
        let mut error = error.context("failed to establish a pre-command sample boundary");
        if let Err(cleanup_error) = cleanup {
            error = error.context(format!(
                "capture cleanup after pre-roll failure also failed: {cleanup_error:#}"
            ));
        }
        return Err(finish_after_error(writer, &mut lifecycle, error));
    }

    if let Err(error) = session.record_event(marker(
        &options.run_id,
        "target-spawn",
        TARGET_SPAWN,
        &options.labels,
        &[],
    )) {
        let cleanup = stop_and_drain(&mut session, &mut writer);
        drop(session);
        let mut error = anyhow!(error).context("failed to record TARGET_SPAWN");
        if let Err(cleanup_error) = cleanup {
            error = error.context(format!(
                "capture cleanup after marker failure also failed: {cleanup_error:#}"
            ));
        }
        return Err(finish_after_error(writer, &mut lifecycle, error));
    }
    if let Err(error) = wait_for_event_ack(&mut session, &mut writer, "target-spawn") {
        let cleanup = stop_and_drain(&mut session, &mut writer);
        drop(session);
        let mut error = error.context("failed to acknowledge TARGET_SPAWN");
        if let Err(cleanup_error) = cleanup {
            error = error.context(format!(
                "capture cleanup after marker acknowledgement failure also failed: {cleanup_error:#}"
            ));
        }
        return Err(finish_after_error(writer, &mut lifecycle, error));
    }

    let mut command = Command::new(&options.argv[0]);
    command
        .args(&options.argv[1..])
        .current_dir(&cwd)
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(source) => {
            let cleanup = stop_and_drain(&mut session, &mut writer);
            drop(session);
            let mut error =
                anyhow!(source).context(format!("failed to spawn command {:?}", options.argv[0]));
            if let Err(cleanup_error) = cleanup {
                error = error.context(format!(
                    "capture cleanup after spawn failure also failed: {cleanup_error:#}"
                ));
            }
            return Err(finish_after_error(writer, &mut lifecycle, error));
        }
    };

    let process_group_id = match i32::try_from(child.id()) {
        Ok(process_group_id) => process_group_id,
        Err(source) => {
            let terminate_error = child.kill().err();
            let _ = child.wait();
            let cleanup = stop_and_drain(&mut session, &mut writer);
            drop(session);
            let mut error = anyhow!(source).context("child pid does not fit in i32");
            if let Some(terminate_error) = terminate_error {
                error = error.context(format!(
                    "terminating the unrepresentable child pid also failed: {terminate_error}"
                ));
            }
            if let Err(cleanup_error) = cleanup {
                error = error.context(format!(
                    "capture cleanup after pid conversion failure also failed: {cleanup_error:#}"
                ));
            }
            return Err(finish_after_error(writer, &mut lifecycle, error));
        }
    };
    let capture_result = capture_child(
        &mut session,
        &mut writer,
        &mut child,
        process_group_id,
        &options.run_id,
        &options.labels,
        tail_tracker,
    );
    let capture_result = match capture_result {
        Ok(result) => Ok(result),
        Err(error) => match terminate_process_group(&mut child, process_group_id) {
            Ok(()) => Err(error),
            Err(cleanup_error) => Err(error.context(format!(
                "bounded process-group cleanup also failed: {cleanup_error:#}"
            ))),
        },
    };
    drop(session);

    let (status, acknowledgement) = match capture_result {
        Ok(result) => result,
        Err(error) => return Err(finish_after_error(writer, &mut lifecycle, error)),
    };
    let (exit_code, signal) = reported_status(&status);
    let run_dir = match writer.finish() {
        Ok(run_dir) => run_dir,
        Err(error) => {
            let error = anyhow!(error).context("failed to finish command trace artifacts");
            let reason = format!("{error:#}");
            let _ = lifecycle.incomplete(&reason);
            return Err(error);
        }
    };
    lifecycle
        .complete(
            exit_code,
            signal,
            acknowledgement.batches_sent,
            acknowledgement.samples_sent,
        )
        .with_context(|| {
            format!(
                "failed to finalize command lifecycle; run artifact retained at {}",
                run_dir.display()
            )
        })?;
    Ok(RunCommandResult {
        run_id: options.run_id,
        run_dir,
        exit_code,
        signal,
        batches: acknowledgement.batches_sent,
        samples: acknowledgement.samples_sent,
    })
}

fn validate_options(options: &RunCommandOptions) -> Result<()> {
    if options.argv.is_empty() {
        bail!("command argv must not be empty");
    }
    if options.argv.iter().any(|argument| argument.contains('\0')) {
        bail!("command argv entries must not contain NUL bytes");
    }
    if options.labels.contains_key(CLOCK_INSTANCE_ID_LABEL) {
        bail!("--label key {CLOCK_INSTANCE_ID_LABEL:?} is reserved");
    }

    // Validate every augmented marker before creating files or connecting.
    for event in [
        marker(
            &options.run_id,
            "target-spawn",
            TARGET_SPAWN,
            &options.labels,
            &[],
        ),
        marker(
            &options.run_id,
            "target-exec",
            TARGET_EXEC,
            &options.labels,
            &[("pid", "0")],
        ),
        marker(
            &options.run_id,
            "target-exit",
            TARGET_EXIT,
            &options.labels,
            &[("returncode", "0"), ("signal", "0")],
        ),
    ] {
        validate_record_event(&event).map_err(|reason| anyhow!(reason))?;
    }
    Ok(())
}

fn create_log(path: &Path) -> Result<File> {
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .with_context(|| format!("failed to create {}", path.display()))
}

fn capture_child(
    session: &mut CaptureSession<'_>,
    writer: &mut RunWriter,
    child: &mut Child,
    process_group_id: i32,
    run_id: &str,
    labels: &BTreeMap<String, String>,
    mut tail_tracker: SampleBoundaryTracker,
) -> Result<(ExitStatus, StopCaptureAck)> {
    let pid = child.id().to_string();
    session
        .record_event(marker(
            run_id,
            "target-exec",
            TARGET_EXEC,
            labels,
            &[("pid", &pid)],
        ))
        .context("failed to record TARGET_EXEC")?;

    let mut status = None;
    let mut exit_event_ns = None;
    let mut tail_deadline = None;
    let mut tail_error = None;
    let mut stop_deadline = None;
    loop {
        if status.is_none() {
            status = child.try_wait().context("failed to poll command status")?;
            if let Some(exit_status) = status.as_ref() {
                terminate_process_group(child, process_group_id)
                    .context("failed to clean up command process group")?;
                let (exit_code, signal) = reported_status(exit_status);
                let returncode = exit_code.to_string();
                let mut extras = vec![("returncode", returncode.as_str())];
                let signal_text = signal.map(|value| value.to_string());
                if let Some(signal_text) = signal_text.as_deref() {
                    extras.push(("signal", signal_text));
                }
                session
                    .record_event(marker(run_id, "target-exit", TARGET_EXIT, labels, &extras))
                    .context("failed to record TARGET_EXIT")?;
            }
        }

        if stop_deadline.is_none() {
            if let Some(exit_ns) = exit_event_ns {
                if tail_tracker.ready(exit_ns) {
                    session.stop().context("failed to stop command capture")?;
                    stop_deadline = Some(stop_deadline_from_now()?);
                } else if tail_deadline.is_none() {
                    tail_deadline = Some(
                        Instant::now()
                            .checked_add(tail_tracker.timeout())
                            .ok_or_else(|| anyhow!("interval tail deadline is too large"))?,
                    );
                } else if tail_deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                    tail_error = Some(anyhow!(
                        "required sources did not emit a fresh post-exit sample boundary before the deadline"
                    ));
                    session.stop().context("failed to stop command capture")?;
                    stop_deadline = Some(stop_deadline_from_now()?);
                }
            }
        }

        let timeout = if let Some(deadline) = stop_deadline {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                bail!("daemon did not acknowledge StopCapture before the deadline");
            }
            remaining.min(CAPTURE_POLL)
        } else if let Some(deadline) = tail_deadline {
            deadline
                .saturating_duration_since(Instant::now())
                .max(Duration::from_millis(1))
                .min(CAPTURE_POLL)
        } else {
            CAPTURE_POLL
        };
        match session.read_next(timeout) {
            Ok(frame) => {
                match &frame {
                    CaptureFrame::RawSampleBatch(batch) => tail_tracker.observe(batch),
                    CaptureFrame::RecordEventAck(ack) if ack.event.event_id == "target-exit" => {
                        exit_event_ns = Some(ack.event.monotonic_ns);
                    }
                    _ => {}
                }
                if let Some(acknowledgement) = persist_frame(writer, frame)? {
                    if let Some(error) = tail_error {
                        return Err(error);
                    }
                    let status = status
                        .ok_or_else(|| anyhow!("capture stopped before the command returned"))?;
                    return Ok((status, acknowledgement));
                }
            }
            Err(error) if client_timeout(&error) => continue,
            Err(error) => return Err(error.into()),
        }
    }
}

fn wait_for_pre_roll(
    session: &mut CaptureSession<'_>,
    writer: &mut RunWriter,
    tail_tracker: &mut SampleBoundaryTracker,
) -> Result<()> {
    if !tail_tracker.has_requirements() {
        bail!("daemon advertised no periodic non-EVENT sources for command boundary coverage");
    }
    let deadline = Instant::now()
        .checked_add(tail_tracker.timeout())
        .ok_or_else(|| anyhow!("pre-roll deadline is too large"))?;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            bail!(
                "daemon did not emit pre-roll samples for source ids {:?} before the deadline",
                tail_tracker.missing_sources()
            );
        }
        match session.read_next(remaining.min(CAPTURE_POLL)) {
            Ok(frame) => {
                if let CaptureFrame::RawSampleBatch(batch) = &frame {
                    tail_tracker.observe(batch);
                }
                if persist_frame(writer, frame)?.is_some() {
                    bail!("capture stopped before the command pre-roll completed");
                }
                if tail_tracker.pre_ready() {
                    return Ok(());
                }
            }
            Err(error) if client_timeout(&error) => continue,
            Err(error) => return Err(error.into()),
        }
    }
}

fn wait_for_event_ack(
    session: &mut CaptureSession<'_>,
    writer: &mut RunWriter,
    event_id: &str,
) -> Result<()> {
    let deadline = stop_deadline_from_now()?;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            bail!("daemon did not acknowledge event {event_id:?} before the deadline");
        }
        match session.read_next(remaining.min(CAPTURE_POLL)) {
            Ok(frame) => {
                let acknowledged = matches!(
                    &frame,
                    CaptureFrame::RecordEventAck(ack) if ack.event.event_id == event_id
                );
                if persist_frame(writer, frame)?.is_some() {
                    bail!("capture stopped before event {event_id:?} was acknowledged");
                }
                if acknowledged {
                    return Ok(());
                }
            }
            Err(error) if client_timeout(&error) => continue,
            Err(error) => return Err(error.into()),
        }
    }
}

fn stop_and_drain(
    session: &mut CaptureSession<'_>,
    writer: &mut RunWriter,
) -> Result<StopCaptureAck> {
    session.stop().context("failed to stop command capture")?;
    let deadline = Instant::now()
        .checked_add(STOP_GRACE)
        .ok_or_else(|| anyhow!("capture stop deadline is too large"))?;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            bail!("daemon did not acknowledge StopCapture before the deadline");
        }
        match session.read_next(remaining.min(CAPTURE_POLL)) {
            Ok(frame) => {
                if let Some(acknowledgement) = persist_frame(writer, frame)? {
                    return Ok(acknowledgement);
                }
            }
            Err(error) if client_timeout(&error) => continue,
            Err(error) => return Err(error.into()),
        }
    }
}

fn persist_frame(writer: &mut RunWriter, frame: CaptureFrame) -> Result<Option<StopCaptureAck>> {
    match frame {
        CaptureFrame::RawSampleBatch(batch) => {
            writer.append_batch(&batch)?;
            Ok(None)
        }
        CaptureFrame::EventBatch(batch) => {
            writer.append_event_batch(&batch)?;
            Ok(None)
        }
        CaptureFrame::RecordEventAck(_) => Ok(None),
        CaptureFrame::StopCaptureAck(acknowledgement) => Ok(Some(acknowledgement)),
    }
}

fn marker(
    run_id: &str,
    event_id: &str,
    kind: &str,
    labels: &BTreeMap<String, String>,
    extras: &[(&str, &str)],
) -> RecordEvent {
    let mut marker_labels = labels.clone();
    for (key, value) in extras {
        marker_labels.insert((*key).to_string(), (*value).to_string());
    }
    RecordEvent {
        run_id: run_id.to_string(),
        event_id: event_id.to_string(),
        kind: kind.to_string(),
        labels: marker_labels,
    }
}

fn reported_status(status: &ExitStatus) -> (i32, Option<i32>) {
    let signal = status.signal();
    let exit_code = status
        .code()
        .or_else(|| signal.and_then(|value| value.checked_add(128)))
        .unwrap_or(1);
    (exit_code, signal)
}

fn stop_deadline_from_now() -> Result<Instant> {
    Instant::now()
        .checked_add(STOP_GRACE)
        .ok_or_else(|| anyhow!("capture stop deadline is too large"))
}

#[derive(Debug)]
struct SampleBoundaryTracker {
    requirements: BTreeMap<u32, TailRequirement>,
    recent_sample_ns: BTreeMap<u32, Vec<u64>>,
    timeout: Duration,
}

impl SampleBoundaryTracker {
    fn new(sources: &[SourceDescriptor], interval_ms: u64) -> Self {
        let fallback_margin_ns = interval_ms.saturating_mul(1_000_000).max(1);
        let requirements: BTreeMap<u32, TailRequirement> = sources
            .iter()
            .filter(|source| {
                !matches!(&source.temporality, Temporality::Event)
                    && !matches!(&source.sampling_mode, SamplingMode::Event)
            })
            .filter_map(|source| {
                let requirement = match &source.temporality {
                    Temporality::IntervalAverage => TailRequirement::IntervalAverage {
                        margin_ns: source
                            .nominal_period_ns
                            .unwrap_or(fallback_margin_ns)
                            .max(1),
                    },
                    Temporality::Event => return None,
                    Temporality::Cumulative
                    | Temporality::Instant
                    | Temporality::Delta
                    | Temporality::Gauge => TailRequirement::FreshSample,
                };
                Some((source.local_source_id, requirement))
            })
            .collect();
        let max_margin_ns = requirements
            .values()
            .filter_map(|requirement| match requirement {
                TailRequirement::FreshSample => None,
                TailRequirement::IntervalAverage { margin_ns } => Some(*margin_ns),
            })
            .max()
            .unwrap_or(0);
        let timeout_ns = max_margin_ns
            .saturating_mul(3)
            .max(fallback_margin_ns.saturating_mul(3))
            .max(5_000_000_000);
        Self {
            requirements,
            recent_sample_ns: BTreeMap::new(),
            timeout: Duration::from_nanos(timeout_ns),
        }
    }

    fn observe(&mut self, batch: &RawSampleBatch) {
        for sample in &batch.samples {
            if !self.requirements.contains_key(&sample.source_id) {
                continue;
            }
            let recent = self.recent_sample_ns.entry(sample.source_id).or_default();
            recent.push(sample.monotonic_ns);
            if recent.len() > 2 {
                recent.remove(0);
            }
        }
    }

    fn ready(&self, exit_ns: u64) -> bool {
        self.requirements.iter().all(|(source_id, requirement)| {
            let qualifying: Vec<u64> = self
                .recent_sample_ns
                .get(source_id)
                .into_iter()
                .flatten()
                .copied()
                .filter(|timestamp| *timestamp >= exit_ns)
                .collect();
            match requirement {
                TailRequirement::FreshSample => !qualifying.is_empty(),
                TailRequirement::IntervalAverage { margin_ns } => {
                    qualifying.len() >= 2
                        || qualifying.last().is_some_and(|timestamp| {
                            *timestamp >= exit_ns.saturating_add(*margin_ns)
                        })
                }
            }
        })
    }

    fn has_requirements(&self) -> bool {
        !self.requirements.is_empty()
    }

    fn pre_ready(&self) -> bool {
        self.has_requirements()
            && self
                .requirements
                .keys()
                .all(|source_id| self.recent_sample_ns.contains_key(source_id))
    }

    fn missing_sources(&self) -> Vec<u32> {
        self.requirements
            .keys()
            .filter(|source_id| !self.recent_sample_ns.contains_key(source_id))
            .copied()
            .collect()
    }

    fn timeout(&self) -> Duration {
        self.timeout
    }
}

#[derive(Clone, Copy, Debug)]
enum TailRequirement {
    FreshSample,
    IntervalAverage { margin_ns: u64 },
}

fn client_timeout(error: &ClientError) -> bool {
    matches!(
        error,
        ClientError::Wire(WireError::Io(source))
            if matches!(source.kind(), io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock)
    )
}

fn terminate_process_group(child: &mut Child, process_group_id: i32) -> Result<()> {
    let process_group = Pid::from_raw(process_group_id);
    if !process_group_exists(process_group)? {
        let _ = child.try_wait();
        return Ok(());
    }

    signal_process_group(process_group, Signal::SIGTERM)?;
    if wait_for_process_group_exit(child, process_group, PROCESS_TERM_GRACE)? {
        return Ok(());
    }

    signal_process_group(process_group, Signal::SIGKILL)?;
    if wait_for_process_group_exit(child, process_group, PROCESS_KILL_GRACE)? {
        return Ok(());
    }
    bail!("process group {process_group_id} still exists after bounded SIGTERM/SIGKILL cleanup")
}

fn signal_process_group(process_group: Pid, signal: Signal) -> Result<()> {
    match killpg(process_group, signal) {
        Ok(()) | Err(Errno::ESRCH) => Ok(()),
        Err(error) => Err(anyhow!(error)).context(format!(
            "failed to send {signal:?} to process group {}",
            process_group.as_raw()
        )),
    }
}

fn process_group_exists(process_group: Pid) -> Result<bool> {
    match killpg(process_group, None) {
        Ok(()) | Err(Errno::EPERM) => Ok(true),
        Err(Errno::ESRCH) => Ok(false),
        Err(error) => Err(anyhow!(error)).context(format!(
            "failed to probe process group {}",
            process_group.as_raw()
        )),
    }
}

fn wait_for_process_group_exit(
    child: &mut Child,
    process_group: Pid,
    grace: Duration,
) -> Result<bool> {
    let deadline = Instant::now()
        .checked_add(grace)
        .ok_or_else(|| anyhow!("process cleanup deadline is too large"))?;
    loop {
        let _ = child.try_wait().context("failed to reap command process")?;
        if !process_group_exists(process_group)? {
            return Ok(true);
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Ok(false);
        }
        std::thread::sleep(remaining.min(PROCESS_POLL));
    }
}

fn finish_after_error(
    writer: RunWriter,
    lifecycle: &mut CommandLifecycleWriter,
    mut error: anyhow::Error,
) -> anyhow::Error {
    let reason = format!("{error:#}");
    match writer.finish() {
        Ok(run_dir) => {
            error = error.context(format!("run artifact retained at {}", run_dir.display()));
        }
        Err(finish_error) => {
            error = error.context(format!(
                "finishing the partial run artifact also failed: {finish_error}"
            ));
        }
    }
    if let Err(lifecycle_error) = lifecycle.incomplete(&reason) {
        error = error.context(format!(
            "recording the incomplete command lifecycle also failed: {lifecycle_error:#}"
        ));
    }
    error
}

struct CommandLifecycleWriter {
    file: File,
    path: PathBuf,
    run_id: String,
    terminal: bool,
}

impl CommandLifecycleWriter {
    fn create(run_dir: &Path, run_id: &str) -> Result<Self> {
        let path = run_dir.join(COMMAND_LIFECYCLE_FILE);
        let file = OpenOptions::new()
            .append(true)
            .create_new(true)
            .open(&path)
            .with_context(|| format!("failed to create {}", path.display()))?;
        let mut writer = Self {
            file,
            path,
            run_id: run_id.to_string(),
            terminal: false,
        };
        writer.append(&CommandLifecycleRecord::started(run_id))?;
        Ok(writer)
    }

    fn complete(
        &mut self,
        exit_code: i32,
        signal: Option<i32>,
        batches: u64,
        samples: u64,
    ) -> Result<()> {
        self.append(&CommandLifecycleRecord::complete(
            &self.run_id,
            exit_code,
            signal,
            batches,
            samples,
        ))?;
        self.terminal = true;
        Ok(())
    }

    fn incomplete(&mut self, reason: &str) -> Result<()> {
        if self.terminal {
            return Ok(());
        }
        let bounded_reason: String = reason.chars().take(4_096).collect();
        self.append(&CommandLifecycleRecord::incomplete(
            &self.run_id,
            bounded_reason,
        ))?;
        self.terminal = true;
        Ok(())
    }

    fn append(&mut self, record: &CommandLifecycleRecord) -> Result<()> {
        let mut encoded = serde_json::to_vec(record)
            .with_context(|| format!("failed to encode {}", self.path.display()))?;
        encoded.push(b'\n');
        self.file
            .write_all(&encoded)
            .with_context(|| format!("failed to append {}", self.path.display()))?;
        self.file
            .sync_all()
            .with_context(|| format!("failed to sync {}", self.path.display()))
    }
}

impl Drop for CommandLifecycleWriter {
    fn drop(&mut self) {
        if !self.terminal {
            let _ = self.incomplete("runner exited before writing a terminal lifecycle record");
        }
    }
}

#[cfg(test)]
mod boundary_tests {
    use super::SampleBoundaryTracker;
    use std::collections::BTreeMap;
    use wattch_core::{
        EvidenceClass, RawSample, RawSampleBatch, RawValue, SamplingMode, SourceDescriptor,
        SourceKind, Temporality, Unit,
    };

    fn descriptor(
        id: u32,
        temporality: Temporality,
        sampling_mode: SamplingMode,
    ) -> SourceDescriptor {
        SourceDescriptor {
            local_source_id: id,
            source_uid: format!("test://source/{id}"),
            node_id: "node".to_string(),
            backend: "test".to_string(),
            kind: SourceKind::Other,
            unit: Unit::Counter,
            temporality,
            evidence_class: EvidenceClass::RawSensorReading,
            source_path: format!("source/{id}"),
            parent_source_uid: None,
            clock_domain: "monotonic".to_string(),
            sampling_mode,
            nominal_period_ns: Some(1_000_000),
            max_value: None,
            resolution: None,
            labels: BTreeMap::new(),
        }
    }

    fn batch(samples: &[(u32, u64)]) -> RawSampleBatch {
        RawSampleBatch {
            run_id: "run".to_string(),
            node_id: "node".to_string(),
            batch_sequence_no: 0,
            samples: samples
                .iter()
                .map(|(source_id, timestamp)| RawSample {
                    source_id: *source_id,
                    sequence_no: 0,
                    monotonic_ns: *timestamp,
                    value: RawValue::I64(1),
                })
                .collect(),
        }
    }

    #[test]
    fn empty_and_sparse_batches_do_not_satisfy_per_source_pre_roll() {
        let sources = vec![
            descriptor(1, Temporality::Cumulative, SamplingMode::Periodic),
            descriptor(2, Temporality::Gauge, SamplingMode::Polled),
        ];
        let mut tracker = SampleBoundaryTracker::new(&sources, 10);

        tracker.observe(&batch(&[]));
        assert!(!tracker.pre_ready());
        assert_eq!(tracker.missing_sources(), vec![1, 2]);

        tracker.observe(&batch(&[(1, 10)]));
        assert!(!tracker.pre_ready());
        assert_eq!(tracker.missing_sources(), vec![2]);

        tracker.observe(&batch(&[(2, 20)]));
        assert!(tracker.pre_ready());
        assert!(tracker.missing_sources().is_empty());
    }

    #[test]
    fn event_sources_are_excluded_but_delta_and_gauge_sources_are_required() {
        let sources = vec![
            descriptor(1, Temporality::Event, SamplingMode::Event),
            descriptor(2, Temporality::Delta, SamplingMode::Periodic),
            descriptor(3, Temporality::Gauge, SamplingMode::Synthetic),
        ];
        let mut tracker = SampleBoundaryTracker::new(&sources, 10);
        assert_eq!(tracker.missing_sources(), vec![2, 3]);
        tracker.observe(&batch(&[(1, 10), (2, 10), (3, 10)]));
        assert!(tracker.pre_ready());
    }
}
