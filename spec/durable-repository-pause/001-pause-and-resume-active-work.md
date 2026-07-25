# Pause and Resume Active Repository Work

Supersedes the disable semantics in `spec/repository-management-ui/001-repository-controls-and-observability.md`, especially its contract that existing runs continue after disabling. The existing `enabled` storage field becomes the durable scheduling gate behind the user-facing Pause/Resume control.

## Contract

- The inventory and repository detail views expose **Pause** while a repository is running or eligible to run and **Resume** while it is paused. They do not describe this operation as merely disabling future monitoring.
- Pausing is a durable repository-wide scheduling barrier. It atomically marks the repository paused, prevents new issue activation polling, prevents every nonterminal run for that repository from advancing, and requests termination of controller-owned model and sandbox process trees currently executing for those runs.
- A pause is not cancellation or failure. It does not move a run to `canceled`, `blocked`, or `closed`, discard its checkout, clear its action history, invalidate completed validation, cancel an open pull request, or erase a quiet-period deadline.
- If a model or sandbox process is active for one of those runs, Pause terminates that process tree. The worker observes the durable pause after the interrupted call and returns without recording the interruption as a run failure.
- Resume clears the scheduling barrier and wakes the scheduler. Existing nonterminal runs continue from their persisted state; no new run, branch, or pull request is created.
- Process termination is scoped to the paused repository's run identifiers. Other repositories continue normally.

## Acceptance Criteria

- [x] The dashboard uses Pause/Resume terminology and visibly distinguishes paused repositories from idle active repositories.
- [x] Pausing excludes the repository from both new activation polling and advancement of all existing nonterminal runs.
- [x] Pausing requests termination of every active controller-owned model and sandbox process tree for that repository without canceling its runs.
- [x] An interrupted worker returns at its next safe boundary without changing the run to `blocked`, `canceled`, or `closed`.
- [x] Run checkout, action history, state, validation evidence, pull-request identity, feedback state, and quiet-period generation survive pause.
- [x] Resuming wakes scheduling and continues each run from its durable state without duplicated external identities.
- [x] Pausing and resuming one repository does not interrupt or suppress another repository.

## Verification

- [x] `UNIT` — pause a repository with a queued run and prove the same scheduler tick neither polls nor advances it while another enabled repository still advances.
- [x] `UNIT` — pause during a supervised model process and during a sandbox command; prove both process groups terminate, run state is unchanged, and no later action starts.
- [x] `UNIT` — resume persisted paused work after recreating the application and prove the same run advances without duplicate activation or PR identity.
- [x] `HTTP` — Pause/Resume mutations retain canonical-origin and CSRF enforcement and return the durable paused state.
- [x] `CLIENT` — drive Pause and Resume in a real browser and observe activity stop, paused status persist across reload, and the same run continue after resume.
- [x] `REGRESSION` — run focused application, interface, execution, lifecycle, and controller suites.
