# Repository Controls and Observability

Changes the local-interface contract established by `spec/repository-agent-mvp/010-local-interface.md` and the inventory contract established by `spec/repository-agent-mvp/002-repository-onboarding.md`. This item adds repository lifecycle controls and operator-facing runtime visibility; it does not change issue execution, validation, publication, or feedback semantics.

## Contract

- The inventory UI provides explicit add, remove, enable, and disable controls. Every mutation uses the existing canonical-origin and anti-CSRF protections.
- Disabling a repository prevents polling it for new issue activations. Existing runs continue to their normal terminal or waiting state and remain visible. Re-enabling resumes polling without rebuilding the stored sandbox or team.
- Removing a repository archives it from the active inventory without destroying its durable history or application-owned files. Removal is rejected while the repository has a nonterminal run; the operator must cancel or finish that run first. Adding the same archived repository restores it and its current stored versions.
- Every active inventory record shows whether it is enabled, whether it has active work, its onboarding state and reason, its current run state, and its latest activity time.
- Selecting a repository opens a detail view with a bounded, live-refreshing activity log. The log combines durable run transitions with the active run's redacted action history, refreshes at least every two seconds, scrolls to new output while the viewer is at the end, and preserves the viewer's position after they scroll upward.
- When a current team exists, the repository detail shows every member's stable identity, role, responsibilities, runtime, and model. Expanding a member reveals the exact stored role prompt (`instructions`).
- Repository logs and team prompts are available only through the local interface and do not expose resolved secret values.

## Acceptance Criteria

- [x] A repository can be added from the inventory UI and appears with its onboarding status.
- [x] A repository can be disabled and re-enabled; disabled repositories remain visible and are excluded from new-activation polling.
- [x] An inactive repository can be removed from active inventory without deleting durable history, while removal of a repository with nonterminal work is rejected with an actionable message.
- [x] Re-adding an archived repository restores it without duplicating its identity or discarding current sandbox and team versions.
- [x] Each repository visibly reports enabled/disabled state, onboarding status and reason, active/idle state, current run state, and latest activity.
- [x] Selecting a repository displays a bounded live activity log that refreshes and follows new output without overriding a viewer who scrolled upward.
- [x] A repository with a current team displays every member's identity and role metadata.
- [x] Expanding a team member displays that member's stored role prompt.
- [x] Existing run, notification, re-onboarding, cancellation, acceptance-evidence, origin, and CSRF behavior remains available.

## Verification

- [x] `UNIT` — schema migration preserves existing repositories as enabled and supports archived inventory records.
- [x] `UNIT` — enable, disable, remove, restore, active-run rejection, and scheduler filtering follow the contract.
- [x] `UNIT` — state and log responses expose bounded repository status, team data, transitions, and redacted active action history.
- [x] `HTTP` — repository mutations reject invalid identifiers, unauthorized origins, and missing CSRF tokens and return bounded JSON errors.
- [x] `CLIENT` — add, disable, enable, and remove repositories through the rendered browser controls and observe state updates.
- [x] `CLIENT` — select a repository, observe live log updates and automatic scrolling, then scroll upward and verify the position is preserved.
- [x] `CLIENT` — inspect team roles and expand a member to read the stored role prompt.
- [x] `REGRESSION` — run the focused database, application, interface, onboarding, and lifecycle suites.
