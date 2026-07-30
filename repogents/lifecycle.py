from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Protocol

from .controller import RunPaused, RunProcessSupervisor, git_environment
from .database import Database
from .github import ActivationEvent, GitHubError, IssueInfo, PullRequestInfo
from .sandbox import RunLayout


class RunState(str, Enum):
    QUEUED = "queued"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    PUBLISHING = "publishing"
    WAITING_FOR_FEEDBACK = "waiting_for_feedback"
    RESOLVING_FEEDBACK = "resolving_feedback"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    CLOSED = "closed"

ACTIVE_RUN_STATES = frozenset(
    {
        RunState.IMPLEMENTING,
        RunState.VALIDATING,
        RunState.PUBLISHING,
        RunState.RESOLVING_FEEDBACK,
    }
)


_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset(
        {RunState.IMPLEMENTING, RunState.BLOCKED, RunState.CANCELED}
    ),
    RunState.IMPLEMENTING: frozenset(
        {RunState.VALIDATING, RunState.BLOCKED, RunState.CANCELED}
    ),
    RunState.VALIDATING: frozenset(
        {
            RunState.IMPLEMENTING,
            RunState.PUBLISHING,
            RunState.BLOCKED,
            RunState.CANCELED,
        }
    ),
    RunState.PUBLISHING: frozenset(
        {
            RunState.IMPLEMENTING,
            RunState.WAITING_FOR_FEEDBACK,
            RunState.BLOCKED,
            RunState.CANCELED,
            RunState.CLOSED,
        }
    ),
    RunState.WAITING_FOR_FEEDBACK: frozenset(
        {
            RunState.RESOLVING_FEEDBACK,
            RunState.BLOCKED,
            RunState.CANCELED,
            RunState.CLOSED,
        }
    ),
    RunState.RESOLVING_FEEDBACK: frozenset(
        {
            RunState.VALIDATING,
            RunState.WAITING_FOR_FEEDBACK,
            RunState.BLOCKED,
            RunState.CANCELED,
            RunState.CLOSED,
        }
    ),
    RunState.BLOCKED: frozenset({RunState.CANCELED, RunState.CLOSED}),
    RunState.CANCELED: frozenset(),
    RunState.CLOSED: frozenset(),
}
_LEGACY_ACCEPTANCE_RECOVERY_REASON = (
    "automatic acceptance recheck for legacy unchallenged visual blocker"
)
_LEGACY_FEEDBACK_TEAM_EXPANSION_RECOVERY_REASON = (
    "automatic feedback conflict retry after stored-team expansion became available"
)
_LEGACY_FEEDBACK_MEMBER_HANDOFF_RECOVERY_REASON = (
    "automatic feedback conflict retry with assigned-member handoff available"
)
FEEDBACK_VALIDATION_BASE_RECOVERY_REASON = (
    "automatic feedback validation retry against prepared base"
)
FEEDBACK_VALIDATION_REPLAY_RECOVERY_REASON = (
    "automatic feedback validation retry against prepared base "
    "without agent replay"
)
FEEDBACK_VALIDATION_RECOVERY_REASONS = frozenset(
    {
        FEEDBACK_VALIDATION_BASE_RECOVERY_REASON,
        FEEDBACK_VALIDATION_REPLAY_RECOVERY_REASON,
    }
)
_LEGACY_PUBLICATION_MERGE_CONFLICT_BLOCKER = (
    "publication blocked: validated commit has a merge conflict with the "
    "current intended-base head"
)
_LEGACY_PUBLICATION_MERGE_BASE_RECOVERY_REASON = (
    "automatic publication retry with corrected candidate/current merge base"
)
_LEGACY_VISUAL_BLOCK_TERMS = (
    "browser",
    "capture",
    "client",
    "dashboard",
    "endpoint",
    "loading",
    "port",
    "scenario",
    "socket",
)


def allowed_transition(current: RunState, target: RunState) -> bool:
    return target in _TRANSITIONS[current]


class ActivationClient(Protocol):
    def list_ready_events(self, owner: str, name: str) -> list[ActivationEvent]: ...

    def get_issue(self, owner: str, name: str, number: int) -> IssueInfo: ...

    def get_branch_head(self, owner: str, name: str, branch: str) -> str: ...


class CheckoutManager(Protocol):
    def create(self, source: Path, base_sha: str, destination: Path) -> None: ...


class CancellableSandbox(Protocol):
    def cancel(self, run_id: str) -> bool: ...


class GitCheckoutManager:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with git_environment(self.token) as environment:
            marker = destination / ".git" / "repogents-base"
            if (destination / ".git").is_dir():
                retained = subprocess.run(
                    [
                        "git",
                        "rev-parse",
                        "--verify",
                        f"refs/repogents/bases/{base_sha}^{{commit}}",
                    ],
                    cwd=destination,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=False,
                    env=environment,
                )
                if (
                    marker.is_file()
                    and marker.read_text(encoding="utf-8").strip() == base_sha
                    and retained.returncode == 0
                    and retained.stdout.strip() == base_sha
                ):
                    return
                shutil.rmtree(destination)
            if destination.exists() and any(destination.iterdir()):
                raise RuntimeError(
                    f"cannot reconstruct nonempty checkout without Git metadata: {destination}"
                )
            if destination.exists():
                destination.rmdir()
            source_origin = subprocess.run(
                ["git", "-C", str(source), "remote", "get-url", "origin"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
                env=environment,
            )
            origin_url = (
                source_origin.stdout.strip() if source_origin.returncode == 0 else None
            )
            clone = subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    "--",
                    str(source),
                    str(destination),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
                env=environment,
            )
            if clone.returncode != 0:
                shutil.rmtree(destination, ignore_errors=True)
                raise RuntimeError(
                    f"isolated checkout clone failed: {clone.stderr.strip()}"
                )
            try:
                if origin_url:
                    remote = subprocess.run(
                        ["git", "remote", "set-url", "origin", origin_url],
                        cwd=destination,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=60,
                        check=False,
                        env=environment,
                    )
                    if remote.returncode != 0:
                        raise RuntimeError(
                            f"cannot retain GitHub origin for run checkout: {remote.stderr.strip()}"
                        )
                present = subprocess.run(
                    ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"],
                    cwd=destination,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=False,
                    env=environment,
                )
                if present.returncode != 0:
                    if not origin_url:
                        raise RuntimeError(
                            f"cannot fetch stored base SHA {base_sha}: source has no GitHub origin"
                        )
                    fetch = subprocess.run(
                        [
                            "git",
                            "fetch",
                            "--quiet",
                            "--no-tags",
                            "--force",
                            "origin",
                            base_sha,
                        ],
                        cwd=destination,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=300,
                        check=False,
                        env=environment,
                    )
                    if fetch.returncode != 0:
                        raise RuntimeError(
                            f"cannot fetch stored base SHA {base_sha} from GitHub origin: "
                            f"{fetch.stderr.strip()}"
                        )
                retain = subprocess.run(
                    ["git", "update-ref", f"refs/repogents/bases/{base_sha}", base_sha],
                    cwd=destination,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=False,
                    env=environment,
                )
                if retain.returncode != 0:
                    raise RuntimeError(
                        f"cannot retain stored base SHA {base_sha}: {retain.stderr.strip()}"
                    )
                checkout = subprocess.run(
                    ["git", "checkout", "--quiet", "--detach", base_sha],
                    cwd=destination,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=False,
                    env=environment,
                )
                if checkout.returncode != 0:
                    raise RuntimeError(
                        f"cannot checkout stored base SHA {base_sha}: {checkout.stderr.strip()}"
                    )
                marker.write_text(f"{base_sha}\n", encoding="utf-8")
            except Exception:
                shutil.rmtree(destination, ignore_errors=True)
                raise


