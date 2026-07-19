//! Serializable declarations for offline energy-impact experiments.
//!
//! These records describe experiment inputs and analysis artifacts. They do
//! not perform integration, baseline subtraction, model inference, claim
//! evaluation, or any other interpretation of raw telemetry.

use crate::model::{
    EvidenceClass, SourceKind, Temporality, Unit, INTERVAL_DURATION_SOURCE_UID_LABEL,
};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Descriptor label naming the physical partition whose members may be
/// combined by a `SUM_DECLARED_DISJOINT` boundary.
pub const DISJOINT_GROUP_LABEL: &str = "wattch.disjoint_group";

/// Descriptor label naming one non-overlapping member within a disjoint group.
pub const DISJOINT_MEMBER_LABEL: &str = "wattch.disjoint_member";

/// The only aggregation permitted for the primary sources of a boundary.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AggregationRule {
    /// The outcome is taken from exactly one source.
    SingleSource,
    /// The selected descriptors carry an explicit backend-provided
    /// disjoint-group/member declaration that permits offline summation.
    SumDeclaredDisjoint,
}

/// Stable source semantics required by a measurement boundary.
///
/// `local_source_id` is intentionally absent because it is capture-local.
/// The signature preserves the evidence boundary across independent runs.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct BoundarySourceSignature {
    pub source_uid: String,
    pub kind: SourceKind,
    pub unit: Unit,
    pub temporality: Temporality,
    pub evidence_class: EvidenceClass,
    pub parent_source_uid: Option<String>,
    pub clock_domain: String,

    /// Frozen copies of the two descriptor labels that establish an explicit
    /// disjointness declaration. Both are required on every primary selected
    /// by `SumDeclaredDisjoint`; legacy and single-source boundaries may omit
    /// them.
    #[serde(default)]
    pub disjoint_group: Option<String>,
    #[serde(default)]
    pub disjoint_member: Option<String>,

    /// Frozen link to the raw duration evidence required to interpret an
    /// interval-average primary source. Legacy and non-interval sources omit
    /// it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interval_duration_source_uid: Option<String>,
}

impl BoundarySourceSignature {
    pub fn from_descriptor(source: &crate::model::SourceDescriptor) -> Self {
        Self {
            source_uid: source.source_uid.clone(),
            kind: source.kind.clone(),
            unit: source.unit.clone(),
            temporality: source.temporality.clone(),
            evidence_class: source.evidence_class.clone(),
            parent_source_uid: source.parent_source_uid.clone(),
            clock_domain: source.clock_domain.clone(),
            disjoint_group: source.labels.get(DISJOINT_GROUP_LABEL).cloned(),
            disjoint_member: source.labels.get(DISJOINT_MEMBER_LABEL).cloned(),
            interval_duration_source_uid: source
                .labels
                .get(INTERVAL_DURATION_SOURCE_UID_LABEL)
                .cloned(),
        }
    }
}

/// Declares the daemon sources that define one comparable outcome boundary.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct MeasurementBoundary {
    pub boundary_id: String,
    pub backend: String,
    pub primary_source_uids: Vec<String>,
    pub aggregation_rule: AggregationRule,

    /// Exact signatures for every primary source and any ancestors needed to
    /// describe its topology.
    pub required_sources: Vec<BoundarySourceSignature>,
}

/// Identifies the role of one trial inside a matched block.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TrialArm {
    /// Background demand plus the experiment's sham launcher.
    BackgroundControl,
    /// Background demand plus the identified program.
    Program { program_id: String },
}

/// Declares how an experiment fixes its observation window or useful work.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EnergyEstimand {
    FixedHorizon { horizon_ns: u64 },
    EqualWork { work_amount: f64, work_unit: String },
}

