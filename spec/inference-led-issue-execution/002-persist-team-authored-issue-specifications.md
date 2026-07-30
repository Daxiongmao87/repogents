# Persist Team-Authored Issue Specifications

## Prior Contracts

This item extends `spec/repository-agent-mvp/006-autonomous-execution.md`, `spec/agent-workflow-graphs/001-model-designed-workflow-graph.md`, and `spec/agent-workflow-graphs/002-durable-ready-node-scheduler.md`. The stored workflow remains model-designed and controller-scheduled; this item adds a durable issue-level behavioral contract authored by the selected run's coordinator before source mutation.

## Contract

- Before the first assignment or checkout mutation for an issue version, the selected run's stored coordinator submits one structured specification revision through a controller action. The specification is derived from the GitHub issue and discussion, repository instructions, current source, and relevant repository evidence; it is not derived from the implementation the team intends to write.
- A specification revision contains one or more atomic items. Each item has a stable lowercase key, concise title, bounded behavioral objective, one or more independently observable acceptance criteria, and one or more verification scenarios. Every criterion has a stable globally unique key and is mapped to at least one verification scenario; every scenario names the criteria it verifies.
- The controller validates bounded sizes, unique keys, complete criterion-to-verification coverage, current issue-version binding, selected team membership, and coordinator authority before persistence. Invalid or partial specifications do not unlock source mutation.
- Revisions are immutable, monotonically numbered per run, and bound to the run, exact issue version, authoring team member, canonical content hash, reason, and creation time. Identical current content is idempotent. A changed issue version requires a new matching revision before work continues.
- The latest revision for the current issue version is the active contract. The coordinator may append a revision when issue evidence or implementation discoveries change required behavior; prior revisions remain queryable and are never rewritten.
- The specification ledger is controller-owned SQLite state. Agents do not create planning, specification, coordination, or status files in the target repository unless the target repository explicitly requires such a tracked artifact for the product change.
- Every model turn receives the active specification when one exists. The pre-specification coordinator may inspect repository evidence through read-only tools or block with a specific irreducible prerequisite; assignment, source writes, and completion are rejected until a valid current revision is durable.
- The local state projection exposes all immutable revisions and identifies the active revision, including each item, criterion, verification mapping, author, issue version, and content hash.

## Acceptance Criteria

- [x] A new run cannot assign implementation work or mutate its checkout until its stored coordinator has persisted a valid specification revision for the current issue version.
- [x] Valid revisions contain atomic items with unique stable keys, observable acceptance criteria, complete verification mappings, and bounded descriptive content.
- [x] Specification creation is authorized only for the selected run's coordinator; invalid member, stale issue version, duplicate keys, unmapped criteria, unknown mappings, empty sections, and oversized content fail without partial persistence.
- [x] A repeated identical submission is idempotent, while a changed valid submission appends the next immutable revision and leaves all prior rows unchanged.
- [x] Returning to content from an older non-active revision appends a new immutable revision relative to the active contract; only repetition of the active content is idempotent.
- [x] A newly observed issue version invalidates the prior active contract for execution until the coordinator appends a revision bound to that issue version.
- [x] Agent context contains the active contract, and specification work remains in SQLite rather than adding controller planning files to the target checkout.
- [x] The local API and selected-run interface expose the active revision and complete immutable revision history without requiring log or database access.

## Verification

- [x] `UNIT` - accept a minimally valid specification and reject malformed keys, duplicate keys, missing criteria, missing verification, incomplete/unknown mappings, oversized strings, stale issue versions, and unauthorized authors.
- [x] `UNIT` - submit identical and changed specifications, then prove idempotent reuse, monotonic revision numbering, immutable history, and canonical hashes.
- [x] `UNIT` - submit content A, then B, then A again and prove monotonic revisions `1, 2, 3`, immutable history, and idempotent reuse only for the active revision.
- [x] `INTEGRATION` - start an unassigned run, allow read-only inspection, reject assignment/write/finish before specification, persist through the coordinator action, restart, and continue with the exact same revision.
- [x] `INTEGRATION` - advance the issue version and prove execution pauses for a matching new revision while the prior revision remains auditable.
- [x] `UNIT` - inspect model context and prove it includes the complete active specification and prohibits controller-owned planning files in the checkout.
- [x] `CLIENT` - inspect a selected run and prove its active specification and revision history are accessible and complete.
- [x] `REGRESSION` - run focused database, workflow execution, execution, interface, and lifecycle suites.
