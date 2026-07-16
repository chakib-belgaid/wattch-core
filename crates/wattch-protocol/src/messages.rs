//! Typed protocol v0.1 messages.
//!
//! Payloads are JSON in v0.1. The frame envelope reserves
//! [`crate::frame::FLAG_PAYLOAD_PROTOBUF`] for a later protobuf migration.

use crate::frame::{read_frame, write_frame, FrameKind, WireError};
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use wattch_core::{RawSampleBatch, SourceDescriptor};

/// Run ids are repeated in every data-plane batch, so keep the field small
/// enough that a control-plane request cannot amplify capture memory/traffic.
pub const MAX_RUN_ID_BYTES: usize = 256;

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

/// All protocol v0.1 messages, paired with their frame kind.
#[derive(Clone, Debug, PartialEq)]
pub enum Message {
    HelloRequest(HelloRequest),
    HelloResponse(HelloResponse),
    StartCapture(StartCapture),
    StartCaptureAck(StartCaptureAck),
    StopCapture(StopCapture),
    StopCaptureAck(StopCaptureAck),
    RawSampleBatch(RawSampleBatch),
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
            Message::RawSampleBatch(_) => FrameKind::RawSampleBatch,
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
            Message::RawSampleBatch(m) => serde_json::to_vec(m),
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
        FrameKind::RawSampleBatch => Ok(Message::RawSampleBatch(parse(kind, payload)?)),
        FrameKind::Error => Ok(Message::Error(parse(kind, payload)?)),
        FrameKind::EventBatch => Err(WireError::InvalidPayload {
            kind,
            reason: "EventBatch is reserved in protocol v0.1".to_string(),
        }),
    }
}

pub fn write_message(writer: &mut impl Write, message: &Message) -> Result<(), WireError> {
    let payload = message.payload_json()?;
    write_frame(writer, message.kind(), 0, &payload)
}

pub fn read_message(reader: &mut impl Read) -> Result<Message, WireError> {
    let (header, payload) = read_frame(reader)?;
    decode_message(header.kind, &payload)
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
                protocol_version: "0.1.0".into(),
                node_id: "node-0".into(),
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
}
