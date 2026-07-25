# Draggable Issue Priority and Focus

Changes the issue/run presentation in `spec/repository-agent-mvp/010-local-interface.md`, supersedes the separate **View log** action and modal established by `spec/issue-run-logs/001-view-specific-issue-run-log.md`, and replaces the repository-level client stream established by `spec/live-activity-streaming/001-server-sent-activity-stream.md`. The run-specific redacted snapshot and SSE contracts remain authoritative.

## Contract

- **Issues and Runs** is a durable global work queue. Every visible run is represented by a full-width issue button; clicking anywhere on that button selects the exact durable run.
- Selecting an issue replaces the contents of the adjacent **Issue Log** panel with that run's bounded redacted snapshot and maintains exactly one live run SSE connection. There is no per-issue **View log** control or run-log modal.
- The former top-level **Notifications** panel becomes **Issue Log**. The selected repository detail replaces **Live activity** with **Notifications**, retaining durable acknowledgment, issue/PR links, read state, and the existing empty state.
- Queue items are draggable. A successful drop stores the complete visible order durably and state responses return that order. Reloads and daemon restarts preserve it; newly activated runs append at the bottom.
- The orchestrator considers nonterminal runs from top to bottom. Its normal tick advances runs in the stored order, preserving existing repository pause, cancellation, lifecycle, validation, publication, feedback, and quiet-period gates.
- A nonterminal queue item exposes a **Force work** option. Forcing it creates one durable global focus selection, moves it to the top, and wakes the scheduler. Until released or automatically cleared when the run reaches a blocked, canceled, closed, waiting-for-feedback, quiet-period, or notified state, scheduler ticks advance only that run.
- Forcing another run atomically transfers focus. **Release focus** returns to normal top-to-bottom scheduling. A force request never kills an already-running model or sandbox process; it takes effect at the next safe orchestration boundary.
- Queue reorder and focus mutations retain canonical-origin and CSRF protection, reject unknown, archived-inventory, duplicate, terminal-force, and malformed inputs with bounded errors, and never alter run identity or lifecycle state.
- Run-log redaction, unknown-run `404` behavior, follow-at-bottom behavior, held-scroll behavior, automatic SSE reconnection, cancellation, GitHub links, and notification acknowledgment remain intact.

## Acceptance Criteria

- [x] Every issue/run is a full-width clickable button with no **View log** control; selecting it populates the inline **Issue Log** with only that run and keeps one live stream.
- [x] The top-level second panel is **Issue Log**, while each selected repository detail shows only that repository's **Notifications** instead of **Live activity**.
- [x] Dragging issue buttons persists their global order across refresh and restart, and new runs append at the bottom.
- [x] Normal orchestration advances eligible runs in the stored top-to-bottom order.
- [x] **Force work** durably focuses one selected nonterminal run, wakes scheduling, excludes other runs until release or an idle/terminal boundary, and transfers atomically when another run is forced.
- [x] Priority/focus APIs are bounded and protected without regressing cancellation, notification acknowledgment, run logs, redaction, repository pause, or GitHub links.
- [x] Switching between repositories never displays another repository's notifications.
- [x] The configured Repogents service runs the completed queue implementation against its durable database after restart.

## Verification

- [x] `MIGRATION` — migrate populated schema v8 data, preserve every run, assign deterministic created-order priorities, persist focus/order across reopen, and append a newly activated run.
- [x] `UNIT` — reorder runs, reject malformed/duplicate/unknown/archived input, force/release eligible runs, reject terminal force, and prove state projection order and focus markers.
- [x] `UNIT` — prove normal ticks advance runs top to bottom and focused ticks advance only the forced run until automatic clearing at an idle/terminal boundary.
- [x] `HTTP` — exercise reorder and force/release routes with valid CSRF/origin and reject unauthorized or malformed requests.
- [x] `CLIENT` — drag two issue buttons, reload and observe retained order, click each full card and observe inline live-log replacement, then force and release one issue.
- [x] `CLIENT` — verify **Issue Log** replaces the old Notifications panel, Notifications replaces repository Live activity, no **View log** or run-log dialog remains, and acknowledgment/GitHub links remain usable.
- [x] `UNIT/CLIENT` — seed notifications for two repositories, switch the selected repository, and prove each detail shows only its own notifications.
- [x] `REGRESSION` — run focused database, lifecycle, application, interface, execution, notification, and pause suites, then the complete suite.
- [x] `DEPLOYMENT` — restart the configured user service, confirm schema v9 and healthy process state, and exercise the deployed queue interface without altering active run focus or priority.
