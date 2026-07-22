from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from repogents.database import Database
from repogents.github import PullRequestInfo
from repogents.lifecycle import RunLifecycle, RunState
from repogents.quiet import QuietPeriodService, TransientQuietCheckError
from repogents.sandbox import SandboxManager


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakePullGateway:
    def __init__(self, pull: PullRequestInfo) -> None:
        self.pull = pull
        self.error: Exception | None = None
        self.calls = 0

    def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestInfo:
        self.calls += 1
        if self.error:
            raise self.error
        return self.pull


class FakeFeedbackPoller:
    def __init__(self) -> None:
        self.new_feedback = 0
        self.error: Exception | None = None
        self.calls = 0

    def poll_run(self, run_id: str) -> int:
        self.calls += 1
        if self.error:
            raise self.error
        return self.new_feedback


class NoActivationClient:
    def list_ready_events(self, owner: str, name: str) -> list[object]:
        return []

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        return "a" * 40


class NoCheckoutManager:
    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        return None


class QuietPeriodTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        now = "2026-01-01T00:00:00Z"
        sandbox = self.data_root / "repositories" / "repo-1" / "sandbox" / "1"
        sandbox.mkdir(parents=True)
        checkout = self.data_root / "repositories" / "repo-1" / "runs" / "run-1" / "checkout"
        checkout.mkdir(parents=True)
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'repo-url', 'main', 'ready', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, ?, '{}', '{}', ?)""",
                (str(sandbox), now),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, '{}', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own', '[]', 'test', 'test', '')"""
            )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 3, 'issue-url', 'Issue', 'Body', '[]', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-1', 'repo-1', 'issue-1', 'event-1', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, last_completed_state, validated_sha,
                    checkout_path, run_path, created_at, updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'activation-1',
                           'sandbox-1', 'team-1', 'main', ?, 'waiting_for_feedback',
                           'publishing', ?, ?, ?, ?, ?)""",
                ("a" * 40, "b" * 40, str(checkout), str(checkout.parent), now, now),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-1', 'run-1', 'PR1', 11, 'pr-url', 'agent/issue-3-run-1',
                           'main', ?, ?, ?, 'open', ?, ?)""",
                ("a" * 40, "b" * 40, "b" * 40, now, now),
            )
        self.lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=SandboxManager(),
        )
        self.clock = MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.pull = PullRequestInfo(
            node_id="PR1",
            number=11,
            url="pr-url",
            state="open",
            merged=False,
            head_branch="agent/issue-3-run-1",
            head_sha="b" * 40,
            base_branch="main",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.gateway = FakePullGateway(self.pull)
        self.feedback = FakeFeedbackPoller()
        self.service = QuietPeriodService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            feedback=self.feedback,
            clock=self.clock,
        )

    def test_notifies_once_only_after_exact_verified_quiet_deadline(self) -> None:
        quiet_id = self.service.start("run-1")
        with self.db.connect() as connection:
            quiet = connection.execute("SELECT * FROM quiet_periods").fetchone()
        self.assertEqual(quiet["id"], quiet_id)
        self.assertEqual(quiet["deadline"], "2026-01-01T00:30:00Z")
        self.clock.value += timedelta(minutes=29, seconds=59)
        self.assertIsNone(self.service.check_due("run-1"))
        self.assertEqual(self.gateway.calls, 0)
        self.clock.value += timedelta(seconds=1)
        notification_id = self.service.check_due("run-1")
        self.assertIsNotNone(notification_id)
        self.assertEqual(self.service.check_due("run-1"), notification_id)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "notified")

    def test_subsecond_start_never_shortens_quiet_deadline(self) -> None:
        self.clock.value = datetime(
            2026, 1, 1, microsecond=900_000, tzinfo=timezone.utc
        )
        self.service.start("run-1")
        with self.db.connect() as connection:
            quiet = connection.execute(
                "SELECT started_at, deadline FROM quiet_periods"
            ).fetchone()
        self.assertEqual(quiet["started_at"], "2026-01-01T00:00:00.900000Z")
        self.assertEqual(quiet["deadline"], "2026-01-01T00:30:00.900000Z")

        self.clock.value += timedelta(minutes=30, microseconds=-1)
        self.assertIsNone(self.service.check_due("run-1"))
        self.assertEqual(self.gateway.calls, 0)

        self.clock.value += timedelta(microseconds=1)
        self.assertIsNotNone(self.service.check_due("run-1"))

    def test_restart_during_quiet_period_preserves_generation_and_deadline(self) -> None:
        quiet_id = self.service.start("run-1")
        self.clock.value += timedelta(minutes=10)
        restarted = QuietPeriodService(
            database=Database(self.root / "db.sqlite3"),
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            feedback=self.feedback,
            clock=self.clock,
        )
        restarted.database.initialize()
        self.assertEqual(restarted.start("run-1"), quiet_id)
        with self.db.connect() as connection:
            periods = connection.execute("SELECT generation, deadline FROM quiet_periods").fetchall()
        self.assertEqual([(row["generation"], row["deadline"]) for row in periods], [(1, "2026-01-01T00:30:00Z")])

    def test_gateway_failure_at_deadline_preserves_active_generation(self) -> None:
        self.service.start("run-1")
        self.clock.value += timedelta(minutes=30)
        with self.db.connect() as connection:
            before = dict(
                connection.execute("SELECT * FROM quiet_periods").fetchone()
            )

        self.gateway.error = RuntimeError("GitHub unavailable")
        with self.assertRaisesRegex(
            TransientQuietCheckError, "pull-request status poll"
        ) as raised:
            self.service.check_due("run-1")
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

        with self.db.connect() as connection:
            after = dict(
                connection.execute("SELECT * FROM quiet_periods").fetchone()
            )
            notification_count = connection.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(notification_count, 0)
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"], RunState.QUIET_PERIOD.value
        )

        self.gateway.error = None
        self.assertIsNotNone(self.service.check_due("run-1"))

    def test_feedback_failure_at_deadline_preserves_active_generation(
        self,
    ) -> None:
        self.service.start("run-1")
        self.clock.value += timedelta(minutes=30)
        with self.db.connect() as connection:
            before = dict(
                connection.execute("SELECT * FROM quiet_periods").fetchone()
            )

        self.feedback.error = RuntimeError("feedback unavailable")
        with self.assertRaisesRegex(
            TransientQuietCheckError, "feedback poll"
        ) as raised:
            self.service.check_due("run-1")
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

        with self.db.connect() as connection:
            after = dict(
                connection.execute("SELECT * FROM quiet_periods").fetchone()
            )
            notification_count = connection.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(notification_count, 0)
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"], RunState.QUIET_PERIOD.value
        )

        self.feedback.error = None
        self.assertIsNotNone(self.service.check_due("run-1"))

    def test_new_feedback_at_deadline_cancels_generation(self) -> None:
        self.service.start("run-1")
        self.clock.value += timedelta(minutes=30)
        self.feedback.new_feedback = 1
        self.assertIsNone(self.service.check_due("run-1"))
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT state FROM quiet_periods").fetchone()[0], "canceled")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "resolving_feedback")

    def test_external_close_or_merge_closes_run_without_notification(self) -> None:
        self.service.start("run-1")
        self.clock.value += timedelta(minutes=30)
        self.gateway.pull = PullRequestInfo(**(self.pull.__dict__ | {"state": "closed"}))
        self.assertIsNone(self.service.check_due("run-1"))
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "closed")
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)

    def test_closure_during_feedback_poll_cannot_create_notification(self) -> None:
        outer = self

        class ClosingFeedback:
            def poll_run(self, run_id: str) -> int:
                now = "2026-01-01T00:30:00Z"
                with outer.db.transaction() as connection:
                    connection.execute(
                        """UPDATE quiet_periods
                           SET state='canceled', canceled_at=?
                           WHERE run_id=? AND state='active'""",
                        (now, run_id),
                    )
                    connection.execute(
                        """UPDATE pull_requests
                           SET state='closed', updated_at=? WHERE run_id=?""",
                        (now, run_id),
                    )
                outer.lifecycle.transition(
                    run_id,
                    RunState.CLOSED,
                    reason="pull request was closed during feedback poll",
                )
                return 0

        self.service.start("run-1")
        self.clock.value += timedelta(minutes=30)
        self.service.feedback = ClosingFeedback()

        self.assertIsNone(self.service.check_due("run-1"))

        with self.db.connect() as connection:
            quiet_state = connection.execute(
                "SELECT state FROM quiet_periods"
            ).fetchone()[0]
            notification_count = connection.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0]
        self.assertEqual(quiet_state, "canceled")
        self.assertEqual(notification_count, 0)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "closed")

    def test_external_closure_is_atomic_and_retryable_after_interruption(
        self,
    ) -> None:
        self.service.start("run-1")
        self.clock.value += timedelta(minutes=30)
        self.gateway.pull = PullRequestInfo(
            **(self.pull.__dict__ | {"state": "closed"})
        )
        with self.db.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER interrupt_external_close
                   BEFORE UPDATE OF state ON runs
                   WHEN NEW.state='closed'
                   BEGIN
                     SELECT RAISE(ABORT, 'injected external-close interruption');
                   END"""
            )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected external-close interruption"
        ):
            self.service.check_due("run-1")

        with self.db.connect() as connection:
            quiet_state = connection.execute(
                "SELECT state FROM quiet_periods"
            ).fetchone()[0]
            pull_state = connection.execute(
                "SELECT state FROM pull_requests WHERE run_id='run-1'"
            ).fetchone()[0]
        self.assertEqual(quiet_state, "active")
        self.assertEqual(pull_state, "open")
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "quiet_period")

        with self.db.transaction() as connection:
            connection.execute("DROP TRIGGER interrupt_external_close")
        restarted = QuietPeriodService(
            database=Database(self.root / "db.sqlite3"),
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            feedback=self.feedback,
            clock=self.clock,
        )
        restarted.database.initialize()
        self.assertIsNone(restarted.check_due("run-1"))

        with self.db.connect() as connection:
            states = connection.execute(
                """SELECT quiet_periods.state AS quiet_state,
                          pull_requests.state AS pull_state,
                          runs.state AS run_state
                   FROM quiet_periods
                   JOIN runs ON runs.id=quiet_periods.run_id
                   JOIN pull_requests ON pull_requests.run_id=runs.id"""
            ).fetchone()
            transition = connection.execute(
                """SELECT from_state, to_state, reason
                   FROM run_transitions
                   WHERE run_id='run-1'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            notification_count = connection.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0]
        self.assertEqual(
            dict(states),
            {
                "quiet_state": "canceled",
                "pull_state": "closed",
                "run_state": "closed",
            },
        )
        self.assertEqual(transition["from_state"], "quiet_period")
        self.assertEqual(transition["to_state"], "closed")
        self.assertEqual(
            transition["reason"],
            "pull request was merged or closed by an external actor",
        )
        self.assertEqual(notification_count, 0)

    def test_feedback_after_notification_can_create_second_generation(self) -> None:
        self.service.start("run-1")
        self.clock.value += timedelta(minutes=30)
        first_notification = self.service.check_due("run-1")
        self.lifecycle.transition("run-1", RunState.RESOLVING_FEEDBACK)
        self.lifecycle.transition("run-1", RunState.WAITING_FOR_FEEDBACK)
        second_quiet = self.service.start("run-1")
        self.assertIsNotNone(second_quiet)
        self.assertNotEqual(second_quiet, first_notification)
        self.clock.value += timedelta(minutes=30)
        second_notification = self.service.check_due("run-1")
        self.assertNotEqual(first_notification, second_notification)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT MAX(generation) FROM quiet_periods").fetchone()[0], 2)

    def test_acknowledges_persistent_notification(self) -> None:
        self.service.start("run-1")
        self.clock.value += timedelta(minutes=30)
        notification_id = self.service.check_due("run-1")
        self.service.acknowledge(str(notification_id))
        with self.db.connect() as connection:
            read_at = connection.execute("SELECT read_at FROM notifications").fetchone()[0]
        self.assertIsNotNone(read_at)


if __name__ == "__main__":
    unittest.main()
