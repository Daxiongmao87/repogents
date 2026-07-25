# Bind Daemon to All Interfaces

Changes the configured bind address for the deployed Repogents daemon. This is a delta to `spec/repository-agent-mvp/010-local-interface.md`: the explicit IPv4 bind address becomes the wildcard address `0.0.0.0`; the configured port and application authorization behavior are unchanged.

## Acceptance Criteria

- [x] The deployed user service is configured with `REPOGENTS_LAN_HOST=0.0.0.0` and retains port `8766`.
- [x] After restart, `repogents.service` is active and listens on `0.0.0.0:8766` without a restart loop.
- [x] The daemon state endpoint is reachable through both the LAN address `192.168.0.206:8766` and ZeroTier address `10.241.0.1:8766`.
- [x] Restarting and probing the service does not alter durable run priority or focus state.

## Verification

- [x] `CONFIG` — inspect the effective environment and service process arguments after restart.
- [x] `HOST` — inspect the listening socket and service health fields.
- [x] `HTTP` — fetch `/api/state` through the LAN and ZeroTier IPv4 addresses and receive valid state responses.
- [x] `DATABASE` — compare run priority and focus rows before and after restart and read-only probes.
