# Render the Cyclic Controller Lifecycle

Extends `spec/agent-workflow-graphs/003-two-dimensional-workflow-preview.md`. The executable graph contract from `spec/agent-workflow-graphs/001-model-designed-workflow-graph.md` and `002-durable-ready-node-scheduler.md` remains acyclic. This item adds a projection-only system view of controller-owned iteration around each immutable generation.

## Contract

- The repository template preview and every run-generation preview expose an explicit virtual origin for issue activation and the immutable run contract. A live origin identifies the run, issue version, team version, sandbox version, exact base SHA, and selected generation without exposing secret values.
- Solid graph edges remain executable dependencies only. Visually distinct dashed controller edges show activation into zero-indegree work, durable node-attempt retry, coordinator revision into generation N+1, validation or acceptance remediation into generation N+1, pull-request feedback into a later feedback generation, and termination after closed or canceled outcomes. Dashed edges are presentation data and never enter topological ordering or ready-node scheduling.
- Controller loops return to the run-contract origin and name the trigger and next durable unit (`attempt N+1` or `generation N+1`) so the system is cyclic for human comprehension without implying an executable backedge inside the selected generation.
- The preview retains immutable-generation selection, assessment history, and reused-versus-rerun deltas. It also exposes a visible terminal-outcome boundary and an accessible lifecycle-transition table equivalent to the dashed visual edges.
- Origin, lifecycle transition, generation identity, and terminal-outcome details remain understandable without color. Selection, keyboard traversal, graph scroll retention, and read-only behavior remain intact.

## Acceptance Criteria

- [x] Template and run projections include an explicit run-contract origin, terminal-outcome boundary, and typed projection-only lifecycle transitions while leaving executable nodes and dependency edges unchanged.
- [x] A live origin exposes run, issue-version, team-version, sandbox-version, exact-base-SHA, and generation identity; the template origin explains the future immutable binding without fabricating live identifiers.
- [x] The graph renders solid executable dependencies and labeled dashed activation, retry, revision, validation-remediation, acceptance-remediation, feedback, and termination transitions, with every iterative transition returning to the run-contract origin.
- [x] The accessible view lists the same lifecycle transition types, triggers, sources, targets, and next durable units without relying on color, and virtual node details explain their controller-owned semantics.
- [x] Generation selection, assessment history, reused-versus-rerun deltas, keyboard node navigation, scroll retention, and the acyclic scheduler semantics remain unchanged.

## Verification

- [x] `UNIT` - project a template and a live run generation, assert the origin and terminal metadata plus every lifecycle transition type, and prove the original executable node and edge sets are unchanged.
- [x] `CLIENT` - assert the dashboard contract includes distinct dependency and lifecycle edge styles, labeled loop paths, run-contract details, a lifecycle legend, and an accessible transition table.
- [x] `CLIENT` - switch immutable generations and verify the selected origin identity, generation delta, reused/rerun status, node selection, keyboard traversal, and scroll context stay bound to the selected generation.
- [ ] `LIVE` - inspect the deployed repository template and a selected run generation in Chromium, confirm the origin-to-work path and visible controller loop, and inspect the tabular equivalent without invoking a mutation control.
  - Gap: the deployed database contains zero `run_workflows`; the isolated fixture verified run-generation behavior but does not satisfy deployed-live verification.
  - Operator decision: wait for the next naturally activated graph-backed run rather than mutate production or GitHub state solely for verification.
- [x] `REGRESSION` - run the focused workflow/interface suites, the complete Python suite, and source compilation.
