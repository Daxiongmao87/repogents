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
        self.assertEqual(
            value,
            restarted.resolve_secret(
                str(projection["reference"]), repository_id=self.repository_id
            ),
        )

        database_bytes = (self.root / "repogents.sqlite3").read_bytes()
        self.assertNotIn(value.encode("utf-8"), database_bytes)
        with self.database.connect() as connection:
            revision_row = connection.execute(
                """SELECT repository_secret_revisions.storage_path
                   FROM repository_secret_revisions
                   JOIN repository_secrets
                     ON repository_secrets.id=repository_secret_revisions.repository_secret_id
                  WHERE repository_secrets.repository_id=?
                    AND repository_secrets.name=?
                  ORDER BY repository_secret_revisions.revision DESC
                  LIMIT 1""",
                (self.repository_id, "PRODUCT_KEY"),
            ).fetchone()
        self.assertIsNotNone(revision_row)
        secret_path = Path(str(revision_row["storage_path"]))
        self.assertEqual(0o600, os.stat(secret_path).st_mode & 0o777)
        self.assertEqual(0o700, os.stat(secret_path.parent).st_mode & 0o777)

    def test_removing_unpinned_secret_deletes_revision_and_plaintext_file(self) -> None:
        value = "obsolete-product-key-that-must-be-deleted"
        store = RepositoryResourceStore(self.database, self.root)
        store.update_secret(
            self.repository_id, name="PRODUCT_KEY", action="replace", value=value
        )
        with self.database.connect() as connection:
            revision_row = connection.execute(
                """SELECT repository_secret_revisions.id,
                          repository_secret_revisions.storage_path
                   FROM repository_secret_revisions
                   JOIN repository_secrets
                     ON repository_secrets.id=repository_secret_revisions.repository_secret_id
                  WHERE repository_secrets.repository_id=?
                    AND repository_secrets.name=?""",
                (self.repository_id, "PRODUCT_KEY"),
            ).fetchone()
        self.assertIsNotNone(revision_row)
        revision_id = str(revision_row["id"])
        secret_path = Path(str(revision_row["storage_path"]))
        self.assertTrue(secret_path.exists())

        projection = store.update_secret(
            self.repository_id, name="PRODUCT_KEY", action="remove"
        )

        self.assertFalse(projection["configured"])
        self.assertFalse(secret_path.exists())
        self.assertIsNone(
            store.resolve_secret(
                f"secret://repository-revision/{revision_id}",
                repository_id=self.repository_id,
            )
        )
        with self.database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM repository_secret_revisions WHERE id=?",
                    (revision_id,),
                ).fetchone()
            )

    def test_concurrent_same_content_upload_survives_artifact_removal(self) -> None:
        import threading
        from unittest import mock

        content = b"shared hash-addressed artifact bytes"
        store = RepositoryResourceStore(self.database, self.root)
        first = store.upload_artifact(
            self.repository_id,
            name="obsolete-fixture",
            description="obsolete copy",
            content=content,
        )
        storage_path = Path(
            str(
                store.artifact_binding(
                    self.repository_id,
                    "obsolete-fixture",
                    int(first["revision"]),
                )["storage_path"]
            )
        )
        unlink_reached = threading.Event()
        upload_started = threading.Event()
        removal_errors: list[BaseException] = []
        upload_errors: list[BaseException] = []
        uploaded: list[dict[str, object]] = []
        original_unlink = Path.unlink

        def coordinated_unlink(path: Path, *args: object, **kwargs: object) -> None:
            if path == storage_path:
                unlink_reached.set()
                self.assertTrue(upload_started.wait(2))
            original_unlink(path, *args, **kwargs)

        def remove() -> None:
            try:
                store.remove_artifact_revision(
                    self.repository_id, "obsolete-fixture", int(first["revision"])
                )
            except BaseException as error:
                removal_errors.append(error)

        def upload() -> None:
            upload_started.set()
            try:
                uploaded.append(
                    RepositoryResourceStore(self.database, self.root).upload_artifact(
                        self.repository_id,
                        name="retained-fixture",
                        description="concurrently retained copy",
                        content=content,
                    )
                )
            except BaseException as error:
                upload_errors.append(error)

        with mock.patch.object(Path, "unlink", coordinated_unlink):
            removal_thread = threading.Thread(target=remove)
            removal_thread.start()
            self.assertTrue(unlink_reached.wait(2))
            upload_thread = threading.Thread(target=upload)
            upload_thread.start()
            removal_thread.join(2)
            upload_thread.join(2)

        self.assertFalse(removal_thread.is_alive())
        self.assertFalse(upload_thread.is_alive())
        self.assertEqual([], removal_errors)
        self.assertEqual([], upload_errors)
        self.assertEqual(1, len(uploaded))
        retained = store.artifact_binding(
            self.repository_id,
            "retained-fixture",
            int(uploaded[0]["revision"]),
        )
        self.assertEqual(storage_path, Path(str(retained["storage_path"])))
        self.assertEqual(content, storage_path.read_bytes())

    def test_case_colliding_secret_name_is_rejected_without_orphan_revision(self) -> None:
        store = RepositoryResourceStore(self.database, self.root)
        store.update_secret(
            self.repository_id, name="TOKEN", action="replace", value="original"
        )
        secret_directory = store.secret_root / self.repository_id
        files_before = sorted(secret_directory.iterdir())
        with self.database.connect() as connection:
            secrets_before = connection.execute(
                "SELECT COUNT(*) FROM repository_secrets WHERE repository_id=?",
                (self.repository_id,),
            ).fetchone()[0]
            revisions_before = connection.execute(
                """SELECT COUNT(*) FROM repository_secret_revisions AS revision
                   JOIN repository_secrets AS secret
                     ON secret.id=revision.repository_secret_id
                  WHERE secret.repository_id=?""",
                (self.repository_id,),
            ).fetchone()[0]

        with self.assertRaisesRegex(ValueError, "conflicts case-insensitively"):
            store.update_secret(
                self.repository_id, name="token", action="replace", value="replacement"
            )

        self.assertEqual(files_before, sorted(secret_directory.iterdir()))
        with self.database.connect() as connection:
            self.assertEqual(
                secrets_before,
                connection.execute(
                    "SELECT COUNT(*) FROM repository_secrets WHERE repository_id=?",
                    (self.repository_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                revisions_before,
                connection.execute(
                    """SELECT COUNT(*) FROM repository_secret_revisions AS revision
                       JOIN repository_secrets AS secret
                         ON secret.id=revision.repository_secret_id
                      WHERE secret.repository_id=?""",
                    (self.repository_id,),
                ).fetchone()[0],
            )
        self.assertEqual("original", files_before[0].read_text(encoding="utf-8"))

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

        with self.assertRaisesRegex(
            RuntimeError, "retained by a sandbox version, run, or in-progress environment build"
        ):
            store.remove_artifact_revision(
                self.repository_id, "fixture-sdk", int(artifact["revision"])
            )


if __name__ == "__main__":
    unittest.main()
