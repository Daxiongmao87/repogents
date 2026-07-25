# Remove Mutation Authorization Gate

Supersedes the origin/CSRF portions of `spec/repository-agent-mvp/010-local-interface.md` and all of `spec/force-button-origin/001-authorize-configured-client-origin.md`. The interface is intentionally single-user and controlled by its configured listen address; mutation authorization is not part of this MVP.

## Contract

- Every supported HTTP mutation is accepted from any client that can reach the configured interface bind, without a CSRF token or `Origin`/`Host` allowlist.
- The dashboard sends ordinary JSON `POST` requests without fetching, storing, or attaching an authorization token.
- `LocalInterfaceServer` has no client-origin configuration, origin normalization, wildcard-bind restriction, CSRF state, authorization check, or authorization-specific 403 response.
- The CLI has no `--client-origin` option and does not read `REPOGENTS_CLIENT_ORIGIN`.
- Repository, model, queue, force, cancel, notification, and poll mutation behavior and payload validation remain unchanged after removal of the gate.
- The configured bind host and port continue to determine interface reachability, including loopback and LAN binds.

## Acceptance Criteria

- [x] **Force work** and **Release forced work** succeed from the LAN dashboard without authorization headers.
- [x] The same force mutations succeed through a loopback-bound interface without authorization headers.
- [x] No client-origin or CSRF setting, token, header, check, or authorization-specific error remains in application code, CLI configuration, deployment configuration, or dashboard JavaScript.
- [x] Malformed payloads and unknown resources continue to return their existing bounded 400/404 responses.

## Verification

- [x] `HTTP` — submit force and release requests without authorization headers and prove both return 200 with the requested durable state.
- [x] `CLIENT` — inspect the served dashboard and prove mutation requests contain only JSON request metadata.
- [x] `REGRESSION` — run interface and application suites, including invalid-payload and missing-resource cases.
- [x] `LIVE` — deploy, use the LAN dashboard to force and release an actionable run, verify both responses are 200, and restore the original force state.
