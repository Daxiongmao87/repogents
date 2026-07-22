from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import venv
import unittest
from pathlib import Path
from unittest import mock

from repogents.sandbox import Mount, RunLayout, SandboxManager, SandboxPolicy


class HostSandboxAcceptanceTests(unittest.TestCase):
    def test_actual_host_boundaries_and_concurrent_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persistent = root / "repository-state"
            persistent.mkdir()
            readonly = root / "readonly"
            readonly.mkdir()
            (readonly / "fixture.txt").write_text("fixture", encoding="utf-8")
            writable = root / "writable"
            writable.mkdir()
            marker = Path.home() / f"repogents-host-canary-{os.getpid()}"
            marker.write_text("unrelated", encoding="utf-8")
            self.addCleanup(marker.unlink, missing_ok=True)
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen()
            self.addCleanup(server.close)
            port = server.getsockname()[1]
            layout = RunLayout.create(root / "runs", "repo", "host-check")
            policy = SandboxPolicy(
                persistent_root=persistent,
                mounts=(
                    Mount(readonly, "/mnt/inputs/readonly", writable=False),
                    Mount(writable, "/mnt/inputs/writable", writable=True),
                ),
            )
            code = """
import json, os, pathlib, socket

def connects(host, port):
    value = socket.socket()
    value.settimeout(.25)
    try:
        value.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        value.close()

readonly_write = True
try:
    pathlib.Path('/mnt/inputs/readonly/blocked').write_text('no')
except OSError:
    readonly_write = False
pathlib.Path('/mnt/inputs/writable/created').write_text('yes')
pathlib.Path('/workspace/created').write_text('yes')
print(json.dumps({
    'fixture': pathlib.Path('/mnt/inputs/readonly/fixture.txt').read_text(),
    'readonly_write': readonly_write,
    'host_file': pathlib.Path(%r).exists(),
    'host_process': pathlib.Path('/proc/%d').exists(),
    'host_service': connects('127.0.0.1', %d),
    'private': connects('10.0.0.1', 80),
    'link_local': connects('169.254.1.1', 80),
    'metadata': connects('169.254.169.254', 80),
    'github_credential': 'GITHUB_TOKEN' in os.environ,
    'model_credential': 'OPENAI_API_KEY' in os.environ,
}))
""" % (str(marker), os.getpid(), port)
            with mock.patch.dict(
                os.environ,
                {"GITHUB_TOKEN": "controller", "OPENAI_API_KEY": "model"},  # pragma: allowlist secret
            ):
                result = SandboxManager().run(
                    policy, layout, ("python3", "-c", code), timeout=20
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            observation = json.loads(result.stdout)
            self.assertEqual(observation["fixture"], "fixture")
            self.assertFalse(observation["readonly_write"])
            for boundary in (
                "host_file",
                "host_process",
                "host_service",
                "private",
                "link_local",
                "metadata",
                "github_credential",
                "model_credential",
            ):
                self.assertFalse(observation[boundary], boundary)
            self.assertEqual((writable / "created").read_text(), "yes")
            self.assertEqual((layout.checkout / "created").read_text(), "yes")

            layouts = (
                RunLayout.create(root / "runs", "repo", "parallel-a"),
                RunLayout.create(root / "runs", "repo", "parallel-b"),
            )
            results: list[object] = []

            def execute(index: int) -> None:
                command = (
                    "python3",
                    "-c",
                    "import pathlib; "
                    f"pathlib.Path('/workspace/run-{index}').write_text('isolated'); "
                    "pathlib.Path('/repository-cache').mkdir(exist_ok=True); "
                    f"pathlib.Path('/repository-cache/cache-{index}').write_text('shared')",
                )
                results.append(
                    SandboxManager().run(policy, layouts[index], command, timeout=20)
                )

            threads = [threading.Thread(target=execute, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(len(results), 2)
            self.assertTrue(
                all(result.returncode == 0 for result in results),
                [(result.returncode, result.stderr) for result in results],
            )
            self.assertTrue((layouts[0].checkout / "run-0").is_file())
            self.assertFalse((layouts[0].checkout / "run-1").exists())
            self.assertTrue((layouts[1].checkout / "run-1").is_file())
            self.assertFalse((layouts[1].checkout / "run-0").exists())
            self.assertEqual((persistent / "shared-cache" / "cache-0").read_text(), "shared")
            self.assertEqual((persistent / "shared-cache" / "cache-1").read_text(), "shared")


    def test_repository_commands_cannot_mutate_controller_run_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persistent = root / "repository-state"
            persistent.mkdir()
            layout = RunLayout.create(root / "runs", "repo", "evidence-protection")
            evidence = {
                "logs": layout.logs / "controller.json",
                "validation": layout.validation / "controller.json",
                "agent-state": layout.agent_state / "controller.json",
            }
            for path in evidence.values():
                path.write_text("controller", encoding="utf-8")
            code = r"""
import json
import pathlib

def overwrite(name):
    path = pathlib.Path("/run-data") / name / "controller.json"
    try:
        path.write_text("repository")
    except OSError:
        return False
    return True

for name in ("temp", "dependency-delta", "build"):
    (pathlib.Path("/run-data") / name / "repository").write_text("writable")
print(json.dumps({
    name: overwrite(name)
    for name in ("logs", "validation", "agent-state")
}))
"""
            result = SandboxManager().run(
                SandboxPolicy(persistent_root=persistent),
                layout,
                ("python3", "-c", code),
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {"logs": False, "validation": False, "agent-state": False},
            )
            for path in evidence.values():
                self.assertEqual(path.read_text(encoding="utf-8"), "controller")
            self.assertTrue(result.log_path.is_file())
            self.assertEqual(
                (layout.temp / "repository").read_text(encoding="utf-8"), "writable"
            )
            self.assertEqual(
                (layout.dependency_delta / "repository").read_text(encoding="utf-8"),
                "writable",
            )
            self.assertEqual(
                (layout.build / "repository").read_text(encoding="utf-8"), "writable"
            )


    def test_run_dependency_delta_is_used_while_baselines_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persistent = root / "repository-state"
            persistent.mkdir()
            python_environment = persistent / "python-venv"
            venv.EnvBuilder(with_pip=False).create(python_environment)
            python_site = (
                python_environment
                / "lib"
                / f"python{os.sys.version_info.major}.{os.sys.version_info.minor}"
                / "site-packages"
            )
            python_site.mkdir(parents=True, exist_ok=True)
            (python_site / "baseline_package.py").write_text(
                "VALUE = 'python-baseline'\n", encoding="utf-8"
            )
            node_modules = persistent / "node" / "node_modules"
            baseline_node = node_modules / "baseline-package"
            baseline_node.mkdir(parents=True)
            (baseline_node / "index.js").write_text(
                "module.exports = 'node-baseline';\n", encoding="utf-8"
            )
            layout = RunLayout.create(root / "runs", "repo", "dependency-delta")
            policy = SandboxPolicy(persistent_root=persistent)
            code = r"""
import baseline_package
import importlib
import json
import os
import pathlib
import subprocess

python_delta = pathlib.Path(os.environ["PIP_TARGET"])
python_delta.mkdir(parents=True, exist_ok=True)
(python_delta / "delta_package.py").write_text("VALUE = 'python-delta'\n")
delta_package = importlib.import_module("delta_package")
python_baseline_write = True
try:
    pathlib.Path(baseline_package.__file__).write_text("changed")
except OSError:
    python_baseline_write = False
node_delta = pathlib.Path("/workspace/node_modules/delta-package")
node_delta.mkdir(parents=True, exist_ok=True)
(node_delta / "index.js").write_text("module.exports = 'node-delta';\n")
node = subprocess.run(
    [
        "node",
        "-e",
        '''
const fs = require("fs");
const baseline = require("baseline-package");
const delta = require("delta-package");
let baselineWrite = true;
try {
  fs.writeFileSync(require.resolve("baseline-package"), "changed");
} catch (error) {
  baselineWrite = false;
}
console.log(JSON.stringify({baseline, delta, baselineWrite}));
''',
    ],
    check=True,
    capture_output=True,
    text=True,
)
print(json.dumps({
    "python_baseline": baseline_package.VALUE,
    "python_delta": delta_package.VALUE,
    "python_baseline_write": python_baseline_write,
    "node": json.loads(node.stdout),
}))
"""
            result = SandboxManager().run(
                policy, layout, ("python3", "-c", code), timeout=20
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            observation = json.loads(result.stdout)
            self.assertEqual(observation["python_baseline"], "python-baseline")
            self.assertEqual(observation["python_delta"], "python-delta")
            self.assertFalse(observation["python_baseline_write"])
            self.assertEqual(observation["node"]["baseline"], "node-baseline")
            self.assertEqual(observation["node"]["delta"], "node-delta")
            self.assertFalse(observation["node"]["baselineWrite"])
            self.assertTrue(
                (layout.dependency_delta / "python" / "delta_package.py").is_file()
            )
            self.assertTrue(
                (
                    layout.dependency_delta
                    / "node"
                    / "node_modules"
                    / "delta-package"
                    / "index.js"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()
