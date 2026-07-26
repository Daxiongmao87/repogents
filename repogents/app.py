from __future__ import annotations

import json
import sqlite3
import urllib.parse
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, cast

from .acceptance import (
    AcceptanceService,
    current_acceptance_verification,
    load_acceptance_artifact,
)
from .configuration import ModelProviderConfiguration
from .controller import EnvironmentSecretResolver, RunPaused, RunProcessSupervisor
from .database import Database
from .execution import ExecutionService, MiniSweModelRuntime
from .feedback import FeedbackService, MiniSweFeedbackEvaluator
from .github import GitHubClient
from .lifecycle import GitCheckoutManager, RunLifecycle, RunState
from .mini_swe import MINI_SWE_RUNTIME
from .onboarding import (
    GitSourceManager,
    OnboardingService,
    MiniSweRepositoryEvidenceAnalyzer,
    RepositoryInspector,
    SandboxEnvironmentProvisioner,
)
from .publication import (
    GitPublicationGateway,
    MiniSweScopeReviewer,
    PublicationService,
)
from .sandbox import SandboxManager, redact_text
from .team import EvidenceTeamFormulator, TeamService

_TERMINAL_RUN_STATES = {
    RunState.CANCELED.value,
    RunState.CLOSED.value,
}
_TERMINAL_OR_IDLE = {
    RunState.BLOCKED.value,
    *_TERMINAL_RUN_STATES,
}
_RUN_REASON_SUMMARY_LIMIT = 400
_FOCUSABLE_RUN_STATES = {
    RunState.QUEUED.value,
    RunState.IMPLEMENTING.value,
    RunState.VALIDATING.value,
    RunState.PUBLISHING.value,
    RunState.RESOLVING_FEEDBACK.value,
}


class SchedulerControl(Protocol):
    def request_tick(self) -> None: ...


class LifecycleOperations(Protocol):
    def reconcile_nonterminal_runs(self) -> tuple[str, ...]: ...
    def reconcile_recoverable_blocked_runs(self) -> tuple[str, ...]: ...

    def poll_repository(self, repository_id: str) -> tuple[str, ...]: ...
    def poll_issue_revision(self, run_id: str) -> bool: ...

    def get_run(self, run_id: str) -> dict[str, object]: ...

    def cancel(self, run_id: str, reason: str) -> None: ...
    def set_repository_paused(
        self, repository_id: str, paused: bool
    ) -> tuple[str, ...]: ...


class ExecutionOperations(Protocol):
    def execute(
        self, run_id: str, *, additional_context: str | None = None
    ) -> str | None: ...


class PublicationOperations(Protocol):
    def publish(self, run_id: str) -> object | None: ...


class FeedbackOperations(Protocol):
    def poll_run(self, run_id: str) -> int: ...

    def resolve_run(self, run_id: str) -> int: ...


class OnboardingOperations(Protocol):
    def onboard(
        self, identity: str, inputs: dict[str, object] | None = None
    ) -> str: ...

    def reonboard(
        self,
        repository_id: str,
        inputs: dict[str, object] | None = None,
    ) -> str: ...


