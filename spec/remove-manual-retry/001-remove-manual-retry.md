# Remove Manual Run Retry

This item changes the contract established by `MVP.md` §§5.3 and 10 and by `spec/repository-agent-mvp/005-run-lifecycle.md`, `010-local-interface.md`, and `011-mvp-acceptance.md`. The user-directed delta removes user-triggered run retry. Those prior items remain historical records; this item supersedes only their manual-retry requirements.

## Contract

- The supported local interface has no Retry control and no run-retry HTTP operation.
- The application action boundary and run lifecycle expose no user-triggered retry operation.
- An internal execution action quantum, if retained, is an automatic scheduling boundary. Reaching it must not block the run or require user action; the same durable run continues automatically with its existing checkout, team, model, and evidence.
- Restart reconciliation, automatic transient-operation retry, idempotent external-operation reconciliation, explicit re-onboarding, cancellation, and durable blocked-state evidence are not removed.
- A blocked run remains visible with its reason and durable evidence, but the product must not present manual retry as recovery from an internal implementation limit.

## Acceptance Criteria

- [x] The local client presents no Retry control for any run state.
- [x] Requests to the former run-retry endpoint are rejected as unknown routes and cannot change run state or request scheduler work.
- [x] Application and lifecycle APIs contain no user-triggered run-retry operation.
- [x] Productive execution that crosses one internal action quantum continues on the same run without entering `blocked` or requiring user input.
- [x] Recoverable model, controller-tool, validation-infrastructure, and orchestration failures preserve the current durable run state and are retried automatically; only a specific irreducible prerequisite may enter `blocked`.
- [x] Cancellation, restart reconciliation, and idempotent external-operation reconciliation retain their existing behavior.
- [x] Current specifications and acceptance reporting identify the superseded manual-retry contract and do not count manually resumed execution as autonomous evidence.

## Verification

- [x] `UNIT` — application and lifecycle tests prove that no manual retry action exists while cancellation and restart reconciliation still work.
- [x] `CLIENT` — the rendered dashboard has no Retry control and `POST /api/runs/<id>/retry` returns 404 without invoking an action or scheduling work.
- [x] `INTEGRATION` — a progressing agent crosses an internal action quantum and reaches validation on the same run without a `blocked` transition or user action.
- [x] `INTEGRATION` — a transient inference failure and a recoverable controller-boundary failure each resume automatically on a later scheduler cycle without a `blocked` transition.
- [x] `INTEGRATION` — interruption at the execution-cycle boundary resumes from durable state without duplicating activation, commits, validation, pushes, pull requests, or feedback responses.
- [x] `REGRESSION` — focused application, lifecycle, execution, interface, publication, and feedback suites pass after manual-retry removal.
