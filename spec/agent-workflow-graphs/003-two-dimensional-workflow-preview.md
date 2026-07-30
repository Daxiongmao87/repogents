# Two-Dimensional Workflow Graph Preview

Extends `spec/agent-workflow-graphs/001-model-designed-workflow-graph.md` and `002-durable-ready-node-scheduler.md` with a read-only representation of the stored orchestration contract and live execution state.

## Contract

- The local interface exposes the current repository workflow template and each run's immutable compiled graph, nodes, edges, generations, attempts, typed parameter metadata, resource claims, and live status without secret values or executable payloads.
- The repository view previews the project workflow template. Selecting a run shows the exact compiled graph and active generation used by that run rather than reconstructing a graph from current team settings.
- The dashboard renders a stable two-dimensional dependency layout: dependency depth determines columns, deterministic stable ordering determines rows, and parallel branches remain visually distinct. Layout coordinates are presentation data and do not affect execution semantics.
- Agent, deterministic, coordinator, verifier, and controller boundary nodes are visually distinguishable. Locked controller behavior cannot be mistaken for an editable model-designed node.
- Selecting a node exposes its role or registered operation, input/output metadata, dependencies, resource claims, state, attempts, timing, and redacted error or artifact references.
- The run view exposes graph-generation lineage and each coordinating-member performance assessment. Users can compare the exact changed topology, prompts, parameters, reuse decisions, and evidence-based revision reason without exposing model chain-of-thought or secret data.
- Live activity refresh updates node and edge state without losing the selected repository, run, node, scroll position, or keyboard focus.
- Every graph has an accessible tabular equivalent containing the same nodes, dependencies, execution state, and properties. Node selection and graph traversal are keyboard operable; status is not conveyed by color alone.
- The graph preview is read-only. No dashboard action can mutate an immutable template, compiled graph, node result, or execution order.

## Acceptance Criteria

- [x] Repository and run state APIs expose privacy-safe workflow templates and exact compiled live graphs with stable node, edge, generation, attempt, resource, and property projections.
- [x] The dashboard renders a stable two-dimensional dependency graph with distinct parallel branches and visually differentiated agent, deterministic, coordinator, verifier, and locked controller nodes.
- [x] Selecting a node shows its complete safe properties and live attempt state; activity refresh preserves current user context.
- [x] The run preview exposes immutable generation history, coordinator assessments, revision reasons, exact safe graph deltas, and which prior node outputs were reused or rerun.
- [x] A keyboard-operable tabular equivalent exposes the same execution and dependency information, and status remains understandable without color.
- [x] The preview offers no mutation path and exposes no secret value, arbitrary executable source, or unredacted controller error.

## Verification

- [x] `UNIT` - project a stored template and multi-generation run graph and prove stable ordering, exact run-version binding, redaction, and omission of executable or secret values.
- [x] `CLIENT` - render a branched graph, select every node type, inspect properties, and prove an activity update preserves repository/run/node selection and focus.
- [x] `CLIENT` - switch between two immutable generations and verify topology, prompt/parameter deltas, coordinator assessment, and reused-versus-rerun nodes remain understandable and correctly bound.
- [x] `ACCESSIBILITY` - traverse the graph and table by keyboard and prove the tabular dependency/status content matches the visual graph without relying on color.
- [x] `LIVE` - onboard or re-onboard a repository with a model-designed graph, activate one issue, and inspect the stored project preview plus live compiled graph in Chromium without invoking any mutation control.
