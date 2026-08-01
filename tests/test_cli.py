from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


class CliVersionTests(unittest.TestCase):
    def test_version_ignores_invalid_configuration_without_starting_runtime(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        executable = repository / ".venv" / "bin" / "repogents"
        pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
        project_section = pyproject.split("[project]", 1)[1].split("[", 1)[0]
        match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project_section, re.MULTILINE)
        self.assertIsNotNone(match)
        expected_version = match.group(1)

        for variable in ("REPOGENTS_LAN_PORT", "REPOGENTS_POLL_SECONDS"):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as home:
                environment = os.environ.copy()
                environment[variable] = "not-a-number"
                environment["HOME"] = home
                environment.pop("REPOGENTS_DATA_DIR", None)
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
                self.assertFalse(Path(home, ".local", "share", "repogents").exists())


if __name__ == "__main__":
    unittest.main()
