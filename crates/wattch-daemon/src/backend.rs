use wattch_core::{RawValue, SourceDescriptor};

/// A raw acquisition backend.
///
/// Implementations read values; the runtime owns timestamps, sequence
/// numbers, batching, and the protocol session. Backends must not aggregate
/// sources or derive metrics — that belongs to offline layers.
pub trait SampleBackend: Send {
    /// Descriptors for every source this backend exposes. Stable for the
    /// lifetime of the backend instance.
    fn descriptors(&self) -> &[SourceDescriptor];

    /// Read one raw value per source, as `(local_source_id, value)` pairs.
    fn sample(&mut self) -> Vec<(u32, RawValue)>;
}
