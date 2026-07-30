from __future__ import annotations

import json
import sqlite3
import threading
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from repogents.database import (
    Database,
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
    SCHEMA_V7,
    SCHEMA_V8,
    SCHEMA_V9,
    SCHEMA_V10,
    SCHEMA_V11,
    SCHEMA_V12,
    SCHEMA_V13,
    SCHEMA_V14,
    SCHEMA_V15,
    SCHEMA_V16,
    SCHEMA_V17,
    SCHEMA_V18,
    SCHEMA_V19,
    SCHEMA_V20,
    SCHEMA_V21,
    SCHEMA_V22,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "repogents.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()

    def legacy_database(self, name: str, version: int) -> Database:
        path = Path(self.tempdir.name) / name
        migrations = {
            2: (SCHEMA_V2,),
            3: SCHEMA_V3,
            4: SCHEMA_V4,
            5: SCHEMA_V5,
            6: SCHEMA_V6,
            7: SCHEMA_V7,
            8: SCHEMA_V8,
            9: SCHEMA_V9,
            10: SCHEMA_V10,
            11: SCHEMA_V11,
            12: SCHEMA_V12,
            13: SCHEMA_V13,
            14: SCHEMA_V14,
            15: SCHEMA_V15,
            16: SCHEMA_V16,
            17: SCHEMA_V17,
            18: SCHEMA_V18,
            19: SCHEMA_V19,
            20: SCHEMA_V20,
            21: SCHEMA_V21,
        }
        with sqlite3.connect(path) as connection:
            connection.executescript(SCHEMA_V1)
            for migration_version in range(2, version + 1):
                for statement in migrations[migration_version]:
                    connection.execute(statement)
                connection.execute(
                    """INSERT INTO schema_version(version, applied_at)
                       VALUES (?, '2026-01-01T00:00:00Z')""",
                    (migration_version,),
                )
        return Database(path)

    def seed_repository_run(
        self,
        *,
        run_id: str = "run-1",
        event_id: str = "event-1",
        database: Database | None = None,
    ) -> None:
        target = database or self.db
        with target.transaction() as connection:
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
            self.assertEqual(version, 22)

    def test_activity_revision_advances_only_after_durable_change(self) -> None:
        initial = self.db.activity_revision
        insert = """INSERT INTO repositories
                    (id, github_node_id, owner, name, url, default_branch,
                     onboarding_state, created_at, updated_at)
                    VALUES ('repo-activity', 'R_activity', 'owner', 'activity',
                            'https://github.com/owner/activity', 'main', 'ready',
                            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""

        with self.assertRaisesRegex(RuntimeError, "roll back"):
            with self.db.transaction() as connection:
                connection.execute(insert)
                raise RuntimeError("roll back")
        self.assertEqual(
            self.db.wait_for_activity_change(initial, timeout=0),
            initial,
        )

        with self.db.transaction() as connection:
            connection.execute(insert)
        committed = self.db.wait_for_activity_change(initial, timeout=0)
        self.assertGreater(committed, initial)

        self.db.notify_activity_change()
        self.assertGreater(
            self.db.wait_for_activity_change(committed, timeout=0),
            committed,
        )

    def test_validation_delta_schema_is_available_after_migration(self) -> None:
        with self.db.connect() as connection:
            baseline_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(validation_baselines)")
            }
            result_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(validation_results)")
            }
        self.assertEqual(
            baseline_columns,
            {
                "id",
                "run_id",
                "validation_command_id",
                "command_json",
                "base_sha",
                "mode",
                "started_at",
                "completed_at",
                "exit_status",
                "log_path",
                "findings_json",
            },
        )
        self.assertTrue(
            {
                "validation_command_id",
                "verdict",
                "findings_json",
                "comparison_json",
            }.issubset(result_columns)
        )

    def test_repository_controls_migrate_existing_inventory_as_enabled(self) -> None:
        legacy_path = Path(self.tempdir.name) / "repository-controls.sqlite3"
        now = "2026-01-01T00:00:00Z"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(SCHEMA_V1)
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('legacy-repo', 'legacy-node', 'owner', 'legacy',
                           'https://github.com/owner/legacy', 'main',
                           'ready', ?, ?)""",
                (now, now),
            )

        migrated = Database(legacy_path)
        migrated.initialize()

        with migrated.connect() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(repositories)")
            }
            repository = connection.execute(
                "SELECT enabled, removed_at FROM repositories WHERE id='legacy-repo'"
            ).fetchone()
        self.assertTrue({"enabled", "removed_at"}.issubset(columns))
        self.assertEqual(tuple(repository), (1, None))

    def test_schema_v1_team_timeout_is_backfilled_idempotently(self) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(SCHEMA_V1)
            connection.execute("""INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('legacy-team', 'legacy-repo', 1, '{}',
                           '2026-01-01T00:00:00Z')""")
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES ('legacy-lead', 'legacy-team', 'lead', 'lead', 'Own',
                           '["read"]', 'mini-swe-agent', 'legacy-model', '')""")
        migrated = Database(legacy_path)
        migrated.initialize()
        migrated.initialize()
        with migrated.connect() as connection:
            member = connection.execute("""SELECT action_timeout_seconds, atomic_role
                   FROM team_members WHERE id='legacy-lead'""").fetchone()
            contract_version = connection.execute("""SELECT design_contract_version
                   FROM team_versions WHERE id='legacy-team'""").fetchone()[0]
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_version ORDER BY version"
                )
            )
        self.assertEqual(tuple(member), (300, "lead"))
        self.assertEqual(contract_version, 1)
        self.assertEqual(versions, tuple(range(1, 23)))

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
        self.assertEqual(versions, tuple(range(1, 23)))
        self.assertEqual(columns.count("action_timeout_seconds"), 1)
        self.assertEqual(columns.count("atomic_role"), 1)
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

    def test_base_conflict_feedback_type_survives_reopen(self) -> None:
        self.seed_repository_run()
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-1', 'run-1', 'PR_node', 9, 'pr-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?,
                           'open', ?, ?)""",
                ("a" * 40, "b" * 40, "b" * 40, now, now),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at)
                   VALUES ('feedback-comment', 'pr-1', 'comment',
                           'comment-1', 'v1', 'reviewer', 'change it',
                           'pending', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at)
                   VALUES ('feedback-conflict', 'pr-1', 'base_conflict',
                           'PR_node', ?, 'github', 'resolve base conflict',
                           'pending', ?)""",
                (f"{'b' * 40}:{'c' * 40}", now),
            )

        reopened = Database(self.path)
        reopened.initialize()
        with reopened.connect() as connection:
            rows = connection.execute("""SELECT id, feedback_type
                   FROM feedback_versions
                   ORDER BY id""").fetchall()
        self.assertEqual(
            [(row["id"], row["feedback_type"]) for row in rows],
            [
                ("feedback-comment", "comment"),
                ("feedback-conflict", "base_conflict"),
            ],
        )

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

    def test_schema_v8_migration_backfills_deterministic_run_priorities(
        self,
    ) -> None:
        migrated = self.legacy_database("schema-v8.sqlite3", 8)
        self.seed_repository_run(database=migrated)
        with migrated.transaction() as connection:
            connection.execute("""INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-2', 'repo-1', 'I_node_2', 4,
                           'https://github.com/owner/repo/issues/4',
                           'Earlier issue', 'Body', '[]',
                           '2025-12-31T23:59:00Z')""")
            connection.execute("""INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-event-2', 'repo-1', 'issue-2',
                           'event-2', '2025-12-31T23:59:00Z')""")
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, created_at, updated_at)
                   VALUES ('run-2', 'repo-1', 'issue-2', 'activation-event-2',
                           'sandbox-1', 'team-1', 'main', ?, 'queued',
                           '2025-12-31T23:59:00Z',
                           '2025-12-31T23:59:00Z')""",
                ("b" * 40,),
            )
            connection.execute("""UPDATE runs
                   SET created_at='2026-01-01T00:00:00Z'
                   WHERE id='run-1'""")

        migrated.initialize()

        with migrated.connect() as connection:
            rows = connection.execute(
                "SELECT id, priority FROM runs ORDER BY priority"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("run-2", 0), ("run-1", 1)],
        )

    def test_schema_v11_backfills_issue_version_identity_without_losing_history(
        self,
    ) -> None:
        legacy_path = Path(self.tempdir.name) / "schema-v10.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(SCHEMA_V1)
            migrations = (
                (2, (SCHEMA_V2,)),
                (3, SCHEMA_V3),
                (4, SCHEMA_V4),
                (5, SCHEMA_V5),
                (6, SCHEMA_V6),
                (7, SCHEMA_V7),
                (8, SCHEMA_V8),
                (9, SCHEMA_V9),
                (10, SCHEMA_V10),
            )
            for version, statements in migrations:
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    """INSERT INTO schema_version(version, applied_at)
                       VALUES (?, '2026-01-01T00:00:00Z')""",
                    (version,),
                )
            connection.commit()
        finally:
            connection.close()

        legacy = Database(legacy_path)
        self.seed_repository_run(database=legacy)
        with legacy.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='publishing', validated_sha=?
                   WHERE id='run-1'""",
                ("b" * 40,),
            )
            connection.execute(
                """INSERT INTO acceptance_verifications
                   (id, run_id, commit_sha, attempt, verifier_member_id, state,
                    claims_json, screenshot_decision_json, started_at,
                    completed_at)
                   VALUES ('verification-1', 'run-1', ?, 1, 'member-1',
                           'passed', '[]', '{}',
                           '2026-01-01T00:01:00Z',
                           '2026-01-01T00:02:00Z')""",
                ("b" * 40,),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pull-1', 'run-1', 'PR1', 9, 'pull-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?, 'open',
                           '2026-01-01T00:02:00Z',
                           '2026-01-01T00:02:00Z')""",
                ("a" * 40, "b" * 40, "b" * 40),
            )

        migrated = Database(legacy_path)
        migrated.initialize()

        with migrated.connect() as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            issue_version = connection.execute(
                """SELECT issue_versions.*, issues.current_version_id
                   FROM issues
                   JOIN issue_versions
                     ON issue_versions.id=issues.current_version_id
                   WHERE issues.id='issue-1'"""
            ).fetchone()
            run = connection.execute("""SELECT validated_sha, validated_issue_version_id
                   FROM runs WHERE id='run-1'""").fetchone()
            activation_version = connection.execute(
                """SELECT issue_version_id FROM activation_events
                   WHERE id='activation-event-1'"""
            ).fetchone()[0]
            acceptance_version = connection.execute(
                """SELECT issue_version_id FROM acceptance_verifications
                   WHERE id='verification-1'"""
            ).fetchone()[0]
            pull_version = connection.execute(
                """SELECT validated_issue_version_id FROM pull_requests
                   WHERE id='pull-1'"""
            ).fetchone()[0]

        self.assertEqual(version, 22)
        self.assertEqual(issue_version["version"], 1)
        self.assertEqual(issue_version["title"], "Issue")
        self.assertEqual(issue_version["body"], "Body")
        self.assertEqual(issue_version["discussion_json"], "[]")
        self.assertEqual(issue_version["current_version_id"], issue_version["id"])
        self.assertEqual(run["validated_sha"], "b" * 40)
        self.assertEqual(run["validated_issue_version_id"], issue_version["id"])
        self.assertEqual(activation_version, issue_version["id"])
        self.assertEqual(acceptance_version, issue_version["id"])
        self.assertEqual(pull_version, issue_version["id"])

    def test_schema_v12_requeues_unverifiable_legacy_issue_proof(self) -> None:
        legacy = self.legacy_database("schema-v11-cutover.sqlite3", 10)
        self.seed_repository_run(database=legacy)
        now = "2026-01-01T00:02:00Z"
        with legacy.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='blocked',
                       last_completed_state='publishing',
                       reason='publication blocked: issue acceptance verification blocked',
                       validated_sha=?
                   WHERE id='run-1'""",
                ("b" * 40,),
            )
            connection.execute(
                """INSERT INTO acceptance_verifications
                   (id, run_id, commit_sha, attempt, verifier_member_id, state,
                    claims_json, screenshot_decision_json, report_json,
                    started_at, completed_at)
                   VALUES ('verification-legacy', 'run-1', ?, 1, 'member-1',
                           'blocked', '[]', '{}',
                           '{"summary":"obsolete requirement"}', ?, ?)""",
                ("b" * 40, now, now),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pull-legacy', 'run-1', 'PR1', 9, 'pull-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?, 'open',
                           ?, ?)""",
                ("a" * 40, "b" * 40, "b" * 40, now, now),
            )
            connection.execute(
                """INSERT INTO quiet_periods
                   (id, run_id, generation, started_at, deadline, state)
                   VALUES ('quiet-legacy', 'run-1', 1, ?, ?, 'active')""",
                (now, "2026-01-01T00:32:00Z"),
            )
            for statement in SCHEMA_V11:
                connection.execute(statement)
            connection.execute(
                """INSERT INTO schema_version(version, applied_at)
                   VALUES (11, ?)""",
                (now,),
            )

        migrated = Database(legacy.path)
        migrated.initialize()

        with migrated.connect() as connection:
            schema_version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            run = connection.execute("""SELECT state, reason, validated_sha,
                          validated_issue_version_id
                   FROM runs WHERE id='run-1'""").fetchone()
            verification = connection.execute("""SELECT state, issue_version_id
                   FROM acceptance_verifications
                   WHERE id='verification-legacy'""").fetchone()
            quiet_state = connection.execute(
                "SELECT state FROM quiet_periods WHERE id='quiet-legacy'"
            ).fetchone()[0]
            transition_count = connection.execute(
                """SELECT COUNT(*) FROM run_transitions
                   WHERE run_id='run-1'
                     AND from_state='blocked'
                     AND to_state='implementing'"""
            ).fetchone()[0]
            identities = connection.execute("""SELECT
                     (SELECT COUNT(*) FROM runs WHERE id='run-1'),
                     (SELECT COUNT(*) FROM pull_requests
                       WHERE id='pull-legacy' AND run_id='run-1')""").fetchone()

        self.assertEqual(schema_version, 22)
        self.assertEqual(run["state"], "implementing")
        self.assertIn("legacy issue snapshot", run["reason"])
        self.assertEqual(run["validated_sha"], "b" * 40)
        self.assertTrue(
            str(run["validated_issue_version_id"]).startswith("issue-version:")
        )
        self.assertEqual(verification["state"], "superseded")
        self.assertEqual(
            verification["issue_version_id"],
            run["validated_issue_version_id"],
        )
        self.assertEqual(quiet_state, "canceled")
        self.assertEqual(transition_count, 1)
        self.assertEqual(tuple(identities), (1, 1))

    def test_schema_v13_retires_quiet_deadlines_without_losing_evidence(
        self,
    ) -> None:
        legacy = self.legacy_database("schema-v12-quiet.sqlite3", 10)
        self.seed_repository_run(database=legacy)
        now = "2026-01-01T00:02:00Z"
        with legacy.transaction() as connection:
            for statement in SCHEMA_V11:
                connection.execute(statement)
            connection.execute(
                """INSERT INTO schema_version(version, applied_at)
                   VALUES (11, ?)""",
                (now,),
            )
            for statement in SCHEMA_V12:
                connection.execute(statement)
            connection.execute(
                """INSERT INTO schema_version(version, applied_at)
                   VALUES (12, ?)""",
                (now,),
            )
            connection.execute("""UPDATE runs
                   SET state='quiet_period',
                       last_completed_state='waiting_for_feedback'
                   WHERE id='run-1'""")
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pull-legacy', 'run-1', 'PR1', 9, 'pull-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?, 'open',
                           ?, ?)""",
                ("a" * 40, "b" * 40, "b" * 40, now, now),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at)
                   VALUES ('feedback-legacy', 'pull-legacy', 'review',
                           'review-1', 'v1', 'reviewer', 'Retained',
                           'resolved', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO quiet_periods
                   (id, run_id, generation, started_at, deadline, state)
                   VALUES ('quiet-legacy', 'run-1', 1, ?, ?, 'active')""",
                (now, "2026-01-01T00:32:00Z"),
            )
            connection.execute(
                """INSERT INTO notifications
                   (id, quiet_period_id, created_at)
                   VALUES ('notification-legacy', 'quiet-legacy', ?)""",
                (now,),
            )

        migrated = Database(legacy.path)
        migrated.initialize()

        with migrated.connect() as connection:
            schema_version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            run = connection.execute("""SELECT state, last_completed_state, reason
                   FROM runs WHERE id='run-1'""").fetchone()
            quiet = connection.execute("""SELECT state, canceled_at
                   FROM quiet_periods WHERE id='quiet-legacy'""").fetchone()
            evidence = connection.execute("""SELECT
                     (SELECT COUNT(*) FROM pull_requests
                       WHERE id='pull-legacy' AND run_id='run-1'),
                     (SELECT COUNT(*) FROM feedback_versions
                       WHERE id='feedback-legacy'
                         AND pull_request_id='pull-legacy'),
                     (SELECT COUNT(*) FROM notifications
                       WHERE id='notification-legacy'
                         AND quiet_period_id='quiet-legacy')""").fetchone()
            transition_count = connection.execute(
                """SELECT COUNT(*) FROM run_transitions
                   WHERE run_id='run-1'
                     AND from_state='quiet_period'
                     AND to_state='waiting_for_feedback'"""
            ).fetchone()[0]

        self.assertEqual(schema_version, 22)
        self.assertEqual(run["state"], "waiting_for_feedback")
        self.assertEqual(run["last_completed_state"], "waiting_for_feedback")
        self.assertIsNone(run["reason"])
        self.assertEqual(quiet["state"], "canceled")
        self.assertIsNotNone(quiet["canceled_at"])
        self.assertEqual(tuple(evidence), (1, 1, 1))
        self.assertEqual(transition_count, 1)

    def test_schema_v14_adds_review_thread_state_without_losing_feedback(
        self,
    ) -> None:
        legacy = self.legacy_database("schema-v13-feedback.sqlite3", 10)
        self.seed_repository_run(database=legacy)
        now = "2026-01-01T00:02:00Z"
        with legacy.transaction() as connection:
            for version, statements in (
                (11, SCHEMA_V11),
                (12, SCHEMA_V12),
                (13, SCHEMA_V13),
            ):
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    """INSERT INTO schema_version(version, applied_at)
                       VALUES (?, ?)""",
                    (version, now),
                )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pull-legacy', 'run-1', 'PR1', 9, 'pull-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?, 'open',
                           ?, ?)""",
                ("a" * 40, "b" * 40, "b" * 40, now, now),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, path, line, state, observed_at,
                    processed_at)
                   VALUES ('feedback-legacy', 'pull-legacy', 'inline_comment',
                           'comment-1', 'v1', 'reviewer', 'Retained', 'app.py',
                           10, 'resolved', ?, ?)""",
                (now, now),
            )

        migrated = Database(legacy.path)
        migrated.initialize()

        with migrated.connect() as connection:
            schema_version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(feedback_versions)")
            }
            feedback = connection.execute(
                """SELECT id, state, review_thread_id, review_thread_resolved
                   FROM feedback_versions WHERE id='feedback-legacy'"""
            ).fetchone()

        self.assertEqual(schema_version, 22)
        self.assertTrue(
            {"review_thread_id", "review_thread_resolved"}.issubset(columns)
        )
        self.assertEqual(
            tuple(feedback),
            ("feedback-legacy", "resolved", None, None),
        )

    def test_schema_v15_adds_conflict_supersession_without_losing_evidence(
        self,
    ) -> None:
        legacy = self.legacy_database("schema-v14-conflict.sqlite3", 10)
        self.seed_repository_run(database=legacy)
        now = "2026-01-01T00:02:00Z"
        with legacy.transaction() as connection:
            for version, statements in (
                (11, SCHEMA_V11),
                (12, SCHEMA_V12),
                (13, SCHEMA_V13),
                (14, SCHEMA_V14),
            ):
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    """INSERT INTO schema_version(version, applied_at)
                       VALUES (?, ?)""",
                    (version, now),
                )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pull-legacy', 'run-1', 'PR1', 9, 'pull-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?, 'open',
                           ?, ?)""",
                ("a" * 40, "b" * 40, "b" * 40, now, now),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, path, line, state, observed_at,
                    decision_json)
                   VALUES ('feedback-legacy', 'pull-legacy', 'base_conflict',
                           'PR1:head:base', 'head:base', 'repogents',
                           'Retained conflict', NULL, NULL, 'processing', ?,
                           '{"action":"revise","reason":"conflict","response":"fixed"}')""",
                (now,),
            )
            connection.execute(
                """INSERT INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state,
                    created_at)
                   VALUES ('operation-legacy', 'run-1',
                           'feedback_revision_batch', 'legacy-batch',
                           '{"feedback_ids":["feedback-legacy"]}', 'pending', ?)""",
                (now,),
            )

        migrated = Database(legacy.path)
        migrated.initialize()

        with migrated.connect() as connection:
            schema_version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(feedback_versions)")
            }
            feedback = connection.execute(
                """SELECT id, state, body, decision_json, superseded_at,
                          superseded_by_feedback_id
                   FROM feedback_versions WHERE id='feedback-legacy'"""
            ).fetchone()
            operation = connection.execute(
                """SELECT id, state, request_json
                   FROM outbound_operations WHERE id='operation-legacy'"""
            ).fetchone()
            integrity = connection.execute("PRAGMA foreign_key_check").fetchall()

        self.assertEqual(schema_version, 22)
        self.assertTrue(
            {"superseded_at", "superseded_by_feedback_id"}.issubset(columns)
        )
        self.assertEqual(
            tuple(feedback),
            (
                "feedback-legacy",
                "processing",
                "Retained conflict",
                '{"action":"revise","reason":"conflict","response":"fixed"}',
                None,
                None,
            ),
        )
        self.assertEqual(
            tuple(operation),
            (
                "operation-legacy",
                "pending",
                '{"feedback_ids":["feedback-legacy"]}',
            ),
        )
        self.assertEqual(integrity, [])

    def test_schema_v10_migration_scopes_forced_run_to_repository(
        self,
    ) -> None:
        migrated = self.legacy_database("schema-v9.sqlite3", 9)
        self.seed_repository_run(database=migrated)
        now = "2026-01-01T00:01:00Z"
        with migrated.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-2', 'R_node_2', 'owner', 'repo-2',
                           'https://github.com/owner/repo-2', 'main', 'ready',
                           ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-other', 'repo-2', 'I_node_other', 4,
                           'https://github.com/owner/repo-2/issues/4',
                           'Other', 'Body', '[]', ?),
                          ('issue-sibling', 'repo-1', 'I_node_sibling', 5,
                           'https://github.com/owner/repo/issues/5',
                           'Sibling', 'Body', '[]', ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-other', 'repo-2', 'issue-other',
                           'event-other', ?),
                          ('activation-sibling', 'repo-1', 'issue-sibling',
                           'event-sibling', ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, created_at, updated_at)
                   VALUES ('run-other', 'repo-2', 'issue-other',
                           'activation-other', 'sandbox-1', 'team-1', 'main',
                           ?, 'queued', ?, ?),
                          ('run-sibling', 'repo-1', 'issue-sibling',
                           'activation-sibling', 'sandbox-1', 'team-1', 'main',
                           ?, 'queued', ?, ?)""",
                ("b" * 40, now, now, "c" * 40, now, now),
            )
        migrated.initialize()
        with migrated.transaction() as connection:
            connection.execute(
                """UPDATE runs SET force_requested_at=? WHERE id='run-1'""",
                (now,),
            )
            connection.execute(
                """UPDATE runs SET force_requested_at=? WHERE id='run-other'""",
                (now,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with migrated.transaction() as connection:
                connection.execute(
                    """UPDATE runs
                       SET force_requested_at=?
                       WHERE id='run-sibling'""",
                    (now,),
                )
        with migrated.connect() as connection:
            indexes = {
                str(row["name"])
                for row in connection.execute("PRAGMA index_list(runs)")
            }
        self.assertIn("one_forced_run_per_repository", indexes)
        self.assertNotIn("one_forced_run", indexes)

    def test_run_priority_and_focus_survive_database_reopen(self) -> None:
        self.seed_repository_run()
        with self.db.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET priority=7,
                       force_requested_at='2026-01-01T00:01:00Z'
                   WHERE id='run-1'""")

        reopened = Database(self.path)
        reopened.initialize()

        with reopened.connect() as connection:
            row = connection.execute("""SELECT priority, force_requested_at
                   FROM runs WHERE id='run-1'""").fetchone()
            columns = {
                item["name"] for item in connection.execute("PRAGMA table_info(runs)")
            }
        self.assertEqual(tuple(row), (7, "2026-01-01T00:01:00Z"))
        self.assertTrue({"priority", "force_requested_at"}.issubset(columns))

    def test_schema_v18_adds_restart_safe_retry_state(self) -> None:
        migrated = self.legacy_database("schema-v17.sqlite3", 17)
        self.seed_repository_run(database=migrated)

        migrated.initialize()

        with migrated.connect() as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            retry_state = connection.execute(
                """SELECT retry_attempt_count, retry_operation,
                          retry_next_at, retry_last_error
                   FROM runs WHERE id='run-1'"""
            ).fetchone()
        self.assertEqual(version, 22)
        self.assertEqual(tuple(retry_state), (0, None, None, None))

    def test_schema_v19_adds_durable_workflow_graph_state(self) -> None:
        migrated = self.legacy_database("schema-v18.sqlite3", 18)
        self.seed_repository_run(database=migrated)

        migrated.initialize()

        with migrated.connect() as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            contract_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='team_workflow_templates'"
            ).fetchone()[0]
            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()

        self.assertEqual(version, 22)
        self.assertTrue(
            {
                "team_workflow_templates",
                "team_workflow_nodes",
                "team_workflow_edges",
                "run_workflows",
                "run_workflow_nodes",
                "run_workflow_edges",
                "run_workflow_attempts",
                "run_workflow_resource_claims",
                "workflow_assessments",
            }.issubset(tables)
        )
        self.assertIn("contract_version = 1", contract_sql)
        self.assertEqual(foreign_key_violations, [])

    def test_schema_v20_adds_durable_publication_scope_reviews(self) -> None:
        migrated = self.legacy_database("schema-v19.sqlite3", 19)

        migrated.initialize()

        with migrated.connect() as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(publication_scope_reviews)"
                )
            }
            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()

        self.assertEqual(version, 22)
        self.assertTrue(
            {
                "id",
                "run_id",
                "issue_version_id",
                "base_sha",
                "candidate_sha",
                "diff_sha256",
                "input_sha256",
                "reviewer_model",
                "rubric_version",
                "changed_files_json",
                "in_scope",
                "reason",
                "created_at",
            }.issubset(columns)
        )
        self.assertEqual(foreign_key_violations, [])

    def test_schema_v21_adds_issue_specifications_and_removes_provisioning_input(
        self,
    ) -> None:
        migrated = self.legacy_database("schema-v20.sqlite3", 20)
        self.seed_repository_run(database=migrated)
        with migrated.transaction() as connection:
            connection.execute(
                """UPDATE repositories SET inputs_json=?
                   WHERE id='repo-1'""",
                (
                    '{"allowed_services":["api.example.test:443"],'
                    '"provisioning_commands":[["npm","ci"]],'
                    '"validation_commands":[["npm","test"]]}',
                ),
            )

        migrated.initialize()

        with migrated.connect() as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            specification_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(run_specification_revisions)"
                )
            }
            acceptance_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(acceptance_verifications)"
                )
            }
            inputs = json.loads(
                connection.execute(
                    "SELECT inputs_json FROM repositories WHERE id='repo-1'"
                ).fetchone()["inputs_json"]
            )
            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()

        self.assertEqual(version, 22)
        self.assertTrue(
            {
                "id",
                "run_id",
                "issue_version_id",
                "revision",
                "items_json",
                "content_sha256",
                "reason",
                "author_member_id",
                "created_at",
            }.issubset(specification_columns)
        )
        self.assertIn("specification_revision_id", acceptance_columns)
        self.assertEqual(
            inputs,
            {
                "allowed_services": ["api.example.test:443"],
                "validation_commands": [["npm", "test"]],
            },
        )
        self.assertEqual(foreign_key_violations, [])

    def test_schema_v22_adds_durable_specification_context_reconciliation(
        self,
    ) -> None:
        migrated = self.legacy_database("schema-v21.sqlite3", 21)
        self.seed_repository_run(database=migrated)
        now = "2026-01-01T00:00:00Z"
        with migrated.transaction() as connection:
            connection.execute(
                """INSERT INTO issue_versions
                   (id, issue_id, version, github_updated_at, content_sha256,
                    title, body, discussion_json, observed_at)
                   VALUES ('issue-version-1', 'issue-1', 1, ?, ?,
                           'Issue', 'Body', '[]', ?)""",
                (now, "a" * 64, now),
            )
            connection.execute(
                """UPDATE issues SET current_version_id='issue-version-1'
                   WHERE id='issue-1'"""
            )
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES ('verifier-1', 'team-1', 'verification', 'verifier',
                           'repository behavior reviewer', 'Review behavior',
                           '["read"]', 'test', 'test/verifier', '')"""
            )
            connection.execute(
                """INSERT INTO run_specification_revisions
                   (id, run_id, issue_version_id, revision, items_json,
                    content_sha256, reason, author_member_id, created_at)
                   VALUES ('spec-1', 'run-1', 'issue-version-1', 1, '[]',
                           ?, 'Existing specification', 'member-1', ?)""",
                ("b" * 64, now),
            )
            connection.execute(
                """INSERT INTO run_specification_reviews
                   (id, run_id, specification_revision_id,
                    reviewer_member_id, reviewer_model, rubric_version,
                    verdict, summary, findings_json, blocker,
                    input_sha256, created_at)
                   VALUES ('review-1', 'run-1', 'spec-1', 'verifier-1',
                           'test/verifier', 1, 'approved',
                           'Existing approval', '[]', NULL, ?, ?)""",
                ("c" * 64, now),
            )
            connection.execute(
                """INSERT INTO acceptance_verifications
                   (id, run_id, commit_sha, attempt, verifier_member_id,
                    state, claims_json, screenshot_decision_json,
                    started_at, issue_version_id, specification_revision_id)
                   VALUES ('acceptance-1', 'run-1', ?, 1, 'verifier-1',
                           'passed', '[]', '{}', ?,
                           'issue-version-1', 'spec-1')""",
                ("d" * 40, now),
            )
            connection.execute(
                """INSERT INTO acceptance_evidence
                   (id, verification_id, sequence, action_json, result_json,
                    started_at, completed_at)
                   VALUES ('evidence-1', 'acceptance-1', 1, '{}', '{}', ?, ?)""",
                (now, now),
            )


        migrated.initialize()

        with migrated.connect() as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(run_specification_contexts)"
                )
            }
            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            preserved = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "run_specification_revisions",
                    "run_specification_reviews",
                    "acceptance_verifications",
                    "acceptance_evidence",
                )
            )

        self.assertEqual(version, 22)
        self.assertTrue(
            {
                "id",
                "run_id",
                "issue_version_id",
                "context_sha256",
                "specification_revision_id",
                "reconciled_at",
            }.issubset(columns)
        )
        self.assertEqual(preserved, (1, 1, 1, 1))
        self.assertEqual(foreign_key_violations, [])



if __name__ == "__main__":
    unittest.main()
