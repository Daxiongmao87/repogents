# Quiet-Period Notification

Implements `MVP.md` §9. Quiet periods are durable generations based on successful GitHub observations, not elapsed local time alone.

## Contract

- The first generation starts only after the initial PR head is confirmed. Later generations start only after all observed feedback is resolved/published/responded and an immediate repoll finds no pending feedback.
- New feedback cancels the current deadline and returns the same open run to resolution, including after notification.
- At or after the 30-minute deadline, a successful GitHub poll must confirm no newer/pending feedback and an open PR before exactly one durable notification is created.
- GitHub/application outage delays verification and notification; it never counts unobserved time as quiet.
- External PR closure/merge closes the run, cancels quiet time, and prevents notification for that interrupted generation.

## Acceptance Criteria

- [x] Quiet-period generations and UTC deadlines persist across restart.
- [x] New feedback atomically invalidates the current generation before resolution begins.
- [x] No notification is created before 30 continuous successfully verified feedback-free minutes.
- [x] A qualifying generation creates exactly one persistent notification and marks the run `notified`.
- [x] Poll failure delays notification; external close/merge closes the run without notification.
- [x] Feedback after notification reuses the same run and may create another generation and notification.
- [x] A transient due-time GitHub failure leaves the run in `quiet_period` with the same active generation and deadline for automatic retry.
- [x] External close/merge updates the quiet generation, pull request, run state, and transition evidence recoverably across interruption.
- [x] Stored deadline precision can never shorten the required continuous 30-minute interval.

## Verification

- [x] `UNIT` — a controllable clock proves deadline boundaries, reset behavior, outage delay, closure, uniqueness, and post-notification feedback.
- [x] `INTEGRATION` — restart during a quiet generation and resume the same deadline without duplication.
- [x] `INTEGRATION` — production orchestration retries a transient due check automatically without blocking or replacing the active generation.
- [x] `UNIT` — sub-second deadline boundaries and interrupted external-closure reconciliation cannot notify early or strand the run.
- [x] `LIVE` — restart during the fixture PR quiet period, verify the open PR after 30 continuous minutes, and create one linked persistent notification without merging/closing.
