use serde::Deserialize;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use wattch_core::{validate_source_set, RawSampleBatch, RawValue};
use wattch_daemon::SampleBackend;
use wattch_testd::scenario::{generate_batches, load_scenario, DeterministicBackend, Scenario};

const EXTENDED_SCENARIOS: &[&str] = &[
    "causal_additive_null.json",
    "causal_interference_reversal.json",
    "causal_low_priority_dvfs.json",
    "causal_carryover_confounding.json",
    "causal_treatment_timing_confounding.json",
    "quality_faults.json",
];

#[derive(Debug, Deserialize)]
struct TruthFixture {
    scenario_file: String,
    #[serde(default)]
    primary_source_id: Option<u32>,
    #[serde(default)]
    work_source_id: Option<u32>,
    #[serde(default)]
    idle_source_id: Option<u32>,
    #[serde(default)]
    windows: BTreeMap<String, TickWindow>,
    expected: Value,
}

#[derive(Clone, Copy, Debug, Deserialize)]
struct TickWindow {
    start_tick: u64,
    end_tick: u64,
}

fn scenario_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../examples/scenarios")
}

fn truth_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/truth")
}

fn load_truth(name: &str) -> (TruthFixture, Scenario) {
    let raw = std::fs::read_to_string(truth_dir().join(name)).unwrap();
    let truth: TruthFixture = serde_json::from_str(&raw).unwrap();
    let scenario = load_scenario(&scenario_dir().join(&truth.scenario_file)).unwrap();
    (truth, scenario)
}

fn source_value(scenario: &Scenario, source_id: u32, tick: u64) -> f64 {
    scenario
        .sources
        .iter()
        .find(|source| source.descriptor.local_source_id == source_id)
        .unwrap_or_else(|| panic!("source {source_id} not found"))
        .behavior
        .value_at(tick)
}

fn delta(scenario: &Scenario, source_id: u32, window: TickWindow) -> f64 {
    source_value(scenario, source_id, window.end_tick)
        - source_value(scenario, source_id, window.start_tick)
}

fn rate(scenario: &Scenario, source_id: u32, window: TickWindow) -> f64 {
    delta(scenario, source_id, window) / (window.end_tick - window.start_tick) as f64
}

fn expected_f64(truth: &TruthFixture, name: &str) -> f64 {
    truth.expected[name]
        .as_f64()
        .unwrap_or_else(|| panic!("expected numeric truth field {name}"))
}

fn assert_close(actual: f64, expected: f64) {
    let tolerance = 1e-9_f64.max(expected.abs() * 1e-9);
    assert!(
        (actual - expected).abs() <= tolerance,
        "expected {expected}, got {actual}"
    );
}

fn batches_by_tick(scenario: &Scenario) -> Vec<RawSampleBatch> {
    generate_batches(scenario).collect()
}

#[test]
fn all_extended_scenarios_validate_and_match_deterministic_backend_values() {
    for file in EXTENDED_SCENARIOS {
        let scenario = load_scenario(&scenario_dir().join(file)).unwrap();
        let descriptors = scenario
            .sources
            .iter()
            .map(|source| source.descriptor.clone())
            .collect::<Vec<_>>();
        validate_source_set(&descriptors).unwrap();

        let mut backend = DeterministicBackend::new(&scenario);
        for batch in generate_batches(&scenario) {
            let readings = backend.sample();
            let expected = batch
                .samples
                .iter()
                .map(|sample| (sample.source_id, sample.value.clone()))
                .collect::<Vec<_>>();
            assert_eq!(readings, expected, "backend mismatch in {file}");
        }
    }
}

#[test]
fn additive_null_has_context_invariant_incremental_effects() {
    let (truth, scenario) = load_truth("additive_null.truth.json");
    let source_id = truth.primary_source_id.unwrap();
    let window = |name| truth.windows[name];

    let p1_idle = rate(&scenario, source_id, window("p1_idle"))
        - rate(&scenario, source_id, window("idle_control"));
    let p1_background = rate(&scenario, source_id, window("p1_background"))
        - rate(&scenario, source_id, window("background_control"));
    let p2_idle = rate(&scenario, source_id, window("p2_idle"))
        - rate(&scenario, source_id, window("idle_control"));
    let p2_background = rate(&scenario, source_id, window("p2_background"))
        - rate(&scenario, source_id, window("background_control"));

    assert_close(
        p1_idle,
        expected_f64(&truth, "p1_idle_incremental_joules_per_tick"),
    );
    assert_close(
        p1_background,
        expected_f64(&truth, "p1_background_incremental_joules_per_tick"),
    );
    assert_close(
        p2_idle,
        expected_f64(&truth, "p2_idle_incremental_joules_per_tick"),
    );
    assert_close(
        p2_background,
        expected_f64(&truth, "p2_background_incremental_joules_per_tick"),
    );
    assert_close(p1_idle, p1_background);
    assert_close(p2_idle, p2_background);
}

