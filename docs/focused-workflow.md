# Focused Agent Workflow

## Purpose

Repogents converts a complete repository issue into focused, independently
validated work without constraining the repository domain or prescribing how
agents perform that work.

The production workflow is:

`Issue Specifier -> Work Specifier -> Work Agents -> Work Validator -> Issue Validator -> Publication`

## Issue Specifier

The Issue Specifier interprets the complete current issue and durable pass
evidence. It returns explicit requirements for required outcomes, constraints,
and methods, with concrete evidence for every interpretation. It organizes all
requirements into strategic work areas and proposes only evidence-backed causal
dependencies. Strategic areas describe what must be achieved, not a fixed
procedure.

The controller validates unique identities, complete requirement coverage,
dependency evidence, and acyclicity, then persists the result before another
stage runs. Restart recovery reuses that exact result.

Agent transport must reject ambiguous multi-action responses before executing
them and reject submission without a valid result object inside the same agent
turn. A protocol violation must become corrective agent context, not a silent
discard followed by an identical poll-level retry.

## Work Specifier

The Work Specifier processes one strategic work area per turn. It returns
observable acceptance criteria, focused work items, evidence requirements,
agent-owned classifications, requirement mappings, and evidence-backed
within-area dependencies. Every applicable requirement and criterion must map
to work. The controller validates and persists each area independently, so a
restart does not discard completed specification work or rerun it.
Persisted normalized specifications are reconstructed through the same semantic
validator and must reproduce the exact durable normalized result before reuse.
Rejected Work Specification payloads and their validator errors are persisted as
attempt evidence. The run remains in specification, and the next unbounded
attempt receives those rejections as corrective context; execution stays closed
until every strategic area has a valid persisted specification.

When an issue mandates a method or interaction with an external system, source,
tool, or environment, the specification must require direct execution evidence
that distinguishes performing the method from merely producing a plausible
artifact. This is an evidence rule, not a taxonomy of methods or tools.

## Work Agents

Each Work Agent chooses its own repository-appropriate procedure. A result may
propose completion or a continuation handoff. Repogents durably retains the
complete JSON result, artifacts, tests, repository state, limitations, context,
and the associated command trajectory. It does not reduce a result to a small
controller-selected subset.

## Work Validator

A proposed completion is validated in the same disposable source snapshot
before its source delta is imported. The Work Validator sees only the focused
work contract, filtered definitions for its applicable requirements and
criteria, direct dependency results, the complete proposal, changed paths,
artifacts, and execution evidence. It does not receive the complete issue
specification. Its result template names every exact key it must disposition,
and it must return those keys exactly once with concrete evidence. It cannot
disposition unrelated requirements, judge unrelated work, or implement
corrections. A mandated method or external interaction is unsupported unless
the execution trajectory directly corroborates that it occurred; artifact
content, citations, and the worker's own claim are not substitutes.

An accepted result may be imported and completed. A rejected result is
persisted with its validation findings, its source delta is discarded, and the
work fails. Existing dependency rules propagate that failure only to causal
descendants, preserve independent work, and create a `work_failure` pass for
adaptive respecification without an iteration cap.

## Issue Validator

After every required work lineage is individually accepted, the Issue
Validator reviews the complete publication candidate as an integrated result.
It dispositions every current issue requirement and focused criterion with
evidence and checks interoperability, cross-work assumptions, required methods,
regressions, out-of-scope changes, and alignment with the original issue.

Publication remains closed for any failed disposition, integration finding,
code-review finding, pending dependency, or failed work. A failed holistic
result creates a new evidence-bearing specification pass. A passed result may
advance to publication, subject to the separate opt-in merge contract.

## Controller Boundary

The controller owns schemas, durable identities, traceability, graph integrity,
state transitions, source import, causal failure propagation, and publication
gates. Agents own semantic interpretation, classification, dependency meaning,
implementation procedure, and validation judgment. The controller must fail
closed when required evidence cannot be validated; it must not replace missing
semantic decisions with a taxonomy, filename heuristic, iteration bound, or
source-code-only assumption.
