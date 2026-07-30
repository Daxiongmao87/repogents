# Validate Specification Before Implementation

## Prior Contracts

This item extends `spec/inference-led-issue-execution/002-persist-team-authored-issue-specifications.md` and adds a semantic validation boundary before the assignment and mutation boundary in `spec/repository-agent-mvp/006-autonomous-execution.md`. Structural specification validation remains controller-owned; this item requires an independent stored verifier to establish that a structurally valid revision is an adequate issue contract before implementation begins.

## Contract

- After the coordinator persists a structurally valid specification revision and before any implementation assignment or checkout mutation, the selected run's stored independent verifier reviews that exact revision against the current GitHub issue and discussion, repository instructions, bounded source evidence, and repository conventions.
- The verifier operates read-only and returns one structured verdict: `approved`, `rejected`, or `blocked`. It assesses issue coverage, behavioral clarity, observability, verification feasibility, internal consistency, repository alignment, scope discipline, and whether criteria were derived independently of a proposed implementation.
- Every review is immutable and bound to the run, specification revision, issue version, verifier team member, verifier model, review-rubric version, verdict, summary, structured findings, and creation time. One terminal semantic review is reused for the same immutable inputs; transient execution failures propagate to the controller's durable retry path rather than becoming semantic verdicts.
- `approved` unlocks assignment and source mutation only while that reviewed specification remains the active revision for the current issue version. A newer specification revision or issue version requires a new review and cannot reuse the prior approval.
- `rejected` keeps the run in pre-implementation work and returns actionable findings to the coordinator. The coordinator must append a corrected immutable specification revision; the rejected revision is never edited or silently approved.
- `blocked` is valid only for a specific irreducible external prerequisite that prevents a trustworthy specification review. It transitions the run to the durable blocked state with the review evidence; repository-fixable ambiguity or missing criteria are `rejected`, not blocked.
- The local state projection exposes every review and identifies whether the active specification is approved to begin implementation. No review result is written into the target checkout.

## Acceptance Criteria

- [x] A structurally valid specification does not unlock assignment or source mutation until the stored independent verifier has durably approved that exact active revision.
- [x] The verifier receives the current issue/discussion, repository instructions and evidence, exact specification revision, semantic rubric, and read-only repository tools without implementation authority.
- [x] Review payload validation rejects unauthorized reviewers, stale or non-active revisions, malformed verdicts, empty or oversized summaries/findings, and a blocked verdict without an irreducible prerequisite.
- [x] Approved, rejected, and blocked reviews are immutable, auditable, and bound to the exact run, issue version, specification revision, reviewer, model, and rubric.
- [x] Rejection returns actionable findings to the coordinator and requires a new revision and review while preserving both historical revisions and verdicts.
- [x] A new active specification revision or issue version invalidates the prior approval for implementation without rewriting it.
- [x] Transient verifier failures enter durable retry handling and do not create an approved, rejected, or blocked semantic review.
- [x] The local API and selected-run interface expose review history and the active revision's implementation-readiness state.

## Verification

- [x] `UNIT` - validate reviewer authority, active-revision binding, verdict schema, finding bounds, blocked prerequisites, immutable input hashes, and idempotent exact-input reuse.
- [x] `INTEGRATION` - persist a specification, attempt assignment and checkout mutation before review, prove both are rejected, approve through the independent verifier, restart, and then permit assignment.
- [x] `INTEGRATION` - reject an incomplete specification, feed its findings to the coordinator, append a corrected revision, approve it, and prove implementation uses only the corrected active revision.
- [x] `INTEGRATION` - approve a revision, append another revision or observe a new issue version, and prove implementation pauses for a new semantic review.
- [x] `UNIT` - distinguish repository-fixable rejection, irreducible blocked review, and transient verifier failure routed through durable retry.
- [x] `CLIENT` - inspect a selected run and trace its active specification to the independent review that does or does not authorize implementation.
- [x] `REGRESSION` - run focused specification, execution, lifecycle, application, interface, and retry suites.
