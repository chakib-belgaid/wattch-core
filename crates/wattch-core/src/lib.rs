//! Shared primitives for Wattch.
//!
//! This crate contains domain types only: identifiers, source descriptors,
//! units, evidence classes, samples, batches, manifests, and validation.
//!
//! It must not contain daemon logic, CLI logic, hardware backends,
//! preprocessing, benchmark interpretation, or impact analysis.

pub mod error;
pub mod model;
pub mod research;
pub mod validate;

pub use error::{Error, Result};
pub use model::*;
pub use research::*;
pub use validate::*;
