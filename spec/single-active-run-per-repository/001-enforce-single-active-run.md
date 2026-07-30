# Enforce One Active Run Per Repository

Supersedes the same-repository multi-run advancement behavior in `spec/per-repository-async-scheduling/001-run-repositories-concurrently.md` and the corresponding normal-order and force behavior in `spec/issue-priority-queue/001-draggable-issue-priority-and-focus.md`. Different repositories remain independently concurrent.

## Contract

A repository run is active while implementing, validating, publishing, or resolving feedback. Each repository has at most one active run. Other issue runs remain durably queued and do not alternate into the repository lane merely because the active controller action returns or waits for an automatic retry.

The active run retains the repository lane until it reaches waiting for feedback, blocked, canceled, or closed. Feedback and issue-revision polling remain independent and durable while the lane is occupied, but discovered work for a sibling run waits without starting agentic execution or source mutation. A forced sibling may take the lane only at a safe orchestration boundary; the displaced run retains its exact resumable lifecycle phase, checkout, workflow, retry, and evidence state.

Existing databases with more than one active run for a repository are reconciled deterministically during migration. Forced selection wins when present; otherwise stored priority, creation time, and run ID select the retained active run. The database rejects any later attempt to persist a second active run for the same repository.

Repository and run projections distinguish the one active run from queued siblings. The repository active-run count never exceeds one, and a queued sibling is not presented as simultaneously implementing.

## Acceptance Criteria

- [x] At most one run per repository can be persisted in an active lifecycle state, while different repositories may each have one active run.
- [x] An active run keeps its repository lane across controller yields and automatic-retry delays; sibling runs do not round-robin and begin only after an idle or terminal boundary.
- [x] Feedback and issue-revision discovery for sibling runs remain durable while the lane is occupied, without activating concurrent agentic work.
- [x] Forced work transfers the lane only at a safe boundary and preserves the displaced run's exact resumable phase and durable checkout/workflow evidence.
- [x] Migration deterministically reconciles existing duplicate active runs without deleting run history, and restart preserves the resulting owner and queued resumable siblings.
- [x] API and dashboard state expose no more than one active run per repository and do not label queued siblings as simultaneously implementing.

## Verification

- [x] `MIGRATION` — migrate schema v22 data containing two active same-repository runs; retain the forced-or-priority winner, queue the sibling with its resumable phase, preserve both run histories, reject a second active state, and report no foreign-key violations.
- [x] `UNIT` — make the first same-repository run yield while remaining active and prove repeated scheduler ticks do not execute the sibling; move the first to an idle boundary and prove the sibling then starts.
- [x] `UNIT` — prove two repositories still execute concurrently and a forced sibling safely displaces and later resumes the prior active run without losing its phase.
- [x] `UNIT` — ingest sibling feedback and issue revision while another run owns the lane; prove discovery persists but active resolution waits for the lane.
- [x] `STATE/CLIENT` — project one active run and queued sibling with `active_run_count == 1`, then inspect the rendered dashboard state.
- [x] `REGRESSION` — run focused database, lifecycle, application, feedback, and interface tests, then compile and run the complete deterministic suite.
- [x] `LIVE` — restart `repogents.service`; prove the live duplicate active states are reconciled to one owner plus a resumable queued sibling, durable rows and queue/focus state are preserved, the API/dashboard show one active run, and no same-repository worker overlap occurs.
