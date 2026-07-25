from __future__ import annotations

import json
import urllib.parse
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .acceptance import (
    AcceptanceService,
    current_acceptance_verification,
    load_acceptance_artifact,
)
from .controller import (
    EnvironmentSecretResolver,
    RunProcessSupervisor,
    require_explicit_model,
)
from .database import Database
from .execution import ExecutionService, MiniSweModelRuntime
from .feedback import FeedbackService, MiniSweFeedbackEvaluator
from .github import GitHubClient, GitHubError
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
from .quiet import QuietPeriodService, TransientQuietCheckError
from .sandbox import SandboxManager
from .team import EvidenceTeamFormulator, TeamService

_TERMINAL_OR_IDLE = {
    RunState.BLOCKED.value,
    RunState.CANCELED.value,
    RunState.CLOSED.value,
}


class SchedulerControl(Protocol):
    def request_tick(self) -> None: ...


class LifecycleOperations(Protocol):
    def reconcile_nonterminal_runs(self) -> tuple[str, ...]: ...

    def poll_repository(self, repository_id: str) -> tuple[str, ...]: ...

    def get_run(self, run_id: str) -> dict[str, object]: ...

    def cancel(self, run_id: str, reason: str) -> None: ...


class ExecutionOperations(Protocol):
    def execute(
        self, run_id: str, *, additional_context: str | None = None
    ) -> str | None: ...


class PublicationOperations(Protocol):
    def publish(self, run_id: str) -> object | None: ...


class FeedbackOperations(Protocol):
    def resolve_run(self, run_id: str) -> int: ...


