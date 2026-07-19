//! Powermeter daemon library.
//!
//! Backends acquire raw power/energy readings; the serving runtime comes
//! from `wattch-daemon`. The binary in `main.rs` wires them together.

pub mod composite;
pub mod linux_os;
pub mod powermetrics;
pub mod rapl;
pub mod synthetic;
