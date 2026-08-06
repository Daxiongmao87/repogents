from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


RUN_STATES = frozenset(
    {
        "QUEUED",
        "SPECIFYING",
        "EXECUTING",
        "WAITING_FOR_WORK_COMPLETION",
        "VALIDATING",
        "CREATING_PR",
        "PR_LISTENING",
        "PENDING_MERGE",
        "COMPLETED",
        "CLOSED",
    }
)
TERMINAL_RUN_STATES = frozenset({"COMPLETED", "CLOSED"})
WORK_STATES = frozenset(
    {"UNASSIGNED", "QUEUED", "RUNNING", "COMPLETED", "HANDED_OFF", "FAILED"}
)
TERMINAL_WORK_STATES = frozenset({"COMPLETED", "HANDED_OFF", "FAILED"})
FEEDBACK_DISPOSITIONS = frozenset({"IN_SCOPE", "OUT_OF_SCOPE", "INVALID"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY,
    github_repository TEXT NOT NULL UNIQUE,
    target_branch TEXT NOT NULL,
    similarity_threshold REAL NOT NULL,
    autonomous_issue_intake INTEGER NOT NULL DEFAULT 0
        CHECK (autonomous_issue_intake IN (0, 1)),
    tracked INTEGER NOT NULL DEFAULT 1 CHECK (tracked IN (0, 1))
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    classification TEXT NOT NULL,
    vector TEXT,
    role_prompt TEXT NOT NULL,
    persistence TEXT NOT NULL CHECK (persistence IN ('PERMANENT', 'EPHEMERAL', 'PERSISTENT')),
    success_count INTEGER NOT NULL DEFAULT 0,
    unused_completed_runs INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_node_per_classification
ON nodes(repository_id, classification)
WHERE active = 1;

CREATE TABLE IF NOT EXISTS classification_vectors (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    classification TEXT NOT NULL,
    vector TEXT NOT NULL,
    PRIMARY KEY(repository_id, classification)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    issue_number INTEGER NOT NULL,
    issue_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'QUEUED',
            'SPECIFYING',
            'EXECUTING',
            'WAITING_FOR_WORK_COMPLETION',
            'VALIDATING',
            'CREATING_PR',
            'PR_LISTENING',
            'PENDING_MERGE',
            'COMPLETED',
            'CLOSED'
        )
    ),
    branch TEXT,
    pull_request TEXT,
    pr_listening_since REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_issue
ON runs(repository_id, issue_number)
WHERE state NOT IN ('COMPLETED', 'CLOSED');

CREATE TABLE IF NOT EXISTS execution_passes (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    trigger_type TEXT NOT NULL,
    trigger_json TEXT NOT NULL,
    UNIQUE(id, run_id)
);

CREATE TABLE IF NOT EXISTS issue_specifications (
    run_id INTEGER NOT NULL,
    pass_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY(run_id, pass_id),
    FOREIGN KEY(pass_id, run_id) REFERENCES execution_passes(id, run_id)
);

CREATE TABLE IF NOT EXISTS work_specification_results (
    run_id INTEGER NOT NULL,
    pass_id INTEGER NOT NULL,
    work_area_key TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY(pass_id, work_area_key),
    FOREIGN KEY(pass_id, run_id) REFERENCES execution_passes(id, run_id)
);

CREATE TABLE IF NOT EXISTS specifications (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    pass_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    dependency_evidence TEXT NOT NULL DEFAULT '[]',
    requirement_keys TEXT NOT NULL DEFAULT '[]',
    acceptance_traceability TEXT NOT NULL DEFAULT '[]',
    executable INTEGER NOT NULL CHECK (executable IN (0, 1)),
    UNIQUE(pass_id, key),
    UNIQUE(id, pass_id, run_id),
    FOREIGN KEY(pass_id, run_id) REFERENCES execution_passes(id, run_id)
);

CREATE TABLE IF NOT EXISTS work_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    pass_id INTEGER NOT NULL,
    specification_id INTEGER NOT NULL,
    parent_work_id INTEGER REFERENCES work_items(id),
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    classification TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    dependency_evidence TEXT NOT NULL DEFAULT '[]',
    requirement_keys TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    evidence_requirements TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL CHECK (
        state IN ('UNASSIGNED', 'QUEUED', 'RUNNING', 'COMPLETED', 'HANDED_OFF', 'FAILED')
    ),
    node_id INTEGER REFERENCES nodes(id),
    result TEXT,
    handoff TEXT,
    UNIQUE(pass_id, key),
    FOREIGN KEY(specification_id, pass_id, run_id)
        REFERENCES specifications(id, pass_id, run_id)
);

CREATE TABLE IF NOT EXISTS validations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    pass_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    UNIQUE(run_id, pass_id),
    FOREIGN KEY(pass_id, run_id) REFERENCES execution_passes(id, run_id)
);

CREATE TABLE IF NOT EXISTS work_validations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    pass_id INTEGER NOT NULL,
    work_id INTEGER NOT NULL REFERENCES work_items(id),
    attempt INTEGER NOT NULL,
    result TEXT NOT NULL,
    UNIQUE(work_id, attempt),
    FOREIGN KEY(pass_id, run_id) REFERENCES execution_passes(id, run_id)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    external_id TEXT NOT NULL,
    package TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RESOLVED', 'ACKNOWLEDGED')),
    addressed_sha TEXT,
    response_url TEXT,
    disposition TEXT CHECK (
        disposition IN ('IN_SCOPE', 'OUT_OF_SCOPE', 'INVALID')
    ),
    disposition_result TEXT,
    follow_up_issue TEXT,
    UNIQUE(run_id, external_id)
);

CREATE TABLE IF NOT EXISTS feedback_scope_results (
    run_id INTEGER NOT NULL,
    pass_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY(run_id, pass_id),
    FOREIGN KEY(pass_id, run_id) REFERENCES execution_passes(id, run_id)
);

