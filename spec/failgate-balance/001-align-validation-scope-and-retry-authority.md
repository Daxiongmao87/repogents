# Align Validation, Scope, Acceptance, and Retry Authority

## Prior Contracts

This item changes `spec/validation-delta-comparator/001-clean-as-you-code-validation.md`, `spec/publication-scope-context/001-repository-scope-context.md`, `spec/issue-acceptance-verification/001-sha-bound-issue-proof.md`, and `spec/run-retry-and-issue-restart/001-recover-failed-and-canceled-runs.md`.

The exact-base validation delta remains the authoritative repository regression gate. Publication scope review remains required, but its authority is narrowed to issue relevance, repository-instruction compliance, forbidden artifacts, and unexplained changed files. Independent acceptance remains the issue-behavior gate and must consume, rather than reinterpret, the controller's exact-base comparison. Recoverable gate failures remain nonterminal and must enter the durable bounded-retry path.

## Contract

- Independent acceptance receives the immutable exact-base and exact-candidate validation comparison for every required command: command identity, baseline mode and exit status, candidate verdict and exit status, baseline findings, candidate findings, new findings, resolved findings, unchanged findings, and validation-policy weakening results.
- Acceptance treats a passing controller validation verdict as authoritative for repository debt. It must not reject a candidate merely because unchanged baseline findings remain or an external limitation affected both exact-base and exact-candidate validation identically. It must still reject any new finding, validation weakening, or failed issue-specific behavior claim.
- Publication scope review judges only whether changed files and behavior are issue-related, necessary regression protection, compliant with immutable repository instructions, free of forbidden artifacts, and free of unexplained unrelated changes. It must not independently judge implementation completeness, test adequacy, runtime correctness, visual quality, or acceptance proof.
- Each semantic scope verdict is durable and keyed by the immutable review input, including the issue version, exact base and candidate identities, complete diff identity, changed-file set, stored repository evidence and instructions, reviewer model identity, and rubric version. An identical key reuses the recorded verdict without another model call; any changed input produces a distinct review.
- A persisted scope rejection remains actionable and returns the same run to implementation. A persisted approval permits the exact candidate to proceed to independent acceptance. Neither verdict weakens exact-SHA validation, secret scanning, deterministic preflight checks, or unresolved-feedback protection.
- Recoverable publication and feedback gate exceptions must escape the gate service to the controller. The controller durably records the operation, consecutive attempt count, bounded retry deadline, and redacted last error while preserving the run's nonterminal state and exact candidate identity. Semantic scope or acceptance rejection remains revision feedback, not a transient retry.

## Acceptance Criteria

- [x] Acceptance receives and explicitly follows the controller's exact-base comparison, so unchanged baseline debt cannot by itself fail an otherwise proven issue candidate.
- [x] Scope review is limited to alignment and forbidden-change authority and no longer duplicates correctness, completeness, validation, or acceptance judgments.
- [x] Scope verdicts are persisted and reused only for byte-equivalent immutable review inputs, with changed inputs invalidating reuse.
- [x] Recoverable publication and feedback gate exceptions produce durable automatic-retry state instead of silent stalled execution.
- [x] Exact-SHA validation, validation weakening detection, secret scanning, deterministic preflight, issue-specific acceptance, and unresolved-feedback boundaries remain fail closed.

## Verification

- [x] `UNIT` - acceptance prompt/context includes an exact-base delta comparison and directs the verifier to accept unchanged baseline debt while rejecting new findings, weakening, and failed issue behavior.
- [x] `UNIT` - repeated identical publication scope input calls the reviewer once and reuses the durable verdict across a new service instance; changing a key input calls the reviewer again.
- [x] `UNIT` - scope rubric excludes completeness, test adequacy, runtime correctness, visual quality, and acceptance-proof authority while retaining issue alignment, repository instructions, forbidden artifacts, and unexplained changes.
- [x] `INTEGRATION` - publication and feedback exceptions schedule durable bounded retry through the controller while semantic rejections return to implementation without retry metadata.
- [x] `REGRESSION` - focused acceptance, publication, feedback, orchestration, and database migration suites pass.
- [x] `SMOKE` - an exact-base fixture with unchanged baseline findings reaches acceptance/publication, while a candidate-only finding remains rejected.
- [x] `REGRESSION` - the complete deterministic project suite passes.
