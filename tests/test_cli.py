from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    from pip._vendor import tomli as tomllib


def _project_version(pyproject: str) -> str:
    project = tomllib.loads(pyproject).get("project", {})
    version = project.get("version")
    if not isinstance(version, str):
        raise AssertionError("pyproject.toml has no [project] version")
    return version


class VersionCommandTests(unittest.TestCase):
    def test_repository_executable_prints_project_version_without_runtime_configuration(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        pyproject = (repository / "pyproject.toml").read_text()
        for expected in (_project_version(pyproject), "1.0-rc1"):
            with self.subTest(declared_version=expected):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    virtualenv = Path(temporary_directory) / "venv"
                    venv.EnvBuilder(with_pip=True).create(virtualenv)
                    bin_directory = "Scripts" if os.name == "nt" else "bin"
                    python = virtualenv / bin_directory / ("python.exe" if os.name == "nt" else "python")
                    executable = virtualenv / bin_directory / ("repogents.exe" if os.name == "nt" else "repogents")
                    from pip._vendor.packaging.version import Version

                    normalized_version = str(Version(expected))
                    wheel = (
                        Path(temporary_directory)
                        / f"repogents-{normalized_version}-py3-none-any.whl"
                    )
                    distribution = f"repogents-{normalized_version}.dist-info"
                    with zipfile.ZipFile(wheel, "w") as archive:
                        for source in (repository / "repogents").rglob("*.py"):
                            relative = source.relative_to(repository)
                            if relative == Path("repogents/__init__.py"):
                                archive.writestr(
                                    str(relative),
                                    source.read_text().replace(
                                        f'__version__ = "{expected}"',
                                        '__version__ = "0.0.0-stale"',
                                    ),
                                )
                            else:
                                archive.write(source, relative)
                        archive.writestr(
                            f"{distribution}/METADATA",
                            f"Metadata-Version: 2.1\nName: repogents\nVersion: {expected}\n",
                        )
                        archive.writestr(
                            f"{distribution}/WHEEL",
                            "Wheel-Version: 1.0\nGenerator: tests.test_cli\n"
                            "Root-Is-Purelib: true\nTag: py3-none-any\n",
                        )
                        archive.writestr(
                            f"{distribution}/entry_points.txt",
                            "[console_scripts]\nrepogents = repogents.cli:main\n",
                        )
                        archive.writestr(f"{distribution}/RECORD", "")

                    installation = subprocess.run(
                        [
                            str(python),
                            "-m",
                            "pip",
                            "install",
                            "--no-index",
                            "--no-deps",
                            "--force-reinstall",
                            str(wheel),
                        ],
                        cwd=repository,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(installation.returncode, 0, installation.stderr)

                    environment = os.environ.copy()
                    for name in (
                        "GITHUB_TOKEN",
                        "GH_TOKEN",
                        "REPOGENTS_MODEL",
                        "REPOGENTS_MODEL_BASE_URL",
                        "REPOGENTS_DATA_DIR",
                    ):
                        environment.pop(name, None)
                    environment["REPOGENTS_DATA_DIR"] = "~missing-user/data"
                    environment["REPOGENTS_LAN_PORT"] = "not-a-port"
                    environment["REPOGENTS_POLL_SECONDS"] = "not-a-number"
                    environment["REPOGENTS_SIMILARITY_THRESHOLD"] = "not-a-threshold"

                    for arguments in (
                        ["--version"],
                        ["--vers"],
                        ["--data-dir", str(Path(temporary_directory) / "data"), "--version"],
                    ):
                        with self.subTest(arguments=arguments):
                            result = subprocess.run(
                                [str(executable), *arguments],
                                cwd=repository,
                                env=environment,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                check=False,
                            )

                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(result.stdout, f"{expected}\n")
                            self.assertEqual(result.stderr, "")


    def test_source_checkout_version_wins_over_distribution_metadata(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
        checkout_version = "9.9.9"
        installed_version = "1.2.3"
        declared_version = _project_version(pyproject)
        source_pyproject = pyproject.replace(
            f'version = "{declared_version}"',
            f'dependencies = [\n    "example-dependency>=1",\n]\nversion = "{checkout_version}"',
            1,
        )
        self.assertNotEqual(source_pyproject, pyproject)

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_checkout = Path(temporary_directory) / "source"
            source_checkout.mkdir()
            (source_checkout / "pyproject.toml").write_text(
                source_pyproject, encoding="utf-8"
            )
            package = source_checkout / "repogents"
            package.mkdir()
            for source in (repository / "repogents").glob("*.py"):
                (package / source.name).write_bytes(source.read_bytes())

            installed_metadata = Path(temporary_directory) / "installed"
            dist_info = installed_metadata / f"repogents-{installed_version}.dist-info"
            dist_info.mkdir(parents=True)
            (dist_info / "METADATA").write_text(
                "Metadata-Version: 2.1\n"
                "Name: repogents\n"
                f"Version: {installed_version}\n",
                encoding="utf-8",
            )

            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(source_checkout), str(installed_metadata))
            )
            for name in (
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "REPOGENTS_MODEL",
                "REPOGENTS_MODEL_BASE_URL",
            ):
                environment.pop(name, None)
            environment["REPOGENTS_DATA_DIR"] = "~missing-user/data"
            environment["REPOGENTS_LAN_PORT"] = "not-a-port"
            environment["REPOGENTS_POLL_SECONDS"] = "not-a-number"
            environment["REPOGENTS_SIMILARITY_THRESHOLD"] = "not-a-threshold"

            metadata_result = subprocess.run(
                [
                    str(Path(os.sys.executable)),
                    "-c",
                    "from importlib.metadata import version; "
                    "print(version('repogents'))",
                ],
                cwd=source_checkout,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(metadata_result.returncode, 0, metadata_result.stderr)
            self.assertEqual(metadata_result.stdout, f"{installed_version}\n")
            self.assertEqual(metadata_result.stderr, "")

            version_result = subprocess.run(
                [str(Path(os.sys.executable)), "-m", "repogents.cli", "--version"],
                cwd=source_checkout,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(version_result.returncode, 0, version_result.stderr)
            self.assertEqual(version_result.stdout, f"{checkout_version}\n")
            self.assertEqual(version_result.stderr, "")

            help_result = subprocess.run(
                [str(Path(os.sys.executable)), "-m", "repogents.cli", "--help"],
                cwd=source_checkout,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("usage: repogents", help_result.stdout)
            self.assertIn("--version", help_result.stdout)
            self.assertEqual(help_result.stderr, "")


if __name__ == "__main__":
    unittest.main()
