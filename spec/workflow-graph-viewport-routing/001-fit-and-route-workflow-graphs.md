# Fit and Route Workflow Graphs

Changes the presentation contract established by `spec/agent-workflow-graphs/003-two-dimensional-workflow-preview.md` and the lifecycle rendering established by `spec/system-lifecycle-graph/001-render-cyclic-controller-lifecycle.md`. The stored executable graph, controller lifecycle projection, immutable generation identity, and accessible tables remain authoritative and unchanged; this item replaces the fixed scroll-only pixel grid and direct edge drawing.

## Contract

- The visual graph uses a deterministic left-to-right layered layout. Executable dependency depth determines the primary rank, concurrently ready nodes in one rank stack vertically, joins follow their contributing branches, and controller-owned boundaries continue the primary flow. Stable projected order breaks layout ties, so refreshes do not reshuffle an unchanged graph.
- Compact node cards expose identity and state in the overview while the existing selected-node panel retains complete safe details. Parallel work remains visibly distinct without repeating full role, operation, resource, or attempt detail inside every card.
- Dependency and controller-lifecycle edges attach to explicit node-boundary ports and follow orthogonal paths through reserved negative-space gutters. A route never intersects an unrelated node rectangle. Routing deterministically minimizes crossings, shared segments, bends, and length; iterative lifecycle transitions use labeled outer rails rather than traversing the executable graph.
- On first render and after repository, run, or generation context changes, the complete graph bounds fit inside the visible container with padding. Accessible zoom-in, zoom-out, fit, and reset controls, pointer-background panning, and keyboard equivalents support inspection without changing graph state. A manual viewport is retained across ordinary refreshes of the same graph context; fitting resumes when explicitly requested.
- Dependency and lifecycle styles remain distinguishable without color, edge labels occupy clear negative space, node focus remains keyboard operable, and the dependency and lifecycle tables remain the complete accessible equivalents. Viewport interaction is presentation-only and exposes no graph mutation path.

## Acceptance Criteria

- [x] The graph presents one stable left-to-right execution flow, with asynchronous siblings stacked vertically and downstream joins and controller boundaries aligned after their prerequisites.
- [x] The initial viewport contains every node and routed edge with visible padding; Fit restores that framing after zoom or pan and container resizing keeps a fitted view fitted.
- [x] Explicit, labeled zoom-in, zoom-out, fit, and reset controls plus keyboard and pointer-background navigation operate within bounded viewport transforms without changing graph data.
- [x] Every dependency and lifecycle route connects through node ports, avoids every unrelated node rectangle, and places iterative controller paths and labels in reserved outer negative space.
- [x] The overview is materially less cluttered while preserving node identity, status, edge semantics, selected-node details, generation selection, refresh stability, and accessible table equivalence.
- [x] Existing executable DAG semantics, projection-only lifecycle semantics, immutable generation behavior, keyboard node traversal, privacy boundaries, and read-only behavior remain unchanged.

## Verification

- [x] `CLIENT` - require the rendered dashboard contract to expose deterministic layered layout, obstacle-aware orthogonal routing, bounded viewport state, and labeled zoom-in, zoom-out, fit, and reset controls without graph mutation controls.
- [x] `BROWSER` - render the six-branch fixture, prove same-rank branches stack perpendicular to left-to-right flow, assert no routed segment intersects an unrelated node rectangle, and verify initial fit, zoom, pan, reset, and refit behavior.
- [x] `BROWSER` - refresh and switch immutable generations, proving the same-context manual viewport persists while a changed graph context starts fitted and node selection and keyboard traversal remain bound correctly.
- [x] `LIVE` - inspect the deployed repository template at desktop and narrow container sizes, confirm the entire default graph is visible, exercise every viewport control, and visually verify readable negative-space routing without invoking a mutation control.
- [x] `REGRESSION` - run focused interface and workflow suites, source compilation, and the complete Python suite.
