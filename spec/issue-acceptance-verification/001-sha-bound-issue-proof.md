# SHA-Bound Issue Acceptance Proof

Adds the missing issue-acceptance gate between repository validation/scope review and pull-request publication. This changes the publication contract established by `spec/repository-agent-mvp/007-pull-request-publication.md` and the feedback revision contract established by `spec/repository-agent-mvp/008-feedback-resolution.md`: repository-required commands and scope review remain necessary, but are no longer sufficient to publish a ready-for-review revision.

## Contract

- A newly formulated stored repository team includes a distinct verifier with read/run access and no write access.
- For each candidate commit, the stored verifier derives a finite set of observable acceptance claims from the GitHub issue, current discussion, repository instructions, and repository behavior. Claims are persisted before issue-behavior commands run.
- The verifier independently exercises every claim against the exact committed SHA. Passing generic tests count only when they directly exercise a claim.
- Controller-observed actions and results are durable evidence. Every claim result references one or more successful evidence observations. Command evidence includes its exit result and external log path.
- The verifier records whether screenshots are required. Visual/UI claims require controller-captured screenshots when capture is possible and materially useful; required screenshots are copied outside the checkout, hashed, and bound to the verification, claim, scenario, viewport/metadata, and commit SHA.
- Every changed file is mapped to an issue claim or a necessary regression test. A failed claim or unnecessary change returns the run to implementation with the exact observed evidence. An irreducibly unverifiable required claim blocks publication explicitly.
- Publication requires repository validation, issue-acceptance verification, and scope/secret review to pass for the same current SHA.
- Any source-changing implementation or feedback revision invalidates earlier issue-acceptance proof. The new SHA is independently reverified before push/publication, and the pull request is refreshed with only the current proof.
- Pull-request proof is a projection of durable records, not the source of truth. It includes claims, scenarios, observed outcomes, commands/results, screenshot/artifact references when safely publishable, verified SHA, changed-file scope mapping, and explicit limitations.
- The local interface exposes the current acceptance status, claim evidence, scope mapping, and locally stored artifacts.

## Acceptance Criteria

- [x] Durable schema and service records restart-safe, versioned verification attempts, issue-derived claims, controller-observed evidence, claim results, scope mapping, screenshot decisions, and artifact hashes for an exact run commit.
- [x] Every newly formulated team contains one distinct verifier that cannot write repository source.
- [x] A candidate revision cannot publish until every issue-derived claim and changed-file scope mapping passes for the same SHA as repository validation and publication review.
- [x] Failed behavior or scope evidence returns the run to implementation with actionable claim-specific evidence; irreducibly missing verification capability blocks without publishing a completion claim.
- [x] A source-changing feedback revision cannot reuse stale proof and refreshes the same pull request with proof for the new verified head.
- [x] Pull-request proof and the local interface render the current durable claims, observations, SHA, scope mapping, limitations, and safe artifact references.
- [x] Visual verification decisions are explicit; required screenshots are controller-validated, copied outside the checkout, hashed, and bound to their claim and SHA.

## Verification

- [x] `UNIT` - persist an in-progress verification, reopen the database, resume it, complete it, and prove exact-SHA attempt/version identity and evidence cardinality.
- [x] `UNIT` - reject pass reports with missing claims, failed/unreferenced observations, incomplete changed-file mappings, stale SHA, or missing required screenshot artifacts.
- [x] `UNIT` - reject a passing claim that cites inspection without a successful issue-behavior command.
- [x] `UNIT` - prove team formulation always stores one read/run-only verifier distinct from the lead.
- [x] `UNIT` - enforce a read-only checkout for verifier tools while preserving writable controller artifact storage.
- [x] `UNIT` - reject screenshot files carried over from a superseded verification attempt or SHA.
- [x] `INTEGRATION` - prove publication is withheld on failed issue behavior, receives actionable implementation feedback, and succeeds only after an independently observed passing revision.
- [x] `INTEGRATION` - prove a feedback source revision creates new proof for the new SHA, retains historical evidence, and refreshes the same pull-request body without duplicate publication.
- [x] `UNIT` - prove pull-request and interface projections show only current proof and safely expose copied screenshot metadata/content.
- [x] `REGRESSION` - run the complete deterministic project suite.
- [x] `SMOKE` - exercise the verifier plan/evidence/report path against a real committed fixture revision and inspect the generated pull-request proof projection.
