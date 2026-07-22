from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Generator

SCHEMA_VERSION = 3

SCHEMA_V1 = r"""
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    github_node_id TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    onboarding_state TEXT NOT NULL CHECK (onboarding_state IN (
        'pending', 'inspecting', 'provisioning', 'ready', 'needs_input', 'blocked'
    )),
    blocking_reason TEXT,
    inputs_json TEXT NOT NULL DEFAULT '{}',
    current_sandbox_version_id TEXT,
    current_team_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (owner, name),
    FOREIGN KEY (current_sandbox_version_id) REFERENCES sandbox_versions(id),
    FOREIGN KEY (current_team_version_id) REFERENCES team_versions(id)
);

CREATE TABLE IF NOT EXISTS repository_inputs (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    access_mode TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (repository_id, kind, name)
);

CREATE TABLE IF NOT EXISTS sandbox_versions (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    root_path TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready' CHECK (state IN ('provisioning', 'ready', 'blocked')),
    created_at TEXT NOT NULL,
    UNIQUE (repository_id, version)
);

CREATE TABLE IF NOT EXISTS team_versions (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (repository_id, version)
);

CREATE TABLE IF NOT EXISTS team_members (
    id TEXT PRIMARY KEY,
    team_version_id TEXT NOT NULL REFERENCES team_versions(id) ON DELETE CASCADE,
    stable_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('lead', 'scout', 'implementer', 'verifier')),
    responsibilities TEXT NOT NULL,
    permitted_tools_json TEXT NOT NULL,
    runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    instructions TEXT NOT NULL,
    UNIQUE (team_version_id, stable_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_lead_per_team
    ON team_members(team_version_id)
    WHERE role = 'lead';

CREATE TABLE IF NOT EXISTS validation_commands (
    id TEXT PRIMARY KEY,
    sandbox_version_id TEXT NOT NULL REFERENCES sandbox_versions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    command_json TEXT NOT NULL,
    source TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
    UNIQUE (sandbox_version_id, position)
);

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    github_node_id TEXT NOT NULL UNIQUE,
    number INTEGER NOT NULL CHECK (number > 0),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    discussion_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (repository_id, number)
);

CREATE TABLE IF NOT EXISTS activation_events (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    issue_id TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    github_event_id TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    UNIQUE (repository_id, github_event_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id),
    issue_id TEXT NOT NULL REFERENCES issues(id),
    activation_event_id TEXT NOT NULL UNIQUE REFERENCES activation_events(id),
    sandbox_version_id TEXT NOT NULL REFERENCES sandbox_versions(id),
    team_version_id TEXT NOT NULL REFERENCES team_versions(id),
    intended_base_branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'implementing', 'validating', 'publishing',
        'waiting_for_feedback', 'resolving_feedback', 'quiet_period',
        'notified', 'blocked', 'canceled', 'closed'
    )),
    last_completed_state TEXT,
    reason TEXT,
    assignment_json TEXT,
    validated_sha TEXT,
    checkout_path TEXT,
    run_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    canceled_at TEXT,
    closed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_nonterminal_run_per_issue
    ON runs(issue_id)
    WHERE state NOT IN ('canceled', 'closed');

CREATE TABLE IF NOT EXISTS run_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_assignments (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    team_member_id TEXT NOT NULL REFERENCES team_members(id),
    reasoning TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    UNIQUE (run_id, team_member_id)
);

CREATE TABLE IF NOT EXISTS command_records (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    member_id TEXT REFERENCES team_members(id),
    kind TEXT NOT NULL,
    command_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    exit_status INTEGER,
    log_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_results (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    command_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    exit_status INTEGER NOT NULL,
    log_path TEXT NOT NULL,
    UNIQUE (run_id, commit_sha, command_json)
);

CREATE TABLE IF NOT EXISTS pull_requests (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    github_node_id TEXT UNIQUE,
    number INTEGER CHECK (number > 0),
    url TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    intended_base_branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    validated_head_sha TEXT NOT NULL,
    remote_head_sha TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'open', 'closed', 'merged')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_versions (
    id TEXT PRIMARY KEY,
    pull_request_id TEXT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    feedback_type TEXT NOT NULL CHECK (feedback_type IN ('review', 'inline_comment', 'comment')),
    github_object_id TEXT NOT NULL,
    github_version TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    path TEXT,
    line INTEGER,
    url TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'processing', 'resolved', 'declined', 'answered')),
    observed_at TEXT NOT NULL,
    processed_at TEXT,
    decision_json TEXT,
    source_sha TEXT,
    response_operation_id TEXT,
    UNIQUE (pull_request_id, feedback_type, github_object_id, github_version)
);

CREATE TABLE IF NOT EXISTS outbound_operations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'attempted', 'completed', 'reconciled', 'failed')),
    external_id TEXT,
    created_at TEXT NOT NULL,
    attempted_at TEXT,
    completed_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS application_outputs (
    id TEXT PRIMARY KEY,
    pull_request_id TEXT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    feedback_type TEXT NOT NULL,
    github_object_id TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES outbound_operations(id),
    created_at TEXT NOT NULL,
    UNIQUE (feedback_type, github_object_id)
);

CREATE TABLE IF NOT EXISTS quiet_periods (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    started_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'canceled', 'completed')),
    canceled_at TEXT,
    completed_at TEXT,
    UNIQUE (run_id, generation)
);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    quiet_period_id TEXT NOT NULL UNIQUE REFERENCES quiet_periods(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    read_at TEXT
);

INSERT OR IGNORE INTO schema_version(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""

SCHEMA_V2 = r"""
ALTER TABLE team_members
ADD COLUMN action_timeout_seconds REAL NOT NULL DEFAULT 300
    CHECK (
        typeof(action_timeout_seconds) IN ('integer', 'real')
        AND action_timeout_seconds > 0
    );
