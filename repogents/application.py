from __future__ import annotations

import json
import math
import queue
import shutil
import time
import tempfile
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from repogents.errors import RepositoryLookupTimeoutError
from repogents.github import GitHubFeedback, PullRequest
from repogents.semantic import SemanticRouter, validate_classification
from repogents.service_ownership import (
    ServiceOwnership,
    ServiceOwnershipUnavailableError,
)
from repogents.store import TERMINAL_RUN_STATES, Store


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    data_dir: str | Path
    default_similarity_threshold: float = 0.75
    promotion_threshold: int = 3
    stale_run_threshold: int = 3
    max_workers: int = 8
    add_repository_lookup_timeout: float = 14.0
    add_repository_lookup_max_workers: int = 4
    repository_add_operation_retention_seconds: float = 7 * 24 * 60 * 60
    repository_add_operation_cleanup_batch_size: int = 100

    def __post_init__(self) -> None:
        if not 0 <= self.default_similarity_threshold < 1:
            raise ValueError(
                "default_similarity_threshold must be at least 0 and less than 1"
            )
        if self.promotion_threshold <= 0:
            raise ValueError("promotion_threshold must be positive")
        if self.stale_run_threshold <= 0:
            raise ValueError("stale_run_threshold must be positive")
        if self.max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if (
            not math.isfinite(self.add_repository_lookup_timeout)
            or self.add_repository_lookup_timeout <= 0
        ):
            raise ValueError(
                "add_repository_lookup_timeout must be finite and positive"
            )
        if (
            isinstance(self.add_repository_lookup_max_workers, bool)
            or not isinstance(self.add_repository_lookup_max_workers, int)
            or self.add_repository_lookup_max_workers <= 0
        ):
            raise ValueError(
                "add_repository_lookup_max_workers must be a positive integer"
            )
        if (
            not math.isfinite(self.repository_add_operation_retention_seconds)
            or self.repository_add_operation_retention_seconds <= 0
        ):
            raise ValueError(
                "repository_add_operation_retention_seconds must be finite and positive"
            )
        if (
            isinstance(self.repository_add_operation_cleanup_batch_size, bool)
            or not isinstance(self.repository_add_operation_cleanup_batch_size, int)
            or self.repository_add_operation_cleanup_batch_size <= 0
        ):
            raise ValueError(
                "repository_add_operation_cleanup_batch_size must be a positive integer"
            )


_CLASSIFICATION_GUIDANCE = (
    "Name every classification as action/capability. A classification names a "
    "repository-reusable agent queue, not the current task. The first level "
    "names the concise kind of action the agent performs. The second level "
    "names the broad stable repository capability that distinguishes the "
    "agent. The capability is a stable repository ownership boundary and a "
    "durable area of repository ownership, not the object, technology, or "
    "deliverable mentioned by the task. Prefer the repository subsystem or "
    "professional discipline that owns the work over the behavior or outcome "
    "requested by the issue. Different issue outcomes in the same ownership "
    "boundary should share the capability. Related action levels should use "
    "the same capability when they serve the same repository area and do not "
    "require different specialists. Verification of a change should keep the "
    "changed area's capability unless it requires a genuinely different "
    "specialist. Choose the shortest lowercase label that "
    "still routes work to a meaningfully different suitable agent; use hyphens "
    "only when a level needs multiple words. Do not summarize the issue, work "
    "item, method, artifact, acceptance criterion, or failure instance. "
    "Include such detail only when it would select a meaningfully different "
    "suitable agent than the broader capability. Choose both levels "
    "semantically; no vocabulary or taxonomy is prescribed."
)


_SPECIFY_SCHEMA = {
    "specifications": [
        {
            "key": "string",
            "title": "string",
            "description": "string",
            "acceptance_criteria": ["string"],
            "dependencies": ["specification key"],
            "executable": True,
            "work_items": [
                {
                    "key": "string",
                    "title": "string",
                    "description": "string",
                    "classification": "agent-chosen concise action/capability",
                    "dependencies": ["work item key"],
                }
            ],
        }
    ]
}
_ROLE_SCHEMA = {"role_prompt": "nonempty string"}
_WORK_SCHEMA = {
    "outcome": "ready_for_validation or continue_work",
    "output": "JSON-safe value",
    "artifacts": [],
    "test_results": [],
    "repository_state": {},
    "classification": "agent-chosen concise action/capability required only for continue_work",
    "context": {},
    "dependencies": [],
    "blocking": None,
}
_VALIDATION_SCHEMA = {
    "passed": True,
    "failed_specifications": [],
    "failed_criteria": [],
    "explanation": "string",
    "evidence": [],
    "repository_state": {},
    "completed_work": [],
}