/// Reproducibility metadata stored with a single experimental trial.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TrialMetadata {
    pub experiment_id: String,
    pub trial_id: String,
    pub run_id: String,
    pub matched_block_id: String,
    pub arm: TrialArm,
    pub background_workload_id: String,
    pub input_id: String,
    pub replay_seed: u64,
    pub estimand: EnergyEstimand,
    pub measurement_boundary: MeasurementBoundary,

    #[serde(default)]
    pub labels: BTreeMap<String, String>,
}

/// Analysis disposition for one captured trial.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum QualityDisposition {
    Accepted,
    ReviewRequired,
    Rejected,
}

/// Severity of a structured evidence-quality issue.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum QualitySeverity {
    Warning,
    Error,
}

/// One structured reason affecting a trial's analysis disposition.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct QualityIssue {
    pub code: String,
    pub severity: QualitySeverity,
    pub message: String,
    pub source_uid: Option<String>,
}

/// Per-source evidence-quality summary computed offline from a raw trace.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SourceQualitySummary {
    pub source_uid: String,
    pub observed_samples: u64,
    pub sequence_gaps: u64,
    pub missing_sample_ratio: f64,
    pub max_timestamp_jitter_ns: Option<u64>,
}

/// Offline quality metadata for a trial artifact.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TrialQualityMetadata {
    pub trial_id: String,
    pub disposition: QualityDisposition,

    /// Fractional overhead measured by a separate calibration procedure.
    pub measurement_overhead_ratio: Option<f64>,

    pub sources: Vec<SourceQualitySummary>,
    #[serde(default)]
    pub issues: Vec<QualityIssue>,
}

/// Provenance for a counterfactual model output.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelIdentity {
    pub model_id: String,
    pub model_version: String,
    pub training_data_id: String,
}

/// Validation status attached to an emitted numeric estimate.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EstimateValidationStatus {
    Exploratory,
    Validated,
}

/// Uncertainty interval for an impact estimate, in joules.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct JouleInterval {
    pub confidence_level: f64,
    pub lower_joules: f64,
    pub upper_joules: f64,
}

/// Numerical rule used to integrate a model-predicted power path.
///
/// This is deliberately a closed enum: counterfactual artifacts must state
/// the rule whose result can be reproduced by an offline validator.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PredictionIntegrationMethod {
    Trapezoidal,
}

/// Content-addressed acceptance decision that authorizes model training.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CausalGroundTruthProvenance {
    pub study_id: String,
    pub claim_id: String,
    pub measurement_boundary_id: String,
    pub claims_artifact_sha256: String,
    pub acceptance: StudyAcceptanceEvidence,
    #[serde(default)]
    pub preregistration_sha256: Option<String>,
    #[serde(default)]
    pub measurement_boundary_sha256: Option<String>,
    #[serde(default)]
    pub source_topology_sha256: Option<String>,
    #[serde(default)]
    pub accepted_calibration_sha256: Vec<String>,
    #[serde(default)]
    pub accepted_machine_provenance_sha256: Vec<String>,

    /// True only when the referenced claim artifact was loaded and its digest
    /// was verified, rather than copied from caller-supplied metadata.
    pub artifact_verified: bool,
}

/// Factory-issued proof that one trial passed the study's calibration bundle.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct TrialBundleSealProvenance {
    pub preregistration_sha256: String,
    pub measurement_boundary_sha256: String,
    pub source_topology_sha256: String,
    pub machine_provenance_sha256: String,
    pub measurement_overhead_calibration_sha256: String,
    pub artifact_verified: bool,
}

/// Portable subset of the confirmatory study acceptance decision.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StudyAcceptanceEvidence {
    pub supported: bool,
    pub fixed_horizon_supported: bool,
    pub equal_work_supported: bool,
    pub placebo_supported: bool,
    pub independent_day_replication_supported: bool,
    pub mechanism_required: bool,
    pub mechanism_supported: Option<bool>,
    #[serde(default)]
    pub reasons: Vec<String>,
}

