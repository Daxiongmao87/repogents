# Resolve GitHub Review Conversations

Changes `spec/repository-agent-mvp/008-feedback-resolution.md`, `spec/continuous-pr-feedback-polling/001-poll-until-pr-closure.md`, and `spec/pre-push-feedback-gate/001-pre-push-feedback.md`. Local feedback completion is not sufficient for an inline review conversation: Repogents must durably reconcile the corresponding GitHub review thread before returning an open run to `waiting_for_feedback`.

## Acceptance Criteria

- [x] Polling maps every inline review comment to its GitHub review-thread node identity and current resolved state, persists that state for new and existing feedback rows, and continues to exclude recorded application outputs from feedback ingestion.
- [x] After every locally handled comment in a review thread has completed, Repogents durably stages and performs exactly one thread-resolution operation for that feedback generation; revision threads are resolved only after the validated revision is published, and answer or decline threads only after the response is recorded.
- [x] A crash before or after GitHub resolves a thread resumes by reconciling the staged operation and never duplicates source execution, publication, response, or effective thread resolution.
- [x] An unresolved actionable review thread keeps the run out of `waiting_for_feedback`; completed historical feedback without prior thread metadata is backfilled and reconciled without re-evaluation or another reply.
- [x] Review bodies and general pull-request comments retain their existing completion behavior because GitHub exposes no resolvable review thread for them.
- [x] Polling an open pull request with no pending feedback and no unresolved review threads leaves `waiting_for_feedback` unchanged without emitting a synthetic `resolving_feedback` transition.

## Verification

- [x] `ADAPTER` — paginate GitHub review threads, map every thread comment database ID to the same thread, preserve `isResolved`, and issue the `resolveReviewThread` GraphQL mutation with the exact node ID.
- [x] `INTEGRATION` — handle multiple comments in one thread and prove resolution waits for all local work and any required push or response, then records one durable completed operation.
- [x] `RECOVERY` — interrupt before mutation and after remote success, then reconcile without duplicate execution, push, response, or unresolved thread.
- [x] `MIGRATION` — upgrade existing completed inline-feedback rows, backfill thread identity/state on the next poll, and resolve them without re-evaluation or another response.
- [x] `REGRESSION` — an idle waiting run incurs no state transition, while newly arriving feedback still activates `resolving_feedback` and is processed once.
- [x] `REGRESSION` — focused GitHub, feedback, publication, scheduler, database, and complete project suites pass.
- [x] `LIVE` — deploy the change, reconcile every unresolved review thread on Daxiongmao87/martite pull request #9 without posting comments or changing its source SHA, and verify the open run returns to stable `waiting_for_feedback` with no pending operation.
