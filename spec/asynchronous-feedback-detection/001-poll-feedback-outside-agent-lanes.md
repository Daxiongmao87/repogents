# Poll Feedback Outside Agent Lanes

Changes the polling boundary established by `spec/per-repository-async-scheduling/001-run-repositories-concurrently.md` and preserves the durable feedback and quiet-period contracts in `spec/repository-agent-mvp/008-feedback-resolution.md` and `spec/repository-agent-mvp/009-quiet-period-notification.md`. Repository-local agentic execution remains serial; GitHub observation does not occupy that execution lane.

## Contract

- Every enabled repository's open, application-owned pull requests are polled for current status and all supported feedback types on the scheduler interval, independently of any repository agent lane.
- Detection performs no model inference, sandbox execution, validation, publication, or response mutation.
- Detected feedback is durably deduplicated before agentic work. Pending feedback cancels any active quiet generation and moves a waiting, quiet, or notified run to `resolving_feedback` so the visible state reflects the observed GitHub state immediately.
- Agentic feedback evaluation and resolution remain serialized with all other agentic work inside that repository and retain stored priority/focus ordering.
- Repeated polls and concurrent lane activity cannot duplicate feedback versions or start concurrent agentic work for one repository.
- Feedback persistence, quiet cancellation, and the run-state transition are one atomic database operation; concurrent due-time quiet verification observes either the prior state or the complete feedback transition.
- A failure polling one pull request is reported as a bounded scheduler error and does not prevent observation of other pull requests or repository execution.

## Acceptance Criteria

- [x] Feedback arriving on a lower-priority pull request is detected while higher-priority agentic work is active in the same repository.
- [x] Detection persists the feedback, cancels the active quiet generation, and changes the run from `quiet_period` to `resolving_feedback` without starting model or sandbox work.
- [x] Agentic work remains single-lane within each repository while different repositories remain independent.
- [x] Duplicate observations remain idempotent across scheduler ticks and restart.
- [x] A polling failure for one pull request does not stop polling other open pull requests.
- [x] Concurrent feedback detection and due-time quiet verification cannot create a notification, lose feedback, or raise an invalid transition.

## Verification

- [x] `UNIT` — block agentic execution for one run and prove a same-repository quiet run is polled again before the first run is released.
- [x] `INTEGRATION` — ingest feedback for a quiet run and prove durable feedback, quiet cancellation, and the `resolving_feedback` transition occur without evaluator or executor calls.
- [x] `RACE` — synchronize background detection with an expiring quiet check and prove one feedback version, one transition, no notification, and no exception.
- [x] `REGRESSION` — run scheduler, feedback, quiet-period, lifecycle, and application suites.
- [x] `LIVE` — deploy the daemon and prove Martite PR #9 is observed and leaves the stale quiet state while other Martite agentic work remains active.
