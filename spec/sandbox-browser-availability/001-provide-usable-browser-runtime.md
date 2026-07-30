# Provide a Usable Browser Runtime

## Prior Contracts

This item changes the browser-validation contract established by `spec/acceptance-browser-runtime/001-sandbox-browser-runtime.md` and refined by `spec/stale-browser-mount-recovery/001-recover-stale-optional-browser-mount.md`. It relies on `spec/agent-dependency-retrieval/001-retrieve-dependencies-in-sandbox.md` when a run must retrieve its own browser. A host launcher that prints a Chromium version but cannot execute inside the repository sandbox is not a validated browser.

## Contract

- The controller advertises `CHROME_BIN` only after the selected Chromium-compatible executable completes a bounded headless render probe from the same Bubblewrap mount and environment boundary used by repository commands.
- A launcher that works only on the host, or reports a version without launching headlessly inside the sandbox, is rejected rather than exposed as a usable browser.
- Controller browser injection is opt-in through `REPOGENTS_BROWSER_EXECUTABLE`; without that configuration, the controller does not auto-discover host or cache browsers and agents retrieve a browser only when their run needs one.
- When no validated controller browser exists, a run may retrieve and execute a browser from its run-local dependency delta under the agent dependency-retrieval contract.
- Existing browser bundle refresh, checkout isolation, secret isolation, and exact-candidate acceptance requirements remain unchanged.

## Acceptance Criteria

- [x] Host-only browser launchers and version-only nonfunctional candidates are not advertised through `CHROME_BIN`.
- [x] With no explicit browser configuration, discoverable host and cache browsers are not injected into repository sandboxes.
- [x] With no controller-injected browser, the existing Repogents issue #7 run is retried at its exact candidate SHA, retrieves and launches a browser inside its sandbox, records direct screenshot evidence, and advances without replacing its run identity.

## Verification

- [x] `UNIT` — prove host-only and version-only candidates are rejected while a sandbox-headless-capable bundle is advertised.
- [x] `REGRESSION` — run the focused sandbox browser tests, including valid bundle mounting, stale-bundle recovery, writable browser runtime paths, and invalid candidates.
- [x] `UNIT` — prove an unconfigured manager does not auto-inject a discoverable host browser.
- [x] `LIVE` — remove the controller-injected browser, restart `repogents.service` with durable state preserved, retry Repogents issue #7, and inspect its sandbox retrieval plus exact-SHA screenshot evidence and resulting lifecycle state.
