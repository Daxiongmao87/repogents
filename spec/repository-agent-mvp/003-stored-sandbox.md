# Stored Repository Sandbox

Implements `MVP.md` §3. This item owns Bubblewrap construction, persistent repository layers, isolated run layers, restricted egress, sensitive-value bindings, redaction, secret scanning, and supervised process trees.

## Contract

- Every repository stores an immutable sandbox-policy version describing mounts, tools, caches, bindings, allowed hosts, and validation environment.
- Each run has a unique writable checkout, dependency delta, build output, agent state, logs, validation, and temporary directories. Repository caches may be shared only when their implementation is concurrency-safe.
- Repository-controlled commands execute in Bubblewrap with only application-owned and explicitly allowed paths mounted, no host service sockets, isolated process/IPC namespaces, and a supervised process group.
- Network access is disabled except through an application-managed allowlisting proxy. Resolution occurs outside the sandbox; prohibited address classes are rejected after every resolution.
- GitHub/model credentials never enter the sandbox. Repository secret values are command-scoped, redacted before persistence/model use, and scanned from committed diffs before publication.

## Acceptance Criteria

- [x] Stored sandbox versions can be loaded repeatedly without reprovisioning unchanged repository dependencies.
- [x] Parallel runs never share writable checkout, delta, output, log, agent-state, or temporary paths.
- [x] A sandboxed command can access only configured paths with the configured permissions.
- [x] Unrelated host files, processes, IPC endpoints, service sockets, and credentials are inaccessible.
- [x] Direct network access fails while allowlisted external HTTP/HTTPS traffic succeeds only through the restricted path; private, loopback, link-local, multicast, and metadata destinations are rejected.
- [x] A secret binding reaches only its authorized command, is redacted from persisted output, and blocks publication when committed.
- [x] Cancellation terminates the complete descendant process tree and preserves repository sandbox/cache state.
- [x] Run-local dependency deltas are writable and participate in language install/search paths while baseline environments remain read-only.
- [x] Repository commands cannot write controller-owned run logs, validation records, or agent state.
- [x] The restricted proxy permits only globally routable resolved addresses and forwards payload bytes received with request headers.
- [x] Production command dispatch resolves only the current command's authorized secret references, redacts their values, and supplies the same values to committed-diff scanners.
- [x] Sandbox-local loopback traffic bypasses the restricted external proxy while proxy requests for loopback destinations remain denied.
- [x] Sandbox-local IPv6 loopback URLs bypass restricted egress using their bracketed URL host form without weakening proxy-side address rejection.

## Verification

- [x] `UNIT` — hostname normalization, post-resolution address filtering, output redaction, and committed-secret detection cover material bypass forms.
- [x] `ADAPTER` — Bubblewrap argv and mount policy expose only declared paths and do not inherit controller credentials.
- [x] `HOST` — execute the `MVP.md` §11.3 filesystem, process, network, cancellation, and canary-secret checks on the actual Linux host.
- [x] `INTEGRATION` — two concurrent run directories remain writable-isolated while approved caches remain reusable.
- [x] `UNIT` — dependency-layer environment paths, controller-owned mount permissions, globally routable address filtering, and coalesced proxy request bodies are covered.
- [x] `INTEGRATION` — production secret-reference resolution reaches only the authorized command and the resolved canary is redacted and blocks publication.
- [x] `HOST` — a command under restricted egress starts and reaches a sandbox-local loopback HTTP service without allowing loopback through the external proxy.
- [x] `HOST` — a restricted-egress command reaches an IPv6 loopback HTTP service at `http://[::1]` while external proxy policy still rejects `::1`.
