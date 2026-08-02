from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


class InstalledCliVersionTests(unittest.TestCase):
    def test_version_matches_project_metadata_without_runtime_configuration(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        project_lines = (repository / "pyproject.toml").read_text(
            encoding="utf-8"
        ).splitlines()
        expected_version = next(
            line.split("=", 1)[1].strip().strip('"')
            for line in project_lines
            if line.startswith("version = ")
        )
        environment = os.environ.copy()
        for name in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "REPOGENTS_MODEL",
            "REPOGENTS_MODEL_BASE_URL",
            "REPOGENTS_DATA_DIR",
        ):
            environment.pop(name, None)
        environment["REPOGENTS_LAN_PORT"] = "invalid"
        environment["REPOGENTS_POLL_SECONDS"] = "invalid"

        result = subprocess.run(
            [repository / ".venv" / "bin" / "repogents", "--version"],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{expected_version}\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
