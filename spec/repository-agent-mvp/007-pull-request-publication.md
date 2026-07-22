# Pull-Request Publication

Implements `MVP.md` §7. This item owns deterministic branch/PR identity, prepublication checks, application-owned GitHub mutations, and restart-safe reconciliation.

## Contract

- Branch names are `agent/issue-<issue-number>-<run-id>` and each run has at most one associated pull request.
- Before any push, durable state records branch, intended base, base SHA, validated head SHA, passing validation records, and a pending outbound operation.
- Publication compares base/head, checks the complete committed diff for issue scope and forbidden artifacts, scans secrets, and requires passing validation for the exact head. Source-fixable scope, artifact, or secret findings return the run to implementation with the exact reviewer feedback before any push.
- The application-owned GitHub client performs GitHub operations outside the sandbox without exposing credentials to agents or repository commands.
- Reconciliation reuses the expected remote branch/PR, blocks on an unexpected branch SHA, and never creates duplicate PRs.
- The application never merges or closes pull requests.

## Acceptance Criteria

- [x] A validated run receives one deterministic branch and one pull-request association.
- [x] Publication returns source-fixable out-of-scope, forbidden-artifact, or secret findings to implementation with feedback for autonomous correction.
- [x] Publication blocks absent/stale validation, unexpected remote branch state, base conflict, and non-actionable external failures.
- [x] Push and PR creation are durably staged and recover idempotently after interruption.
- [x] The remote branch head is verified equal to the validated SHA before the run waits for feedback.
- [x] A validated feedback revision tolerates temporary pull-request head lag after the deterministic branch update and reuses the same pull request.
- [x] The pull request targets the stored intended base branch and remains unmerged/open unless an external actor changes it.
- [x] Controller Git clone/fetch/push uses the configured GitHub credential without requiring ambient `gh` login and never exposes that credential to model or sandbox processes.
- [x] Preflight fetches the current intended-base head, rejects merge conflicts, includes deletions in scope review, and accepts deletion-only in-scope revisions.
- [x] Scope-review prompts are file-backed, and durable cancellation is checked before every push or pull-request mutation boundary.
- [x] Scope review receives the applicable stored repository instructions and evidence in addition to the issue, discussion, changed files, and complete diff.

## Verification

- [x] `UNIT` — deterministic identities, publish preconditions, forbidden-path checks, and reconciliation decisions cover all specified states.
- [x] `INTEGRATION` — a scope-rejected validated diff returns to the lead with the reviewer reason, is revised and revalidated, then reaches publication without a user retry.
- [x] `ADAPTER` — a feedback revision observes stale pull-request head data before convergence, then confirms the new validated SHA without creating another pull request.
- [x] `ADAPTER` — branch, push, PR lookup/create, and remote-head responses are interpreted without leaking credentials.
- [x] `INTEGRATION` — interrupt publication before and after each external boundary and reconcile to one branch and one PR.
- [x] `UNIT` — configured-token Git transport, deleted-file scope, current-base conflict, file-backed large diffs, and cancellation boundaries are covered.
- [x] `UNIT` — scope-review context includes stored repository instructions/evidence, and cancellation during an in-flight push or pull-request creation cannot produce a later external mutation.
- [x] `ADAPTER` — an environment-token-only controller can clone/fetch/push while sandbox/model environments remain credential-free.
- [x] `LIVE` — publish the exact validated public-fixture commit, confirm its remote SHA/base, and confirm no merge or close operation is performed.
