# Adaptive Dependency Resolution

## Purpose

Repogents resolves dependencies as an evidence-backed, agent-discovered graph.
Agents determine the domain meaning of repository work. The controller enforces
graph integrity, durable state, causal readiness, and recovery without imposing
a domain taxonomy, a fixed workflow, or an iteration bound.

This contract applies to every repository-managed domain, including software,
documentation, data, media, research, and operations.

## Principles

1. Dependencies are proposed by agents from current repository evidence, issue
   intent, accepted specifications, prior work, artifacts, validation, and
   runtime discoveries.
2. Every dependency edge carries a reason and supporting evidence. The
   controller rejects missing, duplicate, dangling, or contradictory edge
   evidence.
3. The controller interprets only graph structure and durable execution state.
   It does not infer dependencies from filenames, classifications, or a fixed
   list of domain relationship types.
4. A terminal state is not automatically a satisfied dependency. Completion
   satisfies an edge. A handoff satisfies downstream work only after its
   continuation lineage completes; the direct continuation may consume the
   handing-off work immediately.
5. Failure propagates only through causal dependency edges. Independent work
   remains runnable and its successful output is preserved.
6. Failed or newly discovered dependencies create evidence for another agentic
   specification pass. They do not terminate autonomy and do not consume an
   iteration budget.
7. Validation and publication remain closed while any required dependency is
   unresolved or failed.

## Agent Output Contract

Each specification and work item contains:

- `dependencies`: agent-chosen keys in the current pass.
- `dependency_evidence`: exactly one entry for every dependency, containing:
  - `dependency`: the referenced key.
  - `reason`: why the consumer requires that producer's outcome.
  - `evidence`: one or more concrete observations supporting the edge.

An item with no dependencies returns an empty `dependency_evidence` list.

A `continue_work` handoff uses the same contract. Its dependencies may refer to
work in the current pass. The generated continuation retains `parent_work_id`
as durable lineage independent of agent-selected dependency keys.

## Integrity Rules

For every accepted package or handoff, the controller atomically verifies:

- Keys and dependency references are nonempty and unique.
- Every dependency references the same execution pass.
- Dependency evidence has an exact one-to-one correspondence with dependency
  keys.
- Reasons and evidence are nonempty.
- Specification and work dependency graphs are acyclic when accepted.
- Runtime additions cannot create a dependency cycle.

Invalid graph output fails closed and becomes worker-failure evidence; it is
never guessed, silently discarded, or treated as completed work.

## Readiness

The controller evaluates readiness whenever graph or execution state changes.

For a dependency producer:

- `COMPLETED`: satisfied.
- `FAILED`: failed.
- `UNASSIGNED`, `QUEUED`, or `RUNNING`: pending.
- `HANDED_OFF`: pending until its continuation lineage is satisfied, except
  that the direct continuation may consume its parent's available handoff
  output.

A specification dependency is satisfied only when all executable work and
continuation lineages belonging to that specification are satisfied. A failed
lineage makes the specification dependency failed.

Work is runnable only when every work and specification dependency is
satisfied. Node classification and semantic similarity decide who may perform
the work, not whether its dependencies are satisfied.

## Failure And Adaptation

When work fails, the controller repeatedly marks only pending descendants whose
work or specification dependencies are now failed. Running and independent
work continues. Once no runnable or running work remains, the controller
creates a `work_failure` pass containing the failed items, their dependency
evidence, results, handoffs, and originating feedback relationship when one
exists.

The next Issue Specifier and focused Work Specifier turns may preserve
successful output, replace work, add or remove dependencies, consolidate a
cycle, or produce another domain-appropriate plan. The controller does not
prescribe which adaptation is correct.

## Recovery And Idempotency

Dependency evidence and handoff lineage are stored with graph state. Restarting
the controller requeues interrupted work, recomputes readiness from durable
state, and reuses an already-created adaptive pass. Recovery must not duplicate
work, passes, follow-up issues, or feedback acknowledgements.

## Verification Requirements

Tests must prove:

- Evidence is persisted and exact for specification, work, and handoff edges.
- Invalid, dangling, duplicate, cyclic, or unevidenced edges fail atomically.
- Direct continuations can consume parent handoff output.
- Other dependents wait for the full continuation lineage.
- Continuation failure propagates to causal descendants.
- Independent work continues after a sibling failure.
- Validation and publication never run after unresolved or failed work.
- Failure creates an adaptive pass with sufficient evidence and no iteration
  cap.
- Restart boundaries preserve graph decisions without duplicate effects.
