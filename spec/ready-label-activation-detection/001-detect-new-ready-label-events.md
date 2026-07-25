# Detect New Ready-Label Events

Repairs the activation-polling contract established by `spec/repository-agent-mvp/005-run-lifecycle.md`.

## Contract

- Every enabled, ready, inventoried repository is polled for newly applied `agent:ready` issue-label events during normal scheduler ticks and after application restart.
- A label event observed after earlier polls creates exactly one durable run for that issue and queues the stored lead; detection does not depend on the issue existing when the repository was onboarded or when the service started.
- The client retrieves every event page, and durable GitHub event identity prevents repeated polls and timestamp ties from skipping or duplicating activations.
- Disabled, paused, blocked, removed, and still-onboarding repositories do not activate work.
- GitHub/API failures remain visible and retryable; they do not suppress detection on a later successful poll.

## Acceptance Criteria

- [x] Applying `agent:ready` to a new issue in an enabled ready repository is detected on a later scheduler tick.
- [x] The activating event creates one run with the repository's current sandbox/team versions and snapshots the current issue discussion.
- [x] Repeated polling and restart do not duplicate the run.
- [x] A transient GitHub failure does not cause the later successful poll to miss the label event.
- [x] The deployed application detects a live fixture label event without manual database intervention.

## Verification

- [x] `UNIT` — poll once with no activation, then expose a newly labeled issue event and prove the next poll creates exactly one run.
- [x] `UNIT` — exercise pagination/checkpoint and transient-failure boundaries, then prove the event is detected once.
- [x] `INTEGRATION` — restart between the empty poll and event observation and prove detection still occurs.
- [x] `LIVE` — apply or observe a fresh `agent:ready` fixture event, wait for the configured poll interval, and inspect the durable run and transition evidence.
- [x] `REGRESSION` — run focused GitHub, lifecycle, application, and scheduler suites.