"""

SCHEMA_V3 = (
    """
    CREATE TABLE acceptance_verifications (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        commit_sha TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt > 0),
        verifier_member_id TEXT NOT NULL REFERENCES team_members(id),
        state TEXT NOT NULL CHECK (state IN (
            'verifying', 'passed', 'failed', 'blocked', 'superseded'
        )),
        claims_json TEXT NOT NULL DEFAULT '[]',
        screenshot_decision_json TEXT NOT NULL DEFAULT '{}',
        report_json TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        UNIQUE (run_id, commit_sha, attempt)
    )
    """,
    """
    CREATE INDEX acceptance_verifications_run_commit
        ON acceptance_verifications(run_id, commit_sha, attempt)
    """,
    """
    CREATE TABLE acceptance_evidence (
        id TEXT PRIMARY KEY,
        verification_id TEXT NOT NULL
            REFERENCES acceptance_verifications(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        action_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        log_path TEXT,
        UNIQUE (verification_id, sequence)
    )
    """,
    """
    CREATE TABLE acceptance_artifacts (
        id TEXT PRIMARY KEY,
        verification_id TEXT NOT NULL
            REFERENCES acceptance_verifications(id) ON DELETE CASCADE,
        claim_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('screenshot', 'trace', 'log')),
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        media_type TEXT NOT NULL,
        description TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (verification_id, path)
    )
    """,
)


class Database:
    """Owns SQLite connection policy and transactional schema initialization."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = self._open()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._open()
        try:
            existing = connection.execute("""SELECT name FROM sqlite_master
                   WHERE type='table' AND name='schema_version'""").fetchone()
            if existing is None:
                connection.executescript(SCHEMA_V1)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            ).fetchone()
            if row["version"] is None:
                raise RuntimeError("database schema version is missing")
            version = int(row["version"])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than supported schema "
                    f"{SCHEMA_VERSION}"
                )
            if version < 1:
                raise RuntimeError(f"database schema {version} is invalid")
            if version < 2:
                connection.execute(SCHEMA_V2)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           2,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 3:
                for statement in SCHEMA_V3:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           3,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            connection.commit()
            connection.execute("PRAGMA journal_mode = WAL")
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
