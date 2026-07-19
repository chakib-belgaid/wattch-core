use std::time::Instant;
use wattch_core::{RawValue, SourceDescriptor};

/// One raw backend reading together with the process-monotonic instant at
/// which it was acquired.
///
/// The daemon converts this opaque [`Instant`] into its daemon-wide
/// `clock_instance_id` epoch before writing a protocol
/// [`wattch_core::RawSample`]. Keeping the instant internal avoids changing
/// the compact wire sample while still allowing independently updating
/// collectors to preserve receipt time.
#[derive(Clone, Debug, PartialEq)]
pub struct AcquiredReading {
    pub local_source_id: u32,
    pub value: RawValue,
    pub acquired_at: Instant,
}

impl AcquiredReading {
    pub fn new(local_source_id: u32, value: RawValue, acquired_at: Instant) -> Self {
        Self {
            local_source_id,
            value,
            acquired_at,
        }
    }
}

/// A raw acquisition backend.
///
/// Implementations read values; the runtime owns timestamps, sequence
/// numbers, batching, and the protocol session. Backends must not aggregate
/// sources or derive metrics — that belongs to offline layers.
pub trait SampleBackend: Send {
    /// Descriptors for every source this backend exposes. Stable for the
    /// lifetime of the backend instance.
    fn descriptors(&self) -> &[SourceDescriptor];

    /// Prepare this backend for a newly accepted capture.
    ///
    /// The runtime calls this exactly once after validating `StartCapture`
    /// and immediately before acknowledging it. Most raw collectors need no
    /// reset. Backends backed by an independently updating collector can use
    /// the hook to discard a cached pre-capture value so the first emitted
    /// reading was acquired after the capture request.
    fn begin_capture(&mut self) {}

    /// Read one raw value per source, as `(local_source_id, value)` pairs.
    fn sample(&mut self) -> Vec<(u32, RawValue)>;

    /// Read raw values with their acquisition instants.
    ///
    /// The default preserves existing simple backends and timestamps the
    /// completed collection call. Backends that collect children
    /// sequentially, or that consume an independently updating producer,
    /// should override this method so each reading retains the closest known
    /// acquisition or receipt instant.
    fn sample_acquired(&mut self) -> Vec<AcquiredReading> {
        let readings = self.sample();
        let acquired_at = Instant::now();
        readings
            .into_iter()
            .map(|(local_source_id, value)| {
                AcquiredReading::new(local_source_id, value, acquired_at)
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;
    use std::time::Duration;

    struct DelayedBackend;

    impl SampleBackend for DelayedBackend {
        fn descriptors(&self) -> &[SourceDescriptor] {
            &[]
        }

        fn sample(&mut self) -> Vec<(u32, RawValue)> {
            thread::sleep(Duration::from_millis(5));
            vec![(1, RawValue::I64(1))]
        }
    }

    #[test]
    fn default_acquisition_timestamp_is_taken_after_collection() {
        let before_collection = Instant::now();
        let reading = DelayedBackend.sample_acquired().pop().unwrap();

        assert!(reading.acquired_at >= before_collection + Duration::from_millis(5));
    }
}
