use crate::artifact::EventStreamValidator;
use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, Read};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::{Duration, Instant};
use thiserror::Error;
use wattch_core::{validate_source_set, RawSampleBatch, RawStreamValidator, SourceDescriptor};
use wattch_protocol::frame::WireError;
use wattch_protocol::messages::{
    read_message, validate_record_event, validate_run_id, write_message, Event, EventBatch,
    HelloRequest, HelloResponse, Message, RecordEvent, RecordEventAck, StartCapture, StopCapture,
    StopCaptureAck,
};
use wattch_protocol::PROTOCOL_VERSION;

#[derive(Debug, Error)]
pub enum ClientError {
    #[error("failed to connect to daemon at {path}: {source}")]
    Connect {
        path: String,
        source: std::io::Error,
    },

    #[error(transparent)]
    Wire(#[from] WireError),

    #[error(
        "socket read timed out after consuming {bytes_read} bytes of a frame; connection closed to prevent decoder desynchronization"
    )]
    PartialFrameTimeout { bytes_read: usize },

    #[error("socket configuration failed: {0}")]
    Io(#[from] std::io::Error),

    #[error("daemon error {code}: {message}")]
    Daemon { code: u32, message: String },

    #[error("unexpected message {got:?}, expected {expected}")]
    Unexpected { got: String, expected: &'static str },

    #[error("protocol integrity error: {0}")]
    Protocol(String),

    #[error("invalid raw evidence: {0}")]
    InvalidEvidence(#[from] wattch_core::Error),
}

const CONTROL_TIMEOUT: Duration = Duration::from_secs(5);
const FRAME_COMPLETION_TIMEOUT: Duration = Duration::from_secs(5);
const STOP_GRACE: Duration = Duration::from_secs(5);
const CAPTURE_BATCH_SLACK: u64 = 64;
const MAX_CAPTURE_BATCH_BYTES: usize = 1024 * 1024;

/// Blocking protocol client over a Unix socket.
#[derive(Debug)]
pub struct WattchClient {
    stream: UnixStream,
    hello: HelloResponse,
    next_event_batch_sequence_no: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub enum CaptureFrame {
    RawSampleBatch(RawSampleBatch),
    EventBatch(EventBatch),
    RecordEventAck(RecordEventAck),
    StopCaptureAck(StopCaptureAck),
}

/// Single-reader, single-writer view of an active daemon capture.
///
/// `record_event` and `stop` only write control frames. Call `read_next` from
/// the same owner to consume raw data, daemon-stamped event batches, event
/// acknowledgements, and the final stop acknowledgement in wire order.
pub struct CaptureSession<'a> {
    client: &'a mut WattchClient,
    run_id: String,
    validator: RawStreamValidator,
    event_validator: EventStreamValidator,
    sent_event_ids: BTreeSet<String>,
    pending_events: BTreeMap<String, RecordEvent>,
    awaiting_acks: BTreeMap<String, Event>,
    stop_sent: bool,
    complete: bool,
}

impl WattchClient {
    pub fn connect(socket_path: &Path) -> Result<Self, ClientError> {
        let mut stream =
            UnixStream::connect(socket_path).map_err(|source| ClientError::Connect {
                path: socket_path.display().to_string(),
                source,
            })?;
        stream.set_read_timeout(Some(CONTROL_TIMEOUT))?;
        stream.set_write_timeout(Some(CONTROL_TIMEOUT))?;

        write_message(
            &mut stream,
            &Message::HelloRequest(HelloRequest {
                client_name: "wattch-cli".to_string(),
                client_version: env!("CARGO_PKG_VERSION").to_string(),
                protocol_version: PROTOCOL_VERSION.to_string(),
            }),
        )?;

        let hello = match read_message_before(&mut stream, CONTROL_TIMEOUT)? {
            Message::HelloResponse(hello) => hello,
            Message::Error(err) => {
                return Err(ClientError::Daemon {
                    code: err.code,
                    message: err.message,
                })
            }
            other => {
                return Err(ClientError::Unexpected {
                    got: format!("{:?}", other.kind()),
                    expected: "HelloResponse",
                })
            }
        };
        if !compatible_version(&hello.protocol_version) {
            return Err(ClientError::Protocol(format!(
                "daemon protocol {} is not compatible with client protocol {}",
                hello.protocol_version, PROTOCOL_VERSION
            )));
        }
        validate_source_set(&hello.sources)?;
        if hello.node_id.trim().is_empty() {
            return Err(ClientError::Protocol(
                "HelloResponse node_id must not be empty".to_string(),
            ));
        }
        if hello.clock_instance_id.trim().is_empty() {
            return Err(ClientError::Protocol(
                "HelloResponse clock_instance_id must not be empty".to_string(),
            ));
        }
        if hello.clock_instance_id.len() > 256
            || hello.clock_instance_id.chars().any(char::is_control)
        {
            return Err(ClientError::Protocol(
                "HelloResponse clock_instance_id must be at most 256 bytes and contain no control characters"
                    .to_string(),
            ));
        }
        if let Some(source) = hello
            .sources
            .iter()
            .find(|source| source.node_id != hello.node_id)
        {
            return Err(ClientError::Protocol(format!(
                "source {} belongs to node {:?}, not advertised node {:?}",
                source.source_uid, source.node_id, hello.node_id
            )));
        }

        Ok(Self {
            stream,
            hello,
            next_event_batch_sequence_no: 0,
        })
    }

    pub fn hello(&self) -> &HelloResponse {
        &self.hello
    }

    pub fn sources(&self) -> &[SourceDescriptor] {
        &self.hello.sources
    }

    pub fn start_capture(
        &mut self,
        run_id: &str,
        interval_ms: u64,
    ) -> Result<CaptureSession<'_>, ClientError> {
        self.start_capture_sources(run_id, interval_ms, &[])
    }

    pub fn start_capture_sources(
        &mut self,
        run_id: &str,
        interval_ms: u64,
        source_ids: &[u32],
    ) -> Result<CaptureSession<'_>, ClientError> {
        validate_run_id(run_id).map_err(|reason| ClientError::Protocol(reason.to_string()))?;
        if interval_ms == 0 {
            return Err(ClientError::Protocol(
                "interval_ms must be non-zero".to_string(),
            ));
        }

        let advertised_ids: BTreeSet<u32> = self
            .hello
            .sources
            .iter()
            .map(|source| source.local_source_id)
            .collect();
        let requested_ids: BTreeSet<u32> = source_ids.iter().copied().collect();
        if requested_ids.len() != source_ids.len() {
            return Err(ClientError::Protocol(
                "source_ids must not contain duplicates".to_string(),
            ));
        }
        if let Some(unknown) = requested_ids
            .iter()
            .find(|source_id| !advertised_ids.contains(source_id))
        {
            return Err(ClientError::Protocol(format!(
                "source_ids contains unknown source id {unknown}"
            )));
        }
        let expected_ids = if source_ids.is_empty() {
            &advertised_ids
        } else {
            &requested_ids
        };

        write_message(
            &mut self.stream,
            &Message::StartCapture(StartCapture {
                run_id: run_id.to_string(),
                interval_ms,
                source_ids: source_ids.to_vec(),
            }),
        )?;

        match read_message_before(&mut self.stream, CONTROL_TIMEOUT)? {
            Message::StartCaptureAck(ack) => {
                if ack.run_id != run_id {
                    return Err(ClientError::Protocol(format!(
                        "StartCaptureAck run_id {:?} does not match {:?}",
                        ack.run_id, run_id
                    )));
                }
                let accepted: BTreeSet<u32> = ack.accepted_source_ids.iter().copied().collect();
                if accepted.len() != ack.accepted_source_ids.len() || &accepted != expected_ids {
                    return Err(ClientError::Protocol(
                        "StartCaptureAck accepted_source_ids do not exactly match requested sources"
                            .to_string(),
                    ));
                }
            }
            Message::Error(err) => {
                return Err(ClientError::Daemon {
                    code: err.code,
                    message: err.message,
                })
            }
            other => {
                return Err(ClientError::Unexpected {
                    got: format!("{:?}", other.kind()),
                    expected: "StartCaptureAck",
                })
            }
        }

        let validator =
            RawStreamValidator::new(run_id, self.hello.node_id.clone(), &self.hello.sources)?;
        let event_validator = EventStreamValidator::new(
            run_id.to_string(),
            self.hello.node_id.clone(),
            Some(self.next_event_batch_sequence_no),
        );
        Ok(CaptureSession {
            client: self,
            run_id: run_id.to_string(),
            validator,
            event_validator,
            sent_event_ids: BTreeSet::new(),
            pending_events: BTreeMap::new(),
            awaiting_acks: BTreeMap::new(),
            stop_sent: false,
            complete: false,
        })
    }

    /// Capture for roughly `duration`, invoking `on_batch` per raw batch.
    ///
    /// The active deadline is absolute: a silent or overproducing daemon
    /// cannot keep the client draining indefinitely. This compatibility
    /// wrapper ignores event frames; use `capture_with_events` or the
    /// low-level session API when benchmark markers are required.
    pub fn capture(
        &mut self,
        run_id: &str,
        interval_ms: u64,
        duration: Duration,
        mut on_batch: impl FnMut(RawSampleBatch),
    ) -> Result<StopCaptureAck, ClientError> {
        self.capture_with_events(run_id, interval_ms, duration, |frame| {
            if let CaptureFrame::RawSampleBatch(batch) = frame {
                on_batch(batch);
            }
        })
    }

    /// Timed capture wrapper that exposes every v0.2 data/event frame.
    pub fn capture_with_events(
        &mut self,
        run_id: &str,
        interval_ms: u64,
        duration: Duration,
        mut on_frame: impl FnMut(CaptureFrame),
    ) -> Result<StopCaptureAck, ClientError> {
        let mut session = self.start_capture(run_id, interval_ms)?;

        let deadline = Instant::now()
            .checked_add(duration)
            .ok_or_else(|| ClientError::Protocol("capture duration is too large".to_string()))?;
        let expected_batches = expected_batch_count(duration, interval_ms);
        let stop_after_batches = expected_batches.saturating_add(CAPTURE_BATCH_SLACK);
        let hard_batch_limit = stop_after_batches.saturating_add(CAPTURE_BATCH_SLACK);
        let mut stop_deadline = None;
        loop {
            if !session.stop_sent()
                && (Instant::now() >= deadline || session.batches_seen() >= stop_after_batches)
            {
                session.stop()?;
                stop_deadline = Some(Instant::now().checked_add(STOP_GRACE).ok_or_else(|| {
                    ClientError::Protocol("stop deadline is too large".to_string())
                })?);
            }

            let read_deadline = if session.stop_sent() {
                stop_deadline
                    .ok_or_else(|| ClientError::Protocol("missing stop deadline".to_string()))?
            } else {
                deadline
            };
            if Instant::now() >= read_deadline {
                if !session.stop_sent() {
                    continue;
                }
                return Err(ClientError::Protocol(
                    "daemon did not acknowledge StopCapture before the deadline".to_string(),
                ));
            }
            let frame = match session.read_next(nonzero_remaining(read_deadline)) {
                Ok(frame) => frame,
                Err(err) if client_timeout(&err) => continue,
                Err(err) => return Err(err),
            };
            match frame {
                CaptureFrame::RawSampleBatch(_)
                | CaptureFrame::EventBatch(_)
                | CaptureFrame::RecordEventAck(_) => {
                    on_frame(frame);
                    if session.batches_seen() > hard_batch_limit {
                        return Err(ClientError::Protocol(format!(
                            "daemon exceeded capture batch limit {hard_batch_limit}"
                        )));
                    }
                }
                CaptureFrame::StopCaptureAck(ack) => return Ok(ack),
            }
        }
    }
}

