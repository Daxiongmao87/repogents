# View a Specific Issue Run Log

Changes the repository-only activity contract in `spec/repository-management-ui/001-repository-controls-and-observability.md` and reuses the SSE transport in `spec/live-activity-streaming/001-server-sent-activity-stream.md`.

## Contract

- Every issue-run card exposes a **View log** action identified by its durable run ID.
- `GET /api/runs/<run-id>/logs` returns only that run's issue identity, run state, transitions, and bounded redacted agent action history. It never substitutes the repository's latest run.
- `GET /api/runs/<run-id>/events` streams the same snapshot format through the existing activity-revision SSE mechanism.
- Selecting another run closes the prior run stream and opens one stream for the selected run. The log follows new entries while at the bottom and preserves position after the user scrolls upward.
- Unknown or archived-inventory run IDs return 404. Existing host/origin protections remain unchanged, and raw secret values never enter the response.
- Repository-level activity remains available; this item adds only the missing run-specific view.

## Acceptance Criteria

- [x] A user can select any visible issue run and see its own log.
- [x] The snapshot and stream contain no transitions or action entries from another run in the same repository.
- [x] Historical visible runs remain viewable when they are no longer the repository's latest run.
- [x] Switching runs switches the stream without leaving duplicate live connections.
- [x] Unknown runs return a bounded 404 response and redaction remains intact.

## Verification

- [x] `UNIT` — seed two runs in one repository and prove each run-log snapshot contains only its own transitions and action history.
- [x] `UNIT` — seed a raw run-specific known secret in stored action history and prove both snapshot and stream projections redact it.
- [x] `HTTP` — fetch run snapshots/streams and prove valid, unknown, and archived-inventory behavior.
- [x] `CLIENT` — click two different issue-run cards, observe the displayed issue identity and entries change, and verify scroll-follow behavior.
- [x] `REGRESSION` — run focused application, interface, and live-activity suites.
