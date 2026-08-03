from __future__ import annotations

import json
import shutil
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from repogents.github import GitHubFeedback, PullRequest
from repogents.semantic import SemanticRouter, validate_classification
from repogents.store import TERMINAL_RUN_STATES, Store


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    data_dir: str | Path
    default_similarity_threshold: float = 0.75
    promotion_threshold: int = 3
    stale_run_threshold: int = 3
    max_workers: int = 8

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
        self.store.recover_interrupted_work()
        self._executor = executor or ThreadPoolExecutor(
            max_workers=config.max_workers,
            thread_name_prefix="repogents-node",
        )
        self._owns_executor = executor is None
        self._workers: dict[int, Future] = {}
        self._worker_lock = threading.Lock()
        self._closed = False

    def add_repository(
        self, github_repository: str, target_branch: str | None = None
    ) -> dict:
        metadata = self.github.repository(github_repository)
        branch = target_branch or metadata["default_branch"]
        return self.store.add_repository(
            github_repository,
            branch,
            self.config.default_similarity_threshold,
        )

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
        if self._closed:
            return
        self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=True)
        self._reap_workers()

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