/// Seal for one background-only, quality-accepted C0 training trial.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AcceptedControlTrialProvenance {
    pub trial_id: String,
    pub experiment_id: String,
    pub matched_block_id: String,
    pub measurement_boundary_id: String,
    pub checksums_sha256: String,
    pub arm: String,
    pub program_id: Option<String>,
    pub quality_disposition: String,
    pub background_only: bool,
    #[serde(default)]
    pub trial_bundle_seal: Option<TrialBundleSealProvenance>,

    /// True only when the trial checksum manifest and the artifacts it seals
    /// were verified before this prediction was emitted.
    pub artifact_verified: bool,
}

/// Role of one cryptographically bound model-input payload.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ModelInputRole {
    /// Replay-derived power plus precommitted external inputs for a C0 episode.
    ControlEpisode,
    /// Replay-derived initial state plus precommitted external target inputs.
    TargetRequest,
}

/// Seal binding model inputs to a replay-verified trial and external artifact.
///
/// The referenced feature artifact carries only pre-treatment and exogenous
/// inputs. The power path and initial state are derived from the raw trial
/// sealed by `trial_checksums_sha256`; `payload_sha256` binds their canonical
/// composition before fitting or prediction.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelInputBindingProvenance {
    pub role: ModelInputRole,
    pub trial_id: String,
    pub trial_checksums_sha256: String,
    pub feature_artifact_sha256: String,
    pub payload_sha256: String,
    pub power_derivation: String,
    /// Deterministic rule used to align preregistered relative-time features
    /// to the replay-derived daemon timeline.
    #[serde(default)]
    pub feature_alignment: String,
    pub artifact_verified: bool,
}

/// One source-identity mapping in a cross-host semantic rebind.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceUidRebind {
    pub source_uid: String,
    pub target_uid: String,
}

/// Explicit same-backend semantic mapping used for target-host recalibration.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SemanticBoundaryRebindProvenance {
    pub source_boundary_id: String,
    pub target_boundary_id: String,
    pub backend: String,
    pub aggregation_rule: AggregationRule,
    pub raw_joule_pooling_permitted: bool,
    pub source_to_target_uids: Vec<SourceUidRebind>,
}

/// Episode-level split strategy used to test model generalization without
/// leaking telemetry rows from the same experimental groups.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GroupSplitStrategy {
    PerHost,
    CrossMachineTransfer,
}

/// Replay-derived grouping metadata for one indivisible control episode.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct EpisodeGroupKeyProvenance {
    pub run_id: String,
    pub replay_seed: u64,
    pub day: String,
    pub workload_family: String,
    pub machine: String,
}

/// Content-addressed membership of one replay-issued episode in a split.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SplitEpisodeBindingProvenance {
    pub episode_id: String,
    pub trial_checksums_sha256: String,
    pub model_input_payload_sha256: String,
    pub row_count: u64,
    pub group_key: EpisodeGroupKeyProvenance,
}

/// Verified summary of the complete-run model train/test split.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct GroupSplitValidationProvenance {
    pub train_rows: u64,
    pub test_rows: u64,
    pub train_groups: u64,
    pub test_groups: u64,
    pub strategy: GroupSplitStrategy,
    pub disjoint_axes: Vec<String>,
    #[serde(default)]
    pub train_episodes: Vec<SplitEpisodeBindingProvenance>,
    #[serde(default)]
    pub test_episodes: Vec<SplitEpisodeBindingProvenance>,
    #[serde(default)]
    pub split_artifact_sha256: String,
    pub artifact_verified: bool,
}

/// Fit-issued evaluation over replay-bound C0 episodes excluded from fitting.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct HeldOutModelValidationProvenance {
    pub split_artifact_sha256: String,
    pub training_data_id: String,
    pub fitted_parameters_sha256: String,
    pub observed_paths_sha256: String,
    pub predicted_paths_sha256: String,
    pub test_episode_ids: Vec<String>,
    pub test_rows: u64,
    pub root_mean_squared_error_watts: f64,
    pub mean_absolute_error_watts: f64,
    pub mean_error_watts: f64,
    pub out_of_distribution_rows: u64,
    pub artifact_sha256: String,
    pub artifact_verified: bool,
}

