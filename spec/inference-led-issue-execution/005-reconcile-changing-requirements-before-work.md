# Reconcile Changing Requirements Before Work

## Contract

When pull-request feedback or other controller-supplied information arrives after an issue specification has been approved, the selected run's coordinator must reconcile that information against the current complete specification before any further assignment or checkout mutation. The coordinator either resubmits the unchanged specification, proving that the new information does not alter the behavioral contract, or persists a corrected full revision. The controller durably binds the information context to that specification revision, obtains a fresh independent semantic review for changed content, and supplies the approved active revision to every relevant execution and acceptance agent.

## Acceptance Criteria

- [x] A previously approved run receiving an unassessed feedback or new-information context cannot assign work or mutate its checkout until the coordinator reconciles the complete specification against that context.
- [x] Reconciliation with unchanged specification content durably binds the context to the already-approved revision without creating duplicate revision or review rows, and restart or retry resumes without asking for the same reconciliation again.
- [x] Reconciliation with changed specification content appends an immutable revision and requires a fresh independent approval before assignment or checkout mutation.
- [x] Execution and independent acceptance receive the approved active specification revision after reconciliation, including every atomic item, criterion, and verification mapping.

## Verification

- [x] INTEGRATION — start with an approved specification, supply new revision context, and prove the coordinator specification action precedes every assignment and checkout write.
- [x] INTEGRATION — reconcile unchanged content, recreate the service, and prove the durable context binding reuses the exact approved revision without another coordinator or reviewer action.
- [x] INTEGRATION — reconcile changed content and prove implementation remains paused through independent rejection, corrected revision, and exact-revision approval.
- [x] REGRESSION — run focused execution, specification, acceptance, publication, lifecycle, database, and interface suites.
