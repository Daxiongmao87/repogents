# Stored Repository Agent Team

Implements `MVP.md` §4. Team composition is inferred from repository evidence during onboarding, stored immutably by version, and reused across issues.

## Contract

- Every team version contains exactly one lead with final responsibility and zero or more repository-appropriate scout, implementer, and verifier members.
- Each member records stable identity, role, responsibilities, permitted controller tools, configured runtime/model, and repository instructions.
- Team formation consumes repository evidence rather than target names or issue-specific assumptions.
- The lead assigns only stored members, records the decision, runs agents sequentially unless work is genuinely independent, and owns integration.
- Each member's runtime, model, action timeout, tool permissions, and instructions are immutable fields of its team version.

## Acceptance Criteria

- [x] Onboarding creates and stores a repository-specific team version with exactly one lead.
- [x] Different repository evidence can produce different stored team compositions without product code changes.
- [x] Later runs load the referenced team version without reformulation.
- [x] A lead records issue-specific assignment reasoning and cannot assign a member outside the stored team version.
- [x] When the lead selects stored non-lead members, their stored runtime/model/tool permissions are actually executed and their results return to the lead; the application does not hard-code every issue to lead-only execution.
- [x] Re-onboarding creates a new team version while active runs retain their original version.
- [x] Existing runs retain the stored action timeout from their referenced team version after re-onboarding, controller-default changes, and restart.
- [x] Every newly created team version receives the current 600-second action timeout even when a custom evidence formulator omits the optional field; 300 seconds remains migration-only historical behavior.

## Verification

- [x] `UNIT` — team validation rejects missing/multiple leads, duplicate member identities, and unknown assignment members.
- [x] `INTEGRATION` — onboard both public fixture repositories, record evidence-derived team versions, and reload them after restart.
- [x] `INTEGRATION` — re-onboard one repository and prove an existing run still loads its prior team version.
- [x] `INTEGRATION` — execute an issue-specific assignment containing a stored implementer or verifier and prove that member runs under its stored configuration before lead integration.
- [x] `INTEGRATION` — re-onboard with a different action timeout, restart, and prove an existing run still instantiates its prior stored timeout while a new run receives the new version's timeout.
- [x] `UNIT` — onboard through a formulator payload without a timeout and prove the new stored member receives 600 seconds.