class RunLifecycle:
    def __init__(
        self,
        *,
        database: Database,
        data_root: Path,
        github: ActivationClient,
        checkouts: CheckoutManager,
        sandbox: CancellableSandbox,
        processes: RunProcessSupervisor | None = None,
    ) -> None:
        self.database = database
        self.data_root = Path(data_root).resolve()
        self.github = github
        self.checkouts = checkouts
        self.sandbox = sandbox
        self.processes = processes
        self._run_locks_guard = threading.Lock()
        self._run_locks: dict[str, threading.RLock] = {}

    def poll_repository(self, repository_id: str) -> tuple[str, ...]:
        with self.database.connect() as connection:
            repository = connection.execute(
                "SELECT * FROM repositories WHERE id=?", (repository_id,)
            ).fetchone()
        if repository is None:
            raise KeyError(repository_id)
        if repository["onboarding_state"] != "ready":
            self._record_ready_issue_inactive(
                repository_id, str(repository["onboarding_state"])
            )
            return ()
        if not bool(repository["enabled"]) or repository["removed_at"] is not None:
            return ()
        sandbox_version_id = repository["current_sandbox_version_id"]
        team_version_id = repository["current_team_version_id"]
        if not sandbox_version_id or not team_version_id:
            raise RuntimeError("ready repository has no stored sandbox or team version")
        list_ready_issues = getattr(self.github, "list_ready_issues", None)
        if callable(list_ready_issues):
            try:
                ready_issues = list_ready_issues(
                    str(repository["owner"]), str(repository["name"])
                )
            except GitHubError:
                self._record_ready_issue_failure(repository_id)
            else:
                self._record_ready_issue_success(
                    repository_id, int(repository["ready_issue_generation"]), ready_issues
                )
        events = self.github.list_ready_events(
            str(repository["owner"]), str(repository["name"])
        )
        created: list[str] = []
        for event in events:
            run_id = self._activate(repository, event)
            if run_id is not None:
                created.append(run_id)
        return tuple(created)

    def _record_ready_issue_success(
        self, repository_id: str, ready_issue_generation: int, issues: list[IssueInfo]
    ) -> None:
        values = [
            {
                "repository_id": repository_id,
                "number": issue.number,
                "title": issue.title,
                "url": issue.url,
                "updated_at": issue.updated_at,
            }
            for issue in issues
        ]
        with self.database.transaction() as connection:
            eligible = connection.execute(
                """SELECT 1 FROM repositories
                   WHERE id=?
                     AND enabled=1
                     AND removed_at IS NULL
                     AND onboarding_state='ready'
                     AND ready_issue_generation=?""",
                (repository_id, ready_issue_generation),
            ).fetchone()
            if eligible is None:
                return
            connection.execute(
                """INSERT INTO ready_issue_discovery
                   (repository_id, status, issues_json, last_success_at,
                    last_attempt_at, error)
                   VALUES (?, 'available', ?,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL)
                   ON CONFLICT(repository_id) DO UPDATE SET
                     status='available',
                     issues_json=excluded.issues_json,
                     last_success_at=excluded.last_success_at,
                     last_attempt_at=excluded.last_attempt_at,
                     error=NULL""",
                (repository_id, json.dumps(values, sort_keys=True)),
            )

    def _record_ready_issue_failure(self, repository_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO ready_issue_discovery
                   (repository_id, status, issues_json, last_success_at,
                    last_attempt_at, error)
                   VALUES (?, 'unavailable', '[]', NULL,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                           'GitHub ready-issue discovery failed')
                   ON CONFLICT(repository_id) DO UPDATE SET
                     status=CASE
                       WHEN ready_issue_discovery.last_success_at IS NULL
                         THEN 'unavailable'
                       ELSE 'stale'
                     END,
                     last_attempt_at=excluded.last_attempt_at,
                     error=excluded.error""",
                (repository_id,),
            )

    def _record_ready_issue_inactive(
        self, repository_id: str, onboarding_state: str
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE ready_issue_discovery
                   SET status=CASE
                         WHEN last_success_at IS NULL THEN 'unavailable'
                         ELSE 'stale'
                       END,
                       last_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                       error=?
                   WHERE repository_id=?""",
                (f"Repository onboarding is {onboarding_state}", repository_id),
            )

    def _store_issue_version(
        self,
        connection: sqlite3.Connection,
        repository_id: str,
        proposed_issue_id: str,
        issue: IssueInfo,
    ) -> tuple[str, str, bool]:
        stored = connection.execute(
            """SELECT * FROM issues
               WHERE repository_id=? AND number=?""",
            (repository_id, issue.number),
        ).fetchone()
        discussion_json = _json(issue.discussion)
        if stored is None:
            issue_id = proposed_issue_id
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    issue_id,
                    repository_id,
                    issue.node_id,
                    issue.number,
                    issue.url,
                    issue.title,
                    issue.body,
                    discussion_json,
                    issue.updated_at,
                ),
            )
            current = None
        else:
            issue_id = str(stored["id"])
            current = None
            if stored["current_version_id"] is not None:
                current = connection.execute(
                    "SELECT * FROM issue_versions WHERE id=?",
                    (stored["current_version_id"],),
                ).fetchone()
            if current is None:
                current = connection.execute(
                    """SELECT * FROM issue_versions
                       WHERE issue_id=? ORDER BY version DESC LIMIT 1""",
                    (issue_id,),
                ).fetchone()

        if current is not None:
            github_updated_at = str(current["github_updated_at"])
            if issue.updated_at < github_updated_at:
                return issue_id, str(current["id"]), False
            unchanged = (
                str(current["title"]) == issue.title
                and str(current["body"]) == issue.body
                and str(current["discussion_json"]) == discussion_json
            )
            if unchanged:
                connection.execute(
                    """UPDATE issues
                       SET github_node_id=?, url=?, updated_at=?,
                           current_version_id=?
                       WHERE id=?""",
                    (
                        issue.node_id,
                        issue.url,
                        issue.updated_at,
                        current["id"],
                        issue_id,
                    ),
                )
                return issue_id, str(current["id"]), False

        content_sha256 = _issue_content_sha(issue)
        version = int(current["version"]) + 1 if current is not None else 1
        previous_version_id = str(current["id"]) if current is not None else None
        version_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{issue_id}:issue-version:{version}:{content_sha256}",
            )
        )
        observed_at = _utc_now()
        connection.execute(
            """INSERT INTO issue_versions
               (id, issue_id, version, previous_version_id,
                github_updated_at, content_sha256, title, body,
                discussion_json, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id,
                issue_id,
                version,
                previous_version_id,
                issue.updated_at,
                content_sha256,
                issue.title,
                issue.body,
                discussion_json,
                observed_at,
            ),
        )
        connection.execute(
            """UPDATE issues
               SET github_node_id=?, url=?, title=?, body=?,
                   discussion_json=?, updated_at=?, current_version_id=?
               WHERE id=?""",
            (
                issue.node_id,
                issue.url,
                issue.title,
                issue.body,
                discussion_json,
                issue.updated_at,
                version_id,
                issue_id,
            ),
        )
        return issue_id, version_id, True

    def current_issue_version(self, run_id: str) -> str:
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT runs.repository_id, issues.id, issues.github_node_id,
                          issues.number, issues.url, issues.title, issues.body,
                          issues.discussion_json, issues.updated_at
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            issue = IssueInfo(
                node_id=str(row["github_node_id"]),
                number=int(row["number"]),
                url=str(row["url"]),
                title=str(row["title"]),
                body=str(row["body"]),
                discussion=tuple(json.loads(str(row["discussion_json"]))),
                updated_at=str(row["updated_at"]),
            )
            _, version_id, _ = self._store_issue_version(
                connection,
                str(row["repository_id"]),
                str(row["id"]),
                issue,
            )
            return version_id

    def poll_issue_revision(self, run_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT runs.state, issues.number,
                          repositories.owner, repositories.name
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        if RunState(str(row["state"])) in {RunState.CANCELED, RunState.CLOSED}:
            return False
        issue = self.github.get_issue(
            str(row["owner"]),
            str(row["name"]),
            int(row["number"]),
        )

        now = _utc_now()
        with self.database.transaction() as connection:
            current = connection.execute(
                """SELECT runs.*, issues.id AS stored_issue_id,
                          issues.current_version_id
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
            if current is None:
                raise KeyError(run_id)
            state = RunState(str(current["state"]))
            if state in {RunState.CANCELED, RunState.CLOSED}:
                return False
            _, issue_version_id, changed = self._store_issue_version(
                connection,
                str(current["repository_id"]),
                str(current["stored_issue_id"]),
                issue,
            )
            if not changed:
                return False
            version = connection.execute(
                "SELECT version FROM issue_versions WHERE id=?",
                (issue_version_id,),
            ).fetchone()[0]
            reason = (
                f"issue requirements changed to revision {version}; "
                "re-evaluating implementation and proof"
            )
            if state == RunState.QUEUED and current["resume_state"] is None:
                connection.execute(
                    "UPDATE runs SET reason=?, updated_at=? WHERE id=?",
                    (reason, now, run_id),
                )
                connection.execute(
                    """INSERT INTO run_transitions
                       (run_id, from_state, to_state, reason, occurred_at)
                       VALUES (?, 'queued', 'queued', ?, ?)""",
                    (run_id, reason, now),
                )
            else:
                self.request_repository_lane(
                    connection,
                    run_id,
                    RunState.IMPLEMENTING,
                    reason=reason,
                    allow_restart=True,
                )
            connection.execute(
                """UPDATE acceptance_verifications
                   SET state='superseded',
                       completed_at=COALESCE(completed_at, ?)
                   WHERE run_id=?
                     AND issue_version_id IS NOT ?
                     AND state != 'superseded'""",
                (now, run_id, issue_version_id),
            )

        if self.processes is not None:
            self.processes.pause(run_id)
        cancellation_error: Exception | None = None
        try:
            try:
                self.sandbox.cancel(run_id)
            except Exception as error:
                cancellation_error = error
            with self._run_lock(run_id):
                pass
        finally:
            if self.processes is not None:
                self.processes.resume(run_id)
        if cancellation_error is not None:
            raise RuntimeError(
                "issue revision persisted, but active sandbox cancellation failed"
            ) from cancellation_error
        return True

    def request_repository_lane(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        target: RunState,
        *,
        reason: str | None = None,
        allow_restart: bool = False,
    ) -> bool:
        """Activate a run or durably queue its exact requested phase."""

        if target not in ACTIVE_RUN_STATES:
            raise ValueError(f"{target.value} is not an active run state")
        row = connection.execute(
            """SELECT runs.*,
                      repositories.enabled AS repository_enabled,
                      repositories.removed_at AS repository_removed_at
               FROM runs
               JOIN repositories ON repositories.id=runs.repository_id
               WHERE runs.id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        current = RunState(str(row["state"]))
        if (
            not bool(row["repository_enabled"])
            or row["repository_removed_at"] is not None
        ):
            raise RunPaused(run_id)
        if current in {RunState.CANCELED, RunState.CLOSED}:
            raise ValueError(f"{current.value} run cannot request the repository lane")

        sibling = connection.execute(
            """SELECT id FROM runs
               WHERE repository_id=? AND id!=?
                 AND state IN (
                     'implementing',
                     'validating',
                     'publishing',
                     'resolving_feedback'
                 )
               LIMIT 1""",
            (row["repository_id"], run_id),
        ).fetchone()
        now = _utc_now()
        if sibling is not None:
            if current == RunState.QUEUED:
                connection.execute(
                    """UPDATE runs
                       SET resume_state=?, reason=?, updated_at=?
                       WHERE id=?""",
                    (target.value, reason, now, run_id),
                )
            else:
                last_completed = (
                    row["last_completed_state"]
                    if current == RunState.BLOCKED
                    else current.value
                )
                connection.execute(
                    """UPDATE runs
                       SET state='queued', resume_state=?,
                           last_completed_state=?, reason=?, updated_at=?
                       WHERE id=?""",
                    (target.value, last_completed, reason, now, run_id),
                )
                connection.execute(
                    """INSERT INTO run_transitions
                       (run_id, from_state, to_state, reason, occurred_at)
                       VALUES (?, ?, 'queued', ?, ?)""",
                    (run_id, current.value, reason, now),
                )
            return False

        if current == target:
            if not allow_restart:
                raise ValueError(
                    f"invalid run transition: {current.value} -> {target.value}"
                )
            connection.execute(
                """UPDATE runs
                   SET resume_state=NULL, reason=?, updated_at=?
                   WHERE id=?""",
                (reason, now, run_id),
            )
            connection.execute(
                """INSERT INTO run_transitions
                   (run_id, from_state, to_state, reason, occurred_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, current.value, target.value, reason, now),
            )
            return True
        resumable = (
            current == RunState.QUEUED
            and str(row["resume_state"] or "") == target.value
        )
        if not resumable and not allow_restart and not allowed_transition(
            current, target
        ):
            raise ValueError(
                f"invalid run transition: {current.value} -> {target.value}"
            )
        if current in {RunState.BLOCKED, RunState.QUEUED}:
            last_completed = row["last_completed_state"]
        else:
            last_completed = current.value
        connection.execute(
            """UPDATE runs
               SET state=?, resume_state=NULL, last_completed_state=?,
                   reason=?, updated_at=?
               WHERE id=?""",
            (target.value, last_completed, reason, now, run_id),
        )
        connection.execute(
            """INSERT INTO run_transitions
               (run_id, from_state, to_state, reason, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, current.value, target.value, reason, now),
        )
        return True

    def suspend_for_preemption(self, run_id: str) -> str:
        """Yield an active repository lane without losing its exact phase."""

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT state, resume_state FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = RunState(str(row["state"]))
            if current == RunState.QUEUED and row["resume_state"] is not None:
                return str(row["resume_state"])
            if current not in ACTIVE_RUN_STATES:
                raise ValueError(f"{current.value} run does not own a repository lane")
            now = _utc_now()
            reason = "repository lane yielded to a forced sibling run"
            connection.execute(
                """UPDATE runs
                   SET state='queued', resume_state=?, updated_at=?
                   WHERE id=?""",
                (current.value, now, run_id),
            )
            connection.execute(
                """INSERT INTO run_transitions
                   (run_id, from_state, to_state, reason, occurred_at)
                   VALUES (?, ?, 'queued', ?, ?)""",
                (run_id, current.value, reason, now),
            )
            return current.value

    def resume_suspended(self, run_id: str) -> str | None:
        """Reclaim the repository lane for a durably suspended phase."""

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT resume_state, reason FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["resume_state"] is None:
                return None
            target = RunState(str(row["resume_state"]))
            active = self.request_repository_lane(
                connection,
                run_id,
                target,
                reason=row["reason"],
                allow_restart=True,
            )
            return target.value if active else None

    def activate_queued(self, run_id: str) -> bool:
        """Atomically claim an idle repository lane for queued work."""

        with self.database.transaction() as connection:
            return self.request_repository_lane(
                connection,
                run_id,
                RunState.IMPLEMENTING,
            )

    def record_validated_revision(
        self,
        run_id: str,
        commit_sha: str,
        issue_version_id: str,
    ) -> bool:
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT runs.state, runs.reason, issues.current_version_id
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if (
                row["state"] != RunState.VALIDATING.value
                or row["current_version_id"] != issue_version_id
            ):
                return False
            recovery_reason = (
                str(row["reason"])
                if row["reason"] in FEEDBACK_VALIDATION_RECOVERY_REASONS
                else None
            )
            now = _utc_now()
            connection.execute(
                """UPDATE runs
                   SET state='publishing',
                       last_completed_state='validating',
                       reason=?,
                       validated_sha=?,
                       validated_issue_version_id=?,
                       updated_at=?
                   WHERE id=?""",
                (recovery_reason, commit_sha, issue_version_id, now, run_id),
            )
            connection.execute(
                """INSERT INTO run_transitions
                   (run_id, from_state, to_state, reason, occurred_at)
                   VALUES (?, 'validating', 'publishing', ?, ?)""",
                (run_id, recovery_reason, now),
            )
            return True

    def close_pull_request_attempt(
        self,
        run_id: str,
        pull: PullRequestInfo,
    ) -> str | None:
        if pull.state == "open" and not pull.merged:
            raise ValueError("cannot close a run for an open pull request")
        event_id = f"pull-closed:{pull.node_id}:{pull.updated_at}"
        with self.database.connect() as connection:
            existing = connection.execute(
                """SELECT runs.id
                   FROM activation_events
                   JOIN runs ON runs.activation_event_id=activation_events.id
                   WHERE activation_events.github_event_id=?""",
                (event_id,),
            ).fetchone()
            context = connection.execute(
                """SELECT runs.state, runs.repository_id, runs.issue_id,
                          issues.number,
                          repositories.owner, repositories.name,
                          repositories.default_branch,
                          repositories.current_sandbox_version_id,
                          repositories.current_team_version_id,
                          repositories.enabled, repositories.removed_at
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        if existing is not None:
            return str(existing["id"])
        if context is None:
            raise KeyError(run_id)
        if str(context["state"]) in {
            RunState.CANCELED.value,
            RunState.CLOSED.value,
        }:
            return None
        if pull.merged:
            self.transition(
                run_id,
                RunState.CLOSED,
                reason="pull request was merged by an external actor",
            )
            return None

        issue = self.github.get_issue(
            str(context["owner"]),
            str(context["name"]),
            int(context["number"]),
        )
        if issue.state != "open":
            self.transition(
                run_id,
                RunState.CLOSED,
                reason=(
                    "pull request closed without merge and linked "
                    f"issue is {issue.state}"
                ),
            )
            return None
        if (
            not bool(context["enabled"])
            or context["removed_at"] is not None
            or not context["current_sandbox_version_id"]
            or not context["current_team_version_id"]
        ):
            return None

        base_sha = self.github.get_branch_head(
            str(context["owner"]),
            str(context["name"]),
            str(context["default_branch"]),
        )
        repository_id = str(context["repository_id"])
        issue_id = str(context["issue_id"])
        activation_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{repository_id}:event:{event_id}")
        )
        replacement_run_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{repository_id}:activation:{event_id}")
        )
        root = (
            self.data_root
            / "repositories"
            / repository_id
            / "runs"
            / replacement_run_id
        )
        checkout = root / "checkout"
        now = _utc_now()
        reason = (
            "pull request closed without merge; started replacement run "
            f"{replacement_run_id} for open issue"
        )
        with self.database.transaction() as connection:
            source = connection.execute(
                """SELECT runs.state, pull_requests.state AS pull_state,
                          repositories.enabled, repositories.removed_at,
                          repositories.default_branch,
                          repositories.current_sandbox_version_id,
                          repositories.current_team_version_id
                   FROM runs
                   JOIN pull_requests ON pull_requests.run_id=runs.id
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
            if source is None:
                raise KeyError(run_id)
            existing = connection.execute(
                """SELECT runs.id
                   FROM activation_events
                   JOIN runs ON runs.activation_event_id=activation_events.id
                   WHERE activation_events.repository_id=?
                     AND activation_events.github_event_id=?""",
                (repository_id, event_id),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            if str(source["state"]) in {
                RunState.CANCELED.value,
                RunState.CLOSED.value,
            }:
                return None
            if (
                str(source["pull_state"]) != "closed"
                or not bool(source["enabled"])
                or source["removed_at"] is not None
                or not source["current_sandbox_version_id"]
                or not source["current_team_version_id"]
            ):
                return None
            active = connection.execute(
                """SELECT id FROM runs
                   WHERE issue_id=? AND id!=?
                     AND state NOT IN ('canceled', 'closed')""",
                (issue_id, run_id),
            ).fetchone()
            if active is not None:
                return str(active["id"])
            _, issue_version_id, _ = self._store_issue_version(
                connection,
                repository_id,
                issue_id,
                issue,
            )
            previous_state = str(source["state"])
            connection.execute(
                """UPDATE runs
                   SET state='closed', last_completed_state=?, reason=?,
                       updated_at=?, closed_at=?
                   WHERE id=?""",
                (previous_state, reason, now, now, run_id),
            )
            connection.execute(
                """INSERT INTO run_transitions
                   (run_id, from_state, to_state, reason, occurred_at)
                   VALUES (?, ?, 'closed', ?, ?)""",
                (run_id, previous_state, reason, now),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, issue_version_id,
                    github_event_id, applied_at, kind)
                   VALUES (?, ?, ?, ?, ?, ?, 'closed_pr_restart')""",
                (
                    activation_id,
                    repository_id,
                    issue_id,
                    issue_version_id,
                    event_id,
                    pull.updated_at,
                ),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, checkout_path, run_path, created_at,
                    updated_at, priority)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?,
                           (SELECT COALESCE(MAX(priority), -1) + 1
                              FROM runs WHERE repository_id=?))""",
                (
                    replacement_run_id,
                    repository_id,
                    issue_id,
                    activation_id,
                    source["current_sandbox_version_id"],
                    source["current_team_version_id"],
                    source["default_branch"],
                    base_sha,
                    str(checkout),
                    str(root),
                    now,
                    now,
                    repository_id,
                ),
            )
            connection.execute(
                """INSERT INTO run_transitions
                   (run_id, from_state, to_state, occurred_at)
                   VALUES (?, NULL, 'queued', ?)""",
                (replacement_run_id, now),
            )
        try:
            layout = RunLayout.create(
                self.data_root,
                repository_id,
                replacement_run_id,
            )
            source = self.data_root / "repositories" / repository_id / "source"
            self.checkouts.create(source, base_sha, layout.checkout)
        except Exception as error:
            self.transition(
                replacement_run_id,
                RunState.BLOCKED,
                reason=f"checkout creation failed: {error}",
            )
        return replacement_run_id

    def _activate(self, repository: object, event: ActivationEvent) -> str | None:
        repository_id = str(repository["id"])  # type: ignore[index]
        proposed_issue_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{repository_id}:issue:{event.issue.node_id}",
            )
        )
        with self.database.transaction() as connection:
            eligible = connection.execute(
                """SELECT 1 FROM repositories
                   WHERE id=? AND enabled=1 AND removed_at IS NULL""",
                (repository_id,),
            ).fetchone()
            if eligible is None:
                return None
            already_processed = connection.execute(
                """SELECT 1 FROM activation_events
                   WHERE repository_id=? AND github_event_id=?""",
                (repository_id, event.event_id),
            ).fetchone()
            if already_processed is not None:
                return None
            stored_issue = connection.execute(
                """SELECT id FROM issues
                   WHERE repository_id=? AND number=?""",
                (repository_id, event.issue.number),
            ).fetchone()
            if stored_issue is not None:
                active = connection.execute(
                    """SELECT 1 FROM runs WHERE issue_id=?
                       AND state NOT IN ('canceled', 'closed')""",
                    (stored_issue["id"],),
                ).fetchone()
                if active is not None:
                    return None
            issue_id, issue_version_id, _ = self._store_issue_version(
                connection,
                repository_id,
                proposed_issue_id,
                event.issue,
            )

        base_sha = self.github.get_branch_head(
            str(repository["owner"]),  # type: ignore[index]
            str(repository["name"]),  # type: ignore[index]
            str(repository["default_branch"]),  # type: ignore[index]
        )
        run_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"{repository_id}:activation:{event.event_id}"
            )
        )
        activation_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{repository_id}:event:{event.event_id}")
        )
        root = self.data_root / "repositories" / repository_id / "runs" / run_id
        checkout = root / "checkout"
        now = _utc_now()
        try:
            with self.database.transaction() as connection:
                eligible = connection.execute(
                    """SELECT 1 FROM repositories
                       WHERE id=? AND enabled=1 AND removed_at IS NULL""",
                    (repository_id,),
                ).fetchone()
                if eligible is None:
                    return None
                already_processed = connection.execute(
                    """SELECT 1 FROM activation_events
                       WHERE repository_id=? AND github_event_id=?""",
                    (repository_id, event.event_id),
                ).fetchone()
                if already_processed is not None:
                    return None
                active = connection.execute(
                    """SELECT 1 FROM runs WHERE issue_id=?
                       AND state NOT IN ('canceled', 'closed')""",
                    (issue_id,),
                ).fetchone()
                if active is not None:
                    return None
                connection.execute(
                    """INSERT INTO activation_events
                       (id, repository_id, issue_id, issue_version_id,
                        github_event_id, applied_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        activation_id,
                        repository_id,
                        issue_id,
                        issue_version_id,
                        event.event_id,
                        event.applied_at,
                    ),
                )
                connection.execute(
                    """INSERT INTO runs
                       (id, repository_id, issue_id, activation_event_id,
                        sandbox_version_id, team_version_id, intended_base_branch,
                        base_sha, state, checkout_path, run_path, created_at,
                        updated_at, priority)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?,
                               (SELECT COALESCE(MAX(priority), -1) + 1 FROM runs))""",
                    (
                        run_id,
                        repository_id,
                        issue_id,
                        activation_id,
                        repository["current_sandbox_version_id"],  # type: ignore[index]
                        repository["current_team_version_id"],  # type: ignore[index]
                        repository["default_branch"],  # type: ignore[index]
                        base_sha,
                        str(checkout),
                        str(root),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """INSERT INTO run_transitions
                       (run_id, from_state, to_state, occurred_at)
                       VALUES (?, NULL, 'queued', ?)""",
                    (run_id, now),
                )
        except sqlite3.IntegrityError:
            return None
        try:
            layout = RunLayout.create(self.data_root, repository_id, run_id)
            source = self.data_root / "repositories" / repository_id / "source"
            self.checkouts.create(source, base_sha, layout.checkout)
        except Exception as error:
            self.transition(
                run_id,
                RunState.BLOCKED,
                reason=f"checkout creation failed: {error}",
            )
        return run_id

    def transition(
        self,
        run_id: str,
        target: RunState,
        *,
        reason: str | None = None,
    ) -> None:
        if (
            target in {RunState.BLOCKED, RunState.CANCELED, RunState.CLOSED}
            and not (reason or "").strip()
        ):
            raise ValueError(f"{target.value} transition requires a reason")
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT runs.*,
                          repositories.enabled AS repository_enabled,
                          repositories.removed_at AS repository_removed_at
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = RunState(row["state"])
            if target not in {RunState.CANCELED, RunState.CLOSED} and (
                not bool(row["repository_enabled"])
                or row["repository_removed_at"] is not None
            ):
                raise RunPaused(run_id)
            if not allowed_transition(current, target):
                raise ValueError(
                    f"invalid run transition: {current.value} -> {target.value}"
                )
            if target == RunState.BLOCKED:
                last_completed = row["last_completed_state"]
            elif current == RunState.BLOCKED:
                last_completed = row["last_completed_state"]
            else:
                last_completed = current.value
            now = _utc_now()
            connection.execute(
                """UPDATE runs
                   SET state=?, last_completed_state=?, reason=?, updated_at=?,
                       resume_state=NULL,
                       canceled_at=CASE WHEN ?='canceled' THEN ? ELSE canceled_at END,
                       closed_at=CASE WHEN ?='closed' THEN ? ELSE closed_at END,
                       retry_attempt_count=CASE
                           WHEN ? THEN 0 ELSE retry_attempt_count END,
                       retry_operation=CASE
                           WHEN ? THEN NULL ELSE retry_operation END,
                       retry_next_at=CASE
                           WHEN ? THEN NULL ELSE retry_next_at END,
                       retry_last_error=CASE
                           WHEN ? THEN NULL ELSE retry_last_error END
                   WHERE id=?""",
                (
                    target.value,
                    last_completed,
                    reason,
                    now,
                    target.value,
                    now,
                    target.value,
                    now,
                    int(target in {RunState.CANCELED, RunState.CLOSED}),
                    int(target in {RunState.CANCELED, RunState.CLOSED}),
                    int(target in {RunState.CANCELED, RunState.CLOSED}),
                    int(target in {RunState.CANCELED, RunState.CLOSED}),
                    run_id,
                ),
            )
            connection.execute(
                """INSERT INTO run_transitions
                   (run_id, from_state, to_state, reason, occurred_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, current.value, target.value, reason, now),
            )

    def cancel(self, run_id: str, reason: str) -> None:
        with self._run_lock(run_id):
            self.transition(run_id, RunState.CANCELED, reason=reason)
            try:
                self.sandbox.cancel(run_id)
            finally:
                if self.processes is not None:
                    self.processes.cancel(run_id)

    def retry(self, run_id: str) -> str:
        """Resume a blocked run or remove a pending automatic-retry delay."""

        reason = "retry requested by user"
        with self._run_lock(run_id):
            with self.database.transaction() as connection:
                row = connection.execute(
                    """SELECT runs.*,
                              repositories.enabled AS repository_enabled,
                              repositories.removed_at AS repository_removed_at,
                              repositories.onboarding_state
                       FROM runs
                       JOIN repositories
                         ON repositories.id=runs.repository_id
                       WHERE runs.id=?""",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(run_id)
                if (
                    not bool(row["repository_enabled"])
                    or row["repository_removed_at"] is not None
                    or str(row["onboarding_state"]) != "ready"
                ):
                    raise ValueError("repository is not ready to run")

                current = RunState(str(row["state"]))
                now = _utc_now()
                if current == RunState.BLOCKED:
                    transition = connection.execute(
                        """SELECT from_state
                           FROM run_transitions
                           WHERE run_id=? AND to_state='blocked'
                           ORDER BY id DESC LIMIT 1""",
                        (run_id,),
                    ).fetchone()
                    if transition is None or transition["from_state"] is None:
                        raise ValueError("blocked run has no resumable prior state")
                    target = RunState(str(transition["from_state"]))
                    if target in {
                        RunState.BLOCKED,
                        RunState.CANCELED,
                        RunState.CLOSED,
                    }:
                        raise ValueError(
                            f"blocked run cannot resume from {target.value}"
                        )
                    if target in ACTIVE_RUN_STATES:
                        activated = self.request_repository_lane(
                            connection,
                            run_id,
                            target,
                            reason=reason,
                            allow_restart=True,
                        )
                        stored_target = target if activated else RunState.QUEUED
                    else:
                        connection.execute(
                            """UPDATE runs
                               SET state=?, resume_state=NULL, reason=?, updated_at=?
                               WHERE id=?""",
                            (target.value, reason, now, run_id),
                        )
                        connection.execute(
                            """INSERT INTO run_transitions
                               (run_id, from_state, to_state, reason, occurred_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (run_id, current.value, target.value, reason, now),
                        )
                        stored_target = target
                    connection.execute(
                        """UPDATE runs
                           SET retry_attempt_count=0,
                               retry_operation=NULL,
                               retry_next_at=NULL,
                               retry_last_error=NULL
                           WHERE id=?""",
                        (run_id,),
                    )
                else:
                    if row["retry_next_at"] is None:
                        raise ValueError(
                            "run is not blocked and no retry is pending"
                        )
                    target = current
                    stored_target = current
                    connection.execute(
                        """UPDATE runs
                           SET retry_next_at=NULL, updated_at=?
                           WHERE id=?""",
                        (now, run_id),
                    )
                    connection.execute(
                        """INSERT INTO run_transitions
                           (run_id, from_state, to_state, reason, occurred_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (run_id, current.value, target.value, reason, now),
                    )
                return stored_target.value

    def restart(self, run_id: str) -> str:
        """Create one fresh run for a canceled run's current open issue."""

        event_id = f"manual-restart:{run_id}"
        with self.database.connect() as connection:
            existing = connection.execute(
                """SELECT runs.id
                   FROM activation_events
                   JOIN runs ON runs.activation_event_id=activation_events.id
                   WHERE activation_events.github_event_id=?""",
                (event_id,),
            ).fetchone()
            context = connection.execute(
                """SELECT runs.state, runs.repository_id, runs.issue_id,
                          issues.number,
                          repositories.*,
                          repositories.id AS stored_repository_id
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
            active = (
                None
                if context is None
                else connection.execute(
                    """SELECT id FROM runs
                       WHERE issue_id=? AND id!=?
                         AND state NOT IN ('canceled', 'closed')
                       ORDER BY created_at, id LIMIT 1""",
                    (context["issue_id"], run_id),
                ).fetchone()
            )
        if existing is not None:
            return str(existing["id"])
        if context is None:
            raise KeyError(run_id)
        if str(context["state"]) != RunState.CANCELED.value:
            raise ValueError("only canceled runs can be restarted")
        if active is not None:
            raise ValueError("issue already has a nonterminal run")
        if (
            not bool(context["enabled"])
            or context["removed_at"] is not None
            or str(context["onboarding_state"]) != "ready"
            or not context["current_sandbox_version_id"]
            or not context["current_team_version_id"]
        ):
            raise ValueError("repository is not ready to run")

        issue = self.github.get_issue(
            str(context["owner"]),
            str(context["name"]),
            int(context["number"]),
        )
        if issue.state != "open":
            raise ValueError("GitHub issue is not open")
        event = ActivationEvent(
            event_id=event_id,
            applied_at=_utc_now(),
            issue=issue,
        )
        replacement_run_id = self._activate(context, event)
        if replacement_run_id is not None:
            return replacement_run_id

        with self.database.connect() as connection:
            existing = connection.execute(
                """SELECT runs.id
                   FROM activation_events
                   JOIN runs ON runs.activation_event_id=activation_events.id
                   WHERE activation_events.github_event_id=?""",
                (event_id,),
            ).fetchone()
            active = connection.execute(
                """SELECT id FROM runs
                   WHERE issue_id=? AND id!=?
                     AND state NOT IN ('canceled', 'closed')
                   ORDER BY created_at, id LIMIT 1""",
                (context["issue_id"], run_id),
            ).fetchone()
        if existing is not None:
            return str(existing["id"])
        if active is not None:
            raise ValueError("issue already has a nonterminal run")
        raise RuntimeError("restart could not create a replacement run")

    def set_repository_paused(
        self, repository_id: str, paused: bool
    ) -> tuple[str, ...]:
        if not isinstance(paused, bool):
            raise ValueError("paused must be a boolean")
        with self.database.transaction() as connection:
            repository = connection.execute(
                "SELECT id FROM repositories WHERE id=? AND removed_at IS NULL",
                (repository_id,),
            ).fetchone()
            if repository is None:
                raise KeyError(repository_id)
            rows = connection.execute(
                """SELECT id FROM runs
                   WHERE repository_id=?
                     AND state NOT IN ('canceled', 'closed')
                   ORDER BY created_at, id""",
                (repository_id,),
            ).fetchall()
            connection.execute(
                """UPDATE repositories
                   SET enabled=?,
                       ready_issue_generation=ready_issue_generation + 1,
                       updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE id=?""",
                (int(not paused), repository_id),
            )
            if paused:
                connection.execute(
                    """UPDATE ready_issue_discovery
                       SET status=CASE
                             WHEN last_success_at IS NULL THEN 'unavailable'
                             ELSE 'stale'
                           END,
                           last_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                           error='Repository is paused'
                       WHERE repository_id=?""",
                    (repository_id,),
                )
        run_ids = tuple(str(row["id"]) for row in rows)
        if paused:
            for run_id in run_ids:
                if self.processes is not None:
                    self.processes.pause(run_id)
                self.sandbox.cancel(run_id)
            # Every orchestrator advance owns this lock. Process cancellation
            # makes long-running model/sandbox calls unwind; the barrier makes
            # pause completion the point after which no in-flight step remains.
            for run_id in run_ids:
                with self._run_lock(run_id):
                    pass
        elif self.processes is not None:
            for run_id in run_ids:
                self.processes.resume(run_id)
        return run_ids

    @contextmanager
    def external_effect(self, run_id: str) -> Generator[bool, None, None]:
        """Serialize one external mutation with durable cancellation."""

        with self._run_lock(run_id):
            with self.database.connect() as connection:
                row = connection.execute(
                    """SELECT runs.state, repositories.enabled,
                              repositories.removed_at
                       FROM runs
                       JOIN repositories ON repositories.id=runs.repository_id
                       WHERE runs.id=?""",
                    (run_id,),
                ).fetchone()
            if row is None:
                raise KeyError(run_id)
            state = RunState(str(row["state"]))
            yield (
                bool(row["enabled"])
                and row["removed_at"] is None
                and state not in {RunState.CANCELED, RunState.CLOSED}
            )

    def _run_lock(self, run_id: str) -> threading.RLock:
        with self._run_locks_guard:
            return self._run_locks.setdefault(run_id, threading.RLock())

    def _recover_legacy_feedback_blocks(
        self,
        *,
        blocker_pattern: str,
        recovery_reason: str,
        requires_unassigned_member: bool,
        requires_prior_recovery_reason: str | None = None,
    ) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT runs.id
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.state='blocked'
                     AND repositories.enabled=1
                     AND repositories.removed_at IS NULL
                     AND lower(runs.reason) LIKE ?
                   ORDER BY runs.created_at, runs.id""",
                (blocker_pattern,),
            ).fetchall()
        recovered: list[str] = []
        for row in rows:
            run_id = str(row["id"])
            with self._run_lock(run_id):
                with self.database.transaction() as connection:
                    candidate = connection.execute(
                        """SELECT runs.id
                           FROM runs
                           JOIN repositories
                             ON repositories.id=runs.repository_id
                           WHERE runs.id=?
                             AND runs.state='blocked'
                             AND repositories.enabled=1
                             AND repositories.removed_at IS NULL
                             AND lower(runs.reason) LIKE ?
                             AND EXISTS (
                                 SELECT 1
                                 FROM run_transitions
                                 WHERE run_transitions.run_id=runs.id
                                   AND run_transitions.from_state='resolving_feedback'
                                   AND run_transitions.to_state='blocked'
                             )
                             AND (
                                 (?=1 AND EXISTS (
                                     SELECT 1
                                     FROM team_members
                                     WHERE team_members.team_version_id=
                                           runs.team_version_id
                                       AND NOT EXISTS (
                                           SELECT 1
                                           FROM agent_assignments
                                           WHERE agent_assignments.run_id=runs.id
                                             AND agent_assignments.team_member_id=
                                                 team_members.id
                                       )
                                 ))
                                 OR
                                 (?=0 AND EXISTS (
                                     SELECT 1
                                     FROM agent_assignments
                                     JOIN team_members
                                       ON team_members.id=
                                          agent_assignments.team_member_id
                                     WHERE agent_assignments.run_id=runs.id
                                       AND team_members.team_version_id=
                                           runs.team_version_id
                                       AND team_members.role='implementer'
                                 ))
                             )
                             AND (
                                 EXISTS (
                                     SELECT 1
                                     FROM pull_requests
                                     JOIN feedback_versions
                                       ON feedback_versions.pull_request_id=
                                          pull_requests.id
                                     WHERE pull_requests.run_id=runs.id
                                       AND feedback_versions.feedback_type=
                                           'base_conflict'
                                       AND feedback_versions.state='pending'
                                       AND feedback_versions.source_sha IS NULL
                                       AND feedback_versions.superseded_at IS NULL
                                 )
                                 OR (
                                     EXISTS (
                                         SELECT 1
                                         FROM pull_requests
                                         JOIN feedback_versions
                                           ON feedback_versions.pull_request_id=
                                              pull_requests.id
                                         WHERE pull_requests.run_id=runs.id
                                           AND feedback_versions.feedback_type=
                                               'base_conflict'
                                           AND feedback_versions.state='processing'
                                           AND feedback_versions.source_sha IS NULL
                                           AND feedback_versions.superseded_at IS NULL
                                     )
                                     AND EXISTS (
                                         SELECT 1
                                         FROM outbound_operations
                                         WHERE outbound_operations.run_id=runs.id
                                           AND outbound_operations.kind=
                                               'feedback_revision_batch'
                                           AND outbound_operations.state='pending'
                                     )
                                 )
                             )
                             AND (
                                 ? IS NULL
                                 OR EXISTS (
                                     SELECT 1
                                     FROM run_transitions
                                     WHERE run_transitions.run_id=runs.id
                                       AND run_transitions.from_state='blocked'
                                       AND run_transitions.to_state=
                                           'resolving_feedback'
                                       AND run_transitions.reason=?
                                 )
                             )
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM run_transitions
                                 WHERE run_transitions.run_id=runs.id
                                   AND run_transitions.from_state='blocked'
                                   AND run_transitions.to_state='resolving_feedback'
                                   AND run_transitions.reason=?
                             )""",
                        (
                            run_id,
                            blocker_pattern,
                            int(requires_unassigned_member),
                            int(requires_unassigned_member),
                            requires_prior_recovery_reason,
                            requires_prior_recovery_reason,
                            recovery_reason,
                        ),
                    ).fetchone()
                    if candidate is None:
                        continue
                    self.request_repository_lane(
                        connection,
                        run_id,
                        RunState.RESOLVING_FEEDBACK,
                        reason=recovery_reason,
                        allow_restart=True,
                    )
                    recovered.append(run_id)
        return tuple(recovered)

    def _recover_legacy_feedback_assignment_blocks(self) -> tuple[str, ...]:
        return self._recover_legacy_feedback_blocks(
            blocker_pattern=(
                "%assignment to %rejected because issue work already began%"
            ),
            recovery_reason=_LEGACY_FEEDBACK_TEAM_EXPANSION_RECOVERY_REASON,
            requires_unassigned_member=True,
        )

    def _recover_legacy_feedback_handoff_blocks(self) -> tuple[str, ...]:
        return self._recover_legacy_feedback_blocks(
            blocker_pattern=(
                "%every stored implementation member is already selected%"
                "no controller action exists%already-assigned member%"
            ),
            recovery_reason=_LEGACY_FEEDBACK_MEMBER_HANDOFF_RECOVERY_REASON,
            requires_unassigned_member=False,
        )

    def _recover_legacy_feedback_validation_base_blocks(self) -> tuple[str, ...]:
        return self._recover_legacy_feedback_blocks(
            blocker_pattern=(
                "%validation%inherited unchanged%"
                "controller-required fetched base%zero base-to-head diff%"
            ),
            recovery_reason=FEEDBACK_VALIDATION_BASE_RECOVERY_REASON,
            requires_unassigned_member=False,
        )

    def _recover_legacy_feedback_validation_replay_blocks(
        self,
    ) -> tuple[str, ...]:
        return self._recover_legacy_feedback_blocks(
            blocker_pattern=(
                "%required controller validation cannot pass without "
                "out-of-scope changes%reported as adding broad source "
                "suppression%zero diff from the fetched conflict base%"
                "roll back inherited base behavior%restricted-proxy failures%"
                "sandbox returns 403 for api.github.com%strict required "
                "validation remains externally blocked%"
            ),
            recovery_reason=FEEDBACK_VALIDATION_REPLAY_RECOVERY_REASON,
            requires_unassigned_member=False,
            requires_prior_recovery_reason=(
                FEEDBACK_VALIDATION_BASE_RECOVERY_REASON
            ),
        )

    def _recover_legacy_publication_merge_base_blocks(self) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT runs.id
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.state='blocked'
                     AND runs.reason=?
                     AND runs.validated_sha IS NOT NULL
                     AND repositories.enabled=1
                     AND repositories.removed_at IS NULL
                   ORDER BY runs.created_at, runs.id""",
                (_LEGACY_PUBLICATION_MERGE_CONFLICT_BLOCKER,),
            ).fetchall()
        recovered: list[str] = []
        for row in rows:
            run_id = str(row["id"])
            with self._run_lock(run_id):
                with self.database.transaction() as connection:
                    candidate = connection.execute(
                        """SELECT runs.id
                           FROM runs
                           JOIN repositories
                             ON repositories.id=runs.repository_id
                           WHERE runs.id=?
                             AND runs.state='blocked'
                             AND runs.reason=?
                             AND runs.validated_sha IS NOT NULL
                             AND repositories.enabled=1
                             AND repositories.removed_at IS NULL
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM run_transitions
                                 WHERE run_transitions.run_id=runs.id
                                   AND run_transitions.from_state='blocked'
                                   AND run_transitions.to_state='publishing'
                                   AND run_transitions.reason=?
                             )""",
                        (
                            run_id,
                            _LEGACY_PUBLICATION_MERGE_CONFLICT_BLOCKER,
                            _LEGACY_PUBLICATION_MERGE_BASE_RECOVERY_REASON,
                        ),
                    ).fetchone()
                    if candidate is None:
                        continue
                    self.request_repository_lane(
                        connection,
                        run_id,
                        RunState.PUBLISHING,
                        reason=_LEGACY_PUBLICATION_MERGE_BASE_RECOVERY_REASON,
                        allow_restart=True,
                    )
                    recovered.append(run_id)
        return tuple(recovered)

    def reconcile_recoverable_blocked_runs(self) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT runs.id FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.state='blocked'
                     AND repositories.enabled=1
                     AND repositories.removed_at IS NULL
                     AND runs.reason LIKE
                         'publication blocked: issue acceptance verification blocked:%'
                   ORDER BY runs.created_at, runs.id""").fetchall()
        recovered = list(
            self._recover_legacy_publication_merge_base_blocks()
        )
        recovered.extend(self._recover_legacy_feedback_assignment_blocks())
        recovered.extend(self._recover_legacy_feedback_handoff_blocks())
        recovered.extend(self._recover_legacy_feedback_validation_base_blocks())
        recovered.extend(self._recover_legacy_feedback_validation_replay_blocks())
        for row in rows:
            run_id = str(row["id"])
            with self._run_lock(run_id):
                with self.database.transaction() as connection:
                    candidate = connection.execute(
                        """SELECT runs.id, runs.state, runs.validated_sha,
                                  verification.id AS verification_id,
                                  verification.commit_sha,
                                  verification.report_json,
                                  verification.screenshot_decision_json
                           FROM runs
                           JOIN repositories
                             ON repositories.id=runs.repository_id
                           JOIN acceptance_verifications AS verification
                             ON verification.id = (
                                SELECT latest.id
                                FROM acceptance_verifications AS latest
                                WHERE latest.run_id=runs.id
                                ORDER BY latest.attempt DESC,
                                         latest.started_at DESC,
                                         latest.id DESC
                                LIMIT 1
                             )
                           WHERE runs.id=?
                             AND runs.state='blocked'
                             AND verification.state='blocked'
                             AND repositories.enabled=1
                             AND repositories.removed_at IS NULL""",
                        (run_id,),
                    ).fetchone()
                    if (
                        candidate is None
                        or candidate["validated_sha"] != candidate["commit_sha"]
                    ):
                        continue
                    prior_recovery = connection.execute(
                        """SELECT 1 FROM run_transitions
                           WHERE run_id=? AND from_state='blocked'
                             AND to_state='publishing' AND reason=?
                           LIMIT 1""",
                        (run_id, _LEGACY_ACCEPTANCE_RECOVERY_REASON),
                    ).fetchone()
                    if prior_recovery is not None:
                        continue
                    evidence = connection.execute(
                        """SELECT action_json, result_json
                           FROM acceptance_evidence
                           WHERE verification_id=?
                           ORDER BY sequence""",
                        (candidate["verification_id"],),
                    ).fetchall()
                    if not _is_legacy_unchallenged_visual_block(
                        candidate["report_json"],
                        candidate["screenshot_decision_json"],
                        evidence,
                    ):
                        continue
                    self.request_repository_lane(
                        connection,
                        run_id,
                        RunState.PUBLISHING,
                        reason=_LEGACY_ACCEPTANCE_RECOVERY_REASON,
                        allow_restart=True,
                    )
                    recovered.append(run_id)
        return tuple(recovered)

    def reconcile_nonterminal_runs(self) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT runs.* FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.state NOT IN ('blocked', 'canceled', 'closed')
                     AND repositories.enabled=1
                     AND repositories.removed_at IS NULL
                   ORDER BY runs.created_at""").fetchall()
        reconciled: list[str] = []
        errors: list[str] = []
        for row in rows:
            run_id = str(row["id"])
            try:
                self._reconcile_run(dict(row))
            except Exception as error:
                errors.append(f"{run_id}: {error or error.__class__.__name__}")
                continue
            reconciled.append(run_id)
        if errors:
            raise RuntimeError("restart reconciliation failed: " + "; ".join(errors))
        return tuple(reconciled)

    def _reconcile_run(self, row: dict[str, object]) -> None:
        repository_id = str(row["repository_id"])
        run_id = str(row["id"])
        layout = RunLayout.create(self.data_root, repository_id, run_id)
        source = self.data_root / "repositories" / repository_id / "source"
        self.checkouts.create(source, str(row["base_sha"]), layout.checkout)
        if row.get("checkout_path") != str(layout.checkout) or row.get(
            "run_path"
        ) != str(layout.root):
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE runs SET checkout_path=?, run_path=?, updated_at=? WHERE id=?",
                    (str(layout.checkout), str(layout.root), _utc_now(), run_id),
                )

    def get_run(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)


def _is_legacy_unchallenged_visual_block(
    report_value: object,
    screenshot_decision_value: object,
    evidence: object,
) -> bool:
    try:
        report = json.loads(str(report_value))
        screenshot_decision = json.loads(str(screenshot_decision_value))
    except (TypeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(report, dict)
        or report.get("state") != "blocked"
        or report.get("blocker") is not None
        or not isinstance(screenshot_decision, dict)
        or screenshot_decision.get("required") is not True
        or not isinstance(evidence, list)
    ):
        return False
    for row in evidence:
        try:
            action = json.loads(str(row["action_json"]))
            result = json.loads(str(row["result_json"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            return False
        if (
            isinstance(action, dict)
            and action.get("action") == "verdict_rejected"
            and isinstance(result, dict)
            and str(result.get("error", "")).startswith(
                "blocked acceptance verdict requires one remediation attempt"
            )
        ):
            return False
    description = _json(report).lower()
    return any(term in description for term in _LEGACY_VISUAL_BLOCK_TERMS)


def _issue_content_sha(issue: IssueInfo) -> str:
    payload = {
        "title": issue.title,
        "body": issue.body,
        "discussion": issue.discussion,
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
