# Replace OMP with mini-SWE-agent

Supersedes the OMP-specific runtime clauses and completed OMP verification claims in `spec/repository-agent-mvp/006-autonomous-execution.md`. It also changes the configured runtime consumed by `spec/repository-agent-mvp/004-stored-agent-team.md`, `spec/repository-agent-mvp/007-pull-request-publication.md`, `spec/repository-agent-mvp/008-feedback-resolution.md`, and `spec/repository-agent-mvp/011-mvp-acceptance.md`. Runtime-neutral behavior in those prior items remains in force.

## Contract

- `mini-swe-agent==2.4.5` is the only production inference harness. Repogents does not invoke OMP or Pi, read OMP/Pi configuration, or retain an OMP compatibility path.
- Repogents uses mini-SWE-agent's `DefaultAgent` and LiteLLM model adapter for model querying, retry/format handling, and linear trajectory state. A thin Repogents adapter converts one structured model decision into the existing controller action contract.
- Mini-SWE-agent never executes repository commands itself. It does not use upstream `LocalEnvironment`; every repository read, write, search, command, assignment, note, finish, and block action remains authorized and executed by Repogents through the stored sandbox and controller boundaries.
- Every configured team member stores runtime `mini-swe-agent`, an explicit LiteLLM model selector, and its immutable action timeout. Existing team versions that name another runtime are not silently migrated or executed; explicit repository re-onboarding creates a new version.
- Startup requires an explicit product-owned model selector from `--model` or `REPOGENTS_MODEL`. No model or provider default is discovered from a user home directory, another agent harness, or ambient host configuration.
- An optional OpenAI-compatible base URL is supplied explicitly through `--model-base-url` or `REPOGENTS_MODEL_BASE_URL`, stored with the assembled application runtime, and passed to LiteLLM without consulting another harness.
- Each inference call runs in a supervised child process with a file-backed request and response. The process receives only locale/TLS/runtime necessities, its selected provider credential, and an isolated `MSWEA_GLOBAL_CONFIG_DIR` beneath Repogents state. It receives no `HOME`, OMP/Pi paths, GitHub credential, sandbox secret, or unrelated controller environment variable.
- The mini-SWE trajectory is versioned, bounded, and stored outside the checkout. It contains normalized redacted messages and controller decisions, not raw provider response payloads or credentials, and remains usable after application restart.
- Onboarding repository inference, stored-member issue execution, publication scope review, and pull-request feedback evaluation all use the same mini-SWE boundary and the applicable explicit or stored model configuration.
- Model timeout and durable cancellation semantics remain those of the referenced stored team version. Cancellation terminates the complete supervised mini-SWE child before any later controller action or external mutation.
- The earlier OMP-generated live acceptance is not evidence for this runtime contract. Final acceptance must exercise `Daxiongmao87/websesh#1` through a newly onboarded mini-SWE team and current integrated source.

## Acceptance Criteria

- [x] The project pins `mini-swe-agent==2.4.5`, and every newly formulated stored member uses runtime `mini-swe-agent` with an explicit model and positive timeout.
- [x] Startup fails clearly without an explicit Repogents model selector and never resolves a model from OMP, Pi, mini-SWE user configuration, or the host home directory.
- [x] Official OpenAI and explicitly configured OpenAI-compatible endpoints are represented by product-owned model configuration and reach the mini-SWE LiteLLM adapter.
- [x] A stored model selector may include an explicit supported reasoning-effort suffix; the mini-SWE worker removes that suffix from the endpoint model identifier and sends it as the provider `reasoning_effort` without changing the stored selector.
- [x] Repository onboarding bounds the complete serialized inference prompt so large repositories fit the supported model context without dropping total file counts or deterministic inspection results.
- [x] Repository onboarding supplies the inference model with the actual sandbox privilege and writable-filesystem contract, so provisioning decisions are made against the environment that will execute them rather than an assumed mutable host.
- [x] Explicit re-onboarding returns bounded prior failure evidence to the repository agent for a new inference decision; Repogents does not hardcode or synthesize corrective commands, hosts, or allowlist entries from an agent’s mistake.
- [x] A model omission cannot remove dependency-service access deterministically required by recognized repository manifests; model-inferred and source-derived services are combined before provisioning.
- [x] Node package manifests deterministically authorize both the npm registry and `nodejs.org:443`, allowing native-addon provisioning to fetch the exact runtime headers without broadening sandbox egress.
- [x] Mini-SWE produces schema-valid controller actions while all repository operations remain mediated by existing permission and sandbox enforcement.
- [x] A schema-invalid controller action is returned to mini-SWE as a tool-correlated format error for bounded correction; one malformed model attempt does not block onboarding or run execution when the next attempt is valid.
- [x] Third-party model-library stdout cannot contaminate the worker’s structured response channel; the parent receives exactly one decision JSON document.
- [x] The worker environment excludes `HOME`, OMP/Pi configuration variables, GitHub credentials, unrelated provider credentials, and repository secret bindings while retaining only the selected provider credential and required runtime/TLS values.
- [x] Mini-SWE inference is file-backed, bounded by the stored timeout, supervised for cancellation, and persists normalized versioned trajectory state outside the checkout without raw provider payloads.
- [x] Onboarding inference, stored-team execution, publication scope review, and feedback evaluation all use mini-SWE and reject obsolete stored runtime identifiers without a compatibility fallback.
- [x] Existing runtime-neutral lifecycle, validation, publication, feedback, secret-redaction, and restart behavior remains passing after the clean cutover.
- [x] Stored-agent execution rejects repeated coordination notes until a successful repository mutation advances the recorded next step; inspection, no-op edits, and rejected control actions do not clear the guard or allow note/assignment loops to consume an action quantum.
- [x] The complete live `websesh#1` acceptance path is repeated with a newly stored mini-SWE team; OMP-produced evidence is not credited.

