//! MCP stdio adapter for Wattch.
//!
//! This crate is deliberately a client of the Wattch daemon. It exposes raw
//! source discovery, capture lifecycle, and offline artifact validation to
//! coding agents without moving aggregation or energy interpretation into the
//! acquisition boundary.

mod capture;
mod mcp;
mod service;
mod status;

pub use mcp::{McpServer, CURRENT_MCP_VERSION, SUPPORTED_MCP_VERSIONS};
pub use service::{ServiceConfig, ServiceError, WattchService};
