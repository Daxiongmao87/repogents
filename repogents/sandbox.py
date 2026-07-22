from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import tempfile
import urllib.parse
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitHub credential pattern", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("OpenAI credential pattern", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("AWS access-key pattern", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("private-key material", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
)


@dataclass(frozen=True)
class Mount:
    host_path: Path
    sandbox_path: str
    writable: bool = False

    def __post_init__(self) -> None:
        host = Path(self.host_path).expanduser().resolve()
        if not host.exists():
            raise ValueError(f"allowed host path does not exist: {host}")
        if not self.sandbox_path.startswith("/mnt/inputs/"):
            raise ValueError("repository host mounts must live below /mnt/inputs")
        if ".." in Path(self.sandbox_path).parts:
            raise ValueError("sandbox mount target cannot contain '..'")
        object.__setattr__(self, "host_path", host)


@dataclass(frozen=True)
class SandboxPolicy:
    persistent_root: Path
    cache_root: Path | None = None
    mounts: tuple[Mount, ...] = ()
    allowed_services: tuple[str, ...] = ()
    allowed_secret_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        root = Path(self.persistent_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"persistent sandbox root does not exist: {root}")
        cache = (
            Path(self.cache_root).expanduser().resolve()
            if self.cache_root is not None
            else root / "shared-cache"
        )
        if cache != root and root not in cache.parents:
            raise ValueError("shared cache root must live below persistent sandbox root")
        cache.mkdir(parents=True, exist_ok=True)
        targets = [mount.sandbox_path for mount in self.mounts]
        if len(targets) != len(set(targets)):
            raise ValueError("sandbox mount targets must be unique")
        for name in self.allowed_secret_names:
            if not _ENVIRONMENT_NAME.fullmatch(name):
                raise ValueError(f"invalid secret environment name: {name}")
        object.__setattr__(self, "persistent_root", root)
        object.__setattr__(self, "cache_root", cache)


@dataclass(frozen=True)
class RunLayout:
    repository_id: str
    run_id: str
    root: Path
    checkout: Path
    agent_state: Path
    logs: Path
    temp: Path
    validation: Path
    dependency_delta: Path
    build: Path

    @classmethod
    def create(cls, data_root: Path, repository_id: str, run_id: str) -> "RunLayout":
        if not repository_id or "/" in repository_id or not run_id or "/" in run_id:
            raise ValueError("repository and run identifiers must be nonempty path components")
        root = Path(data_root).resolve() / "repositories" / repository_id / "runs" / run_id
        paths = {
            "checkout": root / "checkout",
            "agent_state": root / "agent-state",
            "logs": root / "logs",
            "temp": root / "temp",
            "validation": root / "validation",
            "dependency_delta": root / "dependency-delta",
            "build": root / "build",
        }
        for path in (root, *paths.values()):
            path.mkdir(parents=True, exist_ok=True)
        return cls(repository_id=repository_id, run_id=run_id, root=root, **paths)


@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    canceled: bool
    log_path: Path
    network_log_path: Path
    started_at: str
    completed_at: str


class SecretScanner:
    def scan(self, text: str, known_secrets: Iterable[str] = ()) -> list[str]:
        findings: list[str] = []
        if any(secret and secret in text for secret in known_secrets):
            findings.append("known secret value")
        for name, pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(text):
                findings.append(name)
        return findings


