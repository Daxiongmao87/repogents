# Preserve Provisioned Executable Permissions

## Prior Contracts

This item changes `spec/repository-evidence-inference/001-repository-evidence-inference.md`. The existing user-space bootstrap and dependency-delta contract remains; this item makes executable-mode preservation explicit for artifacts unpacked under the service's restrictive umask.

## Contract

Repository inference must require every generated bootstrap command to restore and verify executable permissions on downloaded or extracted entrypoints before first invocation and before linking them into `/repository-state/bin`. It must not assume an archive extractor preserves executable mode bits. The requirement is ecosystem-neutral and must not hard-code Plaito, Android, or a particular archive tool. A blocked repository is recovered through ordinary re-onboarding with the recorded failure as inference evidence, not by manually modifying its partial dependency state.

## Acceptance Criteria

- [x] Inferred bootstrap commands are explicitly required to make downloaded or extracted entrypoints executable before invoking or publishing them.
- [x] The inference contract remains generic across repositories, ecosystems, and archive tools.
- [ ] Plaito re-onboarding completes without manually changing the failed dependency tree and persists a ready sandbox whose required entrypoints are executable.

## Verification

- [x] `UNIT` - the repository-inference prompt exposes the restrictive-umask and executable-mode invariant together with prior-failure evidence.
- [x] `REGRESSION` - focused onboarding inference and re-onboarding tests pass.
- [x] `REGRESSION` - the complete deterministic project suite passes.
- [ ] `LIVE` - deployed Plaito re-onboarding reaches `ready`, records new provisioning evidence, and its Android SDK entrypoint executes in the resulting sandbox.
