# Semantic Acceptance Verdict Retry

Fixes a defect discovered during the live `Daxiongmao87/websesh` issue #1 acceptance run. This changes the verifier-completion behavior established by `spec/issue-acceptance-verification/001-sha-bound-issue-proof.md`: an invalid model-authored verdict remains rejected, but it must become durable corrective feedback instead of repeatedly escaping publication without telling the verifier why.

## Contract

- A semantically invalid `verify` action never completes an acceptance attempt and never counts as issue evidence.
- The controller persists each rejected verdict as an unsuccessful, non-behavior acceptance observation containing the exact validation error. Existing plan and repository evidence remain immutable.
- The next verifier action receives the persisted rejection alongside the existing controller observations and can inspect, run additional behavior, or submit a corrected verdict within the same bounded verification loop.
- A corrected verdict is validated against the original durable plan, exact commit SHA, complete changed-file mapping, and successful controller evidence before it can complete the attempt.
- Process restart preserves rejected-verdict feedback because it uses the existing acceptance-evidence ledger. If no valid verdict is produced within the action bound, the attempt remains nonterminal and publication remains safely retryable.

## Acceptance Criteria

- [x] A verifier can recover from a semantically invalid final verdict without stranding the run in `publishing` or accepting the invalid report.
- [x] The rejection reason is durable, unsuccessful, and visible in the next verifier prompt without being treated as successful behavior evidence.
- [x] Corrected verdict processing preserves all existing exact-SHA, claim-evidence, scope-mapping, and screenshot gates.

## Verification

- [x] `UNIT` - submit a passing verdict backed only by non-behavior evidence, observe a persisted rejection, run valid behavior, and complete from the corrected verdict in the same service call.
- [x] `UNIT` - prove incomplete and stale passing verdicts remain rejected, receive their exact validation error, and can be corrected without replacing the active attempt.
- [x] `REGRESSION` - run the focused acceptance and publication suites.
- [x] `LIVE` - redeploy the controller and prove the in-progress `websesh` issue #1 acceptance attempt consumes durable rejection feedback and leaves the stuck publication step through a valid acceptance result.
