# Authorize Configured Client Origin

Changes the mutation-authorization contract established by `spec/repository-agent-mvp/010-local-interface.md`. A wildcard listen address is a socket binding, not a browser origin; `0.0.0.0` must never be used as the expected `Host` or `Origin` for browser mutations.

## Contract

- `LocalInterfaceServer` accepts an optional explicit client origin independently of its listen host and port.
- A concrete listen address may continue to derive its client origin from the actual bound address for local and ephemeral-port use.
- A wildcard listen address requires an explicit client origin and fails closed during startup when none is configured.
- The configured client origin is a normalized absolute HTTP or HTTPS origin with no credentials, path, query, or fragment.
- Mutation authorization still requires all three existing checks: the per-process CSRF token, a `Host` header exactly matching the configured client-origin authority, and an `Origin` header exactly matching the configured client origin.
- The CLI reads the client origin from `--client-origin` or `REPOGENTS_CLIENT_ORIGIN` and passes it to the server.
- Repository, queue, force, cancel, notification, model, and poll mutations retain the same authorization boundary and behavior after authorization succeeds.

## Acceptance Criteria

- [x] A dashboard loaded from the configured LAN origin can force and release an actionable run without a 403 response.
- [x] Wildcard binding does not make `http://0.0.0.0:<port>` the expected browser origin.
- [x] Missing explicit client origin on a wildcard bind fails closed before serving requests.
- [x] A valid token paired with a different `Host` or `Origin` remains forbidden.
- [x] Concrete loopback and ephemeral test servers retain their derived-origin behavior.

## Verification

- [x] `REGRESSION` — bind a server to `0.0.0.0` with a configured LAN origin, submit the force mutation with matching browser headers, and prove the action succeeds; mismatched origins remain forbidden.
- [x] `UNIT` — reject malformed client origins and wildcard binds without a client origin.
- [x] `REGRESSION` — run the interface and application suites.
- [x] `LIVE` — deploy the configured origin, click **Force work on this issue** from that dashboard origin, observe a successful response and forced state, then click **Release forced work** and observe the original state restored.
