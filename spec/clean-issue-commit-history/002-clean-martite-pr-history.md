# Clean Martite Pull Request 9 History

Continues `spec/clean-issue-commit-history/001-consolidate-controller-revisions.md` with one explicitly authorized live remediation. This item cleans the controller-generated duplicate commit history already present on `Daxiongmao87/martite` pull request 9 without merging or replacing that pull request.

## Contract

- Record the current GitHub head SHA, base SHA, branch identity, review-thread state, and corresponding durable Repogents run and pull-request state before preparing a replacement.
- Build the replacement in an isolated checkout. The replacement has one Repogents-owned issue commit relative to the selected pull-request base and preserves the complete current issue diff without unrelated source changes.
- Treat the replacement SHA as a new candidate: rerun repository validation and issue acceptance against that exact SHA and retain prior SHA-bound evidence as history.
- Durably record the replacement candidate and a pending outbound branch operation before changing GitHub. Update the same deterministic branch using an exact force-with-lease against the observed prior remote head; abort if the branch moves.
- Reconcile the durable pull-request and run head fields to the confirmed remote SHA. Do not post comments, create another pull request, merge, close, or resolve unrelated feedback.

## Acceptance Criteria

- [x] Pull request 9 remains open on the same branch and base with exactly one issue commit relative to its base.
- [x] The cleaned commit preserves the current pull-request source change and introduces no unrelated path or content change.
- [x] Repository validation and issue 7 acceptance pass against the exact cleaned commit SHA.
- [x] Durable run, pull-request, validation, acceptance, and outbound-operation state identifies the cleaned SHA while retaining historical evidence for prior SHAs.
- [x] The branch replacement uses an exact force-with-lease against the recorded old head and cannot overwrite a concurrent update.
- [x] All review threads remain resolved, GitHub reports the pull request mergeable, and the pull request remains unmerged for the user.

## Verification

- [x] `GITHUB` — inventory the current PR head/base, commit graph, mergeability, and every review thread immediately before preparation.
- [x] `DATABASE` — capture the exact run, pull-request, validation, acceptance, feedback, and outbound-operation baseline for run `2c4893eb-1f93-5c42-8f38-4e4094d77861`.
- [x] `DIFF` — compare the old and cleaned candidates against the selected base and prove identical changed paths and content.
- [x] `VALIDATION` — run the repository-defined full unittest and compile commands at the cleaned SHA.
- [x] `ACCEPTANCE` — rerun the issue 7 dependency-removal and repository-owned-fixture checks at the cleaned SHA.
- [x] `DURABILITY` — prove the replacement candidate and pending outbound operation were committed before the GitHub push, then reconcile the confirmed remote head without deleting prior evidence.
- [x] `LIVE` — confirm PR 9 has one commit, zero unresolved review threads, a mergeable open state, no merge, and durable SHA agreement after publication.