impl CaptureSession<'_> {
    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    pub fn batches_seen(&self) -> u64 {
        self.validator.batches_seen()
    }

    pub fn samples_seen(&self) -> u64 {
        self.validator.samples_seen()
    }

    pub fn stop_sent(&self) -> bool {
        self.stop_sent
    }

    pub fn is_complete(&self) -> bool {
        self.complete
    }

    /// Send a marker request. Its daemon-stamped `EventBatch` and
    /// `RecordEventAck` are returned by subsequent `read_next` calls.
    pub fn record_event(&mut self, event: RecordEvent) -> Result<(), ClientError> {
        if self.stop_sent || self.complete {
            return Err(ClientError::Protocol(
                "cannot record an event after stopping a capture".to_string(),
            ));
        }
        validate_record_event(&event)
            .map_err(|reason| ClientError::Protocol(reason.to_string()))?;
        if event.run_id != self.run_id {
            return Err(ClientError::Protocol(format!(
                "RecordEvent run_id {:?} does not match active run {:?}",
                event.run_id, self.run_id
            )));
        }
        if self.sent_event_ids.contains(&event.event_id) {
            return Err(ClientError::Protocol(format!(
                "event_id {:?} was already used in this capture",
                event.event_id
            )));
        }
        write_message(
            &mut self.client.stream,
            &Message::RecordEvent(event.clone()),
        )?;
        self.sent_event_ids.insert(event.event_id.clone());
        self.pending_events.insert(event.event_id.clone(), event);
        Ok(())
    }

    /// Request capture termination. Continue calling `read_next` until it
    /// returns `CaptureFrame::StopCaptureAck`.
    pub fn stop(&mut self) -> Result<(), ClientError> {
        if self.complete {
            return Err(ClientError::Protocol(
                "capture is already complete".to_string(),
            ));
        }
        if self.stop_sent {
            return Err(ClientError::Protocol(
                "StopCapture was already sent".to_string(),
            ));
        }
        write_message(
            &mut self.client.stream,
            &Message::StopCapture(StopCapture {
                run_id: self.run_id.clone(),
            }),
        )?;
        self.stop_sent = true;
        Ok(())
    }

    pub fn read_next(&mut self, timeout: Duration) -> Result<CaptureFrame, ClientError> {
        if self.complete {
            return Err(ClientError::Protocol(
                "capture is already complete".to_string(),
            ));
        }
        let message = read_message_before(&mut self.client.stream, timeout)?;
        match message {
            Message::RawSampleBatch(batch) => {
                let encoded_len = serde_json::to_vec(&batch)
                    .map_err(|err| ClientError::Protocol(err.to_string()))?
                    .len();
                if encoded_len > MAX_CAPTURE_BATCH_BYTES {
                    let _ = self.client.stream.shutdown(Shutdown::Both);
                    return Err(ClientError::Protocol(format!(
                        "raw batch is {encoded_len} bytes; limit is {MAX_CAPTURE_BATCH_BYTES}"
                    )));
                }
                self.validator.validate(&batch)?;
                Ok(CaptureFrame::RawSampleBatch(batch))
            }
            Message::EventBatch(batch) => {
                self.validate_event_batch(&batch)?;
                self.client.next_event_batch_sequence_no = self
                    .client
                    .next_event_batch_sequence_no
                    .checked_add(1)
                    .ok_or_else(|| {
                        ClientError::Protocol("event batch sequence overflow".to_string())
                    })?;
                Ok(CaptureFrame::EventBatch(batch))
            }
            Message::RecordEventAck(ack) => {
                let event_id = ack.event.event_id.clone();
                let stamped = self.awaiting_acks.remove(&event_id).ok_or_else(|| {
                    ClientError::Protocol(format!(
                        "RecordEventAck for {:?} arrived before its EventBatch",
                        event_id
                    ))
                })?;
                if ack.event != stamped {
                    return Err(ClientError::Protocol(format!(
                        "RecordEventAck for {:?} does not match its EventBatch",
                        event_id
                    )));
                }
                self.pending_events.remove(&event_id).ok_or_else(|| {
                    ClientError::Protocol(format!(
                        "RecordEventAck for unknown event {:?}",
                        event_id
                    ))
                })?;
                Ok(CaptureFrame::RecordEventAck(ack))
            }
            Message::StopCaptureAck(ack) => {
                if !self.stop_sent {
                    return Err(ClientError::Protocol(
                        "daemon sent StopCaptureAck before StopCapture".to_string(),
                    ));
                }
                if ack.run_id != self.run_id {
                    return Err(ClientError::Protocol(format!(
                        "StopCaptureAck run_id {:?} does not match {:?}",
                        ack.run_id, self.run_id
                    )));
                }
                if ack.batches_sent != self.validator.batches_seen()
                    || ack.samples_sent != self.validator.samples_seen()
                {
                    return Err(ClientError::Protocol(format!(
                        "StopCaptureAck counts {}/{} do not match observed {}/{}",
                        ack.batches_sent,
                        ack.samples_sent,
                        self.validator.batches_seen(),
                        self.validator.samples_seen()
                    )));
                }
                if !self.pending_events.is_empty() || !self.awaiting_acks.is_empty() {
                    return Err(ClientError::Protocol(
                        "StopCaptureAck arrived before all event acknowledgements".to_string(),
                    ));
                }
                self.client.stream.set_read_timeout(Some(CONTROL_TIMEOUT))?;
                self.complete = true;
                Ok(CaptureFrame::StopCaptureAck(ack))
            }
            Message::Error(err) => {
                // The daemon keeps a capture alive after rejecting a malformed
                // marker. Local validation makes this path exceptional, but
                // do not retain an unresolvable pending request.
                if self.pending_events.len() == 1 && self.awaiting_acks.is_empty() {
                    self.pending_events.clear();
                }
                Err(ClientError::Daemon {
                    code: err.code,
                    message: err.message,
                })
            }
            other => Err(ClientError::Unexpected {
                got: format!("{:?}", other.kind()),
                expected: "RawSampleBatch, EventBatch, RecordEventAck, or StopCaptureAck",
            }),
        }
    }

    fn validate_event_batch(&mut self, batch: &EventBatch) -> Result<(), ClientError> {
        self.event_validator
            .validate(batch)
            .map_err(ClientError::Protocol)?;

        // Validate the entire batch before mutating acknowledgement state.
        let mut stamped = Vec::with_capacity(batch.events.len());
        for event in &batch.events {
            let request = self.pending_events.get(&event.event_id).ok_or_else(|| {
                ClientError::Protocol(format!(
                    "daemon emitted unsolicited event {:?}",
                    event.event_id
                ))
            })?;
            if request.run_id != event.run_id
                || request.kind != event.kind
                || request.labels != event.labels
            {
                return Err(ClientError::Protocol(format!(
                    "daemon-stamped event {:?} changed client-supplied fields",
                    event.event_id
                )));
            }
            if self.awaiting_acks.contains_key(&event.event_id) {
                return Err(ClientError::Protocol(format!(
                    "duplicate EventBatch for {:?}",
                    event.event_id
                )));
            }
            stamped.push((event.event_id.clone(), event.clone()));
        }
        self.awaiting_acks.extend(stamped);
        Ok(())
    }
}