#[test]
fn interference_fixture_contains_a_ranking_reversal() {
    let (truth, scenario) = load_truth("interference_reversal.truth.json");
    let source_id = truth.primary_source_id.unwrap();
    let window = |name| truth.windows[name];

    let isolated_difference = rate(&scenario, source_id, window("p1_idle"))
        - rate(&scenario, source_id, window("p2_idle"));
    let background_difference = rate(&scenario, source_id, window("p1_background"))
        - rate(&scenario, source_id, window("p2_background"));
    let p1_incremental = rate(&scenario, source_id, window("p1_background"))
        - rate(&scenario, source_id, window("background_control"));
    let p2_incremental = rate(&scenario, source_id, window("p2_background"))
        - rate(&scenario, source_id, window("background_control"));

    assert!(isolated_difference > 0.0);
    assert!(background_difference < 0.0);
    assert_close(
        isolated_difference,
        expected_f64(&truth, "isolated_p1_minus_p2_joules_per_tick"),
    );
    assert_close(
        background_difference,
        expected_f64(&truth, "background_p1_minus_p2_joules_per_tick"),
    );
    assert_close(
        p1_incremental,
        expected_f64(&truth, "p1_background_incremental_joules_per_tick"),
    );
    assert_close(
        p2_incremental,
        expected_f64(&truth, "p2_background_incremental_joules_per_tick"),
    );
}

#[test]
fn low_priority_fixture_preserves_work_and_changes_under_fixed_frequency() {
    let (truth, scenario) = load_truth("low_priority_dvfs.truth.json");
    let energy_source = truth.primary_source_id.unwrap();
    let work_source = truth.work_source_id.unwrap();
    let window = |name| truth.windows[name];

    for arm in [
        "dynamic_normal",
        "dynamic_low_priority",
        "fixed_normal",
        "fixed_low_priority",
    ] {
        assert_close(
            delta(&scenario, work_source, window(arm)),
            expected_f64(&truth, "work_units_per_arm"),
        );
    }

    let dynamic_normal = delta(&scenario, energy_source, window("dynamic_normal"));
    let dynamic_low = delta(&scenario, energy_source, window("dynamic_low_priority"));
    let fixed_normal = delta(&scenario, energy_source, window("fixed_normal"));
    let fixed_low = delta(&scenario, energy_source, window("fixed_low_priority"));

    assert_close(
        dynamic_normal,
        expected_f64(&truth, "dynamic_normal_joules"),
    );
    assert_close(
        dynamic_low,
        expected_f64(&truth, "dynamic_low_priority_joules"),
    );
    assert_close(fixed_normal, expected_f64(&truth, "fixed_normal_joules"));
    assert_close(fixed_low, expected_f64(&truth, "fixed_low_priority_joules"));
    assert!(dynamic_low < dynamic_normal);
    assert!(fixed_low > fixed_normal);
    assert!(
        window("dynamic_low_priority").end_tick - window("dynamic_low_priority").start_tick
            > window("dynamic_normal").end_tick - window("dynamic_normal").start_tick
    );
}

#[test]
fn carryover_fixture_exposes_fixed_order_bias() {
    let (truth, scenario) = load_truth("carryover_confounding.truth.json");
    let source_id = truth.primary_source_id.unwrap();
    let window = |name| truth.windows[name];

    let predecessor = delta(&scenario, source_id, window("hot_predecessor"));
    let immediate = delta(&scenario, source_id, window("p2_immediate"));
    let rested = delta(&scenario, source_id, window("p2_rested"));

    assert_close(predecessor, expected_f64(&truth, "hot_predecessor_joules"));
    assert_close(immediate, expected_f64(&truth, "p2_immediate_joules"));
    assert_close(rested, expected_f64(&truth, "p2_rested_joules"));
    assert_close(
        immediate - rested,
        expected_f64(&truth, "immediate_minus_rested_joules"),
    );
    assert!(immediate < rested);

    let idle_source_id = truth.idle_source_id.unwrap();
    assert_close(
        source_value(&scenario, idle_source_id, window("p2_immediate").start_tick),
        expected_f64(&truth, "immediate_deep_idle_percent"),
    );
    assert_close(
        source_value(&scenario, idle_source_id, window("p2_rested").start_tick),
        expected_f64(&truth, "rested_deep_idle_percent"),
    );
}

