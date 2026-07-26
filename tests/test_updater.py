from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from repogents.updater import MainBranchUpdater, UpdateRefused


class RecordingServiceController:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.restart_calls: list[str] = []

    def restart_and_verify(self, service_name: str) -> None:
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
        return MainBranchUpdater(
            repository=self.checkout,
            state_file=self.state_file,
            service_name="repogents.service",
            service_controller=controller,
        )

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
        controller = RecordingServiceController()

        outcome = self.updater(controller).check_once()

        self.assertFalse(outcome.updated)
        self.assertFalse(outcome.restarted)
        self.assertEqual(outcome.commit_sha, self.initial_sha)
        self.assertEqual(controller.restart_calls, [])

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


if __name__ == "__main__":
    unittest.main()