CREATE TABLE IF NOT EXISTS issue_order_plans (
    repository_id INTEGER PRIMARY KEY REFERENCES repositories(id),
    issue_snapshot TEXT NOT NULL,
    result TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_run_usage (
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    run_id INTEGER NOT NULL REFERENCES runs(id),
    PRIMARY KEY(node_id, run_id)
);

CREATE TABLE IF NOT EXISTS adapted_runs (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id)
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            self._migrate_work_validation_attempts(connection)
            self._migrate_permanent_workflow_nodes(connection)
            repository_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(repositories)")
            }
            if "autonomous_issue_intake" not in repository_columns:
                connection.execute(
                    """
                    ALTER TABLE repositories
                    ADD COLUMN autonomous_issue_intake INTEGER NOT NULL DEFAULT 0
                        CHECK (autonomous_issue_intake IN (0, 1))
                    """
                )
            self._migrate_runs_pending_merge(connection)
            run_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(runs)")
            }
            if "pr_listening_since" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN pr_listening_since REAL"
                )
            feedback_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(feedback)")
            }
            if "status" not in feedback_columns:
                connection.execute(
                    """
                    ALTER TABLE feedback
                    ADD COLUMN status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'RESOLVED', 'ACKNOWLEDGED'))
                    """
                )
            if "addressed_sha" not in feedback_columns:
                connection.execute(
                    "ALTER TABLE feedback ADD COLUMN addressed_sha TEXT"
                )
            if "response_url" not in feedback_columns:
                connection.execute(
                    "ALTER TABLE feedback ADD COLUMN response_url TEXT"
                )
            if "disposition" not in feedback_columns:
                connection.execute(
                    """
                    ALTER TABLE feedback
                    ADD COLUMN disposition TEXT CHECK (
                        disposition IN ('IN_SCOPE', 'OUT_OF_SCOPE', 'INVALID')
                    )
                    """
                )
            if "disposition_result" not in feedback_columns:
                connection.execute(
                    "ALTER TABLE feedback ADD COLUMN disposition_result TEXT"
                )
            if "follow_up_issue" not in feedback_columns:
                connection.execute(
                    "ALTER TABLE feedback ADD COLUMN follow_up_issue TEXT"
                )
            specification_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(specifications)")
            }
            if "dependency_evidence" not in specification_columns:
                connection.execute(
                    """
                    ALTER TABLE specifications
                    ADD COLUMN dependency_evidence TEXT NOT NULL DEFAULT '[]'
                    """
                )
            for column in ("requirement_keys", "acceptance_traceability"):
                if column not in specification_columns:
                    connection.execute(
                        f"ALTER TABLE specifications ADD COLUMN {column} "
                        "TEXT NOT NULL DEFAULT '[]'"
                    )
            work_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(work_items)")
            }
            if "dependency_evidence" not in work_columns:
                connection.execute(
                    """
                    ALTER TABLE work_items
                    ADD COLUMN dependency_evidence TEXT NOT NULL DEFAULT '[]'
                    """
                )
            for column in (
                "requirement_keys",
                "acceptance_criteria",
                "evidence_requirements",
            ):
                if column not in work_columns:
                    connection.execute(
                        f"ALTER TABLE work_items ADD COLUMN {column} "
                        "TEXT NOT NULL DEFAULT '[]'"
                    )
        finally:
            connection.close()

    @staticmethod
    def _migrate_work_validation_attempts(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(work_validations)")
        }
        if "attempt" in columns:
            return
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE work_validations RENAME TO old_work_validations")
            connection.execute(
                """
                CREATE TABLE work_validations (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    pass_id INTEGER NOT NULL,
                    work_id INTEGER NOT NULL REFERENCES work_items(id),
                    attempt INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    UNIQUE(work_id, attempt),
                    FOREIGN KEY(pass_id, run_id)
                        REFERENCES execution_passes(id, run_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO work_validations(
                    id, run_id, pass_id, work_id, attempt, result
                )
                SELECT id, run_id, pass_id, work_id, 1, result
                FROM old_work_validations
                """
            )
            connection.execute("DROP TABLE old_work_validations")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _migrate_permanent_workflow_nodes(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE nodes SET classification = 'Issue Specifier' "
            "WHERE classification = 'Specify' AND persistence = 'PERMANENT'"
        )
        connection.execute(
            "UPDATE nodes SET classification = 'Issue Validator' "
            "WHERE classification = 'Validate' AND persistence = 'PERMANENT'"
        )
        repository_ids = [
            row["id"] for row in connection.execute("SELECT id FROM repositories")
        ]
        for repository_id in repository_ids:
            existing = {
                row["classification"]
                for row in connection.execute(
                    "SELECT classification FROM nodes "
                    "WHERE repository_id = ? AND persistence = 'PERMANENT'",
                    (repository_id,),
                )
            }
            for classification in (
                "Issue Specifier",
                "Work Specifier",
                "Work Validator",
                "Issue Validator",
            ):
                if classification not in existing:
                    connection.execute(
                        "INSERT INTO nodes(repository_id, classification, vector, "
                        "role_prompt, persistence) VALUES (?, ?, NULL, '', 'PERMANENT')",
                        (repository_id, classification),
                    )

    @staticmethod
    def _migrate_runs_pending_merge(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()
        if row is None or "PENDING_MERGE" in (row["sql"] or ""):
            return

        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE runs_with_pending_merge (
                    id INTEGER PRIMARY KEY,
                    repository_id INTEGER NOT NULL REFERENCES repositories(id),
                    issue_number INTEGER NOT NULL,
                    issue_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'QUEUED',
                            'SPECIFYING',
                            'EXECUTING',
                            'WAITING_FOR_WORK_COMPLETION',
                            'VALIDATING',
                            'CREATING_PR',
                            'PR_LISTENING',
                            'PENDING_MERGE',
                            'COMPLETED',
                            'CLOSED'
                        )
                    ),
                    branch TEXT,
                    pull_request TEXT,
                    pr_listening_since REAL
                )
                """
            )
            columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(runs)")
            }
            listening_value = (
                "pr_listening_since" if "pr_listening_since" in columns else "NULL"
            )
            connection.execute(
                f"""
                INSERT INTO runs_with_pending_merge(
                    id, repository_id, issue_number, issue_json, state,
                    branch, pull_request, pr_listening_since
                )
                SELECT id, repository_id, issue_number, issue_json, state,
                       branch, pull_request, {listening_value}
                FROM runs
                """
            )
            connection.execute("DROP TABLE runs")
            connection.execute("ALTER TABLE runs_with_pending_merge RENAME TO runs")
            connection.execute(
                """
                CREATE UNIQUE INDEX one_active_run_per_issue
                ON runs(repository_id, issue_number)
                WHERE state NOT IN ('COMPLETED', 'CLOSED')
                """
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _dump(value: Any) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("payload must be JSON-safe") from error

    @staticmethod
    def _decode(
        row: sqlite3.Row | None,
        *,
        json_fields: tuple[str, ...] = (),
        bool_fields: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        decoded = dict(row)
        for field in json_fields:
            if field in decoded and decoded[field] is not None:
                decoded[field] = json.loads(decoded[field])
        for field in bool_fields:
            if field in decoded:
                decoded[field] = bool(decoded[field])
        return decoded

    @classmethod
    def _repository_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return cls._decode(
            row,
            bool_fields=("autonomous_issue_intake", "tracked"),
        )

    @classmethod
    def _node_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return cls._decode(row, json_fields=("vector",), bool_fields=("active",))

    @classmethod
    def _run_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return cls._decode(row, json_fields=("issue_json", "pull_request"))

    @classmethod
    def _pass_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return cls._decode(row, json_fields=("trigger_json",))

    @classmethod
    def _specification_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return cls._decode(
            row,
            json_fields=(
                "acceptance_criteria",
                "dependencies",
                "dependency_evidence",
                "requirement_keys",
                "acceptance_traceability",
            ),
            bool_fields=("executable",),
        )

    @classmethod
    def _work_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return cls._decode(
            row,
            json_fields=(
                "dependencies",
                "dependency_evidence",
                "requirement_keys",
                "acceptance_criteria",
                "evidence_requirements",
                "result",
                "handoff",
            ),
        )

    @classmethod
    def _validation_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return cls._decode(row, json_fields=("result",))

    @classmethod
    def _feedback_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return cls._decode(
            row,
            json_fields=("package", "disposition_result", "follow_up_issue"),
        )

    @staticmethod
    def _fetch_one(connection: sqlite3.Connection, query: str, parameters: tuple = ()) -> sqlite3.Row | None:
        return connection.execute(query, parameters).fetchone()

    @staticmethod
    def _nonempty_string(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string")
        return value

    @classmethod
    def _classification(cls, value: Any) -> str:
        label = cls._nonempty_string(value, "classification").strip()
        levels = label.split("/")
        if len(levels) not in (1, 2) or any(not level.strip() for level in levels):
            raise ValueError("classification must contain one or two nonempty levels")
        return label

    @classmethod
    def _string_list(
        cls, value: Any, name: str, *, nonempty: bool = False
    ) -> list[str]:
        if not isinstance(value, list) or (nonempty and not value):
            qualifier = "nonempty " if nonempty else ""
            raise ValueError(f"{name} must be a {qualifier}list")
        for entry in value:
            cls._nonempty_string(entry, name)
        return list(value)

    @staticmethod
    def _require_acyclic(graph: dict[str, list[str]], name: str) -> None:
        remaining = {
            key: len(dependencies) for key, dependencies in graph.items()
        }
        dependents = {key: [] for key in graph}
        for key, dependencies in graph.items():
            for dependency in dependencies:
                dependents[dependency].append(key)

        ready = [key for key, count in remaining.items() if count == 0]
        visited = 0
        while ready:
            key = ready.pop()
            visited += 1
            for dependent in dependents[key]:
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    ready.append(dependent)

        if visited != len(graph):
            raise ValueError(f"{name} must be acyclic")

    @staticmethod
    def _require_fields(value: Any, fields: tuple[str, ...], name: str) -> dict:
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
        missing = [field for field in fields if field not in value]
        if missing:
            raise ValueError(f"{name} is missing {', '.join(missing)}")
        return value

    @classmethod
    def _dependency_contract(
        cls,
        dependencies_value: Any,
        evidence_value: Any,
        name: str,
    ) -> tuple[list[str], list[dict]]:
        dependencies = cls._string_list(
            dependencies_value,
            f"{name} dependencies",
        )
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f"{name} dependencies must be unique")
        if not isinstance(evidence_value, list):
            raise ValueError(f"{name} dependency_evidence must be a list")
        normalized_evidence: list[dict] = []
        evidence_dependencies: list[str] = []
        for value in evidence_value:
            evidence = cls._require_fields(
                value,
                ("dependency", "reason", "evidence"),
                f"{name} dependency evidence",
            )
            dependency = cls._nonempty_string(
                evidence["dependency"],
                f"{name} dependency evidence dependency",
            )
            reason = cls._nonempty_string(
                evidence["reason"],
                f"{name} dependency evidence reason",
            )
            observations = cls._string_list(
                evidence["evidence"],
                f"{name} dependency evidence observations",
                nonempty=True,
            )
            evidence_dependencies.append(dependency)
            normalized_evidence.append(
                {
                    "dependency": dependency,
                    "reason": reason,
                    "evidence": observations,
                }
            )
        if (
            len(set(evidence_dependencies)) != len(evidence_dependencies)
            or set(evidence_dependencies) != set(dependencies)
        ):
            raise ValueError(
                f"{name} dependency_evidence must correspond exactly to dependencies"
            )
        evidence_by_dependency = {
            item["dependency"]: item for item in normalized_evidence
        }
        return dependencies, [
            evidence_by_dependency[dependency] for dependency in dependencies
        ]

    def _validate_persisted_pass_dependencies(
        self,
        connection: sqlite3.Connection,
        pass_id: int,
    ) -> None:
        for row in connection.execute(
            """
            SELECT dependencies, dependency_evidence FROM specifications
            WHERE pass_id = ?
            """,
            (pass_id,),
        ).fetchall():
            self._dependency_contract(
                json.loads(row["dependencies"]),
                json.loads(row["dependency_evidence"]),
                "persisted specification",
            )
        for row in connection.execute(
            """
            SELECT dependencies, dependency_evidence FROM work_items
            WHERE pass_id = ?
            """,
            (pass_id,),
        ).fetchall():
            self._dependency_contract(
                json.loads(row["dependencies"]),
                json.loads(row["dependency_evidence"]),
                "persisted work",
            )

    def add_repository(
        self,
        github_repository: str,
        target_branch: str,
        similarity_threshold: float,
        autonomous_issue_intake: bool = False,
    ) -> dict:
        self._nonempty_string(github_repository, "github_repository")
        self._nonempty_string(target_branch, "target_branch")
        if not isinstance(autonomous_issue_intake, bool):
            raise ValueError("autonomous_issue_intake must be boolean")
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO repositories(
                    github_repository, target_branch, similarity_threshold,
                    autonomous_issue_intake
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    github_repository,
                    target_branch,
                    float(similarity_threshold),
                    int(autonomous_issue_intake),
                ),
            )
            repository_id = cursor.lastrowid
            connection.executemany(
                """
                INSERT INTO nodes(
                    repository_id, classification, vector, role_prompt, persistence
                ) VALUES (?, ?, NULL, '', 'PERMANENT')
                """,
                (
                    (repository_id, "Issue Specifier"),
                    (repository_id, "Work Specifier"),
                    (repository_id, "Work Validator"),
                    (repository_id, "Issue Validator"),
                ),
            )
            row = self._fetch_one(
                connection, "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            )
        return self._repository_row(row)

    def list_repositories(self) -> list[dict]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM repositories WHERE tracked = 1 ORDER BY id"
            ).fetchall()
        return [self._repository_row(row) for row in rows]

    def get_repository(self, repository_id: int) -> dict | None:
        with self._reader() as connection:
            row = self._fetch_one(
                connection, "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            )
        return self._repository_row(row)

    def remove_repository(self, repository_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE repositories SET tracked = 0 WHERE id = ?", (repository_id,)
            )

    def set_autonomous_issue_intake(
        self,
        repository_id: int,
        enabled: bool,
    ) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("autonomous_issue_intake must be boolean")
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE repositories SET autonomous_issue_intake = ?
                WHERE id = ? AND tracked = 1
                """,
                (int(enabled), repository_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(repository_id)
            row = self._fetch_one(
                connection,
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id,),
            )
        repository = self._repository_row(row)
        if repository is None:
            raise RuntimeError("repository disappeared while updating intake")
        return repository

    def get_issue_order_plan(self, repository_id: int) -> dict | None:
        with self._reader() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM issue_order_plans WHERE repository_id = ?",
                (repository_id,),
            )
        return self._decode(
            row,
            json_fields=("issue_snapshot", "result"),
        )

    def save_issue_order_plan(
        self,
        repository_id: int,
        issue_snapshot: list[dict],
        result: dict,
    ) -> dict:
        if not isinstance(issue_snapshot, list) or any(
            not isinstance(issue, dict) for issue in issue_snapshot
        ):
            raise ValueError("issue_snapshot must be a list of objects")
        if not isinstance(result, dict):
            raise ValueError("issue order result must be an object")
        snapshot_payload = self._dump(issue_snapshot)
        result_payload = self._dump(result)
        with self._transaction() as connection:
            repository = self._fetch_one(
                connection,
                "SELECT id FROM repositories WHERE id = ?",
                (repository_id,),
            )
            if repository is None:
                raise KeyError(repository_id)
            connection.execute(
                """
                INSERT INTO issue_order_plans(repository_id, issue_snapshot, result)
                VALUES (?, ?, ?)
                ON CONFLICT(repository_id) DO UPDATE SET
                    issue_snapshot = excluded.issue_snapshot,
                    result = excluded.result
                """,
                (repository_id, snapshot_payload, result_payload),
            )
            row = self._fetch_one(
                connection,
                "SELECT * FROM issue_order_plans WHERE repository_id = ?",
                (repository_id,),
            )
        decoded = self._decode(
            row,
            json_fields=("issue_snapshot", "result"),
        )
        if decoded is None:
            raise RuntimeError("issue order plan disappeared while saving")
        return decoded

    def list_nodes(self, repository_id: int) -> list[dict]:
        with self._reader() as connection:
            rows = connection.execute(
                """
                SELECT * FROM nodes
                WHERE repository_id = ? AND active = 1
                ORDER BY CASE classification
                    WHEN 'Issue Specifier' THEN 0
                    WHEN 'Work Specifier' THEN 1
                    WHEN 'Work Validator' THEN 2
                    WHEN 'Issue Validator' THEN 3
                    ELSE 4
                END, id
                """,
                (repository_id,),
            ).fetchall()
        return [self._node_row(row) for row in rows]

    def create_run(
        self, repository_id: int, issue_number: int, issue_json: dict
    ) -> tuple[dict, bool]:
        if not isinstance(issue_json, dict):
            raise ValueError("issue_json must be an object")
        issue_payload = self._dump(issue_json)
        with self._transaction() as connection:
            existing = self._fetch_one(
                connection,
                """
                SELECT * FROM runs
                WHERE repository_id = ? AND issue_number = ?
                  AND state NOT IN ('COMPLETED', 'CLOSED')
                """,
                (repository_id, issue_number),
            )
            if existing is not None:
                return self._run_row(existing), False
            cursor = connection.execute(
                """
                INSERT INTO runs(repository_id, issue_number, issue_json, state)
                VALUES (?, ?, ?, 'QUEUED')
                """,
                (repository_id, issue_number, issue_payload),
            )
            row = self._fetch_one(
                connection, "SELECT * FROM runs WHERE id = ?", (cursor.lastrowid,)
            )
        return self._run_row(row), True

    def get_run(self, run_id: int) -> dict | None:
        with self._reader() as connection:
            row = self._fetch_one(connection, "SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run_row(row)

    def list_runs(self, repository_id: int | None = None) -> list[dict]:
        with self._reader() as connection:
            if repository_id is None:
                rows = connection.execute("SELECT * FROM runs ORDER BY id").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM runs WHERE repository_id = ? ORDER BY id",
                    (repository_id,),
                ).fetchall()
        return [self._run_row(row) for row in rows]

    def transition_run(self, run_id: int, state: str, **fields: Any) -> dict:
        if state not in RUN_STATES:
            raise ValueError(f"invalid run state: {state}")
        unsupported = set(fields) - {
            "branch",
            "pull_request",
            "pr_listening_since",
        }
        if unsupported:
            raise ValueError(f"unsupported run fields: {', '.join(sorted(unsupported))}")
        if "branch" in fields and fields["branch"] is not None:
            self._nonempty_string(fields["branch"], "branch")
        if "pr_listening_since" in fields:
            listening_since = fields["pr_listening_since"]
            if listening_since is not None:
                if isinstance(listening_since, bool) or not isinstance(
                    listening_since, (int, float)
                ):
                    raise ValueError("pr_listening_since must be a number or null")
                fields["pr_listening_since"] = float(listening_since)
        values = dict(fields)
        if "pull_request" in values:
            if values["pull_request"] is not None and not isinstance(values["pull_request"], dict):
                raise ValueError("pull_request must be an object or null")
            values["pull_request"] = (
                None if values["pull_request"] is None else self._dump(values["pull_request"])
            )

        with self._transaction() as connection:
            current = self._fetch_one(
                connection, "SELECT * FROM runs WHERE id = ?", (run_id,)
            )
            if current is None:
                raise KeyError(run_id)
            if current["state"] in TERMINAL_RUN_STATES and state != current["state"]:
                raise ValueError("terminal runs cannot transition")
            assignments = ["state = ?"]
            parameters: list[Any] = [state]
            for field in ("branch", "pull_request", "pr_listening_since"):
                if field in values:
                    assignments.append(f"{field} = ?")
                    parameters.append(values[field])
            parameters.append(run_id)
            connection.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE id = ?", parameters
            )
            row = self._fetch_one(connection, "SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run_row(row)

    def recover_interrupted_work(self) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE work_items SET state = 'QUEUED' WHERE state = 'RUNNING'"
            )
            return cursor.rowcount

    def create_pass(self, run_id: int, trigger_type: str, trigger_json: dict) -> dict:
        self._nonempty_string(trigger_type, "trigger_type")
        if not isinstance(trigger_json, dict):
            raise ValueError("trigger_json must be an object")
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO execution_passes(run_id, trigger_type, trigger_json)
                VALUES (?, ?, ?)
                """,
                (run_id, trigger_type, self._dump(trigger_json)),
            )
            row = self._fetch_one(
                connection,
                "SELECT * FROM execution_passes WHERE id = ?",
                (cursor.lastrowid,),
            )
        return self._pass_row(row)

    def create_pass_and_transition(
        self,
        run_id: int,
        expected_latest_pass_id: int,
        trigger_type: str,
        trigger_json: dict,
        state: str,
    ) -> dict:
        self._nonempty_string(trigger_type, "trigger_type")
        if not isinstance(trigger_json, dict):
            raise ValueError("trigger_json must be an object")
        if state not in RUN_STATES:
            raise ValueError(f"invalid run state: {state}")
        payload = self._dump(trigger_json)
        with self._transaction() as connection:
            run = self._fetch_one(
                connection,
                "SELECT state FROM runs WHERE id = ?",
                (run_id,),
            )
            if run is None:
                raise KeyError(run_id)
            if run["state"] in TERMINAL_RUN_STATES:
                raise ValueError("terminal runs cannot transition")
            latest_pass = self._fetch_one(
                connection,
                """
                SELECT id FROM execution_passes
                WHERE run_id = ? ORDER BY id DESC LIMIT 1
                """,
                (run_id,),
            )
            if (
                latest_pass is None
                or latest_pass["id"] != expected_latest_pass_id
            ):
                raise ValueError("latest execution pass changed")
            cursor = connection.execute(
                """
                INSERT INTO execution_passes(run_id, trigger_type, trigger_json)
                VALUES (?, ?, ?)
                """,
                (run_id, trigger_type, payload),
            )
            connection.execute(
                "UPDATE runs SET state = ? WHERE id = ?",
                (state, run_id),
            )
            row = self._fetch_one(
                connection,
                "SELECT * FROM execution_passes WHERE id = ?",
                (cursor.lastrowid,),
            )
        return self._pass_row(row)

    def list_passes(self, run_id: int) -> list[dict]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_passes WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [self._pass_row(row) for row in rows]

    def record_issue_specification(
        self, run_id: int, pass_id: int, result: dict
    ) -> dict:
        return self._record_pass_result(
            "issue_specifications", run_id, pass_id, result
        )

    def get_issue_specification(self, run_id: int, pass_id: int) -> dict | None:
        with self._reader() as connection:
            row = self._fetch_one(
                connection,
                "SELECT result FROM issue_specifications WHERE run_id = ? AND pass_id = ?",
                (run_id, pass_id),
            )
        return None if row is None else json.loads(row["result"])

    def record_work_specification_result(
        self,
        run_id: int,
        pass_id: int,
        work_area_key: str,
        result: dict,
    ) -> dict:
        key = self._nonempty_string(work_area_key, "work area key")
        payload = self._dump(result)
        requested = json.loads(payload)
        with self._transaction() as connection:
            execution_pass = self._fetch_one(
                connection,
                "SELECT id FROM execution_passes WHERE id = ? AND run_id = ?",
                (pass_id, run_id),
            )
            if execution_pass is None:
                raise ValueError("pass does not belong to run")
            existing = self._fetch_one(
                connection,
                "SELECT result FROM work_specification_results "
                "WHERE pass_id = ? AND work_area_key = ?",
                (pass_id, key),
            )
            if existing is not None:
                stored = json.loads(existing["result"])
                if stored != requested:
                    raise ValueError(
                        "work area already has a different specification result"
                    )
                return stored
            connection.execute(
                "INSERT INTO work_specification_results"
                "(run_id, pass_id, work_area_key, result) VALUES (?, ?, ?, ?)",
                (run_id, pass_id, key, payload),
            )
        return requested

    def list_work_specification_results(
        self, run_id: int, pass_id: int
    ) -> list[dict]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT work_area_key, result FROM work_specification_results "
                "WHERE run_id = ? AND pass_id = ? ORDER BY work_area_key",
                (run_id, pass_id),
            ).fetchall()
        return [
            {"work_area_key": row["work_area_key"], "result": json.loads(row["result"])}
            for row in rows
        ]

    def clear_work_specification_results(
        self,
        run_id: int,
        pass_id: int,
    ) -> None:
        with self._transaction() as connection:
            execution_pass = self._fetch_one(
                connection,
                "SELECT id FROM execution_passes WHERE id = ? AND run_id = ?",
                (pass_id, run_id),
            )
            if execution_pass is None:
                raise ValueError("pass does not belong to run")
            persisted_package = self._fetch_one(
                connection,
                "SELECT id FROM specifications WHERE run_id = ? AND pass_id = ?",
                (run_id, pass_id),
            )
            if persisted_package is not None:
                raise ValueError(
                    "cannot clear work specifications after package persistence"
                )
            connection.execute(
                "DELETE FROM work_specification_results "
                "WHERE run_id = ? AND pass_id = ?",
                (run_id, pass_id),
            )

    def _record_pass_result(
        self, table: str, run_id: int, pass_id: int, result: dict
    ) -> dict:
        if table not in {"issue_specifications"}:
            raise ValueError("unsupported pass result table")
        payload = self._dump(result)
        requested = json.loads(payload)
        with self._transaction() as connection:
            execution_pass = self._fetch_one(
                connection,
                "SELECT id FROM execution_passes WHERE id = ? AND run_id = ?",
                (pass_id, run_id),
            )
            if execution_pass is None:
                raise ValueError("pass does not belong to run")
            existing = self._fetch_one(
                connection,
                f'SELECT result FROM "{table}" WHERE run_id = ? AND pass_id = ?',
                (run_id, pass_id),
            )
            if existing is not None:
                stored = json.loads(existing["result"])
                if stored != requested:
                    raise ValueError(f"{table} already has a different result")
                return stored
            connection.execute(
                f'INSERT INTO "{table}"(run_id, pass_id, result) VALUES (?, ?, ?)',
                (run_id, pass_id, payload),
            )
        return requested

    def _validated_package(self, package: dict) -> list[dict]:
        package = self._require_fields(package, ("specifications",), "package")
        specifications = package["specifications"]
        if not isinstance(specifications, list) or not specifications:
            raise ValueError("specifications must be a nonempty list")

        normalized: list[dict] = []
        specification_keys: set[str] = set()
        work_keys: set[str] = set()
        specification_fields = (
            "key",
            "title",
            "description",
            "acceptance_criteria",
            "dependencies",
            "dependency_evidence",
            "executable",
            "work_items",
        )
        work_fields = (
            "key",
            "title",
            "description",
            "classification",
            "dependencies",
            "dependency_evidence",
        )

        for specification_value in specifications:
            specification = self._require_fields(
                specification_value, specification_fields, "specification"
            )
            key = self._nonempty_string(specification["key"], "specification key")
            if key in specification_keys:
                raise ValueError("specification keys must be unique")
            specification_keys.add(key)
            title = self._nonempty_string(specification["title"], "specification title")
            description = self._nonempty_string(
                specification["description"], "specification description"
            )
            acceptance_criteria = self._string_list(
                specification["acceptance_criteria"],
                "acceptance criteria",
                nonempty=True,
            )
            requirement_keys = self._string_list(
                specification.get("requirement_keys", []),
                "specification requirement_keys",
            )
            acceptance_traceability = specification.get(
                "acceptance_traceability", []
            )
            if not isinstance(acceptance_traceability, list):
                raise ValueError("specification acceptance_traceability must be a list")
            dependencies, dependency_evidence = self._dependency_contract(
                specification["dependencies"],
                specification["dependency_evidence"],
                "specification",
            )
            if not isinstance(specification["executable"], bool):
                raise ValueError("executable must be a boolean")
            executable = specification["executable"]
            work_values = specification["work_items"]
            if not isinstance(work_values, list) or (executable and not work_values):
                raise ValueError("every executable specification requires work")

            work_items: list[dict] = []
            for work_value in work_values:
                work = self._require_fields(work_value, work_fields, "work item")
                work_key = self._nonempty_string(work["key"], "work key")
                if work_key in work_keys:
                    raise ValueError("work keys must be unique")
                work_keys.add(work_key)
                work_dependencies, work_dependency_evidence = (
                    self._dependency_contract(
                        work["dependencies"],
                        work["dependency_evidence"],
                        "work",
                    )
                )
                work_items.append(
                    {
                        "key": work_key,
                        "title": self._nonempty_string(work["title"], "work title"),
                        "description": self._nonempty_string(
                            work["description"], "work description"
                        ),
                        "classification": self._classification(work["classification"]),
                        "dependencies": work_dependencies,
                        "dependency_evidence": work_dependency_evidence,
                        "requirement_keys": self._string_list(
                            work.get("requirement_keys", []),
                            "work requirement_keys",
                        ),
                        "acceptance_criteria": self._string_list(
                            work.get("acceptance_criteria", []),
                            "work acceptance_criteria",
                        ),
                        "evidence_requirements": self._string_list(
                            work.get("evidence_requirements", []),
                            "work evidence_requirements",
                        ),
                    }
                )
            normalized.append(
                {
                    "key": key,
                    "title": title,
                    "description": description,
                    "acceptance_criteria": acceptance_criteria,
                    "dependencies": dependencies,
                    "dependency_evidence": dependency_evidence,
                    "requirement_keys": requirement_keys,
                    "acceptance_traceability": acceptance_traceability,
                    "executable": executable,
                    "work_items": work_items,
                }
            )

        for specification in normalized:
            if any(
                dependency not in specification_keys
                for dependency in specification["dependencies"]
            ):
                raise ValueError("specification dependency does not reference this package")
            for work in specification["work_items"]:
                if any(dependency not in work_keys for dependency in work["dependencies"]):
                    raise ValueError("work dependency does not reference this package")
        self._require_acyclic(
            {
                specification["key"]: specification["dependencies"]
                for specification in normalized
            },
            "specification dependency graph",
        )
        self._require_acyclic(
            {
                work["key"]: work["dependencies"]
                for specification in normalized
                for work in specification["work_items"]
            },
            "work dependency graph",
        )
        return normalized

    def save_specification_package(
        self, run_id: int, pass_id: int, package: dict
    ) -> dict:
        specifications = self._validated_package(package)
        specification_ids: list[int] = []
        work_ids: list[int] = []
        with self._transaction() as connection:
            execution_pass = self._fetch_one(
                connection,
                "SELECT id FROM execution_passes WHERE id = ? AND run_id = ?",
                (pass_id, run_id),
            )
            if execution_pass is None:
                raise ValueError("pass does not belong to run")
            for specification in specifications:
                cursor = connection.execute(
                    """
                    INSERT INTO specifications(
                        run_id, pass_id, key, title, description,
                        acceptance_criteria, dependencies, executable,
                        dependency_evidence
                        , requirement_keys, acceptance_traceability
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        pass_id,
                        specification["key"],
                        specification["title"],
                        specification["description"],
                        self._dump(specification["acceptance_criteria"]),
                        self._dump(specification["dependencies"]),
                        int(specification["executable"]),
                        self._dump(specification["dependency_evidence"]),
                        self._dump(specification["requirement_keys"]),
                        self._dump(specification["acceptance_traceability"]),
                    ),
                )
                specification_id = cursor.lastrowid
                specification_ids.append(specification_id)
                for work in specification["work_items"]:
                    work_cursor = connection.execute(
                        """
                        INSERT INTO work_items(
                            run_id, pass_id, specification_id, key, title, description,
                            classification, dependencies, state,
                            dependency_evidence
                            , requirement_keys, acceptance_criteria,
                            evidence_requirements
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'UNASSIGNED', ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            pass_id,
                            specification_id,
                            work["key"],
                            work["title"],
                            work["description"],
                            work["classification"],
                            self._dump(work["dependencies"]),
                            self._dump(work["dependency_evidence"]),
                            self._dump(work["requirement_keys"]),
                            self._dump(work["acceptance_criteria"]),
                            self._dump(work["evidence_requirements"]),
                        ),
                    )
                    work_ids.append(work_cursor.lastrowid)
            specification_rows = [
                self._specification_row(
                    self._fetch_one(
                        connection, "SELECT * FROM specifications WHERE id = ?", (row_id,)
                    )
                )
                for row_id in specification_ids
            ]
            work_rows = [
                self._work_row(
                    self._fetch_one(
                        connection, "SELECT * FROM work_items WHERE id = ?", (row_id,)
                    )
                )
                for row_id in work_ids
            ]
        return {"specifications": specification_rows, "work_items": work_rows}

    def list_specifications(self, run_id: int) -> list[dict]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM specifications WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [self._specification_row(row) for row in rows]

    def list_work_items(self, run_id: int, pass_id: int | None = None) -> list[dict]:
        with self._reader() as connection:
            if pass_id is None:
                rows = connection.execute(
                    "SELECT * FROM work_items WHERE run_id = ? ORDER BY id", (run_id,)
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM work_items
                    WHERE run_id = ? AND pass_id = ?
                    ORDER BY id
                    """,
                    (run_id, pass_id),
                ).fetchall()
        return [self._work_row(row) for row in rows]

    def get_classification_vector(
        self, repository_id: int, classification: str
    ) -> list[float] | None:
        label = self._classification(classification)
        with self._reader() as connection:
            row = self._fetch_one(
                connection,
                """
                SELECT vector FROM classification_vectors
                WHERE repository_id = ? AND classification = ?
                """,
                (repository_id, label),
            )
        return None if row is None else json.loads(row["vector"])

    def save_classification_vector(
        self, repository_id: int, classification: str, vector: list[float]
    ) -> list[float]:
        label = self._classification(classification)
        if not isinstance(vector, list):
            raise ValueError("vector must be a list")
        try:
            normalized_vector = [float(component) for component in vector]
        except (TypeError, ValueError) as error:
            raise ValueError("vector must contain numbers") from error
        payload = self._dump(normalized_vector)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO classification_vectors(
                    repository_id, classification, vector
                ) VALUES (?, ?, ?)
                ON CONFLICT(repository_id, classification)
                DO UPDATE SET vector = excluded.vector
                """,
                (repository_id, label, payload),
            )
            row = self._fetch_one(
                connection,
                """
                SELECT vector FROM classification_vectors
                WHERE repository_id = ? AND classification = ?
                """,
                (repository_id, label),
            )
        return json.loads(row["vector"])

    def create_dynamic_node(
        self,
        repository_id: int,
        classification: str,
        vector: list[float],
        role_prompt: str,
    ) -> dict:
        label = self._classification(classification)
        if not isinstance(vector, list):
            raise ValueError("vector must be a list")
        try:
            normalized_vector = [float(component) for component in vector]
        except (TypeError, ValueError) as error:
            raise ValueError("vector must contain numbers") from error
        if not isinstance(role_prompt, str):
            raise ValueError("role_prompt must be a string")
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO nodes(
                    repository_id, classification, vector, role_prompt, persistence
                ) VALUES (?, ?, ?, ?, 'EPHEMERAL')
                """,
                (repository_id, label, self._dump(normalized_vector), role_prompt),
            )
            row = self._fetch_one(
                connection, "SELECT * FROM nodes WHERE id = ?", (cursor.lastrowid,)
            )
        return self._node_row(row)

    def list_dynamic_nodes(self, repository_id: int) -> list[dict]:
        with self._reader() as connection:
            rows = connection.execute(
                """
                SELECT * FROM nodes
                WHERE repository_id = ? AND active = 1
                  AND persistence IN ('EPHEMERAL', 'PERSISTENT')
                ORDER BY id
                """,
                (repository_id,),
            ).fetchall()
        return [self._node_row(row) for row in rows]

    def assign_work(self, work_id: int, node_id: int) -> dict:
        with self._transaction() as connection:
            work = self._fetch_one(
                connection,
                """
                SELECT work_items.*, runs.repository_id
                FROM work_items
                JOIN runs ON runs.id = work_items.run_id
                WHERE work_items.id = ?
                """,
                (work_id,),
            )
            node = self._fetch_one(
                connection, "SELECT * FROM nodes WHERE id = ?", (node_id,)
            )
            if work is None:
                raise KeyError(work_id)
            if node is None:
                raise KeyError(node_id)
            if work["state"] != "UNASSIGNED":
                raise ValueError("only unassigned work can be assigned")
            if (
                not node["active"]
                or node["persistence"] == "PERMANENT"
                or node["repository_id"] != work["repository_id"]
            ):
                raise ValueError("work must be assigned to an active dynamic node in its repository")
            connection.execute(
                "UPDATE work_items SET node_id = ?, state = 'QUEUED' WHERE id = ?",
                (node_id, work_id),
            )
            row = self._fetch_one(
                connection, "SELECT * FROM work_items WHERE id = ?", (work_id,)
            )
        return self._work_row(row)

    @staticmethod
    def _work_resolutions(rows: list[sqlite3.Row] | list[dict]) -> dict[str, str]:
        work_by_id = {row["id"]: row for row in rows}
        children: dict[int, list[int]] = {}
        for row in rows:
            parent_id = row["parent_work_id"]
            if parent_id is not None:
                children.setdefault(parent_id, []).append(row["id"])
        cache: dict[int, str] = {}

        def resolve(work_id: int, visiting: set[int]) -> str:
            if work_id in cache:
                return cache[work_id]
            if work_id in visiting:
                return "PENDING"
            work = work_by_id[work_id]
            state = work["state"]
            if state == "COMPLETED":
                resolution = "SATISFIED"
            elif state == "FAILED":
                resolution = "FAILED"
            elif state != "HANDED_OFF":
                resolution = "PENDING"
            else:
                child_ids = children.get(work_id, [])
                child_resolutions = [
                    resolve(child_id, {*visiting, work_id})
                    for child_id in child_ids
                ]
                if any(value == "FAILED" for value in child_resolutions):
                    resolution = "FAILED"
                elif child_resolutions and all(
                    value == "SATISFIED" for value in child_resolutions
                ):
                    resolution = "SATISFIED"
                else:
                    resolution = "PENDING"
            cache[work_id] = resolution
            return resolution

        return {row["key"]: resolve(row["id"], set()) for row in rows}

    def claim_node_work(self, node_id: int, run_id: int) -> dict | None:
        with self._transaction() as connection:
            node = self._fetch_one(
                connection,
                """
                SELECT * FROM nodes
                WHERE id = ? AND active = 1
                  AND persistence IN ('EPHEMERAL', 'PERSISTENT')
                """,
                (node_id,),
            )
            if node is None:
                return None
            running = self._fetch_one(
                connection,
                "SELECT id FROM work_items WHERE node_id = ? AND state = 'RUNNING'",
                (node_id,),
            )
            if running is not None:
                return None
            candidates = connection.execute(
                """
                SELECT * FROM work_items
                WHERE node_id = ? AND run_id = ? AND state = 'QUEUED'
                ORDER BY id
                """,
                (node_id, run_id),
            ).fetchall()
            selected: sqlite3.Row | None = None
            for candidate in candidates:
                self._validate_persisted_pass_dependencies(
                    connection,
                    candidate["pass_id"],
                )
                pass_work = connection.execute(
                    """
                    SELECT id, key, state, parent_work_id, specification_id
                    FROM work_items WHERE pass_id = ?
                    """,
                    (candidate["pass_id"],),
                ).fetchall()
                work_by_key = {row["key"]: row for row in pass_work}
                resolutions = self._work_resolutions(pass_work)
                dependencies = json.loads(candidate["dependencies"])
                if dependencies:
                    blocked = False
                    for key in dependencies:
                        dependency = work_by_key.get(key)
                        direct_parent_handoff = (
                            dependency is not None
                            and dependency["id"] == candidate["parent_work_id"]
                            and dependency["state"] == "HANDED_OFF"
                        )
                        if not direct_parent_handoff and resolutions.get(key) != "SATISFIED":
                            blocked = True
                            break
                    if blocked:
                        continue

                specification = self._fetch_one(
                    connection,
                    "SELECT dependencies FROM specifications WHERE id = ?",
                    (candidate["specification_id"],),
                )
                if specification is None:
                    continue
                specification_dependencies = list(
                    dict.fromkeys(json.loads(specification["dependencies"]))
                )
                if specification_dependencies:
                    key_placeholders = ", ".join(
                        "?" for _ in specification_dependencies
                    )
                    dependency_specifications = connection.execute(
                        f"""
                        SELECT id FROM specifications
                        WHERE pass_id = ? AND key IN ({key_placeholders})
                        """,
                        (candidate["pass_id"], *specification_dependencies),
                    ).fetchall()
                    if len(dependency_specifications) != len(
                        specification_dependencies
                    ):
                        continue
                    dependency_ids = {
                        row["id"] for row in dependency_specifications
                    }
                    if any(
                        resolutions[row["key"]] != "SATISFIED"
                        for row in pass_work
                        if row["specification_id"] in dependency_ids
                    ):
                        continue

                selected = candidate
                break
            if selected is None:
                return None
            connection.execute(
                "UPDATE work_items SET state = 'RUNNING' WHERE id = ? AND state = 'QUEUED'",
                (selected["id"],),
            )
            row = self._fetch_one(
                connection, "SELECT * FROM work_items WHERE id = ?", (selected["id"],)
            )
        return self._work_row(row)

    def _validated_result(self, result: dict) -> dict:
        result = self._require_fields(
            result,
            ("output", "artifacts", "test_results", "repository_state"),
            "result",
        )
        self._dump(result)
        return result

    def _validated_handoff(self, handoff: dict) -> dict:
        handoff = self._require_fields(
            handoff,
            (
                "classification",
                "context",
                "artifacts",
                "dependencies",
                "dependency_evidence",
                "blocking",
            ),
            "handoff",
        )
        if not isinstance(handoff["context"], dict):
            raise ValueError("handoff context must be an object")
        if not isinstance(handoff["artifacts"], list):
            raise ValueError("handoff artifacts must be a list")
        dependencies, dependency_evidence = self._dependency_contract(
            handoff["dependencies"],
            handoff["dependency_evidence"],
            "handoff",
        )
        if handoff["blocking"] is not None and not isinstance(handoff["blocking"], dict):
            raise ValueError("handoff blocking must be an object or null")
        normalized = dict(handoff)
        normalized["classification"] = self._classification(handoff["classification"])
        normalized["dependencies"] = dependencies
        normalized["dependency_evidence"] = dependency_evidence
        self._dump(normalized)
        return normalized

    def complete_work(
        self, work_id: int, result: dict, handoff: dict | None = None
    ) -> dict | None:
        result = self._validated_result(result)
        normalized_handoff = None if handoff is None else self._validated_handoff(handoff)
        with self._transaction() as connection:
            work = self._fetch_one(
                connection, "SELECT * FROM work_items WHERE id = ?", (work_id,)
            )
            if work is None:
                raise KeyError(work_id)
            if work["state"] != "RUNNING":
                raise ValueError("only running work can complete")
            self._validate_persisted_pass_dependencies(
                connection,
                work["pass_id"],
            )
            if normalized_handoff is None:
                connection.execute(
                    "UPDATE work_items SET state = 'COMPLETED', result = ? WHERE id = ?",
                    (self._dump(result), work_id),
                )
                return None

            dependency_keys = {
                row["key"]
                for row in connection.execute(
                    "SELECT key FROM work_items WHERE pass_id = ?", (work["pass_id"],)
                ).fetchall()
            }
            if any(
                dependency not in dependency_keys
                for dependency in normalized_handoff["dependencies"]
            ):
                raise ValueError("handoff dependency does not reference this pass")
            child_key = f"{work['key']}:handoff:{work_id}"
            pass_work = connection.execute(
                """
                SELECT id, parent_work_id, key, dependencies
                FROM work_items WHERE pass_id = ?
                """,
                (work["pass_id"],),
            ).fetchall()
            work_by_id = {row["id"]: row for row in pass_work}
            dependency_graph = {}
            for row in pass_work:
                parent = work_by_id.get(row["parent_work_id"])
                dependency_graph[row["key"]] = [
                    dependency
                    for dependency in json.loads(row["dependencies"])
                    if parent is None or dependency != parent["key"]
                ]
            for row in pass_work:
                parent = work_by_id.get(row["parent_work_id"])
                if parent is not None:
                    dependency_graph[parent["key"]].append(row["key"])
            dependency_graph[work["key"]].append(child_key)
            dependency_graph[child_key] = [
                dependency
                for dependency in normalized_handoff["dependencies"]
                if dependency != work["key"]
            ]
            self._require_acyclic(
                dependency_graph,
                "handoff dependency graph",
            )
            connection.execute(
                "UPDATE work_items SET state = 'HANDED_OFF', result = ? WHERE id = ?",
                (self._dump(result), work_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO work_items(
                    run_id, pass_id, specification_id, parent_work_id,
                    key, title, description, classification, dependencies,
                    dependency_evidence, requirement_keys,
                    acceptance_criteria, evidence_requirements, state, handoff
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNASSIGNED', ?)
                """,
                (
                    work["run_id"],
                    work["pass_id"],
                    work["specification_id"],
                    work_id,
                    child_key,
                    work["title"],
                    work["description"],
                    normalized_handoff["classification"],
                    self._dump(normalized_handoff["dependencies"]),
                    self._dump(normalized_handoff["dependency_evidence"]),
                    work["requirement_keys"],
                    work["acceptance_criteria"],
                    work["evidence_requirements"],
                    self._dump(normalized_handoff),
                ),
            )
            row = self._fetch_one(
                connection, "SELECT * FROM work_items WHERE id = ?", (cursor.lastrowid,)
            )
        return self._work_row(row)

    def fail_work(self, work_id: int, result: dict) -> dict:
        result = self._validated_result(result)
        with self._transaction() as connection:
            work = self._fetch_one(
                connection, "SELECT * FROM work_items WHERE id = ?", (work_id,)
            )
            if work is None:
                raise KeyError(work_id)
            if work["state"] != "RUNNING":
                raise ValueError("only running work can fail")
            connection.execute(
                "UPDATE work_items SET state = 'FAILED', result = ? WHERE id = ?",
                (self._dump(result), work_id),
            )
            row = self._fetch_one(
                connection, "SELECT * FROM work_items WHERE id = ?", (work_id,)
            )
        return self._work_row(row)

    def settle_failed_pass_work(
        self,
        run_id: int,
        pass_id: int,
        result: dict,
    ) -> bool:
        result = self._validated_result(result)
        payload = self._dump(result)
        with self._transaction() as connection:
            execution_pass = self._fetch_one(
                connection,
                "SELECT id FROM execution_passes WHERE id = ? AND run_id = ?",
                (pass_id, run_id),
            )
            if execution_pass is None:
                raise ValueError("pass does not belong to run")
            self._validate_persisted_pass_dependencies(connection, pass_id)
            rows = [
                dict(row)
                for row in connection.execute(
                """
                SELECT work_items.id, work_items.key, work_items.state,
                       work_items.parent_work_id, work_items.specification_id,
                       work_items.dependencies, specifications.key AS specification_key,
                       specifications.dependencies AS specification_dependencies
                FROM work_items
                JOIN specifications
                  ON specifications.id = work_items.specification_id
                WHERE work_items.run_id = ? AND work_items.pass_id = ?
                ORDER BY work_items.id
                """,
                (run_id, pass_id),
                ).fetchall()
            ]
            if not any(row["state"] == "FAILED" for row in rows):
                return False

            changed = True
            while changed:
                changed = False
                resolutions = self._work_resolutions(rows)
                specification_resolutions: dict[str, list[str]] = {}
                for row in rows:
                    specification_resolutions.setdefault(
                        row["specification_key"], []
                    ).append(resolutions[row["key"]])
                for row in rows:
                    if row["state"] not in {"UNASSIGNED", "QUEUED"}:
                        continue
                    work_blocked = any(
                        resolutions.get(dependency) == "FAILED"
                        for dependency in json.loads(row["dependencies"])
                    )
                    specification_blocked = any(
                        "FAILED" in specification_resolutions.get(dependency, ())
                        for dependency in json.loads(
                            row["specification_dependencies"]
                        )
                    )
                    if not work_blocked and not specification_blocked:
                        continue
                    connection.execute(
                        """
                        UPDATE work_items SET state = 'FAILED', result = ?
                        WHERE id = ? AND state IN ('UNASSIGNED', 'QUEUED')
                        """,
                        (payload, row["id"]),
                    )
                    row["state"] = "FAILED"
                    changed = True

            return not any(
                row["state"] in {"UNASSIGNED", "QUEUED", "RUNNING"}
                for row in rows
            )

    def validation_barrier_ready(self, run_id: int, pass_id: int) -> bool:
        terminal_placeholders = ", ".join("?" for _ in TERMINAL_WORK_STATES)
        terminal_states = tuple(TERMINAL_WORK_STATES)
        with self._reader() as connection:
            execution_pass = self._fetch_one(
                connection,
                "SELECT id FROM execution_passes WHERE id = ? AND run_id = ?",
                (pass_id, run_id),
            )
            if execution_pass is None:
                return False
            self._validate_persisted_pass_dependencies(connection, pass_id)
            specification_count = connection.execute(
                "SELECT COUNT(*) FROM specifications WHERE run_id = ? AND pass_id = ?",
                (run_id, pass_id),
            ).fetchone()[0]
            if specification_count == 0:
                return False
            failed_pass_work = connection.execute(
                """
                SELECT COUNT(*) FROM work_items
                WHERE run_id = ? AND pass_id = ? AND state = 'FAILED'
                """,
                (run_id, pass_id),
            ).fetchone()[0]
            if failed_pass_work:
                return False
            nonterminal_pass_work = connection.execute(
                f"""
                SELECT COUNT(*) FROM work_items
                WHERE run_id = ? AND pass_id = ?
                  AND state NOT IN ({terminal_placeholders})
                """,
                (run_id, pass_id, *terminal_states),
            ).fetchone()[0]
            if nonterminal_pass_work:
                return False
            incomplete_executable = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM specifications AS specifications
                WHERE specifications.run_id = ?
                  AND specifications.pass_id = ?
                  AND specifications.executable = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM work_items AS work
                      WHERE work.specification_id = specifications.id
                        AND work.state IN ({terminal_placeholders})
                  )
                """,
                (run_id, pass_id, *terminal_states),
            ).fetchone()[0]
            if incomplete_executable:
                return False
            outstanding_node_work = connection.execute(
                f"""
                SELECT COUNT(*) FROM work_items
                WHERE run_id = ? AND node_id IS NOT NULL
                  AND state NOT IN ({terminal_placeholders})
                """,
                (run_id, *terminal_states),
            ).fetchone()[0]
            return outstanding_node_work == 0

    def record_validation(self, run_id: int, pass_id: int, result: dict) -> dict:
        if not isinstance(result, dict):
            raise ValueError("validation result must be an object")
        payload = self._dump(result)
        with self._transaction() as connection:
            execution_pass = self._fetch_one(
                connection,
                "SELECT id FROM execution_passes WHERE id = ? AND run_id = ?",
                (pass_id, run_id),
            )
            if execution_pass is None:
                raise ValueError("pass does not belong to run")
            existing = self._fetch_one(
                connection,
                "SELECT * FROM validations WHERE run_id = ? AND pass_id = ?",
                (run_id, pass_id),
            )
            if existing is not None:
                return self._validation_row(existing)
            cursor = connection.execute(
                "INSERT INTO validations(run_id, pass_id, result) VALUES (?, ?, ?)",
                (run_id, pass_id, payload),
            )
            row = self._fetch_one(
                connection, "SELECT * FROM validations WHERE id = ?", (cursor.lastrowid,)
            )
        return self._validation_row(row)

    def list_validations(self, run_id: int) -> list[dict]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM validations WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [self._validation_row(row) for row in rows]

    def record_work_validation(
        self,
        run_id: int,
        pass_id: int,
        work_id: int,
        result: dict,
    ) -> dict:
        payload = self._dump(result)
        with self._transaction() as connection:
            work = self._fetch_one(
                connection,
                "SELECT id, state FROM work_items "
                "WHERE id = ? AND run_id = ? AND pass_id = ?",
                (work_id, run_id, pass_id),
            )
            if work is None:
                raise ValueError("work validation target does not belong to pass")
            if work["state"] != "RUNNING":
                raise ValueError("work validation target must be running")
            attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM work_validations "
                "WHERE work_id = ?",
                (work_id,),
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO work_validations"
                "(run_id, pass_id, work_id, attempt, result) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, pass_id, work_id, attempt, payload),
            )
            row = self._fetch_one(
                connection,
                "SELECT * FROM work_validations WHERE id = ?",
                (cursor.lastrowid,),
            )
        decoded = dict(row)
        decoded["result"] = json.loads(decoded["result"])
        return decoded

    def list_work_validations(
        self, run_id: int, pass_id: int | None = None
    ) -> list[dict]:
        with self._reader() as connection:
            if pass_id is None:
                rows = connection.execute(
                    "SELECT * FROM work_validations WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM work_validations "
                    "WHERE run_id = ? AND pass_id = ? ORDER BY id",
                    (run_id, pass_id),
                ).fetchall()
        decoded = []
        for row in rows:
            value = dict(row)
            value["result"] = json.loads(value["result"])
            decoded.append(value)
        return decoded

    def record_feedback_scope_result(
        self, run_id: int, pass_id: int, result: dict
    ) -> dict:
        if not isinstance(result, dict):
            raise ValueError("feedback scope result must be an object")
        if result.get("specifications"):
            self._validated_package(
                {"specifications": result["specifications"]}
            )
        payload = self._dump(result)
        requested_result = json.loads(payload)
        with self._transaction() as connection:
            execution_pass = self._fetch_one(
                connection,
                "SELECT id FROM execution_passes WHERE id = ? AND run_id = ?",
                (pass_id, run_id),
            )
            if execution_pass is None:
                raise ValueError("pass does not belong to run")
            existing = self._fetch_one(
                connection,
                """
                SELECT result FROM feedback_scope_results
                WHERE run_id = ? AND pass_id = ?
                """,
                (run_id, pass_id),
            )
            if existing is not None:
                stored_result = json.loads(existing["result"])
                if stored_result != requested_result:
                    raise ValueError(
                        "feedback scope result already has a different result"
                    )
                return stored_result
            connection.execute(
                """
                INSERT INTO feedback_scope_results(run_id, pass_id, result)
                VALUES (?, ?, ?)
                """,
                (run_id, pass_id, payload),
            )
        return requested_result

    def get_feedback_scope_result(self, run_id: int, pass_id: int) -> dict | None:
        with self._reader() as connection:
            row = self._fetch_one(
                connection,
                """
                SELECT result FROM feedback_scope_results
                WHERE run_id = ? AND pass_id = ?
                """,
                (run_id, pass_id),
            )
        return None if row is None else json.loads(row["result"])

    def add_feedback(self, run_id: int, external_id: str, package: dict) -> bool:
        self._nonempty_string(external_id, "external_id")
        if not isinstance(package, dict):
            raise ValueError("feedback package must be an object")
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO feedback(run_id, external_id, package)
                VALUES (?, ?, ?)
                """,
                (run_id, external_id, self._dump(package)),
            )
            return cursor.rowcount == 1

    def record_feedback_disposition(
        self,
        run_id: int,
        external_id: str,
        disposition: str,
        result: dict,
    ) -> dict:
        self._nonempty_string(external_id, "external_id")
        if disposition not in FEEDBACK_DISPOSITIONS:
            raise ValueError(
                "disposition must be IN_SCOPE, OUT_OF_SCOPE, or INVALID"
            )
        if not isinstance(result, dict):
            raise ValueError("feedback disposition result must be an object")
        payload = self._dump(result)
        requested_result = json.loads(payload)

        with self._transaction() as connection:
            current = self._fetch_one(
                connection,
                """
                SELECT * FROM feedback
                WHERE run_id = ? AND external_id = ?
                """,
                (run_id, external_id),
            )
            if current is None:
                raise KeyError((run_id, external_id))
            has_completed_disposition = (
                current["disposition"] is not None
                or current["disposition_result"] is not None
            )
            if has_completed_disposition:
                stored_result = (
                    None
                    if current["disposition_result"] is None
                    else json.loads(current["disposition_result"])
                )
                if (
                    current["disposition"] != disposition
                    or stored_result != requested_result
                ):
                    raise ValueError(
                        "feedback already has a different disposition result"
                    )
                row = current
            else:
                connection.execute(
                    """
                    UPDATE feedback
                    SET disposition = ?, disposition_result = ?
                    WHERE id = ?
                    """,
                    (disposition, payload, current["id"]),
                )
                row = self._fetch_one(
                    connection,
                    "SELECT * FROM feedback WHERE id = ?",
                    (current["id"],),
                )
        decoded = self._feedback_row(row)
        if decoded is None:
            raise RuntimeError("feedback disappeared while recording disposition")
        return decoded

    def record_feedback_follow_up(
        self, run_id: int, external_id: str, issue: dict
    ) -> dict:
        self._nonempty_string(external_id, "external_id")
        if not isinstance(issue, dict):
            raise ValueError("follow-up issue must be an object")
        payload = self._dump(issue)
        requested_issue = json.loads(payload)

        with self._transaction() as connection:
            current = self._fetch_one(
                connection,
                """
                SELECT * FROM feedback
                WHERE run_id = ? AND external_id = ?
                """,
                (run_id, external_id),
            )
            if current is None:
                raise KeyError((run_id, external_id))
            if current["follow_up_issue"] is not None:
                stored_issue = json.loads(current["follow_up_issue"])
                if stored_issue != requested_issue:
                    raise ValueError(
                        "feedback already has a different follow-up issue"
                    )
                row = current
            else:
                connection.execute(
                    "UPDATE feedback SET follow_up_issue = ? WHERE id = ?",
                    (payload, current["id"]),
                )
                row = self._fetch_one(
                    connection,
                    "SELECT * FROM feedback WHERE id = ?",
                    (current["id"],),
                )
        decoded = self._feedback_row(row)
        if decoded is None:
            raise RuntimeError("feedback disappeared while recording follow-up")
        return decoded

    def mark_feedback_addressed(
        self,
        run_id: int,
        external_id: str,
        status: str,
        addressed_sha: str,
        response_url: str,
    ) -> None:
        self._nonempty_string(external_id, "external_id")
        if status not in {"RESOLVED", "ACKNOWLEDGED"}:
            raise ValueError("status must be RESOLVED or ACKNOWLEDGED")
        self._nonempty_string(addressed_sha, "addressed_sha")
        self._nonempty_string(response_url, "response_url")

        with self._transaction() as connection:
            current = self._fetch_one(
                connection,
                """
                SELECT * FROM feedback
                WHERE run_id = ? AND external_id = ?
                """,
                (run_id, external_id),
            )
            if current is None:
                raise KeyError((run_id, external_id))
            completed_result = (
                current["status"],
                current["addressed_sha"],
                current["response_url"],
            )
            requested_result = (status, addressed_sha, response_url)
            if completed_result == requested_result:
                return
            if current["status"] != "PENDING":
                raise ValueError("feedback already has a different completed result")
            connection.execute(
                """
                UPDATE feedback
                SET status = ?, addressed_sha = ?, response_url = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (status, addressed_sha, response_url, current["id"]),
            )

    def mark_feedback_without_code(
        self,
        run_id: int,
        external_id: str,
        status: str,
        response_url: str,
    ) -> None:
        self._nonempty_string(external_id, "external_id")
        if status not in {"RESOLVED", "ACKNOWLEDGED"}:
            raise ValueError("status must be RESOLVED or ACKNOWLEDGED")
        self._nonempty_string(response_url, "response_url")

        with self._transaction() as connection:
            current = self._fetch_one(
                connection,
                """
                SELECT * FROM feedback
                WHERE run_id = ? AND external_id = ?
                """,
                (run_id, external_id),
            )
            if current is None:
                raise KeyError((run_id, external_id))
            if current["disposition"] not in {"OUT_OF_SCOPE", "INVALID"}:
                raise ValueError(
                    "feedback disposition must be OUT_OF_SCOPE or INVALID"
                )
            completed_result = (
                current["status"],
                current["addressed_sha"],
                current["response_url"],
            )
            requested_result = (status, None, response_url)
            if completed_result == requested_result:
                return
            if current["status"] != "PENDING":
                raise ValueError("feedback already has a different completed result")
            connection.execute(
                """
                UPDATE feedback
                SET status = ?, addressed_sha = NULL, response_url = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (status, response_url, current["id"]),
            )

    def list_feedback(self, run_id: int) -> list[dict]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM feedback WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [self._feedback_row(row) for row in rows]

    def record_node_success(
        self, node_id: int, run_id: int, promotion_threshold: int
    ) -> dict:
        if promotion_threshold <= 0:
            raise ValueError("promotion_threshold must be positive")
        with self._transaction() as connection:
            node = self._fetch_one(
                connection, "SELECT * FROM nodes WHERE id = ?", (node_id,)
            )
            run = self._fetch_one(connection, "SELECT * FROM runs WHERE id = ?", (run_id,))
            if node is None:
                raise KeyError(node_id)
            if run is None:
                raise KeyError(run_id)
            if (
                not node["active"]
                or node["persistence"] == "PERMANENT"
                or node["repository_id"] != run["repository_id"]
            ):
                raise ValueError("node success must belong to an active dynamic node in the run repository")
            connection.execute(
                "INSERT OR IGNORE INTO node_run_usage(node_id, run_id) VALUES (?, ?)",
                (node_id, run_id),
            )
            connection.execute(
                """
                UPDATE nodes
                SET success_count = success_count + 1,
                    persistence = CASE
                        WHEN success_count + 1 >= ? THEN 'PERSISTENT'
                        ELSE persistence
                    END
                WHERE id = ?
                """,
                (promotion_threshold, node_id),
            )
            row = self._fetch_one(connection, "SELECT * FROM nodes WHERE id = ?", (node_id,))
        return self._node_row(row)

    def adapt_nodes_after_run(self, run_id: int, stale_run_threshold: int) -> list[int]:
        if stale_run_threshold <= 0:
            raise ValueError("stale_run_threshold must be positive")
        with self._transaction() as connection:
            run = self._fetch_one(connection, "SELECT * FROM runs WHERE id = ?", (run_id,))
            if run is None:
                raise KeyError(run_id)
            if run["state"] not in TERMINAL_RUN_STATES:
                raise ValueError("nodes can be adapted only after a terminal run")
            already_adapted = self._fetch_one(
                connection, "SELECT run_id FROM adapted_runs WHERE run_id = ?", (run_id,)
            )
            if already_adapted is not None:
                return []
            used_ids = {
                row["node_id"]
                for row in connection.execute(
                    "SELECT node_id FROM node_run_usage WHERE run_id = ?", (run_id,)
                ).fetchall()
            }
            nodes = connection.execute(
                """
                SELECT * FROM nodes
                WHERE repository_id = ? AND active = 1
                  AND persistence IN ('EPHEMERAL', 'PERSISTENT')
                ORDER BY id
                """,
                (run["repository_id"],),
            ).fetchall()
            removed: list[int] = []
            for node in nodes:
                if node["persistence"] == "EPHEMERAL":
                    connection.execute(
                        "UPDATE nodes SET active = 0 WHERE id = ?", (node["id"],)
                    )
                    removed.append(node["id"])
                elif node["id"] in used_ids:
                    connection.execute(
                        "UPDATE nodes SET unused_completed_runs = 0 WHERE id = ?",
                        (node["id"],),
                    )
                else:
                    unused_completed_runs = node["unused_completed_runs"] + 1
                    if unused_completed_runs >= stale_run_threshold:
                        connection.execute(
                            """
                            UPDATE nodes
                            SET unused_completed_runs = ?, active = 0
                            WHERE id = ?
                            """,
                            (unused_completed_runs, node["id"]),
                        )
                        removed.append(node["id"])
                    else:
                        connection.execute(
                            "UPDATE nodes SET unused_completed_runs = ? WHERE id = ?",
                            (unused_completed_runs, node["id"]),
                        )
            connection.execute("INSERT INTO adapted_runs(run_id) VALUES (?)", (run_id,))
            return removed
