# Route Runtime-Agnostic Bootstrap Egress

## Prior Contracts

This item changes `spec/repository-agent-mvp/003-stored-sandbox.md`, `spec/agent-dependency-retrieval/001-retrieve-dependencies-in-sandbox.md`, and `spec/repository-evidence-inference/001-repository-evidence-inference.md`. The existing restricted proxy remains the only external network path and retains final authority over exact-host allowlisting, public-address resolution, logging, and denial.

## Contract

Every exact allowlisted dependency hostname is also exposed inside the disconnected sandbox network namespace through a controller-owned loopback TCP route to the existing restricted proxy. The route operates below dependency clients and does not depend on proxy environment support or runtime-specific Java, Gradle, npm, Python, Ruby, or other tool configuration. Conventional proxy environment variables remain available for clients that honor them.

The child command receives no host networking, external DNS path, or network-administration capability. Direct IP connections and undeclared names remain unreachable. The restricted proxy performs real external resolution and rejects non-public addresses before connecting. Ordinary child-owned IPv4 and IPv6 loopback services remain independent of the synthetic dependency routes.

## Acceptance Criteria

- [x] Proxy-aware and proxy-ignorant dependency clients can reach an exact allowlisted service through the same restricted proxy boundary.
- [x] The controller configures the route without repository-, ecosystem-, dependency-, or runtime-specific settings.
- [x] Undeclared names, direct IP connections, and private destinations remain unavailable.
- [x] Child-owned IPv4 and IPv6 loopback services continue to work.
- [ ] Plaito provisioning completes its existing Gradle dependency command without changing that repository's inferred command.

## Verification

- [x] `UNIT` - exact dependency services produce isolated synthetic loopback routes while wildcard rules and ordinary localhost names are not captured.
- [x] `INTEGRATION` - a raw TCP client that ignores all proxy environment variables reaches an allowlisted fixture only through the restricted proxy, while an undeclared destination remains denied.
- [x] `INTEGRATION` - the sandbox-local resolver returns synthetic routes for allowlisted exact names and rejects undeclared names without external DNS.
- [x] `REGRESSION` - proxy-aware HTTPS access and sandbox-local IPv4/IPv6 loopback tests still pass.
- [x] `REGRESSION` - focused sandbox and onboarding suites pass.
- [x] `REGRESSION` - the complete deterministic project suite passes.
- [ ] `LIVE` - deployed Plaito re-onboarding reaches `ready`, records new provisioning evidence, and completes the unchanged Gradle dependency command.
