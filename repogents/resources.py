from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .database import Database


_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RepositoryResourceStore:
    """Durable controller-owned storage for repository artifacts and secrets.

    Artifact and secret bytes are deliberately kept outside SQLite. Public methods
    return artifact metadata and configured secret status, never secret values.
    """

    def __init__(self, database: Database, data_root: Path) -> None:
        self.database = database
        self.root = Path(data_root).expanduser().resolve() / "repository-resources"
        self.artifact_root = self.root / "artifacts" / "sha256"
        self.secret_root = self.root / "secrets"
        self._secure_directory(self.root)
        self._secure_directory(self.artifact_root)
        self._secure_directory(self.secret_root)

    def upload_artifact(
        self,
        repository_id: str,
        *,
        name: str,
        description: str,
        content: bytes,
        sandbox_path: str | None = None,
    ) -> dict[str, Any]:
        self._require_repository(repository_id)
        if _ARTIFACT_NAME.fullmatch(name) is None:
            raise ValueError("artifact name is invalid")
        if not isinstance(description, str):
            raise ValueError("artifact description must be a string")
        if not isinstance(content, bytes):
            raise ValueError("artifact content must be bytes")
        mount_path = sandbox_path or f"/repository-resources/artifacts/{name}"
        if not mount_path.startswith("/") or ".." in Path(mount_path).parts:
            raise ValueError("artifact sandbox path must be an absolute normalized path")

        digest = hashlib.sha256(content).hexdigest()
        content_hash = f"sha256:{digest}"
        storage = self.artifact_root / digest[:2] / digest
        self._write_immutable(storage, content)
        now = _utc_now()
        with self.database.transaction() as connection:
            artifact = connection.execute(
                """SELECT id, description FROM repository_artifacts
                   WHERE repository_id=? AND name=?""",
                (repository_id, name),
            ).fetchone()
            if artifact is None:
                artifact_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO repository_artifacts
                       (id, repository_id, name, description, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (artifact_id, repository_id, name, description, now),
                )
                revision = 1
            else:
                artifact_id = str(artifact["id"])
                connection.execute(
                    "UPDATE repository_artifacts SET description=? WHERE id=?",
                    (description, artifact_id),
                )
                existing = connection.execute(
                    """SELECT revision FROM artifact_revisions
                       WHERE artifact_id=? AND content_hash=?""",
                    (artifact_id, content_hash),
                ).fetchone()
                if existing is not None:
                    return self._artifact_projection(
                        repository_id, name, int(existing["revision"]), mount_path
                    )
                row = connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM artifact_revisions WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                revision = int(row["revision"])
            revision_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO artifact_revisions
                   (id, artifact_id, revision, content_hash, size, storage_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision_id,
                    artifact_id,
                    revision,
                    content_hash,
                    len(content),
                    str(storage),
                    now,
                ),
            )
        return self._artifact_projection(repository_id, name, revision, mount_path)

    def artifact_binding(
        self,
        repository_id: str,
        name: str,
        revision: int | None = None,
        *,
        sandbox_path: str | None = None,
    ) -> dict[str, Any]:
        mount_path = sandbox_path or f"/repository-resources/artifacts/{name}"
        with self.database.connect() as connection:
            parameters: list[object] = [repository_id, name]
            revision_filter = ""
            if revision is not None:
                revision_filter = "AND artifact_revisions.revision=?"
                parameters.append(revision)
            row = connection.execute(
                f"""SELECT repository_artifacts.description,
                           artifact_revisions.revision, artifact_revisions.content_hash,
                           artifact_revisions.size, artifact_revisions.storage_path,
                           artifact_revisions.created_at
                    FROM repository_artifacts
                    JOIN artifact_revisions
                      ON artifact_revisions.artifact_id=repository_artifacts.id
                    WHERE repository_artifacts.repository_id=?
                      AND repository_artifacts.name=? {revision_filter}
                    ORDER BY artifact_revisions.revision DESC LIMIT 1""",
                tuple(parameters),
            ).fetchone()
        if row is None:
            raise KeyError(f"artifact revision not found: {name}")
        storage = Path(str(row["storage_path"]))
        if not storage.is_file():
            raise RuntimeError(f"artifact bytes are inaccessible: {name}")
        return {
            "name": name,
            "description": str(row["description"]),
            "revision": int(row["revision"]),
            "content_hash": str(row["content_hash"]),
            "size": int(row["size"]),
            "created_at": str(row["created_at"]),
            "storage_path": str(storage),
            "sandbox_path": mount_path,
        }

    def list_artifacts(self, repository_id: str) -> list[dict[str, Any]]:
        self._require_repository(repository_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT repository_artifacts.name, repository_artifacts.description,
                          artifact_revisions.revision, artifact_revisions.content_hash,
                          artifact_revisions.size, artifact_revisions.created_at
                   FROM repository_artifacts
                   JOIN artifact_revisions
                     ON artifact_revisions.artifact_id=repository_artifacts.id
                   WHERE repository_artifacts.repository_id=?
                   ORDER BY repository_artifacts.name COLLATE NOCASE,
                            artifact_revisions.revision DESC""",
                (repository_id,),
            ).fetchall()
        return [
            {
                "name": str(row["name"]),
                "description": str(row["description"]),
                "revision": int(row["revision"]),
                "content_hash": str(row["content_hash"]),
                "size": int(row["size"]),
                "created_at": str(row["created_at"]),
                "sandbox_path": f"/repository-resources/artifacts/{row['name']}",
            }
            for row in rows
        ]

    def remove_artifact_revision(
        self, repository_id: str, name: str, revision: int
    ) -> None:
        storage: Path
        content_hash: str
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT artifact_revisions.id, artifact_revisions.storage_path,
                          artifact_revisions.content_hash, repository_artifacts.id AS artifact_id
                   FROM repository_artifacts
                   JOIN artifact_revisions
                     ON artifact_revisions.artifact_id=repository_artifacts.id
                   WHERE repository_artifacts.repository_id=?
                     AND repository_artifacts.name=?
                     AND artifact_revisions.revision=?""",
                (repository_id, name, revision),
            ).fetchone()
            if row is None:
                raise KeyError(f"artifact revision not found: {name}@{revision}")
            referenced = connection.execute(
                "SELECT 1 FROM sandbox_artifact_revisions WHERE artifact_revision_id=? LIMIT 1",
                (row["id"],),
            ).fetchone()
            if referenced is not None:
                raise RuntimeError(
                    "artifact revision is retained by a sandbox version or run"
                )
            storage = Path(str(row["storage_path"]))
            content_hash = str(row["content_hash"])
            artifact_id = str(row["artifact_id"])
            connection.execute(
                "DELETE FROM artifact_revisions WHERE id=?", (row["id"],)
            )
            remaining = connection.execute(
                "SELECT 1 FROM artifact_revisions WHERE artifact_id=? LIMIT 1",
                (artifact_id,),
            ).fetchone()
            if remaining is None:
                connection.execute(
                    "DELETE FROM repository_artifacts WHERE id=?", (artifact_id,)
                )
        with self.database.connect() as connection:
            shared = connection.execute(
                "SELECT 1 FROM artifact_revisions WHERE content_hash=? LIMIT 1",
                (content_hash,),
            ).fetchone()
        if shared is None:
            storage.unlink(missing_ok=True)

    def update_secret(
        self,
        repository_id: str,
        *,
        name: str,
        action: str,
        value: str = "",
    ) -> dict[str, Any]:
        self._require_repository(repository_id)
        if _ENVIRONMENT_NAME.fullmatch(name) is None:
            raise ValueError("secret name is not a valid environment name")
        if action not in {"preserve", "replace", "remove"}:
            raise ValueError("secret action must be preserve, replace, or remove")
        reference = f"secret://repository/{repository_id}/{name.lower()}"
        secret_directory = self.secret_root / repository_id
        path = secret_directory / name
        now = _utc_now()
        with self.database.connect() as connection:
            current = connection.execute(
                "SELECT storage_path FROM repository_secrets WHERE repository_id=? AND name=?",
                (repository_id, name),
            ).fetchone()
        if action == "preserve" or (action == "replace" and value == ""):
            if current is None:
                return {"name": name, "reference": reference, "configured": False}
            return {
                "name": name,
                "reference": reference,
                "configured": current["storage_path"] is not None,
            }
        if action == "replace":
            self._secure_directory(secret_directory)
            self._atomic_secret_write(path, value)
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO repository_secrets
                       (id, repository_id, name, reference, storage_path, configured_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(repository_id, name) DO UPDATE SET
                           reference=excluded.reference, storage_path=excluded.storage_path,
                           configured_at=excluded.configured_at, updated_at=excluded.updated_at""",
                    (
                        str(uuid.uuid4()), repository_id, name, reference, str(path), now, now
                    ),
                )
            return {"name": name, "reference": reference, "configured": True}

        path.unlink(missing_ok=True)
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO repository_secrets
                   (id, repository_id, name, reference, storage_path, configured_at, updated_at)
                   VALUES (?, ?, ?, ?, NULL, NULL, ?)
                   ON CONFLICT(repository_id, name) DO UPDATE SET
                       storage_path=NULL, configured_at=NULL, updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), repository_id, name, reference, now),
            )
        return {"name": name, "reference": reference, "configured": False}

    def list_secrets(self, repository_id: str) -> list[dict[str, Any]]:
        self._require_repository(repository_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT name, reference, storage_path, configured_at, updated_at
                   FROM repository_secrets WHERE repository_id=?
                   ORDER BY name COLLATE NOCASE""",
                (repository_id,),
            ).fetchall()
        return [
            {
                "name": str(row["name"]),
                "reference": str(row["reference"]),
                "configured": row["storage_path"] is not None,
                "configured_at": row["configured_at"],
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def resolve_secret(self, reference: str) -> str | None:
        """Controller-only resolution used immediately before authorized execution."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT storage_path FROM repository_secrets WHERE reference=?",
                (reference,),
            ).fetchone()
        if row is None or row["storage_path"] is None:
            return None
        path = Path(str(row["storage_path"]))
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError("configured repository secret is inaccessible") from error

    def _artifact_projection(
        self, repository_id: str, name: str, revision: int, sandbox_path: str
    ) -> dict[str, Any]:
        return self.artifact_binding(
            repository_id, name, revision, sandbox_path=sandbox_path
        )

    def _require_repository(self, repository_id: str) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM repositories WHERE id=? AND removed_at IS NULL",
                (repository_id,),
            ).fetchone()
        if row is None:
            raise KeyError("repository not found")

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    def _write_immutable(self, path: Path, content: bytes) -> None:
        self._secure_directory(path.parent)
        if path.exists():
            if path.read_bytes() != content:
                raise RuntimeError("artifact hash collision detected")
            return
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        path.chmod(0o400)

    def _atomic_secret_write(self, path: Path, value: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".secret-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(value)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
