from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from repogents.database import Database
from repogents.resources import RepositoryResourceStore


class RepositoryResourceRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "repogents.sqlite3")
        self.database.initialize()
        self.repository_id = "github-resource-revision"
        self._insert_repository()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _insert_repository(self) -> None:
        now = "2025-01-01T00:00:00+00:00"
        values = {
            "id": self.repository_id,
            "provider": "github",
            "provider_repository_id": "resource-revision",
            "owner": "example",
            "name": "resources",
            "full_name": "example/resources",
            "html_url": "https://github.com/example/resources",
            "clone_url": "https://github.com/example/resources.git",
            "default_branch": "main",
            "onboarding_state": "ready",
            "enabled": 1,
            "ready_issue_generation": 0,
            "created_at": now,
            "updated_at": now,
        }
        with self.database.transaction() as connection:
            columns = connection.execute("PRAGMA table_info(repositories)").fetchall()
            selected: list[str] = []
            parameters: list[object] = []
            for column in columns:
                name = str(column["name"])
                if name in values:
                    selected.append(name)
                    parameters.append(values[name])
                elif int(column["notnull"]) and column["dflt_value"] is None:
                    selected.append(name)
                    declared = str(column["type"] or "").upper()
                    parameters.append(0 if "INT" in declared else "")
            placeholders = ", ".join("?" for _ in selected)
            connection.execute(
                f"INSERT INTO repositories ({', '.join(selected)}) VALUES ({placeholders})",
                tuple(parameters),
            )

    def test_saved_secret_survives_store_restart_without_database_value(self) -> None:
        value = "fixture-product-key-that-must-not-leak"
        store = RepositoryResourceStore(self.database, self.root)
        projection = store.update_secret(
            self.repository_id, name="PRODUCT_KEY", action="replace", value=value
        )

        self.assertTrue(projection["configured"])
        self.assertNotIn(value, repr(projection))
        restarted = RepositoryResourceStore(
            Database(self.root / "repogents.sqlite3"), self.root
        )
        self.assertEqual(value, restarted.resolve_secret(str(projection["reference"])))

        database_bytes = (self.root / "repogents.sqlite3").read_bytes()
        self.assertNotIn(value.encode("utf-8"), database_bytes)
        secret_path = (
            self.root
            / "repository-resources"
            / "secrets"
            / self.repository_id
            / "PRODUCT_KEY"
        )
        self.assertEqual(0o600, os.stat(secret_path).st_mode & 0o777)
        self.assertEqual(0o700, os.stat(secret_path.parent).st_mode & 0o777)

    def test_pinned_artifact_revision_cannot_be_removed(self) -> None:
        store = RepositoryResourceStore(self.database, self.root)
        artifact = store.upload_artifact(
            self.repository_id,
            name="fixture-sdk",
            description="Licensed fixture SDK",
            content=b"immutable fixture bytes",
        )
        with self.database.transaction() as connection:
            revision = connection.execute(
                """SELECT artifact_revisions.id
                   FROM artifact_revisions
                   JOIN repository_artifacts
                     ON repository_artifacts.id=artifact_revisions.artifact_id
                   WHERE repository_artifacts.repository_id=?
                     AND repository_artifacts.name=?
                     AND artifact_revisions.revision=?""",
                (self.repository_id, "fixture-sdk", artifact["revision"]),
            ).fetchone()
            self.assertIsNotNone(revision)
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "sandbox-resource-revision",
                    self.repository_id,
                    1,
                    str(self.root / "sandbox"),
                    "{}",
                    "{}",
                    "2025-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                """INSERT INTO sandbox_artifact_revisions
                   (sandbox_version_id, artifact_revision_id, sandbox_path)
                   VALUES (?, ?, ?)""",
                (
                    "sandbox-resource-revision",
                    revision["id"],
                    artifact["sandbox_path"],
                ),
            )

        with self.assertRaisesRegex(RuntimeError, "retained by a sandbox version or run"):
            store.remove_artifact_revision(
                self.repository_id, "fixture-sdk", int(artifact["revision"])
            )


if __name__ == "__main__":
    unittest.main()
