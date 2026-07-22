# README, Repository Hygiene, and Initial Publication

This item governs the first Git publication of the current Repogents source tree. It does not change product behavior or widen the MVP in `MVP.md`.

## Contract

- The user-facing product name is **Repogents**.
- `README.md` describes only behavior supported by current source. It explains prerequisites, installation, configuration, first use, repository onboarding, `agent:ready` activation, pull-request feedback, quiet-period notifications, security boundaries, persistent state, and MVP limits.
- `.gitignore` excludes generated build/package metadata, caches, virtual environments, local databases and state, logs, coverage output, temporary/editor/OS files, local agent/session artifacts, environment files, credentials, keys, and certificates. It must not broadly exclude source, tests, specifications, user documentation, dependency lockfiles, or intentional fixtures.
- The initial commit contains the current project source and governing documentation, but no observed environment-specific artifacts or secrets.
- The initial commit is pushed to a new public GitHub repository at `https://github.com/Daxiongmao87/repogents`. No unrelated GitHub resource is created or mutated.

## Acceptance Criteria

- [x] The local browser interface identifies the product as `Repogents`.
- [x] `README.md` is headed `Repogents` and gives a source-accurate, user-facing path from installation through notification acknowledgment.
- [x] `README.md` documents required model/GitHub configuration, durable data handling, sandbox and credential boundaries, and explicit MVP limitations without exposing live credentials or host-specific acceptance values.
- [x] `.gitignore` covers relevant generated, environment-specific, and secret-bearing artifact classes while preserving intentional project inputs.
- [x] The Git index for the initial commit excludes current cache, package metadata, swap, session-capture, local-state, and secret artifacts.
- [x] One initial local commit is created from the intended project files.
- [x] The initial commit is pushed to the user-selected remote repository and branch.

## Verification

- [x] `STATIC` — compare every documented command, option, environment variable, and workflow claim with current package and CLI source.
- [x] `STATIC` — exercise `.gitignore` against representative generated/secret paths and inspect the complete initial Git index before committing.
- [x] `SECURITY` — scan the complete staged initial commit with a redacting secret scanner and resolve every finding before commit or push.
- [x] `INTEGRATION` — run the complete deterministic test suite against the exact source being committed.
- [x] `REMOTE` — verify the pushed remote branch resolves to the exact local initial-commit SHA and the repository has the selected identity and visibility.
