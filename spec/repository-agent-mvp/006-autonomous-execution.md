# Autonomous Implementation and Validation

Implements `MVP.md` §6. The stored lead and assigned members infer issue intent from the issue, discussion, repository instructions, current source, and repository conventions. Terse issues are normal runtime input, not missing product configuration.

## Contract

- Model/runtime processes stay outside repository command sandboxes and receive no raw sensitive values.
- Agent repository tools are controller-owned operations that read/write only the isolated checkout and run commands only through the stored sandbox policy.
- Agents may inspect, edit, and validate; they may not publish, access controller credentials, modify unrelated host state, or escape their assigned run.
- The lead records its assignment and scope decision, resolves ambiguity from evidence, revises until behavior and repository-required validation pass, or blocks with a specific irreducible reason.
- Every validation record binds run, exact commit SHA, command, timestamps, exit status, and external log path. Only the exact passing SHA can advance to publication.

## Acceptance Criteria

- [x] A configured model runtime can drive repository inspection, scoped edits, and sandboxed commands through controller tools.
- [x] Repository-specific behavior, validation, and likely change scope are inferred without target-specific product code or required predeclared fixture metadata.
- [x] Agents can modify only the isolated checkout and cannot receive GitHub/model credentials or raw secret bindings.
- [x] Required validation commands run in the sandbox and produce durable records bound to the exact committed SHA.
- [x] Publication cannot select an unvalidated or subsequently changed SHA.
- [x] Irreducible requirements, dependency, authorization, sandbox, validation, or external-operation failures produce a durable blocked run rather than an invented fallback or endless retry.
- [x] Recoverable inference/controller failures and source-fixable commit or secret-scan findings return to automatic execution with durable feedback instead of entering `blocked`.
- [x] Restart during validation resumes from durable checkout state without repeating issue activation or discarding scoped edits.
- [x] Stored applicable repository instructions constrain lead and scope-review decisions while ambient host rules cannot require repository process artifacts or alter target behavior.
- [x] Large repository evidence and bounded action history reach OMP without exceeding operating-system argument limits.
- [x] OMP inference, feedback, and scope-review subprocesses receive an allowlisted environment without GitHub or unrelated controller secrets.
- [x] Existing runs instantiate their immutable stored member runtime/model configuration; changing CLI defaults affects only explicitly re-onboarded versions.
- [x] Execution rechecks durable cancellation after inference and before every controller tool or validation action.
- [x] Stored-agent model actions use a configurable positive bounded timeout that accommodates the configured live runtime without disabling automatic retry.
- [x] A stored agent can persist a non-mutating inspection note in bounded action history, and the runtime directs it to stop rereading covered evidence and proceed from that note.
- [x] Notes and action snapshots are size-bounded and recursively redact resolved secrets before JSON escaping; rejected snapshots retain correction-critical routing fields ahead of truncated bulk payloads.
- [x] Assignment reasoning is size-bounded and redacts every command-scoped secret resolved earlier in the pre-assignment action cycle before reaching SQLite, action history, or later member context.

## Verification

- [x] `UNIT` — tool authorization rejects paths outside the run and commands outside the sandbox boundary.
- [x] `ADAPTER` — model tool-call requests and final decisions are parsed, bounded, logged, and correlated to stored members.
- [x] `INTEGRATION` — a fixture agent inspects, edits, commits, and validates an isolated repository using only controller tools.
- [x] `INTEGRATION` — resume a run persisted in `validating` and produce exact-SHA validation and publication readiness.
- [x] `UNIT` — stored repository instructions constrain an edit and OMP receives large prompts through file-backed input with bounded history.
- [x] `UNIT` — model subprocess environments exclude controller credentials and stored runtime/model selection survives restart/configuration changes.
- [x] `INTEGRATION` — cancel while inference is in flight and prove no later tool, commit, or validation action executes.
- [x] `INTEGRATION` — transient inference failure, commit hygiene failure, and a removable secret finding each continue automatically on the same run and reach validation after correction.
- [x] `UNIT` — the configured stored-agent timeout reaches the model runner and nonpositive values are rejected.
- [x] `INTEGRATION` — persist an inspection note, prove it reaches the next model action after restart-safe serialization, and complete the edit without invoking a repository tool for the note.
- [x] `UNIT` — a rejected controller action and its redacted failure reason are both persisted so a stateless next model action can correct the specific failure.
- [x] `UNIT` — after resolving a secret containing JSON-escaped characters, persist a note and rejected action containing it and prove neither raw nor escaped secret reaches history or later model context.
- [x] `UNIT` — a rejected write with a multi-kilobyte body retains its action and path in the next bounded model context.
- [x] `UNIT` — run an authorized secret-bearing command before assignment, then prove the assignment reasoning and history are bounded and contain neither the raw nor JSON-escaped secret.
- [x] `LIVE` — process and scope-review a target repository that deliberately ignores local specification ledgers, proving no ambient rule causes `.gitignore` or specification files to enter the issue commit.
- [x] `LIVE` — a configured inference runtime implements a real public issue from terse/current GitHub and repository evidence and records passing validation for the published commit.
