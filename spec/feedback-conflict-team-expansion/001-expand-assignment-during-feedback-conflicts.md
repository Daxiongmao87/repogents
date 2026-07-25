# Expand Stored-Team Assignment During Feedback Conflicts

Changes the issue-assignment timing established by `spec/stored-team-activation/001-stored-team-activation.md` and the feedback revision path established by `spec/open-pr-base-conflicts/001-detect-and-resolve-base-conflicts.md`. The immutable stored team, existing run, checkout, issue branch, and pull request remain authoritative.

## Contract

- The stored lead may expand an existing issue assignment when later issue revisions, pull-request feedback, or base conflicts expose required work outside the selected members' responsibilities or controller permissions.
- An expansion selects a strict superset of the current durable assignment from the run's immutable stored team. It cannot remove or replace prior members, must retain the stored lead and independent verifier, and must add at least one previously unassigned member.
- A successful expansion is durably recorded before the newly selected member executes. Existing assignment records, member completion evidence, feedback rows, revision-batch operations, run identity, checkout, issue branch, and pull request remain unchanged.
- After restart, previously completed members are not repeated solely because the assignment expanded. Every newly selected non-lead member executes before final lead integration and independent verification.
- Non-leads cannot assign. Unknown members, reductions, replacements, and redundant assignment actions are rejected without changing the durable assignment.
- A pre-fix feedback-conflict run blocked only because the controller rejected the stored lead's attempt to select an available stored member can be resumed once through the same feedback-revision batch; genuinely irreducible blocked runs remain blocked.

## Acceptance Criteria

- [x] A stored lead can expand an assignment after issue work has begun to select the stored member needed for newly observed merge conflicts.
- [x] The added member resolves all fetched-base conflicts, after which the lead integrates and the independent verifier approves a validated commit on the existing run, branch, and pull request.
- [x] Assignment expansion survives controller restart without repeating already completed members or losing pending feedback and revision-batch state.
- [x] Redundant, reducing, replacing, out-of-team, and non-lead assignment attempts are rejected without durable assignment changes.
- [ ] The blocked `Daxiongmao87/repogents` issue #1 run resumes through its existing pull request #6, preserves both actionable inline findings, resolves both review threads, and reaches a validated remote head that GitHub reports mergeable with current `main`.

## Verification

- [x] `REGRESSION` - begin real base-conflict feedback with an initial assignment that omits the required conflict owner; prove the lead expands the assignment after work starts, reconstruct the services, and complete merge, validation, and same-pull publication without repeating the completed member.
- [x] `REGRESSION` - prove redundant and reducing expansions are rejected while the original assignment remains intact.
- [x] `REGRESSION` - run the focused execution, team, feedback, lifecycle, publication, and orchestration suites.
- [ ] `LIVE` - deploy the corrected controller, resume the existing issue #1 / pull request #6 revision batch once, and observe the newly assigned stored member complete every unresolved checkout conflict.
- [ ] `LIVE` - verify pull request #6 uses the original run, branch, and PR; both inline review threads are resolved; the published head has passing exact-SHA validation; and GitHub reports it mergeable with current `main`.