class QuietOperations(Protocol):
    def check_due(self, run_id: str) -> str | None: ...

    def list_notifications(self) -> list[dict[str, object]]: ...

    def acknowledge(self, notification_id: str) -> None: ...


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
        quiet: QuietOperations,
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle
        self.execution = execution
        self.publication = publication
        self.feedback = feedback
        self.quiet = quiet
        self._lock = threading.Lock()
        self.last_errors: list[str] = []

    def tick(self) -> None:
        with self._lock:
            self.last_errors = []
            try:
                self.lifecycle.reconcile_nonterminal_runs()
            except Exception as error:
                self.last_errors.append(
                    f"restart reconciliation: {error or error.__class__.__name__}"
                )
            with self.database.connect() as connection:
                repositories = connection.execute("""SELECT id FROM repositories
                       WHERE onboarding_state='ready'
                       ORDER BY created_at, id""").fetchall()
            for repository in repositories:
                repository_id = str(repository["id"])
                try:
                    self.lifecycle.poll_repository(repository_id)
                except Exception as error:
                    self.last_errors.append(
                        f"poll {repository_id}: {error or error.__class__.__name__}"
                    )
            with self.database.connect() as connection:
                runs = connection.execute("""SELECT id FROM runs
                       WHERE state NOT IN ('blocked', 'canceled', 'closed')
                       ORDER BY created_at, id""").fetchall()
            for row in runs:
                self._advance(str(row["id"]))

    def _advance(self, run_id: str) -> None:
        for _ in range(10):
            run = self.lifecycle.get_run(run_id)
            state = str(run["state"])
            try:
                if state in {
                    RunState.QUEUED.value,
                    RunState.IMPLEMENTING.value,
                    RunState.VALIDATING.value,
                }:
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
                elif state == RunState.QUIET_PERIOD.value:
                    try:
                        self.feedback.resolve_run(run_id)
                    except GitHubError as error:
                        raise TransientQuietCheckError(
                            run_id, "feedback poll"
                        ) from error
                    if str(self.lifecycle.get_run(run_id)["state"]) == state:
                        self.quiet.check_due(run_id)
                elif state == RunState.NOTIFIED.value:
                    self.feedback.resolve_run(run_id)
                else:
                    return
            except TransientQuietCheckError as error:
                self.last_errors.append(
                    f"run {run_id}: transient quiet check: "
                    f"{error or error.__class__.__name__}"
                )
                return
            except Exception as error:
                detail = f"orchestration failed: {error or error.__class__.__name__}"
                self.last_errors.append(f"run {run_id}: {detail}")
                return
            after = str(self.lifecycle.get_run(run_id)["state"])
            if after == state or after in _TERMINAL_OR_IDLE:
                return

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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="repogents-scheduler",
            daemon=True,
        )
        self._thread.start()
        self.request_tick()

    def request_tick(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.orchestrator.tick()
            except Exception as error:
                self.orchestrator.last_errors.append(
                    f"scheduler: {error or error.__class__.__name__}"
                )


class ApplicationActions:
    def __init__(
        self,
        *,
        database: Database,
        onboarding: OnboardingOperations,
        lifecycle: LifecycleOperations,
        quiet: QuietOperations,
        scheduler: SchedulerControl,
        github: GitHubClient | None = None,
    ) -> None:
        self.database = database
        self.onboarding = onboarding
        self.lifecycle = lifecycle
        self.quiet = quiet
        self.scheduler = scheduler
        self.github = github or self

    def list_ready_issues(self, owner: str, name: str) -> tuple[object, ...]:
        return ()

    def state(self) -> dict[str, object]:
        with self.database.connect() as connection:
            repositories = connection.execute("""SELECT repositories.id,
                          repositories.owner || '/' || repositories.name AS identity,
                          repositories.url, repositories.default_branch,
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
                   ORDER BY runs.created_at DESC""").fetchall()
            validations = connection.execute(
                """SELECT run_id, commit_sha, command_json, started_at,
                          completed_at, exit_status, log_path
                   FROM validation_results
                   ORDER BY started_at, id"""
            ).fetchall()
            assignments = connection.execute(
                """SELECT agent_assignments.run_id, team_members.stable_key,
                          team_members.role, agent_assignments.reasoning,
                          agent_assignments.assigned_at
                   FROM agent_assignments
                   JOIN team_members
                     ON team_members.id=agent_assignments.team_member_id
                   ORDER BY agent_assignments.assigned_at, agent_assignments.id"""
            ).fetchall()
            ready_issue_discoveries = connection.execute(
                """SELECT repository_id, status, issues_json, last_success_at,
                          last_attempt_at, error
                   FROM ready_issue_discovery"""
            ).fetchall()
        validation_by_run: dict[str, list[dict[str, object]]] = {}
        for row in validations:
            value = dict(row)
            run_id = str(value.pop("run_id"))
            value["command"] = json.loads(str(value.pop("command_json")))
            validation_by_run.setdefault(run_id, []).append(value)
        assignments_by_run: dict[str, list[dict[str, object]]] = {}
        for row in assignments:
            value = dict(row)
            run_id = str(value.pop("run_id"))
            assignments_by_run.setdefault(run_id, []).append(value)
        run_values = [dict(row) for row in runs]
        for run in run_values:
            run_id = str(run["id"])
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
        discovery_by_repository = {
            str(row["repository_id"]): dict(row)
            for row in ready_issue_discoveries
        }
        repository_values: list[dict[str, object]] = []
        ready_issues: list[dict[str, object]] = []
        ready_issue_discovery: list[dict[str, object]] = []
        for row in repositories:
            value = dict(row)
            value["display_inputs"] = _display_repository_inputs(
                str(value.pop("inputs_json"))
            )
            repository_values.append(value)
            repository_id = str(value["id"])
            discovery = discovery_by_repository.get(repository_id)
            if discovery is None:
                ready_issue_discovery.append(
                    {
                        "repository_id": repository_id,
                        "repository": value["identity"],
                        "status": "unavailable",
                        "last_success_at": None,
                        "last_attempt_at": None,
                        "error": "Ready-issue discovery has not run yet",
                    }
                )
                continue
            ready_issue_discovery.append(
                {
                    "repository_id": repository_id,
                    "repository": value["identity"],
                    "status": discovery["status"],
                    "last_success_at": discovery["last_success_at"],
                    "last_attempt_at": discovery["last_attempt_at"],
                    "error": discovery["error"],
                }
            )
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
        return {
            "repositories": repository_values,
            "ready_issues": ready_issues,
            "ready_issue_discovery": ready_issue_discovery,
            "runs": run_values,
            "notifications": self.quiet.list_notifications(),
        }

    def acceptance_artifact(self, artifact_id: str) -> tuple[bytes, str]:
        return load_acceptance_artifact(self.database, artifact_id)

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

    def acknowledge(self, notification_id: str) -> None:
        self.quiet.acknowledge(notification_id)

    def poll(self) -> None:
        self.scheduler.request_tick()


class _QuietStarterProxy:
    def __init__(self) -> None:
        self.service: QuietPeriodService | None = None

    def start(self, run_id: str) -> None:
        if self.service is None:
            raise RuntimeError("quiet-period service is not initialized")
        self.service.start(run_id)


class _FeedbackPollerProxy:
    def __init__(self) -> None:
        self.service: FeedbackService | None = None

    def poll_run(self, run_id: str) -> int:
        if self.service is None:
            raise RuntimeError("feedback service is not initialized")
        return self.service.poll_run(run_id)


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
    quiet: QuietPeriodService
    orchestrator: Orchestrator
    scheduler: Scheduler
    actions: ApplicationActions


def build_runtime(
    data_root: Path,
    *,
    github_token: str | None = None,
    model: str | None = None,
    model_base_url: str | None = None,
    poll_interval: float = 10.0,
) -> RuntimeComponents:
    resolved_model = require_explicit_model(model)
    secret_resolver = EnvironmentSecretResolver()
    root = Path(data_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    model_state_root = root / "model-state"
    database = Database(root / "repogents.sqlite3")
    database.initialize()
    github = GitHubClient(token=github_token)
    sandbox = SandboxManager()
    processes = RunProcessSupervisor()
    onboarding = OnboardingService(
        database=database,
        data_root=root,
        github=github,
        sources=GitSourceManager(token=github_token),
        inspector=RepositoryInspector(),
        evidence_analyzer=MiniSweRepositoryEvidenceAnalyzer(
            model=resolved_model,
            base_url=model_base_url,
            state_root=model_state_root / "onboarding",
        ),
        team_formulator=EvidenceTeamFormulator(
            runtime=MINI_SWE_RUNTIME,
            model=resolved_model,
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
        return MiniSweModelRuntime(
            model=stored_model,
            base_url=model_base_url,
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
            base_url=model_base_url,
            state_root=model_state_root / "scope-review",
            processes=processes,
        ),
        acceptance=acceptance,
        known_secret_values=lambda run_id: _run_secret_values(
            database, secret_resolver, run_id
        ),
    )
    quiet_proxy = _QuietStarterProxy()
    feedback_proxy = _FeedbackPollerProxy()
    feedback = FeedbackService(
        database=database,
        lifecycle=lifecycle,
        gateway=github,
        evaluator=MiniSweFeedbackEvaluator(
            base_url=model_base_url,
            state_root=model_state_root / "feedback",
            processes=processes,
        ),
        executor=execution,
        publisher=publication,
        quiet=quiet_proxy,
    )
    quiet = QuietPeriodService(
        database=database,
        lifecycle=lifecycle,
        gateway=github,
        feedback=feedback_proxy,
    )
    quiet_proxy.service = quiet
    feedback_proxy.service = feedback
    orchestrator = Orchestrator(
        database=database,
        lifecycle=lifecycle,
        execution=execution,
        publication=publication,
        feedback=feedback,
        quiet=quiet,
    )
    scheduler = Scheduler(orchestrator, interval=poll_interval)
    actions = ApplicationActions(
        database=database,
        onboarding=onboarding,
        lifecycle=lifecycle,
        quiet=quiet,
        scheduler=scheduler,
        github=github,
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
        quiet=quiet,
        orchestrator=orchestrator,
        scheduler=scheduler,
        actions=actions,
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
