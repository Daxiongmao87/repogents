from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .database import Database
from .github import PullRequestInfo
from .lifecycle import RunLifecycle, RunState


class TransientQuietCheckError(RuntimeError):
    """A due-time observation failed without changing the active generation."""

    def __init__(self, run_id: str, operation: str) -> None:
        self.run_id = run_id
        self.operation = operation
        super().__init__(
            f"transient quiet-period {operation} failure for run {run_id}"
        )


class PullStatusGateway(Protocol):
    def get_pull_request(
        self, owner: str, name: str, number: int
    ) -> PullRequestInfo: ...


class FeedbackPoller(Protocol):
    def poll_run(self, run_id: str) -> int: ...


def _required_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeError(f"{field} must be an integer")
    return int(value)


class QuietPeriodService:
    def __init__(
        self,
        *,
        database: Database,
        lifecycle: RunLifecycle,
        gateway: PullStatusGateway,
        feedback: FeedbackPoller,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle
        self.gateway = gateway
        self.feedback = feedback
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def start(self, run_id: str) -> str:
        context = self._context(run_id)
        with self.database.connect() as connection:
            active = connection.execute(
                "SELECT * FROM quiet_periods WHERE run_id=? AND state='active'",
                (run_id,),
            ).fetchone()
        if active is not None:
            if context["run_state"] == RunState.WAITING_FOR_FEEDBACK.value:
                self.lifecycle.transition(run_id, RunState.QUIET_PERIOD)
            return str(active["id"])
        if context["run_state"] != RunState.WAITING_FOR_FEEDBACK.value:
            raise ValueError(
                f"quiet period cannot start from state {context['run_state']}"
            )
        if context["pull_state"] != "open":
            raise ValueError("quiet period requires an open pull request")
        if context["remote_head_sha"] != context["validated_head_sha"]:
            raise ValueError("quiet period requires a confirmed validated remote head")
        with self.database.connect() as connection:
            pending = connection.execute(
                """SELECT COUNT(*) FROM feedback_versions
                   JOIN pull_requests ON pull_requests.id=feedback_versions.pull_request_id
                   WHERE pull_requests.run_id=?
                     AND feedback_versions.state IN ('pending', 'processing')""",
                (run_id,),
            ).fetchone()[0]
            generation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 FROM quiet_periods WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
        if pending:
            raise ValueError("quiet period cannot start while feedback is pending")
        started = self._now()
        deadline = started + timedelta(minutes=30)
        quiet_id = _stable_id(f"{run_id}:quiet:{generation}")
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO quiet_periods
                   (id, run_id, generation, started_at, deadline, state)
                   VALUES (?, ?, ?, ?, ?, 'active')""",
                (
                    quiet_id,
                    run_id,
                    generation,
                    _iso(started),
                    _iso(deadline),
                ),
            )
        self.lifecycle.transition(run_id, RunState.QUIET_PERIOD)
        return quiet_id

    def check_due(self, run_id: str) -> str | None:
        with self.database.connect() as connection:
            quiet = connection.execute(
                """SELECT * FROM quiet_periods
                   WHERE run_id=? AND state='active'
                   ORDER BY generation DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        if quiet is None:
            existing = self._existing_notification(run_id)
            if existing is not None:
                run = self.lifecycle.get_run(run_id)
                if run["state"] == RunState.QUIET_PERIOD.value:
                    self.lifecycle.transition(run_id, RunState.NOTIFIED)
                return existing
            return None
        if self._now() < _parse(str(quiet["deadline"])):
            return None
        context = self._context(run_id)
        try:
            pull = self.gateway.get_pull_request(
                str(context["owner"]),
                str(context["name"]),
                _required_int(context["pull_number"], "pull number"),
            )
        except Exception as error:
            raise TransientQuietCheckError(
                run_id, "pull-request status poll"
            ) from error
        if pull.merged or pull.state != "open":
            now = _iso(self._now())
            reason = "pull request was merged or closed by an external actor"
            with self.database.transaction() as connection:
                current_quiet = connection.execute(
                    "SELECT state FROM quiet_periods WHERE id=?", (quiet["id"],)
                ).fetchone()
                if current_quiet is None or current_quiet["state"] != "active":
                    return None
                run = connection.execute(
                    "SELECT state FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if run is None:
                    raise KeyError(run_id)
                current = RunState(run["state"])
                if current != RunState.QUIET_PERIOD:
                    raise ValueError(
                        f"invalid run transition: {current.value} -> "
                        f"{RunState.CLOSED.value}"
                    )
                connection.execute(
                    """UPDATE quiet_periods
                       SET state='canceled', canceled_at=? WHERE id=?""",
                    (now, quiet["id"]),
                )
                connection.execute(
                    """UPDATE pull_requests SET state=?, updated_at=? WHERE run_id=?""",
                    ("merged" if pull.merged else "closed", now, run_id),
                )
                connection.execute(
                    """UPDATE runs
                       SET state='closed', last_completed_state=?, reason=?,
                           updated_at=?, closed_at=?
                       WHERE id=?""",
                    (current.value, reason, now, now, run_id),
                )
                connection.execute(
                    """INSERT INTO run_transitions
                       (run_id, from_state, to_state, reason, occurred_at)
                       VALUES (?, ?, 'closed', ?, ?)""",
                    (run_id, current.value, reason, now),
                )
            return None
        try:
            newly_observed = self.feedback.poll_run(run_id)
        except Exception as error:
            raise TransientQuietCheckError(run_id, "feedback poll") from error
        now = _iso(self._now())
        with self.database.transaction() as connection:
            current = connection.execute(
                """SELECT quiet_periods.state AS quiet_state,
                          runs.state AS run_state
                   FROM quiet_periods
                   JOIN runs ON runs.id=quiet_periods.run_id
                   WHERE quiet_periods.id=?""",
                (quiet["id"],),
            ).fetchone()
            if (
                current is None
                or current["quiet_state"] != "active"
                or current["run_state"] != RunState.QUIET_PERIOD.value
            ):
                return None
            pending = connection.execute(
                """SELECT COUNT(*) FROM feedback_versions
                   JOIN pull_requests
                     ON pull_requests.id=feedback_versions.pull_request_id
                   WHERE pull_requests.run_id=?
                     AND feedback_versions.state IN ('pending', 'processing')""",
                (run_id,),
            ).fetchone()[0]
            if newly_observed or pending:
                connection.execute(
                    """UPDATE quiet_periods
                       SET state='canceled', canceled_at=? WHERE id=?""",
                    (now, quiet["id"]),
                )
                connection.execute(
                    """UPDATE runs
                       SET state='resolving_feedback',
                           last_completed_state='quiet_period',
                           reason=NULL, updated_at=?
                       WHERE id=?""",
                    (now, run_id),
                )
                connection.execute(
                    """INSERT INTO run_transitions
                       (run_id, from_state, to_state, occurred_at)
                       VALUES (?, 'quiet_period', 'resolving_feedback', ?)""",
                    (run_id, now),
                )
                return None
            notification_id = _stable_id(f"{quiet['id']}:notification")
            connection.execute(
                """INSERT OR IGNORE INTO notifications
                   (id, quiet_period_id, created_at) VALUES (?, ?, ?)""",
                (notification_id, quiet["id"], now),
            )
            connection.execute(
                """UPDATE quiet_periods
                   SET state='completed', completed_at=? WHERE id=?""",
                (now, quiet["id"]),
            )
            connection.execute(
                """UPDATE runs
                   SET state='notified', last_completed_state='quiet_period',
                       reason=NULL, updated_at=?
                   WHERE id=?""",
                (now, run_id),
            )
            connection.execute(
                """INSERT INTO run_transitions
                   (run_id, from_state, to_state, occurred_at)
                   VALUES (?, 'quiet_period', 'notified', ?)""",
                (run_id, now),
            )
        return notification_id

    def check_all_due(self) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM quiet_periods WHERE state='active' ORDER BY deadline"
            ).fetchall()
        created: list[str] = []
        for row in rows:
            notification = self.check_due(str(row["run_id"]))
            if notification is not None:
                created.append(notification)
        return tuple(created)

    def acknowledge(self, notification_id: str) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE notifications SET read_at=COALESCE(read_at, ?)
                   WHERE id=?""",
                (_iso(self._now()), notification_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(notification_id)

    def list_notifications(self) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT notifications.id, notifications.created_at,
                          notifications.read_at,
                          repositories.owner, repositories.name,
                          issues.number AS issue_number, issues.url AS issue_url,
                          issues.title AS issue_title,
                          pull_requests.number AS pull_number,
                          pull_requests.url AS pull_url
                   FROM notifications
                   JOIN quiet_periods ON quiet_periods.id=notifications.quiet_period_id
                   JOIN runs ON runs.id=quiet_periods.run_id
                   JOIN repositories ON repositories.id=runs.repository_id
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN pull_requests ON pull_requests.run_id=runs.id
                   ORDER BY notifications.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def _existing_notification(self, run_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT notifications.id FROM notifications
                   JOIN quiet_periods ON quiet_periods.id=notifications.quiet_period_id
                   WHERE quiet_periods.run_id=?
                   ORDER BY quiet_periods.generation DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def _context(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT runs.state AS run_state,
                          repositories.owner, repositories.name,
                          pull_requests.number AS pull_number,
                          pull_requests.state AS pull_state,
                          pull_requests.remote_head_sha,
                          pull_requests.validated_head_sha
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   JOIN pull_requests ON pull_requests.run_id=runs.id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        if row is None or row["pull_number"] is None:
            raise KeyError(run_id)
        return dict(row)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("quiet-period clock must return a timezone-aware instant")
        return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _stable_id(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))
