//! Typed protocol v0 messages.
//!
//! Payloads are JSON in v0.1 and v0.2. The frame envelope reserves
//! [`crate::frame::FLAG_PAYLOAD_PROTOBUF`] for a later protobuf migration.

use crate::frame::{read_frame, read_frame_with_payload_limit, write_frame, FrameKind, WireError};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::io::{Read, Write};
use wattch_core::{RawSampleBatch, SourceDescriptor};

/// Run ids are repeated in every data-plane batch, so keep the field small
/// enough that a control-plane request cannot amplify capture memory/traffic.
pub const MAX_RUN_ID_BYTES: usize = 256;
pub const MAX_EVENT_ID_BYTES: usize = 256;
pub const MAX_EVENT_KIND_BYTES: usize = 256;
pub const MAX_EVENT_LABELS: usize = 64;
pub const MAX_EVENT_LABEL_KEY_BYTES: usize = 128;
pub const MAX_EVENT_LABEL_VALUE_BYTES: usize = 1024;

pub fn validate_run_id(run_id: &str) -> Result<(), &'static str> {
    if run_id.trim().is_empty() {
        return Err("run_id must not be empty");
    }
    if run_id.len() > MAX_RUN_ID_BYTES {
        return Err("run_id exceeds 256 UTF-8 bytes");
    }
    if run_id.chars().any(char::is_control) {
        return Err("run_id must not contain control characters");
    }
    Ok(())
}

pub fn validate_record_event(event: &RecordEvent) -> Result<(), &'static str> {
    validate_run_id(&event.run_id)?;
    validate_event_field(
        &event.event_id,
        "event_id must not be empty",
        "event_id exceeds 256 UTF-8 bytes",
        "event_id must not contain control characters",
        MAX_EVENT_ID_BYTES,
    )?;
    validate_event_field(
        &event.kind,
        "event kind must not be empty",
        "event kind exceeds 256 UTF-8 bytes",
        "event kind must not contain control characters",
        MAX_EVENT_KIND_BYTES,
    )?;
    if event.labels.len() > MAX_EVENT_LABELS {
        return Err("event labels exceed 64 entries");
    }
    for (key, value) in &event.labels {
        validate_event_field(
            key,
            "event label key must not be empty",
            "event label key exceeds 128 UTF-8 bytes",
            "event label key must not contain control characters",
            MAX_EVENT_LABEL_KEY_BYTES,
        )?;
        if value.len() > MAX_EVENT_LABEL_VALUE_BYTES {
            return Err("event label value exceeds 1024 UTF-8 bytes");
        }
        if value.chars().any(char::is_control) {
            return Err("event label value must not contain control characters");
        }
    }
    Ok(())
}

