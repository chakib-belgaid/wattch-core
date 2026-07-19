//! Shared serving runtime for Wattch daemons.
//!
//! Owns the socket transport, the session state machine, timestamps,
//! sequence numbers, and batching. Backends only read raw values and retain
//! their nearest process-monotonic acquisition instants. This keeps the
//! dumb-daemon rule structural: a [`backend::SampleBackend`] has no way to
//! aggregate, derive, or interpret.

pub mod backend;
pub mod peer;
pub mod server;

pub use backend::{AcquiredReading, SampleBackend};
pub use peer::{PeerCreds, PeerPolicy};
pub use server::{
    spawn, DaemonIdentity, ServerConfig, ServerError, ServerHandle, ServerLimits, SocketSource,
};
