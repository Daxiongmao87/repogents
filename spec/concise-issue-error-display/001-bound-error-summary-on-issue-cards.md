# Bound Error Summary on Issue Cards

Changes the run-reason presentation established by `spec/repository-agent-mvp/010-local-interface.md` and `spec/issue-priority-queue/001-draggable-issue-priority-and-focus.md`. Durable diagnostic detail remains available in the selected run's **Issue Log**; queue buttons are summaries, not diagnostic dumps.

## Contract

- The state projection exposes at most the first 400 characters of the first line of a run reason and reports whether detail was omitted.
- The durable database reason and run-log snapshot remain complete and unchanged.
- An issue button renders only the bounded summary. When detail was omitted, it directs the user to the selected **Issue Log** for the complete diagnostic.
- Newlines, large acceptance reports, command output, and source excerpts cannot expand an issue button into a full diagnostic surface.
- Existing HTML escaping, redaction, issue selection, log streaming, queue ordering, and controls remain unchanged.

## Acceptance Criteria

- [x] A multiline or oversized run error produces a bounded one-line state summary with an explicit truncation indicator.
- [x] The issue button displays only that summary and points to **Issue Log** when full detail exists.
- [x] Selecting the issue retains access to the complete durable reason through the bounded run-log endpoint.
- [x] Short single-line reasons remain unchanged.

## Verification

- [x] `UNIT` — project short, multiline, and oversized reasons and verify the 400-character boundary and truncation flag.
- [x] `HTTP` — fetch state and the selected run log and prove only the latter contains the complete diagnostic.
- [x] `CLIENT` — load the deployed queue containing a large Martite failure, confirm the button remains compact, and confirm full detail remains in **Issue Log**.
- [x] `REGRESSION` — run application and interface suites.
