# Run Repositories Concurrently

Supersedes the normal orchestration-order and global force-exclusivity portions of `spec/issue-priority-queue/001-draggable-issue-priority-and-focus.md`. The visible queue remains global and durable, but its stored order and force selection are enforced independently within each repository instead of creating one blocking execution lane for every repository.

## Contract

- Each enabled, non-removed repository has at most one active orchestration lane.
- A repository lane advances its eligible runs synchronously in stored priority order. Two runs from the same repository never execute, publish, poll feedback, or check quiet state concurrently.
- Different repositories advance concurrently. Long model, sandbox, publication, or validation work in one repository does not delay feedback polling, quiet checks, or issue work in another repository.
- Repository lanes have no global completion barrier: an idle lane can be scheduled again without waiting for another repository's long-running lane to finish.
- Force selection is repository-scoped. Each repository may focus at most one nonterminal run; that focus excludes only sibling runs in the same repository. Other repository lanes continue, and a force change takes effect at the next safe orchestration boundary without killing an already-running process.
- Activation polling, repository pause/removal, cancellation, restart recovery, process supervision, and durable queue ordering retain their existing contracts.

## Acceptance Criteria

- [x] Two repositories can have orchestration work in flight concurrently.
- [x] Runs belonging to one repository advance one at a time in stored priority order.
- [x] Feedback polling and resolution for one repository proceed while another repository is blocked in issue execution.
- [x] A completed repository lane can run again without waiting for another repository's active lane.
- [x] Repository-scoped force behavior permits independent repositories to continue while preserving repository pause/cancellation boundaries.
- [x] The deployed daemon resumes pending Codex feedback independently of active work in another repository.

## Verification

- [x] `UNIT` — block issue execution in repository A and prove repository B enters feedback resolution before A is released.
- [x] `UNIT` — seed two runs in one repository and prove the second does not advance until the first returns, preserving priority order.
- [x] `UNIT` — prove a completed repository lane is rescheduled while another repository lane remains blocked.
- [x] `UNIT` — focus one repository's run and prove another repository's runnable lane still advances concurrently.
- [x] `REGRESSION` — run focused application, feedback, lifecycle, pause, and execution suites, then the complete suite.
- [x] `LIVE` — restart the configured daemon and observe distinct repository work progress concurrently without manually rewriting run state.
