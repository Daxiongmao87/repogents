# Model-Designed Repository Workflow Graph

Supersedes the fixed member-order portion of `spec/agent-designed-atomic-teams/001-model-designed-atomic-team.md` and `spec/stored-team-activation/001-stored-team-activation.md`. Existing immutable team and run history remains valid; newly onboarded and explicitly re-onboarded repositories use this graph contract.

## Contract

- Repository team design returns one model-designed workflow template together with the atomic member roster. The template expresses agent and registered deterministic nodes plus directed dependencies; the controller does not infer execution order from member roles.
- The team-builder instruction supplies the graph's execution semantics, registered deterministic-operation catalog, resource model, safety boundaries, and orchestration goals. It explicitly requires the builder to minimize redundant nodes, use deterministic operations for fixed transformations, expose safe parallel branches, serialize conflicting writes, add joins where downstream work needs multiple outputs, and give every agent node a bounded objective, expected output, and handoff prompt.
- The builder records a concise design rationale explaining how the topology and node prompts fit the repository evidence. Generic serial role lists, unconnected specialists, redundant review layers, or graph logic unsupported by repository evidence are invalid designs.
- The controller rejects a template unless node and edge identities are safe and unique, the graph is acyclic, every edge references stored nodes, every agent node references exactly one member from the same immutable team version, and every selected work node can reach the coordinating member and then the independent verifier.
- The coordinating member and independent verifier each have exactly one agent node. The verifier is terminal. A coordinating member cannot be used as the independent verifier or as a checkout-writing specialist.
- Nodes declare controller-known resource claims. Read-only nodes may be independent; checkout-writing agent nodes share the exclusive checkout resource unless the controller supplies isolated writable workspaces.
- The immutable workflow template is stored with its team version. A run receives an immutable compiled graph bound to its run, issue version, team version, sandbox version, and exact base SHA before assigned work starts.
- Issue assignment selects the members needed for that issue. Compilation retains their agent nodes and required deterministic, coordinator, and verifier nodes, removes unselected optional agent branches, and preserves dependency reachability without changing the stored template.
- Every compiled agent node carries a bounded node-specific prompt layered after the immutable member instructions. The prompt states that node's objective, inputs, expected output, constraints, and downstream handoff; it cannot broaden the member's tools or responsibilities.
- Existing team versions created under earlier design contracts remain loadable. Their active and historical runs receive a deterministic compatibility graph matching their recorded specialist, coordinator, and verifier order; their immutable team records are not rewritten.
- Graph definitions and compiled run graphs contain no secret values, arbitrary executable source, shell expressions, or external side-effect authority.

## Acceptance Criteria

- [x] New repository onboarding persists a model-designed, immutable workflow template with atomic members, agent nodes, registered deterministic nodes, resource claims, and directed dependencies.
- [x] Invalid identities, member references, resource claims, duplicate edges, cycles, unreachable selected work, nonterminal verification, and malformed node parameters are rejected before a team version becomes current.
- [x] An issue assignment compiles and persists an immutable issue graph bound to the exact run inputs before specialist execution, retaining only selected work branches plus required joins, coordination, and verification.
- [x] The team-builder receives explicit orchestration goals, node semantics, operation/resource catalogs, and safety constraints, and persists repository-specific node prompts plus a design rationale rather than a generic role sequence.
- [x] Every compiled agent node has a bounded objective, expected output, constraints, and handoff prompt that remains within its stored member's responsibilities and permissions.
- [x] Existing contract-version-one and contract-version-two teams and their active runs remain usable through deterministic compatibility graphs without mutating historical team records.
- [x] No graph record or dashboard projection contains secret values or arbitrary executable source.

## Verification

- [x] `UNIT` - accept a branched model-designed template and reject duplicate identities, unknown members, unknown operations/resources, cycles, unreachable work, and a nonterminal verifier.
- [x] `INTEGRATION` - onboard a repository with a scripted model graph, persist and reload the immutable template, assign a subset of specialists, and prove the compiled run graph is bound to the run versions and exact base SHA.
- [x] `UNIT` - reject an assignment whose selected agent branch depends on an unassigned agent rather than silently dropping the selected work.
- [x] `UNIT` - inspect the builder request and prove it explains fan-out, joins, conflict serialization, deterministic-operation selection, prompt/handoff quality, minimal topology, registered catalogs, and controller-owned safety boundaries.
- [x] `UNIT` - reject empty, generic, permission-expanding, or oversized node prompts and a graph rationale unsupported by the stored repository evidence.
- [x] `REGRESSION` - load legacy team versions and prove their existing runs compile to the prior deterministic execution order without rewriting team rows.
- [x] `SECURITY` - reject executable source, shell expressions, undeclared operations, and graph payloads containing secret values.
