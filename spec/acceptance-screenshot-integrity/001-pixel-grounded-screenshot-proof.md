# Pixel-Grounded Screenshot Proof

Changes the visual-proof contract established by `spec/issue-acceptance-verification/001-sha-bound-issue-proof.md`. Hashing and SHA-binding a screenshot remain necessary, but no longer suffice: required screenshot evidence must also be independently reviewed from the captured pixels before a passing acceptance verdict can publish.

## Contract

- For every screenshot submitted with a passing verdict, the controller reviews the exact staged image bytes after capture and before completing the acceptance verification.
- Review input contains the planned claim, expected visible state, submitted scenario/description, image digest, and the image itself. The review must judge the pixels rather than trust verifier prose or hidden DOM/internal state.
- Review passes only when the image directly and unobscuredly depicts the claimed user-visible state. Blocking authentication/setup modals, loading/error screens, blank pages, unrelated content, or contradictory visible state fail review.
- For a temporal claim, each still image is reviewed as a claim-relevant visible checkpoint selected by its submitted description; controller-recorded action evidence proves the transition sequence. The description cannot override, excuse, or manufacture anything absent or contradicted in the pixels.
- Image-review failure or unavailable vision capability is durable evidence and cannot produce a passed verification or publication. The verifier may correct the user flow, recapture the state, and submit a new verdict within the same exact-SHA attempt.
- Accepted screenshot artifacts retain the controller review result and image digest so the local interface and pull-request proof remain auditable.
- Cached passing reports created before pixel-review provenance existed are stale and must be reverified rather than reused.
- A blocked visual verifier verdict must first receive one controller correction opportunity. Probe remediation cites post-challenge repository endpoint evidence, uses a controller-applied safe runtime binding, and emits a structured successful observation of the corrected target. A stored blocked attempt created without that challenge is automatically requeued once through a durable lifecycle transition; the blocked attempt remains history, while a blocker confirmed after the challenge remains blocked.

## Acceptance Criteria

- [x] A required visual acceptance verdict cannot pass unless every submitted screenshot's exact bytes receive a passing controller-owned pixel review for its mapped claim.
- [x] A screenshot obscured by an authentication/setup modal is rejected even when a successful command reports matching hidden DOM state.
- [x] Failed or unavailable pixel review is recorded durably, remains visible to the verifier, and cannot be reused as successful screenshot proof.
- [x] A corrected screenshot can pass without discarding the rejected observation, and accepted report artifacts expose the matching review result and digest.
- [x] A cached passing report without same-digest pixel-review provenance is not reusable.
- [x] A browser verifier must inspect and honor repository-defined client/server endpoint configuration; a known probe-port mismatch is corrected and retried rather than treated as an irreducible blocker or candidate defect.
- [x] Recoverable visual-verifier probe failures do not terminally block on the first verdict. Remediation must cite repository endpoint evidence, rerun with a different controller-bound target, and report a structured successful observation of that target; unrelated reads, runs, arguments, or process-start output do not qualify. A pre-contract blocked attempt is automatically requeued once without deleting its report or exposing a manual Retry operation.
- [x] A temporal acceptance claim can use screenshots for directly visible before/after checkpoints without requiring one still image to prove that a reload, reconnect, or other transition occurred; the recorded action evidence remains responsible for that sequence.
- [ ] Bazzeye issue #7 is reverified at the exact candidate SHA with screenshots that visibly show disable, persisted-disabled, enable, and persisted-enabled dashboard states on one open unmerged pull request.

## Verification

- [x] `UNIT` - reject an auth-modal screenshot from an otherwise successful visual claim, persist the rejection, then accept corrected pixels and retain both observations.
- [x] `UNIT` - reject a required screenshot when pixel review is unavailable or does not cover the same digest, claim, and expected state.
- [x] `UNIT` - expose accepted pixel-review provenance with the stored screenshot artifact and proof projection.
- [x] `REGRESSION` - ignore a legacy cached pass without pixel-review provenance and create a new exact-SHA verification attempt.
- [x] `REGRESSION` - run the complete deterministic project suite.
- [x] `REGRESSION` - require the verifier boundary to distinguish a source-resolvable browser endpoint mismatch from an irreducible acceptance blocker.
- [x] `INTEGRATION` - reject an initial blocked visual verdict, reject unrelated intervening actions and unobserved targets, accept only source-cited controller-bound corrected-target remediation, automatically requeue one legacy unchallenged blocked attempt through the lifecycle, preserve its history, and leave an irreducible first blocker terminal.
- [x] `UNIT` - accept a directly visible temporal checkpoint when pixels match its claim-relevant submitted description, while still rejecting unrelated, obscured, or contradictory pixels.
- [ ] `LIVE` - reverify Bazzeye issue #7, inspect all captured artifacts directly, and confirm exact-SHA proof on one open unmerged pull request.
