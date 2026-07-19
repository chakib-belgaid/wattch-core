use crate::service::{inspect_value, ServiceError};
use crate::status::{self, CaptureStartRecord, CaptureTerminalRecord, MCP_CAPTURE_ID_LABEL};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::ErrorKind;
use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use wattch_cli::artifact::{inspect_run, replay_trace, RunWriter, CLOCK_INSTANCE_ID_LABEL};
use wattch_cli::client::{CaptureFrame, CaptureSession, ClientError, WattchClient};
use wattch_core::{RawSampleBatch, RunManifest, SourceDescriptor};
use wattch_protocol::frame::WireError;
use wattch_protocol::messages::{Event, RecordEvent, StopCaptureAck};

const READ_SLICE: Duration = Duration::from_millis(100);
const EVENT_ACK_TIMEOUT: Duration = Duration::from_secs(5);
const STOP_ACK_TIMEOUT: Duration = Duration::from_secs(5);
const FIRST_SAMPLE_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_TAIL_WAIT: Duration = Duration::from_secs(10);
const WINDOW_START_KIND: &str = "WATTCH_WINDOW_START";
const WINDOW_END_KIND: &str = "WATTCH_WINDOW_END";

pub(crate) struct CaptureSpec {
    pub capture_id: String,
    pub run_id: String,
    pub socket: PathBuf,
    pub out_root: PathBuf,
    pub interval_ms: u64,
    pub source_ids: Vec<u32>,
    pub labels: BTreeMap<String, String>,
    pub max_duration: Duration,
}

pub(crate) struct CaptureControl {
    pub run_id: String,
    pub stop: Sender<()>,
    pub join: Option<JoinHandle<Result<Value, ServiceError>>>,
}

struct CaptureStatusGuard {
    run_dir: PathBuf,
    capture_id: String,
    run_id: String,
    armed: bool,
}

