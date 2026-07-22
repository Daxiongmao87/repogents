# Minimal Local Interface

Implements `MVP.md` §10 as one locally operated interface. It is single-user and intentionally excludes accounts, tenants, organizations, wizards, and administration surfaces.

## Contract

- The server binds according to explicit configuration. The supported client path is verified against that bind address rather than assuming loopback reachability.
- Mutating requests are accepted only from the local application origin and use per-process anti-CSRF tokens.
- The interface exposes inventory, onboarding/re-onboarding, sandbox inputs, active issues/runs, reasons, cancellation, GitHub links, and durable notification acknowledgment. Its former Retry surface is superseded by `spec/remove-manual-retry/001-remove-manual-retry.md`.
- Notifications display repository, issue, PR, time, and read state. The in-application record is authoritative; desktop notification is not required.

## Acceptance Criteria

- [x] A user can add repositories and supply only genuinely required repository-specific inputs.
- [x] Inventory shows every repository, immutable version identities, states, reasons, and active runs.
- [x] A user can explicitly re-onboard, cancel active or blocked work, and open issue/PR links; recoverable run work resumes automatically without a manual Retry control.
- [x] Run views expose current/last-completed state and durable evidence without leaking secrets.
- [x] Notifications survive restart, identify/link repository/issue/PR, and can be acknowledged.
- [x] The interface contains no non-MVP account, tenant, organization, merge, or administration capability.
- [x] Mutation authorization is pinned to the configured canonical client origin rather than an attacker-controlled `Host` header.
- [x] Inventory exposes a sanitized retained-input object and re-onboarding begins from it instead of silently replacing stored inputs with `{}`.

## Verification

- [x] `UNIT` — request validation, local-origin/CSRF enforcement, and response serialization reject malformed or unsafe mutations.
- [x] `CLIENT` — drive every required user action through the supported browser interface.
- [x] `HOST` — launch the application and prove reachability through its configured intended interface, then restart and prove state/notifications remain available.
- [x] `UNIT` — DNS-rebinding host/origin pairs are rejected and retained inputs are serialized without raw secret values.