impl Drop for CaptureSession<'_> {
    fn drop(&mut self) {
        if !self.complete {
            let _ = self.client.stream.shutdown(Shutdown::Both);
        }
    }
}

fn expected_batch_count(duration: Duration, interval_ms: u64) -> u64 {
    let duration_ns = duration.as_nanos();
    let interval_ns = u128::from(interval_ms) * 1_000_000;
    let count = duration_ns.saturating_add(interval_ns - 1) / interval_ns;
    count.max(1).min(u128::from(u64::MAX)) as u64
}

fn nonzero_remaining(deadline: Instant) -> Duration {
    deadline
        .saturating_duration_since(Instant::now())
        .max(Duration::from_millis(1))
}

fn wire_timeout(error: &WireError) -> bool {
    matches!(
        error,
        WireError::Io(source)
            if matches!(source.kind(), io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock)
    )
}

fn client_timeout(error: &ClientError) -> bool {
    matches!(error, ClientError::Wire(source) if wire_timeout(source))
}

struct DeadlineReader<'a> {
    stream: &'a mut UnixStream,
    idle_deadline: Instant,
    frame_deadline: Option<Instant>,
    frame_completion_timeout: Duration,
    bytes_read: usize,
}

impl Read for DeadlineReader<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let deadline = self.frame_deadline.unwrap_or(self.idle_deadline);
        let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "read deadline exceeded",
            ));
        };
        self.stream
            .set_read_timeout(Some(remaining.max(Duration::from_millis(1))))?;
        let count = self.stream.read(buffer)?;
        if count > 0 && self.bytes_read == 0 {
            self.frame_deadline = Some(
                Instant::now()
                    .checked_add(self.frame_completion_timeout)
                    .ok_or_else(|| io::Error::other("frame completion deadline is too large"))?,
            );
        }
        self.bytes_read = self.bytes_read.saturating_add(count);
        Ok(count)
    }
}

