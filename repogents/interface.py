from __future__ import annotations

import json
import urllib.parse
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Protocol

_MAX_BODY = 1_000_000


class InterfaceActions(Protocol):
    def state(self) -> dict[str, object]: ...
    def configure_model(self, values: dict[str, object]) -> dict[str, object]: ...
    def model_catalog(self) -> dict[str, object]: ...

    def add_repository(self, identity: str, inputs: dict[str, object]) -> str: ...

    def reonboard(self, repository_id: str, inputs: dict[str, object]) -> str: ...

    def set_repository_enabled(self, repository_id: str, enabled: bool) -> None: ...

    def set_repository_autonomous(
        self, repository_id: str, autonomous: bool
    ) -> None: ...

    def remove_repository(self, repository_id: str) -> None: ...

    def repository_log(self, repository_id: str) -> dict[str, object]: ...
    def run_log(self, run_id: str) -> dict[str, object]: ...

    def activity_revision(self) -> int: ...

    def wait_for_activity_change(self, revision: int, timeout: float) -> int: ...

    def reorder_runs(self, run_ids: list[str]) -> None: ...

    def set_run_forced(self, run_id: str, forced: bool) -> None: ...

    def cancel(self, run_id: str) -> None: ...
    def retry(self, run_id: str) -> str: ...
    def restart(self, run_id: str) -> str: ...

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
            self._send(request, HTTPStatus.OK, body, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._send_json(request, HTTPStatus.OK, self.actions.state())
            return
        segments = [
            urllib.parse.unquote(segment)
            for segment in path.strip("/").split("/")
            if segment
        ]
        if segments == ["api", "model-configuration", "models"]:
            try:
                catalog = self.actions.model_catalog()
            except RuntimeError as error:
                self._send_json(
                    request,
                    HTTPStatus.CONFLICT,
                    {"error": str(error)},
                )
                return
            self._send_json(request, HTTPStatus.OK, catalog)
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "runs"]
            and segments[3] == "events"
        ):
            load = partial(self.actions.run_log, segments[2])
            try:
                revision, log = self._stable_log(load)
            except KeyError:
                self._send_json(
                    request,
                    HTTPStatus.NOT_FOUND,
                    {"error": "run not found"},
                )
                return
            except RuntimeError as error:
                self._send_json(
                    request,
                    HTTPStatus.CONFLICT,
                    {"error": str(error)},
                )
                return
            self._stream_log(request, load, revision, log)
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "runs"]
            and segments[3] == "logs"
        ):
            try:
                log = self.actions.run_log(segments[2])
            except KeyError:
                self._send_json(
                    request,
                    HTTPStatus.NOT_FOUND,
                    {"error": "run not found"},
                )
                return
            except RuntimeError as error:
                self._send_json(
                    request,
                    HTTPStatus.CONFLICT,
                    {"error": str(error)},
                )
                return
            self._send_json(request, HTTPStatus.OK, log)
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "repositories"]
            and segments[3] == "events"
        ):
            load = partial(self.actions.repository_log, segments[2])
            try:
                revision, log = self._stable_log(load)
            except KeyError:
                self._send_json(
                    request,
                    HTTPStatus.NOT_FOUND,
                    {"error": "repository not found"},
                )
                return
            except RuntimeError as error:
                self._send_json(
                    request,
                    HTTPStatus.CONFLICT,
                    {"error": str(error)},
                )
                return
            self._stream_log(request, load, revision, log)
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "repositories"]
            and segments[3] == "logs"
        ):
            try:
                log = self.actions.repository_log(segments[2])
            except KeyError:
                self._send_json(
                    request,
                    HTTPStatus.NOT_FOUND,
                    {"error": "repository not found"},
                )
                return
            except RuntimeError as error:
                self._send_json(
                    request,
                    HTTPStatus.CONFLICT,
                    {"error": str(error)},
                )
                return
            self._send_json(request, HTTPStatus.OK, log)
            return
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
        try:
            payload = self._read_json(request)
            path = urllib.parse.urlsplit(request.path).path
            segments = [
                urllib.parse.unquote(segment)
                for segment in path.strip("/").split("/")
                if segment
            ]
            if segments == ["api", "model-configuration"]:
                configuration = self.actions.configure_model(payload)
                self._send_json(request, HTTPStatus.OK, configuration)
                return
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
                and segments[:2] == ["api", "repositories"]
                and segments[3] == "enabled"
            ):
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                self.actions.set_repository_enabled(segments[2], enabled)
                self._send_json(
                    request,
                    HTTPStatus.OK,
                    {"ok": True, "enabled": enabled, "paused": not enabled},
                )
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "repositories"]
                and segments[3] == "autonomous"
            ):
                autonomous = payload.get("autonomous")
                if not isinstance(autonomous, bool):
                    raise ValueError("autonomous must be a boolean")
                self.actions.set_repository_autonomous(segments[2], autonomous)
                self._send_json(
                    request,
                    HTTPStatus.OK,
                    {"ok": True, "autonomous": autonomous},
                )
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "repositories"]
                and segments[3] == "remove"
            ):
                self.actions.remove_repository(segments[2])
                self._send_json(request, HTTPStatus.OK, {"ok": True})
                return
            if segments == ["api", "runs", "priority"]:
                run_ids = payload.get("run_ids")
                if not isinstance(run_ids, list) or not all(
                    isinstance(run_id, str) for run_id in run_ids
                ):
                    raise ValueError("run_ids must be a list of strings")
                self.actions.reorder_runs(run_ids)
                self._send_json(request, HTTPStatus.OK, {"ok": True})
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "runs"]
                and segments[3] == "force"
            ):
                forced = payload.get("forced")
                if not isinstance(forced, bool):
                    raise ValueError("forced must be a boolean")
                self.actions.set_run_forced(segments[2], forced)
                self._send_json(
                    request,
                    HTTPStatus.OK,
                    {"ok": True, "forced": forced},
                )
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
                and segments[:2] == ["api", "runs"]
                and segments[3] == "retry"
            ):
                state = self.actions.retry(segments[2])
                self._send_json(
                    request,
                    HTTPStatus.OK,
                    {"ok": True, "state": state},
                )
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "runs"]
                and segments[3] == "restart"
            ):
                replacement_run_id = self.actions.restart(segments[2])
                self._send_json(
                    request,
                    HTTPStatus.OK,
                    {"ok": True, "run_id": replacement_run_id},
                )
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

    def _stable_log(
        self,
        load: Callable[[], dict[str, object]],
    ) -> tuple[int, dict[str, object]]:
        while True:
            before = self.actions.activity_revision()
            snapshot = load()
            after = self.actions.activity_revision()
            if before == after:
                return after, snapshot

    def _stream_log(
        self,
        request: BaseHTTPRequestHandler,
        load: Callable[[], dict[str, object]],
        revision: int,
        snapshot: dict[str, object],
    ) -> None:
        request.send_response(int(HTTPStatus.OK))
        request.send_header("Content-Type", "text/event-stream; charset=utf-8")
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Accel-Buffering", "no")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        )
        request.send_header("Referrer-Policy", "no-referrer")
        request.end_headers()
        previous: bytes | None = None
        try:
            while True:
                payload = json.dumps(
                    snapshot,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                if payload != previous:
                    frame = (
                        f"id: {revision}\nevent: activity\ndata: ".encode()
                        + payload
                        + b"\n\n"
                    )
                    request.wfile.write(frame)
                    request.wfile.flush()
                    previous = payload
                next_revision = self.actions.wait_for_activity_change(
                    revision,
                    15.0,
                )
                if next_revision == revision:
                    request.wfile.write(b": keepalive\n\n")
                    request.wfile.flush()
                    continue
                revision, snapshot = self._stable_log(load)
        except (BrokenPipeError, ConnectionResetError, KeyError, OSError, RuntimeError):
            return

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
        request.end_headers()
        request.wfile.write(body)


_DASHBOARD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repogents</title>
<style>
:root {
  color-scheme: light dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --page: #f4f6f8;
  --surface: #ffffff;
  --surface-subtle: #f8fafc;
  --text: #172033;
  --muted: #667085;
  --line: #dfe4ea;
  --accent: #3157d5;
  --accent-soft: #edf1ff;
  --success: #18794e;
  --success-soft: #e9f8f0;
  --warning: #9a6700;
  --warning-soft: #fff7d6;
  --danger: #b42318;
  --danger-soft: #fff0ee;
  --shadow: 0 12px 36px rgba(23, 32, 51, .08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #10131a;
    --surface: #171b24;
    --surface-subtle: #1d222d;
    --text: #eef2f8;
    --muted: #aab3c2;
    --line: #303744;
    --accent: #8da4ff;
    --accent-soft: #222d55;
    --success: #76d6aa;
    --success-soft: #17392b;
    --warning: #efc46d;
    --warning-soft: #3d3015;
    --danger: #ff9b91;
    --danger-soft: #48211f;
    --shadow: 0 14px 40px rgba(0, 0, 0, .28);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text); line-height: 1.45; }
button, input, textarea { font: inherit; }
button { cursor: pointer; }
button:disabled { cursor: not-allowed; opacity: .5; }
a { color: var(--accent); overflow-wrap: anywhere; }
code, pre { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; }
.shell { width: min(94rem, 100%); margin: 0 auto; padding: 1.25rem; }
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 1rem;
}
.brand { display: flex; align-items: center; gap: .8rem; }
.brand-mark {
  display: grid;
  width: 2.4rem;
  height: 2.4rem;
  place-items: center;
  border-radius: .7rem;
  background: var(--accent);
  color: #fff;
  font-weight: 800;
}
h1, h2, h3, h4, p { margin-top: 0; }
h1 { margin-bottom: .1rem; font-size: 1.45rem; line-height: 1.1; }
h2 { margin-bottom: .25rem; font-size: 1.12rem; }
h3 { margin-bottom: .5rem; font-size: 1rem; }
h4 { margin-bottom: .4rem; }
.toolbar { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
.connection { display: inline-flex; align-items: center; gap: .4rem; color: var(--muted); font-size: .88rem; }
.connection::before {
  content: "";
  width: .5rem;
  height: .5rem;
  border-radius: 50%;
  background: var(--success);
}
.panel {
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: var(--surface);
  box-shadow: var(--shadow);
  overflow: hidden;
  margin-bottom: 1rem;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.15rem;
  border-bottom: 1px solid var(--line);
}
.panel-header p { margin-bottom: 0; }
.panel-body { padding: 1rem 1.15rem; }
.settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: .85rem 1rem;
}
.settings-field { display: grid; gap: .35rem; color: var(--muted); font-size: .85rem; font-weight: 650; }
.settings-field.wide { grid-column: 1 / -1; }
.settings-field input {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: .6rem;
  padding: .68rem .75rem;
  background: var(--surface);
  color: var(--text);
  font-weight: 400;
}
.settings-actions { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; margin-top: 1rem; }
.checkbox { display: inline-flex; align-items: center; gap: .45rem; color: var(--muted); }
.settings-warning { margin: .8rem 0 0; color: var(--warning); font-size: .85rem; }
#configuration-result { color: var(--success); }
dialog {
  width: min(48rem, calc(100% - 2rem));
  max-height: calc(100vh - 2rem);
  padding: 0;
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: var(--surface);
  color: var(--text);
  box-shadow: 0 24px 80px rgba(0, 0, 0, .3);
}
dialog::backdrop { background: rgba(10, 15, 25, .62); backdrop-filter: blur(2px); }
.dialog-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.15rem;
  border-bottom: 1px solid var(--line);
}
.dialog-header h2 { margin-bottom: .2rem; }
.dialog-header p { margin-bottom: 0; }
.dialog-body { padding: 1rem 1.15rem 1.15rem; overflow: auto; }
.execution-status { margin: 0 0 .85rem; font-weight: 650; }
.execution-status.ready { color: var(--success); }
.catalog-row { display: flex; align-items: center; gap: .6rem; margin: .7rem 0 0; }
.catalog-row .muted { flex: 1; margin: 0; }
#configuration-result.error { color: var(--danger); }
.inventory-grid { display: grid; grid-template-columns: minmax(18rem, 25rem) minmax(0, 1fr); min-height: 35rem; }
.inventory-column { border-right: 1px solid var(--line); padding: 1rem; background: var(--surface-subtle); }
.detail-column { min-width: 0; padding: 1.2rem; }
.add-form {
  display: grid;
  grid-template-columns: minmax(14rem, 1fr) auto;
  gap: .65rem;
  margin-bottom: 1rem;
}
.add-form input, .add-form textarea {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: .6rem;
  padding: .68rem .75rem;
  background: var(--surface);
  color: var(--text);
}
.add-form details { grid-column: 1 / -1; }
.add-form textarea { min-height: 6rem; resize: vertical; }
.button {
  border: 1px solid var(--line);
  border-radius: .58rem;
  padding: .58rem .8rem;
  background: var(--surface);
  color: var(--text);
  font-weight: 650;
}
.button:hover:not(:disabled) { border-color: var(--accent); }
.button.primary { border-color: var(--accent); background: var(--accent); color: #fff; }
.button.danger { color: var(--danger); }
.button.small { padding: .4rem .58rem; font-size: .82rem; }
.repo-card {
  border: 1px solid var(--line);
  border-radius: .8rem;
  background: var(--surface);
  padding: .85rem;
  margin-bottom: .65rem;
  cursor: pointer;
  transition: border-color .15s, box-shadow .15s, transform .15s;
}
.repo-card:hover, .repo-card:focus-visible { border-color: var(--accent); outline: none; transform: translateY(-1px); }
.repo-card.selected { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.row { display: flex; align-items: center; justify-content: space-between; gap: .75rem; flex-wrap: wrap; }
.repo-name { font-weight: 750; }
.repo-meta { margin: .55rem 0; color: var(--muted); font-size: .86rem; }
.actions { display: flex; gap: .45rem; flex-wrap: wrap; }
.badge {
  display: inline-flex;
  align-items: center;
  gap: .3rem;
  border-radius: 999px;
  padding: .2rem .55rem;
  background: var(--surface-subtle);
  border: 1px solid var(--line);
  color: var(--muted);
  font-size: .76rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .03em;
}
.badge.success { color: var(--success); background: var(--success-soft); border-color: transparent; }
.badge.warning { color: var(--warning); background: var(--warning-soft); border-color: transparent; }
.badge.danger { color: var(--danger); background: var(--danger-soft); border-color: transparent; }
.status-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .65rem; margin: 1rem 0; }
.metric { border: 1px solid var(--line); border-radius: .7rem; padding: .7rem; background: var(--surface-subtle); }
.metric span { display: block; color: var(--muted); font-size: .74rem; text-transform: uppercase; letter-spacing: .04em; }
.metric strong { display: block; margin-top: .18rem; overflow-wrap: anywhere; }
.split { display: grid; grid-template-columns: minmax(15rem, .8fr) minmax(0, 1.2fr); gap: 1rem; }
.subpanel { border: 1px solid var(--line); border-radius: .8rem; padding: .9rem; min-width: 0; }
.team-member { border-top: 1px solid var(--line); padding: .65rem 0; }
.team-member:first-of-type { border-top: 0; }
.team-member summary { cursor: pointer; font-weight: 700; }
.team-member p { margin: .6rem 0; }
.prompt {
  max-height: 18rem;
  overflow: auto;
  padding: .75rem;
  border-radius: .55rem;
  background: var(--page);
  border: 1px solid var(--line);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.workflow-preview { margin-top: 1rem; }
.workflow-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: .8rem;
  margin-bottom: .7rem;
}
.workflow-toolbar label { display: flex; align-items: center; gap: .5rem; }
.workflow-grid {
  position: relative;
  min-height: 24rem;
  height: min(32rem, 62vh);
  max-height: 32rem;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: .7rem;
  background:
    linear-gradient(90deg, var(--line) 1px, transparent 1px),
    linear-gradient(var(--line) 1px, transparent 1px),
    var(--page);
  background-size: 2rem 2rem;
  cursor: grab;
  isolation: isolate;
  touch-action: none;
}
.workflow-grid.dragging { cursor: grabbing; }
.workflow-scene {
  position: absolute;
  inset: 0 auto auto 0;
  transform-origin: 0 0;
  will-change: transform;
}
.workflow-canvas { position: relative; }
.workflow-canvas svg {
  position: absolute;
  inset: 0;
  overflow: visible;
  pointer-events: none;
}
.workflow-viewport-controls {
  position: absolute;
  z-index: 20;
  top: .6rem;
  right: .6rem;
  display: flex;
  gap: .3rem;
  padding: .3rem;
  border: 1px solid var(--line);
  border-radius: .6rem;
  background: color-mix(in srgb, var(--surface) 92%, transparent);
  box-shadow: var(--shadow);
}
.workflow-viewport-controls .button {
  min-width: 2.35rem;
  padding: .4rem .55rem;
}
.workflow-edge {
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 2;
  vector-effect: non-scaling-stroke;
}
.workflow-edge-dependency { stroke: var(--muted); opacity: .72; }
.workflow-edge-lifecycle {
  stroke: var(--accent);
  stroke-dasharray: 7 5;
  opacity: .62;
}
.workflow-lifecycle-label {
  fill: var(--text);
  font-size: .68rem;
  font-weight: 650;
  paint-order: stroke;
  stroke: var(--page);
  stroke-linejoin: round;
  stroke-width: 5px;
}
.workflow-node {
  position: absolute;
  z-index: 2;
  width: 11rem;
  height: 4.8rem;
  padding: .55rem .6rem;
  overflow: hidden;
  border: 2px solid var(--line);
  border-radius: .7rem;
  background: var(--surface);
  color: var(--text);
  text-align: left;
  box-shadow: var(--shadow);
}
.workflow-node:hover, .workflow-node:focus-visible {
  z-index: 3;
  border-color: var(--accent);
  outline: 3px solid var(--accent-soft);
}
.workflow-node strong {
  display: -webkit-box;
  overflow: hidden;
  line-height: 1.15;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.workflow-node small {
  display: block;
  margin-top: .25rem;
  overflow: hidden;
  color: var(--muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.workflow-node.running { border-color: var(--accent); }
.workflow-node.succeeded { border-color: var(--success); }
.workflow-node.failed, .workflow-node.canceled { border-color: var(--danger); }
.workflow-node.ready { border-style: dashed; border-color: var(--accent); }
.workflow-node.blocked { border-style: double; border-color: var(--danger); }
.workflow-kind-deterministic { border-radius: .2rem; }
.workflow-kind-controller {
  border-style: double;
  background: var(--surface-subtle);
}
.workflow-boundary-coordinator { border-left-width: .5rem; }
.workflow-boundary-independent-verifier { border-right-width: .5rem; }
.workflow-boundary-controller-owned {
  box-shadow: inset 0 0 0 2px var(--accent-soft);
}
.workflow-boundary-system-origin {
  border-width: 3px;
  background: var(--accent-soft);
}
.workflow-boundary-system-terminal {
  border-style: double;
  border-color: var(--muted);
}
.workflow-edge-key::before {
  content: "";
  display: inline-block;
  width: 1.8rem;
  margin-right: .4rem;
  border-top: 2px solid var(--muted);
  vertical-align: middle;
}
.workflow-edge-key.lifecycle::before {
  border-color: var(--accent);
  border-top-style: dashed;
}
.workflow-scroll-hint {
  margin: .25rem 0 .5rem;
  font-size: .78rem;
  color: var(--muted);
}
.workflow-kind-agent { border-radius: .7rem; }
.controller-boundary { border: 2px double var(--accent); }
.workflow-legend {
  display: flex;
  flex-wrap: wrap;
  gap: .45rem;
  margin: .5rem 0;
}
.workflow-legend-item {
  padding: .25rem .45rem;
  background: var(--surface-subtle);
}
.workflow-lifecycle-table { margin-top: .8rem; }
.workflow-node-details { margin-top: .7rem; }
.workflow-table-wrap { overflow-x: auto; margin-top: .8rem; }
.workflow-table { width: 100%; border-collapse: collapse; font-size: .82rem; }
.workflow-table th, .workflow-table td {
  padding: .5rem;
  border: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
.workflow-table th { background: var(--surface-subtle); }
.log {
  height: 25rem;
  overflow: auto;
  margin: .7rem 0 0;
  padding: .85rem;
  border: 1px solid #273043;
  border-radius: .65rem;
  background: #0d1117;
  color: #d9e1eb;
  font-size: .8rem;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.empty { display: grid; place-items: center; min-height: 25rem; text-align: center; color: var(--muted); }
.muted { color: var(--muted); }
.error { color: var(--danger); white-space: pre-wrap; }
.blocking-error {
  margin: .8rem 0;
  border: 1px solid var(--danger);
  border-radius: .65rem;
  background: var(--danger-soft);
}
.blocking-error summary { padding: .7rem .8rem; color: var(--danger); font-weight: 700; }
.blocking-error pre {
  max-height: 18rem;
  overflow: auto;
  margin: 0;
  padding: .8rem;
  border-top: 1px solid var(--danger);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: .78rem;
}
#error:not(:empty) {
  position: sticky;
  top: .6rem;
  z-index: 5;
  padding: .8rem 1rem;
  border: 1px solid var(--danger);
  border-radius: .7rem;
  background: var(--danger-soft);
  box-shadow: var(--shadow);
}
.card { border: 1px solid var(--line); border-radius: .75rem; padding: .9rem; margin: .7rem 0; }
.run-card {
  display: block;
  width: 100%;
  text-align: left;
  color: var(--text);
  background: var(--surface);
  font: inherit;
  cursor: grab;
  transition: border-color .15s, box-shadow .15s, transform .15s;
}
.run-card:hover, .run-card:focus-visible {
  border-color: var(--accent);
  outline: none;
  transform: translateY(-1px);
}
.run-card.selected { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.run-card.dragging { opacity: .45; cursor: grabbing; }
.run-card-line { display: block; margin-top: .55rem; }
.drag-hint { color: var(--muted); font-size: .76rem; }
.issue-log-details { margin-bottom: .75rem; }
.unread { border-inline-start: .35rem solid var(--accent); }
.compact-list { margin: .5rem 0 0; padding-left: 1.2rem; }
.secondary-sections { display: grid; grid-template-columns: 1.2fr .8fr; gap: 1rem; }
details > summary { cursor: pointer; }
@media (max-width: 900px) {
  .inventory-grid, .split, .secondary-sections { grid-template-columns: 1fr; }
  .inventory-column { border-right: 0; border-bottom: 1px solid var(--line); }
  .status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .settings-grid { grid-template-columns: 1fr; }
  .settings-field.wide { grid-column: auto; }
  .workflow-toolbar { align-items: flex-start; flex-direction: column; }
  .workflow-toolbar label, .workflow-toolbar select { width: 100%; }
  .workflow-grid { max-height: 28rem; }
}
@media (max-width: 560px) {
  .shell { padding: .7rem; }
  .topbar { align-items: flex-start; }
  .add-form { grid-template-columns: 1fr; }
  .add-form button { width: 100%; }
}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">R</div>
      <div><h1>Repogents</h1><div class="muted">Repository operations console</div></div>
    </div>
    <div class="toolbar">
      <span class="connection" id="connection">Connected</span>
      <button class="button" id="open-model-configuration" type="button">Model settings <span class="badge warning" id="model-configuration-status">Needs setup</span></button>
      <button class="button" id="poll" type="button">Poll now</button>
    </div>
  </header>
  <p id="error" class="error" role="alert"></p>
  <main>
    <section class="panel" aria-labelledby="inventory-title">
      <div class="panel-header">
        <div><h2 id="inventory-title">Repositories</h2><p class="muted">Manage inventory and inspect current work.</p></div>
        <span class="badge" id="inventory-count">0 repositories</span>
      </div>
      <div class="inventory-grid">
        <div class="inventory-column">
          <form id="add" class="add-form" aria-label="Add repository">
            <input name="repository" required placeholder="GitHub URL or owner/repository" aria-label="GitHub URL or owner and repository">
            <button class="button primary" type="submit">Add repository</button>
            <details>
              <summary>Repository inputs</summary>
              <p class="muted">Optional JSON for host paths, services, secrets, provisioning, or validation overrides.</p>
              <textarea name="inputs" aria-label="Repository inputs JSON">{}</textarea>
            </details>
          </form>
          <div id="repository-list" aria-live="polite"></div>
        </div>
        <article id="repository-detail" class="detail-column" aria-live="polite">
          <div class="empty"><div><h2>Select a repository</h2><p>Choose an inventory item to view status, team, and live activity.</p></div></div>
        </article>
      </div>
    </section>
    <div class="secondary-sections">
      <section class="panel">
        <div class="panel-header"><div><h2>Agent-ready issues</h2><p class="muted">Issues shown here belong only to the selected repository.</p></div></div>
        <div class="panel-body" id="ready-issues"></div>
        <div class="panel-header"><div><h2>Runs</h2><p class="muted">Active issue runs for the selected repository.</p></div></div>
        <div class="panel-body" id="runs"></div>
        <div class="panel-header"><div><h2>Run history</h2><p class="muted">Blocked and finished runs remain available with their durable evidence and recovery controls.</p></div></div>
        <div class="panel-body" id="run-history"></div>
      </section>
      <section class="panel">
        <div class="panel-header"><div><h2>All active issues</h2><p class="muted">Drag active issue buttons to set execution priority.</p></div></div>
        <div class="panel-body" id="all-active-runs"></div>
      </section>
      <section class="panel" aria-labelledby="issue-log-title">
        <div class="panel-header">
          <div><h2 id="issue-log-title">Issue Log</h2><p id="issue-log-identity" class="muted">Select an issue to view its live log.</p></div>
          <span class="badge" id="issue-log-state">Idle</span>
        </div>
        <div class="panel-body">
          <div id="issue-log-details" class="issue-log-details"><p class="muted">No issue selected.</p></div>
          <pre id="issue-live-log" class="log" tabindex="0" aria-label="Live scrolling issue log" aria-live="polite">Select an issue to view its activity.</pre>
        </div>
      </section>
    </div>
  </main>
</div>
<dialog id="model-configuration-dialog" aria-labelledby="model-configuration-title">
  <div class="dialog-header">
    <div>
      <h2 id="model-configuration-title">Model provider</h2>
      <p class="muted">Configure the endpoint, write-only credential, and repository-agent models.</p>
    </div>
    <button class="button small" id="close-model-configuration" type="button">Close</button>
  </div>
  <div class="dialog-body">
    <p id="model-execution-status" class="execution-status">Execution unavailable: model settings are incomplete.</p>
    <form id="model-configuration-form">
      <div class="settings-grid">
        <label class="settings-field wide"><span>API endpoint</span><input name="api_endpoint" type="url" placeholder="Provider default, or https://models.example/v1" autocomplete="url" spellcheck="false"></label>
        <label class="settings-field wide"><span>API key</span><input name="api_key" type="password" placeholder="Leave blank to keep the configured key" autocomplete="new-password" autocapitalize="none" spellcheck="false"></label>
        <label class="settings-field"><span>Default model</span><input name="default_model" list="model-catalog" required placeholder="Select or enter a model" autocomplete="off" spellcheck="false"></label>
        <label class="settings-field"><span>Lead model</span><input name="lead_model" list="model-catalog" placeholder="Inherit default model" autocomplete="off" spellcheck="false"></label>
        <label class="settings-field"><span>Implementer model</span><input name="implementer_model" list="model-catalog" placeholder="Inherit default model" autocomplete="off" spellcheck="false"></label>
        <label class="settings-field"><span>Verifier model</span><input name="verifier_model" list="model-catalog" placeholder="Inherit default model" autocomplete="off" spellcheck="false"></label>
        <datalist id="model-catalog"></datalist>
      </div>
      <div class="catalog-row">
        <p id="model-catalog-status" class="muted">Open settings to load models from the configured endpoint.</p>
        <button class="button small" id="reload-model-catalog" type="button">Reload models</button>
      </div>
      <div class="settings-actions">
        <button class="button primary" type="submit">Save model configuration</button>
        <label class="checkbox"><input name="clear_api_key" type="checkbox"> Remove stored API key</label>
        <span id="api-key-status" class="muted">No managed API key configured</span>
        <span id="configuration-result" role="status"></span>
      </div>
    </form>
    <p class="muted">Blank role fields inherit the default model. Existing repository team versions keep their stored model selectors; Re-onboard a repository to apply model changes. Endpoint and key changes apply to subsequent invocations immediately.</p>
    <p class="settings-warning">The API key is write-only after saving, but this trusted-LAN interface uses HTTP. Do not enter a key over an untrusted network.</p>
  </div>
</dialog>
<script>
let currentState = {repositories:[], runs:[], run_history:[], model_configuration:{}};
let selectedRepositoryId = null;
let runActivityStream = null;
let runActivityStreamRunId = null;
let selectedRunId = null;
let draggedRunId = null;
let selectedWorkflowGeneration = null;
let selectedWorkflowNode = null;
let suppressRunClick = false;
let modelCatalog = {available:false, reason:'Not loaded', models:[]};
const displayInputs = new Map();
const WORKFLOW_NODE_WIDTH = 176;
const WORKFLOW_NODE_HEIGHT = 77;
const WORKFLOW_COLUMN_GAP = 112;
const WORKFLOW_ROW_GAP = 36;
const WORKFLOW_PADDING_X = 56;
const WORKFLOW_PADDING_Y = 64;
const WORKFLOW_EDGE_CLEARANCE = 14;
const WORKFLOW_MIN_ZOOM = .08;
const WORKFLOW_MAX_ZOOM = 2;
const workflowViewportStates = new Map();
let workflowViewportResizeObserver = null;
const error = document.querySelector('#error');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const boundedMessage = value => {
  const message = String(value || 'Unknown error').split('\n', 1)[0];
  return message.length > 400 ? `${message.slice(0, 400)}…` : message;
};
const githubLink = (url, label) => /^https:\/\/github\.com\//.test(url || '') ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(label)}</a>` : esc(label);
const short = value => value ? esc(String(value).slice(0, 12)) : '—';
const date = value => value ? new Date(value).toLocaleString() : 'Never';
const actionableRun = r => ['queued','implementing','validating','publishing','resolving_feedback'].includes(r.state);
const allRuns = () => [...(currentState.runs || []), ...(currentState.run_history || [])];
const deltaCounts = c => c?.mode ? ` · ${esc(c.mode)}: ${esc(c.new_count ?? 0)} new, ${esc(c.resolved_count ?? 0)} resolved, ${esc(c.unchanged_count ?? 0)} unchanged` : '';
const validationBaseline = b => `<li><code>${esc((b.command || []).join(' '))}</code> — ${esc(b.mode)} baseline, exit ${esc(b.exit_status)}, ${esc((b.findings || []).length)} findings for <code>${short(b.base_sha)}</code>; log <code>${esc(b.log_path)}</code></li>`;
const validationResult = v => `<li><strong>${esc(v.verdict || 'unknown')}</strong> <code>${esc((v.command || []).join(' '))}</code> — exit ${esc(v.exit_status)}${deltaCounts(v.comparison)} for <code>${short(v.commit_sha)}</code>; log <code>${esc(v.log_path)}</code></li>`;
const evidence = r => `<details><summary>Durable evidence</summary><p>Base <code>${short(r.base_sha)}</code> · Validated <code>${short(r.validated_sha)}</code></p><p>Sandbox v${esc(r.sandbox_version)} <code>${esc(r.sandbox_version_id)}</code><br>Team v${esc(r.team_version)} <code>${esc(r.team_version_id)}</code></p><h3>Assignments</h3><ul>${(r.assignments || []).map(a => `<li>${esc(a.stable_key)} (${esc(a.role)}): ${esc(a.reasoning)}</li>`).join('') || '<li>None recorded</li>'}</ul><h3>Validation baselines</h3><ul>${(r.validation_baselines || []).map(validationBaseline).join('') || '<li>None recorded</li>'}</ul><h3>Candidate validation</h3><ul>${(r.validation_results || []).map(validationResult).join('') || '<li>None recorded</li>'}</ul></details>`;
const acceptanceArtifact = a => {
  const artifact = String(a.url || '').startsWith('/api/acceptance-artifacts/')
    ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.description || a.kind)}</a> <code>${short(a.sha256)}</code>`
    : `${esc(a.description || a.kind)} <code>${short(a.sha256)}</code>`;
  const review = a.metadata?.pixel_review;
  return review
    ? `${artifact}<br><span class="muted">Pixel review: ${esc(review.verdict)} · evidence #${esc(review.evidence_sequence)} · ${esc(review.observed)}</span>`
    : artifact;
};
const acceptanceEvidence = a => {
  if (!a) return '<details><summary>Issue acceptance</summary><p>Not yet verified for the current commit.</p></details>';
  const claims = (a.claims || []).map(c => `<li><strong>${esc(c.result || 'planned')}</strong> ${esc(c.claim)}<br><span class="muted">${esc(c.observed || c.expected || '')}</span></li>`).join('') || '<li>No claims recorded.</li>';
  const observations = (a.evidence || []).map(o => `<li>#${esc(o.sequence)} <code>${esc(JSON.stringify(o.action || {}))}</code> — ${esc(JSON.stringify(o.result ?? ''))}</li>`).join('') || '<li>No observations recorded.</li>';
  const scope = (a.scope || []).map(s => `<li><code>${esc(s.path)}</code> — ${esc(s.result)}: ${esc(s.necessity)} (${esc((s.claim_keys || []).join(', '))})</li>`).join('') || '<li>No scope mapping recorded.</li>';
  const artifacts = (a.artifacts || []).map(item => `<li>${acceptanceArtifact(item)}</li>`).join('') || '<li>No artifacts recorded.</li>';
  const limitations = (a.limitations || []).map(item => `<li>${esc(item)}</li>`).join('') || '<li>None recorded.</li>';
  return `<details><summary>Issue acceptance — ${esc(a.state)}</summary><p>Commit <code>${short(a.commit_sha)}</code><br>${esc(a.summary)}</p><h3>Claims</h3><ul>${claims}</ul><h3>Controller observations</h3><ul>${observations}</ul><h3>Changed-file scope</h3><ul>${scope}</ul><h3>Visual decision</h3><p>${a.screenshot_decision?.required ? 'Screenshots required' : 'Screenshots not required'}: ${esc(a.screenshot_decision?.reason || '')}</p><ul>${artifacts}</ul><h3>Limitations</h3><ul>${limitations}</ul></details>`;
};
const specVerdictBadge = verdict => {
  if (verdict === 'approved') return '<span class="badge success">Approved</span>';
  if (verdict === 'rejected') return '<span class="badge danger">Rejected</span>';
  if (verdict === 'blocked') return '<span class="badge warning">Blocked</span>';
  return '<span class="badge">Pending review</span>';
};
const specCriterionResultBadge = result => {
  if (result === 'pass') return '<span class="badge success">Pass</span>';
  if (result === 'fail') return '<span class="badge danger">Fail</span>';
  if (result === 'blocked') return '<span class="badge warning">Blocked</span>';
  return '<span class="badge warning">Pending</span>';
};
const specEvidence = c => {
  if (!Array.isArray(c.evidence) || c.evidence.length === 0) return '';
  const items = c.evidence.map(e => `<li>${esc(e.claim_key)}: ${esc(e.result || 'unknown')}${e.observed ? ' — ' + esc(e.observed) : ''}
    ${Array.isArray(e.evidence_refs) && e.evidence_refs.length > 0 ? '<br>Evidence: ' + e.evidence_refs.map(ref => '#' + esc(String(ref))).join(', ') : ''}</li>`).join('');
  return `<details><summary>Evidence</summary><ul>${items}</ul></details>`;
};
const specContexts = contexts => {
  if (!Array.isArray(contexts) || contexts.length === 0) return '';
  const items = contexts.map(c => `<li>Context <code>${esc((c.context_sha256 || '').slice(0, 16))}</code> → Spec rev ${esc(c.specification_revision_id || 'unknown')} · Reconciled: ${esc(c.reconciled_at || '')}</li>`).join('');
  return `<details><summary>Reconciled contexts (${contexts.length})</summary><ul>${items}</ul></details>`;
};
const specVerification = v => {
  const criterionLinks = (v.criterion_keys || []).map(k => `<code>${esc(k)}</code>`).join(', ');
  return `<li>${esc(v.scenario || '')} · Criteria: ${criterionLinks}</li>`;
};
const specCriterion = c => `<li>${specCriterionResultBadge(c.result)} ${esc(c.requirement || '')}
    <br><span class="muted">Expected: ${esc(c.expected || '')}</span>
    ${c.claim_keys && c.claim_keys.length > 0 ? `<br>Claims: ${(c.claim_keys || []).map(k => esc(k)).join(', ')}` : ''}
    ${specEvidence(c)}</li>`;
const specItem = item => {
  const criteria = (item.acceptance_criteria || []).map(specCriterion).join('') || '<li>No criteria.</li>';
  const verifications = (item.verification || []).map(specVerification).join('') || '<li>None.</li>';
  return `<details class="spec-item"><summary>${esc(item.title || item.key || 'Untitled')} <code>${esc(item.key || '')}</code></summary>
    <p>${esc(item.objective || '')}</p>
    <h4>Acceptance criteria</h4><ul>${criteria}</ul>
    <h4>Verification</h4><ul>${verifications}</ul></details>`;
};
const specReviewEntry = (rev, idx) => {
  const reviews = (rev.reviews || []);
  const reviewDetails = reviews.map(r => `<details><summary>${specVerdictBadge(r.verdict)} ${esc(r.summary || '')} · ${esc(r.created_at || '')}</summary>
    ${r.findings && r.findings.length > 0 ? `<ul>${r.findings.map(f => `<li>${esc(f.severity || '')}: ${esc(f.category || '')} / ${esc(f.key || '')} — ${esc(f.summary || '')}<br><span class="muted">Items: ${esc((f.item_keys || []).join(', '))}</span></li>`).join('')}</ul>` : ''}
    ${r.blocker ? `<p class="error">Blocker: ${esc(r.blocker)}</p>` : ''}
    <p class="muted">${esc(r.reviewer_model || '')} · rubric v${esc(r.rubric_version)}</p>
    </details>`).join('') || '<p class="muted">No reviews yet.</p>';
  const contextsHtml = specContexts(rev.contexts || []);
  const itemsHtml = (rev.items || []).map(specItem).join('') || '<p class="muted">No items.</p>';
  return `<li><strong>Revision ${esc(rev.revision)}</strong> (v${esc(rev.issue_version_id)}) — ${esc(rev.created_at || '')}
    <details><summary>Details</summary><pre class="prompt">${esc(rev.reason || 'No reason')}</pre>
    <p class="muted">Author: ${esc(rev.author_member_id || 'unknown')} · SHA: <code>${esc((rev.content_sha256 || '').slice(0, 16))}</code></p>
    ${reviewDetails}${contextsHtml}
    <h4>Specification items</h4>${itemsHtml}
    </details></li>`;
};
const issueSpecification = run => {
  const spec = run.specification;
  const history = run.specification_revision_history || [];
  if (!spec && history.length === 0) {
    return '<details><summary>Issue specification</summary><p class="muted">No specification persisted for this run.</p></details>';
  }

  // Active spec section
  let activeSection = '';
  if (spec) {
    const reviewStatus = spec.review
      ? specVerdictBadge(spec.review.verdict) + ' ' + esc(spec.review.summary || '')
      : '<span class="badge">Pending review</span>';
    const readinessBadge = spec.implementation_ready
      ? '<span class="badge success">Implementation ready</span>'
      : '<span class="badge warning">Not ready for implementation</span>';
    const itemsHtml = (spec.items || []).map(specItem).join('') || '<p class="muted">No items in this revision.</p>';
    const contextsHtml = specContexts(spec.contexts || []);
    activeSection = `<div class="row">
      <div><h3>Active specification</h3><p>Revision ${esc(spec.revision)} for issue v${esc(spec.issue_version_id || '')} · ${esc(spec.created_at || '')}
      <br>${reviewStatus} · ${readinessBadge}</p></div>
    </div>
    ${contextsHtml}
    ${itemsHtml}`;
  } else {
    activeSection = '<p class="muted">No specification for the current issue version.</p>';
  }

  // Revision history section
  const historyHtml = history.length
    ? `<details><summary>Revision history (${history.length} revision${history.length === 1 ? '' : 's'})</summary>
      <ol>${history.map((rev, idx) => specReviewEntry(rev, idx)).join('')}</ol></details>`
    : '';

  return `<details open><summary>Issue specification</summary>
    ${activeSection}
    ${historyHtml}
  </details>`;
};
const stateBadge = r => {
  if (!r.enabled) return '<span class="badge warning">Paused</span>';
  if (r.active) return '<span class="badge success">Active</span>';
  return '<span class="badge">Idle</span>';
};
const teamMember = m => `<details class="team-member"><summary>${esc(m.stable_key)} <span class="badge">${esc(m.role)}</span></summary><p>${esc(m.responsibilities)}</p><p class="muted">${esc(m.runtime)} · ${esc(m.model)}</p><h4>Role prompt</h4><pre class="prompt">${esc(m.instructions)}</pre></details>`;
const retainedInputs = r => esc(JSON.stringify(r.display_inputs, null, 2));
const workflowNumericColumn = node => {
  const column = Number(node.column);
  return Number.isFinite(column) ? column : 0;
};
const workflowNumericRow = (node, fallback = 0) => {
  const row = Number(node.row);
  return Number.isFinite(row) ? row : fallback;
};
function workflowLayeredLayout(nodes, dependencyEdges) {
  const stableIndex = new Map(
    nodes.map((node, index) => [String(node.stable_key), index])
  );
  const groups = new Map();
  for (const node of nodes) {
    const column = workflowNumericColumn(node);
    if (!groups.has(column)) groups.set(column, []);
    groups.get(column).push(node);
  }
  const columns = [...groups.keys()].sort((first, second) => first - second);
  for (const group of groups.values()) {
    group.sort((first, second) => (
      workflowNumericRow(first, stableIndex.get(String(first.stable_key)))
      - workflowNumericRow(second, stableIndex.get(String(second.stable_key)))
      || stableIndex.get(String(first.stable_key))
      - stableIndex.get(String(second.stable_key))
    ));
  }
  const incoming = new Map(
    nodes.map(node => [String(node.stable_key), []])
  );
  const outgoing = new Map(
    nodes.map(node => [String(node.stable_key), []])
  );
  for (const edge of dependencyEdges) {
    const source = String(edge.source);
    const target = String(edge.target);
    if (incoming.has(target) && outgoing.has(source)) {
      incoming.get(target).push(source);
      outgoing.get(source).push(target);
    }
  }
  const ranks = () => {
    const result = new Map();
    for (const column of columns) {
      groups.get(column).forEach((node, index) => {
        result.set(String(node.stable_key), index);
      });
    }
    return result;
  };
  const neighborScore = (node, neighbors, rank) => {
    const values = (neighbors.get(String(node.stable_key)) || [])
      .filter(key => rank.has(key))
      .map(key => rank.get(key));
    return values.length
      ? values.reduce((total, value) => total + value, 0) / values.length
      : null;
  };
  const reorder = (column, neighbors, rank) => {
    groups.get(column).sort((first, second) => {
      const firstScore = neighborScore(first, neighbors, rank);
      const secondScore = neighborScore(second, neighbors, rank);
      if (firstScore != null && secondScore != null && firstScore !== secondScore) {
        return firstScore - secondScore;
      }
      if (firstScore != null && secondScore == null) return -1;
      if (firstScore == null && secondScore != null) return 1;
      return (
        workflowNumericRow(first, stableIndex.get(String(first.stable_key)))
        - workflowNumericRow(second, stableIndex.get(String(second.stable_key)))
        || stableIndex.get(String(first.stable_key))
        - stableIndex.get(String(second.stable_key))
      );
    });
  };
  for (let pass = 0; pass < 3; pass += 1) {
    let rank = ranks();
    for (const column of columns.slice(1)) {
      reorder(column, incoming, rank);
      rank = ranks();
    }
    rank = ranks();
    for (const column of columns.slice(0, -1).reverse()) {
      reorder(column, outgoing, rank);
      rank = ranks();
    }
  }
  const maxCount = Math.max(
    1,
    ...columns.map(column => groups.get(column).length)
  );
  const coreHeight = (
    maxCount * WORKFLOW_NODE_HEIGHT
    + Math.max(0, maxCount - 1) * WORKFLOW_ROW_GAP
  );
  const coreTop = WORKFLOW_PADDING_Y;
  const positioned = [];
  const byKey = new Map();
  columns.forEach((column, columnIndex) => {
    const group = groups.get(column);
    const groupHeight = (
      group.length * WORKFLOW_NODE_HEIGHT
      + Math.max(0, group.length - 1) * WORKFLOW_ROW_GAP
    );
    const groupTop = coreTop + (coreHeight - groupHeight) / 2;
    group.forEach((node, rowIndex) => {
      const left = (
        WORKFLOW_PADDING_X
        + columnIndex * (WORKFLOW_NODE_WIDTH + WORKFLOW_COLUMN_GAP)
      );
      const top = groupTop + rowIndex * (
        WORKFLOW_NODE_HEIGHT + WORKFLOW_ROW_GAP
      );
      const placed = {
        node,
        key: String(node.stable_key),
        column,
        row: rowIndex,
        left,
        top,
        right: left + WORKFLOW_NODE_WIDTH,
        bottom: top + WORKFLOW_NODE_HEIGHT,
        centerX: left + WORKFLOW_NODE_WIDTH / 2,
        centerY: top + WORKFLOW_NODE_HEIGHT / 2,
      };
      positioned.push(placed);
      byKey.set(placed.key, placed);
    });
  });
  const width = (
    WORKFLOW_PADDING_X * 2
    + columns.length * WORKFLOW_NODE_WIDTH
    + Math.max(0, columns.length - 1) * WORKFLOW_COLUMN_GAP
  );
  const height = coreTop + coreHeight + WORKFLOW_PADDING_Y;
  const horizontalLanes = [
    WORKFLOW_PADDING_Y / 2,
    ...Array.from({length: Math.max(0, maxCount - 1)}, (_, index) => (
      coreTop
      + (index + 1) * WORKFLOW_NODE_HEIGHT
      + index * WORKFLOW_ROW_GAP
      + WORKFLOW_ROW_GAP / 2
    )),
    height - WORKFLOW_PADDING_Y / 2,
  ];
  return {
    nodes: positioned,
    byKey,
    columns,
    width,
    height,
    coreTop,
    coreBottom: coreTop + coreHeight,
    horizontalLanes,
  };
}
function workflowSegmentIntersectsNode(
  start,
  end,
  node,
  clearance = WORKFLOW_EDGE_CLEARANCE
) {
  const left = node.left - clearance;
  const right = node.right + clearance;
  const top = node.top - clearance;
  const bottom = node.bottom + clearance;
  if (Math.abs(start.y - end.y) < .01) {
    return (
      start.y > top
      && start.y < bottom
      && Math.max(start.x, end.x) > left
      && Math.min(start.x, end.x) < right
    );
  }
  if (Math.abs(start.x - end.x) < .01) {
    return (
      start.x > left
      && start.x < right
      && Math.max(start.y, end.y) > top
      && Math.min(start.y, end.y) < bottom
    );
  }
  return true;
}
const workflowOrthogonalSegments = points => points.slice(1).map(
  (point, index) => ({start: points[index], end: point})
);
function workflowSimplifyRoute(points) {
  const unique = [];
  for (const point of points) {
    const previous = unique[unique.length - 1];
    if (!previous || previous.x !== point.x || previous.y !== point.y) {
      unique.push(point);
    }
  }
  const simplified = [];
  for (const point of unique) {
    const first = simplified[simplified.length - 2];
    const second = simplified[simplified.length - 1];
    if (
      first
      && second
      && (
        (first.x === second.x && second.x === point.x)
        || (first.y === second.y && second.y === point.y)
      )
    ) {
      simplified[simplified.length - 1] = point;
    } else {
      simplified.push(point);
    }
  }
  return simplified;
}
function workflowRouteIsClear(points, obstacles) {
  return workflowOrthogonalSegments(points).every(segment => (
    obstacles.every(node => !workflowSegmentIntersectsNode(
      segment.start,
      segment.end,
      node
    ))
  ));
}
function workflowRouteConflictPenalty(points, usedSegments) {
  let penalty = 0;
  for (const segment of workflowOrthogonalSegments(points)) {
    const horizontal = segment.start.y === segment.end.y;
    for (const used of usedSegments) {
      const usedHorizontal = used.start.y === used.end.y;
      if (horizontal === usedHorizontal) {
        if (
          horizontal
          && segment.start.y === used.start.y
        ) {
          const overlap = Math.min(
            Math.max(segment.start.x, segment.end.x),
            Math.max(used.start.x, used.end.x)
          ) - Math.max(
            Math.min(segment.start.x, segment.end.x),
            Math.min(used.start.x, used.end.x)
          );
          penalty += Math.max(0, overlap) * 4;
        } else if (
          !horizontal
          && segment.start.x === used.start.x
        ) {
          const overlap = Math.min(
            Math.max(segment.start.y, segment.end.y),
            Math.max(used.start.y, used.end.y)
          ) - Math.max(
            Math.min(segment.start.y, segment.end.y),
            Math.min(used.start.y, used.end.y)
          );
          penalty += Math.max(0, overlap) * 4;
        }
      } else {
        const horizontalSegment = horizontal ? segment : used;
        const verticalSegment = horizontal ? used : segment;
        const crosses = (
          verticalSegment.start.x > Math.min(
            horizontalSegment.start.x,
            horizontalSegment.end.x
          )
          && verticalSegment.start.x < Math.max(
            horizontalSegment.start.x,
            horizontalSegment.end.x
          )
          && horizontalSegment.start.y > Math.min(
            verticalSegment.start.y,
            verticalSegment.end.y
          )
          && horizontalSegment.start.y < Math.max(
            verticalSegment.start.y,
            verticalSegment.end.y
          )
        );
        if (crosses) penalty += 180;
      }
    }
  }
  return penalty;
}
const workflowRouteLength = points => workflowOrthogonalSegments(points)
  .reduce((total, segment) => (
    total
    + Math.abs(segment.start.x - segment.end.x)
    + Math.abs(segment.start.y - segment.end.y)
  ), 0);
function workflowEdgePortY(node, edge, layout, edgeSet, direction) {
  const nodeKey = node.key;
  const matches = direction === 'outgoing'
    ? candidate => String(candidate.source) === nodeKey
    : candidate => String(candidate.target) === nodeKey;
  const oppositeKey = candidate => String(
    direction === 'outgoing' ? candidate.target : candidate.source
  );
  const peers = edgeSet.filter(matches).sort((first, second) => {
    const firstNode = layout.byKey.get(oppositeKey(first));
    const secondNode = layout.byKey.get(oppositeKey(second));
    return (
      (firstNode?.centerY ?? node.centerY)
      - (secondNode?.centerY ?? node.centerY)
    );
  });
  if (peers.length <= 1) return node.centerY;
  const index = Math.max(0, peers.indexOf(edge));
  const inset = 12;
  return (
    node.top
    + inset
    + index * (WORKFLOW_NODE_HEIGHT - inset * 2) / (peers.length - 1)
  );
}
function workflowEdgePortX(node, edge, layout, edgeSet, direction) {
  const nodeKey = node.key;
  const matches = direction === 'outgoing'
    ? candidate => String(candidate.source) === nodeKey
    : candidate => String(candidate.target) === nodeKey;
  const oppositeKey = candidate => String(
    direction === 'outgoing' ? candidate.target : candidate.source
  );
  const peers = edgeSet.filter(matches).sort((first, second) => {
    const firstNode = layout.byKey.get(oppositeKey(first));
    const secondNode = layout.byKey.get(oppositeKey(second));
    return (
      (firstNode?.centerX ?? node.centerX)
      - (secondNode?.centerX ?? node.centerX)
    );
  });
  if (peers.length <= 1) return node.centerX;
  const index = Math.max(0, peers.indexOf(edge));
  const inset = 24;
  return (
    node.left
    + inset
    + index * (WORKFLOW_NODE_WIDTH - inset * 2) / (peers.length - 1)
  );
}
function workflowRouteEdge(edge, layout, options = {}) {
  const source = layout.byKey.get(String(edge.source));
  const target = layout.byKey.get(String(edge.target));
  if (!source || !target) return null;
  const usedSegments = options.usedSegments || [];
  const laneIndex = Number(options.laneIndex) || 0;
  const edgeSet = options.edges || [edge];
  const sourcePortY = workflowEdgePortY(
    source,
    edge,
    layout,
    edgeSet,
    'outgoing'
  );
  const targetPortY = workflowEdgePortY(
    target,
    edge,
    layout,
    edgeSet,
    'incoming'
  );
  let points;
  if (options.outerLaneY != null) {
    const outerEdges = options.outerEdges || edgeSet;
    let startX = workflowEdgePortX(
      source,
      edge,
      layout,
      outerEdges,
      'outgoing'
    );
    let endX = workflowEdgePortX(
      target,
      edge,
      layout,
      outerEdges,
      'incoming'
    );
    if (source.key === target.key) {
      startX = source.centerX + 24;
      endX = target.centerX - 24;
    }
    const hasNodeBelow = node => layout.nodes.some(other => (
      other.key !== node.key
      && other.column === node.column
      && other.top >= node.bottom
    ));
    const sourceClearsBelow = !hasNodeBelow(source);
    const targetClearsBelow = !hasNodeBelow(target);
    const forward = target.centerX >= source.centerX;
    const sourceSide = source.key === target.key ? 1 : (forward ? 1 : -1);
    const targetSide = source.key === target.key ? -1 : (forward ? -1 : 1);
    const start = sourceClearsBelow
      ? {x: startX, y: source.bottom}
      : {
          x: sourceSide > 0 ? source.right : source.left,
          y: sourcePortY,
        };
    const end = targetClearsBelow
      ? {x: endX, y: target.bottom}
      : {
          x: targetSide > 0 ? target.right : target.left,
          y: targetPortY,
        };
    const sourceRailX = sourceClearsBelow
      ? start.x
      : start.x + sourceSide * WORKFLOW_EDGE_CLEARANCE;
    const targetRailX = targetClearsBelow
      ? end.x
      : end.x + targetSide * WORKFLOW_EDGE_CLEARANCE;
    points = workflowSimplifyRoute([
      start,
      {x: sourceRailX, y: start.y},
      {x: sourceRailX, y: options.outerLaneY},
      {x: targetRailX, y: options.outerLaneY},
      {x: targetRailX, y: end.y},
      end,
    ]);
  } else if (source.key === target.key) {
    const laneY = Math.max(
      16,
      source.top - WORKFLOW_EDGE_CLEARANCE * 3
    );
    points = workflowSimplifyRoute([
      {x: source.right, y: source.centerY - 12},
      {
        x: source.right + WORKFLOW_EDGE_CLEARANCE * 2,
        y: source.centerY - 12,
      },
      {
        x: source.right + WORKFLOW_EDGE_CLEARANCE * 2,
        y: laneY,
      },
      {
        x: source.left - WORKFLOW_EDGE_CLEARANCE * 2,
        y: laneY,
      },
      {
        x: source.left - WORKFLOW_EDGE_CLEARANCE * 2,
        y: source.centerY + 12,
      },
      {x: source.left, y: source.centerY + 12},
    ]);
  } else {
    const forward = target.centerX >= source.centerX;
    const direction = forward ? 1 : -1;
    const start = {
      x: forward ? source.right : source.left,
      y: sourcePortY,
    };
    const end = {
      x: forward ? target.left : target.right,
      y: targetPortY,
    };
    const obstacles = layout.nodes.filter(
      node => node.key !== source.key && node.key !== target.key
    );
    const candidates = [];
    const middleXs = [
      (start.x + end.x) / 2,
      start.x + direction * WORKFLOW_EDGE_CLEARANCE,
      end.x - direction * WORKFLOW_EDGE_CLEARANCE,
    ];
    for (const middleX of middleXs) {
      candidates.push(workflowSimplifyRoute([
        start,
        {x: middleX, y: start.y},
        {x: middleX, y: end.y},
        end,
      ]));
    }
    const laneShift = (laneIndex % 3 - 1) * 6;
    for (const lane of layout.horizontalLanes) {
      const laneY = lane + laneShift;
      candidates.push(workflowSimplifyRoute([
        start,
        {
          x: start.x + direction * WORKFLOW_EDGE_CLEARANCE,
          y: start.y,
        },
        {
          x: start.x + direction * WORKFLOW_EDGE_CLEARANCE,
          y: laneY,
        },
        {
          x: end.x - direction * WORKFLOW_EDGE_CLEARANCE,
          y: laneY,
        },
        {
          x: end.x - direction * WORKFLOW_EDGE_CLEARANCE,
          y: end.y,
        },
        end,
      ]));
    }
    const clear = candidates.filter(
      candidate => workflowRouteIsClear(candidate, obstacles)
    );
    const available = clear.length ? clear : candidates;
    points = available.sort((first, second) => {
      const firstScore = (
        workflowRouteLength(first)
        + workflowRouteConflictPenalty(first, usedSegments)
        + first.length * 12
      );
      const secondScore = (
        workflowRouteLength(second)
        + workflowRouteConflictPenalty(second, usedSegments)
        + second.length * 12
      );
      return firstScore - secondScore;
    })[0];
  }
  const segments = workflowOrthogonalSegments(points);
  usedSegments.push(...segments);
  return {
    source,
    target,
    points,
    path: points.map((point, index) => (
      `${index ? 'L' : 'M'} ${point.x} ${point.y}`
    )).join(' '),
  };
}
function workflowRouteLabelPoint(points) {
  const segments = workflowOrthogonalSegments(points);
  const horizontal = segments
    .filter(segment => segment.start.y === segment.end.y)
    .sort((first, second) => (
      Math.abs(second.start.x - second.end.x)
      - Math.abs(first.start.x - first.end.x)
    ))[0];
  const segment = horizontal || segments[0];
  return segment
    ? {
        x: (segment.start.x + segment.end.x) / 2,
        y: (segment.start.y + segment.end.y) / 2 - 7,
      }
    : {x: 0, y: 0};
}
const workflowClamp = (value, minimum, maximum) => (
  Math.min(maximum, Math.max(minimum, value))
);
function workflowViewportState(context) {
  const key = String(context);
  if (!workflowViewportStates.has(key)) {
    workflowViewportStates.set(key, {
      x: 0,
      y: 0,
      scale: 1,
      mode: 'fit',
    });
  }
  return workflowViewportStates.get(key);
}
function workflowBoundViewport(viewport, bounds, state) {
  const scaledWidth = bounds.width * state.scale;
  const scaledHeight = bounds.height * state.scale;
  const minimumX = 48 - scaledWidth;
  const maximumX = viewport.clientWidth - 48;
  const minimumY = 48 - scaledHeight;
  const maximumY = viewport.clientHeight - 48;
  state.x = minimumX > maximumX
    ? (viewport.clientWidth - scaledWidth) / 2
    : workflowClamp(state.x, minimumX, maximumX);
  state.y = minimumY > maximumY
    ? (viewport.clientHeight - scaledHeight) / 2
    : workflowClamp(state.y, minimumY, maximumY);
}
function workflowApplyViewport(viewport, scene, bounds, state) {
  state.scale = workflowClamp(
    state.scale,
    WORKFLOW_MIN_ZOOM,
    WORKFLOW_MAX_ZOOM
  );
  workflowBoundViewport(viewport, bounds, state);
  scene.style.transform = [
    `translate(${state.x}px, ${state.y}px)`,
    `scale(${state.scale})`,
  ].join(' ');
  viewport.dataset.workflowScale = state.scale.toFixed(3);
  const status = viewport.querySelector('[data-workflow-zoom-status]');
  if (status) status.textContent = `${Math.round(state.scale * 100)}%`;
}
function workflowFitViewport(viewport, scene, bounds, context) {
  const state = workflowViewportState(context);
  const padding = 24;
  const scale = workflowClamp(
    Math.min(
      (viewport.clientWidth - padding * 2) / bounds.width,
      (viewport.clientHeight - padding * 2) / bounds.height
    ),
    WORKFLOW_MIN_ZOOM,
    1
  );
  state.scale = scale;
  state.x = (viewport.clientWidth - bounds.width * scale) / 2;
  state.y = (viewport.clientHeight - bounds.height * scale) / 2;
  state.mode = 'fit';
  workflowApplyViewport(viewport, scene, bounds, state);
  return state;
}
function workflowZoomViewport(
  viewport,
  scene,
  bounds,
  context,
  factor,
  clientX = null,
  clientY = null
) {
  const state = workflowViewportState(context);
  const rectangle = viewport.getBoundingClientRect();
  const originX = clientX == null
    ? viewport.clientWidth / 2
    : clientX - rectangle.left;
  const originY = clientY == null
    ? viewport.clientHeight / 2
    : clientY - rectangle.top;
  const graphX = (originX - state.x) / state.scale;
  const graphY = (originY - state.y) / state.scale;
  const scale = workflowClamp(
    state.scale * factor,
    WORKFLOW_MIN_ZOOM,
    WORKFLOW_MAX_ZOOM
  );
  state.scale = scale;
  state.x = originX - graphX * scale;
  state.y = originY - graphY * scale;
  state.mode = 'manual';
  workflowApplyViewport(viewport, scene, bounds, state);
}
function workflowInstallViewport(viewport, scene, bounds, context) {
  const state = workflowViewportState(context);
  const apply = () => workflowApplyViewport(
    viewport,
    scene,
    bounds,
    state
  );
  const fit = () => workflowFitViewport(
    viewport,
    scene,
    bounds,
    context
  );
  viewport.querySelector('[data-workflow-viewport-control="zoom-in"]')
    ?.addEventListener('click', () => workflowZoomViewport(
      viewport,
      scene,
      bounds,
      context,
      1.2
    ));
  viewport.querySelector('[data-workflow-viewport-control="zoom-out"]')
    ?.addEventListener('click', () => workflowZoomViewport(
      viewport,
      scene,
      bounds,
      context,
      1 / 1.2
    ));
  viewport.querySelector('[data-workflow-viewport-control="fit"]')
    ?.addEventListener('click', fit);
  viewport.querySelector('[data-workflow-viewport-control="reset"]')
    ?.addEventListener('click', () => {
      state.scale = 1;
      state.x = (viewport.clientWidth - bounds.width) / 2;
      state.y = (viewport.clientHeight - bounds.height) / 2;
      state.mode = 'manual';
      apply();
    });
  viewport.addEventListener('wheel', event => {
    event.preventDefault();
    workflowZoomViewport(
      viewport,
      scene,
      bounds,
      context,
      Math.exp(-event.deltaY * .0015),
      event.clientX,
      event.clientY
    );
  }, {passive: false});
  let drag = null;
  viewport.addEventListener('pointerdown', event => {
    if (
      event.button !== 0
      || event.target.closest(
        'button, select, a, [data-workflow-node]'
      )
    ) return;
    drag = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      x: state.x,
      y: state.y,
    };
    state.mode = 'manual';
    viewport.classList.add('dragging');
    viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener('pointermove', event => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    state.x = drag.x + event.clientX - drag.clientX;
    state.y = drag.y + event.clientY - drag.clientY;
    apply();
  });
  const stopDrag = event => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    drag = null;
    viewport.classList.remove('dragging');
    if (viewport.hasPointerCapture(event.pointerId)) {
      viewport.releasePointerCapture(event.pointerId);
    }
  };
  viewport.addEventListener('pointerup', stopDrag);
  viewport.addEventListener('pointercancel', stopDrag);
  viewport.addEventListener('keydown', event => {
    if (event.target !== viewport) return;
    if (event.key === '+' || event.key === '=') {
      event.preventDefault();
      workflowZoomViewport(
        viewport,
        scene,
        bounds,
        context,
        1.2
      );
    } else if (event.key === '-') {
      event.preventDefault();
      workflowZoomViewport(
        viewport,
        scene,
        bounds,
        context,
        1 / 1.2
      );
    } else if (event.key === '0' || event.key.toLowerCase() === 'f') {
      event.preventDefault();
      fit();
    } else if (
      ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(
        event.key
      )
    ) {
      event.preventDefault();
      state.mode = 'manual';
      if (event.key === 'ArrowLeft') state.x += 48;
      if (event.key === 'ArrowRight') state.x -= 48;
      if (event.key === 'ArrowUp') state.y += 48;
      if (event.key === 'ArrowDown') state.y -= 48;
      apply();
    }
  });
  requestAnimationFrame(() => {
    if (state.mode === 'fit') fit();
    else apply();
  });
  workflowViewportResizeObserver?.disconnect();
  workflowViewportResizeObserver = null;
  if ('ResizeObserver' in window) {
    workflowViewportResizeObserver = new ResizeObserver(() => {
      if (state.mode === 'fit') fit();
      else apply();
    });
    workflowViewportResizeObserver.observe(viewport);
  }
}
const workflowStateClass = state => ({
  running: 'running',
  succeeded: 'succeeded',
  failed: 'failed',
  canceled: 'canceled',
})[state] || '';
const workflowControllerTransitions = graph => [
  ...(graph.controller_edges || []),
  ...(graph.lifecycle_edges || []),
];
const workflowLifecycleTypeLabel = type => ({
  activation: 'Issue activation',
  retry: 'Retry failed node',
  revision: 'Coordinator revision',
  'validation-remediation': 'Validation remediation',
  'acceptance-remediation': 'Acceptance remediation',
  feedback: 'Feedback generation',
  termination: 'Terminal outcome',
  validation: 'Exact-SHA validation',
  acceptance: 'Independent acceptance',
  publication: 'Controller publication',
  'feedback-monitoring': 'Feedback monitoring',
})[type] || String(type || 'Controller transition');
function workflowSelection(repository) {
  const repositoryRuns = allRuns().filter(
    run => String(run.repository_id) === String(repository.id)
      && run.workflow
  );
  const explicitlySelected = repositoryRuns.find(
    run => String(run.id) === String(selectedRunId)
  );
  const run = explicitlySelected || repositoryRuns[0];
  if (run?.workflow?.generations?.length) {
    const generations = run.workflow.generations;
    const defaultGeneration = run.workflow.active_generation
      ?? generations[generations.length - 1].generation;
    const requestedGeneration = Number(selectedWorkflowGeneration);
    const generationNumber = generations.some(
      item => Number(item.generation) === requestedGeneration
    ) ? requestedGeneration : Number(defaultGeneration);
    const graph = generations.find(
      item => Number(item.generation) === generationNumber
    );
    selectedWorkflowGeneration = generationNumber;
    return {
      run,
      graph,
      generations,
      title: `Issue #${run.issue_number} workflow`,
      source: 'run',
    };
  }
  if (!repository.workflow_template) return null;
  const template = repository.workflow_template;
  return {
    run: null,
    graph: {
      generation: null,
      active: false,
      state: 'template',
      reason: 'Repository team template',
      rationale: template.rationale,
      assessment_prompt: template.assessment_prompt,
      assessment: null,
      nodes: template.nodes || [],
      edges: template.edges || [],
      controller_boundaries: template.controller_boundaries || [],
      controller_edges: template.controller_edges || [],
      system_boundaries: template.system_boundaries || [],
      lifecycle_edges: template.lifecycle_edges || [],
    },
    generations: [],
    title: 'Repository workflow template',
    source: 'template',
  };
}
function workflowDependencies(graph, stableKey) {
  return (graph.edges || [])
    .filter(edge => String(edge.target) === String(stableKey))
    .map(edge => edge.source);
}
function renderWorkflowNodeDetails(node, graph) {
  const detail = document.querySelector('#workflow-node-details');
  if (!detail) return;
  if (!node) {
    detail.innerHTML = [
      '<p class="muted">',
      'Select a node to inspect its prompt, dependencies, resources, ',
      'and attempts.</p>',
    ].join('');
    return;
  }
  const attempts = (node.attempts || []).map(attempt => {
    const claims = (attempt.resource_claims || []).map(claim => {
      const release = claim.released_at ? ', released' : ', active';
      return `${claim.resource} (${claim.access}${release})`;
    }).join(', ');
    const attemptInput = `<details>
      <summary>Attempt input</summary>
      <pre class="prompt">${esc(JSON.stringify(
        attempt.input || {},
        null,
        2
      ))}</pre>
    </details>`;
    const completed = attempt.completed_at
      ? ` · <strong>Completed:</strong> ${esc(date(
        attempt.completed_at
      ))}`
      : ' · In progress';
    const timing = `<div>
      <strong>Started:</strong> ${esc(date(attempt.started_at))}
      ${completed}
    </div>`;
    const attemptError = attempt.error
      ? `<pre class="prompt">${esc(JSON.stringify(
        attempt.error,
        null,
        2
      ))}</pre>`
      : '';
    const attemptOutput = attempt.output
      ? `<pre class="prompt">${esc(JSON.stringify(
        attempt.output,
        null,
        2
      ))}</pre>`
      : '';
    const log = attempt.log_path
      ? ` · log <code>${esc(attempt.log_path)}</code>`
      : '';
    const claimDetail = claims
      ? `<div><strong>Claims:</strong> ${esc(claims)}</div>`
      : '';
    return [
      `<li>Attempt ${esc(attempt.attempt)} · `,
      `<strong>${esc(attempt.state)}</strong>${log}`,
      timing,
      attemptInput,
      claimDetail,
      attemptError,
      attemptOutput,
      '</li>',
    ].join('');
  }).join('') || '<li>No attempts recorded.</li>';
  const dependencies = workflowDependencies(graph, node.stable_key);
  const metadata = [
    node.kind,
    node.role,
    node.operation,
  ].filter(Boolean).join(' · ');
  const resources = (node.resources || []).join(', ') || 'None';
  const operationVersion = node.operation_version
    ? `<p><strong>Operation version:</strong> <code>${esc(
      String(node.operation_version).slice(0, 12)
    )}</code></p>`
    : '';
  const resourceWaits = node.resource_wait_count
    ? `<p><strong>Resource waits:</strong> ${esc(
      node.resource_wait_count
    )}</p>`
    : '';
  const reused = node.reused
    ? '<p><span class="badge success">Reused output</span></p>'
    : '';
  const relatedTransitions = workflowControllerTransitions(graph).filter(
    edge => (
      String(edge.source) === String(node.stable_key)
      || String(edge.target) === String(node.stable_key)
    )
  );
  const lifecycleDetail = relatedTransitions.length
    ? `<h4>Controller lifecycle transitions</h4><ul>${
        relatedTransitions.map(edge => {
          const direction = String(edge.source) === String(node.stable_key)
            ? `to ${edge.target}`
            : `from ${edge.source}`;
          return `<li><strong>${esc(
            edge.label || workflowLifecycleTypeLabel(edge.type)
          )}</strong> ${esc(direction)}<br><span class="muted">${
            esc(edge.trigger || 'controller-owned transition')
          } · Next durable unit: ${esc(edge.next_unit || 'not applicable')}
          </span></li>`;
        }).join('')
      }</ul>`
    : '';
  const contractLabels = {
    mode: 'Projection',
    run_id: 'Run',
    issue_version_id: 'Issue version',
    team_version_id: 'Team version',
    sandbox_version_id: 'Sandbox version',
    base_sha: 'Exact base SHA',
    generation: 'Generation',
  };
  const contractDetail = node.contract
    ? `<h4>Immutable run contract</h4><dl>${
        Object.entries(contractLabels).map(([key, label]) => {
          const value = node.contract[key];
          const shown = value == null ? 'Bound at issue activation' : value;
          return `<dt>${esc(label)}</dt><dd><code>${esc(shown)}</code></dd>`;
        }).join('')
      }</dl>`
    : '';
  const terminalDetail = Array.isArray(node.outcomes)
    ? `<p><strong>Terminal durable states:</strong> ${esc(
        node.outcomes.join(', ')
      )}</p>`
    : '';
  const expectedOutput = esc(JSON.stringify(
    node.expected_output || {},
    null,
    2
  ));
  const parametersAndBindings = esc(JSON.stringify(
    {
      parameters: node.parameters || {},
      bindings: node.bindings || {},
    },
    null,
    2
  ));
  const output = node.output
    ? `<details><summary>Output</summary><pre class="prompt">${esc(
      JSON.stringify(node.output, null, 2)
    )}</pre></details>`
    : '';
  const error = node.error
    ? `<details open><summary>Error</summary><pre class="prompt">${esc(
      JSON.stringify(node.error, null, 2)
    )}</pre></details>`
    : '';
  detail.innerHTML = `
    <div class="row">
      <h4>${esc(node.title || node.stable_key)}</h4>
      <span class="badge">
        ${esc(node.status_label || node.state || 'Template')}
      </span>
    </div>
    <p>${esc(metadata)}</p>
    <p>
      <strong>Dependencies:</strong>
      ${dependencies.length ? esc(dependencies.join(', ')) : 'None'}
    </p>
    <p><strong>Resources:</strong> ${esc(resources)}</p>
    ${operationVersion}
    ${resourceWaits}
    ${reused}
    ${contractDetail}
    ${terminalDetail}
    ${lifecycleDetail}
    <h4>Prompt</h4>
    <pre class="prompt">${esc(node.prompt || '')}</pre>
    <details>
      <summary>Parameters and bindings</summary>
      <pre class="prompt">${parametersAndBindings}</pre>
    </details>
    <details>
      <summary>Expected output</summary>
      <pre class="prompt">${expectedOutput}</pre>
    </details>
    ${output}
    ${error}
    <details>
      <summary>Attempts</summary>
      <ol>${attempts}</ol>
    </details>`;
}
function workflowGenerationDelta(selection) {
  if (selection.source !== 'run' || selection.generations.length < 2) {
    return '';
  }
  const generations = [...selection.generations].sort(
    (first, second) => Number(first.generation) - Number(second.generation)
  );
  const index = generations.findIndex(
    item => Number(item.generation) === Number(selection.graph.generation)
  );
  if (index <= 0) {
    return `<details>
      <summary>Generation delta</summary>
      <p class="muted">This is the first immutable generation.</p>
    </details>`;
  }
  const previous = generations[index - 1];
  const current = selection.graph;
  const previousNodes = new Map(
    (previous.nodes || []).map(node => [String(node.stable_key), node])
  );
  const currentNodes = new Map(
    (current.nodes || []).map(node => [String(node.stable_key), node])
  );
  const incoming = (graph, key) => (graph.edges || [])
    .filter(edge => String(edge.target) === String(key))
    .map(edge => String(edge.source))
    .sort();
  const canonical = value => {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') {
      return Object.fromEntries(
        Object.keys(value)
          .sort()
          .map(key => [key, canonical(value[key])])
      );
    }
    return value;
  };
  const encoded = value => JSON.stringify(canonical(value ?? null));
  const added = [...currentNodes.keys()].filter(
    key => !previousNodes.has(key)
  );
  const removed = [...previousNodes.keys()].filter(
    key => !currentNodes.has(key)
  );
  const changed = [...currentNodes.entries()].flatMap(([key, node]) => {
    const prior = previousNodes.get(key);
    if (!prior) return [];
    const fields = [];
    if (encoded(node.prompt) !== encoded(prior.prompt)) {
      fields.push('prompt');
    }
    if (
      encoded(node.parameters) !== encoded(prior.parameters)
      || encoded(node.bindings) !== encoded(prior.bindings)
    ) {
      fields.push('parameters or bindings');
    }
    if (encoded(node.expected_output) !== encoded(prior.expected_output)) {
      fields.push('expected output');
    }
    if (
      encoded([
        node.kind,
        node.member_key,
        node.role,
        node.operation,
        node.operation_version,
        node.resources,
      ]) !== encoded([
        prior.kind,
        prior.member_key,
        prior.role,
        prior.operation,
        prior.operation_version,
        prior.resources,
      ])
    ) {
      fields.push('definition or resources');
    }
    if (
      encoded(incoming(current, key))
      !== encoded(incoming(previous, key))
    ) {
      fields.push('dependencies');
    }
    return fields.length ? [`${key} (${fields.join(', ')})`] : [];
  });
  const reused = (current.nodes || [])
    .filter(node => node.reused)
    .map(node => String(node.stable_key));
  const rerun = (current.nodes || [])
    .filter(node => {
      const prior = previousNodes.get(String(node.stable_key));
      return prior && prior.state === 'succeeded' && !node.reused;
    })
    .map(node => String(node.stable_key));
  const value = items => items.length
    ? esc(items.join(', '))
    : '<span class="muted">None</span>';
  return `<details>
    <summary>Generation delta from ${esc(previous.generation)}</summary>
    <dl>
      <dt>Added nodes</dt><dd>${value(added)}</dd>
      <dt>Removed nodes</dt><dd>${value(removed)}</dd>
      <dt>Changed nodes</dt><dd>${value(changed)}</dd>
      <dt>Reused outputs</dt><dd>${value(reused)}</dd>
      <dt>Rerun nodes</dt><dd>${value(rerun)}</dd>
    </dl>
  </details>`;
}
function renderWorkflowGraph(repository, preferredNode = null) {
  const host = document.querySelector('#workflow-preview');
  if (!host) return;
  const selection = workflowSelection(repository);
  if (!selection) {
    host.innerHTML = [
      '<h3 id="workflow-heading">Workflow</h3>',
      '<p class="muted">',
      'No stored workflow template exists yet.',
      '</p>',
    ].join('');
    return;
  }
  const graph = selection.graph;
  host.dataset.workflowContext = [
    String(repository.id),
    selection.run ? String(selection.run.id) : 'template',
    graph.generation == null
      ? 'template'
      : String(graph.generation),
  ].join(':');
  const systemBoundaries = graph.system_boundaries || [];
  const nodes = [
    ...systemBoundaries.slice(0, 1),
    ...(graph.nodes || []),
    ...(graph.controller_boundaries || []),
    ...systemBoundaries.slice(1),
  ];
  const dependencyEdges = graph.edges || [];
  const lifecycleEdges = workflowControllerTransitions(graph);
  const iterativeTypes = new Set([
    'retry',
    'revision',
    'validation-remediation',
    'acceptance-remediation',
    'feedback',
  ]);
  const edges = [
    ...dependencyEdges,
    ...lifecycleEdges.filter(edge => !iterativeTypes.has(edge.type)),
  ];
  const byKey = new Map(
    nodes.map(node => [String(node.stable_key), node])
  );
  const layout = workflowLayeredLayout(nodes, dependencyEdges);
  const isLoopEdge = edge => {
    const source = layout.byKey.get(String(edge.source));
    const target = layout.byKey.get(String(edge.target));
    if (!source || !target) return false;
    return source.column >= target.column || (
      edge.type === 'termination'
      && String(edge.source) === 'controller:run-contract'
    );
  };
  const loopEdges = lifecycleEdges.filter(isLoopEdge);
  const width = Math.max(640, layout.width);
  const loopRailStart = layout.height + 28;
  const height = layout.height + (
    loopEdges.length ? 48 + loopEdges.length * 36 : 0
  );
  const bounds = {width, height};
  const usedSegments = [];
  const routingEdges = [...dependencyEdges, ...lifecycleEdges];
  const dependencyPaths = dependencyEdges.map((edge, index) => {
    const route = workflowRouteEdge(edge, layout, {
      laneIndex: index,
      edges: routingEdges,
      usedSegments,
    });
    if (!route) return '';
    return `<path class="workflow-edge workflow-edge-dependency"
      d="${route.path}" marker-end="url(#workflow-arrow)"
      data-workflow-route="dependency"
      data-edge-source="${esc(edge.source)}"
      data-edge-target="${esc(edge.target)}"
      data-route-points="${esc(JSON.stringify(route.points))}"></path>`;
  }).join('');
  const activationEdges = lifecycleEdges.filter(
    edge => edge.type === 'activation'
  );
  const lifecyclePaths = lifecycleEdges.map((edge, index) => {
    const loopIndex = loopEdges.indexOf(edge);
    const route = workflowRouteEdge(edge, layout, {
      laneIndex: index,
      edges: routingEdges,
      outerEdges: loopEdges,
      usedSegments,
      outerLaneY: loopIndex >= 0
        ? loopRailStart + loopIndex * 36
        : null,
    });
    if (!route) return '';
    const activationIndex = activationEdges.indexOf(edge);
    const label = edge.label || workflowLifecycleTypeLabel(edge.type);
    let visualLabel = '';
    if (loopIndex >= 0) {
      if (edge.type === 'retry') {
        visualLabel = `Retry · ${edge.next_unit || 'attempt N+1'}`;
      } else if (String(edge.target) === 'controller:run-contract') {
        visualLabel = `${label} → run contract`;
      } else if (edge.type === 'termination') {
        visualLabel = 'Cancel → terminal';
      }
    } else if (activationIndex === 0) {
      visualLabel = 'Activate';
    } else if (edge.type === 'termination') {
      visualLabel = 'Close';
    }
    const description = [
      label,
      edge.trigger,
      edge.next_unit
        ? `Next durable unit: ${edge.next_unit}`
        : null,
    ].filter(Boolean).join(' — ');
    const labelPoint = workflowRouteLabelPoint(route.points);
    const visualText = visualLabel
      ? `<text class="workflow-lifecycle-label"
          x="${labelPoint.x}" y="${labelPoint.y}" text-anchor="middle">
          ${esc(visualLabel)}
        </text>`
      : '';
    return `<path class="workflow-edge workflow-edge-lifecycle"
      d="${route.path}" marker-end="url(#workflow-lifecycle-arrow)"
      data-workflow-route="lifecycle"
      data-edge-type="${esc(edge.type)}"
      data-edge-source="${esc(edge.source)}"
      data-edge-target="${esc(edge.target)}"
      data-route-points="${esc(JSON.stringify(route.points))}">
      <title>${esc(description)}</title>
    </path>${visualText}`;
  }).join('');
  const lines = dependencyPaths + lifecyclePaths;
  const nodeButtons = layout.nodes.map(placed => {
    const node = placed.node;
    const status = node.status_label || node.state || 'Template';
    const classes = [
      'workflow-node',
      workflowStateClass(node.state),
      `workflow-kind-${esc(node.kind)}`,
      `workflow-boundary-${esc(node.boundary || 'specialist')}`,
    ].join(' ');
    const metadata = [
      status,
      node.role || node.operation || node.kind,
      node.reused ? 'Reused output' : null,
    ].filter(Boolean).join(' · ');
    return `<button type="button"
      class="${classes}"
      style="left:${placed.left}px;top:${placed.top}px"
      data-workflow-node="${esc(node.stable_key)}"
      data-workflow-column="${placed.column}"
      data-workflow-row="${placed.row}"
      aria-label="${esc(node.title || node.stable_key)}: ${esc(metadata)}">
      <strong>${esc(node.title || node.stable_key)}</strong>
      <small>${esc(metadata)}</small>
    </button>`;
  }).join('');
  const dependencyRows = nodes.map(node => {
    const dependencies = workflowDependencies(
      graph,
      node.stable_key
    ).join(', ') || 'None';
    const resources = (node.resources || []).join(', ') || 'None';
    const status = node.status_label || node.state || 'Template';
    return `
      <tr>
        <th scope="row">${esc(node.stable_key)}</th>
        <td>${esc(node.kind)}</td>
        <td>${esc(node.role || node.operation || '—')}</td>
        <td>${esc(dependencies)}</td>
        <td>${esc(status)}</td>
        <td>${esc(resources)}</td>
        <td>${node.reused ? 'Reused output' : 'No'}</td>
      </tr>`;
  }).join('');
  const lifecycleRows = lifecycleEdges.map(edge => {
    const source = byKey.get(String(edge.source));
    const target = byKey.get(String(edge.target));
    const label = edge.label || workflowLifecycleTypeLabel(edge.type);
    return `
      <tr>
        <th scope="row">${esc(label)}</th>
        <td>${esc(workflowLifecycleTypeLabel(edge.type))}</td>
        <td>${esc(edge.trigger || 'Controller-owned transition')}</td>
        <td>${esc(source?.title || edge.source)}</td>
        <td>${esc(target?.title || edge.target)}</td>
        <td>${esc(edge.next_unit || 'Not applicable')}</td>
      </tr>`;
  }).join('');
  const generationOptions = selection.generations.map(item => {
    const selected = Number(item.generation) === Number(graph.generation)
      ? ' selected'
      : '';
    const active = item.active ? ' · active' : '';
    return `<option value="${esc(item.generation)}"${selected}>
      Generation ${esc(item.generation)} · ${esc(item.state)}${active}
    </option>`;
  }).join('');
  const generationControl = selection.generations.length
    ? `<label>Graph generation
        <select id="workflow-generation">
          ${generationOptions}
        </select>
      </label>`
    : '<span class="badge">Graph generation · template</span>';
  const assessments = Array.isArray(graph.assessments)
    ? graph.assessments
    : graph.assessment
      ? [graph.assessment]
      : [];
  const assessment = assessments.length
    ? `<ol>${assessments.map(item => `
        <li>
          <strong>${esc(item.outcome)}</strong>:
          ${esc(item.evidence)}
          ${item.created_at
            ? `<span class="muted"> · ${esc(item.created_at)}</span>`
            : ''}
        </li>`).join('')}</ol>`
    : [
      '<p class="muted">',
      'No assessment has been recorded for this graph generation.',
      '</p>',
    ].join('');
  const generationDelta = workflowGenerationDelta(selection);
  host.innerHTML = `
    <div class="workflow-toolbar">
      <div>
        <h3 id="workflow-heading">${esc(selection.title)}</h3>
        <p class="muted">${esc(graph.reason || '')}</p>
      </div>
      ${generationControl}
    </div>
    <p>${esc(graph.rationale || '')}</p>
    ${generationDelta}
    <details>
      <summary>Legend and controller semantics</summary>
      <div class="workflow-legend" aria-label="Workflow graph semantics">
        <span class="workflow-legend-item workflow-kind-agent">
          Agent
        </span>
        <span class="workflow-legend-item workflow-kind-deterministic">
          Deterministic
        </span>
        <span class="workflow-legend-item controller-boundary">
          Run contract / issue activation
        </span>
        <span class="workflow-legend-item controller-boundary">
          Exact-SHA validation
        </span>
        <span class="workflow-legend-item controller-boundary">
          Independent acceptance
        </span>
        <span class="workflow-legend-item controller-boundary">
          Controller publication
        </span>
        <span class="workflow-legend-item controller-boundary">
          Terminal outcomes
        </span>
        <span class="workflow-legend-item workflow-edge-key">
          Solid executable dependency
        </span>
        <span class="workflow-legend-item workflow-edge-key lifecycle">
          Controller lifecycle (projection only)
        </span>
      </div>
    </details>
    <p id="workflow-navigation-help" class="workflow-scroll-hint">
      The whole graph is fitted initially. Use the controls or wheel to zoom;
      drag empty space or use arrow keys to pan. Select a node for
      durable details. Solid arrows are executable dependencies; dashed
      arrows are controller transitions and can return for attempt N+1 or
      generation N+1. The tables are the accessible equivalent.
    </p>
    <div id="workflow-graph" class="workflow-grid" role="group" tabindex="0"
      aria-describedby="workflow-navigation-help"
      aria-label="Interactive workflow dependency graph">
      <div class="workflow-viewport-controls" role="toolbar"
        aria-label="Workflow graph viewport controls">
        <button type="button"
          data-workflow-viewport-control="zoom-out"
          aria-label="Zoom out">−</button>
        <button type="button"
          data-workflow-viewport-control="zoom-in"
          aria-label="Zoom in">+</button>
        <button type="button" aria-label="Fit workflow graph"
          data-workflow-viewport-control="fit">Fit graph</button>
        <button type="button" aria-label="Reset workflow graph zoom"
          data-workflow-viewport-control="reset">100%</button>
        <output data-workflow-zoom-status aria-live="polite">100%</output>
      </div>
      <div class="workflow-scene"
        style="width:${width}px;height:${height}px">
        <svg width="${width}" height="${height}"
          viewBox="0 0 ${width} ${height}" role="img"
          aria-label="Workflow dependency graph">
          <defs>
            <marker id="workflow-arrow" viewBox="0 0 10 10"
              refX="8" refY="5" markerWidth="6" markerHeight="6"
              orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z"
                fill="var(--muted)"></path>
            </marker>
            <marker id="workflow-lifecycle-arrow" viewBox="0 0 10 10"
              refX="8" refY="5" markerWidth="6" markerHeight="6"
              orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z"
                fill="var(--accent)"></path>
            </marker>
          </defs>
          ${lines}
        </svg>
        ${nodeButtons}
      </div>
    </div>
    <div id="workflow-node-details"
      class="subpanel workflow-node-details"
      aria-live="polite"></div>
    <details>
      <summary>Performance assessments</summary>
      ${assessment}
      <p>
        <strong>Assessment prompt:</strong>
        ${esc(graph.assessment_prompt || 'Not recorded')}
      </p>
    </details>
    <div class="workflow-table-wrap">
      <table id="workflow-table" class="workflow-table">
        <caption>Workflow nodes and executable dependency status</caption>
        <thead>
          <tr>
            <th>Node</th>
            <th>Type</th>
            <th>Role or operation</th>
            <th>Executable dependencies</th>
            <th>Status</th>
            <th>Resources</th>
            <th>Reused</th>
          </tr>
        </thead>
        <tbody>
          ${dependencyRows || (
            '<tr><td colspan="7">No nodes stored.</td></tr>'
          )}
        </tbody>
      </table>
    </div>
    <div class="workflow-table-wrap workflow-lifecycle-table">
      <table id="workflow-lifecycle-table" class="workflow-table">
        <caption>Lifecycle transitions (projection only)</caption>
        <thead>
          <tr>
            <th>Transition</th>
            <th>Type</th>
            <th>Trigger</th>
            <th>Source</th>
            <th>Target</th>
            <th>Next durable unit</th>
          </tr>
        </thead>
        <tbody>
          ${lifecycleRows || (
            '<tr><td colspan="6">No lifecycle transitions projected.</td></tr>'
          )}
        </tbody>
      </table>
    </div>`;
  const workflowViewport = host.querySelector('#workflow-graph');
  const workflowScene = host.querySelector('.workflow-scene');
  if (workflowViewport && workflowScene) {
    workflowInstallViewport(
      workflowViewport,
      workflowScene,
      bounds,
      host.dataset.workflowContext
    );
  }
  const generation = document.querySelector('#workflow-generation');
  if (generation) {
    generation.addEventListener('change', event => {
      selectedWorkflowGeneration = Number(event.target.value);
      selectedWorkflowNode = null;
      renderWorkflowGraph(repository);
    });
  }
  const selectNode = (key, focus = false) => {
    const node = byKey.get(String(key));
    if (!node) return;
    selectedWorkflowNode = String(key);
    renderWorkflowNodeDetails(node, graph);
    if (focus) {
      const selector = [
        '[data-workflow-node="',
        CSS.escape(String(key)),
        '"]',
      ].join('');
      document.querySelector(selector)?.focus();
    }
  };
  const buttons = [
    ...host.querySelectorAll('[data-workflow-node]'),
  ];
  for (const button of buttons) {
    button.addEventListener(
      'click',
      () => selectNode(button.dataset.workflowNode)
    );
    button.addEventListener(
      'focus',
      () => selectNode(button.dataset.workflowNode)
    );
    button.addEventListener('keydown', event => {
      const current = byKey.get(
        String(button.dataset.workflowNode)
      );
      if (!current) return;
      let target = null;
      if (event.key === 'ArrowRight') {
        target = edges.find(
          edge => String(edge.source) === String(current.stable_key)
        )?.target;
      } else if (event.key === 'ArrowLeft') {
        target = edges.find(
          edge => String(edge.target) === String(current.stable_key)
        )?.source;
      } else if (
        event.key === 'ArrowUp'
        || event.key === 'ArrowDown'
      ) {
        const direction = event.key === 'ArrowUp' ? -1 : 1;
        target = nodes
          .filter(node => (
            Number(node.column) === Number(current.column)
            && direction * (
              Number(node.row) - Number(current.row)
            ) > 0
          ))
          .sort((first, second) => (
            Math.abs(Number(first.row) - Number(current.row))
            - Math.abs(Number(second.row) - Number(current.row))
          ))[0]?.stable_key;
      } else if (event.key === 'Home') {
        target = nodes[0]?.stable_key;
      } else if (event.key === 'End') {
        target = nodes[nodes.length - 1]?.stable_key;
      }
      if (target == null) return;
      event.preventDefault();
      selectNode(target, true);
    });
  }
  const requested = preferredNode || selectedWorkflowNode;
  const initialNode = byKey.has(String(requested))
    ? String(requested)
    : (nodes[0] ? String(nodes[0].stable_key) : null);
  if (initialNode) {
    selectNode(initialNode);
    if (preferredNode) {
      requestAnimationFrame(() => {
        const selector = [
          '[data-workflow-node="',
          CSS.escape(initialNode),
          '"]',
        ].join('');
        document.querySelector(selector)?.focus({preventScroll: true});
      });
    }
  } else {
    renderWorkflowNodeDetails(null, graph);
  }
}
async function mutate(path, payload) {
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
const modelFieldNames = ['default_model', 'lead_model', 'implementer_model', 'verifier_model'];
const catalogDisplayValue = value => {
  const match = (modelCatalog.models || []).find(model => model.value === value);
  return match ? match.id : (value || '');
};
const catalogTransportValue = value => {
  const normalized = String(value || '').trim();
  const match = (modelCatalog.models || []).find(model => model.id === normalized);
  return match ? match.value : normalized;
};
function populateModelConfigurationForm() {
  const configuration = currentState.model_configuration || {};
  const form = document.querySelector('#model-configuration-form');
  form.elements.api_endpoint.value = configuration.api_endpoint || '';
  for (const name of modelFieldNames) {
    form.elements[name].value = catalogDisplayValue(configuration[name]);
  }
  form.elements.clear_api_key.checked = false;
}
function renderModelCatalog() {
  const models = Array.isArray(modelCatalog.models) ? modelCatalog.models : [];
  document.querySelector('#model-catalog').innerHTML = models
    .map(model => `<option value="${esc(model.id)}"></option>`)
    .join('');
  const status = document.querySelector('#model-catalog-status');
  status.textContent = modelCatalog.available
    ? `${models.length} ${models.length === 1 ? 'model' : 'models'} loaded from the configured endpoint.`
    : `${modelCatalog.reason || 'Model catalog unavailable'}. Manual entry remains available.`;
}
async function loadModelCatalog() {
  const status = document.querySelector('#model-catalog-status');
  status.textContent = 'Loading models from the configured endpoint…';
  try {
    const response = await fetch('/api/model-configuration/models', {cache:'no-store'});
    const catalog = await response.json();
    if (!response.ok) throw new Error(catalog.error || `HTTP ${response.status}`);
    modelCatalog = {
      available: catalog.available === true,
      reason: typeof catalog.reason === 'string' ? catalog.reason : null,
      models: Array.isArray(catalog.models)
        ? catalog.models.filter(model => typeof model?.id === 'string' && typeof model?.value === 'string')
        : [],
    };
  } catch (failure) {
    modelCatalog = {available:false, reason:boundedMessage(failure.message), models:[]};
  }
  renderModelCatalog();
  const form = document.querySelector('#model-configuration-form');
  for (const name of modelFieldNames) {
    form.elements[name].value = catalogDisplayValue(form.elements[name].value);
  }
}
function renderModelConfiguration() {
  const configuration = currentState.model_configuration || {};
  const status = document.querySelector('#model-configuration-status');
  const settingsSaved = Boolean(configuration.default_model);
  const missingKey = settingsSaved
    && configuration.api_key_required
    && !configuration.api_key_configured;
  status.textContent = settingsSaved ? 'Settings saved' : 'Needs setup';
  status.className = settingsSaved ? 'badge success' : 'badge warning';
  const execution = document.querySelector('#model-execution-status');
  execution.textContent = configuration.configured
    ? 'Ready for model execution.'
    : (missingKey
      ? 'Execution unavailable: API key missing.'
      : 'Execution unavailable: save a default model.');
  execution.className = configuration.configured
    ? 'execution-status ready'
    : 'execution-status error';
  const keyStatus = document.querySelector('#api-key-status');
  keyStatus.textContent = configuration.api_key_configured
    ? `API key configured (${configuration.api_key_source})`
    : 'No managed API key configured';
  const form = document.querySelector('#model-configuration-form');
  const clearKey = form.elements.clear_api_key;
  clearKey.disabled = configuration.api_key_source !== 'saved';
  const dialog = document.querySelector('#model-configuration-dialog');
  if (!dialog.open || !form.matches(':focus-within')) {
    populateModelConfigurationForm();
  }
}
function renderRepositoryList() {
  const list = document.querySelector('#repository-list');
  document.querySelector('#inventory-count').textContent = `${currentState.repositories.length} ${currentState.repositories.length === 1 ? 'repository' : 'repositories'}`;
  list.innerHTML = currentState.repositories.map(r => `
    <article class="repo-card ${String(r.id) === selectedRepositoryId ? 'selected' : ''}" data-repository="${esc(r.id)}" tabindex="0" aria-label="View ${esc(r.identity)}">
      <div class="row"><span class="repo-name">${esc(r.identity)}</span>${stateBadge(r)}</div>
      <div class="repo-meta">${esc(r.onboarding_state)} · ${r.active_run_count ? `${esc(r.active_run_count)} active run` : 'no active work'}<br>Autonomous mode: ${r.autonomous_mode ? 'Enabled' : 'Disabled'} · Updated ${esc(date(r.latest_activity_at))}</div>
      <div class="actions">
        <button class="button small" data-enabled="${esc(r.id)}" data-next-enabled="${r.enabled ? 'false' : 'true'}">${r.enabled ? 'Pause' : 'Resume'}</button>
        <button class="button small" data-autonomous="${esc(r.id)}" data-next-autonomous="${r.autonomous_mode ? 'false' : 'true'}">${r.autonomous_mode ? 'Disable autonomous mode' : 'Enable autonomous mode'}</button>
        <button class="button small" data-reonboard="${esc(r.id)}">Re-onboard</button>
        <button class="button small danger" data-remove="${esc(r.id)}" ${r.active ? 'disabled title="Cancel or finish active work before removal"' : ''}>Remove repository</button>
      </div>
    </article>`).join('') || '<p class="muted">No repositories. Add one above to begin onboarding.</p>';
}
function blockingError(value) {
  const diagnostic = String(value || '');
  const missingCredential = /Missing credentials|OPENAI_API_KEY|API key/i.test(diagnostic);
  const firstLine = diagnostic.split('\n', 1)[0];
  const bounded = firstLine.length > 240 ? `${firstLine.slice(0, 240)}…` : firstLine;
  const summary = missingCredential
    ? 'Model API credential is missing. Save it in Model provider settings, then Re-onboard this repository.'
    : bounded;
  return `<details class="blocking-error"><summary>${esc(summary)}</summary><pre>${esc(diagnostic)}</pre></details>`;
}
function renderRepositoryDetail() {
  const detail = document.querySelector('#repository-detail');
  const focusedWorkflowNode = document.activeElement?.dataset?.workflowNode || null;
  const previousWorkflow = detail.querySelector('#workflow-preview');
  const previousContext = previousWorkflow?.dataset.workflowContext;
  const previousViewportState = previousContext
    ? workflowViewportStates.get(previousContext)
    : null;
  const workflowViewportSnapshot = previousContext && previousViewportState
    ? {
        context: previousContext,
        state: {...previousViewportState},
      }
    : null;
  const repository = currentState.repositories.find(item => String(item.id) === selectedRepositoryId);
  if (!repository) {
    detail.innerHTML = '<div class="empty"><div><h2>Select a repository</h2><p>Choose an inventory item to view status and team.</p></div></div>';
    return;
  }
  const team = repository.team;
  detail.innerHTML = `
    <div class="row">
      <div><h2>${githubLink(repository.url, repository.identity)}</h2><p class="muted">Default branch: ${esc(repository.default_branch)}</p></div>
      <div class="actions">${stateBadge(repository)}<span class="badge">${esc(repository.onboarding_state)}</span></div>
    </div>
    ${repository.blocking_reason ? blockingError(repository.blocking_reason) : ''}
    <div class="status-grid">
      <div class="metric"><span>Scheduling</span><strong>${repository.enabled ? 'Running' : 'Paused'}</strong></div>
      <div class="metric"><span>Autonomous mode</span><strong>${repository.autonomous_mode ? 'Enabled' : 'Disabled'}</strong></div>
      <div class="metric"><span>Activity</span><strong>${repository.active ? 'Active' : 'Idle'}</strong></div>
      <div class="metric"><span>Current run</span><strong>${esc(repository.latest_run_state ?? 'None')}</strong></div>
      <div class="metric"><span>Last update</span><strong>${esc(date(repository.latest_activity_at))}</strong></div>
    </div>
    <section class="subpanel">
      <div class="row"><h3>Team</h3>${team ? `<span class="badge">v${esc(team.version)}</span>` : ''}</div>
      ${team ? (team.members || []).map(teamMember).join('') || '<p class="muted">No members stored.</p>' : '<p class="muted">No team exists yet.</p>'}
      <details><summary>Retained inputs</summary><pre class="prompt">${retainedInputs(repository)}</pre></details>
    </section>
    <section id="workflow-preview" class="subpanel workflow-preview" aria-labelledby="workflow-heading"></section>`;
  renderWorkflowGraph(repository, focusedWorkflowNode);
  const refreshedWorkflow = detail.querySelector('#workflow-preview');
  const refreshedGraph = refreshedWorkflow?.querySelector('#workflow-graph');
  const refreshedScene = refreshedWorkflow?.querySelector('.workflow-scene');
  if (
    workflowViewportSnapshot
    && refreshedWorkflow
    && refreshedGraph
    && refreshedScene
    && workflowViewportSnapshot.context
      === refreshedWorkflow.dataset.workflowContext
  ) {
    const restoredState = workflowViewportState(
      workflowViewportSnapshot.context
    );
    Object.assign(restoredState, workflowViewportSnapshot.state);
    workflowApplyViewport(
      refreshedGraph,
      refreshedScene,
      {
        width: Number.parseFloat(refreshedScene.style.width),
        height: Number.parseFloat(refreshedScene.style.height),
      },
      restoredState
    );
  }
}
function renderReadyIssues() {
  const inventory = document.querySelector('#ready-issues');
  const repository = currentState.repositories.find(item => String(item.id) === selectedRepositoryId);
  if (!repository) {
    inventory.innerHTML = '<p class="muted">Select a repository to view its agent:ready issues.</p>';
    return;
  }
  const discovery = (currentState.ready_issue_discovery || []).find(
    item => String(item.repository_id) === selectedRepositoryId
  );
  const issues = (currentState.ready_issues || []).filter(
    issue => String(issue.repository_id) === selectedRepositoryId
  );
  const status = discovery?.status || 'unavailable';
  const statusDetail = status === 'available'
    ? ''
    : `<p class="${status === 'stale' ? 'muted' : 'error'}">Inventory ${esc(status)}${discovery?.error ? `: ${esc(discovery.error)}` : ''}.</p>`;
  inventory.innerHTML = `${statusDetail}${issues.map(issue => `
    <article class="card">
      <span class="row"><strong>${githubLink(issue.url, `Issue #${issue.number}: ${issue.title}`)}</strong><span class="badge">agent:ready</span></span>
      <span class="run-card-line muted">Updated ${esc(date(issue.updated_at))}</span>
    </article>`).join('') || `<p class="muted">${status === 'available' ? 'No agent:ready issues for this repository.' : 'No current agent:ready inventory is available.'}</p>`}`;
}
const retryStatus = run => run.retry_next_at
  ? `<span class="run-card-line warning">Automatic retry #${esc(run.retry_attempt_count)} for ${esc(run.retry_operation || 'operation')} at ${esc(date(run.retry_next_at))}: ${esc(run.retry_last_error || 'Unknown error')}</span>`
  : '';
const recoveryButtons = run => {
  const unavailable = esc(run.retry_disabled_reason || 'Retry is currently unavailable.');
  const retry = run.retry_visible
    ? `<button class="button small" data-retry="${esc(run.id)}"${run.can_retry ? '' : ` disabled aria-disabled="true" title="${unavailable}"`}>Retry now</button>`
    : '';
  const prerequisite = run.retry_visible && !run.can_retry
    ? `<span class="muted">${unavailable}</span>`
    : '';
  return `${retry}${prerequisite}${run.can_restart ? `<button class="button small" data-restart="${esc(run.id)}">Restart issue</button>` : ''}`;
};

function runCard(run, reorderable) {
  return `
    <button type="button"
      class="card run-card ${String(run.id) === selectedRunId ? 'selected' : ''}"
      data-run-select="${esc(run.id)}" ${reorderable ? 'draggable="true"' : ''}
      aria-pressed="${String(run.id) === selectedRunId ? 'true' : 'false'}"
      aria-label="Select ${esc(run.repository)} issue ${esc(run.issue_number)}">
      <span class="row"><strong>${esc(run.repository)} · Issue #${esc(run.issue_number)}: ${esc(run.issue_title)}</strong><span class="badge">${esc(run.state)}</span></span>
      <span class="run-card-line">Last completed: ${esc(run.last_completed_state ?? 'none')}${run.pull_number ? ` · Pull request #${esc(run.pull_number)}` : ''}${run.forced ? ' · Forced work' : ''}</span>
      ${run.reason ? `<span class="run-card-line ${run.reason_severity === 'error' ? 'error' : 'muted'}">${esc(run.reason)}${run.reason_truncated ? ' · Open Issue Log for full details.' : ''}</span>` : ''}
      ${retryStatus(run)}
      <span class="run-card-line${reorderable ? ' drag-hint' : ' muted'}">Priority ${esc(run.queue_position)}${reorderable ? ' · Drag to reorder · Alt+Arrow to move' : ''}</span>
    </button>`;
}
function historyCard(run) {
  return `
    <article class="card run-card ${String(run.id) === selectedRunId ? 'selected' : ''}">
      <span class="row"><strong>${esc(run.repository)} · Issue #${esc(run.issue_number)}: ${esc(run.issue_title)}</strong><span class="badge">${esc(run.state)}</span></span>
      <span class="run-card-line">Updated ${esc(date(run.updated_at))}${run.pull_number ? ` · Pull request #${esc(run.pull_number)}` : ''}</span>
      ${run.reason ? `<span class="run-card-line ${run.reason_severity === 'error' ? 'error' : 'muted'}">${esc(run.reason)}${run.reason_truncated ? ' · Open Issue Log for full details.' : ''}</span>` : ''}
      ${retryStatus(run)}
      <div class="actions">
        <button class="button small" data-run-select="${esc(run.id)}">Open Issue Log</button>
        ${recoveryButtons(run)}
      </div>
    </article>`;
}
function renderRuns() {
  renderReadyIssues();
  const activeRuns = currentState.runs.filter(run => !['blocked', 'canceled', 'closed'].includes(String(run.state)));
  const repositoryRuns = activeRuns.filter(run => String(run.repository_id) === selectedRepositoryId);
  const historyRuns = [
    ...currentState.runs.filter(run => String(run.state) === 'blocked'),
    ...(currentState.run_history || []),
  ].filter(run => String(run.repository_id) === selectedRepositoryId);
  document.querySelector('#runs').innerHTML = repositoryRuns.map(run => runCard(run, false)).join('')
    || `<p class="muted">${selectedRepositoryId ? 'No active issue runs for this repository.' : 'Select a repository to view its active issue runs.'}</p>`;
  document.querySelector('#run-history').innerHTML = historyRuns.map(historyCard).join('')
    || `<p class="muted">${selectedRepositoryId ? 'No blocked or finished runs for this repository.' : 'Select a repository to view its run history.'}</p>`;
  document.querySelector('#all-active-runs').innerHTML = activeRuns.map(run => runCard(run, true)).join('')
    || '<p class="muted">No active issue runs.</p>';
  renderIssueLogDetails();
}
function renderIssueLogDetails() {
  const details = document.querySelector('#issue-log-details');
  const run = allRuns().find(item => String(item.id) === selectedRunId);
  if (!run) {
    document.querySelector('#issue-log-identity').textContent = 'Select an issue to view its live log.';
    document.querySelector('#issue-log-state').textContent = 'Idle';
    details.innerHTML = '<p class="muted">No issue selected.</p>';
    return;
  }
  document.querySelector('#issue-log-identity').textContent = `${run.repository} · Issue #${run.issue_number}: ${run.issue_title}`;
  document.querySelector('#issue-log-state').textContent = run.forced ? `${run.state} · Forced` : run.state;
  details.innerHTML = `
    <div class="row">
      <div class="actions">
        ${githubLink(run.issue_url, `Open issue #${run.issue_number}`)}
        ${run.pull_url ? githubLink(run.pull_url, `Open pull request #${run.pull_number}`) : ''}
      </div>
      <div class="actions">
        ${actionableRun(run) ? `<button class="button small" data-force-run="${esc(run.id)}" data-next-forced="${run.forced ? 'false' : 'true'}">${run.forced ? 'Release forced work' : 'Force work on this issue'}</button>` : ''}
        ${recoveryButtons(run)}
        ${!['canceled','closed'].includes(run.state) ? `<button class="button small danger" data-cancel="${esc(run.id)}">Cancel</button>` : ''}
      </div>
    </div>
    ${evidence(run)}
    ${acceptanceEvidence(run.acceptance_verification)}
    ${issueSpecification(run)}`;
}
function renderRunLogSnapshot(snapshot, runId) {
  if (runId !== selectedRunId) return;
  const issue = snapshot.issue || {};
  document.querySelector('#issue-log-identity').textContent = `${snapshot.repository || 'Repository'} · Issue #${issue.number ?? '—'}: ${issue.title || 'Untitled issue'} · run ${snapshot.run_id || runId}`;
  const log = document.querySelector('#issue-live-log');
  const following = log.scrollHeight - log.scrollTop - log.clientHeight < 32;
  const content = (snapshot.entries || []).map(entry => {
    const timestamp = entry.timestamp ? date(entry.timestamp) : 'now';
    return `[${timestamp}] ${String(entry.kind || 'event').toUpperCase()}\n${entry.message || ''}`;
  }).join('\n\n') || 'No activity recorded yet.';
  if (log.textContent !== content) {
    log.textContent = content;
    if (following) requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  }
  document.querySelector('#issue-log-state').textContent = snapshot.active ? `${snapshot.state} · Live` : `${snapshot.state} · Latest`;
}
function closeRunActivityStream() {
  if (runActivityStream) runActivityStream.close();
  runActivityStream = null;
  runActivityStreamRunId = null;
}
function connectRunActivityStream(runId) {
  if (
    runActivityStream
    && runActivityStreamRunId === runId
    && runActivityStream.readyState !== EventSource.CLOSED
  ) return;
  closeRunActivityStream();
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  runActivityStream = source;
  runActivityStreamRunId = runId;
  source.addEventListener('activity', event => {
    if (source !== runActivityStream || runId !== selectedRunId) return;
    try {
      renderRunLogSnapshot(JSON.parse(event.data), runId);
    } catch (failure) {
      error.textContent = `Issue run activity: ${failure.message}`;
    }
  });
  source.onopen = () => {
    if (source === runActivityStream) document.querySelector('#issue-log-state').textContent = 'Connected';
  };
  source.onerror = () => {
    if (source === runActivityStream) document.querySelector('#issue-log-state').textContent = 'Reconnecting';
  };
}
function selectRun(runId) {
  const normalized = String(runId);
  const run = allRuns().find(item => String(item.id) === normalized);
  if (!run) return;
  selectedRunId = normalized;
  selectedWorkflowGeneration = null;
  selectedWorkflowNode = null;
  renderRepositoryDetail();
  renderRuns();
  document.querySelector('#issue-live-log').textContent = 'Loading issue activity…';
  connectRunActivityStream(normalized);
}
async function refresh() {
  try {
    const response = await fetch('/api/state', {cache:'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    currentState = await response.json();
    displayInputs.clear();
    currentState.repositories.forEach(r => displayInputs.set(String(r.id), r.display_inputs));
    if (!currentState.repositories.some(r => String(r.id) === selectedRepositoryId)) {
      selectedRepositoryId = currentState.repositories.length ? String(currentState.repositories[0].id) : null;
    }
    if (
      selectedRunId
      && !allRuns().some(run => String(run.id) === selectedRunId)
    ) {
      selectedRunId = null;
      selectedWorkflowGeneration = null;
      selectedWorkflowNode = null;
      closeRunActivityStream();
      document.querySelector('#issue-live-log').textContent = 'Select an issue to view its activity.';
    }
    renderModelConfiguration();
    renderRepositoryList();
    renderRepositoryDetail();
    renderRuns();
    if (selectedRunId) connectRunActivityStream(selectedRunId);
    document.querySelector('#connection').textContent = 'Connected';
    error.textContent = '';
  } catch (failure) {
    document.querySelector('#connection').textContent = 'Unavailable';
    error.textContent = failure.message;
  }
}
async function action(fn) {
  try {
    await fn();
    await refresh();
  } catch (failure) {
    error.textContent = failure.message;
  }
}
function selectRepository(repositoryId) {
  selectedRepositoryId = String(repositoryId);
  selectedRunId = null;
  selectedWorkflowGeneration = null;
  selectedWorkflowNode = null;
  renderRepositoryList();
  renderRepositoryDetail();
  renderReadyIssues();
  renderRuns();
}
const configurationDialog = document.querySelector('#model-configuration-dialog');
document.querySelector('#open-model-configuration').addEventListener('click', () => {
  populateModelConfigurationForm();
  document.querySelector('#configuration-result').textContent = '';
  configurationDialog.showModal();
  loadModelCatalog();
});
document.querySelector('#close-model-configuration').addEventListener('click', () => {
  configurationDialog.close();
});
document.querySelector('#reload-model-catalog').addEventListener('click', loadModelCatalog);
document.querySelector('#model-configuration-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const result = document.querySelector('#configuration-result');
  result.className = '';
  result.textContent = '';
  let saved;
  try {
    saved = await mutate('/api/model-configuration', {
      api_endpoint: form.elements.api_endpoint.value,
      api_key: form.elements.api_key.value,
      default_model: catalogTransportValue(form.elements.default_model.value),
      lead_model: catalogTransportValue(form.elements.lead_model.value),
      implementer_model: catalogTransportValue(form.elements.implementer_model.value),
      verifier_model: catalogTransportValue(form.elements.verifier_model.value),
      clear_api_key: form.elements.clear_api_key.checked,
    });
  } catch (failure) {
    result.className = 'error';
    result.textContent = boundedMessage(failure.message);
    return;
  }
  form.elements.api_key.value = '';
  form.elements.clear_api_key.checked = false;
  currentState.model_configuration = saved;
  renderModelConfiguration();
  result.textContent = 'Settings saved. Re-onboard repositories to apply model changes.';
  await refresh();
  await loadModelCatalog();
});
document.querySelector('#add').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    const form = new FormData(event.target);
    await mutate('/api/repositories', {repository:form.get('repository'), inputs:JSON.parse(form.get('inputs'))});
    event.target.reset();
    event.target.elements.inputs.value = '{}';
  });
});
document.querySelector('#poll').addEventListener('click', () => action(() => mutate('/api/poll', {})));
document.body.addEventListener('click', event => {
  const b = event.target.closest('button');
  if (b?.dataset.runSelect) {
    if (suppressRunClick) {
      suppressRunClick = false;
      return;
    }
    selectRun(b.dataset.runSelect);
    return;
  }
  if (b?.dataset.enabled) {
    return action(() => mutate(`/api/repositories/${encodeURIComponent(b.dataset.enabled)}/enabled`, {enabled:b.dataset.nextEnabled === 'true'}));
  }
  if (b?.dataset.autonomous) {
    return action(() => mutate(
      `/api/repositories/${encodeURIComponent(b.dataset.autonomous)}/autonomous`,
      {autonomous:b.dataset.nextAutonomous === 'true'},
    ));
  }
  if (b?.dataset.reonboard) {
    return action(() => {
      const raw = prompt('Repository inputs JSON object:', JSON.stringify(displayInputs.get(b.dataset.reonboard), null, 2));
      if (raw === null) return Promise.resolve();
      return mutate(`/api/repositories/${encodeURIComponent(b.dataset.reonboard)}/reonboard`, {inputs:JSON.parse(raw)});
    });
  }
  if (b?.dataset.remove) {
    if (!confirm('Remove this repository from active inventory? Its durable history will be preserved.')) return;
    return action(() => mutate(`/api/repositories/${encodeURIComponent(b.dataset.remove)}/remove`, {}));
  }
  if (b?.dataset.forceRun) {
    return action(() => mutate(
      `/api/runs/${encodeURIComponent(b.dataset.forceRun)}/force`,
      {forced:b.dataset.nextForced === 'true'},
    ));
  }
  if (b?.dataset.retry) {
    return action(() => mutate(`/api/runs/${encodeURIComponent(b.dataset.retry)}/retry`, {}));
  }
  if (b?.dataset.restart) {
    if (!confirm('Restart this issue as a new run?')) return;
    return action(async () => {
      const result = await mutate(`/api/runs/${encodeURIComponent(b.dataset.restart)}/restart`, {});
      selectedRunId = String(result.run_id);
    });
  }
  if (b?.dataset.cancel) {
    if (!confirm('Cancel this run?')) return;
    return action(() => mutate(`/api/runs/${encodeURIComponent(b.dataset.cancel)}/cancel`, {}));
  }
  const card = event.target.closest('[data-repository]');
  if (card && !event.target.closest('a, details')) selectRepository(card.dataset.repository);
});
const runList = document.querySelector('#all-active-runs');
runList.addEventListener('dragstart', event => {
  const card = event.target.closest('[data-run-select]');
  if (!card) return;
  draggedRunId = card.dataset.runSelect;
  suppressRunClick = true;
  card.classList.add('dragging');
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', draggedRunId);
});
runList.addEventListener('dragover', event => {
  if (!draggedRunId) return;
  const target = event.target.closest('[data-run-select]');
  if (!target || target.dataset.runSelect === draggedRunId) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
});
runList.addEventListener('drop', event => {
  const target = event.target.closest('[data-run-select]');
  const dragged = runList.querySelector(`[data-run-select="${CSS.escape(draggedRunId || '')}"]`);
  if (!target || !dragged || target === dragged) return;
  event.preventDefault();
  const before = event.clientY < target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
  runList.insertBefore(dragged, before ? target : target.nextSibling);
  const runIds = [...runList.querySelectorAll('[data-run-select]')].map(card => card.dataset.runSelect);
  action(() => mutate('/api/runs/priority', {run_ids:runIds}));
});
runList.addEventListener('dragend', () => {
  runList.querySelectorAll('.dragging').forEach(card => card.classList.remove('dragging'));
  draggedRunId = null;
  setTimeout(() => { suppressRunClick = false; }, 0);
});

document.body.addEventListener('keydown', event => {
  const run = event.target.closest('#all-active-runs [data-run-select]');
  if (run && event.altKey && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
    const sibling = event.key === 'ArrowUp' ? run.previousElementSibling : run.nextElementSibling;
    if (!sibling?.matches('[data-run-select]')) return;
    event.preventDefault();
    if (event.key === 'ArrowUp') runList.insertBefore(run, sibling);
    else runList.insertBefore(sibling, run);
    const runIds = [...runList.querySelectorAll('[data-run-select]')].map(card => card.dataset.runSelect);
    action(() => mutate('/api/runs/priority', {run_ids:runIds}));
    run.focus();
    return;
  }
  const card = event.target.closest('[data-repository]');
  if (card && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault();
    selectRepository(card.dataset.repository);
  }
});

window.addEventListener('beforeunload', () => {
  closeRunActivityStream();
});
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""
