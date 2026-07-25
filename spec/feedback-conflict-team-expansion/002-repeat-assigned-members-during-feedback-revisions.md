# Repeat Assigned Members During Feedback Revisions

Extends `spec/feedback-conflict-team-expansion/001-expand-assignment-during-feedback-conflicts.md` for a later revision that needs more work from a member already present in the durable assignment. The immutable team and assignment membership remain unchanged.

## Contract

- The stored lead may durably request another execution from one or more currently assigned implementation members when feedback, merge integration, verification, or validation exposes remaining source work after those members previously finished.
- The request identifies a nonempty unique subset of the current assignment, excludes the lead and independent verifier, records a bounded reason before yielding, and does not insert, remove, or replace assignment rows.
- On restart, only the requested completed members become eligible to execute again. Other completed members remain complete; the lead subsequently integrates; the independent verifier always reviews the resulting candidate.
- A non-lead, unknown member, unassigned member, lead, verifier, empty request, or duplicate request member is rejected without changing completion or assignment state.
- A pre-fix feedback run blocked only because the stored lead had no controller action to return repository mutation to already assigned members is resumed once through the same pending feedback-revision batch. Unrelated and repeated blocked runs remain blocked.

## Acceptance Criteria

- [x] A stored lead can hand remaining source work back to a currently assigned implementation member without changing durable assignment membership.
- [x] The targeted member executes again after restart while unrelated completed members do not repeat; the lead integrates and the independent verifier reviews the new candidate.
- [x] Invalid or unauthorized targeted revision requests are rejected without changing assignment or completion state.
- [x] The blocked `Daxiongmao87/repogents` issue #1 run resumes through its existing pull request #6 and pending feedback-revision batch, removes out-of-scope conflict artifacts, validates the corrected merged source, and publishes to the same branch and pull request.

## Verification

- [x] `REGRESSION` - exhaust the complete stored assignment, record completed-member checkpoints beyond the bounded action tail, request a targeted member revision, reconstruct the controller, and prove only that member repeats before lead integration and independent verification.
- [x] `REGRESSION` - reject non-lead, unknown, unassigned, lead, verifier, empty, and duplicate targeted revision requests without assignment or checkpoint mutation.
- [x] `REGRESSION` - recover the recognized pre-fix assigned-member feedback block exactly once while unrelated blocked runs remain unchanged.
- [x] `REGRESSION` - run the focused execution, feedback, lifecycle, application, team, and publication suites plus the complete project suite.
- [x] `LIVE` - deploy the handoff action, resume issue #1 / pull request #6 through the existing batch, and observe the selected member complete the requested cleanup before exact-SHA validation and same-pull publication.