fn read_message_before(stream: &mut UnixStream, timeout: Duration) -> Result<Message, ClientError> {
    read_message_before_with_frame_timeout(stream, timeout, FRAME_COMPLETION_TIMEOUT)
}

fn read_message_before_with_frame_timeout(
    stream: &mut UnixStream,
    idle_timeout: Duration,
    frame_completion_timeout: Duration,
) -> Result<Message, ClientError> {
    let idle_deadline = Instant::now()
        .checked_add(idle_timeout)
        .ok_or_else(|| ClientError::Protocol("read deadline is too large".to_string()))?;
    let (result, bytes_read) = {
        let mut reader = DeadlineReader {
            stream,
            idle_deadline,
            frame_deadline: None,
            frame_completion_timeout,
            bytes_read: 0,
        };
        let result = read_message(&mut reader);
        (result, reader.bytes_read)
    };

    match result {
        Err(error) if wire_timeout(&error) && bytes_read > 0 => {
            let _ = stream.shutdown(Shutdown::Both);
            Err(ClientError::PartialFrameTimeout { bytes_read })
        }
        Ok(message) => Ok(message),
        Err(error) => Err(ClientError::Wire(error)),
    }
}

fn compatible_version(daemon_version: &str) -> bool {
    daemon_version
        .split('.')
        .take(2)
        .eq(PROTOCOL_VERSION.split('.').take(2))
}

