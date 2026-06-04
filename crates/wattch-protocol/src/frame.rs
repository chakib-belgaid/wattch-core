use thiserror::Error;

pub const MAGIC: [u8; 4] = *b"WCH0";
pub const FRAME_HEADER_LEN: usize = 16;
pub const WIRE_VERSION_MAJOR: u16 = 0;
pub const WIRE_VERSION_MINOR: u16 = 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u16)]
pub enum FrameKind {
    HelloRequest = 1,
    HelloResponse = 2,
    StartCapture = 3,
    StopCapture = 4,
    RawSampleBatch = 10,
    EventBatch = 11,
    Error = 255,
}

impl TryFrom<u16> for FrameKind {
    type Error = FrameError;

    fn try_from(value: u16) -> Result<Self, Self::Error> {
        match value {
            1 => Ok(Self::HelloRequest),
            2 => Ok(Self::HelloResponse),
            3 => Ok(Self::StartCapture),
            4 => Ok(Self::StopCapture),
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_header() {
        let frame = encode_frame(FrameKind::RawSampleBatch, 0, b"abc").unwrap();
        let header = decode_header(&frame).unwrap();
        assert_eq!(header.kind, FrameKind::RawSampleBatch);
        assert_eq!(header.payload_len, 3);
        assert_eq!(&frame[FRAME_HEADER_LEN..], b"abc");
    }
}
