use std::collections::BTreeSet;
use std::io::{self, Read};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::{Duration, Instant};
use thiserror::Error;
use wattch_core::{validate_source_set, RawSampleBatch, RawStreamValidator, SourceDescriptor};
use wattch_protocol::frame::WireError;
use wattch_protocol::messages::{
    read_message, validate_run_id, write_message, HelloRequest, HelloResponse, Message,
    StartCapture, StopCapture, StopCaptureAck,
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
const STOP_GRACE: Duration = Duration::from_secs(5);
const CAPTURE_BATCH_SLACK: u64 = 64;
const MAX_CAPTURE_BATCH_BYTES: usize = 1024 * 1024;

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
        if hello.protocol_version != PROTOCOL_VERSION {
            return Err(ClientError::Protocol(format!(
                "daemon protocol {} does not match client protocol {}",
                hello.protocol_version, PROTOCOL_VERSION
            )));
        }
        validate_source_set(&hello.sources)?;
        if hello.node_id.trim().is_empty() {
            return Err(ClientError::Protocol(
                "HelloResponse node_id must not be empty".to_string(),
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
    /// The active deadline is absolute: a silent or overproducing daemon
    /// cannot keep the client draining indefinitely.
    pub fn capture(
        &mut self,
        run_id: &str,
        interval_ms: u64,
        duration: Duration,
        mut on_batch: impl FnMut(RawSampleBatch),
    ) -> Result<StopCaptureAck, ClientError> {
        validate_run_id(run_id).map_err(|reason| ClientError::Protocol(reason.to_string()))?;
        if interval_ms == 0 {
            return Err(ClientError::Protocol(
                "interval_ms must be non-zero".to_string(),
            ));
        }
        write_message(
            &mut self.stream,
            &Message::StartCapture(StartCapture {
                run_id: run_id.to_string(),
                interval_ms,
                source_ids: vec![],
            }),
        )?;

        let advertised_ids: BTreeSet<u32> = self
            .hello
            .sources
            .iter()
            .map(|source| source.local_source_id)
            .collect();
        match read_message_before(&mut self.stream, CONTROL_TIMEOUT)? {
            Message::StartCaptureAck(ack) => {
                if ack.run_id != run_id {
                    return Err(ClientError::Protocol(format!(
                        "StartCaptureAck run_id {:?} does not match {:?}",
                        ack.run_id, run_id
                    )));
                }
                let accepted: BTreeSet<u32> = ack.accepted_source_ids.iter().copied().collect();
                if accepted.len() != ack.accepted_source_ids.len() || accepted != advertised_ids {
                    return Err(ClientError::Protocol(
                        "StartCaptureAck accepted_source_ids do not exactly match advertised sources"
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

        let deadline = Instant::now()
            .checked_add(duration)
            .ok_or_else(|| ClientError::Protocol("capture duration is too large".to_string()))?;
        let expected_batches = expected_batch_count(duration, interval_ms);
        let stop_after_batches = expected_batches.saturating_add(CAPTURE_BATCH_SLACK);
        let hard_batch_limit = stop_after_batches.saturating_add(CAPTURE_BATCH_SLACK);
        let mut validator =
            RawStreamValidator::new(run_id, self.hello.node_id.clone(), &self.hello.sources)?;
        let mut stop_sent = false;
        let mut stop_deadline = None;
        loop {
            if !stop_sent
                && (Instant::now() >= deadline || validator.batches_seen() >= stop_after_batches)
            {
                self.send_stop(run_id)?;
                stop_sent = true;
                stop_deadline = Some(Instant::now().checked_add(STOP_GRACE).ok_or_else(|| {
                    ClientError::Protocol("stop deadline is too large".to_string())
                })?);
            }

            let read_deadline = if stop_sent {
                stop_deadline
                    .ok_or_else(|| ClientError::Protocol("missing stop deadline".to_string()))?
            } else {
                deadline
            };
            if Instant::now() >= read_deadline {
                if !stop_sent {
                    continue;
                }
                let _ = self.stream.shutdown(Shutdown::Both);
                return Err(ClientError::Protocol(
                    "daemon did not acknowledge StopCapture before the deadline".to_string(),
                ));
            }
            let message =
                match read_message_before(&mut self.stream, nonzero_remaining(read_deadline)) {
                    Ok(message) => message,
                    Err(err) if wire_timeout(&err) => continue,
                    Err(err) => return Err(err.into()),
                };
            match message {
                Message::RawSampleBatch(batch) => {
                    let encoded_len = serde_json::to_vec(&batch)
                        .map_err(|err| ClientError::Protocol(err.to_string()))?
                        .len();
                    if encoded_len > MAX_CAPTURE_BATCH_BYTES {
                        let _ = self.stream.shutdown(Shutdown::Both);
                        return Err(ClientError::Protocol(format!(
                            "raw batch is {encoded_len} bytes; limit is {MAX_CAPTURE_BATCH_BYTES}"
                        )));
                    }
                    validator.validate(&batch)?;
                    on_batch(batch);
                    if validator.batches_seen() > hard_batch_limit {
                        let _ = self.stream.shutdown(Shutdown::Both);
                        return Err(ClientError::Protocol(format!(
                            "daemon exceeded capture batch limit {hard_batch_limit}"
                        )));
                    }
                }
                Message::StopCaptureAck(ack) => {
                    if !stop_sent {
                        return Err(ClientError::Protocol(
                            "daemon sent StopCaptureAck before StopCapture".to_string(),
                        ));
                    }
                    if ack.run_id != run_id {
                        return Err(ClientError::Protocol(format!(
                            "StopCaptureAck run_id {:?} does not match {:?}",
                            ack.run_id, run_id
                        )));
                    }
                    if ack.batches_sent != validator.batches_seen()
                        || ack.samples_sent != validator.samples_seen()
                    {
                        return Err(ClientError::Protocol(format!(
                            "StopCaptureAck counts {}/{} do not match observed {}/{}",
                            ack.batches_sent,
                            ack.samples_sent,
                            validator.batches_seen(),
                            validator.samples_seen()
                        )));
                    }
                    self.stream.set_read_timeout(Some(CONTROL_TIMEOUT))?;
                    return Ok(ack);
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
                        expected: "RawSampleBatch or StopCaptureAck",
                    })
                }
            }
        }
    }

    fn send_stop(&mut self, run_id: &str) -> Result<(), ClientError> {
        write_message(
            &mut self.stream,
            &Message::StopCapture(StopCapture {
                run_id: run_id.to_string(),
            }),
        )?;
        Ok(())
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

struct DeadlineReader<'a> {
    stream: &'a mut UnixStream,
    deadline: Instant,
}

impl Read for DeadlineReader<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let Some(remaining) = self.deadline.checked_duration_since(Instant::now()) else {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "read deadline exceeded",
            ));
        };
        self.stream
            .set_read_timeout(Some(remaining.max(Duration::from_millis(1))))?;
        self.stream.read(buffer)
    }
}

fn read_message_before(stream: &mut UnixStream, timeout: Duration) -> Result<Message, WireError> {
    let deadline = Instant::now()
        .checked_add(timeout)
        .ok_or_else(|| WireError::Io(io::Error::other("read deadline is too large")))?;
    read_message(&mut DeadlineReader { stream, deadline })
}
