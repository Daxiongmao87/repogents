# Issue Activation and Run Lifecycle

Implements `MVP.md` §5. This item owns activation identity, deterministic run creation, state transitions, restart and automatic recovery, blocking, and cancellation. Its former user-triggered Retry requirements are superseded by `spec/remove-manual-retry/001-remove-manual-retry.md`.

## Contract

- Poll ready repositories for GitHub timeline events that apply `agent:ready`; the stable event identity, not current label presence, activates work.
- In one transaction, an unseen activation creates one run referencing immutable repository, sandbox, and team versions plus intended base branch and base SHA.
- Each repository issue has at most one nonterminal run. Removing the label does not cancel work; a later label event may activate only after the prior run is terminal.
- Allowed states are `queued`, `implementing`, `validating`, `publishing`, `waiting_for_feedback`, `resolving_feedback`, `quiet_period`, `notified`, `blocked`, `canceled`, and `closed` with explicit transition validation.
- Automatic recovery resumes the same run after reconciliation. Cancellation retains evidence and remote objects while terminating supervised processes.

## Acceptance Criteria

- [x] One unseen activating label event creates exactly one durable run with stored base and immutable version references.
- [x] Repeated polling and process restart cannot duplicate a run for the same event.
- [x] A second nonterminal run for the same repository issue is rejected.
- [x] Every run-state transition follows the defined lifecycle; blocked, canceled, and closed runs record reasons.
- [x] Restart reconciles and resumes the existing nonterminal run from its last completed durable boundary.
- [x] Automatic recovery reconciles before repeating work; cancellation terminates work, marks the run terminal, and preserves logs, sandbox/cache state, branches, and pull requests.
- [x] Activation obtains and retains the exact remote base object even when the default branch advanced after onboarding or a later re-onboarding replaces the source snapshot.
- [x] Automatic recovery resumes the durable state whose work was interrupted, including revision work returned from validation or publication.
- [x] Cancellation is durable before process termination and no in-flight inference, tool, validation, or publication work continues across a cancellation boundary.

## Verification

- [x] `UNIT` — transition-table tests accept every specified path and reject invalid transitions.
- [x] `ADAPTER` — GitHub label timeline events retain stable identities across repeated responses.
- [x] `INTEGRATION` — repeated polls plus application restart produce one run; automatic recovery and cancellation preserve identity and evidence.
- [x] `CLIENT` — current state, last completed state, reasons, and cancel are usable through the local interface.
- [x] `INTEGRATION` — activate after the stored source becomes stale and reconstruct the exact run base after re-onboarding.
- [x] `INTEGRATION` — return revision work from validation/publication automatically to implementation, and cancel during inference and external boundaries without later side effects.