fn validate_event_field(
    value: &str,
    empty_reason: &'static str,
    length_reason: &'static str,
    control_reason: &'static str,
    max_bytes: usize,
) -> Result<(), &'static str> {
    if value.trim().is_empty() {
        return Err(empty_reason);
    }
    if value.len() > max_bytes {
        return Err(length_reason);
    }
    if value.chars().any(char::is_control) {
        return Err(control_reason);
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct HelloRequest {
    pub client_name: String,
    pub client_version: String,
    pub protocol_version: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct HelloResponse {
    pub daemon_name: String,
    pub daemon_version: String,
    pub protocol_version: String,
    pub node_id: String,
    /// Opaque identity for the daemon-wide monotonic clock epoch. Empty only
    /// when decoding a legacy v0.1 response.
    #[serde(default)]
    pub clock_instance_id: String,
    pub sources: Vec<SourceDescriptor>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StartCapture {
    pub run_id: String,
    pub interval_ms: u64,
    /// Empty means capture all sources.
    #[serde(default)]
    pub source_ids: Vec<u32>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StartCaptureAck {
    pub run_id: String,
    pub accepted_source_ids: Vec<u32>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StopCapture {
    pub run_id: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StopCaptureAck {
    pub run_id: String,
    pub batches_sent: u64,
    pub samples_sent: u64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RecordEvent {
    pub run_id: String,
    pub event_id: String,
    pub kind: String,
    #[serde(default)]
    pub labels: BTreeMap<String, String>,
}

/// A daemon-stamped benchmark marker. The daemon copies the descriptive
/// fields from [`RecordEvent`] and owns node identity and timestamping.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Event {
    pub event_id: String,
    pub run_id: String,
    pub node_id: String,
    pub monotonic_ns: u64,
    pub kind: String,
    #[serde(default)]
    pub labels: BTreeMap<String, String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RecordEventAck {
    pub event: Event,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct EventBatch {
    pub run_id: String,
    pub node_id: String,
    pub batch_sequence_no: u64,
    pub events: Vec<Event>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ErrorMessage {
    pub code: u32,
    pub message: String,
}

pub mod error_codes {
    pub const PROTOCOL_VIOLATION: u32 = 1;
    pub const UNSUPPORTED_VERSION: u32 = 2;
    pub const UNKNOWN_SOURCE_ID: u32 = 3;
    pub const CAPTURE_ALREADY_ACTIVE: u32 = 4;
    pub const NO_ACTIVE_CAPTURE: u32 = 5;
    pub const ACCESS_DENIED: u32 = 6;
    pub const INVALID_REQUEST: u32 = 7;
    pub const RESOURCE_EXHAUSTED: u32 = 8;
    pub const TIMEOUT: u32 = 9;
    pub const INTERNAL: u32 = 100;
}

/// All implemented protocol messages, paired with their frame kind.
#[derive(Clone, Debug, PartialEq)]
pub enum Message {
    HelloRequest(HelloRequest),
    HelloResponse(HelloResponse),
    StartCapture(StartCapture),
    StartCaptureAck(StartCaptureAck),
    StopCapture(StopCapture),
    StopCaptureAck(StopCaptureAck),
    RecordEvent(RecordEvent),
    RecordEventAck(RecordEventAck),
    RawSampleBatch(RawSampleBatch),
    EventBatch(EventBatch),
    Error(ErrorMessage),
}

impl Message {
    pub fn kind(&self) -> FrameKind {
        match self {
            Message::HelloRequest(_) => FrameKind::HelloRequest,
            Message::HelloResponse(_) => FrameKind::HelloResponse,
            Message::StartCapture(_) => FrameKind::StartCapture,
            Message::StartCaptureAck(_) => FrameKind::StartCaptureAck,
            Message::StopCapture(_) => FrameKind::StopCapture,
            Message::StopCaptureAck(_) => FrameKind::StopCaptureAck,
            Message::RecordEvent(_) => FrameKind::RecordEvent,
            Message::RecordEventAck(_) => FrameKind::RecordEventAck,
            Message::RawSampleBatch(_) => FrameKind::RawSampleBatch,
            Message::EventBatch(_) => FrameKind::EventBatch,
            Message::Error(_) => FrameKind::Error,
        }
    }

    pub fn payload_json(&self) -> Result<Vec<u8>, WireError> {
        let kind = self.kind();
        let result = match self {
            Message::HelloRequest(m) => serde_json::to_vec(m),
            Message::HelloResponse(m) => serde_json::to_vec(m),
            Message::StartCapture(m) => serde_json::to_vec(m),
            Message::StartCaptureAck(m) => serde_json::to_vec(m),
            Message::StopCapture(m) => serde_json::to_vec(m),
            Message::StopCaptureAck(m) => serde_json::to_vec(m),
            Message::RecordEvent(m) => serde_json::to_vec(m),
            Message::RecordEventAck(m) => serde_json::to_vec(m),
            Message::RawSampleBatch(m) => serde_json::to_vec(m),
            Message::EventBatch(m) => serde_json::to_vec(m),
            Message::Error(m) => serde_json::to_vec(m),
        };
        result.map_err(|e| WireError::InvalidPayload {
            kind,
            reason: e.to_string(),
        })
    }
}

pub fn decode_message(kind: FrameKind, payload: &[u8]) -> Result<Message, WireError> {
    fn parse<'a, T: Deserialize<'a>>(kind: FrameKind, payload: &'a [u8]) -> Result<T, WireError> {
        serde_json::from_slice(payload).map_err(|e| WireError::InvalidPayload {
            kind,
            reason: e.to_string(),
        })
    }

    match kind {
        FrameKind::HelloRequest => Ok(Message::HelloRequest(parse(kind, payload)?)),
        FrameKind::HelloResponse => Ok(Message::HelloResponse(parse(kind, payload)?)),
        FrameKind::StartCapture => Ok(Message::StartCapture(parse(kind, payload)?)),
        FrameKind::StartCaptureAck => Ok(Message::StartCaptureAck(parse(kind, payload)?)),
        FrameKind::StopCapture => Ok(Message::StopCapture(parse(kind, payload)?)),
        FrameKind::StopCaptureAck => Ok(Message::StopCaptureAck(parse(kind, payload)?)),
        FrameKind::RecordEvent => Ok(Message::RecordEvent(parse(kind, payload)?)),
        FrameKind::RecordEventAck => Ok(Message::RecordEventAck(parse(kind, payload)?)),
        FrameKind::RawSampleBatch => Ok(Message::RawSampleBatch(parse(kind, payload)?)),
        FrameKind::EventBatch => Ok(Message::EventBatch(parse(kind, payload)?)),
        FrameKind::Error => Ok(Message::Error(parse(kind, payload)?)),
    }
}

pub fn write_message(writer: &mut impl Write, message: &Message) -> Result<(), WireError> {
    let payload = message.payload_json()?;
    write_frame(writer, message.kind(), 0, &payload)
}

pub fn read_message(reader: &mut impl Read) -> Result<Message, WireError> {
    let (header, payload) = read_frame(reader)?;
    decode_framed_message(header, &payload)
}

/// Read a message with an explicit frame bound for offline artifact replay.
///
/// Live transports must continue to use [`read_message`].
pub fn read_message_with_payload_limit(
    reader: &mut impl Read,
    max_payload_len: u32,
) -> Result<Message, WireError> {
    let (header, payload) = read_frame_with_payload_limit(reader, max_payload_len)?;
    decode_framed_message(header, &payload)
}

fn decode_framed_message(
    header: crate::frame::FrameHeader,
    payload: &[u8],
) -> Result<Message, WireError> {
    if header.wire_version_minor < 2
        && matches!(
            header.kind,
            FrameKind::RecordEvent | FrameKind::RecordEventAck | FrameKind::EventBatch
        )
    {
        return Err(WireError::InvalidPayload {
            kind: header.kind,
            reason: "message kind requires wire protocol v0.2".to_string(),
        });
    }
    decode_message(header.kind, payload)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use wattch_core::{RawSample, RawValue};

    fn all_messages() -> Vec<Message> {
        vec![
            Message::HelloRequest(HelloRequest {
                client_name: "wattch-cli".into(),
                client_version: "0.1.0".into(),
                protocol_version: "0.1.0".into(),
            }),
            Message::HelloResponse(HelloResponse {
                daemon_name: "wattch-testd".into(),
                daemon_version: "0.1.0".into(),
                protocol_version: "0.2.0".into(),
                node_id: "node-0".into(),
                clock_instance_id: "clock-1".into(),
                sources: vec![],
            }),
            Message::StartCapture(StartCapture {
                run_id: "run-1".into(),
                interval_ms: 100,
                source_ids: vec![1, 2],
            }),
            Message::StartCaptureAck(StartCaptureAck {
                run_id: "run-1".into(),
                accepted_source_ids: vec![1, 2],
            }),
            Message::StopCapture(StopCapture {
                run_id: "run-1".into(),
            }),
            Message::StopCaptureAck(StopCaptureAck {
                run_id: "run-1".into(),
                batches_sent: 7,
                samples_sent: 14,
            }),
            Message::RecordEvent(RecordEvent {
                run_id: "run-1".into(),
                event_id: "target-spawn".into(),
                kind: "TARGET_SPAWN".into(),
                labels: BTreeMap::from([("program".into(), "p1".into())]),
            }),
            Message::EventBatch(EventBatch {
                run_id: "run-1".into(),
                node_id: "node-0".into(),
                batch_sequence_no: 2,
                events: vec![Event {
                    event_id: "target-spawn".into(),
                    run_id: "run-1".into(),
                    node_id: "node-0".into(),
                    monotonic_ns: 42,
                    kind: "TARGET_SPAWN".into(),
                    labels: BTreeMap::from([("program".into(), "p1".into())]),
                }],
            }),
            Message::RecordEventAck(RecordEventAck {
                event: Event {
                    event_id: "target-spawn".into(),
                    run_id: "run-1".into(),
                    node_id: "node-0".into(),
                    monotonic_ns: 42,
                    kind: "TARGET_SPAWN".into(),
                    labels: BTreeMap::from([("program".into(), "p1".into())]),
                },
            }),
            Message::RawSampleBatch(RawSampleBatch {
                run_id: "run-1".into(),
                node_id: "node-0".into(),
                batch_sequence_no: 3,
                samples: vec![RawSample {
                    source_id: 1,
                    sequence_no: 9,
                    monotonic_ns: 90_000_000,
                    value: RawValue::F64(1000.42),
                }],
            }),
            Message::Error(ErrorMessage {
                code: error_codes::PROTOCOL_VIOLATION,
                message: "bad frame".into(),
            }),
        ]
    }

    #[test]
    fn round_trips_every_message_kind() {
        for message in all_messages() {
            let mut buf = Vec::new();
            write_message(&mut buf, &message).unwrap();
            let decoded = read_message(&mut Cursor::new(buf)).unwrap();
            assert_eq!(decoded, message);
        }
    }

    #[test]
    fn start_capture_source_ids_default_to_empty() {
        let decoded = decode_message(
            FrameKind::StartCapture,
            br#"{"run_id":"r","interval_ms":50}"#,
        )
        .unwrap();
        assert_eq!(
            decoded,
            Message::StartCapture(StartCapture {
                run_id: "r".into(),
                interval_ms: 50,
                source_ids: vec![],
            })
        );
    }

    #[test]
    fn record_event_labels_default_to_empty() {
        let decoded = decode_message(
            FrameKind::RecordEvent,
            br#"{"run_id":"r","event_id":"e","kind":"TARGET_EXEC"}"#,
        )
        .unwrap();
        assert_eq!(
            decoded,
            Message::RecordEvent(RecordEvent {
                run_id: "r".into(),
                event_id: "e".into(),
                kind: "TARGET_EXEC".into(),
                labels: BTreeMap::new(),
            })
        );
    }

    #[test]
    fn decodes_legacy_v0_1_raw_sample_batch_frame() {
        let message = Message::RawSampleBatch(RawSampleBatch {
            run_id: "legacy".into(),
            node_id: "node-0".into(),
            batch_sequence_no: 0,
            samples: vec![RawSample {
                source_id: 7,
                sequence_no: 0,
                monotonic_ns: 99,
                value: RawValue::F64(12.5),
            }],
        });
        let payload = message.payload_json().unwrap();
        let mut frame = crate::frame::encode_frame(FrameKind::RawSampleBatch, 0, &payload).unwrap();
        frame[6..8].copy_from_slice(&1u16.to_le_bytes());

        assert_eq!(read_message(&mut Cursor::new(frame)).unwrap(), message);
    }

    #[test]
    fn raw_sample_json_shape_remains_unchanged() {
        let sample = RawSample {
            source_id: 1,
            sequence_no: 9,
            monotonic_ns: 90_000_000,
            value: RawValue::F64(1000.42),
        };
        assert_eq!(
            serde_json::to_string(&sample).unwrap(),
            r#"{"source_id":1,"sequence_no":9,"monotonic_ns":90000000,"value":{"type":"f64","value":1000.42}}"#
        );
    }

    #[test]
    fn legacy_hello_response_defaults_clock_instance_id() {
        let decoded = decode_message(
            FrameKind::HelloResponse,
            br#"{"daemon_name":"d","daemon_version":"0.1.0","protocol_version":"0.1.0","node_id":"n","sources":[]}"#,
        )
        .unwrap();
        let Message::HelloResponse(response) = decoded else {
            panic!("expected HelloResponse");
        };
        assert!(response.clock_instance_id.is_empty());
    }

    #[test]
    fn v0_1_header_rejects_v0_2_message_kinds() {
        let message = Message::RecordEvent(RecordEvent {
            run_id: "r".into(),
            event_id: "e".into(),
            kind: "TARGET_EXEC".into(),
            labels: BTreeMap::new(),
        });
        let payload = message.payload_json().unwrap();
        let mut frame = crate::frame::encode_frame(FrameKind::RecordEvent, 0, &payload).unwrap();
        frame[6..8].copy_from_slice(&1u16.to_le_bytes());

        assert!(matches!(
            read_message(&mut Cursor::new(frame)),
            Err(WireError::InvalidPayload {
                kind: FrameKind::RecordEvent,
                ..
            })
        ));
    }

    #[test]
    fn rejects_malformed_payload() {
        let err = decode_message(FrameKind::HelloRequest, b"not-json").unwrap_err();
        assert!(matches!(err, WireError::InvalidPayload { .. }));
    }

    #[test]
    fn run_id_validation_is_bounded() {
        assert!(validate_run_id("run-1").is_ok());
        assert!(validate_run_id("").is_err());
        assert!(validate_run_id(&"x".repeat(MAX_RUN_ID_BYTES + 1)).is_err());
        assert!(validate_run_id("run\n1").is_err());
    }

    #[test]
    fn record_event_validation_is_bounded() {
        let valid = RecordEvent {
            run_id: "run-1".into(),
            event_id: "target-exec".into(),
            kind: "TARGET_EXEC".into(),
            labels: BTreeMap::from([("program".into(), "p1".into())]),
        };
        assert!(validate_record_event(&valid).is_ok());

        let mut invalid = valid.clone();
        invalid.event_id.clear();
        assert_eq!(
            validate_record_event(&invalid),
            Err("event_id must not be empty")
        );

        let mut invalid = valid.clone();
        invalid.kind = "x".repeat(MAX_EVENT_KIND_BYTES + 1);
        assert_eq!(
            validate_record_event(&invalid),
            Err("event kind exceeds 256 UTF-8 bytes")
        );

        let mut invalid = valid;
        invalid.labels = (0..=MAX_EVENT_LABELS)
            .map(|i| (format!("key-{i}"), "value".into()))
            .collect();
        assert_eq!(
            validate_record_event(&invalid),
            Err("event labels exceed 64 entries")
        );
    }
}