class Orchestrator:
    def __init__(
        self,
        *,
        database: Database,
        lifecycle: LifecycleOperations,
        execution: ExecutionOperations,
        publication: PublicationOperations,
        feedback: FeedbackOperations,
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle
        self.execution = execution
        self.publication = publication
        self.feedback = feedback
        self._lock = threading.Lock()
        self._activation_poll_lock = threading.Lock()
        self._advancing_runs = threading.Event()
        self._repository_locks_guard = threading.Lock()
        self._repository_locks: dict[str, threading.Lock] = {}
        self._advancing_lock = threading.Lock()
        self._advancing_repository_count = 0
        self.last_errors: list[str] = []

    def tick(self) -> None:
        self.poll_issue_revisions()
        self.poll_feedback()
        for repository_id in self.prepare_tick():
            self.advance_repository(repository_id)

    def prepare_tick(self) -> tuple[str, ...]:
        with self._lock:
            self.last_errors = []
            try:
                self.lifecycle.reconcile_nonterminal_runs()
            except Exception as error:
                self.last_errors.append(
                    f"restart reconciliation: {error or error.__class__.__name__}"
                )
            try:
                self.lifecycle.reconcile_recoverable_blocked_runs()
            except Exception as error:
                self.last_errors.append(
                    "blocked run recovery: "
                    f"{error or error.__class__.__name__}"
                )
            self._poll_ready_repositories()
            return self._ordered_repository_ids()

    def advance_repository(self, repository_id: str) -> None:
        with self._repository_lock(repository_id):
            self._repository_advance_started()
            try:
                for run_id in self._ordered_run_ids(repository_id):
                    self._advance(run_id)
                    self._clear_focus_if_idle(run_id)
            finally:
                self._repository_advance_finished()

    def _repository_lock(self, repository_id: str) -> threading.Lock:
        with self._repository_locks_guard:
            return self._repository_locks.setdefault(repository_id, threading.Lock())

    def _repository_advance_started(self) -> None:
        with self._advancing_lock:
            self._advancing_repository_count += 1
            self._advancing_runs.set()

    def _repository_advance_finished(self) -> None:
        with self._advancing_lock:
            self._advancing_repository_count -= 1
            if self._advancing_repository_count == 0:
                self._advancing_runs.clear()

    def _ordered_run_rows(
        self, repository_id: str | None = None
    ) -> tuple[sqlite3.Row, ...]:
        with self.database.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET force_requested_at=NULL
                   WHERE force_requested_at IS NOT NULL
                     AND state NOT IN (
                         'queued', 'implementing', 'validating', 'publishing',
                         'resolving_feedback'
                     )""")
            if repository_id is not None:
                focused = connection.execute(
                    """SELECT runs.id, runs.repository_id
                       FROM runs
                       JOIN repositories
                         ON repositories.id=runs.repository_id
                       WHERE runs.repository_id=?
                         AND runs.force_requested_at IS NOT NULL
                         AND repositories.enabled=1
                         AND repositories.removed_at IS NULL
                       LIMIT 1""",
                    (repository_id,),
                ).fetchone()
                if focused is not None:
                    return (focused,)
            rows = connection.execute(
                """SELECT runs.id, runs.repository_id FROM runs
                   JOIN repositories
                     ON repositories.id=runs.repository_id
                   WHERE runs.state NOT IN ('blocked', 'canceled', 'closed')
                     AND repositories.enabled=1
                     AND repositories.removed_at IS NULL
                     AND (? IS NULL OR runs.repository_id=?)
                   ORDER BY runs.priority, runs.created_at, runs.id""",
                (repository_id, repository_id),
            ).fetchall()
        return tuple(rows)

    def _ordered_run_ids(self, repository_id: str | None = None) -> tuple[str, ...]:
        return tuple(str(row["id"]) for row in self._ordered_run_rows(repository_id))

    def _ordered_repository_ids(self) -> tuple[str, ...]:
        repository_ids: list[str] = []
        seen: set[str] = set()
        for row in self._ordered_run_rows():
            repository_id = str(row["repository_id"])
            if repository_id not in seen:
                seen.add(repository_id)
                repository_ids.append(repository_id)
        return tuple(repository_ids)

    def _clear_focus_if_idle(self, run_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET force_requested_at=NULL
                   WHERE id=?
                     AND force_requested_at IS NOT NULL
                     AND state NOT IN (
                         'queued', 'implementing', 'validating', 'publishing',
                         'resolving_feedback'
                     )""",
                (run_id,),
            )

    def poll_activations_while_advancing(self) -> None:
        if self._advancing_runs.is_set():
            self._poll_ready_repositories()

    def _poll_ready_repositories(self) -> None:
        with self._activation_poll_lock:
            with self.database.connect() as connection:
                repositories = connection.execute("""SELECT id FROM repositories
                       WHERE onboarding_state='ready'
                         AND enabled=1
                         AND removed_at IS NULL
                       ORDER BY created_at, id""").fetchall()
            for repository in repositories:
                repository_id = str(repository["id"])
                try:
                    self.lifecycle.poll_repository(repository_id)
                except Exception as error:
                    self.last_errors.append(
                        f"poll {repository_id}: {error or error.__class__.__name__}"
                    )

    def poll_feedback(self) -> None:
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT runs.id
                   FROM runs
                   JOIN pull_requests ON pull_requests.run_id=runs.id
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.state NOT IN ('canceled', 'closed')
                     AND repositories.onboarding_state='ready'
                     AND repositories.enabled=1
                     AND repositories.removed_at IS NULL
                   ORDER BY runs.priority, runs.created_at, runs.id""").fetchall()
        for row in rows:
            run_id = str(row["id"])
            try:
                self.feedback.poll_run(run_id)
            except Exception as error:
                detail, _ = _display_run_reason(str(error) or error.__class__.__name__)
                self.last_errors.append(f"feedback poll {run_id}: {detail}")

    def poll_issue_revisions(self) -> None:
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT runs.id
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.state NOT IN ('canceled', 'closed')
                     AND repositories.onboarding_state='ready'
                     AND repositories.enabled=1
                     AND repositories.removed_at IS NULL
                   ORDER BY runs.priority, runs.created_at, runs.id""").fetchall()
        for row in rows:
            run_id = str(row["id"])
            try:
                self.lifecycle.poll_issue_revision(run_id)
            except Exception as error:
                detail, _ = _display_run_reason(str(error) or error.__class__.__name__)
                self.last_errors.append(f"issue poll {run_id}: {detail}")

    def _advance(self, run_id: str) -> None:
        control = getattr(self.lifecycle, "external_effect", None)
        if not callable(control):
            self._advance_enabled(run_id)
            return
        typed_control = cast(
            Callable[[str], AbstractContextManager[bool]],
            control,
        )
        with typed_control(run_id) as enabled:
            if enabled:
                self._advance_enabled(run_id)

    def _advance_enabled(self, run_id: str) -> None:
        for _ in range(10):
            if not self._run_is_enabled(run_id):
                return
            run = self.lifecycle.get_run(run_id)
            state = str(run["state"])
            try:
                if state in {
                    RunState.QUEUED.value,
                    RunState.IMPLEMENTING.value,
                    RunState.VALIDATING.value,
                }:
                    if (
                        state != RunState.QUEUED.value
                        and self._has_processing_feedback(run_id)
                    ):
                        self.feedback.resolve_run(run_id)
                    else:
                        self.execution.execute(run_id)
                elif state == RunState.PUBLISHING.value:
                    if self._has_processing_feedback(run_id):
                        self.feedback.resolve_run(run_id)
                    else:
                        self.publication.publish(run_id)
                elif state in {
                    RunState.WAITING_FOR_FEEDBACK.value,
                    RunState.RESOLVING_FEEDBACK.value,
                }:
                    self.feedback.resolve_run(run_id)
                else:
                    return
            except RunPaused:
                return
            except Exception as error:
                detail = f"orchestration failed: {error or error.__class__.__name__}"
                self.last_errors.append(f"run {run_id}: {detail}")
                return
            after = str(self.lifecycle.get_run(run_id)["state"])
            if after == state or after in _TERMINAL_OR_IDLE:
                return

    def _run_is_enabled(self, run_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT repositories.enabled, repositories.removed_at
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        return row is not None and bool(row["enabled"]) and row["removed_at"] is None

    def _has_processing_feedback(self, run_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT 1
                   FROM feedback_versions
                   JOIN pull_requests
                     ON pull_requests.id=feedback_versions.pull_request_id
                   WHERE pull_requests.run_id=?
                     AND feedback_versions.state='processing'
                   LIMIT 1""",
                (run_id,),
            ).fetchone()
        return row is not None


class Scheduler:
    def __init__(self, orchestrator: Orchestrator, *, interval: float = 10.0) -> None:
        if interval <= 0:
            raise ValueError("scheduler interval must be positive")
        self.orchestrator = orchestrator
        self.interval = interval
        self._wake = threading.Event()
        self._activation_wake = threading.Event()
        self._feedback_wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._activation_thread: threading.Thread | None = None
        self._feedback_thread: threading.Thread | None = None
        self._repository_threads_lock = threading.Lock()
        self._repository_threads: dict[str, threading.Thread] = {}
        self._pending_repository_starts: set[str] = set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="repogents-scheduler",
            daemon=True,
        )
        self._activation_thread = threading.Thread(
            target=self._run_activation_poll,
            name="repogents-activation-poller",
            daemon=True,
        )
        self._feedback_thread = threading.Thread(
            target=self._run_feedback_poll,
            name="repogents-feedback-poller",
            daemon=True,
        )
        self._thread.start()
        self._activation_thread.start()
        self._feedback_thread.start()
        self.request_tick()

    def request_tick(self) -> None:
        self._wake.set()
        self._activation_wake.set()
        self._feedback_wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._activation_wake.set()
        self._feedback_wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._activation_thread is not None:
            self._activation_thread.join(timeout=5)
        if self._feedback_thread is not None:
            self._feedback_thread.join(timeout=5)
        with self._repository_threads_lock:
            repository_threads = tuple(self._repository_threads.values())
        deadline = time.monotonic() + 5
        for thread in repository_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                repository_ids = self.orchestrator.prepare_tick()
                self._start_repository_threads(repository_ids)
            except Exception as error:
                self.orchestrator.last_errors.append(
                    f"scheduler: {error or error.__class__.__name__}"
                )

    def _start_repository_threads(self, repository_ids: tuple[str, ...]) -> None:
        for repository_id in repository_ids:
            if self._stop.is_set():
                return
            with self._repository_threads_lock:
                existing = self._repository_threads.get(repository_id)
                if existing is not None and existing.is_alive():
                    self._pending_repository_starts.add(repository_id)
                    continue
                thread = threading.Thread(
                    target=self._run_repository,
                    args=(repository_id,),
                    name=f"repogents-repository-{repository_id}",
                    daemon=True,
                )
                self._repository_threads[repository_id] = thread
                thread.start()

    def _run_repository(self, repository_id: str) -> None:
        try:
            self.orchestrator.advance_repository(repository_id)
        except Exception as error:
            self.orchestrator.last_errors.append(
                "repository lane "
                f"{repository_id}: {error or error.__class__.__name__}"
            )
        finally:
            reschedule = False
            with self._repository_threads_lock:
                current = self._repository_threads.get(repository_id)
                if current is threading.current_thread():
                    self._repository_threads.pop(repository_id, None)
                    reschedule = repository_id in self._pending_repository_starts
                    self._pending_repository_starts.discard(repository_id)
            if reschedule and not self._stop.is_set():
                self._wake.set()

    def _run_activation_poll(self) -> None:
        while not self._stop.is_set():
            self._activation_wake.wait(self.interval)
            self._activation_wake.clear()
            if self._stop.is_set():
                return
            try:
                self.orchestrator.poll_activations_while_advancing()
            except Exception as error:
                self.orchestrator.last_errors.append(
                    "activation poll: " f"{error or error.__class__.__name__}"
                )

    def _run_feedback_poll(self) -> None:
        while not self._stop.is_set():
            self._feedback_wake.wait(self.interval)
            self._feedback_wake.clear()
            if self._stop.is_set():
                return
            try:
                self.orchestrator.poll_issue_revisions()
                self.orchestrator.poll_feedback()
                self._wake.set()
            except Exception as error:
                detail, _ = _display_run_reason(str(error) or error.__class__.__name__)
                self.orchestrator.last_errors.append(f"feedback poller: {detail}")


class ApplicationActions:
    def __init__(
        self,
        *,
        database: Database,
        onboarding: OnboardingOperations,
        lifecycle: LifecycleOperations,
        scheduler: SchedulerControl,
        model_configuration: ModelProviderConfiguration | None = None,
        known_secret_values: Callable[[str], tuple[str, ...]] | None = None,
    ) -> None:
        self.database = database
        self.onboarding = onboarding
        self.lifecycle = lifecycle
        self.scheduler = scheduler
        self.model_configuration = model_configuration
        self.known_secret_values = known_secret_values or (lambda _run_id: ())

    def state(self) -> dict[str, object]:
        with self.database.connect() as connection:
            repositories = connection.execute("""SELECT repositories.id,
                          repositories.owner || '/' || repositories.name AS identity,
                          repositories.url, repositories.default_branch,
                          repositories.enabled, repositories.updated_at,
                          repositories.onboarding_state, repositories.inputs_json,
                          repositories.blocking_reason,
                          repositories.current_sandbox_version_id AS sandbox_version_id,
                          repositories.current_team_version_id AS team_version_id,
                          sandbox_versions.version AS sandbox_version,
                          team_versions.version AS team_version
                   FROM repositories
                   LEFT JOIN sandbox_versions
                     ON sandbox_versions.id=repositories.current_sandbox_version_id
                   LEFT JOIN team_versions
                     ON team_versions.id=repositories.current_team_version_id
                   WHERE repositories.removed_at IS NULL
                   ORDER BY repositories.owner COLLATE NOCASE,
                            repositories.name COLLATE NOCASE""").fetchall()
            runs = connection.execute("""SELECT runs.id, runs.repository_id,
                          repositories.owner || '/' || repositories.name AS repository,
                          issues.number AS issue_number,
                          issues.title AS issue_title,
                          issues.url AS issue_url,
                          runs.state, runs.last_completed_state, runs.reason,
                          runs.base_sha, runs.validated_sha,
                          runs.sandbox_version_id, runs.team_version_id,
                          run_sandbox.version AS sandbox_version,
                          run_team.version AS team_version,
                          runs.priority, runs.force_requested_at,
                          runs.created_at, runs.updated_at,
                          pull_requests.number AS pull_number,
                          pull_requests.url AS pull_url
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN sandbox_versions AS run_sandbox
                     ON run_sandbox.id=runs.sandbox_version_id
                   JOIN team_versions AS run_team
                     ON run_team.id=runs.team_version_id
                   LEFT JOIN pull_requests ON pull_requests.run_id=runs.id
                   WHERE repositories.removed_at IS NULL
                   ORDER BY runs.priority, runs.created_at, runs.id""").fetchall()
            validations = connection.execute(
                """SELECT run_id, validation_command_id, commit_sha,
                          command_json, started_at, completed_at, exit_status,
                          log_path, verdict, findings_json, comparison_json
                   FROM validation_results
                   ORDER BY started_at, id"""
            ).fetchall()
            baselines = connection.execute("""SELECT validation_baselines.run_id,
                          validation_baselines.validation_command_id,
                          validation_baselines.base_sha,
                          validation_baselines.mode,
                          validation_baselines.started_at,
                          validation_baselines.completed_at,
                          validation_baselines.exit_status,
                          validation_baselines.log_path,
                          validation_baselines.findings_json,
                          validation_baselines.command_json
                   FROM validation_baselines
                   ORDER BY validation_baselines.started_at,
                            validation_baselines.id""").fetchall()
            assignments = connection.execute(
                """SELECT agent_assignments.run_id, team_members.stable_key,
                          team_members.atomic_role AS role,
                          team_members.role AS execution_class,
                          agent_assignments.reasoning, agent_assignments.assigned_at
                   FROM agent_assignments
                   JOIN team_members
                     ON team_members.id=agent_assignments.team_member_id
                   ORDER BY agent_assignments.assigned_at, agent_assignments.id"""
            ).fetchall()
            members = connection.execute("""SELECT repositories.id AS repository_id,
                          team_versions.id AS team_id,
                          team_versions.version AS team_version,
                          team_members.stable_key,
                          team_members.atomic_role AS role,
                          team_members.role AS execution_class,
                          team_members.responsibilities, team_members.runtime,
                          team_members.model, team_members.instructions
                   FROM repositories
                   JOIN team_versions
                     ON team_versions.id=repositories.current_team_version_id
                   JOIN team_members
                     ON team_members.team_version_id=team_versions.id
                   WHERE repositories.removed_at IS NULL
                   ORDER BY repositories.id,
                            CASE team_members.role
                              WHEN 'lead' THEN 0
                              WHEN 'scout' THEN 1
                              WHEN 'implementer' THEN 2
                              ELSE 3
                            END,
                            team_members.stable_key""").fetchall()
            transition_activity = connection.execute("""SELECT runs.repository_id,
                          MAX(run_transitions.occurred_at) AS occurred_at
                   FROM run_transitions
                   JOIN runs ON runs.id=run_transitions.run_id
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE repositories.removed_at IS NULL
                   GROUP BY runs.repository_id""").fetchall()
        baseline_by_run: dict[str, list[dict[str, object]]] = {}
        for row in baselines:
            value = dict(row)
            run_id = str(value.pop("run_id"))
            value["command"] = json.loads(str(value.pop("command_json")))
            value["findings"] = json.loads(str(value.pop("findings_json")))
            baseline_by_run.setdefault(run_id, []).append(value)
        validation_by_run: dict[str, list[dict[str, object]]] = {}
        for row in validations:
            value = dict(row)
            run_id = str(value.pop("run_id"))
            value["command"] = json.loads(str(value.pop("command_json")))
            raw_findings = value.pop("findings_json")
            raw_comparison = value.pop("comparison_json")
            value["findings"] = (
                json.loads(str(raw_findings)) if raw_findings is not None else []
            )
            value["comparison"] = (
                json.loads(str(raw_comparison)) if raw_comparison is not None else {}
            )
            validation_by_run.setdefault(run_id, []).append(value)
        assignments_by_run: dict[str, list[dict[str, object]]] = {}
        for row in assignments:
            value = dict(row)
            run_id = str(value.pop("run_id"))
            assignments_by_run.setdefault(run_id, []).append(value)
        all_run_values = [dict(row) for row in runs]
        visible_run_values: list[dict[str, object]] = []
        runs_by_repository: dict[str, list[dict[str, object]]] = {}
        for run in all_run_values:
            run_id = str(run["id"])
            repository_id = str(run["repository_id"])
            run["priority"] = int(run["priority"])
            runs_by_repository.setdefault(repository_id, []).append(run)
            if str(run["state"]) in _TERMINAL_RUN_STATES:
                continue
            run["reason"], run["reason_truncated"] = _display_run_reason(run["reason"])
            run["reason_severity"] = (
                "error" if str(run["state"]) == RunState.BLOCKED.value else "neutral"
            )
            run["forced"] = run.pop("force_requested_at") is not None
            run["validation_baselines"] = baseline_by_run.get(run_id, [])
            run["validation_results"] = validation_by_run.get(run_id, [])
            run["assignments"] = assignments_by_run.get(run_id, [])
            acceptance = current_acceptance_verification(
                self.database,
                run_id,
            )
            run["acceptance_verification"] = (
                _display_acceptance_verification(acceptance)
                if acceptance is not None
                else None
            )
            visible_run_values.append(run)
        for queue_position, run in enumerate(visible_run_values, start=1):
            run["queue_position"] = queue_position
        teams_by_repository: dict[str, dict[str, object]] = {}
        for row in members:
            repository_id = str(row["repository_id"])
            team = teams_by_repository.setdefault(
                repository_id,
                {
                    "id": str(row["team_id"]),
                    "version": int(row["team_version"]),
                    "members": [],
                },
            )
            team_members = team["members"]
            assert isinstance(team_members, list)
            team_members.append(
                {
                    "stable_key": row["stable_key"],
                    "role": row["role"],
                    "execution_class": row["execution_class"],
                    "responsibilities": row["responsibilities"],
                    "runtime": row["runtime"],
                    "model": row["model"],
                    "instructions": row["instructions"],
                }
            )
        latest_transition = {
            str(row["repository_id"]): str(row["occurred_at"])
            for row in transition_activity
            if row["occurred_at"] is not None
        }
        with self.database.connect() as connection:
            discovery_rows = connection.execute(
                "SELECT * FROM ready_issue_discovery"
            ).fetchall()
        discovery_by_repository = {
            str(row["repository_id"]): row for row in discovery_rows
        }
        repository_values: list[dict[str, object]] = []
        ready_issues: list[dict[str, object]] = []
        ready_issue_discovery: list[dict[str, object]] = []
        for row in repositories:
            value = dict(row)
            repository_id = str(value["id"])
            repository_runs = runs_by_repository.get(repository_id, [])
            visible_repository_runs = [
                run
                for run in repository_runs
                if str(run["state"]) not in _TERMINAL_RUN_STATES
            ]
            active_runs = [
                run
                for run in visible_repository_runs
                if str(run["state"]) not in _TERMINAL_OR_IDLE
            ]
            latest_run = visible_repository_runs[0] if visible_repository_runs else None
            activity_times = [str(value.pop("updated_at"))]
            transition_time = latest_transition.get(repository_id)
            if transition_time is not None:
                activity_times.append(transition_time)
            activity_times.extend(str(run["updated_at"]) for run in repository_runs)
            value["enabled"] = bool(value["enabled"])
            value["active"] = bool(active_runs)
            value["active_run_count"] = len(active_runs)
            value["latest_run_state"] = (
                str(latest_run["state"]) if latest_run is not None else None
            )
            value["latest_activity_at"] = max(activity_times)
            value["team"] = teams_by_repository.get(repository_id)
            value["display_inputs"] = _display_repository_inputs(
                str(value.pop("inputs_json"))
            )
            repository_values.append(value)
            discovery = discovery_by_repository.get(repository_id)
            repository_ready = str(value["onboarding_state"]) == "ready"
            repository_discoverable = repository_ready and bool(value["enabled"])
            if not repository_ready:
                discovery_unavailable_error = (
                    f"Repository onboarding is {value['onboarding_state']}"
                )
            elif not value["enabled"]:
                discovery_unavailable_error = "Repository is paused"
            else:
                discovery_unavailable_error = "Ready-issue discovery has not run yet"
            if discovery is None:
                ready_issue_discovery.append(
                    {
                        "repository_id": repository_id,
                        "repository": value["identity"],
                        "status": "unavailable",
                        "last_success_at": None,
                        "last_attempt_at": None,
                        "error": discovery_unavailable_error,
                    }
                )
                continue
            discovery_status = str(discovery["status"])
            discovery_error = discovery["error"]
            if not repository_discoverable:
                discovery_status = (
                    "stale" if discovery["last_success_at"] is not None else "unavailable"
                )
                discovery_error = discovery_unavailable_error
            ready_issue_discovery.append(
                {
                    "repository_id": repository_id,
                    "repository": value["identity"],
                    "status": discovery_status,
                    "last_success_at": discovery["last_success_at"],
                    "last_attempt_at": discovery["last_attempt_at"],
                    "error": discovery_error,
                }
            )
            if not repository_discoverable or discovery_status != "available":
                continue
            for issue in json.loads(str(discovery["issues_json"])):
                ready_issues.append(
                    {
                        "repository_id": repository_id,
                        "repository": value["identity"],
                        "number": issue["number"],
                        "title": issue["title"],
                        "url": issue["url"],
                        "updated_at": issue["updated_at"],
                    }
                )
        state: dict[str, object] = {
            "repositories": repository_values,
            "ready_issues": ready_issues,
            "ready_issue_discovery": ready_issue_discovery,
            "runs": visible_run_values,
        }
        if self.model_configuration is not None:
            state["model_configuration"] = self.model_configuration.public_state()
        return state

    def configure_model(self, values: dict[str, object]) -> dict[str, object]:
        if self.model_configuration is None:
            raise RuntimeError("model provider configuration is unavailable")
        return self.model_configuration.update(values)

    def model_catalog(self) -> dict[str, object]:
        if self.model_configuration is None:
            raise RuntimeError("model provider configuration is unavailable")
        return self.model_configuration.model_catalog()

    def acceptance_artifact(self, artifact_id: str) -> tuple[bytes, str]:
        return load_acceptance_artifact(self.database, artifact_id)

    def activity_revision(self) -> int:
        return self.database.activity_revision

    def wait_for_activity_change(self, revision: int, timeout: float) -> int:
        return self.database.wait_for_activity_change(revision, timeout)

    def repository_log(self, repository_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            repository = connection.execute(
                """SELECT id, onboarding_state, blocking_reason, enabled, updated_at
                   FROM repositories
                   WHERE id=? AND removed_at IS NULL""",
                (repository_id,),
            ).fetchone()
            if repository is None:
                raise KeyError(repository_id)
            run = connection.execute(
                """SELECT id, state, run_path, created_at, updated_at
                   FROM runs
                   WHERE repository_id=?
                   ORDER BY updated_at DESC, created_at DESC, id DESC
                   LIMIT 1""",
                (repository_id,),
            ).fetchone()
            transitions = connection.execute(
                """SELECT run_transitions.id, run_transitions.run_id,
                          run_transitions.from_state, run_transitions.to_state,
                          run_transitions.reason, run_transitions.occurred_at
                   FROM run_transitions
                   JOIN runs ON runs.id=run_transitions.run_id
                   WHERE runs.repository_id=?
                   ORDER BY run_transitions.id DESC
                   LIMIT 200""",
                (repository_id,),
            ).fetchall()
        repository_state = str(repository["onboarding_state"])
        repository_message = f"Repository is {repository_state}"
        if not bool(repository["enabled"]):
            repository_message += " and paused"
        if repository["blocking_reason"]:
            repository_message += f" — {repository['blocking_reason']}"
        entries: list[dict[str, object]] = [
            {
                "id": "repository",
                "kind": "repository",
                "timestamp": str(repository["updated_at"]),
                "message": repository_message,
            }
        ]
        for transition in reversed(transitions):
            source = transition["from_state"] or "created"
            message = f"{source} → {transition['to_state']}"
            if transition["reason"]:
                message += f" — {transition['reason']}"
            entries.append(
                {
                    "id": f"transition-{transition['id']}",
                    "kind": "transition",
                    "run_id": transition["run_id"],
                    "state": transition["to_state"],
                    "timestamp": transition["occurred_at"],
                    "message": message,
                }
            )
        if run is not None:
            self._append_action_history(
                entries,
                run_id=str(run["id"]),
                run_path=run["run_path"],
                updated_at=str(run["updated_at"]),
            )
        bounded_entries = entries[-200:]
        return {
            "repository_id": repository_id,
            "run_id": str(run["id"]) if run is not None else None,
            "active": (run is not None and str(run["state"]) not in _TERMINAL_OR_IDLE),
            "updated_at": max(str(entry["timestamp"]) for entry in bounded_entries),
            "entries": bounded_entries,
        }

    def run_log(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            run = connection.execute(
                """SELECT runs.id AS run_id, runs.state, runs.reason,
                          runs.run_path, runs.created_at, runs.updated_at,
                          repositories.id AS repository_id,
                          repositories.owner, repositories.name,
                          issues.id AS issue_id, issues.number AS issue_number,
                          issues.title AS issue_title, issues.url AS issue_url
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   JOIN issues ON issues.id=runs.issue_id
                   WHERE runs.id=? AND repositories.removed_at IS NULL""",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            transitions = connection.execute(
                """SELECT id, from_state, to_state, reason, occurred_at
                   FROM run_transitions
                   WHERE run_id=?
                   ORDER BY id DESC
                   LIMIT 200""",
                (run_id,),
            ).fetchall()
        state = str(run["state"])
        message = f"Issue #{run['issue_number']} run is {state}"
        if run["reason"]:
            message += f" — {run['reason']}"
        entries: list[dict[str, object]] = [
            {
                "id": f"run-{run_id}",
                "kind": "run",
                "run_id": run_id,
                "state": state,
                "timestamp": str(run["updated_at"]),
                "message": message,
            }
        ]
        for transition in reversed(transitions):
            source = transition["from_state"] or "created"
            transition_message = f"{source} → {transition['to_state']}"
            if transition["reason"]:
                transition_message += f" — {transition['reason']}"
            entries.append(
                {
                    "id": f"transition-{transition['id']}",
                    "kind": "transition",
                    "run_id": run_id,
                    "state": transition["to_state"],
                    "timestamp": transition["occurred_at"],
                    "message": transition_message,
                }
            )
        self._append_action_history(
            entries,
            run_id=run_id,
            run_path=run["run_path"],
            updated_at=str(run["updated_at"]),
        )
        bounded_entries = entries[-200:]
        return {
            "run_id": run_id,
            "repository_id": str(run["repository_id"]),
            "repository": f"{run['owner']}/{run['name']}",
            "issue": {
                "id": str(run["issue_id"]),
                "number": int(run["issue_number"]),
                "title": str(run["issue_title"]),
                "url": str(run["issue_url"]),
            },
            "state": state,
            "active": state not in _TERMINAL_OR_IDLE,
            "updated_at": max(str(entry["timestamp"]) for entry in bounded_entries),
            "entries": bounded_entries,
        }

    def _append_action_history(
        self,
        entries: list[dict[str, object]],
        *,
        run_id: str,
        run_path: object,
        updated_at: str,
    ) -> None:
        if not run_path:
            return
        root = Path(str(run_path))
        history_path = root / "agent-state" / "action-history.json"
        if root.is_symlink() or history_path.is_symlink():
            entries.append(
                {
                    "id": f"agent-history-warning-{run_id}",
                    "kind": "warning",
                    "run_id": run_id,
                    "timestamp": updated_at,
                    "message": "Stored agent action history is not a regular path.",
                }
            )
            return
        if not history_path.exists():
            return
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
            if not isinstance(history, list) or not all(
                isinstance(item, str) for item in history
            ):
                raise ValueError("invalid action history")
        except (OSError, ValueError, json.JSONDecodeError):
            entries.append(
                {
                    "id": f"agent-history-warning-{run_id}",
                    "kind": "warning",
                    "run_id": run_id,
                    "timestamp": updated_at,
                    "message": "Stored agent action history is unreadable.",
                }
            )
            return
        try:
            secret_values = self.known_secret_values(run_id)
        except (KeyError, RuntimeError, TypeError, ValueError):
            entries.append(
                {
                    "id": f"agent-history-warning-{run_id}",
                    "kind": "warning",
                    "run_id": run_id,
                    "timestamp": updated_at,
                    "message": (
                        "Stored agent action history cannot be safely redacted."
                    ),
                }
            )
            return
        for index, history_message in enumerate(history[-24:], start=1):
            entries.append(
                {
                    "id": f"agent-{run_id}-{index}",
                    "kind": "agent",
                    "run_id": run_id,
                    "timestamp": updated_at,
                    "message": redact_text(history_message, secret_values)[:8000],
                }
            )

    def set_repository_enabled(self, repository_id: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self.lifecycle.set_repository_paused(repository_id, not enabled)
        self.scheduler.request_tick()

    def reorder_runs(self, run_ids: list[str]) -> None:
        if (
            not isinstance(run_ids, list)
            or not all(isinstance(run_id, str) and run_id for run_id in run_ids)
            or len(run_ids) != len(set(run_ids))
        ):
            raise ValueError("run_ids must be a list of unique nonempty strings")
        with self.database.transaction() as connection:
            rows = connection.execute("""SELECT runs.id, runs.state
                   FROM runs
                   JOIN repositories
                     ON repositories.id=runs.repository_id
                   WHERE repositories.removed_at IS NULL
                     AND runs.state NOT IN ('canceled', 'closed')
                   ORDER BY runs.priority, runs.created_at, runs.id""").fetchall()
            visible_ids = [
                str(row["id"]) for row in rows if str(row["state"]) != "blocked"
            ]
            unknown = [run_id for run_id in run_ids if run_id not in visible_ids]
            if unknown:
                raise KeyError(unknown[0])
            if set(run_ids) != set(visible_ids):
                raise ValueError("run_ids must include every visible run exactly once")
            requested = iter(run_ids)
            ordered_ids = [
                str(row["id"])
                if str(row["state"]) == "blocked"
                else next(requested)
                for row in rows
            ]
            connection.executemany(
                "UPDATE runs SET priority=? WHERE id=?",
                ((priority, run_id) for priority, run_id in enumerate(ordered_ids)),
            )
        self.scheduler.request_tick()

    def set_run_forced(self, run_id: str, forced: bool) -> None:
        if not isinstance(forced, bool):
            raise ValueError("forced must be a boolean")
        with self.database.transaction() as connection:
            run = connection.execute(
                """SELECT runs.id, runs.state, runs.repository_id
                   FROM runs
                   JOIN repositories
                     ON repositories.id=runs.repository_id
                   WHERE runs.id=?
                     AND repositories.removed_at IS NULL""",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if forced and str(run["state"]) not in _FOCUSABLE_RUN_STATES:
                raise ValueError("only an actionable run can be forced")
            if forced:
                rows = connection.execute("""SELECT runs.id
                       FROM runs
                       JOIN repositories
                         ON repositories.id=runs.repository_id
                       WHERE repositories.removed_at IS NULL
                         AND runs.state NOT IN ('canceled', 'closed')
                       ORDER BY runs.priority, runs.created_at, runs.id""").fetchall()
                ordered_ids = [run_id]
                ordered_ids.extend(
                    str(row["id"]) for row in rows if str(row["id"]) != run_id
                )
                connection.executemany(
                    "UPDATE runs SET priority=? WHERE id=?",
                    (
                        (priority, ordered_id)
                        for priority, ordered_id in enumerate(ordered_ids)
                    ),
                )
                connection.execute(
                    """UPDATE runs SET force_requested_at=NULL
                       WHERE repository_id=?""",
                    (str(run["repository_id"]),),
                )
                connection.execute(
                    """UPDATE runs
                       SET force_requested_at=strftime(
                           '%Y-%m-%dT%H:%M:%fZ', 'now'
                       )
                       WHERE id=?""",
                    (run_id,),
                )
            else:
                connection.execute(
                    "UPDATE runs SET force_requested_at=NULL WHERE id=?",
                    (run_id,),
                )
        self.scheduler.request_tick()

    def remove_repository(self, repository_id: str) -> None:
        with self.database.transaction() as connection:
            repository = connection.execute(
                "SELECT id FROM repositories WHERE id=? AND removed_at IS NULL",
                (repository_id,),
            ).fetchone()
            if repository is None:
                raise KeyError(repository_id)
            active_run = connection.execute(
                """SELECT id FROM runs
                   WHERE repository_id=?
                     AND state NOT IN ('canceled', 'closed')
                   ORDER BY created_at DESC
                   LIMIT 1""",
                (repository_id,),
            ).fetchone()
            if active_run is not None:
                raise RuntimeError(
                    "repository has an active run; cancel or finish it before removal"
                )
            connection.execute(
                """UPDATE repositories
                   SET enabled=0,
                       ready_issue_generation=ready_issue_generation + 1,
                       removed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                       updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE id=?""",
                (repository_id,),
            )
            connection.execute(
                """UPDATE ready_issue_discovery
                   SET status=CASE
                         WHEN last_success_at IS NULL THEN 'unavailable'
                         ELSE 'stale'
                       END,
                       last_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                       error='Repository was removed; ready issues require refresh'
                   WHERE repository_id=?""",
                (repository_id,),
            )
        self.scheduler.request_tick()

    def add_repository(self, identity: str, inputs: dict[str, object]) -> str:
        repository_id = self.onboarding.onboard(identity, inputs)
        self.scheduler.request_tick()
        return str(repository_id)

    def reonboard(self, repository_id: str, inputs: dict[str, object]) -> str:
        version_id = self.onboarding.reonboard(repository_id, inputs)
        self.scheduler.request_tick()
        return str(version_id)

    def cancel(self, run_id: str) -> None:
        self.lifecycle.cancel(run_id, "canceled by user")

    def poll(self) -> None:
        self.scheduler.request_tick()


@dataclass
class RuntimeComponents:
    database: Database
    github: GitHubClient
    sandbox: SandboxManager
    onboarding: OnboardingService
    lifecycle: RunLifecycle
    execution: ExecutionService
    acceptance: AcceptanceService
    publication: PublicationService
    feedback: FeedbackService
    orchestrator: Orchestrator
    scheduler: Scheduler
    actions: ApplicationActions
    model_configuration: ModelProviderConfiguration


def build_runtime(
    data_root: Path,
    *,
    github_token: str | None = None,
    model: str | None = None,
    model_base_url: str | None = None,
    poll_interval: float = 10.0,
) -> RuntimeComponents:
    secret_resolver = EnvironmentSecretResolver()
    root = Path(data_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    model_configuration = ModelProviderConfiguration(
        root,
        bootstrap_model=model,
        bootstrap_api_endpoint=model_base_url,
    )
    public_model_configuration = model_configuration.public_state()
    configured_model = public_model_configuration["default_model"]
    resolved_model = configured_model if isinstance(configured_model, str) else None
    configured_endpoint = public_model_configuration["api_endpoint"]
    initial_base_url = (
        configured_endpoint if isinstance(configured_endpoint, str) else None
    )
    initial_api_key = (
        model_configuration.connection_for_model(resolved_model).api_key
        if resolved_model is not None
        else None
    )
    model_state_root = root / "model-state"
    database = Database(root / "repogents.sqlite3")
    database.initialize()
    github = GitHubClient(token=github_token)
    sandbox = SandboxManager()
    processes = RunProcessSupervisor()

    def connection_resolver(model_selector: str) -> tuple[str | None, str | None]:
        connection = model_configuration.connection_for_model(model_selector)
        return connection.api_endpoint, connection.api_key

    onboarding = OnboardingService(
        database=database,
        data_root=root,
        github=github,
        sources=GitSourceManager(token=github_token),
        inspector=RepositoryInspector(),
        evidence_analyzer=MiniSweRepositoryEvidenceAnalyzer(
            model=resolved_model,
            base_url=initial_base_url,
            api_key=initial_api_key,
            configuration_resolver=(
                model_configuration.default_inference_configuration
            ),
            state_root=model_state_root / "onboarding",
        ),
        team_formulator=EvidenceTeamFormulator(
            runtime=MINI_SWE_RUNTIME,
            model=resolved_model,
            model_resolver=model_configuration.model_for_role,
            configuration_resolver=(
                model_configuration.default_inference_configuration
            ),
            state_root=model_state_root / "teams",
        ),
        provisioner=SandboxEnvironmentProvisioner(
            data_root=root,
            sandbox=sandbox,
            secret_resolver=secret_resolver,
        ),
    )
    onboarding.recover_interrupted()
    lifecycle = RunLifecycle(
        database=database,
        data_root=root,
        github=github,
        checkouts=GitCheckoutManager(token=github_token),
        sandbox=sandbox,
        processes=processes,
    )

    def runtime_factory(
        runtime: str,
        stored_model: str,
        timeout: float,
    ) -> MiniSweModelRuntime:
        if runtime != MINI_SWE_RUNTIME:
            raise ValueError(f"unsupported stored model runtime: {runtime}")
        connection = model_configuration.connection_for_model(stored_model)
        return MiniSweModelRuntime(
            model=stored_model,
            base_url=connection.api_endpoint,
            api_key=connection.api_key,
            timeout=timeout,
        )

    teams = TeamService(database)
    execution = ExecutionService(
        database=database,
        lifecycle=lifecycle,
        teams=teams,
        sandbox=sandbox,
        runtime_factory=runtime_factory,
        secret_resolver=secret_resolver,
        process_supervisor=processes,
    )
    acceptance = AcceptanceService(
        database=database,
        lifecycle=lifecycle,
        teams=teams,
        sandbox=sandbox,
        data_root=root,
        runtime_factory=runtime_factory,
        secret_resolver=secret_resolver,
        process_supervisor=processes,
    )
    publication = PublicationService(
        database=database,
        lifecycle=lifecycle,
        gateway=GitPublicationGateway(github, token=github_token),
        scope_reviewer=MiniSweScopeReviewer(
            base_url=initial_base_url,
            api_key=initial_api_key,
            connection_resolver=connection_resolver,
            state_root=model_state_root / "scope-review",
            processes=processes,
        ),
        acceptance=acceptance,
        known_secret_values=lambda run_id: _run_secret_values(
            database, secret_resolver, run_id
        ),
    )
    feedback = FeedbackService(
        database=database,
        lifecycle=lifecycle,
        gateway=github,
        evaluator=MiniSweFeedbackEvaluator(
            base_url=initial_base_url,
            api_key=initial_api_key,
            connection_resolver=connection_resolver,
            state_root=model_state_root / "feedback",
            processes=processes,
        ),
        executor=execution,
        publisher=publication,
    )
    orchestrator = Orchestrator(
        database=database,
        lifecycle=lifecycle,
        execution=execution,
        publication=publication,
        feedback=feedback,
    )
    scheduler = Scheduler(orchestrator, interval=poll_interval)
    actions = ApplicationActions(
        database=database,
        onboarding=onboarding,
        lifecycle=lifecycle,
        scheduler=scheduler,
        model_configuration=model_configuration,
        known_secret_values=lambda run_id: _run_secret_values(
            database,
            secret_resolver,
            run_id,
        ),
    )
    return RuntimeComponents(
        database=database,
        github=github,
        sandbox=sandbox,
        onboarding=onboarding,
        lifecycle=lifecycle,
        execution=execution,
        acceptance=acceptance,
        publication=publication,
        feedback=feedback,
        orchestrator=orchestrator,
        scheduler=scheduler,
        actions=actions,
        model_configuration=model_configuration,
    )


def _display_acceptance_verification(
    value: dict[str, object],
) -> dict[str, object]:
    display = json.loads(json.dumps(value))
    artifacts = display.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact.pop("path", None)
            artifact_id = artifact.get("id")
            if isinstance(artifact_id, str):
                artifact["url"] = "/api/acceptance-artifacts/" + urllib.parse.quote(
                    artifact_id, safe=""
                )
    evidence = display.get("evidence")
    if isinstance(evidence, list):
        for observation in evidence:
            if not isinstance(observation, dict):
                continue
            observation["log_recorded"] = bool(observation.pop("log_path", None))
    return display


def _display_run_reason(value: object) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    reason = str(value)
    line_end = len(reason)
    for separator in ("\r", "\n"):
        position = reason.find(separator)
        if position >= 0:
            line_end = min(line_end, position)
    summary = reason[:line_end]
    truncated = line_end < len(reason)
    if len(summary) > _RUN_REASON_SUMMARY_LIMIT:
        summary = summary[:_RUN_REASON_SUMMARY_LIMIT] + "…"
        truncated = True
    return summary, truncated


def _display_repository_inputs(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    display: dict[str, object] = {}
    for key in (
        "allowed_host_paths",
        "allowed_services",
        "provisioning_commands",
        "validation_commands",
    ):
        if key in payload:
            display[key] = payload[key]
    bindings = payload.get("secret_bindings")
    if isinstance(bindings, list):
        display["secret_bindings"] = [
            {
                key: value[key]
                for key in ("name", "reference", "commands")
                if isinstance(value, dict) and key in value
            }
            for value in bindings
            if isinstance(value, dict)
        ]
    return display


def _run_secret_values(
    database: Database,
    resolver: Callable[[str], str],
    run_id: str,
) -> tuple[str, ...]:
    with database.connect() as connection:
        row = connection.execute(
            """SELECT sandbox_versions.policy_json
               FROM runs
               JOIN sandbox_versions
                 ON sandbox_versions.id=runs.sandbox_version_id
               WHERE runs.id=?""",
            (run_id,),
        ).fetchone()
    if row is None:
        raise KeyError(run_id)
    payload = json.loads(str(row["policy_json"]))
    bindings = payload.get("secret_bindings", [])
    if not isinstance(bindings, list):
        raise ValueError("stored secret bindings must be a list")
    values: list[str] = []
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(
            binding.get("reference"), str
        ):
            raise ValueError("stored secret binding reference is invalid")
        value = resolver(str(binding["reference"]))
        if value not in values:
            values.append(value)
    return tuple(values)
