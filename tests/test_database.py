from __future__ import annotations

import sqlite3
import threading
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from repogents.database import Database, SCHEMA_V1


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "repogents.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()

    def seed_repository_run(
        self, *, run_id: str = "run-1", event_id: str = "event-1"
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?)""",
                (
                    "repo-1",
                    "R_node",
                    "owner",
                    "repo",
                    "https://github.com/owner/repo",
                    "main",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES (?, ?, 1, ?, '{}', '{}', ?)""",
                (
                    "sandbox-1",
                    "repo-1",
                    "/data/repositories/repo-1/sandbox/1",
                    "2026-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES (?, ?, 1, '{}', ?)""",
                ("team-1", "repo-1", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES (?, ?, 'lead', 'lead', 'Own the result', '[\"read\"]', 'test', 'test', '')""",
                ("member-1", "team-1"),
            )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES (?, ?, ?, 3, ?, 'Issue', 'Body', '[]', ?)""",
                (
                    "issue-1",
                    "repo-1",
                    "I_node",
                    "https://github.com/owner/repo/issues/3",
                    "2026-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    f"activation-{event_id}",
                    "repo-1",
                    "issue-1",
                    event_id,
                    "2026-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'main', ?, 'queued', ?, ?)""",
                (
                    run_id,
                    "repo-1",
                    "issue-1",
                    f"activation-{event_id}",
                    "sandbox-1",
                    "team-1",
                    "a" * 40,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )

    def test_initialization_is_idempotent_and_enables_integrity_modes(self) -> None:
        self.db.initialize()
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
            )
            version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            self.assertEqual(version, 4)

    def test_schema_v1_team_timeout_is_backfilled_idempotently(self) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(SCHEMA_V1)
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES ('legacy-lead', 'legacy-team', 'lead', 'lead', 'Own',
                           '["read"]', 'mini-swe-agent', 'legacy-model', '')""")
        migrated = Database(legacy_path)
        migrated.initialize()
        migrated.initialize()
        with migrated.connect() as connection:
            timeout = connection.execute("""SELECT action_timeout_seconds
                   FROM team_members WHERE id='legacy-lead'""").fetchone()[0]
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_version ORDER BY version"
                )
            )
        self.assertEqual(timeout, 300)
        self.assertEqual(versions, (1, 2, 3, 4))

    def test_concurrent_schema_v1_migration_converges_once(self) -> None:
        legacy_path = Path(self.tempdir.name) / "concurrent-legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(SCHEMA_V1)
        real_connect = sqlite3.connect
        ready = threading.Barrier(2)

        def synchronized_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            connection = real_connect(*args, **kwargs)
            synchronized = False

            def trace(statement: str) -> None:
                nonlocal synchronized
                if not synchronized and "FROM sqlite_master" in statement:
                    synchronized = True
                    ready.wait(timeout=5)

            connection.set_trace_callback(trace)
            return connection

        errors: list[BaseException] = []

        def initialize() -> None:
            try:
                Database(legacy_path).initialize()
            except BaseException as error:
                errors.append(error)

        with mock.patch(
            "repogents.database.sqlite3.connect",
            side_effect=synchronized_connect,
        ):
            workers = [threading.Thread(target=initialize) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        with Database(legacy_path).connect() as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_version ORDER BY version"
                )
            )
            columns = tuple(
                row["name"]
                for row in connection.execute("PRAGMA table_info(team_members)")
            )
        self.assertEqual(versions, (1, 2, 3, 4))
        self.assertEqual(columns.count("action_timeout_seconds"), 1)
        with Database(legacy_path).connect() as connection:
            acceptance_tables = {
                row["name"]
                for row in connection.execute("""SELECT name FROM sqlite_master
                       WHERE type='table' AND name LIKE 'acceptance_%'""")
            }
        self.assertEqual(
            acceptance_tables,
            {
                "acceptance_verifications",
                "acceptance_evidence",
                "acceptance_artifacts",
            },
        )

    def test_duplicate_activation_event_is_rejected(self) -> None:
        self.seed_repository_run()
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.db.transaction() as connection,
        ):
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-2', 'repo-1', 'issue-1', 'event-1', ?)""",
                ("2026-01-01T00:00:00Z",),
            )

    def test_second_nonterminal_run_for_issue_is_rejected(self) -> None:
        self.seed_repository_run()
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-2', 'repo-1', 'issue-1', 'event-2', ?)""",
                ("2026-01-01T00:00:01Z",),
            )
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.db.transaction() as connection,
        ):
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, created_at, updated_at)
                   VALUES ('run-2', 'repo-1', 'issue-1', 'activation-2',
                           'sandbox-1', 'team-1', 'main', ?, 'blocked', ?, ?)""",
                ("b" * 40, "2026-01-01T00:00:01Z", "2026-01-01T00:00:01Z"),
            )

    def test_terminal_run_allows_later_activation_run(self) -> None:
        self.seed_repository_run()
        with self.db.transaction() as connection:
            connection.execute("UPDATE runs SET state = 'closed' WHERE id = 'run-1'")
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-2', 'repo-1', 'issue-1', 'event-2', ?)""",
                ("2026-01-01T00:00:01Z",),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, created_at, updated_at)
                   VALUES ('run-2', 'repo-1', 'issue-1', 'activation-2',
                           'sandbox-1', 'team-1', 'main', ?, 'queued', ?, ?)""",
                ("b" * 40, "2026-01-01T00:00:01Z", "2026-01-01T00:00:01Z"),
            )

    def test_duplicate_feedback_pull_request_and_notification_are_rejected(
        self,
    ) -> None:
        self.seed_repository_run()
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-1', 'run-1', 'PR_node', 9, ?, ?, 'main', ?, ?, ?, 'open', ?, ?)""",
                (
                    "https://github.com/owner/repo/pull/9",
                    "agent/issue-3-run-1",
                    "a" * 40,
                    "b" * 40,
                    "b" * 40,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at)
                   VALUES ('feedback-1', 'pr-1', 'comment', 'comment-1',
                           '2026-01-01T00:00:00Z', 'reviewer', 'change it', 'pending', ?)""",
                ("2026-01-01T00:00:00Z",),
            )
            connection.execute(
                """INSERT INTO quiet_periods
                   (id, run_id, generation, started_at, deadline, state)
                   VALUES ('quiet-1', 'run-1', 1, ?, ?, 'active')""",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:30:00Z"),
            )
            connection.execute(
                """INSERT INTO notifications
                   (id, quiet_period_id, created_at)
                   VALUES ('notification-1', 'quiet-1', ?)""",
                ("2026-01-01T00:30:00Z",),
            )

        conflicting_statements = [
            (
                "INSERT INTO pull_requests (id, run_id, github_node_id, number, url, branch_name, intended_base_branch, base_sha, validated_head_sha, state, created_at, updated_at) VALUES ('pr-2', 'run-1', 'PR_node_2', 10, 'u2', 'b2', 'main', ?, ?, 'open', ?, ?)",
                ("a" * 40, "b" * 40, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            ),
            (
                "INSERT INTO feedback_versions (id, pull_request_id, feedback_type, github_object_id, github_version, author, body, state, observed_at) VALUES ('feedback-2', 'pr-1', 'comment', 'comment-1', '2026-01-01T00:00:00Z', 'reviewer', 'duplicate', 'pending', ?)",
                ("2026-01-01T00:00:00Z",),
            ),
            (
                "INSERT INTO notifications (id, quiet_period_id, created_at) VALUES ('notification-2', 'quiet-1', ?)",
                ("2026-01-01T00:30:01Z",),
            ),
        ]
        for statement, parameters in conflicting_statements:
            with (
                self.assertRaises(sqlite3.IntegrityError),
                self.db.transaction() as connection,
            ):
                connection.execute(statement, parameters)

    def test_pending_external_operation_and_full_state_survive_reopen(self) -> None:
        self.seed_repository_run()
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state, created_at)
                   VALUES ('operation-1', 'run-1', 'create_pull_request',
                           'run-1:create_pull_request', '{}', 'pending', ?)""",
                ("2026-01-01T00:00:00Z",),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-1', 'run-1', 'PR_node', 9, 'pr-url', 'branch',
                           'main', ?, ?, ?, 'open', ?, ?)""",
                (
                    "a" * 40,
                    "b" * 40,
                    "b" * 40,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at)
                   VALUES ('feedback-1', 'pr-1', 'comment', 'comment-1',
                           'v1', 'reviewer', 'change it', 'pending', ?)""",
                ("2026-01-01T00:00:00Z",),
            )
            connection.execute(
                """INSERT INTO quiet_periods
                   (id, run_id, generation, started_at, deadline, state)
                   VALUES ('quiet-1', 'run-1', 1, ?, ?, 'active')""",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:30:00Z"),
            )
        reopened = Database(self.path)
        reopened.initialize()
        with reopened.connect() as connection:
            run = connection.execute(
                """SELECT runs.state, runs.base_sha, sandbox_versions.id, team_versions.id,
                          outbound_operations.state, feedback_versions.state,
                          quiet_periods.deadline
                   FROM runs
                   JOIN sandbox_versions ON sandbox_versions.id = runs.sandbox_version_id
                   JOIN team_versions ON team_versions.id = runs.team_version_id
                   JOIN outbound_operations ON outbound_operations.run_id = runs.id
                   JOIN pull_requests ON pull_requests.run_id = runs.id
                   JOIN feedback_versions ON feedback_versions.pull_request_id = pull_requests.id
                   JOIN quiet_periods ON quiet_periods.run_id = runs.id
                   WHERE runs.id = 'run-1'"""
            ).fetchone()
        self.assertEqual(
            tuple(run),
            (
                "queued",
                "a" * 40,
                "sandbox-1",
                "team-1",
                "pending",
                "pending",
                "2026-01-01T00:30:00Z",
            ),
        )

    def test_stored_runtime_model_timeout_survives_database_reopen(self) -> None:
        """Assert exact stored runtime/model/timeout survives restart."""
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-rm', 'R_rm', 'owner', 'repo', 'repo-url', 'main',
                           'ready', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-rm', 'repo-rm', 1, '{}', ?)""",
                (now,),
            )
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('lead-rm', 'team-rm', 'lead', 'lead', 'Own result',
                           '["read"]', 'mini-swe-agent', 'openai/gpt-4', '', 321)""")
        reopened = Database(self.db.path)
        reopened.initialize()
        with reopened.connect() as connection:
            row = connection.execute("""SELECT runtime, model, action_timeout_seconds
                   FROM team_members WHERE id='lead-rm'""").fetchone()
        self.assertEqual(row["runtime"], "mini-swe-agent")
        self.assertEqual(row["model"], "openai/gpt-4")
        self.assertEqual(row["action_timeout_seconds"], 321)

    def test_obsolete_omp_runtime_row_preserved_without_silent_migration(self) -> None:
        """Legacy 'omp' runtime rows are not silently migrated to 'mini-swe-agent'."""
        legacy_path = Path(self.tempdir.name) / "legacy-omp.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(SCHEMA_V1)
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES ('legacy-lead', 'legacy-team', 'lead', 'lead', 'Own',
                           '["read"]', 'omp', 'legacy-model', '')""")
        migrated = Database(legacy_path)
        migrated.initialize()
        with migrated.connect() as connection:
            row = connection.execute("""SELECT runtime, action_timeout_seconds
                   FROM team_members WHERE id='legacy-lead'""").fetchone()
        self.assertEqual(row["runtime"], "omp")
        self.assertEqual(row["action_timeout_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
