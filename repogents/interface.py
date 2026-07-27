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
            if (
                len(segments) == 4
                and segments[:2] == ["api", "repositories"]
                and segments[3] == "artifacts"
            ):
                name = payload.get("name")
                description = payload.get("description", "")
                encoded = payload.get("content_base64")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("artifact name must be a nonempty string")
                if not isinstance(description, str):
                    raise ValueError("artifact description must be a string")
                if not isinstance(encoded, str) or not encoded:
                    raise ValueError("artifact content is required")
                try:
                    content = __import__("base64").b64decode(encoded, validate=True)
                except Exception as error:
                    raise ValueError("artifact content must be valid base64") from error
                artifact = self.actions.upload_repository_artifact(
                    segments[2],
                    name=name.strip(),
                    description=description,
                    content=content,
                )
                self._send_json(request, HTTPStatus.CREATED, artifact)
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "repositories"]
                and segments[3] == "secrets"
            ):
                name = payload.get("name")
                action = payload.get("action", "preserve")
                value = payload.get("value", "")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("secret name must be a nonempty string")
                if not isinstance(action, str):
                    raise ValueError("secret action must be a string")
                if not isinstance(value, str):
                    raise ValueError("secret value must be a string")
                secret = self.actions.update_repository_secret(
                    segments[2], name=name.strip(), action=action, value=value
                )
                self._send_json(request, HTTPStatus.OK, secret)
                return
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
                and segments[3] == "artifacts"
            ):
                name = payload.get("name")
                revision = payload.get("revision")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("artifact name must be a nonempty string")
                if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                    raise ValueError("artifact revision must be a positive integer")
                self.actions.remove_repository_artifact(
                    segments[2], name=name.strip(), revision=revision
                )
                self._send_json(request, HTTPStatus.OK, {"ok": True})
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
              <div class="resources-editor" data-resources-editor>
                <fieldset><legend>Artifacts</legend><p class="muted">Upload licensed SDKs, fixtures, or other binary resources. Artifacts are mounted read-only and pinned when the repository is onboarded.</p><div data-resource-list="artifacts"></div><button type="button" class="button small" data-add-resource="artifact">Add artifact</button></fieldset>
                <fieldset><legend>Variables</legend><p class="muted">Non-secret environment values scoped to selected provisioning or validation commands.</p><div data-resource-list="variables"></div><button type="button" class="button small" data-add-resource="variable">Add variable</button></fieldset>
                <fieldset><legend>Secrets</legend><p class="muted">Values are write-only. Blank values preserve configured secrets; use Replace or Remove explicitly.</p><div data-resource-list="secrets"></div><button type="button" class="button small" data-add-resource="secret">Add secret</button></fieldset>
                <fieldset><legend>Permitted network services</legend><div data-resource-list="services"></div><button type="button" class="button small" data-add-resource="service">Add service</button></fieldset>
                <fieldset><legend>Provisioning commands</legend><div data-resource-list="provisioning"></div><button type="button" class="button small" data-add-resource="provisioning">Add command</button></fieldset>
                <fieldset><legend>Validation commands</legend><div data-resource-list="validation"></div><button type="button" class="button small" data-add-resource="validation">Add command</button></fieldset>
              </div>
              <details class="advanced-inputs"><summary>Advanced JSON view</summary><textarea name="inputs" aria-label="Advanced repository inputs JSON">{}</textarea></details>
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
let suppressRunClick = false;
let modelCatalog = {available:false, reason:'Not loaded', models:[]};
const displayInputs = new Map();
const resourceEditor = document.querySelector('[data-resources-editor]');
const resourceTemplates = {
  artifact: () => `<div class="resource-row" data-resource-row="artifact"><label>Stable name <input data-field="name" required pattern="[A-Za-z0-9][A-Za-z0-9._-]*"></label><label>Description <input data-field="description" required></label><label>Sandbox path <input data-field="sandbox_path" required value="/repository-resources/artifacts/" pattern="/repository-resources/artifacts/.+" title="Use a path below /repository-resources/artifacts/" aria-describedby="artifact-path-help"></label><span class="muted" id="artifact-path-help">Artifacts are mounted read-only below /repository-resources/artifacts/.</span><label>Artifact file <input data-field="file" type="file" required></label><button type="button" class="button small" data-remove-resource>Remove</button></div>`,
  variable: () => `<div class="resource-row" data-resource-row="variable"><label>Name <input data-field="name" required pattern="[A-Za-z_][A-Za-z0-9_]*"></label><label>Value <input data-field="value"></label><label>Command scopes <input data-field="commands" placeholder="one command per line"></label><button type="button" class="button small" data-remove-resource>Remove</button></div>`,
  secret: () => `<div class="resource-row" data-resource-row="secret"><label>Name <input data-field="name" required pattern="[A-Za-z_][A-Za-z0-9_]*"></label><span class="badge" data-field="configured_status">Not configured</span><label>Value <input data-field="value" type="password" autocomplete="new-password" placeholder="Blank preserves current value"></label><label>Action <select data-field="action"><option value="preserve">Preserve</option><option value="replace">Replace</option><option value="remove">Remove</option></select></label><label>Command scopes <input data-field="commands" placeholder="one command per line"></label><button type="button" class="button small" data-remove-resource>Remove</button></div>`,
  service: () => `<div class="resource-row" data-resource-row="service"><label>Host and port <input data-field="value" required placeholder="vendor.example:443"></label><button type="button" class="button small" data-remove-resource>Remove</button></div>`,
  provisioning: () => `<div class="resource-row" data-resource-row="provisioning"><label>Command arguments (JSON array) <input data-field="value" required placeholder='["python","provision.py"]'></label><button type="button" class="button small" data-remove-resource>Remove</button></div>`,
  validation: () => `<div class="resource-row" data-resource-row="validation"><label>Command arguments (JSON array) <input data-field="value" required placeholder='["python","-m","unittest"]'></label><button type="button" class="button small" data-remove-resource>Remove</button></div>`,
};
const resourceFieldError = (control, message) => {
  control.setCustomValidity(message);
  control.reportValidity();
  throw new Error(message);
};
const parsedJsonField = (control, description) => {
  control.setCustomValidity('');
  try {
    return JSON.parse(control.value);
  } catch (_failure) {
    return resourceFieldError(control, `${description} must be valid JSON.`);
  }
};
const commandRows = (control, description = 'Command scopes') => String(control.value || '').split('\n').map(line => line.trim()).filter(Boolean).map((line, index) => {
  let command;
  try {
    command = JSON.parse(line);
  } catch (_failure) {
    return resourceFieldError(control, `${description} line ${index + 1} must be a JSON array.`);
  }
  if (!Array.isArray(command) || !command.length || command.some(argument => typeof argument !== 'string' || !argument)) {
    return resourceFieldError(control, `${description} line ${index + 1} must be a non-empty JSON array of non-empty strings.`);
  }
  control.setCustomValidity('');
  return command;
});
const resourceDraft = editor => {
  const form = editor.closest('form');
  const advancedControl = form.elements.inputs;
  const advanced = parsedJsonField(advancedControl, 'Advanced repository inputs');
  if (!advanced || Array.isArray(advanced) || typeof advanced !== 'object') {
    return resourceFieldError(advancedControl, 'Advanced repository inputs must be a JSON object.');
  }
  const rows = kind => [...editor.querySelectorAll(`[data-resource-row="${kind}"]`)];
  const control = (row, name) => row.querySelector(`[data-field="${name}"]`);
  const field = (row, name) => control(row, name)?.value ?? '';
  for (const input of editor.querySelectorAll('input, select, textarea')) input.setCustomValidity('');
  if (!form.checkValidity()) {
    form.reportValidity();
    throw new Error('Correct the highlighted resource fields.');
  }
  advanced.allowed_services = rows('service').map(row => field(row, 'value').trim()).filter(Boolean);
  advanced.provisioning_commands = rows('provisioning').map((row, index) => {
    const commandControl = control(row, 'value');
    const command = parsedJsonField(commandControl, `Provisioning command ${index + 1}`);
    if (!Array.isArray(command) || !command.length || command.some(argument => typeof argument !== 'string' || !argument)) {
      return resourceFieldError(commandControl, `Provisioning command ${index + 1} must be a non-empty JSON array of non-empty strings.`);
    }
    return command;
  });
  advanced.validation_commands = rows('validation').map((row, index) => {
    const commandControl = control(row, 'value');
    const command = parsedJsonField(commandControl, `Validation command ${index + 1}`);
    if (!Array.isArray(command) || !command.length || command.some(argument => typeof argument !== 'string' || !argument)) {
      return resourceFieldError(commandControl, `Validation command ${index + 1} must be a non-empty JSON array of non-empty strings.`);
    }
    return command;
  });
  advanced.variable_bindings = rows('variable').map(row => ({
    name:field(row, 'name').trim(),
    value:field(row, 'value'),
    commands:commandRows(control(row, 'commands')),
  }));
  delete advanced.artifact_uploads;
  const artifacts = rows('artifact').map(row => {
    const pathControl = control(row, 'sandbox_path');
    const sandboxPath = field(row, 'sandbox_path').trim();
    if (!sandboxPath.startsWith('/repository-resources/artifacts/') || sandboxPath === '/repository-resources/artifacts/') {
      return resourceFieldError(pathControl, 'Artifact sandbox path must be below /repository-resources/artifacts/.');
    }
    return {
      name:field(row, 'name').trim(),
      description:field(row, 'description'),
      sandbox_path:sandboxPath,
      revision:Number(row.dataset.revision || 0),
      file:control(row, 'file')?.files?.[0] || null,
    };
  });
  const secrets = rows('secret').map(row => ({
    name:field(row, 'name').trim(),
    value:field(row, 'value'),
    action:field(row, 'action'),
    commands:commandRows(control(row, 'commands')),
  }));
  // Keep advanced secret:// bindings so environment-backed references survive structured re-onboarding.
  // Repository-managed secret rows are merged explicitly by structuredInputs below.
  delete advanced.artifact_bindings;
  return {advanced, artifacts, secrets};
};
const structuredInputs = (editor, repositoryId = '', artifactBindings = []) => {
  const draft = resourceDraft(editor);
  draft.advanced.artifact_bindings = artifactBindings;
  const legacySecretBindings = (draft.advanced.secret_bindings || []).filter(binding =>
    !String(binding?.reference || '').startsWith('secret://repository/')
  );
  const managedSecretBindings = draft.secrets.map(binding => ({
    name:binding.name,
    reference:`secret://repository/${repositoryId}/${binding.name.toLowerCase()}`,
    commands:binding.commands,
  }));
  draft.advanced.secret_bindings = [...legacySecretBindings, ...managedSecretBindings];
  return draft.advanced;
};
let reonboardRepositoryId = null;
const hydrateResources = (editor, inputs = {}) => {
  editor.querySelectorAll('[data-resource-row]').forEach(row => row.remove());
  const append = (kind, values) => {
    const listName = kind === 'artifact' ? 'artifacts' : kind === 'variable' ? 'variables' : kind === 'secret' ? 'secrets' : kind === 'service' ? 'services' : kind;
    const list = editor.querySelector(`[data-resource-list="${listName}"]`);
    if (!list) return;
    list.insertAdjacentHTML('beforeend', resourceTemplates[kind]());
    const row = list.lastElementChild;
    if (kind === 'artifact' && Number.isInteger(values.revision)) {
      row.dataset.revision = String(values.revision);
      const fileControl = row.querySelector('[data-field="file"]');
      if (fileControl) fileControl.required = false;
    }
    for (const [name, value] of Object.entries(values)) {
      const control = row.querySelector(`[data-field="${name}"]`);
      if (!control || control.type === 'file') continue;
      if ('value' in control) control.value = value ?? '';
      else control.textContent = value ?? '';
    }
  };
  for (const value of inputs.allowed_services || []) append('service', {value});
  for (const value of inputs.provisioning_commands || []) append('provisioning', {value:JSON.stringify(value)});
  for (const value of inputs.validation_commands || []) append('validation', {value:JSON.stringify(value)});
  for (const binding of inputs.variable_bindings || []) append('variable', {
    name:binding.name, value:binding.value, commands:(binding.commands || []).map(JSON.stringify).join('\n'),
  });
  const secretStatus = new Map((inputs.resource_secrets || []).map(secret => [secret.name, Boolean(secret.configured)]));
  for (const binding of inputs.secret_bindings || []) {
    if (!String(binding.reference || '').startsWith('secret://repository/')) continue;
    append('secret', {
      name:binding.name, value:'', action:'preserve',
      configured_status:secretStatus.get(binding.name) ? 'Configured' : 'Not configured',
      commands:(binding.commands || []).map(JSON.stringify).join('\n'),
    });
  }
  for (const artifact of inputs.artifact_bindings || []) append('artifact', {
    name:artifact.name, description:artifact.description || '',
    sandbox_path:artifact.sandbox_path, revision:artifact.revision,
  });
  const {resource_secrets: _resourceSecrets, ...advancedInputs} = inputs;
  editor.closest('form').elements.inputs.value = JSON.stringify(advancedInputs, null, 2);
};
resourceEditor?.addEventListener('click', event => {
  const add = event.target.closest('[data-add-resource]');
  if (add) {
    const kind = add.dataset.addResource;
    const list = resourceEditor.querySelector(`[data-resource-list="${kind === 'artifact' ? 'artifacts' : kind === 'variable' ? 'variables' : kind === 'secret' ? 'secrets' : kind === 'service' ? 'services' : kind}"]`);
    list?.insertAdjacentHTML('beforeend', resourceTemplates[kind]());
    list?.lastElementChild?.querySelector('input,select')?.focus();
  }
  const remove = event.target.closest('[data-remove-resource]');
  if (remove) {
    const row = remove.closest('[data-resource-row]');
    const revision = Number(row?.dataset.revision || 0);
    const name = row?.querySelector('[data-field="name"]')?.value?.trim() || '';
    if (revision && reonboardRepositoryId) {
      if (!confirm(`Remove artifact ${name} revision ${revision} from durable storage? Referenced revisions cannot be removed.`)) return;
      return action(async () => {
        await mutate(`/api/repositories/${encodeURIComponent(reonboardRepositoryId)}/artifacts`, {name, revision});
        row?.remove();
      });
    }
    row?.remove();
  }
});
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
const stateBadge = r => {
  if (!r.enabled) return '<span class="badge warning">Paused</span>';
  if (r.active) return '<span class="badge success">Active</span>';
  return '<span class="badge">Idle</span>';
};
const teamMember = m => `<details class="team-member"><summary>${esc(m.stable_key)} <span class="badge">${esc(m.role)}</span></summary><p>${esc(m.responsibilities)}</p><p class="muted">${esc(m.runtime)} · ${esc(m.model)}</p><h4>Role prompt</h4><pre class="prompt">${esc(m.instructions)}</pre></details>`;
const retainedInputs = r => esc(JSON.stringify(r.display_inputs, null, 2));
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
      <div class="repo-meta">${esc(r.onboarding_state)} · ${r.active_run_count ? `${esc(r.active_run_count)} active run` : 'no active work'}<br>Updated ${esc(date(r.latest_activity_at))}</div>
      <div class="actions">
        <button class="button small" data-enabled="${esc(r.id)}" data-next-enabled="${r.enabled ? 'false' : 'true'}">${r.enabled ? 'Pause' : 'Resume'}</button>
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
      <div class="metric"><span>Activity</span><strong>${repository.active ? 'Active' : 'Idle'}</strong></div>
      <div class="metric"><span>Current run</span><strong>${esc(repository.latest_run_state ?? 'None')}</strong></div>
      <div class="metric"><span>Last update</span><strong>${esc(date(repository.latest_activity_at))}</strong></div>
    </div>
    <section class="subpanel">
      <div class="row"><h3>Team</h3>${team ? `<span class="badge">v${esc(team.version)}</span>` : ''}</div>
      ${team ? (team.members || []).map(teamMember).join('') || '<p class="muted">No members stored.</p>' : '<p class="muted">No team exists yet.</p>'}
      <details><summary>Retained inputs</summary><pre class="prompt">${retainedInputs(repository)}</pre></details>
    </section>`;
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
    ${acceptanceEvidence(run.acceptance_verification)}`;
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
const fileBase64 = file => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onerror = () => reject(new Error(`Could not read artifact ${file.name}.`));
  reader.onload = () => resolve(String(reader.result || '').split(',', 2)[1] || '');
  reader.readAsDataURL(file);
});
document.querySelector('#add').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    const form = event.target;
    const draft = resourceDraft(resourceEditor);
    let repositoryId = reonboardRepositoryId;
    if (!repositoryId) {
      const created = await mutate('/api/repositories', {
        repository:form.elements.repository.value,
        inputs:{...draft.advanced, _defer_resource_onboarding:true},
      });
      repositoryId = created.repository_id;
    }
    const artifactBindings = [];
    for (const artifact of draft.artifacts) {
      let revision = artifact.revision;
      if (artifact.file) {
        const uploaded = await mutate(`/api/repositories/${encodeURIComponent(repositoryId)}/artifacts`, {
          name:artifact.name,
          description:artifact.description,
          content_base64:await fileBase64(artifact.file),
        });
        revision = uploaded.revision;
      }
      if (!revision) throw new Error(`Choose a file for artifact ${artifact.name}.`);
      artifactBindings.push({name:artifact.name, revision, sandbox_path:artifact.sandbox_path});
    }
    for (const secret of draft.secrets) {
      await mutate(`/api/repositories/${encodeURIComponent(repositoryId)}/secrets`, {
        name:secret.name, action:secret.action, value:secret.value,
      });
    }
    const inputs = structuredInputs(resourceEditor, repositoryId, artifactBindings);
    await mutate(`/api/repositories/${encodeURIComponent(repositoryId)}/reonboard`, {inputs});
    reonboardRepositoryId = null;
    form.reset();
    hydrateResources(resourceEditor, {});
    form.elements.repository.disabled = false;
    form.elements.repository.placeholder = 'GitHub URL or owner/repository';
    form.querySelector('button[type=submit]').textContent = 'Add repository';
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
  if (b?.dataset.reonboard) {
    reonboardRepositoryId = b.dataset.reonboard;
    const repository = currentState.repositories.find(item => String(item.id) === String(reonboardRepositoryId));
    const form = document.querySelector('#add');
    form.elements.repository.value = repository?.full_name || repository?.repository || '';
    form.elements.repository.disabled = true;
    hydrateResources(resourceEditor, {
      ...(displayInputs.get(b.dataset.reonboard) || {}),
      resource_secrets:repository?.resource_secrets || [],
    });
    form.querySelector('button[type="submit"]').textContent = 'Save resources and re-onboard';
    form.scrollIntoView({behavior:'smooth', block:'start'});
    resourceEditor.querySelector('input,select,textarea')?.focus();
    return;
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
