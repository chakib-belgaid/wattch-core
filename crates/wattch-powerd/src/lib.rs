//! Powermeter daemon library.
//!
//! Backends acquire raw power/energy readings; the serving runtime comes
//! from `wattch-daemon`. The binary in `main.rs` wires them together.

pub mod rapl;
pub mod synthetic;
