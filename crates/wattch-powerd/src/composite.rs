//! Descriptor-preserving composition of raw acquisition backends.
//!
//! `local_source_id` is scoped to one daemon/run, so a composite is allowed to
//! remap child ids. Stable identities (`source_uid`), topology, evidence class,
//! units, and every other descriptor field remain unchanged.

use std::collections::{BTreeMap, BTreeSet};
use thiserror::Error;
use tracing::warn;
use wattch_core::{validate_source_set, RawValue, SourceDescriptor};
use wattch_daemon::{AcquiredReading, SampleBackend};

#[derive(Debug, Error)]
pub enum CompositeError {
    #[error("composite backend has too many sources")]
    TooManySources,

    #[error("invalid source set from child backend {child_index}: {source}")]
    InvalidChildSources {
        child_index: usize,
        #[source]
        source: wattch_core::Error,
    },

    #[error("invalid composite source set: {0}")]
    InvalidSources(#[from] wattch_core::Error),
}

/// A raw acquisition backend that can be cloned for a new daemon connection.
///
/// Wattch's server factory is infallible and creates one backend per client.
/// Requiring cloneable children lets a fully validated composite be built once
/// at startup and cloned thereafter, instead of rebuilding it with a panic in
/// the connection factory.
pub trait CloneSampleBackend: SampleBackend + Sync {
    fn clone_sample_backend(&self) -> Box<dyn CloneSampleBackend>;
}

impl<T> CloneSampleBackend for T
where
    T: SampleBackend + Clone + Sync + 'static,
{
    fn clone_sample_backend(&self) -> Box<dyn CloneSampleBackend> {
        Box::new(self.clone())
    }
}

impl Clone for Box<dyn CloneSampleBackend> {
    fn clone(&self) -> Self {
        self.clone_sample_backend()
    }
}

#[derive(Clone)]
struct ChildBackend {
    backend: Box<dyn CloneSampleBackend>,
    id_map: BTreeMap<u32, u32>,
}

/// Combines independent raw collectors without aggregating their readings.
#[derive(Clone)]
pub struct CompositeBackend {
    descriptors: Vec<SourceDescriptor>,
    children: Vec<ChildBackend>,
}

impl CompositeBackend {
    pub fn new(backends: Vec<Box<dyn CloneSampleBackend>>) -> Result<Self, CompositeError> {
        let mut descriptors = Vec::new();
        let mut children = Vec::with_capacity(backends.len());

        for (child_index, backend) in backends.into_iter().enumerate() {
            validate_source_set(backend.descriptors()).map_err(|source| {
                CompositeError::InvalidChildSources {
                    child_index,
                    source,
                }
            })?;

            let mut id_map = BTreeMap::new();
            for descriptor in backend.descriptors() {
                let one_based_index = descriptors
                    .len()
                    .checked_add(1)
                    .ok_or(CompositeError::TooManySources)?;
                let assigned_id =
                    u32::try_from(one_based_index).map_err(|_| CompositeError::TooManySources)?;
                id_map.insert(descriptor.local_source_id, assigned_id);

                let mut remapped = descriptor.clone();
                remapped.local_source_id = assigned_id;
                descriptors.push(remapped);
            }
            children.push(ChildBackend { backend, id_map });
        }

        validate_source_set(&descriptors)?;
        Ok(Self {
            descriptors,
            children,
        })
    }
}

impl SampleBackend for CompositeBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn begin_capture(&mut self) {
        for child in &mut self.children {
            child.backend.begin_capture();
        }
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        let mut readings = Vec::with_capacity(self.descriptors.len());
        for child in &mut self.children {
            let mut seen_ids = BTreeSet::new();
            for (child_id, value) in child.backend.sample() {
                let Some(composite_id) = child.id_map.get(&child_id).copied() else {
                    warn!(
                        child_source_id = child_id,
                        "raw backend returned an unadvertised source; skipping reading"
                    );
                    continue;
                };
                if !seen_ids.insert(child_id) {
                    warn!(
                        child_source_id = child_id,
                        "raw backend returned a duplicate source in one tick; skipping reading"
                    );
                    continue;
                }
                readings.push((composite_id, value));
            }
        }
        readings
    }

