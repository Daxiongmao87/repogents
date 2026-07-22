# Repository Context for Publication Scope Review

## Prior Contract

This item completes `spec/repository-agent-mvp/007-pull-request-publication.md`. The prior item requires full issue-scope and forbidden-artifact review but current scope-review context omits the stored repository evidence and instructions that define permitted changes.

## Contract

- Scope review receives the issue and current discussion, full committed base-to-head diff, changed-file list, exact base/head identities, and the run's immutable stored repository instructions and inspection evidence.
- The reviewer must evaluate both issue relevance and compliance with repository-local instructions. A change that appears issue-related but violates a governing repository instruction is rejected with actionable feedback before any push.
- Context is loaded from the run's stored sandbox/team versions and recorded commits, never reconstructed from mutable ambient host state.
- Missing or unreadable required scope evidence is a recoverable publication-preparation failure and cannot be silently represented as an empty instruction set.
- Cancellation boundaries from `spec/cancellation-effect-boundaries/001-cancellation-effect-boundaries.md` apply before and after review and before publication effects.

## Acceptance Criteria

- [x] The scope reviewer receives exact commit identities, complete diff, issue/discussion, and stored repository evidence/instructions.
- [x] An issue-related change that violates a stored repository instruction returns to implementation with the reviewer's specific feedback and is not pushed.
- [x] A compliant deletion-only or binary-including revision can pass without lossy diff reconstruction.
- [x] Missing required stored context preserves the publishing state for automatic retry rather than approving or permanently blocking the run.

## Verification

- [x] `UNIT` - capture the scope-review request and assert every required immutable context field is present.
- [x] `UNIT` - reject an instruction-violating revision and accept a compliant revision using the same issue text.
- [x] `INTEGRATION` - publish only after scope review of the exact validated SHA and prove no GitHub mutation occurs on rejection or missing context.
- [x] `REGRESSION` - run publication preflight, secret scan, large-diff, deletion, cancellation, and reconciliation suites.
