from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


_logger = logging.getLogger(__name__)


_CLIENT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repogents</title>
<style>
:root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #0c1018; color: #eef3ff; }
body { margin: 0; min-height: 100vh; background: radial-gradient(circle at top left, #182743 0, #0c1018 42rem); }
main { width: min(1120px, calc(100% - 2rem)); margin: 0 auto; padding: 2rem 0 4rem; }
h1 { margin: 0; font-size: clamp(2rem, 6vw, 4rem); letter-spacing: -.06em; }
.lead { color: #aebbd1; max-width: 48rem; }
.panel, .repo { border: 1px solid #2b3850; background: rgba(15, 22, 34, .86); border-radius: 14px; padding: 1rem; margin-top: 1rem; box-shadow: 0 12px 40px #0005; }
form { display: grid; grid-template-columns: 2fr 1fr auto; gap: .75rem; }
input, button { border: 1px solid #394a68; border-radius: 9px; padding: .72rem .85rem; color: inherit; background: #121c2b; }
button { cursor: pointer; background: #3267e3; border-color: #5684eb; font-weight: 700; }
button.danger { background: transparent; border-color: #8b4351; color: #ffb7c2; }
.repo-head, .run-head { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.graph { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin: .75rem 0; }
.node, .state { border-radius: 999px; background: #1b2b42; border: 1px solid #365074; padding: .3rem .65rem; font-size: .85rem; }
.arrow { color: #7386a5; }
.run { border-top: 1px solid #26354c; margin-top: .8rem; padding-top: .8rem; }
.columns { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; }
ul { margin: .35rem 0 0; padding-left: 1.2rem; color: #c8d2e4; }
a { color: #7fa9ff; }
.empty, #error { color: #aebbd1; }
#error, #poll-failure { min-height: 1.4em; margin-top: .5rem; color: #ff9eaa; }
@media (max-width: 720px) { form, .columns { grid-template-columns: 1fr; } }
</style>
</head>
<body><main>
<header><h1>Repogents</h1><p class="lead">Adaptive issue-to-pull-request agents. Track repositories, inspect the Saved agent graph, and follow every issue lifecycle.</p></header>
<section class="panel"><h2>Track repository</h2>
<form id="add-form"><input id="repository" required placeholder="owner/repository" aria-label="GitHub repository"><input id="branch" placeholder="target branch (default from GitHub)" aria-label="Target branch"><button type="submit">Add repository</button></form><div id="error" role="alert"></div></section>
<section id="poll-failure" role="alert" aria-live="polite"></section><section id="repositories" aria-live="polite"><p class="empty">Loading tracked repositories…</p></section>
</main>
<script>
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
async function api(path, options = {}) { const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options}); if (!response.ok) { const body = await response.json().catch(() => ({error: response.statusText})); throw new Error(body.error || response.statusText); } return response.status === 204 ? null : response.json(); }
function renderRun(run) { const specs = (run.specifications || []).map(x => `<li>${esc(x.title)}</li>`).join('') || '<li>None yet</li>'; const work = (run.work_items || []).map(x => `<li>${esc(x.title)} — ${esc(x.state)}</li>`).join('') || '<li>None yet</li>'; const pr = run.pull_request ? `<a href="${esc(run.pull_request.url)}" target="_blank" rel="noreferrer">#${esc(run.pull_request.number)}</a>` : 'Not created'; return `<div class="run"><div class="run-head"><strong>Issue #${esc(run.issue_number)}</strong><span class="state">${esc(run.state)}</span></div><p>Branch: ${esc(run.branch || 'Not created')} · Pull request: ${pr}</p><div class="columns"><div><strong>Specifications</strong><ul>${specs}</ul></div><div><strong>Work items</strong><ul>${work}</ul></div></div></div>`; }
function renderPollFailure(failure) { return failure ? `<div class="panel"><strong>Background polling failed</strong><p>${esc(failure.type)}: ${esc(failure.message)}</p><small>Last failure: ${esc(failure.occurred_at)}</small></div>` : ''; }
function renderRepository(repo) { const nodes = (repo.nodes || []).map((node, index) => `${index ? '<span class="arrow">→</span>' : ''}<span class="node">${esc(node.classification)} · ${esc(node.persistence)}</span>`).join(''); const runs = (repo.runs || []).map(renderRun).join('') || '<p class="empty">No queued issues.</p>'; return `<article class="repo"><div class="repo-head"><div><h2>${esc(repo.github_repository)}</h2><span>Target: ${esc(repo.target_branch)}</span></div><button class="danger" data-remove="${esc(repo.id)}">Remove</button></div><strong>Saved agent graph</strong><div class="graph">${nodes}</div>${runs}</article>`; }
async function load() { try { const state = await api('/api/state'); document.querySelector('#poll-failure').innerHTML = renderPollFailure(state.poll_failure); document.querySelector('#repositories').innerHTML = state.repositories.length ? state.repositories.map(renderRepository).join('') : '<p class="panel empty">No tracked repositories.</p>'; document.querySelectorAll('[data-remove]').forEach(button => button.onclick = async () => { await api(`/api/repositories/${button.dataset.remove}`, {method:'DELETE'}); await load(); }); } catch (error) { document.querySelector('#error').textContent = error.message; } }
document.querySelector('#add-form').addEventListener('submit', async event => { event.preventDefault(); const repository = document.querySelector('#repository').value.trim(); const branch = document.querySelector('#branch').value.trim(); try { await api('/api/repositories', {method:'POST', body:JSON.stringify({github_repository:repository, target_branch:branch || null})}); event.target.reset(); document.querySelector('#error').textContent=''; await load(); } catch (error) { document.querySelector('#error').textContent=error.message; } });
load(); setInterval(load, 3000);
</script></body></html>"""


class HttpService:
    def __init__(self, application, host: str, port: int, poll_seconds: float):
        self.application = application
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._poll_failure: dict[str, str] | None = None
        self._poll_failure_lock = threading.Lock()
        self._poll_thread: threading.Thread | None = None
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def _send_json(self, status: int, value) -> None:
                body = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _error(self, status: int, error: Exception | str) -> None:
                self._send_json(status, {"error": str(error)})

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/":
                    body = _CLIENT_HTML.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/state":
                    self._send_json(HTTPStatus.OK, service.state())
                    return
                self._error(HTTPStatus.NOT_FOUND, "not found")

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/api/repositories":
                    self._error(HTTPStatus.NOT_FOUND, "not found")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("request body must be an object")
                    repository = payload.get("github_repository")
                    if not isinstance(repository, str) or not repository.strip():
                        raise ValueError("github_repository is required")
                    target_branch = payload.get("target_branch")
                    if target_branch is not None and (
                        not isinstance(target_branch, str) or not target_branch.strip()
                    ):
                        raise ValueError("target_branch must be a nonempty string or null")
                    added = service.application.add_repository(
                        repository.strip(),
                        None if target_branch is None else target_branch.strip(),
                    )
                    self._send_json(HTTPStatus.CREATED, added)
                except (ValueError, json.JSONDecodeError) as error:
                    self._error(HTTPStatus.BAD_REQUEST, error)
                except Exception as error:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, error)

            def do_DELETE(self) -> None:
                path = urlparse(self.path).path
                prefix = "/api/repositories/"
                if not path.startswith(prefix):
                    self._error(HTTPStatus.NOT_FOUND, "not found")
                    return
                try:
                    repository_id = int(path[len(prefix) :])
                    service.application.remove_repository(repository_id)
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                except ValueError as error:
                    self._error(HTTPStatus.BAD_REQUEST, error)
                except Exception as error:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, error)

        self._server = ThreadingHTTPServer((host, port), Handler)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @staticmethod
    def _sanitized_message(error: Exception) -> str:
        """Return a public-safe poll failure message without exception details."""
        return "poll failure details withheld"

    def state(self):
        state = dict(self.application.state())
        with self._poll_failure_lock:
            state["poll_failure"] = (
                None if self._poll_failure is None else dict(self._poll_failure)
            )
        return state

    def _record_poll_failure(self, error: Exception) -> None:
        _logger.error("Background poll failed: %s", error, exc_info=error)
        failure = {
            "type": type(error).__name__,
            "message": self._sanitized_message(error),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._poll_failure_lock:
            self._poll_failure = failure

    def _clear_poll_failure(self) -> None:
        with self._poll_failure_lock:
            self._poll_failure = None

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                self.application.poll_once()
            except Exception as error:
                self._record_poll_failure(error)
            else:
                self._clear_poll_failure()
            self._stop.wait(self.poll_seconds)

    def serve_forever(self) -> None:
        self._poll_thread = threading.Thread(
            target=self._poll,
            name="repogents-poller",
            daemon=True,
        )
        self._poll_thread.start()
        try:
            self._server.serve_forever(poll_interval=0.2)
        finally:
            self._stop.set()
            if self._poll_thread is not None:
                self._poll_thread.join(timeout=max(1.0, self.poll_seconds + 0.5))
            self._server.server_close()
            self.application.close()

    def shutdown(self) -> None:
        self._stop.set()
        self._server.shutdown()
