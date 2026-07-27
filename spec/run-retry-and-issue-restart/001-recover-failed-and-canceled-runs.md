# Recover Failed and Canceled Issue Runs

## Prior Contracts

This item supersedes the no-manual-retry contract in `spec/remove-manual-retry/001-remove-manual-retry.md` and extends the terminal-run contract in `spec/repository-agent-mvp/005-run-lifecycle.md`. Cancellation remains a durable effect boundary for the canceled run; restarting creates a new run rather than resurrecting canceled execution.

## Contract

- Validation output that identifies deterministic Prettier file findings is normalized as baseline debt, allowing issue work to proceed under the existing validation-delta policy instead of blocking before agent execution.
- A transient controller exception keeps the run nonterminal, durably records the consecutive attempt count, bounded exponential-backoff deadline, and last error, and is retried after that deadline across process restarts. A pending automatic retry exposes **Retry now** to clear only the delay.
- A blocked run remains visible and exposes **Retry now**. Retrying atomically returns the same run to the durable state from which it entered `blocked`, preserves its checkout, immutable sandbox/team versions, evidence, and external identities, and wakes scheduling.
- A canceled run remains visible in issue history and exposes **Restart issue**. Restarting creates one idempotent fresh run from the current open GitHub issue, current repository sandbox/team versions, current default branch, and freshly fetched base SHA. The canceled run and all its evidence remain terminal and unchanged.
- Retry is rejected unless a run is blocked or has a pending automatic retry. Restart is rejected for non-canceled runs, closed GitHub issues, paused/removed/not-ready repositories, and issues that already have a nonterminal run.
- Known repository secrets are redacted from retry errors before persistence and again before dashboard projection; redaction lookup failure stores or displays a generic error rather than raw exception text.
- The local HTTP API and dashboard expose these recovery operations without weakening cancellation process boundaries or creating duplicate runs under repeated requests.
- A blocked run's Retry now control remains visible but disabled when repository state makes retry ineligible, and the dashboard states the repository prerequisite instead of hiding the recovery path.

## Acceptance Criteria

- [x] A failing Prettier baseline with file-level warnings is stored as delta baseline debt and execution continues on the same run.
- [x] A transient controller exception durably schedules the same nonterminal run with bounded backoff, survives controller reconstruction, and clears its current retry state after a successful boundary.
- [x] Retry error persistence and dashboard projection cannot expose known repository secret values.
- [x] A blocked run can be retried from its recorded pre-block state without changing run identity or deleting durable evidence.
- [x] A canceled run can create exactly one fresh run using current issue, repository-version, and base-branch state while the canceled run remains unchanged.
- [x] Repeated restart requests return the same replacement run and cannot create duplicate activation or run records.
- [x] Blocked and terminal runs are visible in the dashboard with state-appropriate Retry now or Restart issue controls.
- [x] A blocked run in a paused repository still displays a disabled Retry now control with a resume prerequisite.
- [x] Invalid retry and restart requests fail without mutating durable state or scheduling work.

## Verification

- [x] `UNIT` — normalize representative Prettier output and prove a nonzero baseline is stored in delta mode before agent execution continues.
- [x] `UNIT` — persist a transient failure and backoff deadline, reconstruct orchestration, prove no early execution, then advance after the deadline and clear current retry state.
- [x] `SECURITY` — inject a known secret into a retryable exception and prove both durable retry state and projected dashboard state redact it, including a legacy stored value.
- [x] `UNIT` — retry a blocked run from each supported pre-block state and reject retry for every other state.
- [x] `INTEGRATION` — restart a canceled run twice and prove one fresh run, current versions/base, preserved canceled evidence, and no duplicate checkout identity.
- [x] `HTTP` — exercise retry/restart routes, invalid-state errors, and scheduler wakeups.
- [x] `CLIENT` — render blocked and canceled history, expose the correct recovery buttons, and dispatch the corresponding mutations.
- [x] `CLIENT` — render a paused repository's blocked history and prove Retry now remains visible, disabled, and accompanied by the resume prerequisite.
- [x] `REGRESSION` — run the complete deterministic project suite.
- [x] `LIVE` — deploy the daemon and inspect the blocked Simulacrum issue plus canceled Repogents issue in Chromium, proving recovery controls are visible without invoking them.
