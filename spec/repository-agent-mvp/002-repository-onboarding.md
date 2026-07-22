# Repository Onboarding and Inventory

Implements `MVP.md` §2. Target repositories are runtime evidence, never hard-coded product assumptions. The lead derives everything safely derivable from repository source and asks for input only when a required fact cannot be inferred.

## Contract

- Repository identity is resolved to stable GitHub owner/name/id metadata and its current default branch.
- Onboarding clones or refreshes application-owned source, inspects repository instructions, manifests, lockfiles, build/test configuration, and repository-defined commands through the sandbox, then stores immutable sandbox and team versions.
- Repository inputs are limited to genuinely non-derivable host paths, licenses, datasets, secret references, services, provisioning details, and explicit command overrides.
- Existing runs retain their loaded version identities. Only explicit re-onboarding creates new versions.
- Public-first acceptance repositories are `Daxiongmao87/bazzeye` and `Daxiongmao87/foundry-portal`.

## Acceptance Criteria

- [x] A GitHub repository URL or owner/name identity can be added through the local interface.
- [x] Onboarding derives repository metadata, environment evidence, validation commands, and team evidence without target-specific code.
- [x] Derivable manifests, dependency services, provisioning steps, and validation commands are discovered from repository evidence rather than a fixed Python/JavaScript/Rust/Go allowlist.
- [x] Successful onboarding stores immutable sandbox and team versions and marks the repository `ready`.
- [x] Missing non-derivable information produces `needs_input` with the exact missing input; other failures produce `blocked` with evidence.
- [x] Re-onboarding is explicit, creates new versions, and does not change versions referenced by existing runs.
- [x] All repositories, including blocked ones, remain visible after restart.
- [x] Supplied host paths, services, secret references, provisioning commands, and validation-command overrides are normalized, validated, and applied during onboarding; a repository without usable validation remains `needs_input`.
- [x] Re-onboarding refreshes GitHub metadata/default branch and retains the current sanitized inputs unless the user explicitly changes them.
- [x] Controller-side inspection rejects repository symlinks or resolved paths that escape the cloned source root.
- [x] Restart reconciliation turns interrupted onboarding work into an actionable, evidence-preserving state instead of leaving it indefinitely in progress.

## Verification

- [x] `ADAPTER` — GitHub repository metadata and default-branch discovery are parsed from the concrete client response.
- [x] `INTEGRATION` — onboard two structurally different public repositories and persist separate evidence, sandbox versions, team versions, and validation commands.
- [x] `INTEGRATION` — restart, load both inventory records without rerunning onboarding, then explicitly re-onboard one and preserve the other and existing run references.
- [x] `CLIENT` — add, inspect, and re-onboard repositories through the supported local interface.
- [x] `UNIT` — invalid policies, missing validation, repository symlinks, retained-input updates, metadata refresh, and interrupted-onboarding recovery are deterministic.
- [x] `INTEGRATION` — supplied mounts/services/provisioning/validation overrides are used while building a ready stored sandbox.
- [x] `INTEGRATION` — onboard a repository outside the built-in Python/JavaScript fixture shapes and derive its declared dependency/provisioning and validation path without an unnecessary `needs_input`.
