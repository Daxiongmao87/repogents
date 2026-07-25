# Systemd LAN Service

Changes the bind guidance established by `spec/repository-agent-mvp/010-local-interface.md` for one explicit trusted-LAN deployment. The default remains loopback-only; this item authorizes the installed daemon to bind the workstation's primary LAN address without weakening the interface's canonical-origin or CSRF checks.

## Contract

- A systemd user service runs Repogents continuously, restarts it after failure, and terminates its full process group when stopped.
- The daemon binds the workstation's primary `192.168.0.0/24` address and port `8766`, not the already-occupied default port `8765`, `0.0.0.0`, loopback, container bridges, or overlay-network addresses.
- LAN browsers use the exact bound authority. Existing exact `Host`, exact `Origin`, and per-process CSRF checks continue to protect every mutation.
- Runtime configuration lives outside the repository in a user-owned environment file. The committed service unit contains no credential values.
- The daemon uses the normal durable data directory and the authenticated user's existing GitHub CLI session.

## Acceptance Criteria

- [x] Repogents is installed into a persistent project virtual environment and launched by a checked-in systemd user unit.
- [x] The enabled service survives terminal exit and automatically restarts after process failure.
- [x] The dashboard is reachable at `http://192.168.0.206:8766` from the LAN-facing interface.
- [x] A browser loaded from the LAN authority can complete an authenticated-CSRF mutation, while a mismatched origin remains forbidden.
- [x] The service listens only on `192.168.0.206:8766` and does not expose Repogents on loopback, overlay, container, or wildcard addresses.

## Verification

- [x] `UNIT` — the affected interface authorization suite continues to pass unchanged.
- [x] `SYSTEMD` — verify, enable, start, inspect, and restart the installed user unit successfully.
- [x] `NETWORK` — inspect the listening socket and fetch the dashboard through the primary LAN address.
- [x] `CLIENT` — open the LAN URL in a browser, load state, perform a harmless poll mutation with the page's CSRF token, and prove an attacker origin receives `403`.
