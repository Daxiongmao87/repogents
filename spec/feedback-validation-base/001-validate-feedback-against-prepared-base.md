# Validate Feedback Revisions Against the Prepared Base

A pull-request feedback revision can merge a newer intended-base commit than the run's immutable activation base. Validation must continue to use the activation-base command baseline while source-policy and issue-diff checks compare the candidate with the exact base prepared for the active conflict generation.

## Acceptance Criteria

- [x] A base-conflict revision passes the exact prepared intended-base SHA from feedback resolution into source execution.
- [x] Commit and validation source-policy checks exclude changes already present in that prepared base while retaining the run's activation-base validation baseline.
- [x] A prepared comparison base is accepted only when the candidate descends from that exact commit.
- [x] Orchestration resumes implementing or validating feedback work through feedback resolution so the prepared-base contract survives process interruption and validation retries.
- [x] A pre-fix feedback run blocked only because inherited prepared-base code was misclassified as issue-owned validation weakening is resumed exactly once through its pending revision batch.
- [x] The same legacy blocker remains recoverable when polling supersedes its processing conflict generation with a current pending generation before recovery runs.
- [x] A recognized validation-base recovery reruns the clean candidate's commit and validation gates against the current prepared base without replaying stale agent completion history.
- [x] If an earlier deployed recovery replayed the lead and produced a second blocker from obsolete fetched-base evidence, that blocker enters the same validation-only path exactly once.
- [x] A feedback-bound revision rejected by publication as source-fixable durably reopens its revision batch and resumes source execution instead of retrying publication from `implementing`.
- [x] Later ordinary feedback on the same pull request reuses the latest durably integrated conflict base instead of reverting source comparison to the activation base.
- [x] Ordinary issue execution without a prepared feedback base retains its existing activation-base behavior.
- [x] Publication preflight uses the candidate/current merge base after an integrated-base revision, so a later clean base advance neither creates a false conflict nor broadens issue scope review to inherited base changes.

## Verification

- [x] `REGRESSION` - prove a candidate containing a broad suppression inherited only from the prepared base validates when its issue delta does not weaken validation.
- [x] `REGRESSION` - prove a candidate that does not contain the prepared base is rejected before validation.
- [x] `REGRESSION` - prove conflict preparation forwards the newest observed base after a moving-base retry.
- [x] `REGRESSION` - prove implementing, validating, and publishing runs with processing feedback route through feedback recovery, while ordinary queued execution is unchanged.
- [x] `REGRESSION` - prove a later ordinary feedback batch reuses the base from the latest completed conflict revision.
- [x] `REGRESSION` - recover the recognized pre-fix prepared-base validation blocker exactly once while unrelated blocked reasons remain unchanged.
- [x] `REGRESSION` - recover a recognized validation-base blocker through the current pending conflict generation after its prior processing generation and revision batch were superseded.
- [x] `REGRESSION` - prove validation-only feedback recovery invokes no model runtime and validates the existing candidate against the prepared base.
- [x] `REGRESSION` - prove feedback resolution requests validation-only execution for a recognized recovery reason and ordinary feedback retains agent execution.
- [x] `REGRESSION` - recover the recognized post-replay stale-base blocker exactly once while unrelated blockers remain unchanged.
- [x] `REGRESSION` - reject a bound feedback revision during publication, restart in `implementing`, and prove the same batch is durably reopened, revised, and republished once.
- [x] `REGRESSION` - integrate one newer base into a candidate, advance that base again without conflict, and prove publication reviews only the candidate delta while the existing true-conflict case still blocks.
- [x] `REGRESSION` - run the focused execution, feedback, and orchestration suites.
- [x] `REGRESSION` - run the complete project suite.
- [ ] `LIVE` - resume Repogents issue #1 / pull request #6 and observe exact-SHA validation and same-pull publication against current `main`.
