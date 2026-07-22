from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from repogents.controller import (
    EnvironmentSecretResolver,
    RunProcessSupervisor,
    git_environment,
    require_explicit_model,
    run_file_backed,
)


class ControllerBoundaryTests(unittest.TestCase):
    def test_model_must_be_explicit_and_is_never_discovered_from_host_configuration(
        self,
    ) -> None:
        poisoned_environment = {
            "HOME": "/home/poisoned",
            "PI_CODING_AGENT_DIR": "/opt/poisoned-pi",
            "OMP_CONFIG": "/opt/poisoned-omp/config.json",
            "REPOGENTS_MODEL": "ambient/model",
        }

        with (
            patch.dict(os.environ, poisoned_environment, clear=True),
            patch("repogents.controller.subprocess.run") as runner,
        ):
            self.assertEqual(
                require_explicit_model("openai/gpt-explicit"),
                "openai/gpt-explicit",
            )
            for missing in (None, "", "   "):
                with self.subTest(model=missing):
                    with self.assertRaisesRegex(
                        ValueError,
                        "explicit.*model|model.*required",
                    ):
                        require_explicit_model(missing)

        runner.assert_not_called()

    def test_git_environment_uses_ephemeral_askpass_without_ambient_credentials(
        self,
    ) -> None:
        source = {
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "HOME": "/home/test",
            "GITHUB_TOKEN": "ambient-github",
            "GH_TOKEN": "ambient-gh",
        }
        askpass_path: Path | None = None
        with git_environment("configured-token", source) as environment:
            askpass_path = Path(environment["GIT_ASKPASS"])
            self.assertTrue(askpass_path.is_file())
            self.assertEqual(
                environment["REPOGENTS_GITHUB_TOKEN"],
                "configured-token",
            )
            self.assertNotIn("GITHUB_TOKEN", environment)
            self.assertNotIn("GH_TOKEN", environment)
            username = subprocess.run(
                [str(askpass_path), "Username for https://github.com"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            password = subprocess.run(
                [str(askpass_path), "Password for https://github.com"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            self.assertEqual(username, "x-access-token")
            self.assertEqual(password, "configured-token")
        self.assertIsNotNone(askpass_path)
        self.assertFalse(askpass_path.exists())

    def test_file_backed_runner_never_places_prompt_in_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured: list[list[str]] = []

            def fake_run(argv, **kwargs):
                captured.append(list(argv))
                prompt_path = Path(argv[-1].removeprefix("@"))
                self.assertEqual(
                    prompt_path.read_text(encoding="utf-8"),
                    "x" * 200_000,
                )
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            with patch(
                "repogents.controller.subprocess.run",
                side_effect=fake_run,
            ):
                result = run_file_backed(
                    ["model-worker", "--request"],
                    "x" * 200_000,
                    root,
                    30,
                    environment={"PATH": "/bin"},
                )

            self.assertEqual(result.stdout, "ok")
            self.assertEqual(len(captured), 1)
            self.assertEqual(
                captured[0][:-1],
                ["model-worker", "--request"],
            )
            self.assertTrue(captured[0][-1].startswith("@"))
            self.assertFalse(Path(captured[0][-1][1:]).exists())

    def test_run_process_supervisor_terminates_model_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "processes"
            supervisor = RunProcessSupervisor()
            completed: list[subprocess.CompletedProcess[str]] = []

            script = (
                "import os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)'])\n"
                f"Path({str(marker)!r}).write_text("
                "f'{os.getpid()} {child.pid}')\n"
                "time.sleep(60)\n"
            )

            thread = threading.Thread(
                target=lambda: completed.append(
                    run_file_backed(
                        ["python3", "-c", script],
                        "ignored prompt",
                        root,
                        30,
                        environment={"PATH": os.environ["PATH"]},
                        supervisor=supervisor,
                        run_id="run-1",
                    )
                )
            )
            thread.start()
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            process_ids = tuple(
                int(value) for value in marker.read_text().split()
            )

            supervisor.cancel("run-1")
            thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(supervisor.active("run-1"), ())
            self.assertEqual(len(completed), 1)
            self.assertLess(completed[0].returncode, 0)
            deadline = time.monotonic() + 5
            while (
                any(_process_is_running(pid) for pid in process_ids)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(
                all(not _process_is_running(pid) for pid in process_ids)
            )

    def test_environment_secret_resolver_maps_only_explicit_secret_references(
        self,
    ) -> None:
        resolver = EnvironmentSecretResolver(
            {
                "REPOGENTS_SECRET_PACKAGE_TOKEN": "canary-value",  # pragma: allowlist secret
                "GITHUB_TOKEN": "controller-token",
            }
        )

        self.assertEqual(
            resolver("secret://package-token"),
            "canary-value",
        )
        with self.assertRaisesRegex(ValueError, "secret://"):
            resolver("env://GITHUB_TOKEN")
        with self.assertRaisesRegex(KeyError, "missing"):
            resolver("secret://missing")



def _process_is_running(process_id: int) -> bool:
    stat_path = Path(f"/proc/{process_id}/stat")
    try:
        fields = stat_path.read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return False
    return len(fields) > 2 and fields[2] != "Z"

if __name__ == "__main__":
    unittest.main()
