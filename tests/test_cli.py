from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliVersionTests(unittest.TestCase):
    def test_installed_executable_prints_declared_version_without_configuration(self) -> None:
        executable = ROOT / ".venv" / "bin" / "repogents"
        self.assertTrue(executable.is_file(), f"missing installed executable: {executable}")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        project_section = pyproject.split("[project]", 1)[1].split("\n[", 1)[0]
        version_match = re.search(
            r'^version\s*=\s*"([^"]+)"\s*$', project_section, re.MULTILINE
        )
        self.assertIsNotNone(version_match, "missing [project] version in pyproject.toml")
        declared_version = version_match.group(1)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "OPENAI_API_KEY",
                "REPOGENTS_DATA_DIR",
                "REPOGENTS_MODEL",
                "REPOGENTS_MODEL_BASE_URL",
            }
        }
        environment["PATH"] = str(executable.parent)

        result = subprocess.run(
            [str(executable), "--version"],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, f"{declared_version}\n".encode())
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
