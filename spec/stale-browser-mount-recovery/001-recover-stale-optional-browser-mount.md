# Recover a Stale Optional Browser Mount

## Prior Contracts

This item changes the browser lifetime established by `spec/acceptance-browser-runtime/001-sandbox-browser-runtime.md`, the command-isolation contract in `spec/repository-agent-mvp/003-stored-sandbox.md`, and the transient controller retry behavior in `spec/run-retry-and-issue-restart/001-recover-failed-and-canceled-runs.md`. A validated browser remains optional, but its cached host path is no longer assumed to remain valid for the daemon lifetime.

## Contract

- Before constructing each sandbox command, the controller checks whether its cached browser executable and standalone bundle still exist and remain executable. A stale automatically discovered browser is rediscovered once under synchronization; a stale explicitly supplied browser is disabled. The command uses one consistent refreshed browser snapshot or advertises no browser.
- A browser bundle that disappears after daemon startup cannot leave a nonexistent Bubblewrap mount on unrelated repository commands. Non-browser commands continue without requiring a daemon restart.
- Exact-base validation probes distinguish a failed sandboxed Git command from a successful probe that proves the checkout SHA or tracked contents differ. Probe execution failures retain the underlying bounded error and propagate through the existing automatic controller-retry path instead of becoming an irreducible `blocked` baseline verdict.
- A successful probe that finds a different HEAD or tracked checkout changes remains a baseline blocker; this item does not weaken exact-base validation or publication requirements.

## Acceptance Criteria

- [x] A sandbox manager whose validated standalone browser bundle is removed after construction refreshes or disables that optional browser and successfully executes the next non-browser command without a daemon restart.
- [x] Concurrent command construction cannot observe a partially refreshed browser executable, bundle, or sandbox path.
- [x] A failed exact-base HEAD or status probe raises a retryable controller error containing the actual sandbox failure rather than reporting that the checkout is dirty.
- [x] A post-validation probe failure attempts to restore the exact base without masking the original retryable probe error, allowing a later retry to recreate the missing baseline.
- [x] Actual exact-base SHA mismatch or tracked changes still prevent baseline creation.

## Verification

- [x] `INTEGRATION` — construct a manager with a valid standalone browser fixture, remove its bundle, and execute a real Bubblewrap command successfully through the same manager.
- [x] `UNIT` — force an exact-base Git probe launch failure and prove execution remains nonterminal and exposes the underlying failure for automatic retry.
- [x] `UNIT` — mutate the tracked checkout during baseline execution, fail the post-command probe once, prove the original error survives a successful exact-base reset, and recreate the baseline on retry.
- [x] `REGRESSION` — run the focused sandbox browser and execution-baseline tests, including existing invalid-browser, browser-launcher, and exact-base baseline behavior.
- [x] `REGRESSION` — run the complete deterministic test suite.
