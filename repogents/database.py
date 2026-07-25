from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Generator

SCHEMA_VERSION = 14

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

SCHEMA_V4 = (
    """
    CREATE TABLE validation_baselines (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        validation_command_id TEXT NOT NULL
            REFERENCES validation_commands(id),
        command_json TEXT NOT NULL,
        base_sha TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('strict', 'delta')),
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        exit_status INTEGER NOT NULL,
        log_path TEXT NOT NULL,
        findings_json TEXT NOT NULL,
        UNIQUE (run_id, validation_command_id)
    )
    """,
    """
    ALTER TABLE validation_results
    ADD COLUMN validation_command_id TEXT REFERENCES validation_commands(id)
    """,
    """
    ALTER TABLE validation_results
    ADD COLUMN verdict TEXT CHECK (verdict IN ('pass', 'fail'))
    """,
    """
    ALTER TABLE validation_results
    ADD COLUMN findings_json TEXT
    """,
    """
    ALTER TABLE validation_results
    ADD COLUMN comparison_json TEXT
    """,
    """
    UPDATE validation_results
       SET validation_command_id = (
           SELECT validation_commands.id
             FROM validation_commands
             JOIN runs
               ON runs.sandbox_version_id =
                  validation_commands.sandbox_version_id
            WHERE runs.id = validation_results.run_id
              AND validation_commands.command_json =
                  validation_results.command_json
            LIMIT 1
       )
     WHERE validation_command_id IS NULL
    """,
    """
    UPDATE validation_results
       SET verdict = CASE
           WHEN exit_status = 0 THEN 'pass'
           ELSE 'fail'
       END
     WHERE verdict IS NULL
    """,
)

SCHEMA_V5 = (
    """
    ALTER TABLE repositories
    ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1))
    """,
    """
    ALTER TABLE repositories
    ADD COLUMN removed_at TEXT
    """,
    """
    CREATE INDEX active_repositories
        ON repositories(enabled, onboarding_state, created_at)
        WHERE removed_at IS NULL
    """,
)

