use crate::error::{Error, Result};
use crate::model::{RawSampleBatch, SourceDescriptor};
use std::collections::{BTreeMap, BTreeSet};

pub fn validate_source_descriptor(desc: &SourceDescriptor) -> Result<()> {
    if desc.source_uid.trim().is_empty() {
        return Err(Error::MissingField("source_uid"));
    }
    if desc.node_id.trim().is_empty() {
        return Err(Error::MissingField("node_id"));
    }
    if desc.backend.trim().is_empty() {
        return Err(Error::MissingField("backend"));
    }
    if desc.source_path.trim().is_empty() {
        return Err(Error::MissingField("source_path"));
    }
    if desc.clock_domain.trim().is_empty() {
        return Err(Error::MissingField("clock_domain"));
    }
    Ok(())
}

pub fn validate_source_set(sources: &[SourceDescriptor]) -> Result<()> {
    let mut ids = BTreeSet::new();
    let mut uids = BTreeSet::new();

    for desc in sources {
        validate_source_descriptor(desc)?;

        if !ids.insert(desc.local_source_id) {
            return Err(Error::DuplicateLocalSourceId(desc.local_source_id));
        }
        if !uids.insert(desc.source_uid.clone()) {
            return Err(Error::DuplicateSourceUid(desc.source_uid.clone()));
        }
    }

    Ok(())
}

pub fn validate_batch(batch: &RawSampleBatch, sources: &[SourceDescriptor]) -> Result<()> {
    let known_ids = sources
        .iter()
        .map(|s| s.local_source_id)
        .collect::<BTreeSet<_>>();

    let mut seen_ids = BTreeSet::new();
    if batch.samples.len() > known_ids.len() {
        return Err(Error::InvalidBatch(format!(
            "batch has {} samples for only {} advertised sources",
            batch.samples.len(),
            known_ids.len()
        )));
    }

    for sample in &batch.samples {
        if !known_ids.contains(&sample.source_id) {
            return Err(Error::InvalidSample(format!(
                "sample references unknown source id {}",
                sample.source_id
            )));
        }
        if !seen_ids.insert(sample.source_id) {
            return Err(Error::InvalidBatch(format!(
                "batch repeats source id {}",
                sample.source_id
            )));
        }
    }

    Ok(())
}

#[derive(Clone, Copy, Debug)]
struct SamplePosition {
    sequence_no: u64,
    monotonic_ns: u64,
}

/// Stateful integrity validator for one raw capture stream.
///
/// Sequence gaps remain valid evidence-quality signals, but replay/reordering,
/// identity changes, duplicate sources, timestamp regression, and unknown
/// source references are rejected before data is persisted or interpreted.
#[derive(Clone, Debug)]
pub struct RawStreamValidator {
    run_id: String,
    node_id: String,
    known_ids: BTreeSet<u32>,
    last_batch_sequence_no: Option<u64>,
    sample_positions: BTreeMap<u32, SamplePosition>,
    batches_seen: u64,
    samples_seen: u64,
}

impl RawStreamValidator {
    pub fn new(
        run_id: impl Into<String>,
        node_id: impl Into<String>,
        sources: &[SourceDescriptor],
    ) -> Result<Self> {
        validate_source_set(sources)?;
        let run_id = run_id.into();
        let node_id = node_id.into();
        if run_id.is_empty() {
            return Err(Error::MissingField("run_id"));
        }
        if node_id.is_empty() {
            return Err(Error::MissingField("node_id"));
        }
        if let Some(source) = sources.iter().find(|source| source.node_id != node_id) {
            return Err(Error::InvalidSourceDescriptor {
                source_uid: source.source_uid.clone(),
                reason: format!(
                    "descriptor node_id {:?} does not match stream node_id {:?}",
                    source.node_id, node_id
                ),
            });
        }

        Ok(Self {
            run_id,
            node_id,
            known_ids: sources
                .iter()
                .map(|source| source.local_source_id)
                .collect(),
            last_batch_sequence_no: None,
            sample_positions: BTreeMap::new(),
            batches_seen: 0,
            samples_seen: 0,
        })
    }

