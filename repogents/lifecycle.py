from __future__ import annotations

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

from .controller import RunProcessSupervisor, git_environment
from .database import Database
from .github import ActivationEvent, GitHubError
from .sandbox import RunLayout


class RunState(str, Enum):
    QUEUED = "queued"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    PUBLISHING = "publishing"
    WAITING_FOR_FEEDBACK = "waiting_for_feedback"
    RESOLVING_FEEDBACK = "resolving_feedback"
    QUIET_PERIOD = "quiet_period"
    NOTIFIED = "notified"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    CLOSED = "closed"


_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.IMPLEMENTING, RunState.BLOCKED, RunState.CANCELED}),
    RunState.IMPLEMENTING: frozenset({RunState.VALIDATING, RunState.BLOCKED, RunState.CANCELED}),
    RunState.VALIDATING: frozenset(
        {RunState.IMPLEMENTING, RunState.PUBLISHING, RunState.BLOCKED, RunState.CANCELED}
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
            RunState.QUIET_PERIOD,
            RunState.BLOCKED,
            RunState.CANCELED,
            RunState.CLOSED,
        }
    ),
    RunState.RESOLVING_FEEDBACK: frozenset(
        {
            RunState.VALIDATING,
            RunState.WAITING_FOR_FEEDBACK,
            RunState.QUIET_PERIOD,
            RunState.BLOCKED,
            RunState.CANCELED,
            RunState.CLOSED,
        }
    ),
    RunState.QUIET_PERIOD: frozenset(
        {
            RunState.RESOLVING_FEEDBACK,
            RunState.NOTIFIED,
            RunState.BLOCKED,
            RunState.CANCELED,
            RunState.CLOSED,
        }
    ),
    RunState.NOTIFIED: frozenset(
        {RunState.RESOLVING_FEEDBACK, RunState.BLOCKED, RunState.CANCELED, RunState.CLOSED}
    ),
    RunState.BLOCKED: frozenset({RunState.CANCELED}),
    RunState.CANCELED: frozenset(),
    RunState.CLOSED: frozenset(),
}



def allowed_transition(current: RunState, target: RunState) -> bool:
    return target in _TRANSITIONS[current]


