# Remove Onboarding Dependency Prescription

## Prior Contracts

This item changes `spec/repository-agent-mvp/002-repository-onboarding.md`, `spec/repository-evidence-inference/001-repository-evidence-inference.md`, `spec/provisioned-executable-permissions/001-preserve-bootstrap-executable-permissions.md`, and `spec/agent-dependency-retrieval/001-retrieve-dependencies-in-sandbox.md`. It supersedes the requirement that onboarding infer, execute, or persist repository dependency and toolchain provisioning commands or inferred dependency-service allowlists. It retains repository inspection, immutable sandbox/team versions, repository validation contracts, explicit non-derivable resource bindings, and action-scoped dependency retrieval during issue execution and independent acceptance.

## Contract

- Onboarding inspects bounded repository evidence and uses the configured inference runtime to derive repository characteristics and validation commands for a model-designed stored team and workflow graph. The inference response does not prescribe dependency-installation commands, toolchain bootstraps, or ecosystem registry hosts.
- Onboarding never executes target-repository dependency or toolchain setup. A ready sandbox version records isolation policy, explicit resource bindings, repository evidence, and an initially empty reusable dependency area; readiness does not depend on eagerly installing dependencies.
- Repository-supplied mounts, secret references, validation overrides, and exact public service boundaries remain normalized controller inputs. The obsolete `provisioning_commands` input is removed from retained repository inputs during schema migration and is rejected on new input.
- Existing immutable sandbox versions and runs retain their stored evidence and identities. Newly onboarded or explicitly re-onboarded repositories use the issue-time dependency contract without rewriting historical versions.
- During issue execution and independent acceptance, agents determine the dependencies and tools required by the current issue and checkout. A command may request only bounded exact public HTTP/HTTPS services for that action, may write reusable results only in run-local dependency state, and remains subject to controller validation, isolation, timeouts, credential scoping, and exact-SHA validation.
- Controller prompts describe those capabilities and boundaries without supplying package-, ecosystem-, or repository-specific dependency plans. Repository agents choose commands from current repository and issue evidence.

## Acceptance Criteria

- [x] Repository inference and stored new-version evidence contain no model-authored provisioning commands or inferred dependency-service allowlists.
- [x] Onboarding reaches `ready` after repository inspection, validation-contract derivation, sandbox-policy persistence, and stored team/workflow formulation without executing repository dependency commands.
- [x] New repository inputs reject `provisioning_commands`; migration removes that obsolete key from retained repository inputs while preserving all other inputs and historical sandbox versions.
- [x] Explicit mounts, secret bindings, validation overrides, and exact service boundaries remain available without becoming inferred ecosystem policy.
- [x] Issue and acceptance agents can retrieve a newly discovered dependency through an action-scoped exact public service into run-local dependency state, while undeclared/private destinations remain blocked.
- [x] Agent instructions expose generic dependency capabilities and safety constraints but contain no repository-, language-, package-manager-, or toolchain-specific install plan.

## Verification

- [x] `UNIT` - parse repository inference without provisioning fields and reject responses or inputs that attempt to restore onboarding provisioning commands.
- [x] `INTEGRATION` - onboard structurally different fixtures and prove no dependency command runs while immutable ready sandbox/team/workflow versions are stored.
- [x] `MIGRATION` - upgrade a populated schema with retained `provisioning_commands`, preserve other inputs and historical sandbox evidence, and remove only the obsolete key.
- [x] `UNIT` - inspect execution and acceptance requests and prove they describe only generic action-scoped dependency retrieval and controller-owned constraints.
- [x] `INTEGRATION` - retrieve one issue-discovered dependency through the restricted proxy into run-local state and reject an undeclared or private destination.
- [x] `REGRESSION` - run focused onboarding, sandbox, execution, acceptance, and interface suites.
