from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from repogents.sandbox import (
    Mount,
    RestrictedNetworkPolicy,
    RestrictedProxy,
    RunLayout,
    SandboxManager,
    SandboxPolicy,
    SecretScanner,
    redact_text,
)


class SandboxPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.persistent = self.root / "sandbox"
        self.persistent.mkdir()
        self.layout = RunLayout.create(self.root / "data", "repo-1", "run-1")

    def test_run_layout_is_isolated_by_run(self) -> None:
        other = RunLayout.create(self.root / "data", "repo-1", "run-2")
        fields = (
            "root",
            "checkout",
            "agent_state",
            "logs",
            "temp",
            "validation",
            "dependency_delta",
            "build",
        )
        for field in fields:
            first_path = getattr(self.layout, field)
            second_path = getattr(other, field)
            self.assertTrue(first_path.is_dir())
            self.assertTrue(second_path.is_dir())
            self.assertNotEqual(first_path, second_path)

    def test_bwrap_policy_mounts_only_declared_paths_and_sanitizes_environment(
        self,
    ) -> None:
        allowed = self.root / "allowed"
        allowed.mkdir()
        policy = SandboxPolicy(
            persistent_root=self.persistent,
            mounts=(Mount(allowed, "/mnt/inputs/allowed", writable=False),),
            allowed_services=(),
        )
        manager = SandboxManager()
        with mock.patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "controller-secret", "OPENAI_API_KEY": "model-secret"},  # pragma: allowlist secret
        ):
            argv, environment = manager.build_command(
                policy, self.layout, ("python3", "-c", "print('ok')")
            )
        joined = "\0".join(argv)
        self.assertIn(str(allowed.resolve()), joined)
        self.assertIn("/mnt/inputs/allowed", argv)
        mounted_sources = {
            argv[index + 1]
            for index, argument in enumerate(argv[:-2])
            if argument in {"--bind", "--ro-bind"}
        }
        sandbox_mounts = {
            argv[index + 2]: (argument, argv[index + 1])
            for index, argument in enumerate(argv[:-2])
            if argument in {"--bind", "--ro-bind"}
        }
        for controller_path in (
            self.layout.root,
            self.layout.logs,
            self.layout.validation,
            self.layout.agent_state,
        ):
            self.assertNotIn(str(controller_path.resolve()), mounted_sources)
        for sandbox_path, host_path in (
            ("/run-data/temp", self.layout.temp),
            ("/run-data/dependency-delta", self.layout.dependency_delta),
            ("/run-data/build", self.layout.build),
        ):
            self.assertEqual(
                sandbox_mounts[sandbox_path], ("--bind", str(host_path.resolve()))
            )
        node_delta = (self.layout.dependency_delta / "node" / "node_modules").resolve()
        self.assertEqual(
            sandbox_mounts["/workspace/node_modules"],
            ("--bind", str(node_delta)),
        )
        sandbox_environment = {
            argv[index + 1]: argv[index + 2]
            for index, argument in enumerate(argv[:-2])
            if argument == "--setenv"
        }
        self.assertEqual(
            sandbox_environment["PYTHONPATH"],
            "/run-data/dependency-delta/python",
        )
        self.assertEqual(
            sandbox_environment["PIP_TARGET"],
            "/run-data/dependency-delta/python",
        )
        self.assertEqual(
            sandbox_environment["NODE_PATH"],
            "/workspace/node_modules",
        )
        self.assertEqual(
            sandbox_environment["npm_config_prefix"],
            "/run-data/dependency-delta/node",
        )
        self.assertTrue(
            sandbox_environment["PATH"].startswith(
                "/repository-state/bin:" "/repository-state/home/.local/bin:"
            )
        )
        self.assertNotIn(str(Path.home()), mounted_sources)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(environment["HOME"], "/home/agent")
        ro_index = argv.index(str(allowed.resolve())) - 1
        self.assertEqual(argv[ro_index], "--ro-bind")

    def test_nested_onboarded_node_dependencies_are_mounted_at_package_root(
        self,
    ) -> None:
        executable = (
            self.persistent
            / "dependencies"
            / "node"
            / "client"
            / "node_modules"
            / ".bin"
            / "fixture-tool"
        )
        executable.parent.mkdir(parents=True)
        executable.write_text(
            "#!/bin/sh\nprintf nested-dependency-ready",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        (self.layout.checkout / "client").mkdir()
        policy = SandboxPolicy(persistent_root=self.persistent)

        result = SandboxManager().run(
            policy,
            self.layout,
            ("/workspace/client/node_modules/.bin/fixture-tool",),
            timeout=20,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "nested-dependency-ready")

    def test_read_only_checkout_mounts_nested_onboarded_node_dependencies(
        self,
    ) -> None:
        executable = (
            self.persistent
            / "dependencies"
            / "node"
            / "client"
            / "node_modules"
            / ".bin"
            / "fixture-tool"
        )
        executable.parent.mkdir(parents=True)
        executable.write_text(
            "#!/bin/sh\nprintf readonly-dependency-ready",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        (self.layout.checkout / "client").mkdir()
        policy = SandboxPolicy(persistent_root=self.persistent)
        manager = SandboxManager()

        argv, _ = manager.build_command(
            policy,
            self.layout,
            ("/workspace/client/node_modules/.bin/fixture-tool",),
            checkout_writable=False,
        )
        sandbox_mounts = {
            argv[index + 2]: (argument, argv[index + 1])
            for index, argument in enumerate(argv[:-2])
            if argument in {"--bind", "--ro-bind"}
        }
        self.assertEqual(
            sandbox_mounts["/workspace/client/node_modules"],
            (
                "--ro-bind",
                str(
                    self.layout.dependency_delta
                    / "node"
                    / "client"
                    / "node_modules"
                ),
            ),
        )
        result = manager.run(
            policy,
            self.layout,
            ("/workspace/client/node_modules/.bin/fixture-tool",),
            timeout=20,
            checkout_writable=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "readonly-dependency-ready")

    def test_validated_browser_bundle_is_mounted_without_its_host_cache(
        self,
    ) -> None:
        cache = self.root / "browser-cache"
        bundle = cache / "chromium-123" / "chrome-linux"
        browser = bundle / "chrome"
        bundle.mkdir(parents=True)
        browser.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf 'Chromium 123 fixture'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        browser.chmod(0o755)
        policy = SandboxPolicy(persistent_root=self.persistent)

        argv, _ = SandboxManager(browser_executable=browser).build_command(
            policy,
            self.layout,
            ("python3", "-c", "print('browser-ready')"),
            checkout_writable=False,
        )
        sandbox_mounts = {
            argv[index + 2]: (argument, argv[index + 1])
            for index, argument in enumerate(argv[:-2])
            if argument in {"--bind", "--ro-bind"}
        }
        sandbox_environment = {
            argv[index + 1]: argv[index + 2]
            for index, argument in enumerate(argv[:-2])
            if argument == "--setenv"
        }

        self.assertEqual(
            sandbox_mounts["/opt/repogents-browser"],
            ("--ro-bind", str(bundle.resolve())),
        )
        mounted_sources = {source for _, source in sandbox_mounts.values()}
        self.assertNotIn(str(cache.resolve()), mounted_sources)
        self.assertEqual(
            sandbox_environment["CHROME_BIN"],
            "/run-data/temp/.repogents-browser-launcher",
        )

    def test_browser_launcher_uses_run_writable_profile_when_home_is_read_only(
        self,
    ) -> None:
        bundle = self.root / "browser-bundle"
        browser = bundle / "chrome"
        bundle.mkdir()
        browser.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = \"--version\" ]; then\n"
            "  printf 'Chromium 123 fixture'\n"
            "  exit 0\n"
            "fi\n"
            "case \" $* \" in *' --disable-crash-reporter '*) ;; *) exit 2 ;; esac\n"
            "case \" $* \" in *' --disable-breakpad '*) ;; *) exit 3 ;; esac\n"
            "printf '%s\\n%s\\n%s' \"$HOME\" \"$XDG_CONFIG_HOME\" \"$XDG_CACHE_HOME\"\n",
            encoding="utf-8",
        )
        browser.chmod(0o755)
        policy = SandboxPolicy(persistent_root=self.persistent)

        result = SandboxManager(browser_executable=browser).run(
            policy,
            self.layout,
            (
                "bash",
                "-lc",
                "export HOME=/repository-state/home; \"$CHROME_BIN\"",
            ),
            timeout=20,
            checkout_writable=False,
        )

        runtime = "/run-data/temp/.repogents-browser-runtime"
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [f"{runtime}/home", f"{runtime}/config", f"{runtime}/cache"],
        )

    def test_invalid_browser_candidate_is_not_advertised(self) -> None:
        browser = self.root / "not-a-browser"
        browser.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        browser.chmod(0o755)
        policy = SandboxPolicy(persistent_root=self.persistent)

        argv, _ = SandboxManager(browser_executable=browser).build_command(
            policy,
            self.layout,
            ("python3", "-c", "print('no-browser')"),
        )
        mounted_targets = {
            argv[index + 2]
            for index, argument in enumerate(argv[:-2])
            if argument in {"--bind", "--ro-bind"}
        }
        sandbox_environment = {
            argv[index + 1]: argv[index + 2]
            for index, argument in enumerate(argv[:-2])
            if argument == "--setenv"
        }

        self.assertNotIn("/opt/repogents-browser", mounted_targets)
        self.assertNotIn("CHROME_BIN", sandbox_environment)

    def test_verifier_checkout_can_be_mounted_read_only(self) -> None:
        policy = SandboxPolicy(persistent_root=self.persistent)
        manager = SandboxManager()
        argv, _ = manager.build_command(
            policy,
            self.layout,
            ("python3", "-c", "print('verify')"),
            checkout_writable=False,
        )
        sandbox_mounts = {
            argv[index + 2]: (argument, argv[index + 1])
            for index, argument in enumerate(argv[:-2])
            if argument in {"--bind", "--ro-bind"}
        }
        self.assertEqual(
            sandbox_mounts["/workspace"],
            ("--ro-bind", str(self.layout.checkout.resolve())),
        )
        self.assertEqual(
            sandbox_mounts["/run-data/temp"],
            ("--bind", str(self.layout.temp.resolve())),
        )

    def test_provisioning_can_write_only_persistent_environment_state(self) -> None:
        policy = SandboxPolicy(persistent_root=self.persistent)
        manager = SandboxManager()
        discovered_bin = self.persistent / "home" / ".toolchain" / "bin"
        discovered_bin.mkdir(parents=True)
        argv, _ = manager.build_command(
            policy,
            self.layout,
            ("python3", "-c", "print('provision')"),
            persistent_writable=True,
        )
        state_index = argv.index(str(self.persistent.resolve()))
        self.assertEqual(argv[state_index - 1], "--bind")
        self.assertEqual(argv[state_index + 1], "/repository-state")
        workspace_index = argv.index(str(self.layout.checkout.resolve()))
        self.assertEqual(argv[workspace_index - 1], "--bind")
        self.assertNotIn(
            str(Path.home()),
            {
                argv[index + 1]
                for index, argument in enumerate(argv[:-2])
                if argument in {"--bind", "--ro-bind"}
            },
        )
        provisioning_environment = {
            argv[index + 1]: argv[index + 2]
            for index, argument in enumerate(argv[:-2])
            if argument == "--setenv"
        }
        self.assertEqual(provisioning_environment["HOME"], "/repository-state/home")
        self.assertTrue(
            provisioning_environment["PATH"].startswith(
                "/repository-state/bin:" "/repository-state/home/.local/bin:"
            )
        )
        self.assertIn(
            "/repository-state/home/.toolchain/bin",
            provisioning_environment["PATH"].split(":"),
        )

    def test_network_policy_rejects_prohibited_addresses_and_non_allowlisted_hosts(
        self,
    ) -> None:
        policy = RestrictedNetworkPolicy(("api.github.com", "*.python.org:443"))
        self.assertTrue(policy.host_allowed("api.github.com", 443))
        self.assertTrue(policy.host_allowed("docs.python.org", 443))
        self.assertFalse(policy.host_allowed("python.org", 443))
        self.assertFalse(policy.host_allowed("api.github.com", 80))
        for address in (
            "127.0.0.1",
            "10.0.0.1",
            "100.64.0.1",
            "169.254.169.254",
            "224.0.0.1",
            "::1",
            "fe80::1",
        ):
            self.assertFalse(policy.address_allowed(address), address)
        self.assertTrue(policy.address_allowed("140.82.112.5"))

    def test_restricted_proxy_forwards_post_body_coalesced_with_headers(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        received: list[bytes] = []
        upstream_errors: list[BaseException] = []

        def serve() -> None:
            try:
                connection, _address = listener.accept()
                with connection:
                    connection.settimeout(3)
                    data = bytearray()
                    while b"\r\n\r\n" not in data:
                        data.extend(connection.recv(8192))
                    header, body = bytes(data).split(b"\r\n\r\n", 1)
                    content_length = next(
                        int(line.split(b":", 1)[1])
                        for line in header.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                    while len(body) < content_length:
                        body += connection.recv(8192)
                    received.append(header + b"\r\n\r\n" + body)
                    connection.sendall(
                        b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
                    )
            except BaseException as error:
                upstream_errors.append(error)

        upstream = threading.Thread(target=serve, daemon=True)
        upstream.start()
        body = b"coalesced POST body"
        request = (
            b"POST http://allowed.test/upload?part=1 HTTP/1.1\r\n"
            b"Host: allowed.test\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"\r\n"
            + body
        )
        expected = request.replace(
            b"http://allowed.test/upload?part=1", b"/upload?part=1", 1
        )
        policy = mock.Mock()
        policy.resolve.return_value = [
            (socket.AF_INET, ("127.0.0.1", port), "93.184.216.34")
        ]
        proxy_socket = self.root / "restricted-proxy.sock"
        with RestrictedProxy(proxy_socket, self.layout.logs / "network.jsonl", policy):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(str(proxy_socket))
                client.sendall(request)
                try:
                    response = client.recv(4096)
                except socket.timeout:
                    response = b""
        upstream.join(4)
        self.assertFalse(upstream_errors)
        self.assertFalse(upstream.is_alive())
        self.assertEqual(received, [expected])
        self.assertIn(b"204 No Content", response)

    def test_restricted_proxy_forwards_connect_bytes_coalesced_with_headers(
        self,
    ) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        payload = b"\x16\x03\x01coalesced tunnel bytes"
        received: list[bytes] = []
        upstream_errors: list[BaseException] = []

        def serve() -> None:
            try:
                connection, _address = listener.accept()
                with connection:
                    connection.settimeout(3)
                    data = bytearray()
                    while len(data) < len(payload):
                        data.extend(connection.recv(8192))
                    received.append(bytes(data))
                    connection.sendall(b"tunnel response")
            except BaseException as error:
                upstream_errors.append(error)

        upstream = threading.Thread(target=serve, daemon=True)
        upstream.start()
        request = (
            b"CONNECT allowed.test:443 HTTP/1.1\r\n"
            b"Host: allowed.test:443\r\n"
            b"\r\n" + payload
        )
        policy = mock.Mock()
        policy.resolve.return_value = [
            (socket.AF_INET, ("127.0.0.1", port), "93.184.216.34")
        ]
        proxy_socket = self.root / "connect-proxy.sock"
        with RestrictedProxy(
            proxy_socket, self.layout.logs / "connect-network.jsonl", policy
        ):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(str(proxy_socket))
                client.sendall(request)
                response = bytearray()
                while b"tunnel response" not in response:
                    try:
                        chunk = client.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    response.extend(chunk)
        upstream.join(4)
        self.assertFalse(upstream_errors)
        self.assertFalse(upstream.is_alive())
        self.assertEqual(received, [payload])
        self.assertTrue(
            bytes(response).startswith(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        )
        self.assertIn(b"tunnel response", response)

    def test_redaction_and_secret_scanning_cover_known_values_and_credentials(
        self,
    ) -> None:
        secret = "canary-super-secret"  # pragma: allowlist secret
        credential_fixture = "".join(("ghp_", "A" * 36))
        text = f"value={secret} token={credential_fixture}"
        redacted = redact_text(text, (secret,))
        self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)
        findings = SecretScanner().scan(text, (secret,))
        self.assertIn("known secret value", findings)
        self.assertTrue(any("GitHub" in finding for finding in findings))

    def test_actual_bubblewrap_hides_host_and_controller_credentials(self) -> None:
        marker = Path.home() / "repogents-unrelated-host-marker"
        marker.write_text("must-not-be-visible", encoding="utf-8")
        self.addCleanup(marker.unlink, missing_ok=True)
        policy = SandboxPolicy(persistent_root=self.persistent)
        manager = SandboxManager()
        code = (
            "import json, os, pathlib; "
            "pathlib.Path('/workspace/result').write_text('written'); "
            "print(json.dumps({'host': pathlib.Path('" + str(marker) + "').exists(), "
            "'github': 'GITHUB_TOKEN' in os.environ, 'model': 'OPENAI_API_KEY' in os.environ}))"
        )
        with mock.patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "controller-secret", "OPENAI_API_KEY": "model-secret"},  # pragma: allowlist secret
        ):
            result = manager.run(
                policy, self.layout, ("python3", "-c", code), timeout=20
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        observation = json.loads(result.stdout)
        self.assertEqual(observation, {"host": False, "github": False, "model": False})
        self.assertEqual(
            (self.layout.checkout / "result").read_text(encoding="utf-8"), "written"
        )

    def test_secret_is_scoped_to_one_command_and_redacted_before_persistence(
        self,
    ) -> None:
        policy = SandboxPolicy(
            persistent_root=self.persistent, allowed_secret_names=("CANARY",)
        )
        manager = SandboxManager()
        secret = "canary-value-123"  # pragma: allowlist secret
        result = manager.run(
            policy,
            self.layout,
            ("python3", "-c", "import os; print(os.environ.get('CANARY', 'missing'))"),
            timeout=20,
            secrets={"CANARY": secret},
        )
        self.assertEqual(result.stdout.strip(), "[REDACTED]")
        self.assertNotIn(secret, result.log_path.read_text(encoding="utf-8"))
        later = manager.run(
            policy,
            self.layout,
            ("python3", "-c", "import os; print(os.environ.get('CANARY', 'missing'))"),
            timeout=20,
        )
        self.assertEqual(later.stdout.strip(), "missing")

    def test_direct_host_loopback_is_unreachable(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        policy = SandboxPolicy(persistent_root=self.persistent)
        result = SandboxManager().run(
            policy,
            self.layout,
            (
                "python3",
                "-c",
                f"import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',{port}))",
            ),
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_allowlisted_https_uses_restricted_proxy(self) -> None:
        policy = SandboxPolicy(
            persistent_root=self.persistent,
            allowed_services=("api.github.com:443",),
        )
        result = SandboxManager().run(
            policy,
            self.layout,
            (
                "python3",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('https://api.github.com', timeout=10).status)",
            ),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "200")
        events = [
            json.loads(line)
            for line in result.network_log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(
            any(
                event["host"] == "api.github.com" and event["decision"] == "allowed"
                for event in events
            )
        )
        self.assertTrue(all("payload" not in event for event in events))

    def test_restricted_proxy_supports_long_durable_run_paths(self) -> None:
        long_root = self.root / ("durable-" + "x" * 100)
        layout = RunLayout.create(long_root, "repository", "run")
        policy = SandboxPolicy(
            persistent_root=self.persistent,
            allowed_services=("api.github.com:443",),
        )
        result = SandboxManager().run(
            policy,
            layout,
            (
                "python3",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('https://api.github.com', timeout=10).status)",
            ),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "200")

    def test_restricted_egress_preserves_sandbox_local_loopback(self) -> None:
        policy = SandboxPolicy(
            persistent_root=self.persistent,
            allowed_services=("api.github.com:443",),
        )
        script = """
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    print(urllib.request.urlopen(
        f"http://127.0.0.1:{server.server_port}/",
        timeout=5,
    ).status)
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
"""
        result = SandboxManager().run(
            policy,
            self.layout,
            ("python3", "-c", script),
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "200")

    def test_restricted_egress_preserves_ipv6_loopback(self) -> None:
        policy = SandboxPolicy(
            persistent_root=self.persistent,
            allowed_services=("api.github.com:443",),
        )
        script = """
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

class IPv6HTTPServer(HTTPServer):
    address_family = socket.AF_INET6

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        pass

server = IPv6HTTPServer(("::1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    print(urllib.request.urlopen(
        f"http://[::1]:{server.server_port}/",
        timeout=5,
    ).status)
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
"""
        result = SandboxManager().run(
            policy,
            self.layout,
            ("python3", "-c", script),
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "200")

    def test_cancel_terminates_descendant_process_tree_and_preserves_sandbox(
        self,
    ) -> None:
        policy = SandboxPolicy(persistent_root=self.persistent)
        manager = SandboxManager()
        finished: list[object] = []

        def run_command() -> None:
            finished.append(
                manager.run(
                    policy,
                    self.layout,
                    (
                        "python3",
                        "-c",
                        "import subprocess,time; subprocess.Popen(['sleep','60']); time.sleep(60)",
                    ),
                    timeout=120,
                )
            )

        thread = threading.Thread(target=run_command)
        thread.start()
        deadline = time.monotonic() + 10
        while not manager.is_active(self.layout.run_id) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(manager.cancel(self.layout.run_id))
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.persistent.is_dir())
        self.assertTrue(self.layout.logs.is_dir())
        self.assertEqual(len(finished), 1)
        self.assertNotEqual(finished[0].returncode, 0)


if __name__ == "__main__":
    unittest.main()
