# Durable Ready-Node Workflow Scheduler

Extends `spec/agent-workflow-graphs/001-model-designed-workflow-graph.md` and supersedes the fixed specialist loop established by `spec/stored-team-activation/001-stored-team-activation.md`. The outer durable run lifecycle, validation, acceptance, publication, feedback, cancellation, and repository pause contracts remain controller-owned.

## Contract

- The controller advances a compiled run graph by durable node state and dependencies rather than member-role order. A node becomes ready only after every required predecessor has completed successfully and its declared resource claims can be acquired.
- Node states are `pending`, `ready`, `running`, `succeeded`, `failed`, `blocked`, `skipped`, or `canceled`. Every attempt and resource claim is committed before process or model execution; a validated output or terminal failure is committed before downstream readiness changes.
- Independent ready nodes may execute concurrently. Read-only agent nodes and pure deterministic nodes can overlap; nodes claiming the same exclusive resource cannot overlap. The repository checkout is exclusive for writing unless the controller explicitly provides distinct isolated writable workspaces.
- Agent nodes retain the existing stored-member runtime, model, tool permissions, sandbox, cancellation, secret-redaction, bounded-action, and action-history contracts. Each node has independently durable progress and output so one completed node is not repeated after controller reconstruction.
- Deterministic nodes reference controller-registered operations. Each operation declares typed input and output schemas, purity, resource claims, and a bounded callable. Graphs may bind constant parameters and named predecessor outputs, but cannot provide executable source or expand the operation's permissions.
- Deterministic inputs are schema-validated before invocation and outputs before persistence. Invalid inputs block that node without invocation; invalid outputs fail the attempt and do not release successors.
- A controller restart reconciles abandoned `running` attempts to retryable `pending` state while preserving attempt history. Successfully completed nodes and their outputs are never re-executed merely because the process restarted.
- Failure, cancellation, pause, and retry operate through durable graph state. No downstream node runs after a failed or blocked required predecessor. Canceling a run marks unfinished nodes canceled without changing completed evidence.
- Successful coordinator and verifier nodes return control to the existing exact-SHA commit and validation boundary. Revision requests create a bounded new graph generation rather than mutating or erasing the prior generation.
- At controller-owned assessment checkpoints, the coordinating member receives a bounded performance record containing node outcomes, attempts, durations, redacted errors, validated outputs and handoffs, resource contention, and verifier or validation feedback. It assesses whether the current topology, prompts, deterministic parameters, or member selection impeded the issue.
- The coordinating member may finish the assessment unchanged or propose a revised graph generation with a specific evidence-based reason. A proposal may change dependencies, selected optional nodes, registered deterministic operations and parameters, or node-specific prompts, but cannot change immutable team members, broaden tools/resources, supply executable source, weaken controller gates, or erase completed attempts.
- A revision is validated against the same graph and security contract, then stored as a new immutable generation. Prior generations and assessments remain queryable. A completed node result may be reused only when the controller-derived node definition, prompt, inputs, dependencies, resources, and operation version hash are unchanged; otherwise the node starts pending in the new generation.
- Revision counts are bounded. Invalid or exhausted proposals leave the current generation and evidence intact and return a concrete controller error to the coordinating member rather than partially mutating execution state.

## Acceptance Criteria

- [x] Ready-node selection is dependency-driven, restart-safe, and records every attempt before execution and every validated result before enabling successors.
- [x] Independent read-only or pure nodes execute concurrently while conflicting exclusive-resource nodes remain serialized.
- [x] Agent nodes use their stored team-member configuration and preserve independent durable progress, outputs, action history, cancellation, and redaction.
- [x] Each agent action is authorized by both the immutable member tool set and the compiled node resource claims; a node cannot use undeclared checkout read, checkout write, diff, or command capabilities.
- [x] Registered deterministic operations accept custom typed inputs and outputs without permitting inline executable code, undeclared resources, secrets, or external effects.
- [x] Invalid deterministic inputs, invalid outputs, node failures, cancellation, pause, and controller reconstruction preserve an honest durable graph state and never execute an ineligible successor.
- [x] Completion returns the candidate through the existing exact-SHA validation, acceptance, and publication contracts; revision work is represented as a bounded immutable graph generation.
- [x] The coordinating member receives durable, privacy-safe node performance evidence and can either retain the current workflow or propose an evidence-based topology, prompt, parameter, or selected-node adjustment.
- [x] Every accepted adjustment creates a validated immutable generation, preserves prior assessments and attempts, reuses results only under exact controller-derived identity, and cannot broaden authority or weaken mandatory gates.
- [x] Invalid, partial, or exhausted graph revisions fail atomically with a concrete error and leave the active generation executable and unchanged.
- [x] New issue or pull-request feedback cannot reuse an already accepted generation unchanged: the controller durably opens one feedback-bound revision generation before source work, and a restart after generation persistence resumes that same generation without asking the coordinator to create a duplicate.

## Verification

- [x] `UNIT` - prove readiness, fan-out, fan-in, skipped/ineligible successors, and deterministic tie ordering from a compiled DAG.
- [x] `UNIT` - register a custom pure operation with object input/output schemas; prove valid binding succeeds and invalid input, invalid output, undeclared operation, and executable payloads fail closed.
- [x] `INTEGRATION` - block two independent read-only nodes at a barrier and prove both run concurrently, then prove two checkout-writing nodes never overlap.
- [x] `INTEGRATION` - interrupt after attempt start and after output commit, reconstruct the scheduler, and prove only incomplete work retries while completed work remains single-execution.
- [x] `INTEGRATION` - run assigned specialists, coordinator, and verifier from graph state and reach the existing exact-SHA validation boundary.
- [x] `SECURITY` - attempt read, write, diff, and command actions from agent nodes lacking the matching resource claim and prove each action is rejected before sandbox invocation.
- [x] `UNIT` - provide node performance evidence to a scripted coordinator and accept an unchanged assessment plus topology, prompt, parameter, and optional-node revisions while rejecting authority expansion and unsupported logic.
- [x] `INTEGRATION` - revise a live graph generation after weak specialist output, prove changed nodes rerun, unchanged exact-identity outputs are reused, and the original generation and assessment remain immutable.
- [x] `REGRESSION` - exhaust the revision bound and interrupt revision persistence, then prove the last valid generation resumes without duplicate completed work or a partial graph.
- [x] `INTEGRATION` - complete and accept an initial graph, deliver pull-request feedback, interrupt after the feedback revision generation is committed, and prove restart executes that same generation once instead of skipping feedback or creating another generation.
- [x] `REGRESSION` - run execution, lifecycle retry/restart, cancellation, pause, validation, feedback, acceptance, and publication suites.