## Verification

- [x] `UNIT` — model configuration rejects missing selectors, preserves explicit selectors/base URLs, and cannot discover or invoke an OMP/Pi default even when poisoned host configuration and executables are present.
- [x] `UNIT` — `openai/codex/gpt-5.6-terra:medium` reaches LiteLLM as model `openai/codex/gpt-5.6-terra` with `reasoning_effort=medium`.
- [x] `UNIT` — oversized repository evidence is deterministically reduced to a serialized prompt below the product limit while retaining repository identity, total file count, initial observations, and a bounded file/content sample.
- [x] `UNIT` — the onboarding inference packet identifies immutable system locations, writable persistent/run locations, the absence of root/system-package installation, and the supported repository-local tool bootstrap path.
- [x] `INTEGRATION` — a blocked repository’s prior inference or provisioning failure is supplied to mini-SWE on explicit re-onboarding, while the resulting commands and dependency services remain entirely model-selected and schema-validated.
- [x] `INTEGRATION` — when mini-SWE returns no dependency services for a recognized package manifest, onboarding still provisions with the manifest-derived registry allowlist and preserves any additional model-inferred services.
- [x] `INTEGRATION` — package-manifest onboarding retains model-inferred services and authorizes npm package downloads plus native Node header downloads through the restricted proxy.
- [x] `UNIT` — the mini-SWE worker uses `DefaultAgent`, parses exactly one schema-valid controller decision, rejects malformed/multiple decisions, and writes a normalized bounded trajectory without raw provider response data.
- [x] `UNIT` — the decision environment converts malformed marker, JSON, and schema output into mini-SWE `FormatError`, preserves the action’s tool-call correlation in a tool-role correction message, and a corrected second action completes through `Submitted`.
- [x] `UNIT` — provider diagnostics printed during inference are captured away from worker stdout while the final decision remains the only stdout document.
- [x] `UNIT` — environment construction selects only the configured provider credential, omits `HOME`, OMP/Pi variables, GitHub/controller/sandbox secrets, and points `MSWEA_GLOBAL_CONFIG_DIR` at isolated Repogents state.
- [x] `ADAPTER` — a file-backed mini-SWE child receives explicit model/endpoint configuration, returns a controller action, honors timeout/cancellation supervision, and never executes a repository command directly.
- [x] `INTEGRATION` — onboarding, lead/member execution, scope review, and feedback evaluation consume the stored `mini-swe-agent` runtime/model across restart and reject an obsolete runtime row.
- [x] `INTEGRATION` — run the complete deterministic suite and smoke-test the supported local workflow with no OMP process or configuration dependency.
- [x] `UNIT` — after one actionable note, another note remains rejected across a disallowed reassignment, a successful read, and a no-op replacement; the stored agent sees each rejection, performs a real source edit, and only that successful mutation permits a later note.
- [x] `LIVE` — through the current application and a newly onboarded team, execute the complete `websesh#1` issue, publication, real-feedback, restart, and 30-minute quiet-notification path using mini-SWE-agent.

## Coordination

- Requested behavior: make the runtime choice a hard constraint by replacing OMP cleanly with mini-SWE-agent; do not retain OMP/Pi execution, ambient configuration discovery, compatibility aliases, or unrelated product changes.
- Frozen integration boundary: `repogents.mini_swe.MiniSweInference` accepts an explicit model, optional base URL, timeout, supervisor/run identity, response schema, prompt, and state directory; it returns one JSON object. `repogents.mini_swe_worker` is the only module importing mini-SWE-agent and never executes repository actions.
- Frozen stored values: runtime identifier `mini-swe-agent`; exact package version `2.4.5`; model selector is nonempty and explicit; obsolete runtime rows fail and require re-onboarding.
- Wave 1 owns tests only: `tests/test_mini_swe.py` plus OMP-specific assertions in `tests/test_controller.py`, `tests/test_execution.py`, `tests/test_onboarding.py`, `tests/test_feedback.py`, `tests/test_publication.py`, `tests/test_app.py`, `tests/test_team.py`, and `tests/test_database.py`. Focused red checks are required; no production edits.
- Wave 2 has one production owner for the shared boundary (`pyproject.toml`, `repogents/mini_swe.py`, `repogents/mini_swe_worker.py`, `repogents/controller.py`) before dependent callsites are changed. Dependent callsite files have exclusive owners and consume only the frozen boundary.
- Integration barrier: stop all writers, run focused runtime/controller/onboarding/execution/feedback/publication/app/team/database tests, then run diagnostics and the complete deterministic suite once.
- Verification tiers: worker/configuration invariants are `UNIT`; child-process, timeout, and cancellation behavior are `ADAPTER`; composed onboarding/execution/publication/feedback and restart are `INTEGRATION`; final `websesh#1` evidence is `LIVE`.
- Required local inputs: Python 3.10+, the pinned package, an explicit LiteLLM model, and its dedicated provider credential. Required live inputs remain the authorized GitHub identity, current fixture repository/issue/PR, sandbox services, and intended network path. No host or external mutation occurs before the integration barrier is green.
