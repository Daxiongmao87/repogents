# Model Provider Configuration UX

Implements a secure, browser-operated configuration surface for the model provider used by onboarding and repository agents. This changes the startup-only model contract established by `spec/repository-agent-mvp/006-autonomous-execution.md` and the local-interface contract in `spec/repository-agent-mvp/010-local-interface.md`: provider settings can be persisted and changed at runtime, while repository team versions remain immutable.

## Contract

- The local dashboard exposes one **Model provider** settings panel with an optional OpenAI-compatible API endpoint, a write-only API key, one required default model selector, and optional lead, implementer, and verifier model overrides. Blank role overrides inherit the default model.
- The default model performs onboarding evidence inference. Newly onboarded or explicitly re-onboarded repositories store the configured role selectors in their immutable team version. Existing team versions and active runs are never rewritten by a settings change.
- Endpoint and API-key changes apply to subsequent model invocations, including invocations by existing stored teams. A settings change does not interrupt an invocation already in progress.
- Non-secret settings are stored atomically under the durable data root. The API key is stored separately in a mode-`0600` file beneath a mode-`0700` directory and is never stored in SQLite, returned by an API, rendered into HTML, included in logs, or exposed to repository sandboxes.
- The settings response exposes only whether an API key is configured. A blank API-key field preserves the stored key; removing it requires an explicit clear-key control.
- Existing CLI/environment model and endpoint values bootstrap the runtime when no browser-saved settings exist. Existing provider environment credentials remain usable until a key is saved through the interface. The application can start and present configuration UX without a model; model-dependent work then fails with a bounded configuration error rather than preventing the dashboard from starting.
- Provider configuration mutations retain the interface's exact canonical-origin and per-process CSRF requirements. Input is bounded and model selectors are nonempty; an endpoint, when supplied, must be an absolute `http` or `https` URL without embedded credentials.

## Acceptance Criteria

- [x] The dashboard lets the operator save an API endpoint, write-only API key, default model, and optional per-role model overrides, and clearly explains inheritance and re-onboarding semantics.
- [x] Saving settings applies the endpoint and key to subsequent inference and applies model selectors to subsequent onboarding/team formulation without restarting the daemon.
- [x] The API key is atomically persisted with restrictive permissions and no response, page state, database record, or log contains its value.
- [x] Reloading the process restores browser-saved settings; startup-only values remain a backward-compatible bootstrap when no saved settings exist.
- [x] A blank key preserves the current key, explicit removal clears it, invalid inputs return bounded JSON errors, and origin/CSRF protections remain enforced.
- [x] The dashboard remains available when model configuration is incomplete and reports configuration state without exposing a secret.
- [x] A key-based provider without a credential is reported as incomplete, and repository failures show a concise actionable summary while retaining the full diagnostic behind an explicit disclosure.
- [x] A saved key is injected only for its configured single-key provider; provider changes cannot reuse that secret implicitly, and multi-value environment credentials retain their original variable names.

## Verification

- [x] `UNIT` — prove settings validation, atomic persistence, file permissions, reload, bootstrap precedence, blank-key preservation, explicit key removal, and secret redaction.
- [x] `UNIT` — prove subsequent onboarding and stored-agent runtime construction use the current endpoint/key and role model selectors while existing stored team rows remain unchanged.
- [x] `HTTP` — read and mutate settings through canonical-origin/CSRF endpoints, reject invalid and unauthorized requests, and prove the key is absent from state and HTML.
- [x] `CLIENT` — configure and reload settings in a real browser, observe only a configured-key indicator, and verify the form explains when re-onboarding is required.
- [x] `CLIENT` — prove the missing-key status and concise repository error remain visible without expanding the complete worker traceback.
- [x] `UNIT` — prove cross-provider key reuse and unsupported managed-key saves are rejected, while multi-value and alternative environment credentials pass through without collapsing into one API-key variable.
- [x] `REGRESSION` — run the affected controller, mini-SWE, onboarding, application, interface, execution, publication, feedback, acceptance, and lifecycle suites, then the complete suite.