SCHEMA_V6 = (
    """
    ALTER TABLE team_members
    ADD COLUMN atomic_role TEXT NOT NULL DEFAULT ''
    """,
    """
    UPDATE team_members
       SET atomic_role = role
     WHERE atomic_role = ''
    """,
)
SCHEMA_V7 = (
    """
    ALTER TABLE team_versions
    ADD COLUMN design_contract_version INTEGER NOT NULL DEFAULT 1
        CHECK (design_contract_version IN (1, 2))
    """,
    """
    CREATE UNIQUE INDEX one_independent_verifier_per_team
        ON team_members(team_version_id)
        WHERE role = 'verifier'
    """,
)
SCHEMA_V8 = (
    """
    ALTER TABLE feedback_versions RENAME TO feedback_versions_v7
    """,
    """
    CREATE TABLE feedback_versions (
        id TEXT PRIMARY KEY,
        pull_request_id TEXT NOT NULL
            REFERENCES pull_requests(id) ON DELETE CASCADE,
        feedback_type TEXT NOT NULL CHECK (
            feedback_type IN (
                'review', 'inline_comment', 'comment', 'base_conflict'
            )
        ),
        github_object_id TEXT NOT NULL,
        github_version TEXT NOT NULL,
        author TEXT NOT NULL,
        body TEXT NOT NULL,
        path TEXT,
        line INTEGER,
        url TEXT,
        state TEXT NOT NULL CHECK (
            state IN (
                'pending', 'processing', 'resolved', 'declined', 'answered'
            )
        ),
        observed_at TEXT NOT NULL,
        processed_at TEXT,
        decision_json TEXT,
        source_sha TEXT,
        response_operation_id TEXT,
        UNIQUE (
            pull_request_id,
            feedback_type,
            github_object_id,
            github_version
        )
    )
    """,
    """
    INSERT INTO feedback_versions
        (id, pull_request_id, feedback_type, github_object_id,
         github_version, author, body, path, line, url, state, observed_at,
         processed_at, decision_json, source_sha, response_operation_id)
    SELECT id, pull_request_id, feedback_type, github_object_id,
           github_version, author, body, path, line, url, state, observed_at,
           processed_at, decision_json, source_sha, response_operation_id
      FROM feedback_versions_v7
    """,
    """
    DROP TABLE feedback_versions_v7
    """,
)
SCHEMA_V9 = (
    """
    ALTER TABLE runs
    ADD COLUMN priority INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(priority) = 'integer' AND priority >= 0)
    """,
    """
    ALTER TABLE runs
    ADD COLUMN force_requested_at TEXT
    """,
    """
    UPDATE runs AS target
       SET priority = (
           SELECT COUNT(*)
             FROM runs AS preceding
            WHERE preceding.created_at < target.created_at
               OR (
                   preceding.created_at = target.created_at
                   AND preceding.id < target.id
               )
       )
    """,
    """
    CREATE INDEX run_queue_order
        ON runs(priority, created_at, id)
    """,
    """
    CREATE UNIQUE INDEX one_forced_run
        ON runs((1))
        WHERE force_requested_at IS NOT NULL
    """,
)
SCHEMA_V10 = (
    """
    DROP INDEX one_forced_run
    """,
    """
    CREATE UNIQUE INDEX one_forced_run_per_repository
        ON runs(repository_id)
        WHERE force_requested_at IS NOT NULL
    """,
)
SCHEMA_V11 = (
    """
    CREATE TABLE issue_versions (
        id TEXT PRIMARY KEY,
        issue_id TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        version INTEGER NOT NULL CHECK (version > 0),
        previous_version_id TEXT REFERENCES issue_versions(id),
        github_updated_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        discussion_json TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        UNIQUE (issue_id, version)
    )
    """,
    """
    ALTER TABLE issues
    ADD COLUMN current_version_id TEXT REFERENCES issue_versions(id)
    """,
    """
    ALTER TABLE activation_events
    ADD COLUMN issue_version_id TEXT REFERENCES issue_versions(id)
    """,
    """
    ALTER TABLE runs
    ADD COLUMN validated_issue_version_id TEXT REFERENCES issue_versions(id)
    """,
    """
    ALTER TABLE pull_requests
    ADD COLUMN validated_issue_version_id TEXT REFERENCES issue_versions(id)
    """,
    """
    ALTER TABLE acceptance_verifications
    ADD COLUMN issue_version_id TEXT REFERENCES issue_versions(id)
    """,
    """
    INSERT INTO issue_versions
        (id, issue_id, version, previous_version_id, github_updated_at,
         content_sha256, title, body, discussion_json, observed_at)
    SELECT 'issue-version:' || issues.id || ':1',
           issues.id,
           1,
           NULL,
           issues.updated_at,
           lower(hex(zeroblob(32))),
           issues.title,
           issues.body,
           issues.discussion_json,
           issues.updated_at
      FROM issues
    """,
    """
    UPDATE issues
       SET current_version_id = 'issue-version:' || issues.id || ':1'
    """,
    """
    UPDATE activation_events
       SET issue_version_id = (
           SELECT issues.current_version_id
             FROM issues
            WHERE issues.id = activation_events.issue_id
       )
    """,
    """
    UPDATE runs
       SET validated_issue_version_id = (
           SELECT activation_events.issue_version_id
             FROM activation_events
            WHERE activation_events.id = runs.activation_event_id
       )
     WHERE validated_sha IS NOT NULL
    """,
    """
    UPDATE pull_requests
       SET validated_issue_version_id = (
           SELECT runs.validated_issue_version_id
             FROM runs
            WHERE runs.id = pull_requests.run_id
       )
    """,
    """
    UPDATE acceptance_verifications
       SET issue_version_id = (
           SELECT COALESCE(
               runs.validated_issue_version_id,
               activation_events.issue_version_id
           )
             FROM runs
             JOIN activation_events
               ON activation_events.id = runs.activation_event_id
            WHERE runs.id = acceptance_verifications.run_id
       )
    """,
    """
    CREATE INDEX issue_versions_history
        ON issue_versions(issue_id, version)
    """,
    """
    CREATE INDEX acceptance_verifications_issue_revision
        ON acceptance_verifications(
            run_id, commit_sha, issue_version_id, attempt
        )
    """,
)
SCHEMA_V12 = (
    """
    INSERT INTO run_transitions
        (run_id, from_state, to_state, reason, occurred_at)
    SELECT runs.id,
           runs.state,
           'implementing',
           'legacy issue snapshot was not immutable; re-evaluating current requirements and proof',
           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
      FROM runs
     WHERE runs.state NOT IN ('queued', 'implementing', 'canceled', 'closed')
       AND EXISTS (
           SELECT 1
             FROM acceptance_verifications
             JOIN issue_versions
               ON issue_versions.id =
                  acceptance_verifications.issue_version_id
            WHERE acceptance_verifications.run_id = runs.id
              AND issue_versions.content_sha256 = lower(hex(zeroblob(32)))
              AND CASE
                    WHEN json_valid(
                        acceptance_verifications.report_json
                    )
                    THEN json_type(
                        acceptance_verifications.report_json,
                        '$.issue_version_id'
                    ) IS NULL
                    ELSE 1
                  END
       )
    """,
    """
    UPDATE quiet_periods
       SET state='canceled',
           canceled_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE state='active'
       AND run_id IN (
           SELECT runs.id
             FROM runs
            WHERE runs.state NOT IN ('canceled', 'closed')
              AND EXISTS (
                  SELECT 1
                    FROM acceptance_verifications
                    JOIN issue_versions
                      ON issue_versions.id =
                         acceptance_verifications.issue_version_id
                   WHERE acceptance_verifications.run_id = runs.id
                     AND issue_versions.content_sha256 =
                         lower(hex(zeroblob(32)))
                     AND CASE
                           WHEN json_valid(
                               acceptance_verifications.report_json
                           )
                           THEN json_type(
                               acceptance_verifications.report_json,
                               '$.issue_version_id'
                           ) IS NULL
                           ELSE 1
                         END
              )
       )
    """,
    """
    UPDATE runs
       SET state = CASE
               WHEN state='queued' THEN 'queued'
               ELSE 'implementing'
           END,
           last_completed_state = CASE
               WHEN state IN ('queued', 'implementing', 'blocked')
               THEN last_completed_state
               ELSE state
           END,
           reason =
               'legacy issue snapshot was not immutable; re-evaluating current requirements and proof',
           updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE state NOT IN ('canceled', 'closed')
       AND EXISTS (
           SELECT 1
             FROM acceptance_verifications
             JOIN issue_versions
               ON issue_versions.id =
                  acceptance_verifications.issue_version_id
            WHERE acceptance_verifications.run_id = runs.id
              AND issue_versions.content_sha256 = lower(hex(zeroblob(32)))
              AND CASE
                    WHEN json_valid(
                        acceptance_verifications.report_json
                    )
                    THEN json_type(
                        acceptance_verifications.report_json,
                        '$.issue_version_id'
                    ) IS NULL
                    ELSE 1
                  END
       )
    """,
    """
    UPDATE acceptance_verifications
       SET state='superseded',
           completed_at=COALESCE(
               completed_at,
               strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
           )
     WHERE EXISTS (
           SELECT 1
             FROM runs
             JOIN issue_versions
               ON issue_versions.id =
                  acceptance_verifications.issue_version_id
            WHERE runs.id = acceptance_verifications.run_id
              AND runs.state NOT IN ('canceled', 'closed')
              AND issue_versions.content_sha256 = lower(hex(zeroblob(32)))
              AND CASE
                    WHEN json_valid(
                        acceptance_verifications.report_json
                    )
                    THEN json_type(
                        acceptance_verifications.report_json,
                        '$.issue_version_id'
                    ) IS NULL
                    ELSE 1
                  END
       )
    """,
)
SCHEMA_V13 = (
    """
    ALTER TABLE activation_events
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'ready_label'
        CHECK (kind IN ('ready_label', 'closed_pr_restart'))
    """,
    """
    INSERT INTO run_transitions
        (run_id, from_state, to_state, reason, occurred_at)
    SELECT id,
           state,
           'waiting_for_feedback',
           'quiet deadline removed; monitoring continues until pull request closure',
           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
      FROM runs
     WHERE state IN ('quiet_period', 'notified')
    """,
    """
    UPDATE quiet_periods
       SET state='canceled',
           canceled_at=COALESCE(
               canceled_at,
               strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
           )
     WHERE state='active'
    """,
    """
    UPDATE runs
       SET state='waiting_for_feedback',
           last_completed_state='waiting_for_feedback',
           reason=NULL,
           updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE state IN ('quiet_period', 'notified')
    """,
)
SCHEMA_V14 = (
    """
    ALTER TABLE feedback_versions
    ADD COLUMN review_thread_id TEXT
        CHECK (review_thread_id IS NULL OR length(review_thread_id) > 0)
    """,
    """
    ALTER TABLE feedback_versions
    ADD COLUMN review_thread_resolved INTEGER
        CHECK (
            (review_thread_id IS NULL AND review_thread_resolved IS NULL)
            OR (
                review_thread_id IS NOT NULL
                AND review_thread_resolved IN (0, 1)
            )
        )
    """,
    """
    CREATE INDEX feedback_versions_review_threads
        ON feedback_versions(
            pull_request_id,
            review_thread_id,
            review_thread_resolved
        )
        WHERE review_thread_id IS NOT NULL
    """,
)



