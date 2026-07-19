use crate::error::{Error, Result};
use crate::model::{
    EvidenceClass, RawSampleBatch, SourceDescriptor, SourceKind, Temporality, Unit,
    INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1, INTERVAL_ANCHOR_LABEL,
    INTERVAL_DURATION_SOURCE_UID_LABEL, INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP,
    INTERVAL_TIMELINE_RECONSTRUCTION_LABEL,
};
use crate::research::{
    AcceptedControlTrialProvenance, AcceptedTargetTrialProvenance, AggregationRule,
    BoundarySourceSignature, CausalGroundTruthProvenance, CounterfactualEstimateResult,
    CounterfactualImpactEstimate, CounterfactualPredictionPath, CounterfactualTrainingProvenance,
    EnergyEstimand, EpisodeGroupKeyProvenance, EstimateValidationStatus,
    GroupSplitValidationProvenance, HeldOutModelValidationProvenance, MeasurementBoundary,
    ModelInputBindingProvenance, ModelInputRole, QualityDisposition, QualitySeverity,
    SemanticBoundaryRebindProvenance, SplitEpisodeBindingProvenance, StudyAcceptanceEvidence,
    TrialArm, TrialBundleSealProvenance, TrialMetadata, TrialQualityMetadata, DISJOINT_GROUP_LABEL,
    DISJOINT_MEMBER_LABEL,
};
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
    if desc.temporality == Temporality::IntervalAverage {
        let duration_uid = desc
            .labels
            .get(INTERVAL_DURATION_SOURCE_UID_LABEL)
            .map(String::as_str)
            .unwrap_or_default();
        if duration_uid.trim().is_empty() {
            return Err(Error::InvalidSourceDescriptor {
                source_uid: desc.source_uid.clone(),
                reason: format!(
                    "INTERVAL_AVERAGE requires descriptor label {INTERVAL_DURATION_SOURCE_UID_LABEL:?}"
                ),
            });
        }
        if desc.labels.get(INTERVAL_ANCHOR_LABEL).map(String::as_str)
            != Some(INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP)
        {
            return Err(Error::InvalidSourceDescriptor {
                source_uid: desc.source_uid.clone(),
                reason: format!(
                    "INTERVAL_AVERAGE requires {INTERVAL_ANCHOR_LABEL:?}={INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP:?}"
                ),
            });
        }
        if let Some(reconstruction) = desc
            .labels
            .get(INTERVAL_TIMELINE_RECONSTRUCTION_LABEL)
            .map(String::as_str)
        {
            if reconstruction != INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1 {
                return Err(Error::InvalidSourceDescriptor {
                    source_uid: desc.source_uid.clone(),
                    reason: format!(
                        "INTERVAL_AVERAGE uses unsupported {INTERVAL_TIMELINE_RECONSTRUCTION_LABEL:?}={reconstruction:?}"
                    ),
                });
            }
        }
    } else if desc.labels.contains_key(INTERVAL_DURATION_SOURCE_UID_LABEL)
        || desc.labels.contains_key(INTERVAL_ANCHOR_LABEL)
        || desc
            .labels
            .contains_key(INTERVAL_TIMELINE_RECONSTRUCTION_LABEL)
    {
        return Err(Error::InvalidSourceDescriptor {
            source_uid: desc.source_uid.clone(),
            reason: "only INTERVAL_AVERAGE sources may declare interval linkage labels".to_string(),
        });
    }
    if desc.kind == SourceKind::SampleIntervalDuration
        && (desc.unit != Unit::Nanosecond || desc.temporality != Temporality::Delta)
    {
        return Err(Error::InvalidSourceDescriptor {
            source_uid: desc.source_uid.clone(),
            reason: "SAMPLE_INTERVAL_DURATION must use NANOSECOND and DELTA".to_string(),
        });
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

    let by_uid = sources
        .iter()
        .map(|source| (source.source_uid.as_str(), source))
        .collect::<BTreeMap<_, _>>();
    for source in sources
        .iter()
        .filter(|source| source.temporality == Temporality::IntervalAverage)
    {
        let duration_uid = source
            .labels
            .get(INTERVAL_DURATION_SOURCE_UID_LABEL)
            .expect("descriptor validation established the interval duration link");
        let Some(duration) = by_uid.get(duration_uid.as_str()) else {
            return Err(Error::InvalidSourceDescriptor {
                source_uid: source.source_uid.clone(),
                reason: format!(
                    "linked interval duration source {duration_uid:?} is not advertised"
                ),
            });
        };
        if duration.kind != SourceKind::SampleIntervalDuration
            || duration.unit != Unit::Nanosecond
            || duration.temporality != Temporality::Delta
        {
            return Err(Error::InvalidSourceDescriptor {
                source_uid: source.source_uid.clone(),
                reason: format!(
                    "linked source {duration_uid:?} must be SAMPLE_INTERVAL_DURATION/NANOSECOND/DELTA"
                ),
            });
        }
        if duration.node_id != source.node_id
            || duration.backend != source.backend
            || duration.clock_domain != source.clock_domain
            || duration.evidence_class != source.evidence_class
        {
            return Err(Error::InvalidSourceDescriptor {
                source_uid: source.source_uid.clone(),
                reason: format!(
                    "linked source {duration_uid:?} must share node, backend, clock, and evidence class"
                ),
            });
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

/// Validates an offline measurement boundary against the descriptors captured
/// with a raw trace.
///
/// `SumDeclaredDisjoint` requires explicit evidence in each selected source
/// descriptor. Primary sources must share the same `wattch.disjoint_group`
/// label and have unique `wattch.disjoint_member` labels. This is still a
/// backend-provided semantic declaration rather than an independent physical
/// calibration, but it prevents sibling topology alone from authorizing a sum.
pub fn validate_measurement_boundary(
    boundary: &MeasurementBoundary,
    sources: &[SourceDescriptor],
) -> Result<()> {
    validate_source_set(sources)?;

    let invalid = |reason: String| Error::InvalidMeasurementBoundary {
        boundary_id: boundary.boundary_id.clone(),
        reason,
    };

    if boundary.boundary_id.trim().is_empty() {
        return Err(invalid("boundary_id must not be empty".to_string()));
    }
    if boundary.backend.trim().is_empty() {
        return Err(invalid("backend must not be empty".to_string()));
    }
    if boundary.primary_source_uids.is_empty() {
        return Err(invalid(
            "at least one primary source uid is required".to_string(),
        ));
    }
    if boundary.required_sources.is_empty() {
        return Err(invalid(
            "at least one required source signature is required".to_string(),
        ));
    }

    let available = sources
        .iter()
        .map(|source| (source.source_uid.as_str(), source))
        .collect::<BTreeMap<_, _>>();

    let mut primary_uids = BTreeSet::new();
    for source_uid in &boundary.primary_source_uids {
        if source_uid.trim().is_empty() {
            return Err(invalid("primary source uid must not be empty".to_string()));
        }
        if !primary_uids.insert(source_uid.as_str()) {
            return Err(invalid(format!(
                "duplicate primary source uid {source_uid:?}"
            )));
        }
    }

    let mut required = BTreeMap::new();
    for signature in &boundary.required_sources {
        validate_boundary_signature(boundary, signature)?;
        if required
            .insert(signature.source_uid.as_str(), signature)
            .is_some()
        {
            return Err(invalid(format!(
                "duplicate required source signature {:?}",
                signature.source_uid
            )));
        }

        let source = available
            .get(signature.source_uid.as_str())
            .ok_or_else(|| {
                invalid(format!(
                    "required source {:?} is not present in the captured descriptors",
                    signature.source_uid
                ))
            })?;
        if source.backend != boundary.backend {
            return Err(invalid(format!(
                "source {:?} uses backend {:?}, expected {:?}",
                source.source_uid, source.backend, boundary.backend
            )));
        }
        validate_boundary_signature_match(boundary, signature, source)?;
    }

    let mut topology_uids = BTreeSet::new();
    for primary_uid in &boundary.primary_source_uids {
        if !required.contains_key(primary_uid.as_str()) {
            return Err(invalid(format!(
                "primary source {primary_uid:?} has no required signature"
            )));
        }

        let mut current_uid = primary_uid.as_str();
        let mut chain = BTreeSet::new();
        loop {
            if !chain.insert(current_uid) {
                return Err(invalid(format!(
                    "source topology contains a cycle at {current_uid:?}"
                )));
            }
            topology_uids.insert(current_uid);

            let signature = required.get(current_uid).ok_or_else(|| {
                invalid(format!(
                    "required topology omits ancestor {current_uid:?} of primary source {primary_uid:?}"
                ))
            })?;
            match signature.parent_source_uid.as_deref() {
                Some(parent_uid) => current_uid = parent_uid,
                None => break,
            }
        }
    }

    if topology_uids.len() != required.len() {
        let Some(unused) = required.keys().find(|uid| !topology_uids.contains(**uid)) else {
            return Err(invalid(
                "required source topology is internally inconsistent".to_string(),
            ));
        };
        return Err(invalid(format!(
            "required source {unused:?} is not a primary source or one of its ancestors"
        )));
    }

    match boundary.aggregation_rule {
        AggregationRule::SingleSource => {
            if boundary.primary_source_uids.len() != 1 {
                return Err(invalid(
                    "SINGLE_SOURCE requires exactly one primary source".to_string(),
                ));
            }
        }
        AggregationRule::SumDeclaredDisjoint => {
            if boundary.primary_source_uids.len() < 2 {
                return Err(invalid(
                    "SUM_DECLARED_DISJOINT requires at least two primary sources".to_string(),
                ));
            }

            let Some(first) = available.get(boundary.primary_source_uids[0].as_str()) else {
                return Err(invalid(
                    "first primary source is missing from captured descriptors".to_string(),
                ));
            };

            let Some(first_signature) = required.get(boundary.primary_source_uids[0].as_str())
            else {
                return Err(invalid(
                    "first primary source has no required signature".to_string(),
                ));
            };
            let Some(disjoint_group) = first_signature.disjoint_group.as_deref() else {
                return Err(invalid(format!(
                    "primary source {:?} lacks descriptor label {DISJOINT_GROUP_LABEL:?}",
                    first.source_uid
                )));
            };
            let Some(first_member) = first_signature.disjoint_member.as_deref() else {
                return Err(invalid(format!(
                    "primary source {:?} lacks descriptor label {DISJOINT_MEMBER_LABEL:?}",
                    first.source_uid
                )));
            };
            let mut disjoint_members = BTreeSet::from([first_member]);

            for primary_uid in &boundary.primary_source_uids[1..] {
                let Some(source) = available.get(primary_uid.as_str()) else {
                    return Err(invalid(format!(
                        "primary source {primary_uid:?} is missing from captured descriptors"
                    )));
                };
                if source.unit != first.unit
                    || source.temporality != first.temporality
                    || source.clock_domain != first.clock_domain
                {
                    return Err(invalid(format!(
                        "summed source {primary_uid:?} must match unit, temporality, and clock domain of {:?}",
                        first.source_uid
                    )));
                }
                let Some(signature) = required.get(primary_uid.as_str()) else {
                    return Err(invalid(format!(
                        "primary source {primary_uid:?} has no required signature"
                    )));
                };
                if signature.disjoint_group.as_deref() != Some(disjoint_group) {
                    return Err(invalid(format!(
                        "summed source {primary_uid:?} must share descriptor label {DISJOINT_GROUP_LABEL:?}={disjoint_group:?}"
                    )));
                }
                let Some(member) = signature.disjoint_member.as_deref() else {
                    return Err(invalid(format!(
                        "primary source {primary_uid:?} lacks descriptor label {DISJOINT_MEMBER_LABEL:?}"
                    )));
                };
                if !disjoint_members.insert(member) {
                    return Err(invalid(format!(
                        "summed source {primary_uid:?} repeats disjoint member {member:?} in group {disjoint_group:?}"
                    )));
                }
            }

            for (index, left_uid) in boundary.primary_source_uids.iter().enumerate() {
                for right_uid in &boundary.primary_source_uids[index + 1..] {
                    if is_ancestor(left_uid, right_uid, &available)
                        || is_ancestor(right_uid, left_uid, &available)
                    {
                        return Err(invalid(format!(
                            "cannot sum nested primary sources {left_uid:?} and {right_uid:?}"
                        )));
                    }
                }
            }
        }
    }

    Ok(())
}

fn validate_boundary_signature(
    boundary: &MeasurementBoundary,
    signature: &BoundarySourceSignature,
) -> Result<()> {
    let invalid = |reason: String| Error::InvalidMeasurementBoundary {
        boundary_id: boundary.boundary_id.clone(),
        reason,
    };

    if signature.source_uid.trim().is_empty() {
        return Err(invalid("required source uid must not be empty".to_string()));
    }
    if signature.clock_domain.trim().is_empty() {
        return Err(invalid(format!(
            "required source {:?} has an empty clock domain",
            signature.source_uid
        )));
    }
    if let Some(parent_uid) = &signature.parent_source_uid {
        if parent_uid.trim().is_empty() {
            return Err(invalid(format!(
                "required source {:?} has an empty parent uid",
                signature.source_uid
            )));
        }
        if parent_uid == &signature.source_uid {
            return Err(invalid(format!(
                "required source {:?} cannot be its own parent",
                signature.source_uid
            )));
        }
    }
    match (&signature.disjoint_group, &signature.disjoint_member) {
        (Some(group), Some(member)) => {
            if group.trim().is_empty() {
                return Err(invalid(format!(
                    "required source {:?} has an empty disjoint group",
                    signature.source_uid
                )));
            }
            if member.trim().is_empty() {
                return Err(invalid(format!(
                    "required source {:?} has an empty disjoint member",
                    signature.source_uid
                )));
            }
        }
        (None, None) => {}
        _ => {
            return Err(invalid(format!(
                "required source {:?} must provide both disjoint group and member or neither",
                signature.source_uid
            )));
        }
    }
    match (
        &signature.temporality,
        &signature.interval_duration_source_uid,
    ) {
        (Temporality::IntervalAverage, Some(duration_uid)) if !duration_uid.trim().is_empty() => {}
        (Temporality::IntervalAverage, _) => {
            return Err(invalid(format!(
                "required interval-average source {:?} must freeze its duration source uid",
                signature.source_uid
            )));
        }
        (_, Some(_)) => {
            return Err(invalid(format!(
                "non-interval source {:?} cannot declare an interval duration source",
                signature.source_uid
            )));
        }
        (_, None) => {}
    }
    Ok(())
}

fn validate_boundary_signature_match(
    boundary: &MeasurementBoundary,
    signature: &BoundarySourceSignature,
    source: &SourceDescriptor,
) -> Result<()> {
    let mismatch = |field: &str| Error::InvalidMeasurementBoundary {
        boundary_id: boundary.boundary_id.clone(),
        reason: format!(
            "required source {:?} has a different {field} in captured descriptors",
            signature.source_uid
        ),
    };

    if signature.kind != source.kind {
        return Err(mismatch("kind"));
    }
    if signature.unit != source.unit {
        return Err(mismatch("unit"));
    }
    if signature.temporality != source.temporality {
        return Err(mismatch("temporality"));
    }
    if signature.evidence_class != source.evidence_class {
        return Err(mismatch("evidence class"));
    }
    if signature.parent_source_uid != source.parent_source_uid {
        return Err(mismatch("parent source uid"));
    }
    if signature.clock_domain != source.clock_domain {
        return Err(mismatch("clock domain"));
    }
    if signature.disjoint_group.as_deref()
        != source.labels.get(DISJOINT_GROUP_LABEL).map(String::as_str)
    {
        return Err(mismatch("disjoint group"));
    }
    if signature.disjoint_member.as_deref()
        != source.labels.get(DISJOINT_MEMBER_LABEL).map(String::as_str)
    {
        return Err(mismatch("disjoint member"));
    }
    if signature.interval_duration_source_uid.as_deref()
        != source
            .labels
            .get(INTERVAL_DURATION_SOURCE_UID_LABEL)
            .map(String::as_str)
    {
        return Err(mismatch("interval duration source uid"));
    }
    Ok(())
}

fn is_ancestor<'a>(
    candidate_uid: &str,
    descendant_uid: &str,
    available: &BTreeMap<&'a str, &'a SourceDescriptor>,
) -> bool {
    let mut current_uid = descendant_uid;
    let mut seen = BTreeSet::new();
    while seen.insert(current_uid) {
        let Some(source) = available.get(current_uid) else {
            return false;
        };
        let Some(parent_uid) = source.parent_source_uid.as_deref() else {
            return false;
        };
        if parent_uid == candidate_uid {
            return true;
        }
        current_uid = parent_uid;
    }
    false
}

/// Validates a trial record and binds its measurement boundary to the trace's
/// source descriptors.
pub fn validate_trial_metadata(trial: &TrialMetadata, sources: &[SourceDescriptor]) -> Result<()> {
    let invalid = |reason: String| Error::InvalidTrialMetadata {
        trial_id: trial.trial_id.clone(),
        reason,
    };

    for (name, value) in [
        ("experiment_id", trial.experiment_id.as_str()),
        ("trial_id", trial.trial_id.as_str()),
        ("run_id", trial.run_id.as_str()),
        ("matched_block_id", trial.matched_block_id.as_str()),
        (
            "background_workload_id",
            trial.background_workload_id.as_str(),
        ),
        ("input_id", trial.input_id.as_str()),
    ] {
        if value.trim().is_empty() {
            return Err(invalid(format!("{name} must not be empty")));
        }
    }

    if let TrialArm::Program { program_id } = &trial.arm {
        if program_id.trim().is_empty() {
            return Err(invalid("program_id must not be empty".to_string()));
        }
    }
    validate_energy_estimand(&trial.estimand).map_err(invalid)?;
    validate_measurement_boundary(&trial.measurement_boundary, sources)
        .map_err(|error| invalid(format!("measurement boundary validation failed: {error}")))?;
    Ok(())
}

fn validate_energy_estimand(estimand: &EnergyEstimand) -> std::result::Result<(), String> {
    match estimand {
        EnergyEstimand::FixedHorizon { horizon_ns } if *horizon_ns == 0 => {
            Err("fixed horizon must be greater than zero".to_string())
        }
        EnergyEstimand::EqualWork {
            work_amount,
            work_unit,
        } => {
            if !work_amount.is_finite() || *work_amount <= 0.0 {
                return Err("equal-work amount must be finite and greater than zero".to_string());
            }
            if work_unit.trim().is_empty() {
                return Err("equal-work unit must not be empty".to_string());
            }
            Ok(())
        }
        EnergyEstimand::FixedHorizon { .. } => Ok(()),
    }
}

fn require_counterfactual_text(value: &str, field: &str) -> std::result::Result<(), String> {
    if value.trim().is_empty() {
        Err(format!("{field} must not be empty"))
    } else {
        Ok(())
    }
}

fn validate_sha256_digest(value: &str, field: &str) -> std::result::Result<(), String> {
    if value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(())
    } else {
        Err(format!(
            "{field} must contain 64 lowercase hexadecimal characters"
        ))
    }
}

fn counterfactual_numbers_equal(left: f64, right: f64) -> bool {
    let scale = left.abs().max(right.abs()).max(1.0);
    (left - right).abs() <= 1e-12 * scale
}

fn validate_study_acceptance(
    acceptance: &StudyAcceptanceEvidence,
) -> std::result::Result<(), String> {
    if !acceptance.supported
        || !acceptance.fixed_horizon_supported
        || !acceptance.equal_work_supported
        || !acceptance.placebo_supported
        || !acceptance.independent_day_replication_supported
    {
        return Err("causal ground truth must pass every confirmatory acceptance gate".to_string());
    }
    if acceptance.mechanism_required && acceptance.mechanism_supported != Some(true) {
        return Err("causal ground truth requires a supported mechanism result".to_string());
    }
    if acceptance
        .reasons
        .iter()
        .any(|reason| reason.trim().is_empty())
    {
        return Err("causal ground-truth reasons must not contain empty text".to_string());
    }
    if !acceptance.reasons.is_empty() {
        return Err("accepted causal ground truth cannot retain falsification reasons".to_string());
    }
    Ok(())
}

fn validate_causal_ground_truth(
    provenance: &CausalGroundTruthProvenance,
    boundary_id: &str,
) -> std::result::Result<(), String> {
    for (field, value) in [
        ("causal_ground_truth.study_id", provenance.study_id.as_str()),
        ("causal_ground_truth.claim_id", provenance.claim_id.as_str()),
        (
            "causal_ground_truth.measurement_boundary_id",
            provenance.measurement_boundary_id.as_str(),
        ),
    ] {
        require_counterfactual_text(value, field)?;
    }
    validate_sha256_digest(
        &provenance.claims_artifact_sha256,
        "causal_ground_truth.claims_artifact_sha256",
    )?;
    for (field, digest) in [
        (
            "causal_ground_truth.preregistration_sha256",
            provenance.preregistration_sha256.as_deref(),
        ),
        (
            "causal_ground_truth.measurement_boundary_sha256",
            provenance.measurement_boundary_sha256.as_deref(),
        ),
        (
            "causal_ground_truth.source_topology_sha256",
            provenance.source_topology_sha256.as_deref(),
        ),
    ] {
        if let Some(digest) = digest {
            validate_sha256_digest(digest, field)?;
        }
    }
    for (field, digests) in [
        (
            "causal_ground_truth.accepted_calibration_sha256",
            &provenance.accepted_calibration_sha256,
        ),
        (
            "causal_ground_truth.accepted_machine_provenance_sha256",
            &provenance.accepted_machine_provenance_sha256,
        ),
    ] {
        let mut unique = BTreeSet::new();
        for digest in digests {
            validate_sha256_digest(digest, field)?;
            if !unique.insert(digest) {
                return Err(format!("{field} must contain unique digests"));
            }
        }
    }
    if provenance.measurement_boundary_id != boundary_id {
        return Err("causal ground-truth boundary must match the prediction boundary".to_string());
    }
    validate_study_acceptance(&provenance.acceptance)
}

fn validate_trial_bundle_seal(seal: &TrialBundleSealProvenance) -> std::result::Result<(), String> {
    for (field, digest) in [
        (
            "trial seal preregistration_sha256",
            seal.preregistration_sha256.as_str(),
        ),
        (
            "trial seal measurement_boundary_sha256",
            seal.measurement_boundary_sha256.as_str(),
        ),
        (
            "trial seal source_topology_sha256",
            seal.source_topology_sha256.as_str(),
        ),
        (
            "trial seal machine_provenance_sha256",
            seal.machine_provenance_sha256.as_str(),
        ),
        (
            "trial seal measurement_overhead_calibration_sha256",
            seal.measurement_overhead_calibration_sha256.as_str(),
        ),
    ] {
        validate_sha256_digest(digest, field)?;
    }
    Ok(())
}

fn validate_trial_seal_against_ground_truth(
    seal: &TrialBundleSealProvenance,
    ground_truth: &CausalGroundTruthProvenance,
) -> std::result::Result<(), String> {
    validate_trial_bundle_seal(seal)?;
    if ground_truth.preregistration_sha256.as_deref() != Some(seal.preregistration_sha256.as_str())
        || ground_truth.measurement_boundary_sha256.as_deref()
            != Some(seal.measurement_boundary_sha256.as_str())
        || ground_truth.source_topology_sha256.as_deref()
            != Some(seal.source_topology_sha256.as_str())
        || !ground_truth
            .accepted_calibration_sha256
            .contains(&seal.measurement_overhead_calibration_sha256)
        || !ground_truth
            .accepted_machine_provenance_sha256
            .contains(&seal.machine_provenance_sha256)
    {
        return Err(
            "trial calibration, topology, host, or preregistration seal is not accepted by causal ground truth"
                .to_string(),
        );
    }
    Ok(())
}

fn validate_control_provenance(
    provenance: &AcceptedControlTrialProvenance,
    boundary_id: &str,
) -> std::result::Result<(), String> {
    for (field, value) in [
        ("control trial_id", provenance.trial_id.as_str()),
        ("control experiment_id", provenance.experiment_id.as_str()),
        (
            "control matched_block_id",
            provenance.matched_block_id.as_str(),
        ),
        (
            "control measurement_boundary_id",
            provenance.measurement_boundary_id.as_str(),
        ),
    ] {
        require_counterfactual_text(value, field)?;
    }
    validate_sha256_digest(&provenance.checksums_sha256, "control checksums_sha256")?;
    if provenance.measurement_boundary_id != boundary_id {
        return Err("accepted control boundary must match the prediction boundary".to_string());
    }
    if provenance.arm != "C0"
        || provenance.program_id.is_some()
        || provenance.quality_disposition != "ACCEPTED"
        || !provenance.background_only
    {
        return Err(
            "accepted control provenance must describe a background-only, quality-accepted C0 trial"
                .to_string(),
        );
    }
    Ok(())
}

fn validate_model_input_binding(
    binding: &ModelInputBindingProvenance,
    expected_role: ModelInputRole,
) -> std::result::Result<(), String> {
    for (field, value) in [
        ("model input trial_id", binding.trial_id.as_str()),
        (
            "model input power_derivation",
            binding.power_derivation.as_str(),
        ),
        (
            "model input feature_alignment",
            binding.feature_alignment.as_str(),
        ),
    ] {
        require_counterfactual_text(value, field)?;
    }
    for (field, digest) in [
        (
            "model input trial_checksums_sha256",
            binding.trial_checksums_sha256.as_str(),
        ),
        (
            "model input feature_artifact_sha256",
            binding.feature_artifact_sha256.as_str(),
        ),
        (
            "model input payload_sha256",
            binding.payload_sha256.as_str(),
        ),
    ] {
        validate_sha256_digest(digest, field)?;
    }
    if binding.role != expected_role {
        return Err("model input binding has the wrong trial role".to_string());
    }
    Ok(())
}

fn validate_episode_group_key(key: &EpisodeGroupKeyProvenance) -> std::result::Result<(), String> {
    for (field, value) in [
        ("split run_id", key.run_id.as_str()),
        ("split day", key.day.as_str()),
        ("split workload_family", key.workload_family.as_str()),
    ] {
        require_counterfactual_text(value, field)?;
    }
    validate_sha256_digest(&key.machine, "split machine digest")
}

fn validate_split_episode_binding(
    binding: &SplitEpisodeBindingProvenance,
) -> std::result::Result<(), String> {
    require_counterfactual_text(&binding.episode_id, "split episode_id")?;
    validate_sha256_digest(
        &binding.trial_checksums_sha256,
        "split trial_checksums_sha256",
    )?;
    validate_sha256_digest(
        &binding.model_input_payload_sha256,
        "split model_input_payload_sha256",
    )?;
    if binding.row_count == 0 {
        return Err("split episode row_count must be positive".to_string());
    }
    validate_episode_group_key(&binding.group_key)
}

fn validate_semantic_rebind_provenance(
    provenance: &SemanticBoundaryRebindProvenance,
    boundary: &MeasurementBoundary,
) -> std::result::Result<(), String> {
    for (field, value) in [
        (
            "semantic_rebind.source_boundary_id",
            provenance.source_boundary_id.as_str(),
        ),
        (
            "semantic_rebind.target_boundary_id",
            provenance.target_boundary_id.as_str(),
        ),
        ("semantic_rebind.backend", provenance.backend.as_str()),
    ] {
        require_counterfactual_text(value, field)?;
    }
    if provenance.target_boundary_id != boundary.boundary_id
        || provenance.backend != boundary.backend
        || provenance.aggregation_rule != boundary.aggregation_rule
    {
        return Err(
            "semantic rebind target semantics must match the prediction boundary".to_string(),
        );
    }
    if provenance.raw_joule_pooling_permitted {
        return Err("semantic rebind cannot permit raw-joule pooling".to_string());
    }
    if provenance.source_to_target_uids.is_empty() {
        return Err("semantic rebind must map at least one source uid".to_string());
    }
    let mut source_uids = BTreeSet::new();
    let mut target_uids = BTreeSet::new();
    for mapping in &provenance.source_to_target_uids {
        require_counterfactual_text(&mapping.source_uid, "semantic rebind source_uid")?;
        require_counterfactual_text(&mapping.target_uid, "semantic rebind target_uid")?;
        if !source_uids.insert(mapping.source_uid.as_str())
            || !target_uids.insert(mapping.target_uid.as_str())
        {
            return Err("semantic rebind source and target uids must be unique".to_string());
        }
    }
    let required_target_uids = boundary
        .required_sources
        .iter()
        .map(|source| source.source_uid.as_str())
        .collect::<BTreeSet<_>>();
    if target_uids != required_target_uids {
        return Err(
            "semantic rebind must map every source required by the prediction boundary".to_string(),
        );
    }
    Ok(())
}

fn validate_group_split_provenance(
    provenance: &GroupSplitValidationProvenance,
) -> std::result::Result<(), String> {
    if provenance.train_rows == 0
        || provenance.test_rows == 0
        || provenance.train_groups == 0
        || provenance.test_groups == 0
    {
        return Err("group-split row and group counts must be positive".to_string());
    }
    if provenance.train_groups > provenance.train_rows
        || provenance.test_groups > provenance.test_rows
    {
        return Err("group-split group counts cannot exceed row counts".to_string());
    }
    if provenance.disjoint_axes.is_empty() {
        return Err("group-split validation needs at least one disjoint axis".to_string());
    }
    let mut axes = BTreeSet::new();
    for axis in &provenance.disjoint_axes {
        require_counterfactual_text(axis, "group-split disjoint axis")?;
        if !axes.insert(axis.as_str()) {
            return Err("group-split disjoint axes must be unique".to_string());
        }
    }
    if provenance.train_episodes.is_empty() && provenance.test_episodes.is_empty() {
        return Ok(());
    }
    if provenance.train_episodes.is_empty() || provenance.test_episodes.is_empty() {
        return Err(
            "replay-bound group split needs both train and test episode bindings".to_string(),
        );
    }
    validate_sha256_digest(
        &provenance.split_artifact_sha256,
        "group-split artifact sha256",
    )?;
    let mut episode_ids = BTreeSet::new();
    let mut train_groups = BTreeSet::new();
    let mut test_groups = BTreeSet::new();
    for binding in &provenance.train_episodes {
        validate_split_episode_binding(binding)?;
        if !episode_ids.insert(binding.episode_id.as_str()) {
            return Err("split episode ids must be unique across partitions".to_string());
        }
        train_groups.insert(&binding.group_key);
    }
    for binding in &provenance.test_episodes {
        validate_split_episode_binding(binding)?;
        if !episode_ids.insert(binding.episode_id.as_str()) {
            return Err("split episode ids must be unique across partitions".to_string());
        }
        test_groups.insert(&binding.group_key);
    }
    let train_rows: u64 = provenance
        .train_episodes
        .iter()
        .map(|binding| binding.row_count)
        .sum();
    let test_rows: u64 = provenance
        .test_episodes
        .iter()
        .map(|binding| binding.row_count)
        .sum();
    if train_rows != provenance.train_rows
        || test_rows != provenance.test_rows
        || train_groups.len() as u64 != provenance.train_groups
        || test_groups.len() as u64 != provenance.test_groups
    {
        return Err("group-split counts must match replay-bound episode bindings".to_string());
    }
    let required_axes = match provenance.strategy {
        crate::research::GroupSplitStrategy::PerHost => {
            ["run_id", "replay_seed", "day", "workload_family"].as_slice()
        }
        crate::research::GroupSplitStrategy::CrossMachineTransfer => {
            ["run_id", "replay_seed", "day", "workload_family", "machine"].as_slice()
        }
    };
    if axes != required_axes.iter().copied().collect::<BTreeSet<_>>() {
        return Err("group-split disjoint axes do not match its strategy".to_string());
    }
    let overlaps = required_axes.iter().any(|axis| match *axis {
        "run_id" => train_groups
            .iter()
            .any(|train| test_groups.iter().any(|test| train.run_id == test.run_id)),
        "replay_seed" => train_groups.iter().any(|train| {
            test_groups
                .iter()
                .any(|test| train.replay_seed == test.replay_seed)
        }),
        "day" => train_groups
            .iter()
            .any(|train| test_groups.iter().any(|test| train.day == test.day)),
        "workload_family" => train_groups.iter().any(|train| {
            test_groups
                .iter()
                .any(|test| train.workload_family == test.workload_family)
        }),
        "machine" => train_groups
            .iter()
            .any(|train| test_groups.iter().any(|test| train.machine == test.machine)),
        _ => true,
    });
    if overlaps {
        return Err("group-split episode bindings overlap on a required axis".to_string());
    }
    if provenance.strategy == crate::research::GroupSplitStrategy::PerHost {
        let train_machines = train_groups
            .iter()
            .map(|group| group.machine.as_str())
            .collect::<BTreeSet<_>>();
        let test_machines = test_groups
            .iter()
            .map(|group| group.machine.as_str())
            .collect::<BTreeSet<_>>();
        if train_machines.len() != 1 || train_machines != test_machines {
            return Err("per-host group split requires one shared machine".to_string());
        }
    }
    Ok(())
}

fn validate_held_out_model_validation(
    validation: &HeldOutModelValidationProvenance,
    split: &GroupSplitValidationProvenance,
    training_data_id: &str,
) -> std::result::Result<(), String> {
    for (field, digest) in [
        (
            "held-out split_artifact_sha256",
            validation.split_artifact_sha256.as_str(),
        ),
        (
            "held-out fitted_parameters_sha256",
            validation.fitted_parameters_sha256.as_str(),
        ),
        (
            "held-out observed_paths_sha256",
            validation.observed_paths_sha256.as_str(),
        ),
        (
            "held-out predicted_paths_sha256",
            validation.predicted_paths_sha256.as_str(),
        ),
        (
            "held-out artifact_sha256",
            validation.artifact_sha256.as_str(),
        ),
    ] {
        validate_sha256_digest(digest, field)?;
    }
    require_counterfactual_text(&validation.training_data_id, "held-out training_data_id")?;
    if validation.training_data_id != training_data_id
        || validation.split_artifact_sha256 != split.split_artifact_sha256
    {
        return Err(
            "held-out validation must bind the fitted model and split artifact".to_string(),
        );
    }
    if validation.test_rows == 0
        || validation.test_rows != split.test_rows
        || validation.out_of_distribution_rows > validation.test_rows
    {
        return Err("held-out validation row counts do not match the test partition".to_string());
    }
    for value in [
        validation.root_mean_squared_error_watts,
        validation.mean_absolute_error_watts,
        validation.mean_error_watts,
    ] {
        if !value.is_finite() {
            return Err("held-out validation metrics must be finite".to_string());
        }
    }
    if validation.root_mean_squared_error_watts < 0.0 || validation.mean_absolute_error_watts < 0.0
    {
        return Err("held-out RMSE and MAE must be non-negative".to_string());
    }
    let declared_ids = validation
        .test_episode_ids
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    if declared_ids.len() != validation.test_episode_ids.len()
        || declared_ids
            .iter()
            .any(|episode_id| episode_id.trim().is_empty())
    {
        return Err("held-out test episode ids must be unique and non-empty".to_string());
    }
    let split_ids = split
        .test_episodes
        .iter()
        .map(|binding| binding.episode_id.as_str())
        .collect::<BTreeSet<_>>();
    if declared_ids != split_ids {
        return Err("held-out validation must identify the exact test episodes".to_string());
    }
    Ok(())
}

fn validate_training_provenance(
    provenance: &CounterfactualTrainingProvenance,
    boundary: &MeasurementBoundary,
    training_data_id: &str,
) -> std::result::Result<(), String> {
    validate_causal_ground_truth(&provenance.causal_ground_truth, &boundary.boundary_id)?;
    if provenance.control_episode_ids.is_empty() {
        return Err("training provenance needs at least one control episode id".to_string());
    }
    let mut episode_ids = BTreeSet::new();
    for episode_id in &provenance.control_episode_ids {
        require_counterfactual_text(episode_id, "control episode id")?;
        if !episode_ids.insert(episode_id.as_str()) {
            return Err("control episode ids must be unique".to_string());
        }
    }
    if provenance.accepted_control_trials.len() != episode_ids.len() {
        return Err(
            "every control episode must have one accepted trial provenance record".to_string(),
        );
    }
    let mut trial_ids = BTreeSet::new();
    for control in &provenance.accepted_control_trials {
        validate_control_provenance(control, &boundary.boundary_id)?;
        if let Some(seal) = &control.trial_bundle_seal {
            validate_trial_seal_against_ground_truth(seal, &provenance.causal_ground_truth)?;
        }
        if !trial_ids.insert(control.trial_id.as_str()) {
            return Err("accepted control trial ids must be unique".to_string());
        }
    }
    if episode_ids != trial_ids {
        return Err("control episode ids must identify the accepted control trials".to_string());
    }
    let control_checksums = provenance
        .accepted_control_trials
        .iter()
        .map(|control| (control.trial_id.as_str(), control.checksums_sha256.as_str()))
        .collect::<BTreeMap<_, _>>();
    let mut model_input_ids = BTreeSet::new();
    for binding in &provenance.control_model_inputs {
        validate_model_input_binding(binding, ModelInputRole::ControlEpisode)?;
        if !model_input_ids.insert(binding.trial_id.as_str()) {
            return Err("control model input trial ids must be unique".to_string());
        }
        if control_checksums.get(binding.trial_id.as_str()).copied()
            != Some(binding.trial_checksums_sha256.as_str())
        {
            return Err(
                "control model inputs must match an accepted trial and checksum seal".to_string(),
            );
        }
    }
    if let Some(rebind) = &provenance.semantic_rebind {
        validate_semantic_rebind_provenance(rebind, boundary)?;
    }
    if let Some(group_split) = &provenance.group_split_validation {
        validate_group_split_provenance(group_split)?;
        if !group_split.train_episodes.is_empty() {
            let split_train_ids = group_split
                .train_episodes
                .iter()
                .map(|binding| binding.episode_id.as_str())
                .collect::<BTreeSet<_>>();
            if split_train_ids != episode_ids {
                return Err(
                    "training controls must be exactly the replay-bound TRAIN partition"
                        .to_string(),
                );
            }
            let split_by_id = group_split
                .train_episodes
                .iter()
                .map(|binding| (binding.episode_id.as_str(), binding))
                .collect::<BTreeMap<_, _>>();
            for binding in &provenance.control_model_inputs {
                let Some(split_binding) = split_by_id.get(binding.trial_id.as_str()) else {
                    return Err("control model input is absent from the TRAIN split".to_string());
                };
                if split_binding.trial_checksums_sha256 != binding.trial_checksums_sha256
                    || split_binding.model_input_payload_sha256 != binding.payload_sha256
                {
                    return Err(
                        "TRAIN split seals must match control trial and model-input bindings"
                            .to_string(),
                    );
                }
            }
        }
        if let Some(held_out) = &provenance.held_out_model_validation {
            validate_held_out_model_validation(held_out, group_split, training_data_id)?;
        }
    } else if provenance.held_out_model_validation.is_some() {
        return Err("held-out validation requires a group split artifact".to_string());
    }
    Ok(())
}

fn validate_target_provenance(
    provenance: &AcceptedTargetTrialProvenance,
    estimate: &CounterfactualImpactEstimate,
) -> std::result::Result<(), String> {
    for (field, value) in [
        ("target trial_id", provenance.trial_id.as_str()),
        ("target experiment_id", provenance.experiment_id.as_str()),
        (
            "target matched_block_id",
            provenance.matched_block_id.as_str(),
        ),
        (
            "target measurement_boundary_id",
            provenance.measurement_boundary_id.as_str(),
        ),
        ("target arm", provenance.arm.as_str()),
        ("target program_id", provenance.program_id.as_str()),
    ] {
        require_counterfactual_text(value, field)?;
    }
    validate_sha256_digest(&provenance.checksums_sha256, "target checksums_sha256")?;
    if provenance.trial_id != estimate.trial_id {
        return Err("accepted target provenance must identify estimate.trial_id".to_string());
    }
    if provenance.measurement_boundary_id != estimate.measurement_boundary.boundary_id {
        return Err("accepted target boundary must match the prediction boundary".to_string());
    }
    if !matches!(provenance.arm.as_str(), "C1" | "C2")
        || provenance.quality_disposition != "ACCEPTED"
    {
        return Err(
            "accepted target provenance must describe a quality-accepted C1 or C2 trial"
                .to_string(),
        );
    }
    validate_energy_estimand(&provenance.estimand)?;
    if provenance.estimand != estimate.estimand {
        return Err("accepted target estimand must match the prediction estimand".to_string());
    }
    if !provenance.observed_joules.is_finite() || provenance.observed_joules < 0.0 {
        return Err("target observed_joules must be finite and non-negative".to_string());
    }
    Ok(())
}

fn validate_target_model_input(
    binding: &ModelInputBindingProvenance,
    target: &AcceptedTargetTrialProvenance,
) -> std::result::Result<(), String> {
    validate_model_input_binding(binding, ModelInputRole::TargetRequest)?;
    if binding.trial_id != target.trial_id
        || binding.trial_checksums_sha256 != target.checksums_sha256
    {
        return Err(
            "target model input must match the accepted target trial and checksum seal".to_string(),
        );
    }
    Ok(())
}

fn validate_prediction_timestamps(
    path: &CounterfactualPredictionPath,
    estimand: &EnergyEstimand,
) -> std::result::Result<(), String> {
    if path.timestamps_ns.len() < 2 {
        return Err("prediction path needs at least two timestamps".to_string());
    }
    if path.timestamps_ns.windows(2).any(|pair| pair[1] <= pair[0]) {
        return Err("prediction path timestamps must strictly increase".to_string());
    }
    if let EnergyEstimand::FixedHorizon { horizon_ns } = estimand {
        let duration_ns = path.timestamps_ns[path.timestamps_ns.len() - 1] - path.timestamps_ns[0];
        if duration_ns != *horizon_ns {
            return Err(
                "prediction path duration must equal the declared fixed horizon".to_string(),
            );
        }
    }
    Ok(())
}

fn trapezoidal_integral_joules(timestamps_ns: &[u64], power_watts: &[f64]) -> f64 {
    timestamps_ns
        .windows(2)
        .zip(power_watts.windows(2))
        .map(|(timestamps, power)| {
            let seconds = (timestamps[1] - timestamps[0]) as f64 / 1_000_000_000.0;
            (power[0] + power[1]) * 0.5 * seconds
        })
        .sum()
}

fn validate_prediction_series(
    path: &CounterfactualPredictionPath,
) -> std::result::Result<(), String> {
    let expected_len = path.timestamps_ns.len();
    if path.predicted_power_watts.len() != expected_len
        || path.lower_power_watts.len() != expected_len
        || path.upper_power_watts.len() != expected_len
    {
        return Err("prediction power paths must align with timestamps".to_string());
    }
    for (index, ((predicted, lower), upper)) in path
        .predicted_power_watts
        .iter()
        .zip(&path.lower_power_watts)
        .zip(&path.upper_power_watts)
        .enumerate()
    {
        if !predicted.is_finite()
            || !lower.is_finite()
            || !upper.is_finite()
            || *lower < 0.0
            || *predicted < *lower
            || *predicted > *upper
        {
            return Err(format!(
                "prediction power interval at index {index} must be finite, non-negative, and ordered"
            ));
        }
    }
    Ok(())
}

fn validated_provenance_is_verified(path: &CounterfactualPredictionPath) -> bool {
    path.training_provenance
        .causal_ground_truth
        .artifact_verified
        && path
            .training_provenance
            .causal_ground_truth
            .preregistration_sha256
            .is_some()
        && path
            .training_provenance
            .causal_ground_truth
            .measurement_boundary_sha256
            .is_some()
        && path
            .training_provenance
            .causal_ground_truth
            .source_topology_sha256
            .is_some()
        && !path
            .training_provenance
            .causal_ground_truth
            .accepted_calibration_sha256
            .is_empty()
        && !path
            .training_provenance
            .causal_ground_truth
            .accepted_machine_provenance_sha256
            .is_empty()
        && path
            .training_provenance
            .accepted_control_trials
            .iter()
            .all(|control| {
                control.artifact_verified
                    && control
                        .trial_bundle_seal
                        .as_ref()
                        .is_some_and(|seal| seal.artifact_verified)
            })
        && path.training_provenance.control_model_inputs.len()
            == path.training_provenance.accepted_control_trials.len()
        && path
            .training_provenance
            .control_model_inputs
            .iter()
            .all(|binding| binding.artifact_verified)
        && path
            .training_provenance
            .group_split_validation
            .as_ref()
            .is_some_and(|split| {
                split.artifact_verified
                    && !split.train_episodes.is_empty()
                    && !split.test_episodes.is_empty()
            })
        && path
            .training_provenance
            .held_out_model_validation
            .as_ref()
            .is_some_and(|validation| validation.artifact_verified)
        && path.accepted_target_trial.artifact_verified
        && path
            .accepted_target_trial
            .trial_bundle_seal
            .as_ref()
            .is_some_and(|seal| seal.artifact_verified)
        && path
            .target_model_input
            .as_ref()
            .is_some_and(|binding| binding.artifact_verified)
}

/// Validates structural consistency and portable numeric values in a trial's
/// offline quality report. Acceptance thresholds remain experiment policy.
pub fn validate_trial_quality(quality: &TrialQualityMetadata) -> Result<()> {
    let invalid = |reason: String| Error::InvalidTrialQuality {
        trial_id: quality.trial_id.clone(),
        reason,
    };

    if quality.trial_id.trim().is_empty() {
        return Err(invalid("trial_id must not be empty".to_string()));
    }
    if let Some(overhead) = quality.measurement_overhead_ratio {
        if !overhead.is_finite() || overhead < 0.0 {
            return Err(invalid(
                "measurement_overhead_ratio must be finite and non-negative".to_string(),
            ));
        }
    }

    let mut source_uids = BTreeSet::new();
    for source in &quality.sources {
        if source.source_uid.trim().is_empty() {
            return Err(invalid("source_uid must not be empty".to_string()));
        }
        if !source_uids.insert(source.source_uid.as_str()) {
            return Err(invalid(format!(
                "duplicate source quality summary {:?}",
                source.source_uid
            )));
        }
        if !source.missing_sample_ratio.is_finite()
            || !(0.0..=1.0).contains(&source.missing_sample_ratio)
        {
            return Err(invalid(format!(
                "source {:?} missing_sample_ratio must be between zero and one",
                source.source_uid
            )));
        }
    }

    for issue in &quality.issues {
        if issue.code.trim().is_empty() {
            return Err(invalid("quality issue code must not be empty".to_string()));
        }
        if issue.message.trim().is_empty() {
            return Err(invalid(
                "quality issue message must not be empty".to_string(),
            ));
        }
        if issue
            .source_uid
            .as_ref()
            .is_some_and(|source_uid| source_uid.trim().is_empty())
        {
            return Err(invalid(
                "quality issue source_uid must not be empty when present".to_string(),
            ));
        }
    }

    let has_error = quality
        .issues
        .iter()
        .any(|issue| issue.severity == QualitySeverity::Error);
    match quality.disposition {
        QualityDisposition::Accepted if has_error => Err(invalid(
            "ACCEPTED quality metadata cannot contain an ERROR issue".to_string(),
        )),
        QualityDisposition::ReviewRequired if quality.issues.is_empty() => Err(invalid(
            "REVIEW_REQUIRED quality metadata must include an issue".to_string(),
        )),
        QualityDisposition::Rejected if !has_error => Err(invalid(
            "REJECTED quality metadata must include an ERROR issue".to_string(),
        )),
        _ => Ok(()),
    }
}

/// Validates metadata, portable numeric values, and the canonical impact
/// arithmetic for a counterfactual model output.
///
/// The impact is required to equal `observed_joules - counterfactual_joules`
/// within a narrow serialization tolerance. This prevents a producer from
/// attaching an internally consistent interval to a tampered impact value.
pub fn validate_counterfactual_impact_estimate(
    estimate: &CounterfactualImpactEstimate,
    sources: &[SourceDescriptor],
) -> Result<()> {
    let invalid = |reason: String| Error::InvalidCounterfactualImpactEstimate {
        estimate_id: estimate.estimate_id.clone(),
        reason,
    };

    for (name, value) in [
        ("estimate_id", estimate.estimate_id.as_str()),
        ("trial_id", estimate.trial_id.as_str()),
        ("model_id", estimate.model.model_id.as_str()),
        ("model_version", estimate.model.model_version.as_str()),
        ("training_data_id", estimate.model.training_data_id.as_str()),
    ] {
        if value.trim().is_empty() {
            return Err(invalid(format!("{name} must not be empty")));
        }
    }
    if estimate.evidence_class != EvidenceClass::ModelDerivedEstimate {
        return Err(invalid(
            "evidence_class must be MODEL_DERIVED_ESTIMATE".to_string(),
        ));
    }

    validate_energy_estimand(&estimate.estimand).map_err(invalid)?;
    validate_measurement_boundary(&estimate.measurement_boundary, sources)
        .map_err(|error| invalid(format!("measurement boundary validation failed: {error}")))?;
    validate_prediction_timestamps(&estimate.prediction_path, &estimate.estimand)
        .map_err(&invalid)?;
    validate_training_provenance(
        &estimate.prediction_path.training_provenance,
        &estimate.measurement_boundary,
        &estimate.model.training_data_id,
    )
    .map_err(&invalid)?;
    validate_target_provenance(&estimate.prediction_path.accepted_target_trial, estimate)
        .map_err(&invalid)?;
    if let Some(binding) = &estimate.prediction_path.target_model_input {
        validate_target_model_input(binding, &estimate.prediction_path.accepted_target_trial)
            .map_err(&invalid)?;
    }
    if let Some(seal) = &estimate
        .prediction_path
        .accepted_target_trial
        .trial_bundle_seal
    {
        validate_trial_seal_against_ground_truth(
            seal,
            &estimate
                .prediction_path
                .training_provenance
                .causal_ground_truth,
        )
        .map_err(&invalid)?;
    }

    match &estimate.result {
        CounterfactualEstimateResult::Estimated {
            observed_joules,
            counterfactual_joules,
            impact_joules,
            interval,
            validation_status,
        } => {
            if !observed_joules.is_finite() || *observed_joules < 0.0 {
                return Err(invalid(
                    "observed_joules must be finite and non-negative".to_string(),
                ));
            }
            if !counterfactual_joules.is_finite() || *counterfactual_joules < 0.0 {
                return Err(invalid(
                    "counterfactual_joules must be finite and non-negative".to_string(),
                ));
            }
            if !impact_joules.is_finite() {
                return Err(invalid("impact_joules must be finite".to_string()));
            }

            let expected_impact_joules = observed_joules - counterfactual_joules;
            let arithmetic_scale = expected_impact_joules
                .abs()
                .max(impact_joules.abs())
                .max(1.0);
            let arithmetic_tolerance = 1e-12 * arithmetic_scale;
            if (*impact_joules - expected_impact_joules).abs() > arithmetic_tolerance {
                return Err(invalid(format!(
                    "impact_joules must equal observed_joules - counterfactual_joules \
                     (expected {expected_impact_joules}, got {impact_joules})"
                )));
            }
            if !interval.confidence_level.is_finite()
                || interval.confidence_level <= 0.0
                || interval.confidence_level >= 1.0
            {
                return Err(invalid(
                    "confidence_level must be finite and strictly between zero and one".to_string(),
                ));
            }
            if !interval.lower_joules.is_finite() || !interval.upper_joules.is_finite() {
                return Err(invalid("interval bounds must be finite".to_string()));
            }
            if interval.lower_joules > interval.upper_joules {
                return Err(invalid(
                    "interval lower bound must not exceed upper bound".to_string(),
                ));
            }
            if *impact_joules < interval.lower_joules || *impact_joules > interval.upper_joules {
                return Err(invalid(
                    "impact_joules must lie inside its uncertainty interval".to_string(),
                ));
            }

            if !counterfactual_numbers_equal(
                *observed_joules,
                estimate
                    .prediction_path
                    .accepted_target_trial
                    .observed_joules,
            ) {
                return Err(invalid(
                    "result observed_joules must match the accepted target trial".to_string(),
                ));
            }

            validate_prediction_series(&estimate.prediction_path).map_err(&invalid)?;
            let integrated = estimate
                .prediction_path
                .integrated_counterfactual
                .as_ref()
                .ok_or_else(|| {
                    invalid("numeric prediction needs an integrated counterfactual".to_string())
                })?;
            let predicted_integral = trapezoidal_integral_joules(
                &estimate.prediction_path.timestamps_ns,
                &estimate.prediction_path.predicted_power_watts,
            );
            let lower_integral = trapezoidal_integral_joules(
                &estimate.prediction_path.timestamps_ns,
                &estimate.prediction_path.lower_power_watts,
            );
            let upper_integral = trapezoidal_integral_joules(
                &estimate.prediction_path.timestamps_ns,
                &estimate.prediction_path.upper_power_watts,
            );
            if !counterfactual_numbers_equal(integrated.joules, predicted_integral)
                || !counterfactual_numbers_equal(*counterfactual_joules, predicted_integral)
            {
                return Err(invalid(
                    "counterfactual_joules must equal the trapezoidal prediction-path integral"
                        .to_string(),
                ));
            }
            if !integrated.interval.confidence_level.is_finite()
                || integrated.interval.confidence_level <= 0.0
                || integrated.interval.confidence_level >= 1.0
            {
                return Err(invalid(
                    "integrated confidence_level must be finite and strictly between zero and one"
                        .to_string(),
                ));
            }
            if !counterfactual_numbers_equal(
                integrated.interval.confidence_level,
                interval.confidence_level,
            ) || !counterfactual_numbers_equal(integrated.interval.lower_joules, lower_integral)
                || !counterfactual_numbers_equal(integrated.interval.upper_joules, upper_integral)
            {
                return Err(invalid(
                    "integrated counterfactual interval must equal the trapezoidal lower/upper path integrals"
                        .to_string(),
                ));
            }
            if integrated.interval.lower_joules > integrated.joules
                || integrated.joules > integrated.interval.upper_joules
            {
                return Err(invalid(
                    "integrated counterfactual must lie inside its interval".to_string(),
                ));
            }
            let expected_impact_lower = observed_joules - integrated.interval.upper_joules;
            let expected_impact_upper = observed_joules - integrated.interval.lower_joules;
            if !counterfactual_numbers_equal(interval.lower_joules, expected_impact_lower)
                || !counterfactual_numbers_equal(interval.upper_joules, expected_impact_upper)
            {
                return Err(invalid(
                    "impact interval must equal observed energy minus the reversed counterfactual interval"
                        .to_string(),
                ));
            }
            if *validation_status == EstimateValidationStatus::Validated
                && !validated_provenance_is_verified(&estimate.prediction_path)
            {
                return Err(invalid(
                    "VALIDATED estimates require verified causal, control, model-input, group-split, and target artifacts"
                        .to_string(),
                ));
            }
        }
        CounterfactualEstimateResult::AbstainedOutOfDistribution { reason } => {
            if reason.trim().is_empty() {
                return Err(invalid(
                    "out-of-distribution abstention reason must not be empty".to_string(),
                ));
            }
            if !estimate.prediction_path.predicted_power_watts.is_empty()
                || !estimate.prediction_path.lower_power_watts.is_empty()
                || !estimate.prediction_path.upper_power_watts.is_empty()
                || estimate.prediction_path.integrated_counterfactual.is_some()
            {
                return Err(invalid(
                    "out-of-distribution abstention cannot contain a numeric prediction path"
                        .to_string(),
                ));
            }
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
    use crate::research::*;

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

    fn source(
        id: u32,
        uid: &str,
        kind: SourceKind,
        unit: Unit,
        temporality: Temporality,
        evidence_class: EvidenceClass,
        parent_source_uid: Option<&str>,
    ) -> SourceDescriptor {
        SourceDescriptor {
            local_source_id: id,
            source_uid: uid.to_string(),
            node_id: "node-0".to_string(),
            backend: "power-meter".to_string(),
            kind,
            unit,
            temporality,
            evidence_class,
            source_path: format!("meter/{uid}"),
            parent_source_uid: parent_source_uid.map(str::to_string),
            clock_domain: "monotonic".to_string(),
            sampling_mode: SamplingMode::Polled,
            nominal_period_ns: Some(10_000_000),
            max_value: None,
            resolution: None,
            labels: Default::default(),
        }
    }

    fn signature(source: &SourceDescriptor) -> BoundarySourceSignature {
        BoundarySourceSignature::from_descriptor(source)
    }

    fn declare_disjoint(
        mut source: SourceDescriptor,
        group: &str,
        member: &str,
    ) -> SourceDescriptor {
        source
            .labels
            .insert(DISJOINT_GROUP_LABEL.to_string(), group.to_string());
        source
            .labels
            .insert(DISJOINT_MEMBER_LABEL.to_string(), member.to_string());
        source
    }

    #[test]
    fn interval_average_requires_linked_raw_duration_evidence() {
        let duration_uid = "powermetrics://node-0/frame/elapsed_ns";
        let mut power = source(
            1,
            "powermetrics://node-0/cpu/power",
            SourceKind::CpuPower,
            Unit::Milliwatt,
            Temporality::IntervalAverage,
            EvidenceClass::ToolDerivedEstimate,
            None,
        );
        power.labels.insert(
            INTERVAL_DURATION_SOURCE_UID_LABEL.to_string(),
            duration_uid.to_string(),
        );
        power.labels.insert(
            INTERVAL_ANCHOR_LABEL.to_string(),
            INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP.to_string(),
        );
        let duration = source(
            2,
            duration_uid,
            SourceKind::SampleIntervalDuration,
            Unit::Nanosecond,
            Temporality::Delta,
            EvidenceClass::ToolDerivedEstimate,
            None,
        );

        validate_source_set(&[power.clone(), duration.clone()]).unwrap();
        let signature = BoundarySourceSignature::from_descriptor(&power);
        assert_eq!(
            signature.interval_duration_source_uid.as_deref(),
            Some(duration_uid)
        );
        assert_eq!(
            serde_json::to_value(&signature).unwrap()["interval_duration_source_uid"],
            duration_uid
        );

        let error = validate_source_set(&[power.clone()]).unwrap_err();
        assert!(error.to_string().contains("is not advertised"));

        let mut wrong_duration = duration;
        wrong_duration.unit = Unit::Counter;
        let error = validate_source_set(&[power, wrong_duration]).unwrap_err();
        assert!(error
            .to_string()
            .contains("SAMPLE_INTERVAL_DURATION must use NANOSECOND and DELTA"));
    }

    fn single_boundary(source: &SourceDescriptor) -> MeasurementBoundary {
        MeasurementBoundary {
            boundary_id: "cpu-package".to_string(),
            backend: source.backend.clone(),
            primary_source_uids: vec![source.source_uid.clone()],
            aggregation_rule: AggregationRule::SingleSource,
            required_sources: vec![signature(source)],
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

    #[test]
    fn measurement_boundary_preserves_raw_and_tool_derived_evidence() {
        let raw = source(
            1,
            "rapl-package-0",
            SourceKind::CpuPackageEnergy,
            Unit::Joule,
            Temporality::Cumulative,
            EvidenceClass::RawHardwareCounter,
            None,
        );
        let tool = source(
            2,
            "powermetrics-cpu",
            SourceKind::CpuPower,
            Unit::Milliwatt,
            Temporality::Instant,
            EvidenceClass::ToolDerivedEstimate,
            None,
        );

        validate_measurement_boundary(&single_boundary(&raw), std::slice::from_ref(&raw)).unwrap();
        validate_measurement_boundary(&single_boundary(&tool), std::slice::from_ref(&tool))
            .unwrap();

        let mut mislabeled = single_boundary(&tool);
        mislabeled.required_sources[0].evidence_class = EvidenceClass::RawSensorReading;
        assert!(matches!(
            validate_measurement_boundary(&mislabeled, &[tool]),
            Err(Error::InvalidMeasurementBoundary { .. })
        ));
    }

    #[test]
    fn measurement_boundary_allows_declared_disjoint_siblings() {
        let package = source(
            1,
            "package",
            SourceKind::CpuPackageEnergy,
            Unit::Joule,
            Temporality::Cumulative,
            EvidenceClass::RawHardwareCounter,
            None,
        );
        let core = declare_disjoint(
            source(
                2,
                "core",
                SourceKind::CpuCoreEnergy,
                Unit::Joule,
                Temporality::Cumulative,
                EvidenceClass::RawHardwareCounter,
                Some("package"),
            ),
            "package-0-domains",
            "core",
        );
        let dram = declare_disjoint(
            source(
                3,
                "dram",
                SourceKind::DramEnergy,
                Unit::Joule,
                Temporality::Cumulative,
                EvidenceClass::RawHardwareCounter,
                Some("package"),
            ),
            "package-0-domains",
            "dram",
        );
        let sources = vec![package.clone(), core.clone(), dram.clone()];
        let boundary = MeasurementBoundary {
            boundary_id: "core-plus-dram".to_string(),
            backend: "power-meter".to_string(),
            primary_source_uids: vec!["core".to_string(), "dram".to_string()],
            aggregation_rule: AggregationRule::SumDeclaredDisjoint,
            required_sources: vec![signature(&core), signature(&dram), signature(&package)],
        };

        validate_measurement_boundary(&boundary, &sources).unwrap();
    }

    #[test]
    fn measurement_boundary_requires_descriptor_proof_for_disjoint_sum() {
        let cpu = source(
            1,
            "cpu",
            SourceKind::CpuPower,
            Unit::Watt,
            Temporality::Instant,
            EvidenceClass::ToolDerivedEstimate,
            None,
        );
        let gpu = source(
            2,
            "gpu",
            SourceKind::GpuPower,
            Unit::Watt,
            Temporality::Instant,
            EvidenceClass::ToolDerivedEstimate,
            None,
        );
        let boundary = MeasurementBoundary {
            boundary_id: "compute".to_string(),
            backend: "power-meter".to_string(),
            primary_source_uids: vec!["cpu".to_string(), "gpu".to_string()],
            aggregation_rule: AggregationRule::SumDeclaredDisjoint,
            required_sources: vec![signature(&cpu), signature(&gpu)],
        };

        let error = validate_measurement_boundary(&boundary, &[cpu, gpu]).unwrap_err();
        assert!(error.to_string().contains(DISJOINT_GROUP_LABEL));
    }

    #[test]
    fn measurement_boundary_rejects_duplicate_disjoint_members() {
        let cpu = declare_disjoint(
            source(
                1,
                "cpu",
                SourceKind::CpuPower,
                Unit::Watt,
                Temporality::Instant,
                EvidenceClass::ToolDerivedEstimate,
                None,
            ),
            "compute",
            "same-member",
        );
        let gpu = declare_disjoint(
            source(
                2,
                "gpu",
                SourceKind::GpuPower,
                Unit::Watt,
                Temporality::Instant,
                EvidenceClass::ToolDerivedEstimate,
                None,
            ),
            "compute",
            "same-member",
        );
        let boundary = MeasurementBoundary {
            boundary_id: "compute".to_string(),
            backend: "power-meter".to_string(),
            primary_source_uids: vec!["cpu".to_string(), "gpu".to_string()],
            aggregation_rule: AggregationRule::SumDeclaredDisjoint,
            required_sources: vec![signature(&cpu), signature(&gpu)],
        };

        let error = validate_measurement_boundary(&boundary, &[cpu, gpu]).unwrap_err();
        assert!(error.to_string().contains("repeats disjoint member"));
    }

    #[test]
    fn measurement_boundary_rejects_nested_sums_and_incomplete_topology() {
        let package = source(
            1,
            "package",
            SourceKind::CpuPackageEnergy,
            Unit::Joule,
            Temporality::Cumulative,
            EvidenceClass::RawHardwareCounter,
            None,
        );
        let dram = source(
            2,
            "dram",
            SourceKind::DramEnergy,
            Unit::Joule,
            Temporality::Cumulative,
            EvidenceClass::RawHardwareCounter,
            Some("package"),
        );
        let sources = vec![package.clone(), dram.clone()];

        let incomplete = MeasurementBoundary {
            boundary_id: "dram-only".to_string(),
            backend: "power-meter".to_string(),
            primary_source_uids: vec!["dram".to_string()],
            aggregation_rule: AggregationRule::SingleSource,
            required_sources: vec![signature(&dram)],
        };
        assert!(matches!(
            validate_measurement_boundary(&incomplete, &sources),
            Err(Error::InvalidMeasurementBoundary { .. })
        ));

        let nested_sum = MeasurementBoundary {
            boundary_id: "nested".to_string(),
            backend: "power-meter".to_string(),
            primary_source_uids: vec!["package".to_string(), "dram".to_string()],
            aggregation_rule: AggregationRule::SumDeclaredDisjoint,
            required_sources: vec![signature(&package), signature(&dram)],
        };
        assert!(matches!(
            validate_measurement_boundary(&nested_sum, &sources),
            Err(Error::InvalidMeasurementBoundary { .. })
        ));
    }

    #[test]
    fn measurement_boundary_rejects_incompatible_sum_semantics() {
        let energy = source(
            1,
            "energy",
            SourceKind::CpuPackageEnergy,
            Unit::Joule,
            Temporality::Cumulative,
            EvidenceClass::RawHardwareCounter,
            None,
        );
        let power = source(
            2,
            "power",
            SourceKind::GpuPower,
            Unit::Watt,
            Temporality::Instant,
            EvidenceClass::RawSensorReading,
            None,
        );
        let boundary = MeasurementBoundary {
            boundary_id: "incompatible".to_string(),
            backend: "power-meter".to_string(),
            primary_source_uids: vec!["energy".to_string(), "power".to_string()],
            aggregation_rule: AggregationRule::SumDeclaredDisjoint,
            required_sources: vec![signature(&energy), signature(&power)],
        };

        assert!(matches!(
            validate_measurement_boundary(&boundary, &[energy, power]),
            Err(Error::InvalidMeasurementBoundary { .. })
        ));
    }

    #[test]
    fn validates_trial_estimand_and_serialized_arm_tag() {
        let package = source(
            1,
            "package",
            SourceKind::CpuPackageEnergy,
            Unit::Joule,
            Temporality::Cumulative,
            EvidenceClass::RawHardwareCounter,
            None,
        );
        let mut trial = TrialMetadata {
            experiment_id: "exp-1".to_string(),
            trial_id: "trial-1".to_string(),
            run_id: "run-1".to_string(),
            matched_block_id: "block-1".to_string(),
            arm: TrialArm::Program {
                program_id: "P1".to_string(),
            },
            background_workload_id: "cpu-batch".to_string(),
            input_id: "input-1".to_string(),
            replay_seed: 7,
            estimand: EnergyEstimand::FixedHorizon { horizon_ns: 1_000 },
            measurement_boundary: single_boundary(&package),
            labels: Default::default(),
        };

        validate_trial_metadata(&trial, std::slice::from_ref(&package)).unwrap();
        let arm_json = serde_json::to_value(&trial.arm).unwrap();
        assert_eq!(arm_json["kind"], "PROGRAM");
        assert_eq!(arm_json["program_id"], "P1");

        trial.estimand = EnergyEstimand::FixedHorizon { horizon_ns: 0 };
        assert!(matches!(
            validate_trial_metadata(&trial, &[package]),
            Err(Error::InvalidTrialMetadata { .. })
        ));
    }

    #[test]
    fn trial_quality_requires_consistent_disposition_and_finite_ratios() {
        let mut quality = TrialQualityMetadata {
            trial_id: "trial-1".to_string(),
            disposition: QualityDisposition::Accepted,
            measurement_overhead_ratio: Some(0.005),
            sources: vec![SourceQualitySummary {
                source_uid: "package".to_string(),
                observed_samples: 100,
                sequence_gaps: 0,
                missing_sample_ratio: 0.0,
                max_timestamp_jitter_ns: Some(100),
            }],
            issues: vec![],
        };
        validate_trial_quality(&quality).unwrap();

        quality.issues.push(QualityIssue {
            code: "TRACE_GAP".to_string(),
            severity: QualitySeverity::Error,
            message: "trace contains a gap".to_string(),
            source_uid: Some("package".to_string()),
        });
        assert!(matches!(
            validate_trial_quality(&quality),
            Err(Error::InvalidTrialQuality { .. })
        ));

        quality.disposition = QualityDisposition::Rejected;
        quality.sources[0].missing_sample_ratio = f64::NAN;
        assert!(matches!(
            validate_trial_quality(&quality),
            Err(Error::InvalidTrialQuality { .. })
        ));
    }

    #[test]
    fn counterfactual_estimate_is_model_derived_or_explicitly_abstains() {
        let package = source(
            1,
            "package",
            SourceKind::CpuPackageEnergy,
            Unit::Joule,
            Temporality::Cumulative,
            EvidenceClass::RawHardwareCounter,
            None,
        );
        let boundary = single_boundary(&package);
        let estimand = EnergyEstimand::EqualWork {
            work_amount: 100.0,
            work_unit: "requests".to_string(),
        };
        let mut estimate = CounterfactualImpactEstimate {
            estimate_id: "estimate-1".to_string(),
            trial_id: "trial-1".to_string(),
            measurement_boundary: boundary.clone(),
            estimand: estimand.clone(),
            evidence_class: EvidenceClass::ModelDerivedEstimate,
            model: ModelIdentity {
                model_id: "state-space".to_string(),
                model_version: "1".to_string(),
                training_data_id: "controls-v1".to_string(),
            },
            result: CounterfactualEstimateResult::Estimated {
                observed_joules: 98.0,
                counterfactual_joules: 100.0,
                impact_joules: -2.0,
                interval: JouleInterval {
                    confidence_level: 0.95,
                    lower_joules: -3.0,
                    upper_joules: -1.0,
                },
                validation_status: EstimateValidationStatus::Validated,
            },
            prediction_path: CounterfactualPredictionPath {
                timestamps_ns: vec![0, 10_000_000_000],
                predicted_power_watts: vec![10.0, 10.0],
                lower_power_watts: vec![9.9, 9.9],
                upper_power_watts: vec![10.1, 10.1],
                integration_method: PredictionIntegrationMethod::Trapezoidal,
                integrated_counterfactual: Some(IntegratedCounterfactualEnergy {
                    joules: 100.0,
                    interval: JouleInterval {
                        confidence_level: 0.95,
                        lower_joules: 99.0,
                        upper_joules: 101.0,
                    },
                }),
                training_provenance: CounterfactualTrainingProvenance {
                    control_episode_ids: vec!["control-1".to_string()],
                    causal_ground_truth: CausalGroundTruthProvenance {
                        study_id: "study-1".to_string(),
                        claim_id: "claim-1".to_string(),
                        measurement_boundary_id: boundary.boundary_id.clone(),
                        claims_artifact_sha256: "a".repeat(64),
                        acceptance: StudyAcceptanceEvidence {
                            supported: true,
                            fixed_horizon_supported: true,
                            equal_work_supported: true,
                            placebo_supported: true,
                            independent_day_replication_supported: true,
                            mechanism_required: false,
                            mechanism_supported: None,
                            reasons: Vec::new(),
                        },
                        preregistration_sha256: Some("1".repeat(64)),
                        measurement_boundary_sha256: Some("2".repeat(64)),
                        source_topology_sha256: Some("3".repeat(64)),
                        accepted_calibration_sha256: vec!["4".repeat(64)],
                        accepted_machine_provenance_sha256: vec!["5".repeat(64)],
                        artifact_verified: true,
                    },
                    accepted_control_trials: vec![AcceptedControlTrialProvenance {
                        trial_id: "control-1".to_string(),
                        experiment_id: "training-experiment".to_string(),
                        matched_block_id: "training-block".to_string(),
                        measurement_boundary_id: boundary.boundary_id.clone(),
                        checksums_sha256: "b".repeat(64),
                        arm: "C0".to_string(),
                        program_id: None,
                        quality_disposition: "ACCEPTED".to_string(),
                        background_only: true,
                        trial_bundle_seal: Some(crate::research::TrialBundleSealProvenance {
                            preregistration_sha256: "1".repeat(64),
                            measurement_boundary_sha256: "2".repeat(64),
                            source_topology_sha256: "3".repeat(64),
                            machine_provenance_sha256: "5".repeat(64),
                            measurement_overhead_calibration_sha256: "4".repeat(64),
                            artifact_verified: true,
                        }),
                        artifact_verified: true,
                    }],
                    control_model_inputs: vec![ModelInputBindingProvenance {
                        role: ModelInputRole::ControlEpisode,
                        trial_id: "control-1".to_string(),
                        trial_checksums_sha256: "b".repeat(64),
                        feature_artifact_sha256: "d".repeat(64),
                        payload_sha256: "e".repeat(64),
                        power_derivation: "RAW_COUNTER_INTERVAL_RATE".to_string(),
                        feature_alignment: "OUTCOME_START_STEP_PREVIOUS_V1".to_string(),
                        artifact_verified: true,
                    }],
                    semantic_rebind: None,
                    group_split_validation: Some(GroupSplitValidationProvenance {
                        train_rows: 120,
                        test_rows: 40,
                        train_groups: 1,
                        test_groups: 1,
                        strategy: GroupSplitStrategy::PerHost,
                        disjoint_axes: vec![
                            "run_id".to_string(),
                            "replay_seed".to_string(),
                            "day".to_string(),
                            "workload_family".to_string(),
                        ],
                        train_episodes: vec![crate::research::SplitEpisodeBindingProvenance {
                            episode_id: "control-1".to_string(),
                            trial_checksums_sha256: "b".repeat(64),
                            model_input_payload_sha256: "e".repeat(64),
                            row_count: 120,
                            group_key: crate::research::EpisodeGroupKeyProvenance {
                                run_id: "run-train".to_string(),
                                replay_seed: 1,
                                day: "2026-07-18".to_string(),
                                workload_family: "service".to_string(),
                                machine: "8".repeat(64),
                            },
                        }],
                        test_episodes: vec![crate::research::SplitEpisodeBindingProvenance {
                            episode_id: "control-holdout".to_string(),
                            trial_checksums_sha256: "9".repeat(64),
                            model_input_payload_sha256: "7".repeat(64),
                            row_count: 40,
                            group_key: crate::research::EpisodeGroupKeyProvenance {
                                run_id: "run-test".to_string(),
                                replay_seed: 2,
                                day: "2026-07-19".to_string(),
                                workload_family: "batch".to_string(),
                                machine: "8".repeat(64),
                            },
                        }],
                        split_artifact_sha256: "1".repeat(64),
                        artifact_verified: true,
                    }),
                    held_out_model_validation: Some(
                        crate::research::HeldOutModelValidationProvenance {
                            split_artifact_sha256: "1".repeat(64),
                            training_data_id: "controls-v1".to_string(),
                            fitted_parameters_sha256: "2".repeat(64),
                            observed_paths_sha256: "3".repeat(64),
                            predicted_paths_sha256: "4".repeat(64),
                            test_episode_ids: vec!["control-holdout".to_string()],
                            test_rows: 40,
                            root_mean_squared_error_watts: 0.5,
                            mean_absolute_error_watts: 0.4,
                            mean_error_watts: -0.1,
                            out_of_distribution_rows: 0,
                            artifact_sha256: "6".repeat(64),
                            artifact_verified: true,
                        },
                    ),
                },
                accepted_target_trial: AcceptedTargetTrialProvenance {
                    trial_id: "trial-1".to_string(),
                    experiment_id: "confirmatory-experiment".to_string(),
                    matched_block_id: "confirmatory-block".to_string(),
                    measurement_boundary_id: boundary.boundary_id,
                    checksums_sha256: "c".repeat(64),
                    arm: "C1".to_string(),
                    program_id: "P1".to_string(),
                    quality_disposition: "ACCEPTED".to_string(),
                    observed_joules: 98.0,
                    estimand,
                    trial_bundle_seal: Some(crate::research::TrialBundleSealProvenance {
                        preregistration_sha256: "1".repeat(64),
                        measurement_boundary_sha256: "2".repeat(64),
                        source_topology_sha256: "3".repeat(64),
                        machine_provenance_sha256: "5".repeat(64),
                        measurement_overhead_calibration_sha256: "4".repeat(64),
                        artifact_verified: true,
                    }),
                    artifact_verified: true,
                },
                target_model_input: Some(ModelInputBindingProvenance {
                    role: ModelInputRole::TargetRequest,
                    trial_id: "trial-1".to_string(),
                    trial_checksums_sha256: "c".repeat(64),
                    feature_artifact_sha256: "f".repeat(64),
                    payload_sha256: "0".repeat(64),
                    power_derivation: "RAW_COUNTER_INTERVAL_RATE".to_string(),
                    feature_alignment: "OUTCOME_START_STEP_PREVIOUS_V1".to_string(),
                    artifact_verified: true,
                }),
            },
            labels: Default::default(),
        };
        validate_counterfactual_impact_estimate(&estimate, std::slice::from_ref(&package)).unwrap();

        estimate.evidence_class = EvidenceClass::ToolDerivedEstimate;
        assert!(matches!(
            validate_counterfactual_impact_estimate(&estimate, std::slice::from_ref(&package)),
            Err(Error::InvalidCounterfactualImpactEstimate { .. })
        ));

        estimate.evidence_class = EvidenceClass::ModelDerivedEstimate;
        estimate.result = CounterfactualEstimateResult::AbstainedOutOfDistribution {
            reason: "pre-launch state is outside the training support".to_string(),
        };
        estimate.prediction_path.predicted_power_watts.clear();
        estimate.prediction_path.lower_power_watts.clear();
        estimate.prediction_path.upper_power_watts.clear();
        estimate.prediction_path.integrated_counterfactual = None;
        validate_counterfactual_impact_estimate(&estimate, &[package]).unwrap();
        let json = serde_json::to_value(&estimate.result).unwrap();
        assert_eq!(json["kind"], "ABSTAINED_OUT_OF_DISTRIBUTION");
    }
}
