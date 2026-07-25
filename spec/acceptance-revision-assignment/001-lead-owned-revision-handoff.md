# Lead-Owned Acceptance Revision Handoff

Fixes a defect discovered during the live `Daxiongmao87/websesh` issue #1 acceptance run. This item changes the member-blocking behavior established by `spec/repository-agent-mvp/004-stored-agent-team.md` and `spec/repository-agent-mvp/006-autonomous-execution.md`, and completes the failed-acceptance return path established by `spec/issue-acceptance-verification/001-sha-bound-issue-proof.md`.

## Contract

- Assigned non-lead members may inspect, implement, and verify under their stored permissions, but they do not own the run's final blocked decision.
- When a non-lead reports a blocker, the controller persists that report as a completed member handoff and continues to the stored lead.
- The stored lead receives the member's exact bounded, redacted reason and decides whether to revise with lead permissions or transition the run to `blocked`.
- A failed issue-acceptance verification that returns a run to implementation can therefore reach a repository-writing lead even when an assigned verifier is read-only.
- A lead's own irreducible `block` action retains the existing durable `blocked` transition.

## Acceptance Criteria

- [x] A read-only assigned verifier cannot strand a source-fixable acceptance revision by transitioning the run to `blocked` solely because the required edit exceeds verifier permissions.
- [x] A non-lead blocker report is durably handed to the lead, and the lead can revise, validate, and return the same run to publication.
- [x] A lead can still durably block the run for a genuinely irreducible prerequisite.

## Verification

- [x] `REGRESSION` - assign a read-only verifier that reports a source-fixable blocker, prove the lead receives the report, performs the edit, validates the exact commit, and reaches `publishing` without a blocked transition.
- [x] `REGRESSION` - run the existing lead-block test and prove a lead block still records the irreducible reason.
- [x] `LIVE` - resume the blocked `websesh` issue #1 run after deploying the fix and prove acceptance feedback reaches a writable revision owner.