    pub fn validate(&mut self, batch: &RawSampleBatch) -> Result<()> {
        if batch.run_id != self.run_id {
            return Err(Error::InvalidBatch(format!(
                "run_id {:?} does not match expected {:?}",
                batch.run_id, self.run_id
            )));
        }
        if batch.node_id != self.node_id {
            return Err(Error::InvalidBatch(format!(
                "node_id {:?} does not match expected {:?}",
                batch.node_id, self.node_id
            )));
        }
        if let Some(last) = self.last_batch_sequence_no {
            if batch.batch_sequence_no <= last {
                return Err(Error::InvalidBatch(format!(
                    "batch sequence {} does not advance past {}",
                    batch.batch_sequence_no, last
                )));
            }
        }
        if batch.samples.len() > self.known_ids.len() {
            return Err(Error::InvalidBatch(format!(
                "batch has {} samples for only {} advertised sources",
                batch.samples.len(),
                self.known_ids.len()
            )));
        }

        let mut seen_ids = BTreeSet::new();
        for sample in &batch.samples {
            if !self.known_ids.contains(&sample.source_id) {
                return Err(Error::InvalidSample(format!(
                    "sample references unknown source id {}",
                    sample.source_id
                )));
            }
            if !seen_ids.insert(sample.source_id) {
                return Err(Error::InvalidBatch(format!(
                    "batch repeats source id {}",
                    sample.source_id
                )));
            }
            if let Some(last) = self.sample_positions.get(&sample.source_id) {
                if sample.sequence_no <= last.sequence_no {
                    return Err(Error::InvalidSample(format!(
                        "source {} sequence {} does not advance past {}",
                        sample.source_id, sample.sequence_no, last.sequence_no
                    )));
                }
                if sample.monotonic_ns < last.monotonic_ns {
                    return Err(Error::InvalidSample(format!(
                        "source {} timestamp {} regresses from {}",
                        sample.source_id, sample.monotonic_ns, last.monotonic_ns
                    )));
                }
            }
        }

        for sample in &batch.samples {
            self.sample_positions.insert(
                sample.source_id,
                SamplePosition {
                    sequence_no: sample.sequence_no,
                    monotonic_ns: sample.monotonic_ns,
                },
            );
        }
        self.last_batch_sequence_no = Some(batch.batch_sequence_no);
        self.batches_seen = self
            .batches_seen
            .checked_add(1)
            .ok_or_else(|| Error::InvalidBatch("batch count overflow".to_string()))?;
        self.samples_seen = self
            .samples_seen
            .checked_add(batch.samples.len() as u64)
            .ok_or_else(|| Error::InvalidBatch("sample count overflow".to_string()))?;
        Ok(())
    }

    pub fn batches_seen(&self) -> u64 {
        self.batches_seen
    }

    pub fn samples_seen(&self) -> u64 {
        self.samples_seen
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::*;

    fn descriptor(id: u32, uid: &str) -> SourceDescriptor {
        SourceDescriptor {
            local_source_id: id,
            source_uid: uid.to_string(),
            node_id: "node-0".to_string(),
            backend: "deterministic".to_string(),
            kind: SourceKind::CpuPackageEnergy,
            unit: Unit::Joule,
            temporality: Temporality::Cumulative,
            evidence_class: EvidenceClass::RawHardwareCounter,
            source_path: "cpu/package/0/energy".to_string(),
            parent_source_uid: None,
            clock_domain: "monotonic".to_string(),
            sampling_mode: SamplingMode::Synthetic,
            nominal_period_ns: Some(10_000_000),
            max_value: None,
            resolution: None,
            labels: Default::default(),
        }
    }

    #[test]
    fn rejects_duplicate_local_source_ids() {
        let sources = vec![descriptor(1, "a"), descriptor(1, "b")];
        assert!(matches!(
            validate_source_set(&sources),
            Err(Error::DuplicateLocalSourceId(1))
        ));
    }

    #[test]
    fn rejects_duplicate_source_uids() {
        let sources = vec![descriptor(1, "a"), descriptor(2, "a")];
        assert!(matches!(
            validate_source_set(&sources),
            Err(Error::DuplicateSourceUid(_))
        ));
    }

    fn batch(
        run_id: &str,
        node_id: &str,
        batch_sequence_no: u64,
        sequence_no: u64,
    ) -> RawSampleBatch {
        RawSampleBatch {
            run_id: run_id.to_string(),
            node_id: node_id.to_string(),
            batch_sequence_no,
            samples: vec![RawSample {
                source_id: 1,
                sequence_no,
                monotonic_ns: sequence_no * 10,
                value: RawValue::I64(1),
            }],
        }
    }

    #[test]
    fn stream_validator_binds_identity_and_rejects_replay() {
        let sources = vec![descriptor(1, "a")];
        let mut validator = RawStreamValidator::new("run-1", "node-0", &sources).unwrap();
        validator.validate(&batch("run-1", "node-0", 4, 8)).unwrap();

        assert!(matches!(
            validator.validate(&batch("other", "node-0", 5, 9)),
            Err(Error::InvalidBatch(_))
        ));
        assert!(matches!(
            validator.validate(&batch("run-1", "node-0", 4, 9)),
            Err(Error::InvalidBatch(_))
        ));
        assert!(matches!(
            validator.validate(&batch("run-1", "node-0", 5, 8)),
            Err(Error::InvalidSample(_))
        ));
    }

    #[test]
    fn stream_validator_allows_forward_gaps_but_rejects_duplicate_sources() {
        let sources = vec![descriptor(1, "a")];
        let mut validator = RawStreamValidator::new("run-1", "node-0", &sources).unwrap();
        validator.validate(&batch("run-1", "node-0", 1, 1)).unwrap();
        validator.validate(&batch("run-1", "node-0", 3, 3)).unwrap();
        assert_eq!(validator.batches_seen(), 2);
        assert_eq!(validator.samples_seen(), 2);

        let mut duplicate = batch("run-1", "node-0", 4, 4);
        duplicate.samples.push(duplicate.samples[0].clone());
        assert!(matches!(
            validator.validate(&duplicate),
            Err(Error::InvalidBatch(_))
        ));
    }
}
