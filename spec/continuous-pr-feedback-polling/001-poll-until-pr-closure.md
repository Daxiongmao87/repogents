# Poll Pull Requests Until Closure

Supersedes the deadline and notification behavior in `spec/repository-agent-mvp/009-quiet-period-notification.md` and the quiet-period portions of `spec/repository-agent-mvp/011-mvp-acceptance.md`. It changes `spec/asynchronous-feedback-detection/001-poll-feedback-outside-agent-lanes.md` only by removing quiet-generation cancellation and due-time notification races; its independent open-pull-request polling boundary remains authoritative. The feedback identity, evaluation, revision, and response contract in `spec/repository-agent-mvp/008-feedback-resolution.md` remains unchanged.

## Contract

- After initial publication and after every completed feedback cycle, an open pull request remains in `waiting_for_feedback`; elapsed time never advances, completes, or terminates the run.
- Every enabled repository's open application-owned pull requests continue to be polled on the scheduler interval independently of repository-local agent work. New feedback durably moves the same run to `resolving_feedback` and is handled by the existing stored team.
- GitHub reporting the pull request merged moves the monitoring run to terminal `closed` with no replacement. GitHub reporting it closed without merge closes that attempt; if the linked issue is still open, Repogents atomically records one stable close-trigger activation and starts exactly one fresh run from the repository's current stored sandbox, team, default branch, and base SHA. If the issue is also closed, no replacement run is created.
- Repeated status polls and daemon restarts cannot duplicate a closed-PR replacement run. The prior run, branch, pull request, feedback, and validation evidence remain immutable and linked to the completed attempt.
- No new quiet-period generation, deadline, quiet notification, or `notified` lifecycle state is created or exposed. The local interface presents the open run's monitoring state without quiet-notification controls.
- On upgrade, legacy `quiet_period` and `notified` runs with open pull requests move to `waiting_for_feedback`, active quiet generations are canceled, and existing pull-request, feedback, transition, and historical notification records are preserved.

## Acceptance Criteria

- [x] Initial publication and completed feedback resolution leave an open run in `waiting_for_feedback` without creating a quiet period or notification.
- [x] Scheduler polling continues for every open application-owned pull request until GitHub reports it merged or closed, including while another run owns the repository execution lane.
- [x] Feedback arriving after any elapsed interval is stored once and returns the same run from `waiting_for_feedback` to `resolving_feedback`.
- [x] A merged pull request moves the same run to terminal `closed` without replacement; elapsed time alone never does.
- [x] A pull request closed without merge closes the prior run and creates one fresh queued run only when the linked GitHub issue is still open.
- [x] The closed-PR replacement uses a stable activation identity and current repository sandbox/team/base references; repeated polling and restart preserve exactly one replacement while retaining all prior-attempt evidence.
- [x] Schema migration converts legacy monitoring states and cancels active deadlines without deleting durable pull-request, feedback, transition, or historical notification evidence.
- [x] The local interface no longer advertises quiet-period notifications or exposes notification acknowledgment controls.

## Verification

- [x] `UNIT` — publication/feedback-cycle completion remains `waiting_for_feedback` with zero quiet-period and notification rows.
- [x] `INTEGRATION` — repeated scheduler ticks poll an open waiting run, ingest later feedback exactly once, and retain the same run identity.
- [x] `INTEGRATION` — concurrent repository-lane work does not suppress open-pull polling; merged status closes the monitored run without a replacement.
- [x] `INTEGRATION` — closed-unmerged status plus an open issue creates one fresh run, while a closed issue creates none; repeated polling and restart cannot duplicate the replacement.
- [x] `MIGRATION` — schema upgrade maps legacy `quiet_period`/`notified` rows to `waiting_for_feedback`, cancels active generations, and preserves associated durable evidence.
- [x] `CLIENT` — the dashboard shows the waiting run and contains no quiet-period notification panel or acknowledgment route.
- [x] `REGRESSION` — focused lifecycle, feedback, scheduler, database, interface, and complete project suites pass.
- [x] `LIVE` — the Websesh fixture reaches an open `waiting_for_feedback` run, later real feedback is resolved on that same run, and no quiet deadline or notification is created.
