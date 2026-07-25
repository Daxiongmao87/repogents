# Route Addressed and Actionable Pull-Request Feedback

## Prior Contract

This item changes `spec/repository-agent-mvp/008-feedback-resolution.md` and `spec/feedback-resolution-context/001-current-implementation-context.md`. Those items require arbitrary manually created pull-request comments to be persisted and evaluated, including comments authored through the same GitHub identity used by Repogents. This item narrows evaluation and response eligibility without weakening durable ingestion or output deduplication.

## Contract

- Every newly observed feedback version remains durably persisted before routing so polling, edits, restarts, and application-output deduplication remain deterministic.
- Repogents resolves the authenticated GitHub login used for its controller-owned mutations. A standalone external-integration command whose leading `@login` differs from that authenticated login is deterministically ignored without model evaluation or an outbound response. Recognized command forms include `@login review` and `@login address that feedback`.
- A leading mention of the configured login remains eligible for evaluation. A leading mention of another account does not itself suppress a concrete pull-request question, requested change, contradiction, or finding; incidental mentions remain equally eligible.
- Unresolved inline review findings remain eligible without an `@` mention. An inline comment whose GitHub review thread is already resolved is recorded but ignored without evaluation, source mutation, thread mutation, or response.
- General comments and substantive review bodies remain eligible only when they contain a concrete pull-request question, requested change, contradiction, or review finding. Generic review wrappers, acknowledgements, status remarks, and recognized commands addressed to another integration are ignored.
- The feedback evaluator must not invent a review or status summary merely because it can describe the current diff. `answer`, `revise`, and `decline` require an actual incoming question, requested change, or finding; otherwise the decision is `ignore` with an empty response.
- Failure to resolve the authenticated application login leaves feedback pending for retry rather than guessing the addressee or dropping feedback.

## Acceptance Criteria

- [x] A general comment containing exactly `@codex review`, when Repogents is authenticated as a different login, is persisted and completed as ignored without evaluator, executor, publisher, or response activity.
- [x] A leading mention of the configured GitHub login remains evaluable and can receive a response to a concrete question.
- [x] An actionable unaddressed comment and an unresolved inline review finding remain evaluable without mentioning the configured login.
- [x] An incidental mention of another account does not suppress otherwise actionable feedback.
- [x] A concrete general comment beginning with another account mention remains evaluable rather than being discarded solely because of that leading mention.
- [x] An already-resolved inline review thread is persisted and ignored without evaluator, source, thread-resolution, or response activity.
- [x] Evaluator instructions define the actionability boundary, preserve concrete feedback with another leading addressee, and prohibit invented review or status acknowledgements for non-actionable input.
- [x] Existing application-output filtering, edited-feedback identity, crash reconciliation, cancellation, and pull-closure boundaries remain unchanged.

## Verification

- [x] `REGRESSION` — reproduce the current `@codex review` response with a focused test before implementation, then prove it becomes a durable ignored decision with no outbound operation.
- [x] `UNIT` — cover configured-login addressing, actionable unaddressed feedback, actionable feedback with another leading addressee, incidental mentions, unresolved inline findings, and already-resolved inline threads.
- [x] `UNIT` — inspect the mini-SWE evaluator prompt contract for concrete actionability, other-integration command handling, preservation of actionable leading mentions, and the prohibition on invented status/review summaries.
- [x] `FOCUSED` — run the feedback and GitHub adapter test modules.
- [x] `SMOKE` — exercise the exact `@codex review` service path with a different configured login and observe persisted `ignore`, zero evaluator calls, and zero responses.
- [x] `REGRESSION` — run the complete project test suite.
