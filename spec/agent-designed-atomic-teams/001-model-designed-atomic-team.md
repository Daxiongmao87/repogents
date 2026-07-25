# Model-Designed Atomic Repository Team

Supersedes the team-composition mechanism established by `spec/repository-agent-mvp/004-stored-agent-team.md`. The prior immutable team versions remain historical; newly onboarded and explicitly re-onboarded repositories use this contract.

## Contract

- Repository onboarding gives the configured inference agent bounded repository evidence and instructs it to design the complete persistent development team for that repository.
- The instruction requires repository-specific, atomic member responsibilities: each member owns one bounded concern; coordination, implementation, and independent verification are not collapsed into one member; the lead coordinates and integrates rather than serving as the default implementer and verifier.
- The agent, not controller thresholds, repository-name conditionals, or a fixed role vocabulary, chooses the number of members, their atomic role names, stable identities, responsibilities, permitted controller tools, and which members coordinate or independently verify.
- The controller supplies only the safe response schema and orchestration invariants. It derives internal execution scheduling and model selection from the returned coordination, independent-verification, and tool-capability markers, then attaches the configured runtime, model, repository instructions, and action timeout before storing the immutable team version. Internal execution classes are not repository team roles.
- A malformed or unsafe model design blocks onboarding with a specific error. There is no hard-coded fallback team.
- Re-onboarding asks the agent to design a new team version from current evidence. Existing runs continue to load their referenced prior version.

## Acceptance Criteria

- [x] Onboarding invokes the configured inference agent to design team composition from repository evidence instead of selecting a hard-coded lead/optional-implementer/verifier template by file count or language count.
- [x] The formulation request explicitly requires atomic repository-specific responsibilities and forbids making the lead own implementation and verification.
- [x] A valid returned design is stored exactly as designed for stable identity, atomic role name, responsibility, permitted tools, coordination, and independent-verification markers, with controller-owned runtime/model/instructions/timeout fields attached.
- [x] Schema, identity, exactly-one-coordinator, exactly-one-independent-verifier, tool-permission, and stored-member invariants reject malformed or unsafe designs without silently substituting a default team.
- [x] Re-onboarding creates a new agent-designed team version while active runs retain their original stored version.
- [x] Re-onboarding `Daxiongmao87/martite` produces a real development team whose non-lead atomic members, rather than its lead, own implementation and verification work.

## Verification

- [x] `UNIT` — feed a small single-language repository to a scripted inference agent that returns several repository-specific atomic members and prove the formulator preserves that design rather than applying a size threshold.
- [x] `UNIT` — capture the bounded formulation request and prove it supplies repository evidence, asks the agent to design atomic responsibilities, and prohibits a lead that implements and verifies everything.
- [x] `UNIT` — prove missing/multiple coordinators, missing/multiple independent verifiers, duplicate identities, unsupported tools, malformed fields, and model/runtime failures are rejected without a fallback team.
- [x] `INTEGRATION` — onboard and re-onboard with different scripted agent designs, persist both immutable versions, and prove an existing run still loads its prior version.
- [x] `REGRESSION` — run the focused team, onboarding, application, execution, publication, feedback, and acceptance suites.
- [x] `LIVE` — re-onboard `Daxiongmao87/martite` through the configured model, inspect the stored version in the deployed interface/state, and prove implementation and verification are assigned to non-lead atomic members.