#[cfg(test)]
mod version_tests {
    use super::{
        client_timeout, compatible_version, read_message_before,
        read_message_before_with_frame_timeout, ClientError,
    };
    use std::io::Write;
    use std::os::unix::net::UnixStream;
    use std::thread;
    use std::time::Duration;
    use wattch_protocol::frame::FRAME_HEADER_LEN;
    use wattch_protocol::messages::{write_message, HelloRequest, Message};
    use wattch_protocol::PROTOCOL_VERSION;

    fn encoded_message() -> (Message, Vec<u8>) {
        let message = Message::HelloRequest(HelloRequest {
            client_name: "fragment-test".to_string(),
            client_version: "1.2.3".to_string(),
            protocol_version: PROTOCOL_VERSION.to_string(),
        });
        let mut encoded = Vec::new();
        write_message(&mut encoded, &message).unwrap();
        (message, encoded)
    }

    #[test]
    fn live_protocol_accepts_same_major_minor_patch_versions() {
        assert!(compatible_version(PROTOCOL_VERSION));
        assert!(compatible_version("0.2.99"));
    }

    #[test]
    fn live_protocol_rejects_other_or_malformed_versions() {
        assert!(!compatible_version("0.1.99"));
        assert!(!compatible_version("0.3.0"));
        assert!(!compatible_version("1.2.0"));
        assert!(!compatible_version(""));
        assert!(!compatible_version("garbage"));
    }

