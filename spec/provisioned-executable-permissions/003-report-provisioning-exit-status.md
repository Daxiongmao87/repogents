# Report Provisioning Exit Status

## Prior Contracts

This item changes `spec/repository-evidence-inference/001-repository-evidence-inference.md` and continues the recovery work in `spec/provisioned-executable-permissions/001-preserve-bootstrap-executable-permissions.md`. Re-onboarding still uses the prior durable blocking reason as inference evidence; this item makes that evidence diagnostic when a sandbox command exits without output.

## Contract

Every failed environment-provisioning command reports its numeric process exit status. Redacted standard error remains the preferred diagnostic detail, redacted standard output is used when standard error is empty, and a shell-safe rendering of the command is used only when both output streams are empty. The resulting durable blocking reason is supplied unchanged to the next repository-inference attempt. Formatting is independent of repository, dependency, ecosystem, and runtime.

## Acceptance Criteria

- [x] A nonzero provisioning result always records its numeric exit status in the repository blocking reason.
- [x] Diagnostic detail uses standard error, then standard output, then the shell-safe command in that order.
- [x] Re-onboarding receives the complete durable failure reason as prior inference evidence.
- [x] No repository-, dependency-, ecosystem-, or runtime-specific failure rule is introduced.

## Verification

- [x] `UNIT` - empty-output exit status 141 records the status and shell-safe command.
- [x] `UNIT` - standard error and standard output precedence is deterministic.
- [x] `INTEGRATION` - re-onboarding passes the stored status-bearing failure reason to repository inference and can recover on the next attempt.
- [x] `REGRESSION` - focused onboarding tests pass.
- [x] `REGRESSION` - the complete deterministic project suite passes.
