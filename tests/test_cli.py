from __future__ import annotations

import os
import subprocess
import re
import unittest
from pathlib import Path


class VersionCommandTests(unittest.TestCase):
    def test_version_ignores_invalid_lan_port(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        manifest = (repository / "pyproject.toml").read_text(encoding="utf-8")
        project = manifest.split("[project]", 1)[1].split("[", 1)[0]
        match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project, re.MULTILINE)
        self.assertIsNotNone(match)
        expected = match.group(1)
        environment = os.environ.copy()
        environment["REPOGENTS_LAN_PORT"] = "not-a-number"

        result = subprocess.run(
            (repository / ".venv" / "bin" / "repogents", "--version"),
            cwd=repository,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, f"{expected}\n".encode())
        self.assertEqual(result.stderr, b"")


    def test_version_ignores_invalid_poll_seconds(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        manifest = (repository / "pyproject.toml").read_text(encoding="utf-8")
        project = manifest.split("[project]", 1)[1].split("[", 1)[0]
        match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project, re.MULTILINE)
        self.assertIsNotNone(match)
        expected = match.group(1)
        environment = os.environ.copy()
        environment["REPOGENTS_POLL_SECONDS"] = "not-a-number"

        result = subprocess.run(
            (repository / ".venv" / "bin" / "repogents", "--version"),
            cwd=repository,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, f"{expected}\n".encode())
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