    #[test]
    fn deadline_reader_accepts_fragmented_header_and_payload_within_one_deadline() {
        let (expected, encoded) = encoded_message();
        let (mut reader, mut writer) = UnixStream::pair().unwrap();
        let first_payload_end = (FRAME_HEADER_LEN + 3).min(encoded.len());
        let chunks = [
            encoded[..3].to_vec(),
            encoded[3..first_payload_end].to_vec(),
            encoded[first_payload_end..].to_vec(),
        ];
        let sender = thread::spawn(move || {
            for chunk in chunks {
                writer.write_all(&chunk).unwrap();
                thread::sleep(Duration::from_millis(5));
            }
        });

        let decoded = read_message_before(&mut reader, Duration::from_millis(250)).unwrap();
        sender.join().unwrap();
        assert_eq!(decoded, expected);
    }

    #[test]
    fn timeout_after_partial_header_is_non_retryable() {
        let (_, encoded) = encoded_message();
        let (mut reader, mut writer) = UnixStream::pair().unwrap();
        let sender = thread::spawn(move || {
            writer.write_all(&encoded[..3]).unwrap();
            thread::sleep(Duration::from_millis(100));
        });

        let error = read_message_before_with_frame_timeout(
            &mut reader,
            Duration::from_millis(100),
            Duration::from_millis(20),
        )
        .unwrap_err();
        assert!(matches!(
            error,
            ClientError::PartialFrameTimeout { bytes_read: 3 }
        ));
        assert!(!client_timeout(&error));
        sender.join().unwrap();
    }

    #[test]
    fn timeout_after_partial_payload_is_non_retryable() {
        let (_, encoded) = encoded_message();
        let sent = FRAME_HEADER_LEN + 2;
        assert!(encoded.len() > sent);
        let (mut reader, mut writer) = UnixStream::pair().unwrap();
        let sender = thread::spawn(move || {
            writer.write_all(&encoded[..sent]).unwrap();
            thread::sleep(Duration::from_millis(100));
        });

        let error = read_message_before_with_frame_timeout(
            &mut reader,
            Duration::from_millis(100),
            Duration::from_millis(20),
        )
        .unwrap_err();
        assert!(matches!(
            error,
            ClientError::PartialFrameTimeout { bytes_read } if bytes_read == sent
        ));
        assert!(!client_timeout(&error));
        sender.join().unwrap();
    }
}
