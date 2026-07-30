# Gate Publication on Specification Satisfaction

## Prior Contracts

This item extends `spec/issue-acceptance-verification/001-sha-bound-issue-proof.md`, `spec/validation-delta-comparator/001-classify-validation-delta.md`, and the publication boundary in `spec/repository-agent-mvp/007-pull-request-publication.md`. Existing exact-SHA validation, baseline comparison, source-scope review, secret scanning, visual evidence, and independent issue-derived acceptance remain required.

## Contract

- Independent acceptance for a candidate commit is bound to the active specification revision for the run's current validated issue version. Acceptance cannot start or reuse a result when no matching specification exists.
- The verifier receives the exact immutable specification in addition to the GitHub issue, discussion, repository evidence, changed files, validation comparison, and controller-observed behavior. It remains independent of the implementation team and may add issue-derived claims beyond the team's specification.
- Each acceptance-plan claim names the specification criterion keys it verifies. The controller rejects a plan unless every criterion in every active atomic item is covered by at least one claim and every mapping names a real criterion. Additional issue-derived claims may have no specification mapping.
- A passing report requires every planned claim to pass with cited controller evidence, which transitively marks every mapped specification criterion and item satisfied. Existing changed-file scope, screenshot, evidence-integrity, exact-commit, issue-version, and validation requirements remain conjunctive.
- Acceptance records persist the exact specification revision identity. The report exposes item-, criterion-, claim-, and evidence-level satisfaction mappings. A new specification revision supersedes an otherwise passing acceptance result and requires fresh independent verification for the same candidate.
- Publication rechecks that the passing acceptance report matches the candidate SHA, validated issue version, and currently active specification revision immediately before any pull-request side effect. A mismatch fails closed without publication.
- A source-fixable unsatisfied criterion produces actionable revision feedback. An irreducibly unverifiable criterion blocks with its missing capability; it is never silently treated as satisfied.

## Acceptance Criteria

- [x] Independent acceptance cannot pass or be reused without an active specification revision matching the run's current validated issue version.
- [x] The verifier receives the exact active revision and produces a plan covering every specification criterion, with unknown or incomplete mappings rejected before verification continues.
- [x] A pass durably proves every criterion and atomic item satisfied by passing claims with controller evidence while preserving all existing acceptance and publication gates.
- [x] Failed or blocked criteria produce actionable durable findings and cannot be converted into publication success by scope review, generic tests, or stale evidence.
- [x] Appending a specification revision invalidates prior passing acceptance for publication and requires a new report bound to the new revision.
- [x] The local API and selected-run interface show the specification revision used by acceptance plus criterion-to-claim satisfaction evidence.

## Verification

- [x] `UNIT` - reject missing/stale specification revisions, incomplete criterion coverage, unknown criterion mappings, and mapped claims without passing evidence.
- [x] `UNIT` - accept a plan with complete criterion coverage plus additional issue-derived claims and persist item/criterion satisfaction mappings for the exact revision and commit.
- [x] `INTEGRATION` - pass acceptance, append a specification revision, prove the cached report and publication boundary reject it, then re-verify and publish only the new bound report.
- [x] `INTEGRATION` - fail one source-fixable criterion and block one irreducibly unverifiable criterion, preserving distinct durable remediation outcomes without a pull-request side effect.
- [x] `CLIENT` - inspect a selected run and trace each active specification criterion to its acceptance claim and controller evidence.
- [x] `REGRESSION` - run focused acceptance, publication, feedback, application, and interface suites.
