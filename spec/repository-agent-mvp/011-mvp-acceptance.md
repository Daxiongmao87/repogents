# MVP Acceptance

Implements `MVP.md` §11 and preserves §12 non-requirements. It composes the current integrated source; component success cannot substitute for the required client, host, and live boundaries.

## Fixtures

- Initial public inventory repositories: `https://github.com/Daxiongmao87/bazzeye` and `https://github.com/Daxiongmao87/foundry-portal`.
- Public issue evidence: `https://github.com/Daxiongmao87/bazzeye/issues/3` and `https://github.com/Daxiongmao87/foundry-portal/issues/3`.
- The application must infer repository inputs, commands, team, issue behavior, and scoped changes from runtime evidence. A review comment is acceptance activity posted during the run and is resolved according to its actual content; it is not product configuration.
- The required complete end-to-end fixture is `https://github.com/Daxiongmao87/websesh/issues/1`; the public `bazzeye` and `foundry-portal` runs are inventory or preliminary evidence and cannot substitute for that final test.

## Acceptance Criteria

- [x] Both public repositories are onboarded with distinct persisted sandbox/team versions and remain available without implicit re-onboarding after restart.
- [x] Actual-host sandbox acceptance proves filesystem/process/network isolation, restricted allowed egress, credential absence, scoped/redacted canary secret handling, committed-secret blocking, and descendant cancellation.
- [x] The real activating `agent:ready` event on `Daxiongmao87/websesh#1` yields exactly one run across repeated polling and restart, referencing its stored versions and isolated checkout.
- [x] The stored team infers and implements the real issue, passes discovered repository validation for an exact commit, and publishes exactly one unmerged PR containing only intended changes at that SHA.
- [x] Real arriving PR feedback is stored once, inferred and resolved by the stored team, with any changed SHA revalidated and pushed to the same PR and any response posted once.
- [x] Restart during a 30-minute quiet generation preserves the deadline; one linked persistent notification is created after a successful qualifying poll.
- [x] The application never merges or closes the fixture pull request.
- [x] No explicit `MVP.md` §12 non-requirement is introduced as required architecture or UI.

## Verification

- [x] `UNIT` — run all deterministic unit tests derived by items 001–010.
- [x] `ADAPTER` — run all concrete GitHub, model-runtime, Git, Bubblewrap-policy, and HTTP adapter tests.
- [x] `INTEGRATION` — run the composed persistence/onboarding/sandbox/lifecycle/publication/feedback/notification suite against real local dependencies.
- [x] `CLIENT` — drive repository, run, cancel, link, notification, and acknowledgment flows in a real browser, with no manual Retry control.
- [x] `HOST` — execute the complete §11.3 acceptance procedure on this Linux host.
- [x] `LIVE` — execute the complete §11.4 path against `https://github.com/Daxiongmao87/websesh/issues/1` and its resulting real pull request, including restart, feedback, and 30 verified minutes.
