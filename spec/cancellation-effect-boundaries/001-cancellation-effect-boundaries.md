# Cancellation Effect Boundaries

## Prior Contracts

This item corrects and completes `spec/repository-agent-mvp/005-run-lifecycle.md`, `007-pull-request-publication.md`, and `008-feedback-resolution.md`. It supersedes their checked cancellation claims where current source permits model, sandbox, Git, GitHub, or feedback effects after cancellation or external pull-request closure.

## Contract

- A durable cancellation is a run-wide effect boundary. Once cancellation completes, no model/tool process remains alive and no later repository mutation, validation, push, pull-request creation, feedback revision, or feedback response may begin or complete for that run.
- Every controller-launched process associated with a run, including inference, scope review, and feedback evaluation, is registered with a run process supervisor and is terminated as a process group during cancellation.
- Cancellation is serialized with application-owned GitHub mutation boundaries. A mutation already inside the boundary either reconciles before cancellation becomes durable or is terminated and reconciled; it cannot appear after the durable canceled state.
- Feedback resolution rechecks durable run state and current pull-request openness after inference and immediately before revision or response effects. External pull-request merge or closure stops further work.
- Restart reconciliation and idempotent pending-operation reconciliation continue to work without reviving canceled work.

## Acceptance Criteria

- [x] Canceling during model inference terminates the inference process tree and execution returns without a later tool, commit, or validation action.
- [x] Canceling concurrently with publication produces no push or pull-request mutation after the run becomes durably canceled.
- [x] Canceling during feedback evaluation produces no revision, push, response, or feedback-state rewrite after cancellation.
- [x] Closing or merging the pull request during feedback evaluation produces no later revision, push, or response.
- [x] Existing restart and external-operation reconciliation semantics remain idempotent.

## Verification

- [x] `UNIT` - supervise a real temporary descendant process, cancel its run, and prove the process group exits and no later action executes.
- [x] `INTEGRATION` - race cancellation against publication boundaries and prove operation/state ordering prevents a post-cancel push or pull-request creation.
- [x] `INTEGRATION` - race cancellation and external pull-request closure against feedback inference and prove no post-boundary response or revision occurs.
- [x] `REGRESSION` - run lifecycle, execution, publication, feedback, and application tests covering restart and pending-operation reconciliation.
