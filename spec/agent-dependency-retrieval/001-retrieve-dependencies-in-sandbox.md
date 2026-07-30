# Retrieve Dependencies Inside the Sandbox

## Prior Contracts

This item changes the dependency and network contracts established by `spec/repository-agent-mvp/003-stored-sandbox.md` and `spec/repository-evidence-inference/001-repository-evidence-inference.md`. Onboarding may still pre-provision reusable repository dependencies, but a run is no longer limited to hosts anticipated when its immutable sandbox version was created.

## Contract

- A run action may request a bounded list of exact dependency service hosts for that action. The controller validates and logs those destinations, combines them with the immutable repository policy only for that command, and never modifies the stored sandbox version.
- Action-scoped dependency destinations are exact public web services. Malformed, wildcard, excessive, or non-web requests fail before command execution. Direct networking, private addresses, credentials, and undeclared destinations remain unavailable.
- Agents and independent acceptance verifiers are told how to install missing packages and toolchains into the writable run dependency delta without modifying a read-only candidate checkout. Later sandbox commands in the same run can execute those retrieved dependencies.
- Dependency retrieval uses the existing restricted proxy, run-local writable storage, bounded command timeout, durable command logs, cancellation, and redaction paths.

## Acceptance Criteria

- [ ] A sandboxed agent can request exact public dependency services for one run action and retrieve a missing dependency into run-local storage.
- [ ] A later command in the same run can use the retrieved dependency while the immutable repository sandbox policy remains unchanged.
- [ ] Malformed, wildcard, excessive, or non-web dependency service requests fail before starting a sandbox command.
- [ ] Private, direct, unrelated, and no-longer-requested network destinations remain unreachable, and controller or repository credentials remain absent.
- [ ] Implementation agents and independent acceptance verifiers receive the same actionable dependency-retrieval contract.

## Verification

- [ ] `UNIT` — prove action-scoped dependency services reach only that sandbox invocation, leave the stored policy unchanged, and reject invalid requests before execution.
- [ ] `UNIT` — prove implementation and acceptance prompts and schemas expose the writable dependency workflow and exact per-action service requests.
- [ ] `INTEGRATION` — retrieve a dependency through the restricted proxy into a run dependency delta, use it in a later command without the transient destination, and prove unrelated/private egress remains blocked.
- [ ] `REGRESSION` — run the focused sandbox, execution-tool, and acceptance suites.
