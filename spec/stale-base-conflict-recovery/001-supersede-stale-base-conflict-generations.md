# Supersede Stale Base-Conflict Generations

Changes `spec/open-pr-base-conflicts/001-detect-and-resolve-base-conflicts.md`. The earlier contract remains in force except that the intended-base SHA carried by a pull-request status response is no longer treated as authoritative when creating or preparing a synthetic base-conflict revision.

## Contract

- When an open pull request is reported unmergeable, Repogents reads the current intended-base branch head independently and keys the synthetic base-conflict generation to that current SHA.
- If the intended base advances after conflict observation but before checkout preparation, the preparation attempt reports that exact generation change. Feedback resolution repolls and retries the newer generation without executing source work for the stale generation.
- A newer base-conflict generation atomically completes any unfinished older synthetic conflict rows as superseded, records their replacement, and reconciles pending revision-batch operations that included them. Non-synthetic feedback in a superseded mixed batch remains pending and is included in the replacement batch.
- If the pull request is confirmed mergeable, unfinished synthetic conflict rows are completed as superseded without a replacement. An indeterminate mergeability result does not supersede conflict work.
- Supersession retains the old feedback row, decision, source binding, and outbound-operation record as durable evidence. It does not post a response, push a branch, create a replacement pull request, merge, or close a pull request.
- Existing handling for ordinary review feedback, non-base preparation failures, cancellation, pull-request closure, publication, and resolved conflict generations remains unchanged.

## Acceptance Criteria

- [x] A stale pull-request base SHA cannot create or execute a synthetic conflict generation when the intended-base branch has a newer current head.
- [x] A base advance between conflict observation and checkout preparation is retried against the newer generation without source execution for the stale generation.
- [x] Unfinished older synthetic conflict rows and their pending revision batches are durably superseded while unrelated feedback remains actionable in the replacement batch.
- [x] A confirmed mergeable pull supersedes unfinished synthetic conflicts without replacement, while unknown mergeability preserves them.
- [x] Restart from the previously stranded state converges without a manual retry, duplicate pull request, response, merge, or close operation.

## Verification

- [x] `REGRESSION` — reproduce a base advance between observation and preparation and prove resolution executes once against only the latest base SHA.
- [x] `INTEGRATION` — seed a stale mixed pending revision batch, restart resolution, and prove the old conflict/operation are superseded while the ordinary findings and replacement conflict complete in one new batch.
- [x] `UNIT` — prove confirmed mergeability clears stale synthetic work and indeterminate mergeability does not.
- [x] `MIGRATION` — upgrade a schema-v14 database and preserve feedback and operation evidence while adding explicit conflict-supersession links.
- [x] `REGRESSION` — run the focused feedback, publication, database, lifecycle, and orchestration suites.
- [x] `REGRESSION` — run the complete project test suite.
