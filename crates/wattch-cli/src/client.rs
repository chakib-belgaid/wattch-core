use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::{Duration, Instant};
use thiserror::Error;
use wattch_core::{RawSampleBatch, SourceDescriptor};
use wattch_protocol::frame::WireError;
use wattch_protocol::messages::{
    read_message, write_message, HelloRequest, HelloResponse, Message, StartCapture, StopCapture,
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

    #[error("daemon error {code}: {message}")]
    Daemon { code: u32, message: String },

    #[error("unexpected message {got:?}, expected {expected}")]
    Unexpected { got: String, expected: &'static str },
}

/// Blocking protocol v0.1 client over a Unix socket.
#[derive(Debug)]
pub struct WattchClient {
    stream: UnixStream,
    hello: HelloResponse,
}

impl WattchClient {
    pub fn connect(socket_path: &Path) -> Result<Self, ClientError> {
        let mut stream =
            UnixStream::connect(socket_path).map_err(|source| ClientError::Connect {
                path: socket_path.display().to_string(),
                source,
            })?;

        write_message(
            &mut stream,
            &Message::HelloRequest(HelloRequest {
                client_name: "wattch-cli".to_string(),
                client_version: env!("CARGO_PKG_VERSION").to_string(),
                protocol_version: PROTOCOL_VERSION.to_string(),
            }),
        )?;

        let hello = match read_message(&mut stream)? {
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

        Ok(Self { stream, hello })
    }

    pub fn hello(&self) -> &HelloResponse {
        &self.hello
    }

    pub fn sources(&self) -> &[SourceDescriptor] {
        &self.hello.sources
    }

    /// Capture for roughly `duration`, invoking `on_batch` per batch.
    ///
    /// The stop request goes out at the first batch after the deadline, so
    /// captures run one sampling interval past `duration` at most.
    pub fn capture(
        &mut self,
        run_id: &str,
        interval_ms: u64,
        duration: Duration,
        mut on_batch: impl FnMut(RawSampleBatch),
    ) -> Result<StopCaptureAck, ClientError> {
        write_message(
            &mut self.stream,
            &Message::StartCapture(StartCapture {
                run_id: run_id.to_string(),
                interval_ms,
                source_ids: vec![],
            }),
        )?;

        match read_message(&mut self.stream)? {
            Message::StartCaptureAck(_) => {}
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

        let deadline = Instant::now() + duration;
        let mut stop_sent = false;
        loop {
            match read_message(&mut self.stream)? {
                Message::RawSampleBatch(batch) => {
                    on_batch(batch);
                    if !stop_sent && Instant::now() >= deadline {
                        write_message(
                            &mut self.stream,
                            &Message::StopCapture(StopCapture {
                                run_id: run_id.to_string(),
                            }),
                        )?;
                        stop_sent = true;
                    }
                }
                Message::StopCaptureAck(ack) => return Ok(ack),
                Message::Error(err) => {
                    return Err(ClientError::Daemon {
                        code: err.code,
                        message: err.message,
                    })
                }
                other => {
                    return Err(ClientError::Unexpected {
                        got: format!("{:?}", other.kind()),
                        expected: "RawSampleBatch or StopCaptureAck",
                    })
                }
            }
        }
    }
}
