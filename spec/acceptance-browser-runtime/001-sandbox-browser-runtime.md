# Sandbox Browser Runtime

Changes the sandbox and live-acceptance contracts established by `spec/repository-agent-mvp/003-stored-sandbox.md`, `spec/repository-agent-mvp/011-mvp-acceptance.md`, and `spec/validation-delta-comparator/001-clean-as-you-code-validation.md`.

## Contract

- When a configured or locally cached standalone Chromium-compatible executable is available, the controller validates it and exposes only its browser bundle read-only inside repository sandboxes.
- Sandbox commands receive a stable browser executable path through `CHROME_BIN`; controller home and cache directories remain inaccessible.
- The advertised browser launcher always assigns isolated writable home, profile, configuration, and cache paths under run storage and disables standalone crash reporting, even when a repository command overrides `HOME`.
- Root and nested onboarded Node dependency trees are mounted at their workspace-relative package roots for both writable agent commands and read-only acceptance commands. Read-only commands receive read-only dependency mounts.
- If no executable browser is available, the sandbox does not advertise one or substitute a stub; visual acceptance may block with the missing capability.
- Acceptance evidence remains commit-bound and must come from the isolated checkout and controller-observed screenshot artifact path.

## Acceptance Criteria

- [x] A sandbox command can launch the validated standalone browser without access to its host cache directory.
- [x] Read-only acceptance commands can use onboarded root and nested-package dependencies.
- [x] Browser and dependency mounts are read-only when the checkout is read-only.
- [x] Missing or invalid browser candidates are not advertised as usable.
- [x] Chrome for Testing startup remains usable when an acceptance script points `HOME` at read-only persistent state.
- [ ] Bazzeye issue #7 visual behavior is verified at the exact candidate SHA and publishes one unmerged pull request.

## Verification

- [x] `UNIT` — command construction mounts only the validated browser bundle and sets the stable `CHROME_BIN` path.
- [x] `REGRESSION` — a read-only checkout executes an onboarded nested-package tool through a read-only mount.
- [x] `INTEGRATION` — an actual sandbox launches the configured browser headlessly and writes a screenshot under run storage.
- [x] `UNIT` — an invalid browser candidate yields no browser mount or environment variable.
- [x] `REGRESSION` — the browser launcher replaces a read-only `HOME` and supplies standalone crash-reporting disable flags.
- [ ] `LIVE` — retry Bazzeye issue #7, capture required browser evidence, and confirm an unmerged exact-SHA pull request.