#[test]
fn treatment_timing_fixture_requires_a_matched_control() {
    let (truth, scenario) = load_truth("treatment_timing_confounding.truth.json");
    let source_id = truth.primary_source_id.unwrap();
    let window = |name| truth.windows[name];

    let quiet_control = delta(&scenario, source_id, window("quiet_control"));
    let busy_control = delta(&scenario, source_id, window("busy_control"));
    let quiet_treatment = delta(&scenario, source_id, window("quiet_treatment"));
    let busy_treatment = delta(&scenario, source_id, window("busy_treatment"));

    assert_close(
        quiet_treatment,
        expected_f64(&truth, "quiet_treatment_joules"),
    );
    assert_close(
        busy_treatment,
        expected_f64(&truth, "busy_treatment_joules"),
    );
    assert_close(
        busy_treatment - quiet_treatment,
        expected_f64(&truth, "naive_busy_minus_quiet_joules"),
    );

    let quiet_incremental = quiet_treatment - quiet_control;
    let busy_incremental = busy_treatment - busy_control;
    assert_close(
        quiet_incremental,
        expected_f64(&truth, "quiet_incremental_joules"),
    );
    assert_close(
        busy_incremental,
        expected_f64(&truth, "busy_incremental_joules"),
    );
    assert_close(quiet_incremental, busy_incremental);
}

#[test]
fn quality_faults_are_reproducible_raw_evidence() {
    let (truth, scenario) = load_truth("quality_faults.truth.json");
    let expected = &truth.expected;
    let batches = batches_by_tick(&scenario);

    let wrapped_id = expected["wrapped_source_id"].as_u64().unwrap() as u32;
    let wrapped_prefix = expected["wrapped_prefix"].as_array().unwrap();
    for (tick, value) in wrapped_prefix.iter().enumerate() {
        let sample = batches[tick]
            .samples
            .iter()
            .find(|sample| sample.source_id == wrapped_id)
            .unwrap();
        assert_eq!(sample.value, RawValue::F64(value.as_f64().unwrap()));
    }

    let gapped_id = expected["gapped_source_id"].as_u64().unwrap() as u32;
    for tick in expected["missing_ticks"].as_array().unwrap() {
        let tick = tick.as_u64().unwrap() as usize;
        assert!(batches[tick]
            .samples
            .iter()
            .all(|sample| sample.source_id != gapped_id));
    }

    let disappearing_id = expected["disappearing_source_id"].as_u64().unwrap() as u32;
    let last_present = expected["last_present_tick"].as_u64().unwrap() as usize;
    assert!(batches[last_present]
        .samples
        .iter()
        .any(|sample| sample.source_id == disappearing_id));
    assert!(batches[last_present + 1..].iter().all(|batch| batch
        .samples
        .iter()
        .all(|sample| sample.source_id != disappearing_id)));

    let [marker] = scenario.record_events.as_slice() else {
        panic!("quality fixture must contain exactly one scheduled record event");
    };
    assert_eq!(
        marker.event_id,
        expected["delayed_marker_event_id"].as_str().unwrap()
    );
    assert_eq!(
        marker.intended_tick,
        expected["marker_intended_tick"].as_u64().unwrap()
    );
    assert_eq!(
        marker.record_after_tick,
        expected["marker_record_after_tick"].as_u64().unwrap()
    );
    assert!(marker.record_after_tick > marker.intended_tick);

    let drifting_id = expected["drifting_source_id"].as_u64().unwrap() as u32;
    let drifting_tick_9 = batches[9]
        .samples
        .iter()
        .find(|sample| sample.source_id == drifting_id)
        .unwrap();
    assert_eq!(
        drifting_tick_9.monotonic_ns,
        expected["tick_9_timestamp_ns"].as_u64().unwrap()
    );

    let aliased_id = expected["aliased_source_id"].as_u64().unwrap() as u32;
    let aliased_value = expected["aliased_observed_value"].as_f64().unwrap();
    let aliased = batches
        .iter()
        .flat_map(|batch| &batch.samples)
        .filter(|sample| sample.source_id == aliased_id)
        .collect::<Vec<_>>();
    assert_eq!(aliased.len(), 5);
    assert!(aliased
        .iter()
        .all(|sample| sample.value == RawValue::F64(aliased_value)));
}
