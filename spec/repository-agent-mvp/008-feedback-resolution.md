# Pull-Request Feedback Resolution

Implements `MVP.md` §8. Feedback is discovered dynamically from open agent-created pull requests; users do not predeclare comments or expected files.

## Contract

- Poll submitted reviews/bodies, inline review comments, and general pull-request comments.
- A feedback version is uniquely identified by type, stable GitHub object identity, and update version/`updated_at`; edited objects are new versions of the same object.
- Persist feedback before assignment. Application-created response IDs are outputs and never reingested; manually created objects remain feedback even under the configured identity.
- The stored lead evaluates each pending version in durable order against issue/discussion, repository instructions, current implementation, prior PR discussion, and scope.
- Valid requests are implemented; relevant questions answered; incorrect or out-of-scope requests are explained/declined. Source changes are revalidated, committed, and pushed to the same branch before a response is posted.
- Pending outbound responses are reconciled by target, author, body, and attempted time after interruption.

## Acceptance Criteria

- [x] All three feedback types and edited versions are durably ingested exactly once.
- [x] Recorded application outputs do not become feedback while unrecorded manual comments do.
- [x] Empty GitHub review containers are ignored while their separately listed inline comments remain actionable.
- [x] The stored team evaluates arbitrary arriving feedback without preconfigured text or outcome.
- [x] Feedback evaluation receives the current implementation diff and relevant source context, including the addressed path/line for inline feedback, rather than only commit identifiers.
- [x] Source-changing feedback produces a newly validated SHA on the same PR; non-source responses do not force unrelated validation.
- [x] Responses are posted and recorded once, including across crash/restart reconciliation.
- [x] A validated feedback source SHA is checkpointed before publication so a publication interruption cannot execute the same source revision twice.
- [x] After each pending set, an immediate repoll captures feedback arriving during resolution before quiet time begins.
- [x] Feedback polling refreshes pull-request open/closed/merged state before ingesting or mutating; external closure terminates the run without a revision.
- [x] After inference and immediately before any revision or response mutation, resolution rechecks durable cancellation and current pull-request open/merged state.
- [x] Response reconciliation requires the configured application author and unambiguous target in addition to body and attempted time.

## Verification

- [x] `UNIT` — version identities, output filtering, edited-object handling, ordering, and response reconciliation decisions are deterministic.
- [x] `ADAPTER` — an empty review wrapper associated with an inline comment yields only the inline feedback item.
- [x] `INTEGRATION` — mixed review, inline, general, edited, self-authored-manual, and application-output objects are stored and processed with correct cardinality.
- [x] `INTEGRATION` — interrupt feedback publication after validation, reconcile the same SHA to the PR, and complete the response without a second executor call.
- [x] `INTEGRATION` — crash after feedback validation but before publication and resume without executing the same revision twice.
- [x] `ADAPTER` — closed pull status and an identical third-party response cannot trigger or impersonate application work.
- [x] `INTEGRATION` — cancel or externally close the pull request while feedback inference is in flight and prove no response, revision, push, or state rewrite occurs afterward.
- [x] `LIVE` — post real feedback to the public fixture PR, observe inferred resolution and any required validated update on the same PR, and prove application responses are not reprocessed.