    fn sample_acquired(&mut self) -> Vec<AcquiredReading> {
        let mut readings = Vec::with_capacity(self.descriptors.len());
        for child in &mut self.children {
            let mut seen_ids = BTreeSet::new();
            for reading in child.backend.sample_acquired() {
                let child_id = reading.local_source_id;
                let Some(composite_id) = child.id_map.get(&child_id).copied() else {
                    warn!(
                        child_source_id = child_id,
                        "raw backend returned an unadvertised source; skipping reading"
                    );
                    continue;
                };
                if !seen_ids.insert(child_id) {
                    warn!(
                        child_source_id = child_id,
                        "raw backend returned a duplicate source in one tick; skipping reading"
                    );
                    continue;
                }
                readings.push(AcquiredReading::new(
                    composite_id,
                    reading.value,
                    reading.acquired_at,
                ));
            }
        }
        readings
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::{Duration, Instant};
    use wattch_core::{EvidenceClass, RawValue, SamplingMode, SourceKind, Temporality, Unit};

    #[derive(Clone)]
    struct StubBackend {
        descriptors: Vec<SourceDescriptor>,
        readings: Vec<(u32, RawValue)>,
        capture_starts: Option<Arc<AtomicUsize>>,
    }

    impl SampleBackend for StubBackend {
        fn descriptors(&self) -> &[SourceDescriptor] {
            &self.descriptors
        }

        fn begin_capture(&mut self) {
            if let Some(capture_starts) = &self.capture_starts {
                capture_starts.fetch_add(1, Ordering::SeqCst);
            }
        }

        fn sample(&mut self) -> Vec<(u32, RawValue)> {
            self.readings.clone()
        }
    }

    fn descriptor(uid: &str, path: &str) -> SourceDescriptor {
        SourceDescriptor {
            local_source_id: 1,
            source_uid: uid.to_string(),
            node_id: "node-0".to_string(),
            backend: "stub".to_string(),
            kind: SourceKind::Other,
            unit: Unit::Counter,
            temporality: Temporality::Cumulative,
            evidence_class: EvidenceClass::OsTelemetry,
            source_path: path.to_string(),
            parent_source_uid: None,
            clock_domain: "monotonic".to_string(),
            sampling_mode: SamplingMode::Polled,
            nominal_period_ns: None,
            max_value: None,
            resolution: Some(1.0),
            labels: BTreeMap::new(),
        }
    }

    #[test]
    fn remaps_colliding_local_ids_without_changing_stable_identity() {
        let a = StubBackend {
            descriptors: vec![descriptor("stub://node-0/a", "a")],
            readings: vec![(1, RawValue::I64(10))],
            capture_starts: None,
        };
        let b = StubBackend {
            descriptors: vec![descriptor("stub://node-0/b", "b")],
            readings: vec![(1, RawValue::I64(20))],
            capture_starts: None,
        };

        let mut composite = CompositeBackend::new(vec![Box::new(a), Box::new(b)]).unwrap();
        assert_eq!(composite.descriptors()[0].local_source_id, 1);
        assert_eq!(composite.descriptors()[1].local_source_id, 2);
        assert_eq!(composite.descriptors()[1].source_uid, "stub://node-0/b");
        assert_eq!(
            composite.sample(),
            vec![(1, RawValue::I64(10)), (2, RawValue::I64(20))]
        );
    }

    #[test]
    fn rejects_duplicate_stable_source_identity() {
        let a = StubBackend {
            descriptors: vec![descriptor("stub://node-0/shared", "a")],
            readings: vec![(1, RawValue::I64(1))],
            capture_starts: None,
        };
        let b = StubBackend {
            descriptors: vec![descriptor("stub://node-0/shared", "b")],
            readings: vec![(1, RawValue::I64(2))],
            capture_starts: None,
        };

        assert!(CompositeBackend::new(vec![Box::new(a), Box::new(b)]).is_err());
    }

    #[test]
    fn rejects_duplicate_local_ids_inside_a_child_before_remapping() {
        let backend = StubBackend {
            descriptors: vec![
                descriptor("stub://node-0/a", "a"),
                descriptor("stub://node-0/b", "b"),
            ],
            readings: Vec::new(),
            capture_starts: None,
        };

        assert!(matches!(
            CompositeBackend::new(vec![Box::new(backend)]),
            Err(CompositeError::InvalidChildSources { child_index: 0, .. })
        ));
    }

    #[test]
    fn clone_preserves_layout_and_resets_children_from_the_prototype() {
        let backend = StubBackend {
            descriptors: vec![descriptor("stub://node-0/a", "a")],
            readings: vec![(1, RawValue::I64(10))],
            capture_starts: None,
        };
        let prototype = CompositeBackend::new(vec![Box::new(backend)]).unwrap();
        let mut clone = prototype.clone();

        assert_eq!(clone.descriptors(), prototype.descriptors());
        assert_eq!(clone.sample(), vec![(1, RawValue::I64(10))]);
    }

    #[test]
    fn skips_unadvertised_and_duplicate_child_readings() {
        let backend = StubBackend {
            descriptors: vec![descriptor("stub://node-0/a", "a")],
            readings: vec![
                (1, RawValue::I64(10)),
                (1, RawValue::I64(20)),
                (99, RawValue::I64(30)),
            ],
            capture_starts: None,
        };
        let mut composite = CompositeBackend::new(vec![Box::new(backend)]).unwrap();

        assert_eq!(composite.sample(), vec![(1, RawValue::I64(10))]);
    }

    #[test]
    fn begin_capture_is_propagated_once_to_every_child() {
        let first_starts = Arc::new(AtomicUsize::new(0));
        let second_starts = Arc::new(AtomicUsize::new(0));
        let first = StubBackend {
            descriptors: vec![descriptor("stub://node-0/a", "a")],
            readings: Vec::new(),
            capture_starts: Some(Arc::clone(&first_starts)),
        };
        let second = StubBackend {
            descriptors: vec![descriptor("stub://node-0/b", "b")],
            readings: Vec::new(),
            capture_starts: Some(Arc::clone(&second_starts)),
        };
        let mut composite = CompositeBackend::new(vec![Box::new(first), Box::new(second)]).unwrap();

        composite.begin_capture();

        assert_eq!(first_starts.load(Ordering::SeqCst), 1);
        assert_eq!(second_starts.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn preserves_each_childs_acquisition_instant() {
        #[derive(Clone)]
        struct TimedStub {
            descriptors: Vec<SourceDescriptor>,
            value: i64,
            acquired_at: Instant,
        }

        impl SampleBackend for TimedStub {
            fn descriptors(&self) -> &[SourceDescriptor] {
                &self.descriptors
            }

            fn sample(&mut self) -> Vec<(u32, RawValue)> {
                vec![(1, RawValue::I64(self.value))]
            }

            fn sample_acquired(&mut self) -> Vec<AcquiredReading> {
                vec![AcquiredReading::new(
                    1,
                    RawValue::I64(self.value),
                    self.acquired_at,
                )]
            }
        }

        let now = Instant::now();
        let first_at = now - Duration::from_millis(20);
        let second_at = now - Duration::from_millis(5);
        let first = TimedStub {
            descriptors: vec![descriptor("stub://node-0/a", "a")],
            value: 10,
            acquired_at: first_at,
        };
        let second = TimedStub {
            descriptors: vec![descriptor("stub://node-0/b", "b")],
            value: 20,
            acquired_at: second_at,
        };
        let mut composite = CompositeBackend::new(vec![Box::new(first), Box::new(second)]).unwrap();

        let readings = composite.sample_acquired();

        assert_eq!(readings.len(), 2);
        assert_eq!(readings[0].local_source_id, 1);
        assert_eq!(readings[0].acquired_at, first_at);
        assert_eq!(readings[1].local_source_id, 2);
        assert_eq!(readings[1].acquired_at, second_at);
    }
}
