//! Protocol framing utilities for Wattch.
//!
//! The semantic protocol is defined in `proto/wattch/v0/wattch.proto` and
//! `docs/protocol-v0.md`.
//!
//! This crate starts with the frame envelope and version constants. Payload
//! serialization should remain swappable while the schema stabilizes.

pub mod frame;

pub const PROTOCOL_NAME: &str = "wattch";
pub const PROTOCOL_VERSION: &str = "0.1.0";
