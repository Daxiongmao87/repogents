# Stored Team Activation

## Prior Contract

This item completes `spec/repository-agent-mvp/004-stored-agent-team.md` and `006-autonomous-execution.md`. It supersedes the implicit application-authored lead-only assignment currently used for every issue.

## Contract

- The stored lead receives the complete stored team roster and makes the issue-specific assignment decision from issue and repository evidence.
- The lead records selected stable member identities and a nonempty reason. Selection may be lead-only for a small issue, but the application does not preselect that outcome.
- Only members in the run's immutable stored team version may be selected, and the lead is always included.
- Selected non-lead members execute sequentially unless the lead explicitly identifies independent work. Their runtime/model, permitted tools, responsibilities, instructions, action history, and completion evidence come from the stored team version and durable run state.
- The lead receives member results, integrates their work, and retains responsibility for final scope and validation.
- Action-quantum yields and restart resume the current member and assignment without duplicate assignment or lost completion evidence.

## Acceptance Criteria

- [x] The lead can inspect evidence before recording a lead-only or multi-member assignment, and the persisted reason originates from the lead action.
- [x] Unknown or out-of-version member identities are rejected without creating assignments.
- [x] Every selected non-lead member executes with its own stored configuration and permitted tool boundary before final lead completion.
- [x] Member progress and summaries survive restart, and completed members are not executed twice.
- [x] A lead-only assignment remains a valid explicit lead decision rather than an application default.

## Verification

- [x] `UNIT` - scripted lead actions produce lead-only and multi-member durable assignments with exact reasons and reject unknown members.
- [x] `INTEGRATION` - an implementer and verifier execute sequentially with distinct stored configurations, then the lead receives their summaries and completes.
- [x] `INTEGRATION` - interrupt between member actions, reconstruct the service, and prove execution resumes without duplicate assignment or member completion.
- [x] `REGRESSION` - run stored-team validation, re-onboarding version retention, execution, and exact-SHA validation suites.
