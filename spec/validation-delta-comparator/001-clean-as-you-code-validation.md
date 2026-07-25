# Clean-as-You-Code Validation Delta

Changes `spec/repository-agent-mvp/006-autonomous-execution.md`, `spec/repository-agent-mvp/007-pull-request-publication.md`, and `spec/repository-agent-mvp/010-local-interface.md`.

## Contract

- Before the first agent action, each run executes its required validation commands on the exact stored base SHA and saves the raw results as its baseline.
- A command whose baseline exits zero remains a strict gate. A command whose baseline exits nonzero becomes a delta gate when Repogents can extract nonempty stable finding identities from its output; otherwise the run blocks before implementation.
- Exact-base validation reuses onboarded root and nested-package dependency state at the same workspace-relative package roots; it does not depend on an agent reinstalling dependencies.
- Candidate findings must be a multiset subset of baseline findings. Existing findings may remain or be removed. Any new finding fails, even when the total finding count decreases.
- Finding identity retains repository-relative path, severity, rule or test identifier, and message while ignoring volatile line, column, and timing values.
- Candidate validation stores raw exit status separately from its `pass` or `fail` policy verdict. Publication requires a passing verdict for every required command on the exact candidate SHA.
- Scope review rejects validation-rule, ignore, or configuration changes used only to hide findings.
- Changed policy filenames and source-level suppression markers are recorded for scope review, but do not fail validation by name alone. Added exclusions, broad suppressions, and suppression-only source edits invalidate the candidate verdict; a narrow suppression accompanying substantive source work remains reviewable.
- The local interface shows baseline mode, raw exit status, verdict, and new/resolved/unchanged finding counts.

## Acceptance Criteria

- [x] Exact-base validation finishes before assignment or source mutation.
- [x] Exact-base validation can execute commands from onboarded nested-package dependencies.
- [x] Strict baseline commands still require candidate exit status zero.
- [x] Unchanged or reduced baseline findings pass a delta gate.
- [x] Any new finding fails a delta gate regardless of net finding reduction.
- [x] Missing, unparseable, mutating, or stale validation evidence cannot publish.
- [x] Validation weakening cannot satisfy the delta policy.
- [x] Legitimate policy-file changes remain eligible for validation and scope review.
- [x] Broad or suppression-only source edits cannot erase baseline findings.
- [x] Durable delta evidence is visible in the local interface.

## Verification

- [x] `UNIT` — normalize finding output while ignoring volatile positions and preserve duplicate identities.
- [x] `UNIT` — compare equal, subset, and lower-count-with-new finding sets.
- [x] `INTEGRATION` — record exact-base results before the first agent action and resume them after restart.
- [x] `REGRESSION` — stored nested-package executables are available to exact-base validation before agent work.
- [x] `INTEGRATION` — store passing nonzero delta verdicts and return new findings to implementation.
- [x] `UNIT` — publication requires exact-SHA passing verdicts and scope review rejects validation weakening.
- [x] `REGRESSION` — a migrated passing result without a matching baseline cannot publish.
- [x] `REGRESSION` — a non-suppressing policy-file change passes while an added finding suppression fails.
- [x] `REGRESSION` — a source-only suppression that removes a baseline finding fails.
- [x] `CLIENT` — render validation mode, verdict, and finding counts.
- [ ] `LIVE` — re-onboard `Daxiongmao87/bazzeye`, deliver issue #7 without broad lint suppression, and publish one unmerged exact-SHA pull request.
