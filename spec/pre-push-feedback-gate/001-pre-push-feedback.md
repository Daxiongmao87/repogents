# Pre-Push Feedback Gate

Changes `spec/repository-agent-mvp/006-autonomous-execution.md`, `007-pull-request-publication.md`, and `008-feedback-resolution.md`: Repogents must resolve internal and already-known review feedback before pushing.

## Acceptance Criteria

- [x] The stored independent verifier reviews every candidate before any GitHub push; a rejected candidate is revised and reviewed again without an external mutation.
- [x] Every persisted feedback revision known before publication is incorporated into the final candidate, and multiple known revisions produce no intermediate pushes.
- [x] Internal review stays inside Repogents, and revision findings are resolved by the code update rather than an owner-attributed GitHub reply.
- [x] Restart after validation or an interrupted publication resumes the same reviewed candidate without repeating execution or creating duplicate effects.

## Verification

- [x] Reject a candidate in internal review, verify no publication effect, then revise and approve it before the first push.
- [x] Resolve multiple persisted revision findings and verify one combined execution, one final push, the same final SHA on every finding, and no reply comments.
- [x] Add feedback before the publication boundary and verify the earlier candidate is not pushed.
- [x] Interrupt after validation and during publication, then verify resume performs no duplicate execution, push, pull request, or response.
- [x] Run the affected execution, team, publication, feedback, acceptance, scheduler, and complete project test suites.
