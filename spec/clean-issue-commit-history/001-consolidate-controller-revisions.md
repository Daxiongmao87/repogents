# Consolidate Controller-Owned Issue Revisions

Changes the commit-history behavior established by `spec/repository-agent-mvp/006-autonomous-execution.md`, the branch-update contract in `spec/repository-agent-mvp/007-pull-request-publication.md`, and source-changing feedback publication in `spec/repository-agent-mvp/008-feedback-resolution.md`. The prior items remain authoritative except for this explicit delta: repeated controller preparation of the same issue revision replaces the current controller-owned issue commit instead of appending another indistinguishable commit, and rewritten deterministic branches are updated with compare-and-swap lease protection.

## Contract

- The first controller-prepared source candidate creates one issue commit with the normalized issue subject and controller identity.
- When later source work leaves staged changes on top of that same current controller-owned issue commit, commit preparation amends it. This applies to repository-validation corrections, publication-review corrections, and pull-request feedback revisions.
- Commit preparation never amends the stored activation base or a current commit not owned by the controller for this issue. Base-integration commits and other distinct repository history remain intact.
- Every amended candidate receives a new SHA and must pass the existing exact-SHA validation and acceptance gates. Historical validation, acceptance, feedback, and outbound-operation rows remain durable evidence for their original SHAs.
- Updating the deterministic remote branch supplies an explicit expected prior remote SHA. Initial creation leases against branch absence; later rewrites lease against the last durably recorded remote head.
- A remote branch that changes after observation but before push is not overwritten. Existing publication retry and reconciliation rules remain in force when the intended SHA was accepted before a response was observed.

## Acceptance Criteria

- [x] Repeated controller source corrections for one issue leave one current controller-owned issue commit instead of accumulating the same generated commit subject.
- [x] The stored activation base and non-controller current commits are never amended by controller commit consolidation.
- [x] A rewritten feedback candidate can update the same deterministic pull-request branch only when its remote head still equals the durably recorded prior head.
- [x] Initial branch creation and later branch replacement both reject a concurrent unexpected remote write rather than overwriting it.
- [x] Exact-SHA validation, publication proof, restart reconciliation, and one-branch/one-pull-request identity continue to apply to every replacement candidate.

## Verification

- [x] `INTEGRATION` — fail repository validation, correct the source, and prove the passing candidate replaces the failed controller commit while both validation results retain their original SHAs.
- [x] `INTEGRATION` — publish one controller candidate, prepare a later source correction, and prove the current issue history still contains one controller commit with a new validated SHA.
- [x] `UNIT` — preserve a non-controller current commit and append the first controller-owned issue commit above it.
- [x] `UNIT` — Git transport sends an explicit force-with-lease expectation for an absent branch and for a known prior remote SHA.
- [x] `INTEGRATION` — mutate the remote fixture after branch observation and prove publication cannot overwrite the concurrent head.
- [x] `REGRESSION` — focused execution and publication suites plus the complete project suite pass.
