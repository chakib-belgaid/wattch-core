# Result contract

Return a table with one row per retained trial and these fields:

```text
condition
model_id
reasoning_effort
skill_enabled
prompt_sha256
candidate_sha256
command_argv_sha256
outcome_type
trial_index
execution_order
run_id
artifact_path
correct
completed_work
duration_s
source_uid
backend
sampling_mode
evidence_class
energy_joules_or_null
descriptive_local_run_eligible
physical_claim_eligible
failure_or_warning
```

Then report:

- the exact correctness and workload command;
- the common base tree plus prompt, candidate, and command hashes;
- whether skill invocation was observed for a skill treatment;
- the marker window and single-source boundary;
- repetitions and execution order;
- median and raw trial values, not only a percentage;
- failed/missing trials and capture-quality warnings;
- the optimization mechanism;
- a narrowly worded conclusion and explicit non-claims.

Allowed synthetic conclusion:

> The MCP/CLI workflow produced valid replayable artifacts. The source was synthetic, so no physical energy ranking is reported.

Allowed exploratory local-domain conclusion when every schema-v1 gate passes:

> In this exploratory local-domain estimate for the frozen host, source, and workload, the candidate had a lower median source-domain outcome while preserving declared output and useful work. This is not a confirmatory physical-energy claim.

Never say the result is the model's datacenter energy, total-machine energy, carbon footprint, or process-private consumption unless those boundaries were independently measured.
