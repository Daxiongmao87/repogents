# Acceptance Fixtures and Ledger Cleanup

## Prior Contracts

This item corrects `MVP.md` and `spec/repository-agent-mvp/005-run-lifecycle.md`, `010-local-interface.md`, and `011-mvp-acceptance.md`. It also incorporates the clean-cutover requirements from `spec/remove-manual-retry/001-remove-manual-retry.md`.

## Contract

- The required complete live acceptance fixture is `https://github.com/Daxiongmao87/websesh/issues/1`. `Daxiongmao87/bazzeye` and `Daxiongmao87/foundry-portal` remain public inventory or preliminary evidence only.
- Repository commands, stored team composition, permitted scoped changes, and feedback behavior are inferred from repository, issue, discussion, and arriving GitHub evidence. They are not predeclared fixture configuration.
- The product has no user-triggered run Retry control or HTTP operation. Recoverable failures retry automatically; `blocked` is reserved for a specific irreducible prerequisite and offers cancellation, not manual execution recovery.
- Historical specification items remain historically identifiable, but current unchecked/check states and supersession notes cannot report manual intervention or a substituted fixture as autonomous acceptance evidence.
- The acceptance service that was stopped during audit is not restarted or otherwise mutated by repository cleanup; any later host/live operation requires separate direct authorization and fresh evidence.

## Acceptance Criteria

- [x] `MVP.md` contains one current lifecycle/recovery/UI contract with no manual run Retry requirement or predeclared dynamic fixture metadata.
- [x] Current specifications explicitly identify superseded Retry language and consistently require automatic recovery.
- [x] The final LIVE acceptance criterion names `websesh#1` and does not accept the preliminary repositories as substitutes.
- [x] No manually resumed, substituted-fixture, interrupted, or stale run is marked as autonomous final evidence.
- [x] Repository cleanup performs no host-service restart or external GitHub mutation.

## Verification

- [x] `UNIT` - application/lifecycle/interface regression tests prove Retry is absent and recoverable failures retain automatic work.
- [x] `STATIC` - search governing MVP and active specifications for contradictory Retry controls, predeclared review comments/files/commands, and fixture substitution.
- [x] `STATIC` - inspect current acceptance checkboxes against present source and evidence; unsupported CLIENT, HOST, or LIVE criteria remain unchecked.
- [x] `LIVE` - under separate explicit authorization, execute the complete path against `https://github.com/Daxiongmao87/websesh/issues/1`, including restart, real arriving feedback, and 30 verified quiet minutes.
