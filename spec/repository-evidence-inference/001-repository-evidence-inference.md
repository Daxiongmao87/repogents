# Repository Evidence Inference

## Prior Contract

This item completes `spec/repository-agent-mvp/002-repository-onboarding.md`. It supersedes any interpretation that a fixed language, manifest, package-manager, registry, or test-command allowlist is sufficient repository-agnostic onboarding.

## Contract

- Deterministic inspection gathers a safe repository evidence packet: bounded path inventory plus relevant repository instructions, build manifests, task definitions, and continuous-integration configuration.
- The configured controller-side inference runtime derives languages, manifests, provisioning commands, dependency services, validation commands, and team evidence from that packet. Target repository names and issue fixtures are never hard-coded.
- Deterministic discoveries may seed inference but do not limit accepted ecosystems. Repository-declared Ruby or another previously unknown ecosystem can be onboarded without a product-code change.
- Inferred commands and services are normalized and validated before storage. Commands execute only in the onboarding sandbox; network access remains restricted to inferred or explicitly supplied hosts.
- Repository-required toolchains absent from the baseline host are bootstrapped by inferred commands into persistent repository state. Reusable dependency outputs are directed to one ecosystem-neutral dependency-delta root that is persisted and seeded into later runs; tool and dependency paths remain available without exposing the controller home.
- Only genuinely non-derivable facts enter `needs_input`. Inference failures use the existing onboarding failure state and remain visible for explicit re-onboarding.
- Explicit retained user inputs override inference only for the named fields and remain versioned through explicit re-onboarding.

## Acceptance Criteria

- [x] Repository source files and evidence drive provisioning, dependency services, validation commands, and team formation through configured inference, rather than a fixed Python/JavaScript/Rust/Go allowlist.
- [x] Deterministic discoveries may seed inference but do not limit accepted ecosystems. Repository-declared Ruby or another previously unknown ecosystem can be onboarded without a product-code change.
- [x] Structurally different repositories produce repository-specific inferred provisioning, validation, network, and team evidence.
- [x] Accepted inferred commands, services, and team evidence are persisted in immutable sandbox/team versions and reused across restart and issue runs.
- [x] Only genuinely non-derivable facts enter `needs_input`. Inference failures use the existing onboarding failure state and remain visible for explicit re-onboarding.
- [x] Repository-required toolchains absent from the baseline host can be installed by inferred bootstrap commands, and inferred dependency outputs from any ecosystem persist into later validation runs.
- [x] Runs referencing a previously stored Python/Node dependency layout continue after restart while new onboarding versions use the ecosystem-neutral layout.
- [x] The repository inference process has a configurable positive bound long enough for the configured live model without making the controller unbounded.
- [x] Inferred Node provisioning uses the sandbox's pre-mounted writable `node_modules` path rather than attempting to replace the mount.

## Verification

- [x] `UNIT` - the configured inference adapter receives bounded generic repository evidence through a file-backed prompt, uses the explicit model environment, and validates its structured result.
- [x] `UNIT` - parse and validate inference output, rejecting malformed commands, unsafe service values, and incomplete evidence.
- [x] `INTEGRATION` - onboard Python/JavaScript and Ruby fixtures through the same evidence-inference path and compare distinct stored sandbox/team/validation records.
- [x] `UNIT` - every inferred provisioning command supplies an explicit bounded timeout to the concrete sandbox adapter.
- [x] `UNIT` - inference requests idempotent toolchain bootstrap commands and ecosystem-neutral durable dependency outputs; the sandbox exposes both during provisioning and later read-only execution.
- [x] `ADAPTER` - inferred provisioning and allowlisted egress execute through Bubblewrap while unauthorized destinations remain denied.
- [x] `HOST` - a run mounts a previously stored Node dependency read-only through its writable run delta after the controller source is upgraded.
- [x] `UNIT` - the configured repository-inference timeout reaches the file-backed model runner and rejects nonpositive values.
- [x] `UNIT` - the inference contract names the Node dependency mount and prohibits replacing it with a symlink.
- [x] `REGRESSION` - run metadata refresh, retained-input, re-onboarding, and interrupted-onboarding recovery suites.
