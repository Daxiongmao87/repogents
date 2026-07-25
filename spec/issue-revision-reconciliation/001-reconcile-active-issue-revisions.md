# Reconcile Active Issue Revisions

Changes the activation snapshot contract in `spec/repository-agent-mvp/005-run-lifecycle.md`, the same-pull-request revision contract in `spec/repository-agent-mvp/008-feedback-resolution.md`, the asynchronous observation boundary in `spec/asynchronous-feedback-detection/001-poll-feedback-outside-agent-lanes.md`, and the exact-candidate proof contract in `spec/issue-acceptance-verification/001-sha-bound-issue-proof.md`. The GitHub issue remains authoritative while its run is nonterminal; editing it does not create another run, branch, checkout, or pull request.

## Contract

- Every nonterminal run in an enabled, ready repository has its GitHub issue title, body, and discussion polled on the scheduler interval outside repository agent lanes. A failure observing one issue is bounded to that run and does not stop other issue, feedback, or repository work.
- The initial activation snapshot and every later content revision are immutable durable issue versions. Each version records exact content, GitHub update identity, content digest, predecessor, observation time, and monotonically increasing per-issue version. Repeated observations are idempotent.
- A changed issue version atomically becomes the run's current requirement version, cancels any active quiet generation, invalidates acceptance proof for older versions without deleting history, and fences stale execution, acceptance, and publication work. Controller processes are interrupted at a safe boundary so the stored lead resumes against current content.
- An active changed run returns to `implementing` (or remains `queued` before work begins), including from `validating`, `publishing`, `waiting_for_feedback`, `resolving_feedback`, `quiet_period`, `notified`, or `blocked`. The lead receives the exact old/current snapshots and their diff. A changed requirement, including one that removes a prior blocker, needs no relabel, user retry, or force action.
- Repository validation, issue acceptance, pull-request proof, and every outbound publication mutation are bound to both the exact candidate commit SHA and exact current issue-version ID. A same-SHA proof for an older issue version is stale and cannot be reused. A version change racing publication either fences the mutation before it starts or requires the resulting same pull request to be revised before quiet time.
- Successful revision work updates the existing deterministic branch and existing open pull request. Publication idempotency includes the issue version where proof content can differ for the same SHA. No issue edit may create a duplicate activation, run, branch, checkout, or pull request.
- `canceled` and `closed` runs are never resurrected. External pull-request closure or merge remains terminal and wins over later issue content.

## Acceptance Criteria

- [x] Activation stores one immutable initial issue version and binds the new run to it; schema migration backfills existing issue, run, acceptance, and pull-request records without losing history.
- [x] Polling stores each changed title/body/discussion snapshot once and ignores repeated or content-identical observations.
- [x] Observation remains independent of repository agent lanes and isolates one issue polling failure from all other work.
- [x] A changed issue atomically cancels quiet time, supersedes stale acceptance, and returns every supported nonterminal state—including `blocked`—to autonomous implementation without a new activation.
- [x] In-flight execution and acceptance cannot complete against an older issue version after a newer version is durable.
- [x] Scope review, acceptance reuse, pull-request proof, and every push/create/update boundary reject an issue-version mismatch even when the commit SHA is unchanged.
- [x] The lead receives the current issue plus an exact durable delta from the activation or last validated issue version, and the revised candidate updates the same open pull request.
- [x] Terminal runs remain terminal and rapid edits retain an ordered audit history while execution converges on the latest version.
- [x] A one-time upgrade cannot treat pre-versioning acceptance as proof of mutable legacy issue content; unverifiable legacy proof is superseded and its nonterminal run resumes against the backfilled current snapshot.

## Verification

- [x] `MIGRATION` — upgrade a populated schema-v10 database and prove initial issue-version backfill plus preserved run, acceptance, and pull-request identity.
- [x] `UNIT` — activate and repeatedly poll unchanged/changed issue content; prove ordered immutable versions, exact predecessor data, and idempotency.
- [x] `INTEGRATION` — edit an issue for a blocked run with an open pull request; prove stale acceptance is superseded, quiet state is canceled, the same run resumes implementation, and no activation/run/pull-request is duplicated.
- [x] `RACE` — change the issue while execution or acceptance is in flight and prove the old version cannot store a current validation or acceptance result.
- [x] `RACE` — change the issue immediately before a publication gateway mutation and prove the stale candidate cannot push, create, update, or enter quiet time.
- [x] `SCHEDULER` — block one repository agent lane and prove issue observation still wakes another active run while a polling failure remains bounded.
- [x] `REGRESSION` — run the complete deterministic project suite.
- [x] `MIGRATION` — upgrade a populated schema-v11 database containing a blocked legacy acceptance and open pull request; prove the proof is superseded, the same run resumes, and pull/run identity is preserved.
- [ ] `LIVE` — deploy the daemon, observe the corrected Martite issue #7, revise existing PR #9 without another run or pull request, and confirm current issue-version-bound proof and state.
