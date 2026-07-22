# Current Implementation Context for Feedback

## Prior Contract

This item completes `spec/repository-agent-mvp/008-feedback-resolution.md`. The prior item requires arbitrary arriving feedback to be inferred against durable run context but did not ensure that the evaluator receives the implementation being reviewed.

## Contract

- Feedback evaluation receives the issue and discussion, repository instructions/evidence, stored team identity, current base and validated commit identities, the complete current base-to-validated diff, prior feedback, and the addressed source context for an inline comment when available.
- Source context is read from the recorded validated commit, not mutable ambient working-tree state.
- GitHub-provided paths are treated as untrusted input and cannot escape the repository or invoke a shell.
- Context collection failure preserves the current durable feedback/run state for automatic retry; it does not fabricate empty implementation evidence or block a recoverable run.
- Cancellation and pull-request closure checks from `spec/cancellation-effect-boundaries/001-cancellation-effect-boundaries.md` apply after evaluation and before every effect.

## Acceptance Criteria

- [x] A general pull-request comment is evaluated with the complete current committed diff and repository evidence.
- [x] An inline comment is additionally evaluated with bounded source context from its path and line at the exact validated SHA.
- [x] Missing, deleted, binary, or invalid inline paths are represented safely without reading outside the committed repository.
- [x] A transient context-collection failure is automatically retried without losing or resolving the feedback version.

## Verification

- [x] `UNIT` - assert evaluator context contains exact base/validated identities, committed diff, repository evidence, and inline source lines.
- [x] `UNIT` - cover deleted, binary, traversal, and unavailable source paths without host reads or shell interpretation.
- [x] `INTEGRATION` - resolve feedback against a changed checkout and prove context is sourced from the recorded validated commit rather than later working-tree changes.
- [x] `REGRESSION` - run feedback polling, decision, response reconciliation, and cancellation suites.
