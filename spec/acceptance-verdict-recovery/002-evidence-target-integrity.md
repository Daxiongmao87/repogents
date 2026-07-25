# Acceptance Evidence Target Integrity

Fixes a defect discovered during the live `Daxiongmao87/websesh` issue #1 acceptance run. This extends the verifier recovery contract in `spec/acceptance-verdict-recovery/001-semantic-verdict-retry.md`: a verifier must not turn output from an unvalidated or incorrectly mapped internal target into issue-failure evidence.

## Contract

- A verifier validates that every internal process, session, pane, socket, or similar probe target exists and maps to the application object under test before interpreting its output. A missing, defaulted, or ambiguous target is invalid evidence.
- User-visible claims are decided primarily from the observable client behavior required by the issue. Internal implementation-state probes may corroborate that behavior, but may not override direct client evidence unless their identity mapping and causal relevance are established.
- When direct behavior and an internal probe conflict, the verifier investigates and reconciles the contradiction before returning `pass` or `fail`; it does not select the easier observation or infer that public and internal identifiers are interchangeable.
- These evidence-integrity rules are included in every verifier action prompt, including resumed attempts.

## Acceptance Criteria

- [x] Every acceptance verifier prompt requires existence and identity validation for internal probe targets.
- [x] Every acceptance verifier prompt requires user-visible evidence to remain authoritative for user-visible claims and requires contradictory evidence to be reconciled.
- [x] Verifier guidance forbids production changes whose only purpose is making an internal acceptance probe easier or aligning distinct public and internal identifiers.
- [x] The live `websesh` issue #1 acceptance retry does not fail from treating the public session UUID as the distinct tmux supervisor target.

## Verification

- [x] `UNIT` - inspect a verifier action prompt and prove it contains target-existence, identity-mapping, user-visible-authority, and contradiction-reconciliation requirements.
- [x] `REGRESSION` - run the focused acceptance suite.
- [x] `UNIT` - prove the verifier prompt requires probes to adapt to existing application identity boundaries instead of requiring source changes for observability.
- [x] `LIVE` - redeploy the controller and prove the active exact-commit browser-scroll verification resolves its public/internal identifier discrepancy before returning a verdict.
