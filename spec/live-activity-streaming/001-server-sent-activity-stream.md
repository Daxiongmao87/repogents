# Server-Sent Activity Stream

Changes the live-log contract established by `spec/repository-management-ui/001-repository-controls-and-observability.md`. Its one-second client polling is superseded by an event-driven Server-Sent Events stream; orchestration polling remains a separate ten-second scheduler concern.

## Contract

- Selecting a repository opens one same-origin Server-Sent Events connection and immediately receives the current bounded, redacted activity snapshot.
- A committed database state change or durable agent action-history write wakes connected streams without a fixed polling delay. Rolled-back transactions do not emit change notifications.
- The server sends a replacement bounded snapshot only when that repository's observable log content changes and sends periodic SSE comments solely to keep idle connections alive.
- Browser `EventSource` reconnection handles transient disconnects. Selecting another repository closes the prior stream before opening the next one.
- Streamed content retains the existing follow-at-bottom behavior and preserves the operator's position while older output is being reviewed.
- The dashboard makes no periodic repository-log HTTP requests. Its ten-second state refresh and the daemon's ten-second GitHub/orchestration poll remain unchanged.
- Unknown repository streams return `404`; stream data uses the same redacted repository-log projection as the existing snapshot endpoint.

## Acceptance Criteria

- [x] A selected repository receives its current activity immediately and later durable activity is pushed over SSE without waiting for a browser polling interval.
- [x] The browser maintains only the selected repository's stream, reconnects automatically, and never uses a fixed live-log polling timer.
- [x] New activity follows the bottom when the operator was already following and does not override an intentional upward scroll position.
- [x] Database commits and durable action-history writes signal activity, while failed transactions do not.
- [x] Existing state refresh, orchestration polling, redaction, CSRF, canonical-origin, and repository-management behavior remains unchanged.

## Verification

- [x] `UNIT` — prove transaction commit/rollback and durable action-history notification behavior.
- [x] `HTTP` — receive an initial SSE snapshot, signal a changed snapshot, and reject an unknown repository stream.
- [x] `CLIENT` — observe `EventSource` delivery, automatic reconnect, selected-repository replacement, follow-at-bottom, and held-scroll behavior with no periodic log fetches.
- [x] `REGRESSION` — run the focused database, execution, interface, application, and lifecycle suites.