@dataclass(slots=True)
class _RepositoryLookupTask:
    repository: str
    outcome: queue.Queue[tuple[bool, object]]


class _BoundedRepositoryLookupPool:
    """Run repository metadata calls on a fixed set of daemon workers.

    Capacity represents executing or worker-owned calls, not merely Python thread
    objects. A caller must reserve a slot before queueing work, so an upstream call
    that never returns can consume at most one of the configured fixed slots and
    repeated requests cannot build an unbounded executor queue. Workers are daemon
    threads because Python cannot cancel an arbitrary blocked transport call; close
    stops admission and abandons only that fixed, process-exitable worker set.
    """

    def __init__(self, github, max_workers: int):
        self._github = github
        self._capacity = threading.BoundedSemaphore(max_workers)
        self._tasks: queue.Queue[_RepositoryLookupTask | None] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._workers = []
        for index in range(max_workers):
            worker = threading.Thread(
                target=self._run,
                name=f"repogents-add-repository-lookup-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def submit(self, repository: str, timeout: float) -> _RepositoryLookupTask:
        deadline = time.monotonic() + timeout
        if not self._capacity.acquire(timeout=timeout):
            raise RepositoryLookupTimeoutError(
                "GitHub repository metadata lookup timed out while waiting for "
                "bounded lookup capacity before Repogents could add the repository; "
                "no repository was added"
            )
        task = _RepositoryLookupTask(repository, queue.Queue(maxsize=1))
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("application is closed")
                remaining = max(0.0, deadline - time.monotonic())
                self._tasks.put(task, timeout=remaining)
        except BaseException:
            self._capacity.release()
            raise
        return task

    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                self._tasks.task_done()
                return
            try:
                try:
                    value = self._github.repository(task.repository)
                except BaseException as error:
                    task.outcome.put((False, error))
                else:
                    task.outcome.put((True, value))
            finally:
                self._capacity.release()
                self._tasks.task_done()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # Cancel work that was admitted but not yet claimed by a worker. Calls
            # already inside third-party transport remain bounded by the fixed worker
            # count and lose commit authority through Application._closed.
            while True:
                try:
                    task = self._tasks.get_nowait()
                except queue.Empty:
                    break
                if task is not None:
                    task.outcome.put((False, RuntimeError("application is closed")))
                    self._capacity.release()
                self._tasks.task_done()
            # Idle workers exit promptly. A worker blocked in third-party transport
            # sees its sentinel only after that fixed live call eventually returns.
            for _ in self._workers:
                self._tasks.put_nowait(None)


@dataclass(slots=True)
class _RepositoryAddLockEntry:
    """One same-identity serialization lock plus every live caller reference."""

    lock: threading.Lock
    references: int = 0


class Application:
    def __init__(
        self,
        store: Store,
        github,
        runtime,
        router: SemanticRouter,
        config: ApplicationConfig,
        executor=None,
    ):
        self.store = store
        self.github = github
        self.runtime = runtime
        self.router = router
        self.config = config
        self.data_dir = Path(config.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._executor = executor or ThreadPoolExecutor(
            max_workers=config.max_workers,
            thread_name_prefix="repogents-node",
        )
        self._owns_executor = executor is None
        self._workers: dict[int, Future] = {}
        self._worker_lock = threading.Lock()
        self._repository_add_lock_guard = threading.Lock()
        self._repository_add_locks: dict[str, _RepositoryAddLockEntry] = {}
        self._repository_lookup_pool = _BoundedRepositoryLookupPool(
            github, config.add_repository_lookup_max_workers
        )
        self._service_ownership: ServiceOwnership | None = None
        self._service_ownership_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._close_complete = threading.Event()
        self._closed = False

    def _purge_expired_repository_add_operations(self) -> int:
        """Run one short, centrally configured terminal-operation cleanup batch."""
        return self.store.purge_expired_repository_add_operations(
            self.config.repository_add_operation_retention_seconds,
            limit=self.config.repository_add_operation_cleanup_batch_size,
        )

    def acquire_service_ownership(self) -> None:
        """Acquire exclusive data-directory ownership, then perform startup recovery.

        Application construction is deliberately side-effect free with respect to
        durable recovery. The HTTP service calls this only after its listener has
        bound successfully, so a duplicate startup that loses either boundary cannot
        rewrite state owned by the live process. The advisory lock is held until
        :meth:`close` and is released automatically by the OS after process death.
        """
        with self._service_ownership_lock:
            if self._service_ownership is not None:
                return
            ownership = ServiceOwnership(
                self.data_dir / ".repogents-service.lock"
            )
            try:
                ownership.acquire()
            except ServiceOwnershipUnavailableError as error:
                raise RuntimeError(
                    f"Repogents data directory is already owned: {self.data_dir}"
                ) from error
            self._service_ownership = ownership
            try:
                self.store.recover_interrupted_work()
                self.store.recover_pending_repository_add_operations()
                self._purge_expired_repository_add_operations()
            except BaseException:
                self._release_service_ownership_locked()
                raise

    def _release_service_ownership_locked(self) -> None:
        ownership = self._service_ownership
        self._service_ownership = None
        if ownership is not None:
            ownership.close()

    def add_repository(
        self, github_repository: str, target_branch: str | None = None,
        operation_id: str | None = None,
    ) -> dict:
        """Add a repository under a durable, idempotent completion identity."""
        if self._closed:
            raise RuntimeError("application is closed")
        self._purge_expired_repository_add_operations()
        operation_id = operation_id or str(uuid.uuid4())
        with self._repository_add_lock_guard:
            operation_lock = self._repository_add_locks.get(operation_id)
            if operation_lock is None:
                operation_lock = _RepositoryAddLockEntry(threading.Lock())
                self._repository_add_locks[operation_id] = operation_lock
            # Count callers before releasing the registry guard. This includes the
            # current executor and every waiter, so an entry cannot be retired while
            # another caller can still acquire its lock.
            operation_lock.references += 1
        try:
            # Concurrent replay of one idempotency identity must observe the first
            # execution's terminal state, not race it by performing upstream work twice.
            with operation_lock.lock:
                return self._execute_repository_add(
                    operation_id, github_repository, target_branch
                )
        finally:
            with self._repository_add_lock_guard:
                operation_lock.references -= 1
                if (
                    operation_lock.references == 0
                    and self._repository_add_locks.get(operation_id) is operation_lock
                ):
                    # The identity may be used again later, but no caller can still
                    # reference this entry. A future replay will create a fresh lock
                    # only after this execution and all of its waiters have released.
                    del self._repository_add_locks[operation_id]

    def _execute_repository_add(
        self, operation_id: str, github_repository: str, target_branch: str | None
    ) -> dict:
        """Own upstream lookup and atomic storage completion for one operation."""
        operation = self.store.begin_repository_add_operation(
            operation_id, github_repository, target_branch
        )
        if operation["state"] == "COMMITTED":
            repository = self.store.get_repository(operation["repository_id"])
            if repository is None:
                raise RuntimeError("committed repository add is missing its repository")
            return repository
        if operation["state"] == "FAILED":
            raise ValueError(operation["error"] or "repository add operation failed")
        try:
            metadata = self._repository_metadata_with_commit_boundary(github_repository)
            if self._closed:
                raise RuntimeError("application is closed")
            branch = target_branch or metadata["default_branch"]
            return self.store.add_repository_for_operation(
                operation_id,
                github_repository,
                branch,
                self.config.default_similarity_threshold,
            )
        except BaseException as error:
            # A transaction may have committed immediately before an exceptional
            # response path. Consult durable operation state before exposing failure.
            current = self.store.get_repository_add_operation(operation_id)
            if current and current["state"] == "COMMITTED":
                repository = self.store.get_repository(current["repository_id"])
                if repository is not None:
                    return repository
            if current and current["state"] == "PENDING":
                self.store.fail_repository_add_operation(
                    operation_id, self._repository_add_failure_message(error)
                )
            raise

    @staticmethod
    def _repository_add_failure_message(error: BaseException) -> str:
        """Return a stable, nonempty diagnostic without replacing the exception.

        Some exception types, including a bare ``TimeoutError()``, stringify to an
        empty value. Durable operation failures cannot store that value, because the
        store deliberately rejects empty diagnostics. Preserve meaningful messages
        exactly; otherwise identify the local exception type with a concise fallback.
        A broken custom ``__str__`` is treated the same way so failure normalization
        cannot obscure the exception that the caller is meant to receive.
        """
        try:
            message = str(error)
        except BaseException:
            message = ""
        if message.strip():
            return message
        error_type = type(error).__name__.strip() or "Exception"
        return f"{error_type}: repository add failed"

    def repository_add_operation(self, operation_id: str) -> dict | None:
        """Return authoritative completion while opportunistically bounding history."""
        self._purge_expired_repository_add_operations()
        operation = self.store.get_repository_add_operation(operation_id)
        if operation is None:
            return None
        projected = dict(operation)
        projected["repository"] = (
            self.store.get_repository(operation["repository_id"])
            if operation["state"] == "COMMITTED"
            else None
        )
        return projected

    def _repository_metadata_with_commit_boundary(
        self, github_repository: str
    ) -> dict:
        """Bound metadata lookup concurrency and abandon persistence after timeout.

        The fixed daemon-worker pool prevents repeated hung upstream calls from
        creating unbounded threads or queued operations. A timed-out caller remains
        the only authority that could have committed its result, so late completion
        merely releases pool capacity and can never reach repository storage.
        """
        deadline = time.monotonic() + self.config.add_repository_lookup_timeout
        task = self._repository_lookup_pool.submit(
            github_repository, self.config.add_repository_lookup_timeout
        )
        try:
            succeeded, value = task.outcome.get(
                timeout=max(0.0, deadline - time.monotonic())
            )
        except queue.Empty as error:
            raise RepositoryLookupTimeoutError(
                "GitHub repository metadata lookup timed out before Repogents "
                "could add the repository; no repository was added"
            ) from error
        if not succeeded:
            raise value
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned an invalid repository")
        return value

    def remove_repository(self, repository_id: int) -> None:
        self.store.remove_repository(repository_id)

    def state(self) -> dict:
        repositories = []
        for repository in self.store.list_repositories():
            projected = dict(repository)
            projected["nodes"] = self.store.list_nodes(repository["id"])
            projected_runs = []
            for run in self.store.list_runs(repository["id"]):
                run_projection = dict(run)
                run_projection["passes"] = self.store.list_passes(run["id"])
                run_projection["specifications"] = self.store.list_specifications(
                    run["id"]
                )
                run_projection["work_items"] = self.store.list_work_items(run["id"])
                run_projection["validations"] = self.store.list_validations(run["id"])
                run_projection["feedback"] = self.store.list_feedback(run["id"])
                projected_runs.append(run_projection)
            projected["runs"] = projected_runs
            repositories.append(projected)
        return {"repositories": repositories}

    def poll_once(self) -> None:
        if self._closed:
            raise RuntimeError("application is closed")
        self._reap_workers()
        repositories = self.store.list_repositories()
        for repository in repositories:
            for issue in self.github.list_ready_issues(repository["github_repository"]):
                self.store.create_run(
                    repository["id"],
                    issue.number,
                    {
                        "number": issue.number,
                        "title": issue.title,
                        "body": issue.body,
                        "url": issue.url,
                    },
                )

        repositories_by_id = {item["id"]: item for item in repositories}
        for run in self.store.list_runs():
            if run["state"] in TERMINAL_RUN_STATES:
                self.store.adapt_nodes_after_run(
                    run["id"], self.config.stale_run_threshold
                )
                continue
            repository = repositories_by_id.get(run["repository_id"])
            if repository is None:
                continue
            self._advance_run(repository, run)
        self._start_workers()

    def close(self) -> None:
        # One caller owns teardown; concurrent close callers wait for the same
        # ownership-release boundary instead of returning while workers remain live.
        with self._close_lock:
            if self._close_complete.is_set():
                return
            if self._closed:
                wait_for_close = True
            else:
                self._closed = True
                wait_for_close = False
        if wait_for_close:
            self._close_complete.wait()
            return

        try:
            self._repository_lookup_pool.close()
            if self._owns_executor:
                self._executor.shutdown(wait=True)
            # A borrowed executor remains caller-owned, but every future submitted
            # by this application remains our mutation authority. Join those futures
            # explicitly before releasing data-directory service ownership.
            self._wait_for_workers()
        finally:
            with self._service_ownership_lock:
                self._release_service_ownership_locked()
            self._close_complete.set()

    def _workspace(self, repository_id: int, run_id: int) -> Path:
        return self.data_dir / "workspaces" / str(repository_id) / str(run_id)

    def _trajectory(self, run_id: int, name: str) -> Path:
        return self.data_dir / "trajectories" / str(run_id) / f"{name}.json"

    @staticmethod
    def _task(kind: str, instruction: str, context: dict) -> str:
        return json.dumps(
            {"kind": kind, "instruction": instruction, "context": context},
            sort_keys=True,
        )

    def _advance_run(self, repository: dict, run: dict) -> None:
        state = run["state"]
        if state == "QUEUED":
            self._begin_run(repository, run)
        elif state == "SPECIFYING":
            self._specify(repository, run)
        elif state == "EXECUTING":
            self.store.transition_run(run["id"], "WAITING_FOR_WORK_COMPLETION")
        elif state == "WAITING_FOR_WORK_COMPLETION":
            self._wait_for_work(repository, run)
        elif state == "VALIDATING":
            self._validate(repository, run)
        elif state == "CREATING_PR":
            self._publish(repository, run)
        elif state == "PR_LISTENING":
            self._listen_for_pull_request(repository, run)

    def _begin_run(self, repository: dict, run: dict) -> None:
        workspace = self._workspace(repository["id"], run["id"])
        self.github.checkout(
            repository["github_repository"], repository["target_branch"], workspace
        )
        if not self.store.list_passes(run["id"]):
            self.store.create_pass(run["id"], "issue", run["issue_json"])
        self.store.transition_run(run["id"], "SPECIFYING")

    @staticmethod
    def _pass_feedback_context(
        execution_pass: dict,
    ) -> tuple[list[dict], str | None]:
        if execution_pass["trigger_type"] != "feedback":
            return [], None
        packages = execution_pass["trigger_json"].get("feedback", [])
        pull_request_diff = packages[0].get("diff") if packages else None
        feedback = [
            {
                field: package.get(field)
                for field in (
                    "external_id",
                    "kind",
                    "body",
                    "path",
                    "line",
                    "review_thread_id",
                    "top_level_comment_id",
                )
            }
            for package in packages
        ]
        return feedback, pull_request_diff

    @staticmethod
    def _claimed_feedback_ids(execution_passes: list[dict]) -> set[str]:
        claimed_feedback_ids = set()
        for execution_pass in execution_passes:
            if execution_pass["trigger_type"] != "feedback":
                continue
            trigger_feedback = execution_pass["trigger_json"].get("feedback", [])
            if not isinstance(trigger_feedback, list):
                continue
            claimed_feedback_ids.update(
                item["external_id"]
                for item in trigger_feedback
                if isinstance(item, dict)
                and isinstance(item.get("external_id"), str)
            )
        return claimed_feedback_ids

    def _specify_context(
        self,
        repository: dict,
        run: dict,
        execution_pass: dict,
    ) -> dict:
        validations = self.store.list_validations(run["id"])
        feedback, pull_request_diff = self._pass_feedback_context(execution_pass)
        return {
            "original_issue": run["issue_json"],
            "repository": repository,
            "prior_validation_failures": [
                item
                for item in validations
                if item.get("result", {}).get("passed") is False
            ],
            "validation_history": validations,
            "feedback": feedback,
            "pull_request_diff": pull_request_diff,
            "existing_specifications": self.store.list_specifications(run["id"]),
            "existing_work": self.store.list_work_items(run["id"]),
        }

    def _specify(self, repository: dict, run: dict) -> None:
        execution_passes = self.store.list_passes(run["id"])
        if not execution_passes:
            raise RuntimeError("specifying run has no execution pass")
        execution_pass = execution_passes[-1]
        existing = [
            item
            for item in self.store.list_specifications(run["id"])
            if item["pass_id"] == execution_pass["id"]
        ]
        if not existing:
            result = self.runtime.run(
                self._task(
                    "specify",
                    "Convert only the issue, validation deficiency, or feedback in context into atomic specifications, acceptance criteria, classified work items, and dependencies. "
                    + _CLASSIFICATION_GUIDANCE,
                    self._specify_context(repository, run, execution_pass),
                ),
                self._workspace(repository["id"], run["id"]),
                result_schema=_SPECIFY_SCHEMA,
                trajectory_path=self._trajectory(
                    run["id"], f"specify-{execution_pass['id']}"
                ),
            )
            self.store.save_specification_package(
                run["id"], execution_pass["id"], result
            )
        self._route_unassigned(repository, run, execution_pass)
        self.store.transition_run(run["id"], "EXECUTING")
        self.store.transition_run(run["id"], "WAITING_FOR_WORK_COMPLETION")

    def _route_unassigned(
        self, repository: dict, run: dict, execution_pass: dict
    ) -> None:
        for work in self.store.list_work_items(run["id"], execution_pass["id"]):
            if work["state"] != "UNASSIGNED":
                continue
            classification = validate_classification(work["classification"])
            nodes = self.store.list_dynamic_nodes(repository["id"])
            cached_vector = self.store.get_classification_vector(
                repository["id"], classification
            )
            node, vector = self.router.route(
                classification,
                nodes,
                repository["similarity_threshold"],
                vector=cached_vector,
            )
            if cached_vector is None:
                self.store.save_classification_vector(
                    repository["id"], classification, vector
                )
            if node is None:
                role_result = self.runtime.run(
                    self._task(
                        "node_role",
                        "Generate a flexible role prompt for this repository-reusable agent queue. Describe responsibilities broad enough to serve this classification across repository issues without prescribing a fixed implementation workflow. Use the current work only as context; do not narrow the role to this issue or work item.",
                        {
                            "classification": classification,
                            "work_item": work,
                            "repository": repository,
                            "repository_context": self._specify_context(
                                repository, run, execution_pass
                            ),
                        },
                    ),
                    self._workspace(repository["id"], run["id"]),
                    result_schema=_ROLE_SCHEMA,
                    trajectory_path=self._trajectory(run["id"], f"role-{work['id']}"),
                )
                role_prompt = role_result.get("role_prompt")
                if not isinstance(role_prompt, str) or not role_prompt.strip():
                    raise ValueError("Node Role Agent must return a nonempty role_prompt")
                node = self.store.create_dynamic_node(
                    repository["id"], classification, vector, role_prompt
                )
            self.store.assign_work(work["id"], node["id"])

    def _wait_for_work(self, repository: dict, run: dict) -> None:
        execution_passes = self.store.list_passes(run["id"])
        if not execution_passes:
            raise RuntimeError("waiting run has no execution pass")
        execution_pass = execution_passes[-1]
        self._route_unassigned(repository, run, execution_pass)
        if self.store.validation_barrier_ready(run["id"], execution_pass["id"]):
            self.store.transition_run(run["id"], "VALIDATING")

    def _start_workers(self) -> None:
        for repository in self.store.list_repositories():
            for node in self.store.list_dynamic_nodes(repository["id"]):
                with self._worker_lock:
                    if self._closed:
                        return
                    if node["id"] in self._workers:
                        continue
                    work = self.store.claim_node_work(node["id"])
                    if work is None:
                        continue
                    future = self._executor.submit(self._run_work, node, work)
                    self._workers[node["id"]] = future

    def _reap_workers(self) -> None:
        with self._worker_lock:
            done = [node_id for node_id, future in self._workers.items() if future.done()]
            futures = [self._workers.pop(node_id) for node_id in done]
        for future in futures:
            try:
                future.result()
            except Exception:
                pass

    def _wait_for_workers(self) -> None:
        """Join all application-tracked work without owning a borrowed executor."""
        while True:
            with self._worker_lock:
                futures = list(self._workers.values())
            if not futures:
                return
            for future in futures:
                try:
                    future.result()
                except Exception:
                    # _run_work already persists its failure outcome where possible;
                    # shutdown must still collect the future and continue draining.
                    pass
            self._reap_workers()

    def _run_work(self, node: dict, work: dict) -> None:
        run = self.store.get_run(work["run_id"])
        if run is None:
            raise RuntimeError("claimed work has no run")
        repository = self.store.get_repository(run["repository_id"])
        if repository is None:
            raise RuntimeError("claimed work run has no repository")
        execution_pass = next(
            item
            for item in self.store.list_passes(run["id"])
            if item["id"] == work["pass_id"]
        )
        feedback, pull_request_diff = self._pass_feedback_context(execution_pass)
        specifications = {
            item["id"]: item for item in self.store.list_specifications(run["id"])
        }
        all_work = self.store.list_work_items(run["id"], work["pass_id"])
        dependencies = [
            item for item in all_work if item["key"] in set(work["dependencies"])
        ]
        try:
            result = self.runtime.run(
                self._task(
                    "work",
                    "Use your tools and judgment flexibly to complete this bounded work. Return ready_for_validation when no more agent work is needed, or continue_work with the next classification and handoff context. "
                    + _CLASSIFICATION_GUIDANCE,
                    {
                        "original_issue": run["issue_json"],
                        "repository": repository,
                        "specification": specifications[work["specification_id"]],
                        "work_item": work,
                        "dependency_results": dependencies,
                        "prior_specifications": self.store.list_specifications(run["id"]),
                        "prior_work": self.store.list_work_items(run["id"]),
                        "feedback": feedback,
                        "pull_request_diff": pull_request_diff,
                    },
                ),
                self._workspace(repository["id"], run["id"]),
                role_prompt=node["role_prompt"],
                result_schema=_WORK_SCHEMA,
                trajectory_path=self._trajectory(run["id"], f"work-{work['id']}"),
            )
            outcome = result.get("outcome")
            persisted_result = {
                "output": result["output"],
                "artifacts": result["artifacts"],
                "test_results": result["test_results"],
                "repository_state": result["repository_state"],
            }
            if outcome == "ready_for_validation":
                self.store.complete_work(work["id"], persisted_result)
            elif outcome == "continue_work":
                handoff = {
                    "classification": result["classification"],
                    "context": result["context"],
                    "artifacts": result["artifacts"],
                    "dependencies": result["dependencies"],
                    "blocking": result["blocking"],
                }
                self.store.complete_work(work["id"], persisted_result, handoff)
            else:
                raise ValueError("work outcome must be ready_for_validation or continue_work")
            self.store.record_node_success(
                node["id"], run["id"], self.config.promotion_threshold
            )
        except Exception as error:
            failure = {
                "output": {"error": str(error), "type": type(error).__name__},
                "artifacts": [],
                "test_results": [],
                "repository_state": {},
            }
            try:
                self.store.fail_work(work["id"], failure)
            except (KeyError, ValueError):
                pass

    def _validation_context(
        self,
        repository: dict,
        run: dict,
        execution_pass: dict,
    ) -> dict:
        feedback, pull_request_diff = self._pass_feedback_context(execution_pass)
        return {
            "original_issue": run["issue_json"],
            "repository": repository,
            "specifications": self.store.list_specifications(run["id"]),
            "work_items": self.store.list_work_items(run["id"]),
            "validation_history": self.store.list_validations(run["id"]),
            "feedback": feedback,
            "pull_request_diff": pull_request_diff,
        }

    @staticmethod
    def _validated_validation_result(result: dict) -> dict:
        required = {
            "passed",
            "failed_specifications",
            "failed_criteria",
            "explanation",
            "evidence",
            "repository_state",
            "completed_work",
        }
        if not isinstance(result, dict) or not required.issubset(result):
            raise ValueError("Validate must return the complete validation result")
        if not isinstance(result["passed"], bool):
            raise ValueError("validation passed must be boolean")
        for field in ("failed_specifications", "failed_criteria"):
            values = result[field]
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"validation {field} must be a list of strings")
        if not isinstance(result["explanation"], str):
            raise ValueError("validation explanation must be a string")
        if not isinstance(result["evidence"], list):
            raise ValueError("validation evidence must be a list")
        if not isinstance(result["repository_state"], dict):
            raise ValueError("validation repository_state must be an object")
        if not isinstance(result["completed_work"], list):
            raise ValueError("validation completed_work must be a list")
        has_failures = bool(
            result["failed_specifications"] or result["failed_criteria"]
        )
        if result["passed"] == has_failures:
            raise ValueError("validation outcome and failures are inconsistent")
        return dict(result)

    def _pass_has_specifications(self, run_id: int, pass_id: int) -> bool:
        return any(
            item["pass_id"] == pass_id
            for item in self.store.list_specifications(run_id)
        )

    def _validate(self, repository: dict, run: dict) -> None:
        execution_passes = self.store.list_passes(run["id"])
        if not execution_passes:
            raise RuntimeError("validating run has no execution pass")
        execution_pass = execution_passes[-1]
        if (
            execution_pass["trigger_type"] in {"validation_failure", "feedback"}
            and not self._pass_has_specifications(
                run["id"], execution_pass["id"]
            )
        ):
            self.store.transition_run(run["id"], "SPECIFYING")
            return
        recorded = next(
            (
                item["result"]
                for item in self.store.list_validations(run["id"])
                if item["pass_id"] == execution_pass["id"]
            ),
            None,
        )
        if recorded is None:
            with tempfile.TemporaryDirectory(
                prefix="repogents-validate-",
                dir=self.data_dir,
            ) as temporary_directory:
                validation_workspace = Path(temporary_directory) / "workspace"
                shutil.copytree(
                    self._workspace(repository["id"], run["id"]),
                    validation_workspace,
                    symlinks=True,
                )
                result = self.runtime.run(
                    self._task(
                        "validate",
                        "Judge the completed result against both every atomic specification and acceptance criterion and the intent of the original issue. Do not modify repository files or implement corrections. If any requirement is unmet, return a failed validation result for Specify.",
                        self._validation_context(repository, run, execution_pass),
                    ),
                    validation_workspace,
                    result_schema=_VALIDATION_SCHEMA,
                    trajectory_path=self._trajectory(
                        run["id"], f"validate-{execution_pass['id']}"
                    ),
                )
            result = self._validated_validation_result(result)
            self.store.record_validation(run["id"], execution_pass["id"], result)
        else:
            result = self._validated_validation_result(recorded)
        if not result["passed"]:
            latest_pass = self.store.list_passes(run["id"])[-1]
            if latest_pass["id"] == execution_pass["id"]:
                self.store.create_pass(run["id"], "validation_failure", result)
            self.store.transition_run(run["id"], "SPECIFYING")
            return
        self.store.transition_run(run["id"], "CREATING_PR")

    def _publish(self, repository: dict, run: dict) -> None:
        existing = run.get("pull_request")
        existing_number = None if existing is None else int(existing["number"])
        pull = self.github.publish(
            repository["github_repository"],
            run["issue_number"],
            repository["target_branch"],
            self._workspace(repository["id"], run["id"]),
            existing_pr=existing_number,
        )
        claimed_feedback_ids = self._claimed_feedback_ids(
            self.store.list_passes(run["id"])
        )
        for feedback_row in self.store.list_feedback(run["id"]):
            if (
                feedback_row["status"] != "PENDING"
                or feedback_row["external_id"] not in claimed_feedback_ids
            ):
                continue
            package = feedback_row["package"]
            feedback = GitHubFeedback(
                external_id=feedback_row["external_id"],
                kind=package["kind"],
                body=package["body"],
                path=package.get("path"),
                line=package.get("line"),
                review_thread_id=package.get("review_thread_id"),
                top_level_comment_id=package.get("top_level_comment_id"),
            )
            address = self.github.address_feedback(
                repository["github_repository"],
                pull.number,
                feedback,
                pull.head_sha,
            )
            self.store.mark_feedback_addressed(
                run["id"],
                feedback.external_id,
                address.status,
                pull.head_sha,
                address.response_url,
            )
        self.store.transition_run(
            run["id"],
            "PR_LISTENING",
            branch=pull.branch,
            pull_request=asdict(pull),
        )

    @staticmethod
    def _feedback_package(
        feedback: GitHubFeedback,
        pull: PullRequest,
        run: dict,
        specifications: list[dict],
        work_items: list[dict],
        validations: list[dict],
    ) -> dict:
        return {
            "external_id": feedback.external_id,
            "kind": feedback.kind,
            "body": feedback.body,
            "path": feedback.path,
            "line": feedback.line,
            "review_thread_id": feedback.review_thread_id,
            "top_level_comment_id": feedback.top_level_comment_id,
            "diff": pull.diff,
            "original_issue": run["issue_json"],
            "specifications": specifications,
            "work_items": work_items,
            "validations": validations,
        }

    def _listen_for_pull_request(self, repository: dict, run: dict) -> None:
        reference = run.get("pull_request")
        if not reference:
            raise RuntimeError("PR_LISTENING run has no pull request")
        pull = self.github.pull_request(
            repository["github_repository"], int(reference["number"])
        )
        if pull.merged:
            self.store.transition_run(
                run["id"], "COMPLETED", branch=pull.branch, pull_request=asdict(pull)
            )
            self.store.adapt_nodes_after_run(
                run["id"], self.config.stale_run_threshold
            )
            return
        if pull.state == "closed":
            self.store.transition_run(
                run["id"], "CLOSED", branch=pull.branch, pull_request=asdict(pull)
            )
            self.store.adapt_nodes_after_run(
                run["id"], self.config.stale_run_threshold
            )
            return

        execution_passes = self.store.list_passes(run["id"])
        if execution_passes:
            latest_pass = execution_passes[-1]
            if (
                latest_pass["trigger_type"] == "feedback"
                and not self._pass_has_specifications(
                    run["id"], latest_pass["id"]
                )
            ):
                self.store.transition_run(
                    run["id"],
                    "SPECIFYING",
                    branch=pull.branch,
                    pull_request=asdict(pull),
                )
                return

        specifications = self.store.list_specifications(run["id"])
        work_items = self.store.list_work_items(run["id"])
        validations = self.store.list_validations(run["id"])
        for feedback in self.github.list_feedback(
            repository["github_repository"], pull.number
        ):
            package = self._feedback_package(
                feedback, pull, run, specifications, work_items, validations
            )
            self.store.add_feedback(run["id"], feedback.external_id, package)

        claimed_feedback_ids = self._claimed_feedback_ids(
            self.store.list_passes(run["id"])
        )
        pending_packages = [
            item["package"]
            for item in self.store.list_feedback(run["id"])
            if item["external_id"] not in claimed_feedback_ids
        ]
        if pending_packages:
            self.store.create_pass(
                run["id"], "feedback", {"feedback": pending_packages}
            )
            self.store.transition_run(
                run["id"],
                "SPECIFYING",
                branch=pull.branch,
                pull_request=asdict(pull),
            )
            return
        self.store.transition_run(
            run["id"],
            "PR_LISTENING",
            branch=pull.branch,
            pull_request=asdict(pull),
        )
