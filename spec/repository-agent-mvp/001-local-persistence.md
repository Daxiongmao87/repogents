# Local Persistence

Implements `MVP.md` §1 as the durable source of truth for one local user. This item owns the SQLite schema, transaction boundaries, filesystem identity layout, and state constraints consumed by every later item.

## Contract

- SQLite is authoritative for repository, sandbox, team, activation, run, publication, feedback, quiet-period, validation, outbound-operation, and notification state.
- Application-owned filesystem state is rooted at one configured data directory; database records use stable identifiers and paths beneath that root.
- State needed to reconcile an external side effect is committed before that effect begins.
- Time values are stored as UTC instants. Structured payloads are canonical JSON.
- The schema is initialized and migrated transactionally and enables foreign keys and WAL mode.
- Stored team-member execution bounds are versioned data. Migration from schema v1 preserves the historical 300-second action timeout for existing team versions.

## Acceptance Criteria

- [x] The application creates and reopens one durable SQLite database without losing records.
- [x] The schema stores every entity and relationship required by `MVP.md` §1.
- [x] Database constraints prevent duplicate activation runs, concurrent nonterminal runs for one issue, duplicate feedback versions, duplicate run pull requests, and duplicate quiet-generation notifications.
- [x] External effects can be represented as durable pending operations and reconciled after interruption.
- [x] Repository, sandbox, team, run, feedback, quiet-period, and notification data survive process restart.
- [x] Schema migration stores a positive action timeout for every team member and backfills existing versions without changing their historical behavior.
- [x] Concurrent schema-v2 initialization serializes version discovery with migration writes and converges without duplicate-column failure.

## Verification

- [x] `UNIT` — database initialization and migration are idempotent and foreign-key safe.
- [x] `UNIT` — each required uniqueness and nonterminal-run constraint rejects a conflicting transaction.
- [x] `INTEGRATION` — close and reopen the database and recover a populated nonterminal run with its referenced versions, pending operation, feedback, and deadline.
- [x] `UNIT` — migrate a populated schema-v1 team member, prove its 300-second timeout is retained, and prove repeated schema-v2 initialization is idempotent.
- [x] `INTEGRATION` — release two concurrent initializers against schema v1 and prove both complete with one schema-v2 migration.
