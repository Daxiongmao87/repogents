# Pull Main and Restart Repogents

Adds an automatic updater for the trusted local `~/projects/repogents` checkout and its `repogents.service` user service. The updater follows `origin/main` only through clean fast-forward updates and retains a durable post-restart checkpoint so an interruption after updating source cannot skip the required service restart.

## Acceptance Criteria

- [x] An enabled systemd user timer checks `origin/main` automatically at a bounded interval and runs at most one update check at a time.
- [x] When a clean local `main` is behind `origin/main`, the updater fast-forwards the checkout, restarts `repogents.service`, verifies it is active, and records the restarted commit only after that succeeds.
- [x] When the checkout commit already matches both `origin/main` and the durable restarted-commit checkpoint, an update check does not restart Repogents.
- [x] If updating succeeds but restarting or health verification fails, the restarted-commit checkpoint remains unchanged so a later check retries the restart without requiring another remote commit.
- [x] The updater refuses to change or restart a checkout on another branch, with tracked changes, or requiring a non-fast-forward update; untracked application state does not block a safe update.
- [x] The updater service and timer are installed and enabled for the current user without changing the existing Repogents service configuration, and live Repogents durable state survives the updater's initial restart.

## Verification

- [x] `UPDATER TESTS` — use a temporary local Git remote and fake service controller to prove fast-forward/restart/checkpoint, unchanged no-op, failed-restart retry, and unsafe-checkout refusal behavior.
- [x] `UNIT VALIDATION` — verify the updater systemd service and timer definitions with `systemd-analyze verify`.
- [x] `LIVE TIMER` — enable and start the updater timer, run one updater check, and verify its successful result plus the timer's next trigger.
- [x] `LIVE SERVICE` — verify `repogents.service` remains active with no restart loop after the updater check.
- [x] `DURABLE STATE` — compare the live database schema and run identity/state baseline before and after the updater-triggered restart.
