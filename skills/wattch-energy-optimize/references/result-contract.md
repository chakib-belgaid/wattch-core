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
claim_eligible
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

Allowed local-domain conclusion when calibrated evidence exists:

> For this host, source boundary, frozen workload, and repeated trial set, the candidate had a lower median source-domain energy outcome while preserving the declared output and useful work.

Never say the result is the model's datacenter energy, total-machine energy, carbon footprint, or process-private consumption unless those boundaries were independently measured.
