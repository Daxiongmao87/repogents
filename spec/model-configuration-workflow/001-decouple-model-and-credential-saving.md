# Decouple Model and Credential Saving

Supersedes the cross-provider key-reuse requirement in `spec/runtime-configuration-ui/001-model-provider-configuration.md` acceptance criterion 8 and changes its inline-panel contract. The endpoint, credential, and model selection are independent settings in one connection profile; changing a model must not become credential rotation.

## Contract

- Model-provider configuration opens from a compact dashboard status control in a modal dialog rather than occupying the primary repository workspace.
- Saving a valid endpoint and model selection persists them whether or not an API key is available. The interface distinguishes “settings saved” from “ready for model execution.”
- A blank API-key field always preserves the stored key. Changing any model field never requires replacing or clearing that key. Explicit key replacement and explicit key removal remain supported.
- The browser clears a newly entered key only after a successful save. A rejected submission retains the field so correcting another input does not require re-entering the credential.
- The modal displays bounded mutation errors and a precise post-save result. Closing and reopening it restores persisted non-secret settings and never renders the stored key.
- When the configured OpenAI-compatible endpoint and credential can list models, the server exposes a redacted catalog to the modal. The user selects the endpoint's model identifier while the application owns the internal LiteLLM transport prefix.
- Catalog availability and model validation affect execution readiness, not persistence. A missing credential or unavailable catalog leaves bounded manual model entry available.
- Permanent provider configuration failures such as an unavailable model stop after the first response and surface as actionable configuration errors rather than entering the transient retry schedule.
- Existing secret storage permissions, response redaction, canonical-origin checks, and CSRF protection remain unchanged.

## Acceptance Criteria

- [x] The configuration form is available through a modal opened from a compact dashboard control; repository inventory remains the primary page content.
- [x] Endpoint and model changes save without an API key, while execution readiness separately reports a missing required credential.
- [x] Changing the default or role model preserves a stored key without requiring key replacement or removal, including when the model prefix changes.
- [x] A failed save retains the newly entered key in the form; a successful save clears it and no response or page state exposes the stored value.
- [x] Explicit key replacement and removal continue to work, and all existing secret, origin, CSRF, and bounded-input protections remain enforced.
- [x] A reachable configured endpoint supplies its model catalog without exposing the credential; selecting `codex/gpt-5.6-sol` stores and executes the correct internal OpenAI-compatible model identifier.
- [x] Missing credentials or an unavailable catalog do not block manual model persistence, while a permanent `model not found` response does not retry.
- [x] The deployed browser workflow can save a model-only change, report success, survive reload, and reopen the modal with the persisted model and a configured-key indicator.

## Verification

- [x] `UNIT` — prove model-only and endpoint/model saves persist without credentials, and provider-prefix changes preserve a stored key.
- [x] `UNIT` — prove explicit key replacement/removal, reload, permissions, redaction, and invalid-input atomicity remain intact.
- [x] `HTTP` — prove configuration mutation still enforces canonical origin and CSRF and never returns a secret.
- [x] `CLIENT` — prove modal open/close, saved-versus-ready status, model-only save, failure retention, successful clearing, reload, and configured-key indication in a real browser.
- [x] `CLIENT` — load the live endpoint catalog without exposing its credential, select a returned model, and prove the internal transport identifier is not required user input.
- [x] `INTEGRATION` — prove permanent provider configuration errors stop after one attempt while transient failures retain bounded retry behavior.
- [x] `REGRESSION` — run the affected configuration, application, interface, mini-SWE, onboarding, and team suites, then the complete suite.
