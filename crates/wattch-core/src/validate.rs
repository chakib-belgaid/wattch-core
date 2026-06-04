use crate::error::{Error, Result};
use crate::model::{RawSampleBatch, SourceDescriptor};
use std::collections::BTreeSet;

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

    for sample in &batch.samples {
        if !known_ids.contains(&sample.source_id) {
            return Err(Error::InvalidSample(format!(
                "sample references unknown source id {}",
                sample.source_id
            )));
        }
    }

    Ok(())
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
}