impl CaptureStatusGuard {
    fn new(run_dir: PathBuf, capture_id: &str, run_id: &str) -> Self {
        Self {
            run_dir,
            capture_id: capture_id.to_string(),
            run_id: run_id.to_string(),
            armed: true,
        }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for CaptureStatusGuard {
    fn drop(&mut self) {
        if self.armed {
            let terminal = CaptureTerminalRecord::incomplete(
                &self.capture_id,
                &self.run_id,
                "CAPTURE_ABORTED",
                "capture worker exited before committing a terminal status",
                false,
            );
            let _ = status::write_terminal(&self.run_dir, &terminal);
        }
    }
}

pub(crate) fn spawn_capture(
    spec: CaptureSpec,
) -> (CaptureControl, Receiver<Result<Value, ServiceError>>) {
    let (stop_tx, stop_rx) = mpsc::channel();
    let (ready_tx, ready_rx) = mpsc::channel();
    let run_id = spec.run_id.clone();
    let join = thread::spawn(move || run_capture(spec, stop_rx, ready_tx));
    (
        CaptureControl {
            run_id,
            stop: stop_tx,
            join: Some(join),
        },
        ready_rx,
    )
}

fn run_capture(
    spec: CaptureSpec,
    stop_rx: Receiver<()>,
    ready_tx: Sender<Result<Value, ServiceError>>,
) -> Result<Value, ServiceError> {
    let mut ready_tx = Some(ready_tx);
    let result = run_capture_inner(&spec, &stop_rx, &mut ready_tx);
    if let Err(error) = &result {
        if let Some(sender) = ready_tx.take() {
            let _ = sender.send(Err(error.clone()));
        }
    }
    result
}

fn run_capture_inner(
    spec: &CaptureSpec,
    stop_rx: &Receiver<()>,
    ready_tx: &mut Option<Sender<Result<Value, ServiceError>>>,
) -> Result<Value, ServiceError> {
    let mut client = WattchClient::connect(&spec.socket).map_err(client_error)?;
    let hello = client.hello().clone();
    let accepted_ids = if spec.source_ids.is_empty() {
        hello
            .sources
            .iter()
            .map(|source| source.local_source_id)
            .collect::<Vec<_>>()
    } else {
        spec.source_ids.clone()
    };
    let selected = hello
        .sources
        .iter()
        .filter(|source| accepted_ids.contains(&source.local_source_id))
        .cloned()
        .collect::<Vec<_>>();

    let mut labels = spec.labels.clone();
    labels.insert(
        CLOCK_INSTANCE_ID_LABEL.to_string(),
        hello.clock_instance_id.clone(),
    );
    labels.insert(MCP_CAPTURE_ID_LABEL.to_string(), spec.capture_id.clone());
    let manifest = RunManifest {
        run_id: spec.run_id.clone(),
        node_id: hello.node_id.clone(),
        protocol_version: hello.protocol_version.clone(),
        clock_instance_id: hello.clock_instance_id.clone(),
        wattch_version: env!("CARGO_PKG_VERSION").to_string(),
        created_unix_ns: unix_ns(),
        sources: hello.sources.clone(),
        labels,
    };

    let mut session = client
        .start_capture_sources(&spec.run_id, spec.interval_ms, &spec.source_ids)
        .map_err(client_error)?;
    let mut writer = RunWriter::create(&spec.out_root, &manifest).map_err(artifact_error)?;
    let run_dir = writer.run_dir().to_path_buf();
    status::write_start(
        &run_dir,
        &CaptureStartRecord::new(&spec.capture_id, &spec.run_id),
    )
    .map_err(status_error)?;
    let mut status_guard = CaptureStatusGuard::new(run_dir.clone(), &spec.capture_id, &spec.run_id);

    let active_result = run_active_capture(
        spec,
        &selected,
        &accepted_ids,
        stop_rx,
        ready_tx,
        &run_dir,
        &mut session,
        &mut writer,
    );
    match active_result {
        Ok(mut completion) => {
            let run_dir = writer.finish().map_err(artifact_error)?;
            inspect_run(&run_dir).map_err(artifact_error)?;
            let mut final_tail = TailCoverage::new(
                &selected,
                spec.interval_ms,
                completion.end_event.monotonic_ns,
            );
            replay_trace(&run_dir, |batch| final_tail.observe(&batch)).map_err(artifact_error)?;
            completion.tail_complete = final_tail.complete();
            completion.tail_target_monotonic_ns = final_tail.target_ns();
            completion.tail_last_monotonic_ns = final_tail.last_ns();
            completion.tail_incomplete_source_ids = final_tail.incomplete_source_ids();
            let terminal = CaptureTerminalRecord::complete(
                &spec.capture_id,
                &spec.run_id,
                completion.status,
                spec.interval_ms,
                &accepted_ids,
                &completion.start_event.event_id,
                completion.start_event.monotonic_ns,
                &completion.end_event.event_id,
                completion.end_event.monotonic_ns,
                completion.tail_complete,
                completion.tail_target_monotonic_ns,
                completion.tail_last_monotonic_ns,
                completion.tail_incomplete_source_ids.clone(),
                completion.stop_ack.batches_sent,
                completion.stop_ack.samples_sent,
            );
            status::write_terminal(&run_dir, &terminal).map_err(status_error)?;
            status_guard.disarm();
            let inspect = inspect_value(&run_dir)?;
            Ok(json!({
                "capture_id": spec.capture_id,
                "run_id": spec.run_id,
                "run_dir": run_dir,
                "status": completion.status,
                "batches_sent": completion.stop_ack.batches_sent,
                "samples_sent": completion.stop_ack.samples_sent,
                "start_event": completion.start_event,
                "end_event": completion.end_event,
                "tail_complete": completion.tail_complete,
                "tail_target_monotonic_ns": completion.tail_target_monotonic_ns,
                "tail_last_monotonic_ns": completion.tail_last_monotonic_ns,
                "tail_incomplete_source_ids": completion.tail_incomplete_source_ids,
                "inspect": inspect,
            }))
        }
        Err(error) => {
            let partial_dir = writer.run_dir().to_path_buf();
            let trace_sync_succeeded = writer.finish().is_ok();
            let terminal = CaptureTerminalRecord::incomplete(
                &spec.capture_id,
                &spec.run_id,
                error.code,
                &error.message,
                trace_sync_succeeded,
            );
            let status_error = status::write_terminal(&partial_dir, &terminal).err();
            if status_error.is_none() {
                status_guard.disarm();
            }
            Err(error.with_data(json!({
                "capture_id": spec.capture_id,
                "run_id": spec.run_id,
                "partial_run_dir": partial_dir,
                "trace_sync_succeeded": trace_sync_succeeded,
                "status_error": status_error.map(|value| value.to_string()),
            })))
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn run_active_capture(
    spec: &CaptureSpec,
    selected: &[SourceDescriptor],
    accepted_ids: &[u32],
    stop_rx: &Receiver<()>,
    ready_tx: &mut Option<Sender<Result<Value, ServiceError>>>,
    run_dir: &PathBuf,
    session: &mut CaptureSession<'_>,
    writer: &mut RunWriter,
) -> Result<CaptureCompletion, ServiceError> {
    wait_for_first_sample(session, writer, selected)?;

    let start_request = RecordEvent {
        run_id: spec.run_id.clone(),
        event_id: format!("{}-window-start", spec.capture_id),
        kind: WINDOW_START_KIND.to_string(),
        labels: spec.labels.clone(),
    };
    let start_event = record_and_persist_event(session, writer, start_request)?;

    if let Some(sender) = ready_tx.take() {
        sender
            .send(Ok(json!({
                "run_id": spec.run_id,
                "run_dir": run_dir,
                "status": "capturing",
                "accepted_source_ids": accepted_ids,
                "start_event": start_event,
                "window_semantics": "capture-only marker; not a TARGET_* trial marker",
            })))
            .map_err(|_| {
                ServiceError::new(
                    "MCP_SESSION_CLOSED",
                    "MCP client disconnected before capture start completed",
                )
            })?;
    }

    let capture_deadline = Instant::now()
        .checked_add(spec.max_duration)
        .ok_or_else(|| ServiceError::new("INVALID_CONFIG", "capture deadline overflow"))?;
    let status = loop {
        match stop_rx.try_recv() {
            Ok(()) => break "stopped",
            Err(TryRecvError::Disconnected) => break "server_shutdown",
            Err(TryRecvError::Empty) => {}
        }
        if Instant::now() >= capture_deadline {
            break "max_duration_reached";
        }
        match session.read_next(READ_SLICE) {
            Ok(frame) => persist_data_frame(writer, frame)?,
            Err(error) if is_timeout(&error) => {}
            Err(error) => return Err(client_error(error)),
        }
    };

    let end_request = RecordEvent {
        run_id: spec.run_id.clone(),
        event_id: format!("{}-window-end", spec.capture_id),
        kind: WINDOW_END_KIND.to_string(),
        labels: spec.labels.clone(),
    };
    let end_event = record_and_persist_event(session, writer, end_request)?;
    let tail = retain_post_end_tail(
        session,
        writer,
        selected,
        spec.interval_ms,
        end_event.monotonic_ns,
    )?;

    session.stop().map_err(client_error)?;
    let stop_ack = drain_stop_ack(session, writer)?;
    Ok(CaptureCompletion {
        status,
        stop_ack,
        start_event,
        end_event,
        tail_complete: tail.complete,
        tail_target_monotonic_ns: tail.target_ns,
        tail_last_monotonic_ns: tail.last_ns,
        tail_incomplete_source_ids: tail.incomplete_source_ids,
    })
}

struct CaptureCompletion {
    status: &'static str,
    stop_ack: StopCaptureAck,
    start_event: Event,
    end_event: Event,
    tail_complete: bool,
    tail_target_monotonic_ns: u64,
    tail_last_monotonic_ns: u64,
    tail_incomplete_source_ids: Vec<u32>,
}

struct TailResult {
    complete: bool,
    target_ns: u64,
    last_ns: u64,
    incomplete_source_ids: Vec<u32>,
}

#[derive(Debug)]
struct PreStartCoverage {
    required_source_ids: BTreeSet<u32>,
    observed_source_ids: BTreeSet<u32>,
    saw_any_sample: bool,
}

impl PreStartCoverage {
    fn new(selected: &[SourceDescriptor]) -> Self {
        Self {
            required_source_ids: selected
                .iter()
                .filter(|source| is_periodic_source(source))
                .map(|source| source.local_source_id)
                .collect(),
            observed_source_ids: BTreeSet::new(),
            saw_any_sample: false,
        }
    }

    fn observe(&mut self, batch: &RawSampleBatch) {
        self.saw_any_sample |= !batch.samples.is_empty();
        self.observed_source_ids.extend(
            batch
                .samples
                .iter()
                .map(|sample| sample.source_id)
                .filter(|source_id| self.required_source_ids.contains(source_id)),
        );
    }

    fn ready(&self) -> bool {
        self.saw_any_sample
            && self
                .required_source_ids
                .is_subset(&self.observed_source_ids)
    }

    fn missing_source_ids(&self) -> Vec<u32> {
        self.required_source_ids
            .difference(&self.observed_source_ids)
            .copied()
            .collect()
    }
}

#[derive(Debug)]
struct SourceTailCoverage {
    requirement: TailRequirement,
    last_ns: u64,
    last_fresh_ns: Option<u64>,
    fresh_generations: u8,
}

#[derive(Debug)]
enum TailRequirement {
    FreshSample,
    IntervalAverage { target_ns: u64 },
}

#[derive(Debug)]
pub(crate) struct TailCoverage {
    end_ns: u64,
    target_ns: u64,
    last_ns: u64,
    sources: BTreeMap<u32, SourceTailCoverage>,
}

impl TailCoverage {
    pub(crate) fn new(selected: &[SourceDescriptor], interval_ms: u64, end_ns: u64) -> Self {
        let requested_ns = interval_ms.saturating_mul(1_000_000);
        let sources = selected
            .iter()
            .filter(|source| is_periodic_source(source))
            .map(|source| {
                let requirement = if matches!(
                    source.temporality,
                    wattch_core::Temporality::IntervalAverage
                ) {
                    let margin_ns = source.nominal_period_ns.unwrap_or(requested_ns).max(1);
                    TailRequirement::IntervalAverage {
                        target_ns: end_ns.saturating_add(margin_ns),
                    }
                } else {
                    TailRequirement::FreshSample
                };
                (
                    source.local_source_id,
                    SourceTailCoverage {
                        requirement,
                        last_ns: end_ns,
                        last_fresh_ns: None,
                        fresh_generations: 0,
                    },
                )
            })
            .collect::<BTreeMap<_, _>>();
        let target_ns = sources
            .values()
            .filter_map(|coverage| match coverage.requirement {
                TailRequirement::FreshSample => None,
                TailRequirement::IntervalAverage { target_ns } => Some(target_ns),
            })
            .max()
            .unwrap_or(end_ns);
        Self {
            end_ns,
            target_ns,
            last_ns: end_ns,
            sources,
        }
    }

    pub(crate) fn observe(&mut self, batch: &RawSampleBatch) {
        for sample in &batch.samples {
            self.last_ns = self.last_ns.max(sample.monotonic_ns);
            let Some(source) = self.sources.get_mut(&sample.source_id) else {
                continue;
            };
            source.last_ns = source.last_ns.max(sample.monotonic_ns);
            if sample.monotonic_ns >= self.end_ns
                && source.last_fresh_ns != Some(sample.monotonic_ns)
            {
                source.last_fresh_ns = Some(sample.monotonic_ns);
                source.fresh_generations = source.fresh_generations.saturating_add(1);
            }
        }
    }

    fn source_complete(source: &SourceTailCoverage) -> bool {
        match source.requirement {
            TailRequirement::FreshSample => source.last_fresh_ns.is_some(),
            TailRequirement::IntervalAverage { target_ns } => {
                source.last_ns >= target_ns || source.fresh_generations >= 2
            }
        }
    }

    pub(crate) fn complete(&self) -> bool {
        self.sources.values().all(Self::source_complete)
    }

    pub(crate) fn incomplete_source_ids(&self) -> Vec<u32> {
        self.sources
            .iter()
            .filter_map(|(source_id, coverage)| {
                (!Self::source_complete(coverage)).then_some(*source_id)
            })
            .collect()
    }

    pub(crate) fn target_ns(&self) -> u64 {
        self.target_ns
    }

    pub(crate) fn last_ns(&self) -> u64 {
        self.last_ns
    }
}

fn is_periodic_source(source: &SourceDescriptor) -> bool {
    !matches!(source.temporality, wattch_core::Temporality::Event)
        && !matches!(source.sampling_mode, wattch_core::SamplingMode::Event)
}

fn wait_for_first_sample(
    session: &mut CaptureSession<'_>,
    writer: &mut RunWriter,
    selected: &[SourceDescriptor],
) -> Result<(), ServiceError> {
    let deadline = Instant::now() + FIRST_SAMPLE_TIMEOUT;
    let mut coverage = PreStartCoverage::new(selected);
    loop {
        if Instant::now() >= deadline {
            return Err(ServiceError::new(
                "FIRST_SAMPLE_TIMEOUT",
                "daemon did not provide pre-START evidence for every selected periodic source",
            )
            .with_data(json!({
                "missing_source_ids": coverage.missing_source_ids(),
            })));
        }
        match session.read_next(remaining_slice(deadline)) {
            Ok(CaptureFrame::RawSampleBatch(batch)) => {
                coverage.observe(&batch);
                writer.append_batch(&batch).map_err(artifact_error)?;
                if coverage.ready() {
                    return Ok(());
                }
            }
            Ok(CaptureFrame::EventBatch(batch)) => {
                writer.append_event_batch(&batch).map_err(artifact_error)?;
            }
            Ok(CaptureFrame::RecordEventAck(_)) => {
                return Err(ServiceError::new(
                    "PROTOCOL_INTEGRITY",
                    "received an event acknowledgement before requesting an event",
                ));
            }
            Ok(CaptureFrame::StopCaptureAck(_)) => {
                return Err(ServiceError::new(
                    "PROTOCOL_INTEGRITY",
                    "capture stopped before its first sample",
                ));
            }
            Err(error) if is_timeout(&error) => {}
            Err(error) => return Err(client_error(error)),
        }
    }
}

fn record_and_persist_event(
    session: &mut CaptureSession<'_>,
    writer: &mut RunWriter,
    request: RecordEvent,
) -> Result<Event, ServiceError> {
    let event_id = request.event_id.clone();
    session.record_event(request).map_err(client_error)?;
    let deadline = Instant::now() + EVENT_ACK_TIMEOUT;
    loop {
        if Instant::now() >= deadline {
            return Err(ServiceError::new(
                "EVENT_ACK_TIMEOUT",
                format!("daemon did not acknowledge event {event_id:?}"),
            ));
        }
        match session.read_next(remaining_slice(deadline)) {
            Ok(CaptureFrame::RawSampleBatch(batch)) => {
                writer.append_batch(&batch).map_err(artifact_error)?;
            }
            Ok(CaptureFrame::EventBatch(batch)) => {
                writer.append_event_batch(&batch).map_err(artifact_error)?;
            }
            Ok(CaptureFrame::RecordEventAck(ack)) if ack.event.event_id == event_id => {
                return Ok(ack.event);
            }
            Ok(CaptureFrame::RecordEventAck(ack)) => {
                return Err(ServiceError::new(
                    "PROTOCOL_INTEGRITY",
                    format!(
                        "received acknowledgement for event {:?} while waiting for {event_id:?}",
                        ack.event.event_id
                    ),
                ));
            }
            Ok(CaptureFrame::StopCaptureAck(_)) => {
                return Err(ServiceError::new(
                    "PROTOCOL_INTEGRITY",
                    "capture stopped while waiting for an event acknowledgement",
                ));
            }
            Err(error) if is_timeout(&error) => {}
            Err(error) => return Err(client_error(error)),
        }
    }
}

fn retain_post_end_tail(
    session: &mut CaptureSession<'_>,
    writer: &mut RunWriter,
    selected: &[SourceDescriptor],
    interval_ms: u64,
    end_ns: u64,
) -> Result<TailResult, ServiceError> {
    let requested_ns = interval_ms.saturating_mul(1_000_000);
    let native_period_ns = selected
        .iter()
        .filter_map(|source| source.nominal_period_ns)
        .max()
        .unwrap_or(requested_ns)
        .max(requested_ns);
    let wait_ns = native_period_ns
        .saturating_mul(3)
        .max(500_000_000)
        .min(MAX_TAIL_WAIT.as_nanos() as u64);
    let deadline = Instant::now() + Duration::from_nanos(wait_ns);
    let mut coverage = TailCoverage::new(selected, interval_ms, end_ns);
    while Instant::now() < deadline {
        match session.read_next(remaining_slice(deadline)) {
            Ok(CaptureFrame::RawSampleBatch(batch)) => {
                coverage.observe(&batch);
                writer.append_batch(&batch).map_err(artifact_error)?;
                if coverage.complete() {
                    return Ok(TailResult {
                        complete: true,
                        target_ns: coverage.target_ns,
                        last_ns: coverage.last_ns,
                        incomplete_source_ids: Vec::new(),
                    });
                }
            }
            Ok(CaptureFrame::EventBatch(batch)) => {
                writer.append_event_batch(&batch).map_err(artifact_error)?;
            }
            Ok(CaptureFrame::RecordEventAck(ack)) => {
                return Err(ServiceError::new(
                    "PROTOCOL_INTEGRITY",
                    format!(
                        "unexpected event acknowledgement {:?} during post-window tail",
                        ack.event.event_id
                    ),
                ));
            }
            Ok(CaptureFrame::StopCaptureAck(_)) => {
                return Err(ServiceError::new(
                    "PROTOCOL_INTEGRITY",
                    "capture stopped during post-window tail",
                ));
            }
            Err(error) if is_timeout(&error) => {}
            Err(error) => return Err(client_error(error)),
        }
    }
    Ok(TailResult {
        complete: false,
        target_ns: coverage.target_ns,
        last_ns: coverage.last_ns,
        incomplete_source_ids: coverage.incomplete_source_ids(),
    })
}

fn drain_stop_ack(
    session: &mut CaptureSession<'_>,
    writer: &mut RunWriter,
) -> Result<StopCaptureAck, ServiceError> {
    let deadline = Instant::now() + STOP_ACK_TIMEOUT;
    loop {
        if Instant::now() >= deadline {
            return Err(ServiceError::new(
                "STOP_ACK_TIMEOUT",
                "daemon did not acknowledge capture stop before the deadline",
            ));
        }
        match session.read_next(remaining_slice(deadline)) {
            Ok(CaptureFrame::RawSampleBatch(batch)) => {
                writer.append_batch(&batch).map_err(artifact_error)?;
            }
            Ok(CaptureFrame::EventBatch(batch)) => {
                writer.append_event_batch(&batch).map_err(artifact_error)?;
            }
            Ok(CaptureFrame::RecordEventAck(ack)) => {
                return Err(ServiceError::new(
                    "PROTOCOL_INTEGRITY",
                    format!(
                        "unexpected event acknowledgement {:?} while stopping",
                        ack.event.event_id
                    ),
                ));
            }
            Ok(CaptureFrame::StopCaptureAck(ack)) => return Ok(ack),
            Err(error) if is_timeout(&error) => {}
            Err(error) => return Err(client_error(error)),
        }
    }
}

fn persist_data_frame(writer: &mut RunWriter, frame: CaptureFrame) -> Result<(), ServiceError> {
    match frame {
        CaptureFrame::RawSampleBatch(batch) => writer.append_batch(&batch).map_err(artifact_error),
        CaptureFrame::EventBatch(batch) => {
            writer.append_event_batch(&batch).map_err(artifact_error)
        }
        CaptureFrame::RecordEventAck(ack) => Err(ServiceError::new(
            "PROTOCOL_INTEGRITY",
            format!("unexpected event acknowledgement {:?}", ack.event.event_id),
        )),
        CaptureFrame::StopCaptureAck(_) => Err(ServiceError::new(
            "PROTOCOL_INTEGRITY",
            "unexpected stop acknowledgement before requesting stop",
        )),
    }
}

fn remaining_slice(deadline: Instant) -> Duration {
    deadline
        .saturating_duration_since(Instant::now())
        .min(READ_SLICE)
        .max(Duration::from_millis(1))
}

fn is_timeout(error: &ClientError) -> bool {
    matches!(
        error,
        ClientError::Wire(WireError::Io(io_error))
            if matches!(io_error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut)
    )
}

fn client_error(error: ClientError) -> ServiceError {
    ServiceError::new("DAEMON_CLIENT", error.to_string())
}

fn artifact_error(error: wattch_cli::artifact::ArtifactError) -> ServiceError {
    ServiceError::new("ARTIFACT", error.to_string())
}

fn status_error(error: status::CaptureStatusError) -> ServiceError {
    ServiceError::new("ARTIFACT_STATUS", error.to_string())
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
    use wattch_core::{
        EvidenceClass, RawSample, RawValue, SamplingMode, SourceKind, Temporality, Unit,
    };

    fn source(id: u32, period_ns: u64, temporality: Temporality) -> SourceDescriptor {
        SourceDescriptor {
            local_source_id: id,
            source_uid: format!("test://tail/{id}"),
            node_id: "tail-node".to_string(),
            backend: "tail-test".to_string(),
            kind: SourceKind::CpuPower,
            unit: Unit::Milliwatt,
            temporality,
            evidence_class: EvidenceClass::ToolDerivedEstimate,
            source_path: format!("source/{id}"),
            parent_source_uid: None,
            clock_domain: "monotonic".to_string(),
            sampling_mode: SamplingMode::Synthetic,
            nominal_period_ns: Some(period_ns),
            max_value: None,
            resolution: None,
            labels: BTreeMap::new(),
        }
    }

    fn batch(samples: &[(u32, u64)]) -> RawSampleBatch {
        RawSampleBatch {
            run_id: "tail-run".to_string(),
            node_id: "tail-node".to_string(),
            batch_sequence_no: 0,
            samples: samples
                .iter()
                .enumerate()
                .map(|(sequence, (source_id, timestamp))| RawSample {
                    source_id: *source_id,
                    sequence_no: sequence as u64,
                    monotonic_ns: *timestamp,
                    value: RawValue::F64(1.0),
                })
                .collect(),
        }
    }

    #[test]
    fn fast_source_cannot_complete_tail_for_slow_interval_source() {
        let end_ns = 1_000_000_000;
        let selected = vec![
            source(1, 10_000_000, Temporality::Gauge),
            source(2, 100_000_000, Temporality::IntervalAverage),
        ];
        let mut coverage = TailCoverage::new(&selected, 10, end_ns);

        coverage.observe(&batch(&[(1, end_ns + 10_000_000)]));
        assert!(!coverage.complete());
        assert_eq!(coverage.incomplete_source_ids(), vec![2]);

        coverage.observe(&batch(&[(2, end_ns + 50_000_000)]));
        assert!(!coverage.complete());

        coverage.observe(&batch(&[(2, end_ns + 100_000_000)]));
        assert!(coverage.complete());
        assert!(coverage.incomplete_source_ids().is_empty());
    }

    #[test]
    fn fast_source_cannot_satisfy_slow_source_pre_start_coverage() {
        let selected = vec![
            source(1, 10_000_000, Temporality::Cumulative),
            source(2, 100_000_000, Temporality::IntervalAverage),
        ];
        let mut coverage = PreStartCoverage::new(&selected);

        coverage.observe(&batch(&[(1, 10_000_000)]));
        coverage.observe(&batch(&[(1, 20_000_000)]));
        assert!(!coverage.ready());
        assert_eq!(coverage.missing_source_ids(), vec![2]);

        coverage.observe(&batch(&[(2, 100_000_000)]));
        assert!(coverage.ready());
        assert!(coverage.missing_source_ids().is_empty());
    }
}
