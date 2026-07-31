# Correct Invalid Agent Workflow Output In Turn

Extends `spec/agent-workflow-graphs/002-durable-ready-node-scheduler.md`. The prior contract correctly rejects executable or secret configuration from persisted workflow outputs, but currently performs that validation only after an agent action cycle returns. This item changes the rejection boundary: an invalid `finish.output` is returned to the same bounded agent action cycle for correction before the durable workflow attempt is failed.

## Acceptance Criteria

- [x] An agent workflow node whose `finish.output` violates its expected schema or protected-output policy receives a concrete safe rejection in the same action cycle and may submit a corrected output without an outer run retry.
- [x] The invalid finish is not recorded as member completion or persisted as node output; the corrected output is committed once and downstream workflow execution may continue.
- [x] Executable configuration keys, secret references, and resolved secret values remain rejected rather than being persisted or exposed.
- [x] Existing retrying runs recover from this validation failure after deployment without repeating completed repository edits.

## Verification

- [x] `INTEGRATION` — submit a workflow finish containing passive test evidence under a forbidden `command` key, then a corrected scenario/result payload; prove one durable node attempt succeeds and the graph advances.
- [x] `SECURITY` — prove the rejected output and any protected value are absent from durable node output while the correction reason is safely recorded for the agent.
- [x] `REGRESSION` — run the focused execution/workflow tests and complete deterministic project suite.
- [x] `LIVE` — restart the deployed service and prove issues #4 and #5 advance beyond the repeatedly failed `implement-state-lineage` output boundary with preserved checkout work and durable attempt evidence.
