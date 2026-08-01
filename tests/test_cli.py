from __future__ import annotations

import os
import subprocess
import re
import unittest
from pathlib import Path


class CliVersionTests(unittest.TestCase):
    def test_version_ignores_invalid_lan_port_without_starting_runtime(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        executable = repository / ".venv" / "bin" / "repogents"
        pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
        project_section = pyproject.split("[project]", 1)[1].split("[", 1)[0]
        match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project_section, re.MULTILINE)
        self.assertIsNotNone(match)
        expected_version = match.group(1)

        environment = os.environ.copy()
        environment["REPOGENTS_LAN_PORT"] = "not-a-number"
        environment["REPOGENTS_DATA_DIR"] = str(repository / "pyproject.toml")
        environment.pop("GITHUB_TOKEN", None)
        environment.pop("GH_TOKEN", None)
        environment.pop("REPOGENTS_MODEL", None)

        result = subprocess.run(
            [str(executable), "--version"],
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{expected_version}\n")
        self.assertEqual(result.stderr, "")


    def test_version_ignores_invalid_poll_seconds_without_starting_runtime(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        executable = repository / ".venv" / "bin" / "repogents"
        pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
        project_section = pyproject.split("[project]", 1)[1].split("[", 1)[0]
        match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project_section, re.MULTILINE)
        self.assertIsNotNone(match)
        expected_version = match.group(1)

        environment = os.environ.copy()
        environment["REPOGENTS_POLL_SECONDS"] = "not-a-number"
        environment["REPOGENTS_DATA_DIR"] = str(repository / "pyproject.toml")
        environment.pop("GITHUB_TOKEN", None)
        environment.pop("GH_TOKEN", None)
        environment.pop("REPOGENTS_MODEL", None)

        result = subprocess.run(
            [str(executable), "--version"],
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{expected_version}\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