class Database:
    """Owns SQLite connection policy and transactional schema initialization."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._activity_condition = threading.Condition()
        self._activity_revision = 0

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
            initial_changes = connection.total_changes
            yield connection
            changed = connection.total_changes != initial_changes
            connection.commit()
            if changed:
                self.notify_activity_change()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @property
    def activity_revision(self) -> int:
        with self._activity_condition:
            return self._activity_revision

    def notify_activity_change(self) -> None:
        with self._activity_condition:
            self._activity_revision += 1
            self._activity_condition.notify_all()

    def wait_for_activity_change(self, revision: int, timeout: float) -> int:
        if timeout < 0:
            raise ValueError("activity wait timeout cannot be negative")
        with self._activity_condition:
            self._activity_condition.wait_for(
                lambda: self._activity_revision != revision,
                timeout=timeout,
            )
            return self._activity_revision

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
            if version < 4:
                for statement in SCHEMA_V4:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           4,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 5:
                for statement in SCHEMA_V5:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           5,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 6:
                for statement in SCHEMA_V6:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           6,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 7:
                for statement in SCHEMA_V7:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           7,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 8:
                for statement in SCHEMA_V8:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           8,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 9:
                for statement in SCHEMA_V9:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           9,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 10:
                for statement in SCHEMA_V10:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           10,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 11:
                for statement in SCHEMA_V11:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           11,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 12:
                for statement in SCHEMA_V12:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           12,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 13:
                for statement in SCHEMA_V13:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           13,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       )""")
            if version < 14:
                for statement in SCHEMA_V14:
                    connection.execute(statement)
                connection.execute("""INSERT INTO schema_version(version, applied_at)
                       VALUES (
                           14,
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