/// Provenance for the complete set of C0 episodes used to train the model.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CounterfactualTrainingProvenance {
    pub control_episode_ids: Vec<String>,
    pub causal_ground_truth: CausalGroundTruthProvenance,
    pub accepted_control_trials: Vec<AcceptedControlTrialProvenance>,
    /// Replay-issued model-input bindings. Legacy/exploratory artifacts may
    /// omit these; a `VALIDATED` estimate requires exactly one per control.
    #[serde(default)]
    pub control_model_inputs: Vec<ModelInputBindingProvenance>,
    pub semantic_rebind: Option<SemanticBoundaryRebindProvenance>,
    #[serde(default)]
    pub group_split_validation: Option<GroupSplitValidationProvenance>,
    #[serde(default)]
    pub held_out_model_validation: Option<HeldOutModelValidationProvenance>,
}

/// Seal for the quality-accepted treatment trial being estimated.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AcceptedTargetTrialProvenance {
    pub trial_id: String,
    pub experiment_id: String,
    pub matched_block_id: String,
    pub measurement_boundary_id: String,
    pub checksums_sha256: String,
    pub arm: String,
    pub program_id: String,
    pub quality_disposition: String,
    pub observed_joules: f64,
    pub estimand: EnergyEstimand,
    #[serde(default)]
    pub trial_bundle_seal: Option<TrialBundleSealProvenance>,

    /// True only when the trial checksum manifest and the artifacts it seals
    /// were verified before this prediction was emitted.
    pub artifact_verified: bool,
}

/// Reproducible integration of the mean and interval prediction paths.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct IntegratedCounterfactualEnergy {
    pub joules: f64,
    pub interval: JouleInterval,
}

/// Complete no-program prediction path retained with every model result.
///
/// OOD abstentions retain timestamps and provenance but carry empty power
/// vectors and no integrated counterfactual. This preserves why and where the
/// model abstained without fabricating a numeric estimate.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CounterfactualPredictionPath {
    pub timestamps_ns: Vec<u64>,
    pub predicted_power_watts: Vec<f64>,
    pub lower_power_watts: Vec<f64>,
    pub upper_power_watts: Vec<f64>,
    pub integration_method: PredictionIntegrationMethod,
    pub integrated_counterfactual: Option<IntegratedCounterfactualEnergy>,
    pub training_provenance: CounterfactualTrainingProvenance,
    pub accepted_target_trial: AcceptedTargetTrialProvenance,
    /// Replay-issued target-input binding. Legacy/exploratory artifacts may
    /// omit it; a `VALIDATED` estimate requires it.
    #[serde(default)]
    pub target_model_input: Option<ModelInputBindingProvenance>,
}

/// A numeric counterfactual result or an explicit out-of-distribution
/// abstention. The tagged representation prevents abstention from being
/// confused with a zero effect.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CounterfactualEstimateResult {
    Estimated {
        observed_joules: f64,
        counterfactual_joules: f64,
        impact_joules: f64,
        interval: JouleInterval,
        validation_status: EstimateValidationStatus,
    },
    AbstainedOutOfDistribution {
        reason: String,
    },
}

/// Metadata envelope for an offline counterfactual impact estimate.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CounterfactualImpactEstimate {
    pub estimate_id: String,
    pub trial_id: String,
    pub measurement_boundary: MeasurementBoundary,
    pub estimand: EnergyEstimand,

    /// Must remain `MODEL_DERIVED_ESTIMATE`, even when the model abstains.
    pub evidence_class: EvidenceClass,
    pub model: ModelIdentity,
    pub result: CounterfactualEstimateResult,
    pub prediction_path: CounterfactualPredictionPath,

    #[serde(default)]
    pub labels: BTreeMap<String, String>,
}
