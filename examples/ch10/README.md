# Ch10: iterative reasoning and plan revision

This is an isolated **ch10-proposal-v0** deterministic teaching example, under the repository MIT license. It is not the book's shared Plan or evidence schema. No network, LLM, real payment, or real ticket service is called. `unsafe_full` is an intentionally unsafe in-memory negative control.

From the repository root, with Python 3.11+ and PyYAML installed:

```powershell
python examples/ch10/run_experiment.py --out examples/ch10/results
python -m pytest tests/ch10 -q
```

The committed `task_chains.jsonl` contains 10 original 11-node chains. Three goal changes, three tool failures, two resource changes, a permanent stall and an unknown outcome are injected before step 6. Each chain has a distinct task ID and business effect key, but repeated scenarios share a skeleton; this is not a representative production benchmark.

Main strategies: `fixed`, `global` (all unfinished nodes), `local` (conservative unfinished descendant closure). All three retain side-effect protection. In a pure chain the latter two scopes coincide. A separate branching behavior test demonstrates preservation of unrelated future nodes; no performance benefit is inferred from that test.

Outputs: `runs.jsonl` (40 rows: 30 main, 10 negative control), `traces.jsonl`, `summary.json`, `plan_revision_log.jsonl`, `Tab-10-02.tsv`, `environment.json`. The dedicated root `plan_revision_log.jsonl` is updated only for the default results directory. For a repeat run use a private `--out` path and compare deterministic outputs; wall_seconds is intentionally not deterministic. Existing input is read, not silently regenerated.

Budget: 40 synthetic ticks; actions and checkpoints cost 1 each; planning costs one per replacement node. Max installed revisions 3, no-progress limit 3. The simulator reads `checkpoint_policy.yaml`. Tick values are not milliseconds, tokens or money. Safety stop/escalation is not business completion. A mock verifier reads world outcomes separately from planner completion flags; it is not independent human review.

Deterministic reference results: fixed 0/10 business completions; global/local each 8/10 with 0 duplicate effects, 1 stall stop and 1 escalation. The unsafe negative control produces 8 duplicate effects and completes 0/10. See raw logs for denominators and costs. There is no model-performance or production-reliability claim.

Limitations: single thread, memory-only side-effect ledger, no crash recovery, permission provider or remote reconciliation. Unknown outcome is an injected signal that immediately escalates; no remote write was sent.

## Case-10-05: branched revision

```powershell
python examples/ch10/branch_revision.py
```

This separate deterministic structural case starts with an already confirmed three-node prefix and a current budget of 40 ticks. First-observation checking costs 1; local repair replaces 6 nodes and leaves 33, while global repair replaces 8 and leaves 31. Four read-only mock actions with their checkpoints leave the local run at 25. A second observation costs 1; replacing 4 remaining nodes leaves 20. Outputs are in `branch_results/`.

The case checks history and unrelated-branch preservation, merge dependencies, two version transitions and rejection of a reused observation. Its future suffix is not executed, and `business_task_accepted` remains false. It is not added to the 10-task experimental denominator; its 40-tick window begins after the supplied completed prefix, so do not compare its balance directly with Exp-10-01's end-to-end balance.
