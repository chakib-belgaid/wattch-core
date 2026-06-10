use std::io::{Read, Write};
use thiserror::Error;

pub const MAGIC: [u8; 4] = *b"WCH0";
pub const FRAME_HEADER_LEN: usize = 16;
pub const WIRE_VERSION_MAJOR: u16 = 0;
pub const WIRE_VERSION_MINOR: u16 = 1;

/// Reserved flag bit: payload is protobuf instead of JSON. Not used in v0.1.
pub const FLAG_PAYLOAD_PROTOBUF: u16 = 0b1;

/// Upper bound for a single frame payload. Control-plane messages are small
/// and sample batches are bounded by batching policy; anything larger is a
/// protocol violation, not a legitimate frame.
pub const MAX_PAYLOAD_LEN: u32 = 16 * 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u16)]
pub enum FrameKind {
    HelloRequest = 1,
    HelloResponse = 2,
    StartCapture = 3,
    StopCapture = 4,
    StartCaptureAck = 5,
    StopCaptureAck = 6,
    RawSampleBatch = 10,
    EventBatch = 11,
    Error = 255,
}

impl TryFrom<u16> for FrameKind {
    type Error = FrameError;

    fn try_from(value: u16) -> Result<Self, <Self as TryFrom<u16>>::Error> {
        match value {
            1 => Ok(Self::HelloRequest),
            2 => Ok(Self::HelloResponse),
            3 => Ok(Self::StartCapture),
            4 => Ok(Self::StopCapture),
            5 => Ok(Self::StartCaptureAck),
            6 => Ok(Self::StopCaptureAck),
            10 => Ok(Self::RawSampleBatch),
            11 => Ok(Self::EventBatch),
            255 => Ok(Self::Error),
            other => Err(FrameError::UnknownFrameKind(other)),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FrameHeader {
    pub kind: FrameKind,
    pub payload_len: u32,
    pub flags: u16,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum FrameError {
    #[error("payload too large: {0} bytes")]
    PayloadTooLarge(usize),

    #[error("buffer too short for frame header")]
    BufferTooShort,

    #[error("invalid magic bytes")]
    InvalidMagic,

    #[error("unsupported wire version {major}.{minor}")]
    UnsupportedVersion { major: u16, minor: u16 },

    #[error("unknown frame kind: {0}")]
    UnknownFrameKind(u16),
}

/// Errors for frame I/O over a stream transport.
#[derive(Debug, Error)]
pub enum WireError {
    #[error(transparent)]
    Frame(#[from] FrameError),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("invalid payload for {kind:?}: {reason}")]
    InvalidPayload { kind: FrameKind, reason: String },

    #[error("unexpected frame kind {got:?}, expected {expected}")]
    UnexpectedKind {
        got: FrameKind,
        expected: &'static str,
    },

    #[error("peer closed the connection")]
    ConnectionClosed,
}

pub fn encode_frame(kind: FrameKind, flags: u16, payload: &[u8]) -> Result<Vec<u8>, FrameError> {
    if payload.len() > u32::MAX as usize {
        return Err(FrameError::PayloadTooLarge(payload.len()));
    }

    let mut out = Vec::with_capacity(FRAME_HEADER_LEN + payload.len());
    out.extend_from_slice(&MAGIC);
    out.extend_from_slice(&WIRE_VERSION_MAJOR.to_le_bytes());
    out.extend_from_slice(&WIRE_VERSION_MINOR.to_le_bytes());
    out.extend_from_slice(&(kind as u16).to_le_bytes());
    out.extend_from_slice(&flags.to_le_bytes());
    out.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    out.extend_from_slice(payload);
    Ok(out)
}

pub fn decode_header(buf: &[u8]) -> Result<FrameHeader, FrameError> {
    if buf.len() < FRAME_HEADER_LEN {
        return Err(FrameError::BufferTooShort);
    }

    if buf[0..4] != MAGIC {
        return Err(FrameError::InvalidMagic);
    }

    let major = u16::from_le_bytes([buf[4], buf[5]]);
    let minor = u16::from_le_bytes([buf[6], buf[7]]);
    if major != WIRE_VERSION_MAJOR || minor != WIRE_VERSION_MINOR {
        return Err(FrameError::UnsupportedVersion { major, minor });
    }

    let raw_kind = u16::from_le_bytes([buf[8], buf[9]]);
    let flags = u16::from_le_bytes([buf[10], buf[11]]);
    let payload_len = u32::from_le_bytes([buf[12], buf[13], buf[14], buf[15]]);

    Ok(FrameHeader {
        kind: FrameKind::try_from(raw_kind)?,
        payload_len,
        flags,
    })
}

/// Write one complete frame to a stream.
pub fn write_frame(
    writer: &mut impl Write,
    kind: FrameKind,
    flags: u16,
    payload: &[u8],
) -> Result<(), WireError> {
    let frame = encode_frame(kind, flags, payload)?;
    writer.write_all(&frame)?;
    writer.flush()?;
    Ok(())
}

/// Read one complete frame from a stream.
///
/// Returns [`WireError::ConnectionClosed`] when the stream ends cleanly at a
/// frame boundary.
pub fn read_frame(reader: &mut impl Read) -> Result<(FrameHeader, Vec<u8>), WireError> {
    let mut header_buf = [0u8; FRAME_HEADER_LEN];
    let mut filled = 0;
    while filled < FRAME_HEADER_LEN {
        let n = reader.read(&mut header_buf[filled..])?;
        if n == 0 {
            if filled == 0 {
                return Err(WireError::ConnectionClosed);
            }
            return Err(WireError::Frame(FrameError::BufferTooShort));
        }
        filled += n;
    }

    let header = decode_header(&header_buf)?;
    if header.payload_len > MAX_PAYLOAD_LEN {
        return Err(WireError::Frame(FrameError::PayloadTooLarge(
            header.payload_len as usize,
        )));
    }

    let mut payload = vec![0u8; header.payload_len as usize];
    reader.read_exact(&mut payload)?;
    Ok((header, payload))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn round_trips_header() {
        let frame = encode_frame(FrameKind::RawSampleBatch, 0, b"abc").unwrap();
        let header = decode_header(&frame).unwrap();
        assert_eq!(header.kind, FrameKind::RawSampleBatch);
        assert_eq!(header.payload_len, 3);
        assert_eq!(&frame[FRAME_HEADER_LEN..], b"abc");
    }

    #[test]
    fn round_trips_ack_kinds() {
        for kind in [FrameKind::StartCaptureAck, FrameKind::StopCaptureAck] {
            let frame = encode_frame(kind, 0, b"{}").unwrap();
            assert_eq!(decode_header(&frame).unwrap().kind, kind);
        }
    }

    #[test]
    fn read_write_frame_round_trip_over_stream() {
        let mut buf = Vec::new();
        write_frame(&mut buf, FrameKind::HelloRequest, 0, b"payload-1").unwrap();
        write_frame(&mut buf, FrameKind::Error, 0, b"payload-2").unwrap();

        let mut cursor = Cursor::new(buf);
        let (h1, p1) = read_frame(&mut cursor).unwrap();
        assert_eq!(h1.kind, FrameKind::HelloRequest);
        assert_eq!(p1, b"payload-1");
        let (h2, p2) = read_frame(&mut cursor).unwrap();
        assert_eq!(h2.kind, FrameKind::Error);
        assert_eq!(p2, b"payload-2");
        assert!(matches!(
            read_frame(&mut cursor),
            Err(WireError::ConnectionClosed)
        ));
    }

    #[test]
    fn read_frame_rejects_bad_magic() {
        let mut frame = encode_frame(FrameKind::HelloRequest, 0, b"x").unwrap();
        frame[0] = b'X';
        assert!(matches!(
            read_frame(&mut Cursor::new(frame)),
            Err(WireError::Frame(FrameError::InvalidMagic))
        ));
    }

    #[test]
    fn read_frame_rejects_oversized_payload() {
        let mut frame = encode_frame(FrameKind::HelloRequest, 0, b"x").unwrap();
        let oversized = (MAX_PAYLOAD_LEN + 1).to_le_bytes();
        frame[12..16].copy_from_slice(&oversized);
        assert!(matches!(
            read_frame(&mut Cursor::new(frame)),
            Err(WireError::Frame(FrameError::PayloadTooLarge(_)))
        ));
    }

    #[test]
    fn rejects_unknown_kind() {
        let mut frame = encode_frame(FrameKind::HelloRequest, 0, b"").unwrap();
        frame[8..10].copy_from_slice(&42u16.to_le_bytes());
        assert!(matches!(
            decode_header(&frame),
            Err(FrameError::UnknownFrameKind(42))
        ));
    }
}