def redact_text(text: str, known_secrets: Iterable[str]) -> str:
    redacted = text
    for secret in sorted({value for value in known_secrets if value}, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


class RestrictedNetworkPolicy:
    def __init__(self, allowed_services: Sequence[str]) -> None:
        self._rules = tuple(self._parse_rule(rule) for rule in allowed_services)

    @staticmethod
    def _parse_rule(rule: str) -> tuple[str, int]:
        value = rule.strip().lower().rstrip(".")
        if not value:
            raise ValueError("allowed service cannot be empty")
        host = value
        port = 443
        candidate_host, separator, candidate_port = value.rpartition(":")
        if separator and candidate_port.isdecimal():
            host = candidate_host
            port = int(candidate_port)
        if not host or not (1 <= port <= 65535):
            raise ValueError(f"invalid allowed service: {rule}")
        if host.startswith("*."):
            normalized = "*." + host[2:].encode("idna").decode("ascii")
        else:
            normalized = host.encode("idna").decode("ascii")
        return normalized, port

    def host_allowed(self, host: str, port: int) -> bool:
        try:
            normalized = host.strip().lower().rstrip(".").encode("idna").decode("ascii")
        except UnicodeError:
            return False
        for rule, allowed_port in self._rules:
            if port != allowed_port:
                continue
            if rule.startswith("*."):
                suffix = rule[1:]
                if normalized.endswith(suffix) and normalized != rule[2:]:
                    return True
            elif normalized == rule:
                return True
        return False

    @staticmethod
    def address_allowed(address: str) -> bool:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        return parsed.is_global and not parsed.is_multicast

    def resolve(self, host: str, port: int) -> list[tuple[int, tuple[object, ...], str]]:
        if not self.host_allowed(host, port):
            raise PermissionError(f"destination is not allowlisted: {host}:{port}")
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise ConnectionError(f"cannot resolve {host}: {error}") from error
        resolved: list[tuple[int, tuple[object, ...], str]] = []
        prohibited: list[str] = []
        seen: set[tuple[int, str]] = set()
        for info in infos:
            family = info[0]
            sockaddr = info[4]
            address = str(sockaddr[0])
            key = (family, address)
            if key in seen:
                continue
            seen.add(key)
            if not self.address_allowed(address):
                prohibited.append(address)
                continue
            resolved.append((family, sockaddr, address))
        if prohibited:
            raise PermissionError(
                f"destination resolved to prohibited address class: {','.join(sorted(prohibited))}"
            )
        if not resolved:
            raise PermissionError("destination has no permitted resolved address")
        return resolved


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _ThreadingUnixServer)
        policy: RestrictedNetworkPolicy = server.network_policy  # type: ignore[attr-defined]
        log_path: Path = server.network_log_path  # type: ignore[attr-defined]
        lock: threading.Lock = server.network_log_lock  # type: ignore[attr-defined]
        header, pending_payload = self._read_header()
        if not header:
            return
        first_line, *header_lines = header.split(b"\r\n")
        try:
            method_raw, target_raw, version = first_line.split(b" ", 2)
            method = method_raw.decode("ascii").upper()
            target = target_raw.decode("ascii")
            if method == "CONNECT":
                host, port = _split_authority(target, 443)
                rewritten = None
            else:
                parsed = urllib.parse.urlsplit(target)
                if parsed.scheme != "http" or not parsed.hostname:
                    raise ValueError("proxy requires an absolute HTTP URL or CONNECT")
                host = parsed.hostname
                port = parsed.port or 80
                path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                rewritten = (
                    b" ".join((method_raw, path.encode("ascii"), version))
                    + b"\r\n"
                    + b"\r\n".join(header_lines)
                    + b"\r\n\r\n"
                    + pending_payload
                )
            addresses = policy.resolve(host, port)
            upstream = _connect_resolved(addresses)
            _write_network_event(log_path, lock, host, port, "allowed", addresses[0][2])
        except Exception as error:
            host_value = locals().get("host", "unknown")
            port_value = int(locals().get("port", 0))
            _write_network_event(log_path, lock, str(host_value), port_value, "denied", str(error))
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            if method == "CONNECT":
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if pending_payload:
                    upstream.sendall(pending_payload)
            else:
                assert rewritten is not None
                upstream.sendall(rewritten)
            _relay(self.request, upstream)
        finally:
            upstream.close()

    def _read_header(self) -> tuple[bytes, bytes]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(8192)
            if not chunk:
                return b"", b""
            data.extend(chunk)
            if len(data) > 65536:
                self.request.sendall(
                    b"HTTP/1.1 431 Request Header Fields Too Large\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return b"", b""
        parts = bytes(data).partition(b"\r\n\r\n")
        return parts[0], parts[2]


class RestrictedProxy(AbstractContextManager["RestrictedProxy"]):
    def __init__(self, socket_path: Path, log_path: Path, policy: RestrictedNetworkPolicy) -> None:
        self.socket_path = socket_path
        self.log_path = log_path
        self.policy = policy
        self._server: _ThreadingUnixServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "RestrictedProxy":
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.log_path.touch(exist_ok=True)
        server = _ThreadingUnixServer(str(self.socket_path), _ProxyHandler)
        server.network_policy = self.policy  # type: ignore[attr-defined]
        server.network_log_path = self.log_path  # type: ignore[attr-defined]
        server.network_log_lock = threading.Lock()  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="repogents-egress", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.socket_path.unlink(missing_ok=True)


def _persistent_tool_paths(root: Path) -> tuple[str, ...]:
    tool_bin = root / "bin"
    tool_home = root / "home"
    tool_bin.mkdir(parents=True, exist_ok=True)
    tool_home.mkdir(parents=True, exist_ok=True)
    paths = [
        "/repository-state/bin",
        "/repository-state/home/.local/bin",
    ]
    for child in sorted(tool_home.iterdir(), key=lambda value: value.name):
        candidate = child / "bin"
        if candidate.is_dir():
            paths.append(
                f"/repository-state/home/{child.name}/bin"
            )
    return tuple(dict.fromkeys(paths))


def _seed_read_only_dependencies(
    source: Path,
    target: Path,
    sandbox_source: str,
) -> None:
    if not source.is_dir():
        return
    pending = [(source, target)]
    while pending:
        source_directory, target_directory = pending.pop()
        target_directory.mkdir(parents=True, exist_ok=True)
        for entry in source_directory.iterdir():
            target_entry = target_directory / entry.name
            if entry.is_dir() and not entry.is_symlink():
                if target_entry.is_symlink() or (
                    target_entry.exists() and not target_entry.is_dir()
                ):
                    continue
                pending.append((entry, target_entry))
                continue
            if target_entry.exists() or target_entry.is_symlink():
                continue
            relative = entry.relative_to(source).as_posix()
            target_entry.symlink_to(
                f"{sandbox_source.rstrip('/')}/{relative}"
            )


class SandboxManager:
    def __init__(self, *, bwrap: str = "bwrap") -> None:
        self.bwrap = bwrap
        self.package_root = Path(__file__).parent.resolve()
        self._active: dict[str, subprocess.Popen[bytes]] = {}
        self._active_lock = threading.Lock()
        if shutil.which(self.bwrap) is None:
            raise RuntimeError(f"Bubblewrap executable not found: {self.bwrap}")

    def build_command(
        self,
        policy: SandboxPolicy,
        layout: RunLayout,
        command: Sequence[str],
        *,
        proxy_socket: str | None = None,
        proxy_socket_host: Path | None = None,
        secrets: dict[str, str] | None = None,
        persistent_writable: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        if not command or any("\x00" in item for item in command):
            raise ValueError("sandbox command must contain non-NUL arguments")
        secret_values = secrets or {}
        unknown = set(secret_values) - set(policy.allowed_secret_names)
        if unknown:
            raise PermissionError(f"secrets are not authorized by sandbox policy: {','.join(sorted(unknown))}")
        for name, value in secret_values.items():
            if not _ENVIRONMENT_NAME.fullmatch(name) or "\x00" in value:
                raise ValueError("invalid secret binding")
        if not persistent_writable:
            for source, target, sandbox_source in (
                (
                    policy.persistent_root / "dependencies",
                    layout.dependency_delta,
                    "/repository-state/dependencies",
                ),
                (
                    policy.persistent_root / "python-packages",
                    layout.dependency_delta / "python",
                    "/repository-state/python-packages",
                ),
                (
                    policy.persistent_root / "node" / "node_modules",
                    layout.dependency_delta / "node" / "node_modules",
                    "/repository-state/node/node_modules",
                ),
            ):
                _seed_read_only_dependencies(
                    source, target, sandbox_source
                )
        python_delta = layout.dependency_delta / "python"
        node_delta = layout.dependency_delta / "node"
        node_modules_delta = node_delta / "node_modules"
        python_delta.mkdir(parents=True, exist_ok=True)
        node_modules_delta.mkdir(parents=True, exist_ok=True)

        argv = [
            self.bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--dir",
            "/etc",
        ]
        for source in ("/etc/ssl", "/etc/alternatives", "/etc/ld.so.cache", "/etc/localtime"):
            if Path(source).exists():
                argv.extend(("--ro-bind", source, source))
        argv.extend(
            (
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/home",
                "--dir",
                "/home/agent",
                "--ro-bind",
                str(self.package_root),
                "/opt/repogents",
                "--bind" if persistent_writable else "--ro-bind",
                str(policy.persistent_root),
                "/repository-state",
                "--bind",
                str(policy.cache_root),
                "/repository-cache",
                "--dir",
                "/run-data",
                "--bind",
                str(layout.temp),
                "/run-data/temp",
                "--bind",
                str(layout.dependency_delta),
                "/run-data/dependency-delta",
                "--bind",
                str(layout.build),
                "/run-data/build",
                "--bind",
                str(layout.checkout),
                "/workspace",
            )
        )
        argv.extend(("--bind", str(node_modules_delta), "/workspace/node_modules"))
        if proxy_socket_host is not None:
            if proxy_socket is None:
                raise ValueError("proxy socket mount requires a sandbox target")
            argv.extend(("--bind", str(proxy_socket_host), proxy_socket))
        for mount in policy.mounts:
            argv.extend(("--bind" if mount.writable else "--ro-bind", str(mount.host_path), mount.sandbox_path))
        python_environment = policy.persistent_root / "python-venv"
        runtime_path = ":".join(
            (
                *_persistent_tool_paths(policy.persistent_root),
                "/usr/local/sbin",
                "/usr/local/bin",
                "/usr/sbin",
                "/usr/bin",
                "/sbin",
                "/bin",
            )
        )
        if (python_environment / "bin" / "python3").exists():
            runtime_path = "/repository-state/python-venv/bin:" + runtime_path
        for key, value in (
            ("PATH", runtime_path),
            (
                "HOME",
                "/repository-state/home"
                if persistent_writable
                else "/home/agent",
            ),
            ("LANG", "C.UTF-8"),
            ("LC_ALL", "C.UTF-8"),
            ("TMPDIR", "/tmp"),
            ("PYTHONDONTWRITEBYTECODE", "1"),
            ("PYTHONPYCACHEPREFIX", "/run-data/build/python-cache"),
            ("PYTHONPATH", "/run-data/dependency-delta/python"),
            ("PIP_TARGET", "/run-data/dependency-delta/python"),
            ("XDG_CACHE_HOME", "/repository-cache/xdg"),
            ("PIP_CACHE_DIR", "/repository-cache/pip"),
            ("CARGO_HOME", "/repository-cache/cargo-home"),
            ("CARGO_TARGET_DIR", "/run-data/build/cargo"),
            ("GOCACHE", "/repository-cache/go-build"),
            ("GOMODCACHE", "/repository-cache/go-modules"),
            ("npm_config_cache", "/repository-cache/npm"),
            ("NODE_PATH", "/workspace/node_modules"),
            ("npm_config_prefix", "/run-data/dependency-delta/node"),
        ):
            argv.extend(("--setenv", key, value))
        if (python_environment / "bin" / "python3").exists():
            argv.extend(("--setenv", "VIRTUAL_ENV", "/repository-state/python-venv"))
        for name, value in sorted(secret_values.items()):
            argv.extend(("--setenv", name, value))
        argv.extend(("--chdir", "/workspace"))
        if proxy_socket is None:
            argv.extend(command)
        else:
            argv.extend(
                (
                    "/usr/bin/python3",
                    "/opt/repogents/proxy_bridge.py",
                    proxy_socket,
                    "--",
                    *command,
                )
            )
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/home/agent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        return argv, environment

    def run(
        self,
        policy: SandboxPolicy,
        layout: RunLayout,
        command: Sequence[str],
        *,
        timeout: float,
        secrets: dict[str, str] | None = None,
        persistent_writable: bool = False,
    ) -> SandboxResult:
        invocation_id = str(uuid.uuid4())
        log_path = layout.logs / f"command-{invocation_id}.json"
        network_log_path = layout.logs / f"network-{invocation_id}.jsonl"
        proxy_directory: Path | None = None
        proxy_socket_host: Path | None = None
        proxy_socket_sandbox = "/run-data/temp/restricted-proxy.sock"
        proxy: RestrictedProxy | None = None
        if policy.allowed_services:
            proxy_directory = Path(
                tempfile.mkdtemp(prefix="repogents-proxy-", dir="/tmp")
            )
            proxy_directory.chmod(0o700)
            proxy_socket_host = proxy_directory / "proxy.sock"
            proxy = RestrictedProxy(
                proxy_socket_host,
                network_log_path,
                RestrictedNetworkPolicy(policy.allowed_services),
            )
            proxy.__enter__()
        else:
            network_log_path.touch(exist_ok=True)
        started_at = _utc_now()
        timed_out = False
        canceled = False
        secret_values = tuple((secrets or {}).values())
        try:
            argv, environment = self.build_command(
                policy,
                layout,
                command,
                proxy_socket=proxy_socket_sandbox if proxy else None,
                proxy_socket_host=proxy_socket_host,
                secrets=secrets,
                persistent_writable=persistent_writable,
            )
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
            with self._active_lock:
                if layout.run_id in self._active:
                    process.kill()
                    raise RuntimeError(f"run already has an active sandbox process: {layout.run_id}")
                self._active[layout.run_id] = process
            try:
                stdout_raw, stderr_raw = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
                stdout_raw, stderr_raw = process.communicate()
            with self._active_lock:
                canceled = self._active.get(layout.run_id) is not process
            returncode = process.returncode
        finally:
            with self._active_lock:
                current = self._active.get(layout.run_id)
                if current is locals().get("process"):
                    self._active.pop(layout.run_id, None)
            if proxy is not None:
                proxy.__exit__(None, None, None)
            if proxy_directory is not None:
                shutil.rmtree(proxy_directory, ignore_errors=True)
        completed_at = _utc_now()
        stdout = redact_text(stdout_raw.decode("utf-8", "replace"), secret_values)
        stderr = redact_text(stderr_raw.decode("utf-8", "replace"), secret_values)
        record = {
            "command": list(command),
            "started_at": started_at,
            "completed_at": completed_at,
            "returncode": returncode,
            "timed_out": timed_out,
            "canceled": canceled,
            "stdout": stdout,
            "stderr": stderr,
        }
        log_path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        return SandboxResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            canceled=canceled,
            log_path=log_path,
            network_log_path=network_log_path,
            started_at=started_at,
            completed_at=completed_at,
        )

    def is_active(self, run_id: str) -> bool:
        with self._active_lock:
            return run_id in self._active

    def cancel(self, run_id: str) -> bool:
        with self._active_lock:
            process = self._active.pop(run_id, None)
        if process is None:
            return False
        self._terminate(process)
        return True

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)


