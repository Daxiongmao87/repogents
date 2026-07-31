from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from repogents.database import Database, SCHEMA_VERSION


class AutonomousRepositoryPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "repogents.sqlite3"
        self.database = Database(self.database_path)
        self.database.initialize()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO repositories (
                    id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "repository-1",
                    "github-repository-1",
                    "owner",
                    "repository",
                    "https://github.com/owner/repository",
                    "main",
                    "ready",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )

    def autonomous_mode(self) -> int:
        with Database(self.database_path).connect() as connection:
            row = connection.execute(
                "SELECT autonomous_mode FROM repositories WHERE id = ?",
                ("repository-1",),
            ).fetchone()
        self.assertIsNotNone(row)
        return int(row["autonomous_mode"])

    def test_autonomous_mode_defaults_off_and_round_trips_across_restart(self) -> None:
        self.assertEqual(self.autonomous_mode(), 0)

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET autonomous_mode = 1 WHERE id = ?",
                ("repository-1",),
            )

        restarted = Database(self.database_path)
        restarted.initialize()
        self.assertEqual(self.autonomous_mode(), 1)

        with restarted.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET autonomous_mode = 0 WHERE id = ?",
                ("repository-1",),
            )

        Database(self.database_path).initialize()
        self.assertEqual(self.autonomous_mode(), 0)

    def test_autonomous_mode_rejects_values_outside_boolean_domain(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE repositories SET autonomous_mode = 2 WHERE id = ?",
                    ("repository-1",),
                )
        self.assertEqual(self.autonomous_mode(), 0)

    def test_concurrent_v23_migration_converges_on_current_schema(self) -> None:
        with self.database.connect() as connection:
            connection.execute("DROP TABLE autonomous_issue_observations")
            connection.execute("ALTER TABLE repositories DROP COLUMN autonomous_mode")
            connection.execute(
                "DELETE FROM schema_version WHERE version IN (24, 25)"
            )
            connection.commit()

        barrier = threading.Barrier(2)
        failures: list[BaseException] = []

        def initialize() -> None:
            try:
                barrier.wait(timeout=5)
                Database(self.database_path).initialize()
            except BaseException as error:  # captured for assertion in the test thread
                failures.append(error)

        workers = [threading.Thread(target=initialize) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        with self.database.connect() as connection:
            versions = [
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_version "
                    "WHERE version IN (23, 24, 25) ORDER BY version"
                )
            ]
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(repositories)")
            }
            observation_table = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'autonomous_issue_observations'"
            ).fetchone()
            observation_indexes = connection.execute(
                "PRAGMA index_list(autonomous_issue_observations)"
            ).fetchall()
            repository = connection.execute(
                "SELECT autonomous_mode FROM repositories WHERE id = ?",
                ("repository-1",),
            ).fetchone()

        self.assertEqual(SCHEMA_VERSION, 25)
        self.assertEqual(versions, [23, 24, 25])
        self.assertIn("autonomous_mode", columns)
        self.assertIsNotNone(observation_table)
        self.assertTrue(any(row["unique"] for row in observation_indexes))
        self.assertIsNotNone(repository)
        self.assertEqual(repository["autonomous_mode"], 0)


if __name__ == "__main__":
    unittest.main()
