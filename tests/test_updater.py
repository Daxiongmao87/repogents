from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repogents.updater import MainBranchUpdater, UpdateRefused


class RecordingServiceController:
    def __init__(
        self, failures: int = 0, events: list[str] | None = None
    ) -> None:
        self.failures = failures
        self.events = events
        self.restart_calls: list[str] = []

    def restart_and_verify(self, service_name: str) -> None:
        if self.events is not None:
            self.events.append("restart")
        self.restart_calls.append(service_name)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("service restart failed")


class MainBranchUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.publisher = self.root / "publisher"
        self.checkout = self.root / "checkout"
        self.state_file = self.root / "state" / "restarted-commit"

        self.git(self.root, "init", "--bare", str(self.remote))
        self.git(
            self.root, "init", "--initial-branch=main", str(self.publisher)
        )
        self.git(self.publisher, "config", "user.name", "Updater Test")
        self.git(
            self.publisher, "config", "user.email", "updater@example.test"
        )
        (self.publisher / "application.txt").write_text(
            "first\n", encoding="utf-8"
        )
        self.git(self.publisher, "add", "application.txt")
        self.git(self.publisher, "commit", "-m", "initial")
        self.git(self.publisher, "remote", "add", "origin", str(self.remote))
        self.git(self.publisher, "push", "--set-upstream", "origin", "main")
        self.git(
            self.root,
            "clone",
            "--branch",
            "main",
            str(self.remote),
            str(self.checkout),
        )
        self.initial_sha = self.git(
            self.checkout, "rev-parse", "HEAD"
        ).stdout.strip()

    @staticmethod
    def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def record_restarted_commit(self, sha: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(f"{sha}\n", encoding="utf-8")

    @property
    def refresh_state_file(self) -> Path:
        return self.state_file.with_name(
            f"{self.state_file.name}.installation-refreshed"
        )

    def record_refreshed_commit(self, sha: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.refresh_state_file.write_text(f"{sha}\n", encoding="utf-8")

    def publish(self, contents: str = "second\n") -> str:
        (self.publisher / "application.txt").write_text(
            contents, encoding="utf-8"
        )
        self.git(self.publisher, "add", "application.txt")
        self.git(self.publisher, "commit", "-m", contents.strip())
        self.git(self.publisher, "push", "origin", "main")
        return self.git(self.publisher, "rev-parse", "HEAD").stdout.strip()

    def updater(
        self, controller: RecordingServiceController
    ) -> MainBranchUpdater:
        updater = MainBranchUpdater(
            repository=self.checkout,
            state_file=self.state_file,
            service_name="repogents.service",
            service_controller=controller,
        )
        updater._refresh_installation_metadata = lambda: None
        return updater

    def test_fast_forward_restarts_and_records_commit_after_success(
        self,
    ) -> None:
        self.record_restarted_commit(self.initial_sha)
        expected_sha = self.publish()
        untracked_state = (
            self.checkout / ".repogents-model-state" / "state.json"
        )
        untracked_state.parent.mkdir()
        untracked_state.write_text("{}\n", encoding="utf-8")
        controller = RecordingServiceController()

        outcome = self.updater(controller).check_once()

        self.assertTrue(outcome.updated)
        self.assertTrue(outcome.restarted)
        self.assertEqual(outcome.commit_sha, expected_sha)
        self.assertEqual(controller.restart_calls, ["repogents.service"])
        self.assertEqual(
            self.git(self.checkout, "rev-parse", "HEAD").stdout.strip(),
            expected_sha,
        )
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            f"{expected_sha}\n",
        )
        self.assertTrue(untracked_state.exists())

    def test_current_restarted_commit_is_a_no_op(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        self.record_refreshed_commit(self.initial_sha)
        controller = RecordingServiceController()

        outcome = self.updater(controller).check_once()

        self.assertFalse(outcome.updated)
        self.assertFalse(outcome.restarted)
        self.assertEqual(outcome.commit_sha, self.initial_sha)
        self.assertEqual(controller.restart_calls, [])

    def test_legacy_restart_checkpoint_requires_separate_refresh(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        controller = RecordingServiceController()
        updater = MainBranchUpdater(
            repository=self.checkout,
            state_file=self.state_file,
            service_name="repogents.service",
            service_controller=controller,
        )
        events: list[str] = []
        controller.events = events
        updater._refresh_installation_metadata = lambda: events.append("refresh")

        first = updater.check_once()

        self.assertFalse(first.updated)
        self.assertTrue(first.restarted)
        self.assertEqual(events, ["refresh", "restart"])
        self.assertEqual(controller.restart_calls, ["repogents.service"])
        self.assertEqual(
            self.refresh_state_file.read_text(encoding="utf-8"),
            f"{self.initial_sha}\n",
        )
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            f"{self.initial_sha}\n",
        )

        second = updater.check_once()
        self.assertFalse(second.updated)
        self.assertFalse(second.restarted)
        self.assertEqual(events, ["refresh", "restart"])
        self.assertEqual(controller.restart_calls, ["repogents.service"])

    def test_legacy_restart_checkpoint_does_not_advance_restart_on_failure(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        events: list[str] = []
        controller = RecordingServiceController(failures=1, events=events)
        updater = MainBranchUpdater(
            repository=self.checkout,
            state_file=self.state_file,
            service_name="repogents.service",
            service_controller=controller,
        )
        updater._refresh_installation_metadata = lambda: events.append("refresh")

        with self.assertRaisesRegex(RuntimeError, "service restart failed"):
            updater.check_once()

        self.assertEqual(events, ["refresh", "restart"])
        self.assertEqual(
            self.refresh_state_file.read_text(encoding="utf-8"),
            f"{self.initial_sha}\n",
        )
        self.assertFalse(self.state_file.exists())

        outcome = updater.check_once()
        self.assertFalse(outcome.updated)
        self.assertTrue(outcome.restarted)
        self.assertEqual(events, ["refresh", "restart", "restart"])
        self.assertEqual(
            controller.restart_calls,
            ["repogents.service", "repogents.service"],
        )
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            f"{self.initial_sha}\n",
        )

        final = updater.check_once()
        self.assertFalse(final.updated)
        self.assertFalse(final.restarted)
        self.assertEqual(events, ["refresh", "restart", "restart"])
        self.assertEqual(
            controller.restart_calls,
            ["repogents.service", "repogents.service"],
        )

    def test_legacy_restart_checkpoint_does_not_advance_refresh_on_failure(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        controller = RecordingServiceController()
        updater = MainBranchUpdater(
            repository=self.checkout,
            state_file=self.state_file,
            service_name="repogents.service",
            service_controller=controller,
        )

        def fail_refresh() -> None:
            raise RuntimeError("dependency reconciliation failed")

        updater._refresh_installation_metadata = fail_refresh
        with self.assertRaisesRegex(RuntimeError, "dependency reconciliation failed"):
            updater.check_once()

        self.assertFalse(self.refresh_state_file.exists())
        self.assertEqual(controller.restart_calls, [])
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            f"{self.initial_sha}\n",
        )

    def test_failed_restart_leaves_checkpoint_and_next_check_retries(
        self,
    ) -> None:
        self.record_restarted_commit(self.initial_sha)
        expected_sha = self.publish()
        failing_controller = RecordingServiceController(failures=1)

        with self.assertRaisesRegex(RuntimeError, "service restart failed"):
            self.updater(failing_controller).check_once()

        self.assertEqual(
            self.git(self.checkout, "rev-parse", "HEAD").stdout.strip(),
            expected_sha,
        )
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            f"{self.initial_sha}\n",
        )

        retry_controller = RecordingServiceController()
        outcome = self.updater(retry_controller).check_once()

        self.assertFalse(outcome.updated)
        self.assertTrue(outcome.restarted)
        self.assertEqual(retry_controller.restart_calls, ["repogents.service"])
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            f"{expected_sha}\n",
        )

    def test_refuses_non_main_branch_without_restart(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        self.git(self.checkout, "switch", "--create", "work")
        controller = RecordingServiceController()

        with self.assertRaisesRegex(
            UpdateRefused, "checked out branch is 'work'"
        ):
            self.updater(controller).check_once()

        self.assertEqual(controller.restart_calls, [])

    def test_refuses_tracked_changes_without_restart(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        (self.checkout / "application.txt").write_text(
            "dirty\n", encoding="utf-8"
        )
        controller = RecordingServiceController()

        with self.assertRaisesRegex(UpdateRefused, "tracked changes"):
            self.updater(controller).check_once()

        self.assertEqual(controller.restart_calls, [])
        self.assertEqual(
            self.git(self.checkout, "rev-parse", "HEAD").stdout.strip(),
            self.initial_sha,
        )

    def test_refuses_non_fast_forward_remote_without_restart(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        self.git(self.checkout, "config", "user.name", "Updater Test")
        self.git(self.checkout, "config", "user.email", "updater@example.test")
        (self.checkout / "local.txt").write_text("local\n", encoding="utf-8")
        self.git(self.checkout, "add", "local.txt")
        self.git(self.checkout, "commit", "-m", "local commit")
        local_sha = self.git(self.checkout, "rev-parse", "HEAD").stdout.strip()
        self.publish()
        controller = RecordingServiceController()

        with self.assertRaisesRegex(UpdateRefused, "not a fast-forward"):
            self.updater(controller).check_once()

        self.assertEqual(controller.restart_calls, [])
        self.assertEqual(
            self.git(self.checkout, "rev-parse", "HEAD").stdout.strip(),
            local_sha,
        )

    def test_fast_forward_refreshes_editable_metadata_before_restart(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        package = self.publisher / "repogents"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "cli.py").write_text(
            "from importlib.metadata import version\n"
            "def main():\n"
            "    print(version('repogents'))\n",
            encoding="utf-8",
        )
        (self.publisher / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = ['setuptools>=68']\n"
            "build-backend = 'setuptools.build_meta'\n\n"
            "[project]\n"
            "name = 'repogents'\n"
            "version = '2.0.0'\n\n"
            "[project.scripts]\n"
            "repogents = 'repogents.cli:main'\n",
            encoding="utf-8",
        )
        self.git(
            self.publisher, "add", "pyproject.toml", "repogents"
        )
        self.git(self.publisher, "commit", "-m", "add versioned package")
        self.git(self.publisher, "push", "origin", "main")
        self.git(self.checkout, "pull", "--ff-only", "origin", "main")
        version_two_sha = self.git(
            self.checkout, "rev-parse", "HEAD"
        ).stdout.strip()
        self.record_restarted_commit(version_two_sha)

        with tempfile.TemporaryDirectory() as environment_directory:
            environment = Path(environment_directory)
            subprocess.run(
                [sys.executable, "-m", "venv", str(environment)],
                check=True,
            )
            bin_directory = "Scripts" if os.name == "nt" else "bin"
            python = environment / bin_directory / (
                "python.exe" if os.name == "nt" else "python"
            )
            executable = environment / bin_directory / (
                "repogents.exe" if os.name == "nt" else "repogents"
            )
            removal = subprocess.run(
                [str(python), "-m", "pip", "uninstall", "--yes", "setuptools"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(removal.returncode, 0, removal.stderr)
            build_environment = {
                **os.environ,
                "PIP_NO_INDEX": "1",
            }
            initial_updater = MainBranchUpdater(
                repository=self.checkout,
                state_file=self.state_file,
                service_name="repogents.service",
                service_controller=RecordingServiceController(),
            )
            with patch("repogents.updater.sys.executable", str(python)), patch.dict(
                os.environ, build_environment, clear=True
            ):
                initial_updater._refresh_installation_metadata()
            setuptools = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import importlib.util; print(importlib.util.find_spec('setuptools'))",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(setuptools.stdout, "None\n")
            (self.publisher / "pyproject.toml").write_text(
                (self.publisher / "pyproject.toml").read_text(encoding="utf-8").replace(
                    "version = '2.0.0'", "version = '1.0-rc1'"
                ),
                encoding="utf-8",
            )
            self.git(self.publisher, "add", "pyproject.toml")
            self.git(self.publisher, "commit", "-m", "release 1.0-rc1")
            self.git(self.publisher, "push", "origin", "main")

            events: list[str] = []
            provenance: dict[str, object] = {}

            class ProvenanceCheckingController(RecordingServiceController):
                def restart_and_verify(self, service_name: str) -> None:
                    site_packages_result = subprocess.run(
                        [
                            str(python),
                            "-c",
                            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    installed_dist_info = (
                        Path(site_packages_result.stdout.strip())
                        / "repogents-1.0rc1.dist-info"
                    )
                    direct_url = __import__("json").loads(
                        (installed_dist_info / "direct_url.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    from urllib.parse import unquote, urlparse

                    parsed = urlparse(direct_url["url"])
                    target = Path(unquote(parsed.path))
                    if os.name == "nt" and parsed.netloc:
                        target = Path(f"{parsed.netloc}{parsed.path}")
                    freeze = subprocess.run(
                        [str(python), "-m", "pip", "freeze"],
                        cwd=self.checkout if hasattr(self, "checkout") else None,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    provenance.update(
                        direct_url=direct_url,
                        target=target,
                        freeze=freeze.stdout,
                    )
                    super().restart_and_verify(service_name)

            controller = ProvenanceCheckingController(events=events)
            controller.checkout = self.checkout
            updater = MainBranchUpdater(
                repository=self.checkout,
                state_file=self.state_file,
                service_name="repogents.service",
                service_controller=controller,
            )
            with patch("repogents.updater.sys.executable", str(python)), patch.dict(
                os.environ, build_environment, clear=True
            ):
                outcome = updater.check_once()

            self.assertTrue(outcome.updated)
            self.assertEqual(events, ["restart"])
            metadata = subprocess.run(
                [
                    str(python),
                    "-c",
                    "from importlib.metadata import version; print(version('repogents'))",
                ],
                cwd=self.checkout,
                check=True,
                capture_output=True,
                text=True,
            )
            metadata_directories = subprocess.run(
                [
                    str(python),
                    "-c",
                    "from importlib.metadata import distributions; "
                    "print('\\n'.join(sorted(set("
                    "distribution.version for distribution in distributions() "
                    "if distribution.metadata.get('Name', '').lower() == 'repogents'"
                    "))))",
                ],
                cwd=self.checkout,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                metadata_directories.returncode, 0, metadata_directories.stderr
            )
            command = subprocess.run(
                [str(executable)],
                cwd=self.checkout,
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "REPOGENTS_DATA_DIR": "~missing-user/data"},
            )
            self.assertEqual(metadata.stdout, "1.0-rc1\n")
            self.assertEqual(metadata_directories.stdout.splitlines(), ["1.0-rc1"])
            site_packages = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            dist_info = Path(site_packages.stdout.strip()) / "repogents-1.0rc1.dist-info"
            self.assertTrue(dist_info.is_dir(), dist_info)
            self.assertIn(
                "Version: 1.0-rc1\n",
                (dist_info / "METADATA").read_text(encoding="utf-8"),
            )
            setuptools_after_refresh = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import importlib.util; print(importlib.util.find_spec('setuptools'))",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(setuptools_after_refresh.stdout, "None\n")
            self.assertEqual(command.returncode, 0, command.stderr)
            self.assertEqual(command.stdout, "1.0-rc1\n")
            self.assertEqual(command.stderr, "")
            direct_url = provenance["direct_url"]
            target = provenance["target"]
            freeze = provenance["freeze"]
            self.assertEqual(direct_url.get("dir_info"), {"editable": True})
            self.assertIsInstance(target, Path)
            persistent_target = self.state_file.parent / "editable-refresh"
            self.assertEqual(target.resolve(), persistent_target.resolve())
            self.assertTrue(target.is_dir(), target)
            self.assertTrue((target / "pyproject.toml").is_file())
            self.assertTrue((target / "refresh_backend.py").is_file())
            refresh_metadata = __import__("json").loads(
                (target / "refresh.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                Path(refresh_metadata["source"]).resolve(), self.checkout.resolve()
            )
            self.assertIn(str(target), freeze)
            self.assertNotIn("repogents-refresh-", freeze)

    def test_requires_python_is_enforced_before_restart_and_checkpoint(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        package = self.publisher / "repogents"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (self.publisher / "pyproject.toml").write_text(
            "[project]\n"
            "name = 'repogents'\n"
            "version = '1.0.0'\n"
            "requires-python = '>=3.8'\n",
            encoding="utf-8",
        )
        self.git(self.publisher, "add", "pyproject.toml", "repogents")
        self.git(self.publisher, "commit", "-m", "add compatible Python requirement")
        self.git(self.publisher, "push", "origin", "main")

        with tempfile.TemporaryDirectory() as environment_directory:
            environment = Path(environment_directory)
            subprocess.run(
                [sys.executable, "-m", "venv", str(environment)], check=True
            )
            bin_directory = "Scripts" if os.name == "nt" else "bin"
            python = environment / bin_directory / (
                "python.exe" if os.name == "nt" else "python"
            )
            controller = RecordingServiceController()
            updater = MainBranchUpdater(
                repository=self.checkout,
                state_file=self.state_file,
                service_name="repogents.service",
                service_controller=controller,
            )
            with patch("repogents.updater.sys.executable", str(python)), patch.dict(
                os.environ, {**os.environ, "PIP_NO_INDEX": "1"}, clear=True
            ):
                compatible = updater.check_once()

            self.assertTrue(compatible.updated)
            self.assertTrue(compatible.restarted)
            self.assertEqual(controller.restart_calls, ["repogents.service"])
            compatible_sha = compatible.commit_sha
            self.assertEqual(
                self.state_file.read_text(encoding="utf-8"), f"{compatible_sha}\n"
            )
            metadata = subprocess.run(
                [
                    str(python),
                    "-c",
                    "from importlib.metadata import metadata; "
                    "print(metadata('repogents')['Requires-Python'])",
                ],
                cwd=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(metadata.stdout, ">=3.8\n")
            refresh = __import__("json").loads(
                (self.state_file.parent / "editable-refresh" / "refresh.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(refresh["requires_python"], ">=3.8")

            incompatible = f">={sys.version_info.major + 1}.0"
            pyproject = self.publisher / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    "requires-python = '>=3.8'",
                    f"requires-python = '{incompatible}'",
                ),
                encoding="utf-8",
            )
            self.git(self.publisher, "add", "pyproject.toml")
            self.git(self.publisher, "commit", "-m", "require incompatible Python")
            self.git(self.publisher, "push", "origin", "main")
            incompatible_sha = self.git(
                self.publisher, "rev-parse", "HEAD"
            ).stdout.strip()

            with patch("repogents.updater.sys.executable", str(python)), patch.dict(
                os.environ, {**os.environ, "PIP_NO_INDEX": "1"}, clear=True
            ):
                with self.assertRaisesRegex(
                    Exception, "refresh editable Repogents installation"
                ):
                    updater.check_once()

            self.assertEqual(controller.restart_calls, ["repogents.service"])
            self.assertEqual(
                self.state_file.read_text(encoding="utf-8"), f"{compatible_sha}\n"
            )
            self.assertEqual(
                self.git(self.checkout, "rev-parse", "HEAD").stdout.strip(),
                incompatible_sha,
            )
            generated = __import__("json").loads(
                (self.state_file.parent / "editable-refresh" / "refresh.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(generated["requires_python"], incompatible)

    def test_failed_metadata_refresh_does_not_restart_or_advance_checkpoint(self) -> None:
        self.record_restarted_commit(self.initial_sha)
        expected_sha = self.publish()
        controller = RecordingServiceController()
        updater = self.updater(controller)

        def fail_refresh() -> None:
            raise RuntimeError("metadata refresh failed")

        updater._refresh_installation_metadata = fail_refresh
        with self.assertRaisesRegex(RuntimeError, "metadata refresh failed"):
            updater.check_once()

        self.assertEqual(controller.restart_calls, [])
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            f"{self.initial_sha}\n",
        )
        self.assertEqual(
            self.git(self.checkout, "rev-parse", "HEAD").stdout.strip(),
            expected_sha,
        )


    def test_absent_checkpoint_retries_refresh_before_restart(self) -> None:
        expected_sha = self.publish()
        controller = RecordingServiceController()
        updater = self.updater(controller)
        refresh_calls = 0

        def refresh() -> None:
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls == 1:
                raise RuntimeError("installation refresh failed")

        updater._refresh_installation_metadata = refresh

        with self.assertRaisesRegex(RuntimeError, "installation refresh failed"):
            updater.check_once()

        self.assertFalse(self.state_file.exists())
        self.assertEqual(controller.restart_calls, [])
        self.assertEqual(
            self.git(self.checkout, "rev-parse", "HEAD").stdout.strip(),
            expected_sha,
        )

        outcome = updater.check_once()

        self.assertFalse(outcome.updated)
        self.assertTrue(outcome.restarted)
        self.assertEqual(refresh_calls, 2)
        self.assertEqual(controller.restart_calls, ["repogents.service"])
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            f"{expected_sha}\n",
        )


    def test_installation_refresh_synchronizes_dependencies_before_restart(self) -> None:
        (self.checkout / "pyproject.toml").write_text(
            "[project]\nname = 'repogents'\nversion = '1.0.0'\n",
            encoding="utf-8",
        )
        events: list[str] = []
        controller = RecordingServiceController(events=events)
        updater = MainBranchUpdater(
            repository=self.checkout,
            state_file=self.state_file,
            service_name="repogents.service",
            service_controller=controller,
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        commands: list[list[str]] = []

        def run_refresh(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            command = args[0]
            self.assertIsInstance(command, list)
            commands.append(command)
            events.append("refresh" if len(commands) == 1 else "dependencies")
            return completed

        with patch("repogents.updater.subprocess.run", side_effect=run_refresh):
            updater._refresh_installation_metadata()

        controller.restart_and_verify("repogents.service")
        self.assertEqual(events, ["refresh", "dependencies", "restart"])
        self.assertEqual(len(commands), 2)
        persistent_target = self.state_file.parent / "editable-refresh"
        self.assertTrue(persistent_target.is_dir())
        for command in commands:
            self.assertEqual(command[:4], [sys.executable, "-m", "pip", "install"])
            self.assertNotIn("--no-build-isolation", command)
            self.assertNotIn("--ignore-installed", command)
            self.assertIn("--editable", command)
            self.assertEqual(command[-1], str(persistent_target))
        self.assertIn("--force-reinstall", commands[0])
        self.assertIn("--no-deps", commands[0])
        self.assertNotIn("--force-reinstall", commands[1])
        self.assertNotIn("--no-deps", commands[1])

    def test_distribution_refresh_failure_prevents_dependency_reconciliation(self) -> None:
        (self.checkout / "pyproject.toml").write_text(
            "[project]\nname = 'repogents'\nversion = '1.0.0'\n",
            encoding="utf-8",
        )
        updater = MainBranchUpdater(
            repository=self.checkout,
            state_file=self.state_file,
            service_name="repogents.service",
            service_controller=RecordingServiceController(),
        )
        failed = subprocess.CompletedProcess([], 1, "", "refresh failed")
        with patch("repogents.updater.subprocess.run", return_value=failed) as run:
            with self.assertRaisesRegex(
                Exception, "refresh editable Repogents installation"
            ):
                updater._refresh_installation_metadata()
        self.assertEqual(run.call_count, 1)

    def test_dependency_synchronization_failure_prevents_restart_and_checkpoint(self) -> None:
        (self.publisher / "pyproject.toml").write_text(
            "[project]\nname = 'repogents'\nversion = '1.0.0'\n",
            encoding="utf-8",
        )
        self.git(self.publisher, "add", "pyproject.toml")
        self.git(self.publisher, "commit", "-m", "add project metadata")
        self.git(self.publisher, "push", "origin", "main")
        self.git(self.checkout, "pull", "--ff-only", "origin", "main")
        self.initial_sha = self.git(self.checkout, "rev-parse", "HEAD").stdout.strip()
        self.record_restarted_commit(self.initial_sha)
        expected_sha = self.publish()
        controller = RecordingServiceController()
        updater = MainBranchUpdater(
            repository=self.checkout,
            state_file=self.state_file,
            service_name="repogents.service",
            service_controller=controller,
        )
        original_run = subprocess.run
        install_calls = 0

        def fail_dependency_install(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal install_calls
            command = args[0]
            if (
                isinstance(command, list)
                and command[:4] == [sys.executable, "-m", "pip", "install"]
            ):
                install_calls += 1
                if install_calls == 1:
                    self.assertIn("--force-reinstall", command)
                    self.assertIn("--no-deps", command)
                    return subprocess.CompletedProcess(command, 0, "", "")
                self.assertNotIn("--force-reinstall", command)
                self.assertNotIn("--no-deps", command)
                return subprocess.CompletedProcess(
                    command, 1, "", "dependency install failed"
                )
            return original_run(*args, **kwargs)

        with patch(
            "repogents.updater.subprocess.run", side_effect=fail_dependency_install
        ):
            with self.assertRaisesRegex(
                Exception, "reconcile Repogents runtime dependencies"
            ):
                updater.check_once()

        self.assertEqual(install_calls, 2)
        self.assertEqual(controller.restart_calls, [])
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            f"{self.initial_sha}\n",
        )
        self.assertEqual(
            self.git(self.checkout, "rev-parse", "HEAD").stdout.strip(),
            expected_sha,
        )


if __name__ == "__main__":
    unittest.main()