def _split_authority(value: str, default_port: int) -> tuple[str, int]:
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            raise ValueError("invalid IPv6 authority")
        host = value[1:closing]
        port = int(value[closing + 2 :]) if value[closing + 1 :].startswith(":") else default_port
        return host, port
    host, separator, port_value = value.rpartition(":")
    if separator and port_value.isdecimal():
        return host, int(port_value)
    return value, default_port


def _connect_resolved(addresses: Sequence[tuple[int, tuple[object, ...], str]]) -> socket.socket:
    errors: list[OSError] = []
    for resolved in addresses:
        family, sockaddr = resolved[0], resolved[1]
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(15)
        try:
            connection.connect(sockaddr)
            connection.settimeout(None)
            return connection
        except OSError as error:
            errors.append(error)
            connection.close()
    raise ConnectionError(f"cannot connect to permitted destination: {errors[-1] if errors else 'no address'}")


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while sockets:
        readable, _, exceptional = select.select(sockets, [], sockets, 60)
        if exceptional or not readable:
            return
        for source in readable:
            destination = right if source is left else left
            data = source.recv(65536)
            if not data:
                return
            destination.sendall(data)


def _write_network_event(
    path: Path,
    lock: threading.Lock,
    host: str,
    port: int,
    decision: str,
    detail: str,
) -> None:
    event = {
        "time": _utc_now(),
        "host": host,
        "port": port,
        "decision": decision,
        "detail": detail,
    }
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
    with lock:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
