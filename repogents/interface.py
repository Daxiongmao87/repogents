from __future__ import annotations

import json
import secrets
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

_MAX_BODY = 1_000_000


class InterfaceActions(Protocol):
    def state(self) -> dict[str, object]: ...

    def add_repository(self, identity: str, inputs: dict[str, object]) -> str: ...

    def reonboard(self, repository_id: str, inputs: dict[str, object]) -> str: ...

    def cancel(self, run_id: str) -> None: ...

    def acknowledge(self, notification_id: str) -> None: ...

    def poll(self) -> None: ...

    def acceptance_artifact(self, artifact_id: str) -> tuple[bytes, str]: ...


class LocalInterfaceServer:
    def __init__(
        self,
        *,
        actions: InterfaceActions,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.actions = actions
        self.csrf_token = secrets.token_urlsafe(32)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                outer._get(self)

            def do_POST(self) -> None:  # noqa: N802
                outer._post(self)

            def log_message(self, format: str, *args: object) -> None:
                del format, args
                return None

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        bound_host, bound_port = self.address
        self._canonical_authority = _http_authority(bound_host, bound_port)
        self._canonical_origin = f"http://{self._canonical_authority}"

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def close(self) -> None:
        self.httpd.server_close()

    def _get(self, request: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(request.path).path
        if path == "/":
            body = _DASHBOARD.encode("utf-8")
            self._send(
                request,
                HTTPStatus.OK,
                body,
                "text/html; charset=utf-8",
                extra_headers={"X-Repogents-CSRF": self.csrf_token},
            )
            return
        if path == "/api/state":
            self._send_json(request, HTTPStatus.OK, self.actions.state())
            return
        segments = [
            urllib.parse.unquote(segment)
            for segment in path.strip("/").split("/")
            if segment
        ]
        if len(segments) == 3 and segments[:2] == ["api", "acceptance-artifacts"]:
            try:
                body, media_type = self.actions.acceptance_artifact(segments[2])
            except KeyError:
                self._send_json(
                    request,
                    HTTPStatus.NOT_FOUND,
                    {"error": "acceptance artifact not found"},
                )
                return
            except RuntimeError as error:
                self._send_json(
                    request,
                    HTTPStatus.CONFLICT,
                    {"error": str(error)},
                )
                return
            self._send(request, HTTPStatus.OK, body, media_type)
            return
        self._send_json(request, HTTPStatus.NOT_FOUND, {"error": "route not found"})

    def _post(self, request: BaseHTTPRequestHandler) -> None:
        if not self._authorized_mutation(request):
            self._send_json(
                request,
                HTTPStatus.FORBIDDEN,
                {"error": "invalid local origin or CSRF token"},
            )
            return
        try:
            payload = self._read_json(request)
            path = urllib.parse.urlsplit(request.path).path
            segments = [
                urllib.parse.unquote(segment)
                for segment in path.strip("/").split("/")
                if segment
            ]
            if segments == ["api", "repositories"]:
                identity = payload.get("repository")
                inputs = payload.get("inputs", {})
                if not isinstance(identity, str) or not identity.strip():
                    raise ValueError("repository must be a nonempty string")
                if not isinstance(inputs, dict):
                    raise ValueError("inputs must be an object")
                repository_id = self.actions.add_repository(identity.strip(), inputs)
                self._send_json(
                    request,
                    HTTPStatus.CREATED,
                    {"repository_id": repository_id},
                )
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "repositories"]
                and segments[3] == "reonboard"
            ):
                inputs = payload.get("inputs", {})
                if not isinstance(inputs, dict):
                    raise ValueError("inputs must be an object")
                version_id = self.actions.reonboard(segments[2], inputs)
                self._send_json(request, HTTPStatus.OK, {"version_id": version_id})
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "runs"]
                and segments[3] == "cancel"
            ):
                self.actions.cancel(segments[2])
                self._send_json(request, HTTPStatus.OK, {"ok": True})
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "notifications"]
                and segments[3] == "acknowledge"
            ):
                self.actions.acknowledge(segments[2])
                self._send_json(request, HTTPStatus.OK, {"ok": True})
                return
            if segments == ["api", "poll"]:
                self.actions.poll()
                self._send_json(request, HTTPStatus.OK, {"ok": True})
                return
            self._send_json(request, HTTPStatus.NOT_FOUND, {"error": "route not found"})
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json(request, HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except KeyError as error:
            self._send_json(request, HTTPStatus.NOT_FOUND, {"error": str(error)})
        except Exception as error:
            self._send_json(
                request,
                HTTPStatus.CONFLICT,
                {"error": str(error) or error.__class__.__name__},
            )

    def _authorized_mutation(self, request: BaseHTTPRequestHandler) -> bool:
        token = request.headers.get("X-Repogents-CSRF", "")
        origin = request.headers.get("Origin", "")
        host = request.headers.get("Host", "")
        return (
            secrets.compare_digest(token, self.csrf_token)
            and host == self._canonical_authority
            and origin == self._canonical_origin
        )

    @staticmethod
    def _read_json(request: BaseHTTPRequestHandler) -> dict[str, object]:
        content_type = request.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("request content type must be application/json")
        raw_length = request.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("request content length is required")
        length = int(raw_length)
        if length < 0 or length > _MAX_BODY:
            raise ValueError("request body is too large")
        value = json.loads(request.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _send_json(
        self,
        request: BaseHTTPRequestHandler,
        status: HTTPStatus,
        value: object,
    ) -> None:
        self._send(
            request,
            status,
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
        )

    @staticmethod
    def _send(
        request: BaseHTTPRequestHandler,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        request.send_response(int(status))
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        )
        request.send_header("Referrer-Policy", "no-referrer")
        for name, value in (extra_headers or {}).items():
            request.send_header(name, value)
        request.end_headers()
        request.wfile.write(body)


def _http_authority(host: str, port: int) -> str:
    canonical_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{canonical_host}:{port}"


_DASHBOARD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repogents</title>
<style>
:root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }
body { max-width: 78rem; margin: 0 auto; padding: 1.5rem; line-height: 1.45; }
header, .row { display: flex; gap: .75rem; align-items: center; justify-content: space-between; flex-wrap: wrap; }
h1, h2 { line-height: 1.1; } section { margin: 2rem 0; }
.card { border: 1px solid #8888; border-radius: .6rem; padding: 1rem; margin: .75rem 0; }
.muted { opacity: .72; } .error { color: #d33; white-space: pre-wrap; }
input, textarea, button { font: inherit; padding: .55rem; }
input, textarea { width: min(100%, 42rem); box-sizing: border-box; }
textarea { min-height: 5rem; font-family: ui-monospace, monospace; }
button { cursor: pointer; } a { overflow-wrap: anywhere; }
.badge { border: 1px solid #8888; border-radius: 999px; padding: .2rem .55rem; }
.unread { border-inline-start: .4rem solid #2878ff; }
</style>
</head>
<body>
<header><h1>Repogents</h1><button id="poll">Poll now</button></header>
<p id="error" class="error" role="alert"></p>
<section>
<h2>Add repository</h2>
<form id="add"><p><label>GitHub URL or owner/name<br><input name="repository" required placeholder="owner/repository"></label></p>
<p><label>Repository inputs (JSON object)<br><textarea name="inputs">{}</textarea></label></p>
<button type="submit">Onboard</button></form>
</section>
<section><h2>Repositories</h2><div id="repositories"></div></section>
<section>
  <div class="row"><h2>Selected repository</h2><label>Repository <select id="repository-filter"></select></label></div>
  <h3>agent:ready issues</h3><div id="ready-issues"></div>
  <h3>Issues and runs</h3><div id="runs"></div>
</section>
<section><h2>All active issues</h2><div id="active-issues"></div></section>
<section><h2>Notifications</h2><div id="notifications"></div></section>
<script>
let token = '';
let selectedRepositoryId = '';
let currentState = {repositories: [], runs: [], ready_issues: []};
const displayInputs = new Map();
const error = document.querySelector('#error');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const githubLink = (url, label) => /^https:\/\/github\.com\//.test(url || '') ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(label)}</a>` : esc(label);
const short = value => value ? esc(String(value).slice(0, 12)) : '—';
const runActions = r => !['canceled','closed'].includes(r.state) ? `<button data-cancel="${esc(r.id)}">Cancel</button>` : '';
const evidence = r => `<details><summary>Durable evidence</summary><p>Base <code>${short(r.base_sha)}</code> · Validated <code>${short(r.validated_sha)}</code></p><p>Sandbox v${esc(r.sandbox_version)} <code>${esc(r.sandbox_version_id)}</code><br>Team v${esc(r.team_version)} <code>${esc(r.team_version_id)}</code></p><h3>Assignments</h3><ul>${(r.assignments || []).map(a => `<li>${esc(a.stable_key)} (${esc(a.role)}): ${esc(a.reasoning)}</li>`).join('') || '<li>None recorded</li>'}</ul><h3>Validation</h3><ul>${(r.validation_results || []).map(v => `<li><code>${esc((v.command || []).join(' '))}</code> — exit ${esc(v.exit_status)} for <code>${short(v.commit_sha)}</code>; log <code>${esc(v.log_path)}</code></li>`).join('') || '<li>None recorded</li>'}</ul></details>`;
const acceptanceArtifact = a => String(a.url || '').startsWith('/api/acceptance-artifacts/')
  ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.description || a.kind)}</a> <code>${short(a.sha256)}</code>`
  : `${esc(a.description || a.kind)} <code>${short(a.sha256)}</code>`;
const acceptanceEvidence = a => {
  if (!a) return '<details><summary>Issue acceptance</summary><p>Not yet verified for the current commit.</p></details>';
  const claims = (a.claims || []).map(c => `<li><strong>${esc(c.result || 'planned')}</strong> ${esc(c.claim)}<br><span class="muted">${esc(c.observed || c.expected || '')}</span></li>`).join('') || '<li>No claims recorded.</li>';
  const observations = (a.evidence || []).map(o => `<li>#${esc(o.sequence)} <code>${esc(JSON.stringify(o.action || {}))}</code> — ${esc(JSON.stringify(o.result ?? ''))}</li>`).join('') || '<li>No observations recorded.</li>';
  const scope = (a.scope || []).map(s => `<li><code>${esc(s.path)}</code> — ${esc(s.result)}: ${esc(s.necessity)} (${esc((s.claim_keys || []).join(', '))})</li>`).join('') || '<li>No scope mapping recorded.</li>';
  const artifacts = (a.artifacts || []).map(item => `<li>${acceptanceArtifact(item)}</li>`).join('') || '<li>No artifacts recorded.</li>';
  const limitations = (a.limitations || []).map(item => `<li>${esc(item)}</li>`).join('') || '<li>None recorded.</li>';
  return `<details><summary>Issue acceptance — ${esc(a.state)}</summary><p>Commit <code>${short(a.commit_sha)}</code><br>${esc(a.summary)}</p><h3>Claims</h3><ul>${claims}</ul><h3>Controller observations</h3><ul>${observations}</ul><h3>Changed-file scope</h3><ul>${scope}</ul><h3>Visual decision</h3><p>${a.screenshot_decision?.required ? 'Screenshots required' : 'Screenshots not required'}: ${esc(a.screenshot_decision?.reason || '')}</p><ul>${artifacts}</ul><h3>Limitations</h3><ul>${limitations}</ul></details>`;
};
async function mutate(path, payload) {
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Repogents-CSRF':token}, body:JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
const activeRun = run => !['blocked', 'canceled', 'closed'].includes(run.state);
const runCard = run => `<article class="card"><div class="row"><strong>${esc(run.repository)} · ${githubLink(run.issue_url, `Issue #${run.issue_number}: ${run.issue_title}`)}</strong><span class="badge">${esc(run.state)}</span></div><p>Last completed: ${esc(run.last_completed_state ?? 'none')}</p>${run.pull_url ? `<p>${githubLink(run.pull_url, `Pull request #${run.pull_number}`)}</p>` : ''}${run.reason ? `<p class="error">${esc(run.reason)}</p>` : ''}${evidence(run)}${acceptanceEvidence(run.acceptance_verification)}<p>${runActions(run)}</p></article>`;
function renderIssueViews() {
  const repositories = currentState.repositories || [];
  if (!repositories.some(repository => String(repository.id) === selectedRepositoryId)) {
    selectedRepositoryId = repositories.length ? String(repositories[0].id) : '';
  }
  const filter = document.querySelector('#repository-filter');
  filter.innerHTML = repositories.map(repository => `<option value="${esc(repository.id)}" ${String(repository.id) === selectedRepositoryId ? 'selected' : ''}>${esc(repository.identity)}</option>`).join('');
  filter.disabled = !repositories.length;
  const readyIssues = (currentState.ready_issues || []).filter(issue => String(issue.repository_id) === selectedRepositoryId);
  document.querySelector('#ready-issues').innerHTML = readyIssues.map(issue => `<article class="card"><strong>${githubLink(issue.url, `Issue #${issue.number}: ${issue.title}`)}</strong><p class="muted">Updated ${esc(issue.updated_at)}</p></article>`).join('') || '<p class="muted">No agent:ready issues for this repository.</p>';
  const repositoryRuns = (currentState.runs || []).filter(run => String(run.repository_id) === selectedRepositoryId && activeRun(run));
  document.querySelector('#runs').innerHTML = repositoryRuns.map(runCard).join('') || '<p class="muted">No active issue runs for this repository.</p>';
  const activeRuns = (currentState.runs || []).filter(activeRun);
  document.querySelector('#active-issues').innerHTML = activeRuns.map(runCard).join('') || '<p class="muted">No active issues.</p>';
}
async function refresh() {
  try {
    const response = await fetch('/api/state', {cache:'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    currentState = await response.json();
    displayInputs.clear();
    currentState.repositories.forEach(r => displayInputs.set(String(r.id), r.display_inputs));
    document.querySelector('#repositories').innerHTML = currentState.repositories.map(r => `<article class="card"><div class="row"><strong>${githubLink(r.url, r.identity)}</strong><span class="badge">${esc(r.onboarding_state)}</span></div><p>Default: ${esc(r.default_branch)}<br>Sandbox v${esc(r.sandbox_version ?? '—')} <code>${esc(r.sandbox_version_id ?? '—')}</code><br>Team v${esc(r.team_version ?? '—')} <code>${esc(r.team_version_id ?? '—')}</code></p><details><summary>Retained inputs</summary><pre>${esc(JSON.stringify(r.display_inputs, null, 2))}</pre></details>${r.blocking_reason ? `<p class="error">${esc(r.blocking_reason)}</p>` : ''}<button data-reonboard="${esc(r.id)}">Re-onboard</button></article>`).join('') || '<p class="muted">No repositories.</p>';
    renderIssueViews();
    document.querySelector('#notifications').innerHTML = currentState.notifications.map(n => `<article class="card ${n.read_at ? '' : 'unread'}"><strong>${esc(n.owner)}/${esc(n.name)}</strong><p>${githubLink(n.issue_url, `Issue #${n.issue_number}: ${n.issue_title}`)} · ${githubLink(n.pull_url, `PR #${n.pull_number}`)}</p><p class="muted">${esc(n.created_at)}</p>${n.read_at ? '<span class="badge">Acknowledged</span>' : `<button data-ack="${esc(n.id)}">Acknowledge</button>`}</article>`).join('') || '<p class="muted">No notifications.</p>';
    error.textContent = '';
  } catch (e) { error.textContent = e.message; }
}
async function action(fn) { try { await fn(); await refresh(); } catch (e) { error.textContent = e.message; } }
document.querySelector('#add').addEventListener('submit', event => { event.preventDefault(); action(async () => { const form = new FormData(event.target); await mutate('/api/repositories', {repository:form.get('repository'), inputs:JSON.parse(form.get('inputs'))}); event.target.reset(); event.target.elements.inputs.value='{}'; }); });
document.querySelector('#poll').addEventListener('click', () => action(() => mutate('/api/poll', {})));
document.querySelector('#repository-filter').addEventListener('change', event => { selectedRepositoryId = String(event.target.value || ''); renderIssueViews(); });
document.body.addEventListener('click', event => { const b=event.target.closest('button'); if (!b) return; if (b.dataset.reonboard) action(() => { const raw=prompt('Repository inputs JSON object:', JSON.stringify(displayInputs.get(b.dataset.reonboard), null, 2)); if (raw === null) return Promise.resolve(); return mutate(`/api/repositories/${encodeURIComponent(b.dataset.reonboard)}/reonboard`, {inputs:JSON.parse(raw)}); }); if (b.dataset.cancel && confirm('Cancel this run?')) action(() => mutate(`/api/runs/${encodeURIComponent(b.dataset.cancel)}/cancel`, {})); if (b.dataset.ack) action(() => mutate(`/api/notifications/${encodeURIComponent(b.dataset.ack)}/acknowledge`, {})); });
fetch('/', {cache:'no-store'}).then(r => { token = r.headers.get('X-Repogents-CSRF') || ''; refresh(); });
setInterval(refresh, 10000);
</script>
</body>
</html>
"""
