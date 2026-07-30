# Correct Invalid Workflow Resource Permissions

## Contract

This item extends `spec/agent-workflow-graphs/001-model-designed-workflow-graph.md`. Invalid model-designed teams and graphs still fail closed and never become current. The delta is that the team-design contract exposes the exact resource-to-tool rules and one semantically invalid model result receives one bounded correction opportunity before repository onboarding is blocked.

## Acceptance Criteria

- [x] The team-design request identifies the required stored-member tool for every agent resource claim and states the coordinator/verifier write restriction.
- [x] The team-design request states the exact graph ordering invariant: every non-coordinator/non-verifier node reaches the sole coordinator, the coordinator reaches the sole terminal verifier, and no second coordinator node is used for pre-work decomposition.
- [x] When the first complete model response fails team or workflow semantic validation, formulation requests one complete replacement using the same repository evidence and the concrete bounded rejection reason.
- [x] The replacement is independently validated; a valid replacement is returned without expanding member permissions or silently rewriting resource claims.
- [x] A second invalid response, or an inference execution failure, remains fail-closed with its concrete error and no fallback team or workflow.
- [x] Re-onboarding persists no new sandbox/team version until a valid design exists, preserving the previously current versions when correction fails.

## Verification

- [x] UNIT — reject a coordinator node claiming `validation:read` without `run`, then accept a corrected replacement and prove the correction request contains the exact compatibility contract and rejection reason.
- [x] UNIT — reject a design with two coordinator nodes, then prove the correction request carries the exact topology contract and accepts a replacement whose work converges on one coordinator before verification.
- [x] UNIT — reject two invalid responses after exactly one correction attempt and prove inference execution errors are not retried as semantic corrections.
- [x] REGRESSION — run the focused team, workflow, and onboarding suites.
- [x] REGRESSION — run the complete Python suite and source compilation.
- [x] LIVE — deploy the correction, re-onboard the blocked Repogents repository, and verify a new immutable current team with a stored workflow graph while historical run bindings and durable state remain intact.