class ActivationClient(Protocol):
    def list_ready_events(self, owner: str, name: str) -> list[ActivationEvent]: ...

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
            origin_url = source_origin.stdout.strip() if source_origin.returncode == 0 else None
            clone = subprocess.run(
                ["git", "clone", "--quiet", "--no-hardlinks", "--", str(source), str(destination)],
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
                raise RuntimeError(f"isolated checkout clone failed: {clone.stderr.strip()}")
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
                        ["git", "fetch", "--quiet", "--no-tags", "--force", "origin", base_sha],
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
            return ()
        sandbox_version_id = repository["current_sandbox_version_id"]
        team_version_id = repository["current_team_version_id"]
        if not sandbox_version_id or not team_version_id:
            raise RuntimeError("ready repository has no stored sandbox or team version")
        owner = str(repository["owner"])
        name = str(repository["name"])
        try:
            ready_issues = self.github.list_ready_issues(owner, name)
        except GitHubError:
            self._record_ready_issue_failure(repository_id)
        else:
            self._record_ready_issue_success(repository_id, ready_issues)
        events = self.github.list_ready_events(owner, name)
        created: list[str] = []
        for event in events:
            run_id = self._activate(repository, event)
            if run_id is not None:
                created.append(run_id)
        return tuple(created)

    def _record_ready_issue_success(
        self, repository_id: str, issues: object
    ) -> None:
        values = [
            {
                "number": issue.number,
                "title": issue.title,
                "url": issue.url,
                "updated_at": issue.updated_at,
            }
            for issue in issues  # type: ignore[union-attr]
        ]
        with self.database.transaction() as connection:
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

    def _activate(self, repository: object, event: ActivationEvent) -> str | None:
        repository_id = str(repository["id"])  # type: ignore[index]
        issue_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{repository_id}:issue:{event.issue.node_id}"))
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(repository_id, number) DO UPDATE SET
                     github_node_id=excluded.github_node_id,
                     url=excluded.url,
                     title=excluded.title,
                     body=excluded.body,
                     discussion_json=excluded.discussion_json,
                     updated_at=excluded.updated_at""",
                (
                    issue_id,
                    repository_id,
                    event.issue.node_id,
                    event.issue.number,
                    event.issue.url,
                    event.issue.title,
                    event.issue.body,
                    _json(event.issue.discussion),
                    event.issue.updated_at,
                ),
            )
            stored_issue = connection.execute(
                "SELECT id FROM issues WHERE repository_id=? AND number=?",
                (repository_id, event.issue.number),
            ).fetchone()
            issue_id = str(stored_issue["id"])
            already_processed = connection.execute(
                "SELECT 1 FROM activation_events WHERE repository_id=? AND github_event_id=?",
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

        base_sha = self.github.get_branch_head(
            str(repository["owner"]),  # type: ignore[index]
            str(repository["name"]),  # type: ignore[index]
            str(repository["default_branch"]),  # type: ignore[index]
        )
        run_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{repository_id}:activation:{event.event_id}")
        )
        activation_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{repository_id}:event:{event.event_id}")
        )
        root = self.data_root / "repositories" / repository_id / "runs" / run_id
        checkout = root / "checkout"
        now = _utc_now()
        try:
            with self.database.transaction() as connection:
                active = connection.execute(
                    """SELECT 1 FROM runs WHERE issue_id=?
                       AND state NOT IN ('canceled', 'closed')""",
                    (issue_id,),
                ).fetchone()
                if active is not None:
                    return None
                connection.execute(
                    """INSERT INTO activation_events
                       (id, repository_id, issue_id, github_event_id, applied_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (activation_id, repository_id, issue_id, event.event_id, event.applied_at),
                )
                connection.execute(
                    """INSERT INTO runs
                       (id, repository_id, issue_id, activation_event_id,
                        sandbox_version_id, team_version_id, intended_base_branch,
                        base_sha, state, checkout_path, run_path, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
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
            self.transition(run_id, RunState.BLOCKED, reason=f"checkout creation failed: {error}")
        return run_id

    def transition(
        self,
        run_id: str,
        target: RunState,
        *,
        reason: str | None = None,
    ) -> None:
        if target in {RunState.BLOCKED, RunState.CANCELED, RunState.CLOSED} and not (reason or "").strip():
            raise ValueError(f"{target.value} transition requires a reason")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = RunState(row["state"])
            if not allowed_transition(current, target):
                raise ValueError(f"invalid run transition: {current.value} -> {target.value}")
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
                       canceled_at=CASE WHEN ?='canceled' THEN ? ELSE canceled_at END,
                       closed_at=CASE WHEN ?='closed' THEN ? ELSE closed_at END
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

    @contextmanager
    def external_effect(self, run_id: str) -> Generator[bool, None, None]:
        """Serialize one external mutation with durable cancellation."""

        with self._run_lock(run_id):
            state = RunState(str(self.get_run(run_id)["state"]))
            yield state not in {RunState.CANCELED, RunState.CLOSED}

    def _run_lock(self, run_id: str) -> threading.RLock:
        with self._run_locks_guard:
            return self._run_locks.setdefault(run_id, threading.RLock())

    def reconcile_nonterminal_runs(self) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM runs
                   WHERE state NOT IN ('blocked', 'canceled', 'closed')
                   ORDER BY created_at"""
            ).fetchall()
        reconciled: list[str] = []
        errors: list[str] = []
        for row in rows:
            run_id = str(row["id"])
            try:
                self._reconcile_run(dict(row))
            except Exception as error:
                errors.append(
                    f"{run_id}: {error or error.__class__.__name__}"
                )
                continue
            reconciled.append(run_id)
        if errors:
            raise RuntimeError(
                "restart reconciliation failed: " + "; ".join(errors)
            )
        return tuple(reconciled)

    def _reconcile_run(self, row: dict[str, object]) -> None:
        repository_id = str(row["repository_id"])
        run_id = str(row["id"])
        layout = RunLayout.create(self.data_root, repository_id, run_id)
        source = self.data_root / "repositories" / repository_id / "source"
        self.checkouts.create(source, str(row["base_sha"]), layout.checkout)
        if row.get("checkout_path") != str(layout.checkout) or row.get("run_path") != str(layout.root):
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE runs SET checkout_path=?, run_path=?, updated_at=? WHERE id=?",
                    (str(layout.checkout), str(layout.root), _utc_now(), run_id),
                )

    def get_run(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))




def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
