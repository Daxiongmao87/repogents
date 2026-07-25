# Detect and Resolve Open Pull-Request Base Conflicts

Changes the open-pull-request monitoring contract in `spec/repository-agent-mvp/008-feedback-resolution.md` and the quiet-period contract in `spec/repository-agent-mvp/009-quiet-period-notification.md`. Publication-time conflict checks in `spec/repository-agent-mvp/007-pull-request-publication.md` remain unchanged.

## Contract

- Different labeled issues in one repository create independent runs and isolated checkouts.
- Every normal open-PR poll also reads GitHub's current mergeability result. An indeterminate result waits for a later poll; it is not treated as clean or conflicting.
- When an open agent PR becomes conflicting after its base branch advances, the application records one durable synthetic feedback item for that PR head/base generation, stops the current quiet period, and sends the conflict through the existing feedback-revision path.
- The assigned team resolves the base conflict in the existing checkout, reruns required validation, and updates the same branch and pull request. The application neither opens a replacement PR nor merges either PR.
- Repeated polls and restarts do not duplicate one conflict event or its response. A later distinct conflicting head/base generation may create a new event.
- Merging or closing another run's PR affects that run normally while every other open run continues to be polled independently.

## Acceptance Criteria

- [x] Multiple ready-label events for different issues in one repository remain independently runnable.
- [x] An already-open PR that changes from mergeable to conflicting produces one durable conflict-revision item.
- [x] Conflict revision uses the existing run, checkout, branch, validation, publication, and PR identity.
- [x] Quiet notification cannot be emitted while the current PR is known to conflict.
- [x] Unknown mergeability causes no destructive transition and is checked again later.
- [x] Polling and restart are idempotent for one head/base conflict generation.

## Verification

- [x] `UNIT` — poll two open runs, merge the first externally, report the second as conflicting, and prove only the second enters conflict resolution.
- [x] `UNIT` — repeat the same conflicting head/base poll across restart and prove only one synthetic feedback version exists.
- [x] `INTEGRATION` — resolve a fixture conflict, validate the new commit, and publish it to the original branch/PR.
- [x] `REGRESSION` — run focused lifecycle, feedback, publication, quiet-period, and application suites.
