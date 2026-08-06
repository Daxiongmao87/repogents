from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from repogents.github import GitHubFeedback, GitHubIssue, PublicationCandidate, PullRequest
from repogents.semantic import SemanticRouter, validate_classification
from repogents.store import TERMINAL_RUN_STATES, Store


_SOURCE_IGNORED_NAMES = frozenset({".git", "__pycache__"})


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    data_dir: str | Path
    default_similarity_threshold: float = 0.75
    promotion_threshold: int = 3
    stale_run_threshold: int = 3
    max_workers: int = 8
    pr_silence_seconds: float = 3600
    auto_merge: bool = False

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
            isinstance(self.pr_silence_seconds, bool)
            or not math.isfinite(self.pr_silence_seconds)
            or self.pr_silence_seconds <= 0
        ):
            raise ValueError("pr_silence_seconds must be positive and finite")
        if not isinstance(self.auto_merge, bool):
            raise ValueError("auto_merge must be boolean")


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
            "dependency_evidence": [
                {
                    "dependency": "specification key",
                    "reason": "why this outcome is required",
                    "evidence": ["concrete repository or task observation"],
                }
            ],
            "executable": True,
            "work_items": [
                {
                    "key": "string",
                    "title": "string",
                    "description": "string",
                    "classification": "agent-chosen concise action/capability",
                    "dependencies": ["work item key"],
                    "dependency_evidence": [
                        {
                            "dependency": "work item key",
                            "reason": "why this outcome is required",
                            "evidence": ["concrete repository or task observation"],
                        }
                    ],
                }
            ],
        }
    ]
}
_ISSUE_SPECIFY_SCHEMA = {
    "requirements": [
        {
            "key": "stable requirement key",
            "statement": "explicit required outcome, constraint, or method",
            "evidence": ["concrete issue, feedback, failure, or repository observation"],
        }
    ],
    "work_areas": [
        {
            "key": "stable strategic work area key",
            "title": "string",
            "description": "strategic outcome without a prescribed procedure",
            "requirement_keys": ["requirement key"],
            "dependencies": ["work area key"],
            "dependency_evidence": [
                {
                    "dependency": "work area key",
                    "reason": "why this outcome is required first",
                    "evidence": ["concrete causal observation"],
                }
            ],
        }
    ],
}
_WORK_SPECIFY_SCHEMA = {
    "work_area_key": "strategic work area key",
    "specification": {
        "key": "same strategic work area key",
        "title": "string",
        "description": "bounded outcome",
        "requirement_keys": ["requirement key"],
        "acceptance_criteria": [
            {
                "key": "stable criterion key",
                "description": "observable criterion",
                "requirement_keys": ["requirement key"],
            }
        ],
        "work_items": [
            {
                "key": "string",
                "title": "string",
                "description": "focused actionable outcome",
                "classification": "agent-chosen concise action/capability",
                "requirement_keys": ["requirement key"],
                "acceptance_criteria": ["criterion key"],
                "evidence_requirements": ["evidence needed to judge completion"],
                "dependencies": ["work item key in this work area"],
                "dependency_evidence": [
                    {
                        "dependency": "work item key",
                        "reason": "why this outcome is required first",
                        "evidence": ["concrete causal observation"],
                    }
                ],
            }
        ],
    },
}
_FEEDBACK_SPECIFY_SCHEMA = {
    "dispositions": [
        {
            "external_id": "string",
            "valid": "boolean",
            "in_scope": "boolean; false whenever valid is false",
            "pr_regression": "boolean; true requires valid and in_scope",
            "explanation": "string",
            "evidence": ["string"],
            "specification_keys": ["specification key"],
            "follow_up_issue": {
                "title": "string",
                "observed_defect": "string",
                "affected_behavior": "string",
                "affected_paths": ["string"],
                "acceptance_criteria": ["string"],
            },
        }
    ],
    "specifications": _SPECIFY_SCHEMA["specifications"],
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
    "dependency_evidence": [],
    "blocking": None,
}
_OPERATION_WORK_SCHEMA = {
    **_WORK_SCHEMA,
    "resolved_paths": [
        "operation_failure-only source path intentionally ready for controller staging"
    ],
}
_VALIDATION_SCHEMA = {
    "passed": True,
    "failed_specifications": [],
    "failed_criteria": [],
    "code_review_findings": ["string"],
    "explanation": "string",
    "evidence": [],
    "repository_state": {},
    "completed_work": [],
    "commit_message": "concise imperative subject describing the actual completed change",
    "requirement_results": [
        {"requirement_key": "string", "passed": True, "evidence": ["string"]}
    ],
    "criterion_results": [
        {"criterion_key": "string", "passed": True, "evidence": ["string"]}
    ],
    "integration_findings": ["string"],
}
_ISSUE_ORDER_SCHEMA = {
    "ordered_issues": [
        {
            "issue_number": 1,
            "reason": "why this issue belongs at this position",
            "evidence": ["concrete observation from the supplied issues"],
            "dependencies": [
                {
                    "issue_number": 2,
                    "reason": "why the other issue must be completed first",
                    "evidence": ["concrete causal observation"],
                }
            ],
        }
    ]
}


@dataclass(frozen=True, slots=True)
class _SourceTreeEntry:
    kind: str
    mode: int
    value: str | None


_SOURCE_IMPORT_JOURNAL_VERSION = 1
_SOURCE_IMPORT_SUCCESS_STATES = {"COMPLETED", "HANDED_OFF"}


@dataclass(frozen=True, slots=True)
class _SourceImportJournal:
    path: Path
    backup: Path
    repository_id: int
    run_id: int
    work_id: int

_SOURCE_ACTIVE_RUN_STATES = {
    "SPECIFYING",
    "EXECUTING",
    "WAITING_FOR_WORK_COMPLETION",
    "VALIDATING",
    "CREATING_PR",
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
        clock=None,
    ):
        self.store = store
        self.github = github
        self.runtime = runtime
        self.router = router
        self.config = config
        self._clock = clock or time.time
        self.data_dir = Path(config.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._source_lock = threading.RLock()
        self._recover_source_import_journals()
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
        self,
        github_repository: str,
        target_branch: str | None = None,
        autonomous_issue_intake: bool = False,
    ) -> dict:
        metadata = self.github.repository(github_repository)
        branch = target_branch or metadata["default_branch"]
        return self.store.add_repository(
            github_repository,
            branch,
            self.config.default_similarity_threshold,
            autonomous_issue_intake,
        )

    def remove_repository(self, repository_id: int) -> None:
        self.store.remove_repository(repository_id)

    def set_autonomous_issue_intake(
        self,
        repository_id: int,
        enabled: bool,
    ) -> dict:
        return self.store.set_autonomous_issue_intake(repository_id, enabled)

    def state(self) -> dict:
        repositories = []
        for repository in self.store.list_repositories():
            projected = dict(repository)
            projected["nodes"] = self.store.list_nodes(repository["id"])
            projected["issue_order"] = self.store.get_issue_order_plan(
                repository["id"]
            )
            projected_runs = []
            for run in self._ordered_runs(
                repository["id"],
                self.store.list_runs(repository["id"]),
            ):
                run_projection = dict(run)
                run_projection["passes"] = self.store.list_passes(run["id"])
                run_projection["specifications"] = self.store.list_specifications(
                    run["id"]
                )
                run_projection["issue_specifications"] = [
                    {
                        "pass_id": execution_pass["id"],
                        "result": issue_specification,
                    }
                    for execution_pass in run_projection["passes"]
                    if (
                        issue_specification := self.store.get_issue_specification(
                            run["id"], execution_pass["id"]
                        )
                    )
                    is not None
                ]
                run_projection["work_specification_results"] = [
                    result
                    for execution_pass in run_projection["passes"]
                    for result in self.store.list_work_specification_results(
                        run["id"], execution_pass["id"]
                    )
                ]
                run_projection["work_items"] = self.store.list_work_items(run["id"])
                run_projection["work_validations"] = self.store.list_work_validations(
                    run["id"]
                )
                run_projection["validations"] = self.store.list_validations(run["id"])
                run_projection["feedback"] = self.store.list_feedback(run["id"])
                projected_runs.append(run_projection)
            projected["runs"] = projected_runs
            repositories.append(projected)
        return {"repositories": repositories}

    @staticmethod
    def _issue_snapshot(issues: list[GitHubIssue]) -> list[dict]:
        return [
            {
                "number": issue.number,
                "title": issue.title,
                "body": issue.body,
                "url": issue.url,
                "labels": list(issue.labels),
            }
            for issue in sorted(issues, key=lambda item: item.number)
        ]

    @staticmethod
    def _validated_issue_order_result(
        result: dict,
        issues: list[GitHubIssue],
    ) -> dict:
        if not isinstance(result, dict) or set(result) != {"ordered_issues"}:
            raise ValueError("issue ordering must return ordered_issues")
        ordered = result["ordered_issues"]
        if not isinstance(ordered, list):
            raise ValueError("ordered_issues must be a list")
        expected_numbers = {issue.number for issue in issues}
        positions: dict[int, int] = {}
        normalized = []
        for position, item in enumerate(ordered):
            if not isinstance(item, dict) or set(item) != {
                "issue_number",
                "reason",
                "evidence",
                "dependencies",
            }:
                raise ValueError("ordered issue entry is incomplete")
            issue_number = item["issue_number"]
            if (
                type(issue_number) is not int
                or issue_number not in expected_numbers
                or issue_number in positions
            ):
                raise ValueError("ordered issue number is invalid or duplicated")
            reason = item["reason"]
            evidence = item["evidence"]
            dependencies = item["dependencies"]
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("ordered issue reason must be nonempty")
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in evidence
                )
            ):
                raise ValueError("ordered issue evidence must be nonempty strings")
            if not isinstance(dependencies, list):
                raise ValueError("ordered issue dependencies must be a list")
            normalized_dependencies = []
            dependency_numbers: set[int] = set()
            for dependency in dependencies:
                if not isinstance(dependency, dict) or set(dependency) != {
                    "issue_number",
                    "reason",
                    "evidence",
                }:
                    raise ValueError("issue dependency evidence is incomplete")
                dependency_number = dependency["issue_number"]
                dependency_reason = dependency["reason"]
                dependency_evidence = dependency["evidence"]
                if (
                    type(dependency_number) is not int
                    or dependency_number not in expected_numbers
                    or dependency_number == issue_number
                    or dependency_number in dependency_numbers
                ):
                    raise ValueError("issue dependency reference is invalid")
                if (
                    not isinstance(dependency_reason, str)
                    or not dependency_reason.strip()
                ):
                    raise ValueError("issue dependency reason must be nonempty")
                if (
                    not isinstance(dependency_evidence, list)
                    or not dependency_evidence
                    or any(
                        not isinstance(value, str) or not value.strip()
                        for value in dependency_evidence
                    )
                ):
                    raise ValueError(
                        "issue dependency evidence must be nonempty strings"
                    )
                dependency_numbers.add(dependency_number)
                normalized_dependencies.append(
                    {
                        "issue_number": dependency_number,
                        "reason": dependency_reason.strip(),
                        "evidence": list(dependency_evidence),
                    }
                )
            positions[issue_number] = position
            normalized.append(
                {
                    "issue_number": issue_number,
                    "reason": reason.strip(),
                    "evidence": list(evidence),
                    "dependencies": normalized_dependencies,
                }
            )
        if set(positions) != expected_numbers:
            raise ValueError("issue ordering must contain every open issue exactly once")
        for item in normalized:
            for dependency in item["dependencies"]:
                if positions[dependency["issue_number"]] >= positions[item["issue_number"]]:
                    raise ValueError(
                        "issue dependencies must precede their dependents"
                    )
        return {"ordered_issues": normalized}

    def _ordered_open_issues(
        self,
        repository: dict,
        issues: list[GitHubIssue],
    ) -> list[GitHubIssue]:
        snapshot = self._issue_snapshot(issues)
        persisted = self.store.get_issue_order_plan(repository["id"])
        if persisted is not None and persisted["issue_snapshot"] == snapshot:
            result = self._validated_issue_order_result(
                persisted["result"],
                issues,
            )
        elif len(issues) <= 1:
            result = {
                "ordered_issues": [
                    {
                        "issue_number": issue.number,
                        "reason": "This is the only open issue.",
                        "evidence": [
                            "The complete open-issue snapshot contains one issue."
                        ],
                        "dependencies": [],
                    }
                    for issue in issues
                ]
            }
            result = self._validated_issue_order_result(result, issues)
            self.store.save_issue_order_plan(repository["id"], snapshot, result)
        else:
            with tempfile.TemporaryDirectory(
                prefix="repogents-issue-order-",
                dir=self.data_dir,
            ) as workspace:
                result = self.runtime.run(
                    self._task(
                        "issue_order",
                        "Order every supplied open GitHub issue for autonomous processing. Use issue-described urgency, causal dependencies, and repository impact. Put every causal prerequisite before its dependents. Give a reason and concrete evidence for every position and every dependency. Do not prescribe or assume a domain priority or dependency taxonomy. Return every supplied issue exactly once. Do not modify repository files.",
                        {
                            "repository": repository,
                            "open_issues": snapshot,
                            "durable_runs": [
                                {
                                    "issue_number": run["issue_number"],
                                    "state": run["state"],
                                    "branch": run.get("branch"),
                                }
                                for run in self.store.list_runs(repository["id"])
                            ],
                        },
                    ),
                    workspace,
                    result_schema=_ISSUE_ORDER_SCHEMA,
                    trajectory_path=self._trajectory(
                        0,
                        f"issue-order-repository-{repository['id']}",
                    ),
                )
            result = self._validated_issue_order_result(result, issues)
            self.store.save_issue_order_plan(repository["id"], snapshot, result)
        issues_by_number = {issue.number: issue for issue in issues}
        return [
            issues_by_number[item["issue_number"]]
            for item in result["ordered_issues"]
        ]

    def _ordered_runs(self, repository_id: int, runs: list[dict]) -> list[dict]:
        plan = self.store.get_issue_order_plan(repository_id)
        if plan is None:
            return list(runs)
        ordered = plan.get("result", {}).get("ordered_issues", [])
        ranks = {
            item.get("issue_number"): rank
            for rank, item in enumerate(ordered)
            if isinstance(item, dict)
            and type(item.get("issue_number")) is int
        }
        fallback = len(ranks)
        return sorted(
            runs,
            key=lambda run: (
                ranks.get(run["issue_number"], fallback),
                run["id"],
            ),
        )

    def poll_once(self) -> None:
        if self._closed:
            raise RuntimeError("application is closed")
        self._reap_workers()
        repositories = self.store.list_repositories()
        for repository in repositories:
            issues = self.github.list_open_issues(repository["github_repository"])
            authorized_issues = [
                issue
                for issue in issues
                if repository["autonomous_issue_intake"]
                or "agent:ready" in issue.labels
            ]
            for issue in self._ordered_open_issues(repository, authorized_issues):
                self.store.create_run(
                    repository["id"],
                    issue.number,
                    {
                        "number": issue.number,
                        "title": issue.title,
                        "body": issue.body,
                        "url": issue.url,
                        "labels": list(issue.labels),
                    },
                )

        repositories_by_id = {item["id"]: item for item in repositories}
        for run in self.store.list_runs():
            repository = repositories_by_id.get(run["repository_id"])
            if repository is None:
                continue
            if run["state"] in TERMINAL_RUN_STATES:
                self.store.adapt_nodes_after_run(
                    run["id"], self.config.stale_run_threshold
                )
            elif run.get("pull_request") is not None:
                self._poll_pull_request(repository, run)

        focused_run_ids: set[int] = set()
        for repository in repositories:
            runs = self._ordered_runs(repository["id"], [
                run
                for run in self.store.list_runs(repository["id"])
                if run["state"] not in TERMINAL_RUN_STATES
            ])
            source_active = [
                run
                for run in runs
                if run["state"] in _SOURCE_ACTIVE_RUN_STATES
            ]
            if source_active:
                focused = source_active[0]
                focused_run_ids.add(focused["id"])
                self._advance_run(repository, focused)
                continue

            pending_feedback = self._pending_feedback_selection(runs)
            if pending_feedback is not None:
                focused, packages = pending_feedback
                if packages is not None:
                    self.store.create_pass(
                        focused["id"],
                        "feedback",
                        {"feedback": packages},
                    )
                self.store.transition_run(
                    focused["id"],
                    "SPECIFYING",
                    branch=focused.get("branch"),
                    pull_request=focused.get("pull_request"),
                )
                focused_run_ids.add(focused["id"])
                continue

            now = self._clock()
            listening_runs = [
                run for run in runs if run["state"] == "PR_LISTENING"
            ]
            if any(
                run.get("pr_listening_since") is None
                or now - float(run["pr_listening_since"])
                < self.config.pr_silence_seconds
                for run in listening_runs
            ):
                continue
            if listening_runs and not self.config.auto_merge:
                for listening_run in listening_runs:
                    pull_request = listening_run["pull_request"]
                    validated_head = pull_request.get("validated_head_sha")
                    if (
                        not isinstance(validated_head, str)
                        or not validated_head
                        or pull_request["head_sha"] != validated_head
                    ):
                        continue
                    self.store.transition_run(
                        listening_run["id"],
                        "PENDING_MERGE",
                        branch=listening_run.get("branch"),
                        pull_request=listening_run.get("pull_request"),
                        pr_listening_since=listening_run.get(
                            "pr_listening_since"
                        ),
                    )
                continue
            direct_publication_blocked = False
            for listening_run in listening_runs:
                pull_request = listening_run["pull_request"]
                validated_head = pull_request.get("validated_head_sha")
                if (
                    not isinstance(validated_head, str)
                    or not validated_head
                    or pull_request["head_sha"] != validated_head
                ):
                    direct_publication_blocked = True
                    continue
                if not self.github.publish_validated_to_target(
                    repository["github_repository"],
                    repository["target_branch"],
                    self._workspace(repository["id"], listening_run["id"]),
                    validated_head,
                    issue_branch=pull_request["branch"],
                ):
                    direct_publication_blocked = True
                    continue
                published_pull = self.github.pull_request(
                    repository["github_repository"],
                    int(pull_request["number"]),
                )
                published_reference = asdict(published_pull)
                published_reference["validated_head_sha"] = validated_head
                if not published_pull.merged:
                    direct_publication_blocked = True
                    self.store.transition_run(
                        listening_run["id"],
                        "PR_LISTENING",
                        branch=published_pull.branch,
                        pull_request=published_reference,
                        pr_listening_since=listening_run.get(
                            "pr_listening_since"
                        ),
                    )
                    continue
                self.store.transition_run(
                    listening_run["id"],
                    "COMPLETED",
                    branch=published_pull.branch,
                    pull_request=published_reference,
                )
                self.store.adapt_nodes_after_run(
                    listening_run["id"], self.config.stale_run_threshold
                )

            if direct_publication_blocked:
                continue
            if any(run["state"] == "PENDING_MERGE" for run in runs):
                continue
            queued = next(
                (run for run in runs if run["state"] == "QUEUED"),
                None,
            )
            if queued is not None:
                focused_run_ids.add(queued["id"])
                self._advance_run(repository, queued)
        self._start_workers(focused_run_ids)

    def _pending_feedback_selection(
        self,
        runs: list[dict],
    ) -> tuple[dict, list[dict] | None] | None:
        for run in runs:
            if run["state"] not in {"PR_LISTENING", "PENDING_MERGE"}:
                continue
            pending = [
                item
                for item in self.store.list_feedback(run["id"])
                if item["status"] == "PENDING"
            ]
            if not pending:
                continue
            claimed_ids = self._claimed_feedback_ids(
                self.store.list_passes(run["id"])
            )
            if any(item["external_id"] in claimed_ids for item in pending):
                return run, None
            packages = [
                item["package"]
                for item in pending
                if item["external_id"] not in claimed_ids
            ]
            if packages:
                return run, packages
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=True)
        self._reap_workers()

    def _workspace(self, repository_id: int, run_id: int) -> Path:
        return self.data_dir / "workspaces" / str(repository_id) / str(run_id)

    @staticmethod
    def _source_copy_ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if Application._source_name_ignored(name)}

    @staticmethod
    def _source_name_ignored(name: str) -> bool:
        return name in _SOURCE_IGNORED_NAMES or name.startswith(".repogents")

    @staticmethod
    def _manifest_path(root: Path, relative_path: str) -> Path:
        return root.joinpath(*PurePosixPath(relative_path).parts)

    @staticmethod
    def _validated_source_link_target(
        relative_path: str,
        target: str,
    ) -> None:
        target_path = PurePosixPath(target)
        if target_path.is_absolute():
            raise ValueError(
                f"source symlink target must be relative: {relative_path}"
            )
        resolved = list(PurePosixPath(relative_path).parent.parts)
        for part in target_path.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    raise ValueError(
                        "source symlink target escapes the source tree: "
                        f"{relative_path}"
                    )
                resolved.pop()
                continue
            if part == ".git" or part.startswith(".repogents"):
                raise ValueError(
                    "source symlink targets controller metadata: "
                    f"{relative_path}"
                )
            resolved.append(part)

    @classmethod
    def _source_manifest(
        cls,
        root: Path,
        *,
        excluded_roots: set[str] | None = None,
    ) -> dict[str, _SourceTreeEntry]:
        excluded_roots = excluded_roots or set()
        manifest: dict[str, _SourceTreeEntry] = {}

        def visit(directory: Path, parent: PurePosixPath | None = None) -> None:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
            for child in children:
                if cls._source_name_ignored(child.name):
                    continue
                if parent is None and child.name in excluded_roots:
                    continue
                relative = (
                    PurePosixPath(child.name)
                    if parent is None
                    else parent / child.name
                )
                relative_path = relative.as_posix()
                source_path = directory / child.name
                metadata = child.stat(follow_symlinks=False)
                mode = stat.S_IMODE(metadata.st_mode)
                if stat.S_ISLNK(metadata.st_mode):
                    target = os.readlink(source_path)
                    cls._validated_source_link_target(
                        relative_path,
                        target,
                    )
                    manifest[relative_path] = _SourceTreeEntry(
                        "symlink",
                        mode,
                        target,
                    )
                elif stat.S_ISDIR(metadata.st_mode):
                    manifest[relative_path] = _SourceTreeEntry(
                        "directory",
                        mode,
                        None,
                    )
                    visit(source_path, relative)
                elif stat.S_ISREG(metadata.st_mode):
                    digest = hashlib.sha256()
                    with source_path.open("rb") as source_file:
                        while chunk := source_file.read(1024 * 1024):
                            digest.update(chunk)
                    manifest[relative_path] = _SourceTreeEntry(
                        "file",
                        mode,
                        digest.hexdigest(),
                    )
                else:
                    raise ValueError(
                        f"unsupported source path type: {relative_path}"
                    )

        visit(root)
        return manifest


    @contextmanager
    def _source_snapshot(self, workspace: Path):
        with tempfile.TemporaryDirectory(
            prefix="repogents-source-",
            dir=self.data_dir,
        ) as temporary_directory:
            snapshot = Path(temporary_directory) / "workspace"
            with self._source_lock:
                if workspace.exists():
                    self._source_manifest(workspace)
                if workspace.exists():
                    shutil.copytree(
                        workspace,
                        snapshot,
                        symlinks=True,
                        ignore=self._source_copy_ignore,
                    )
                else:
                    snapshot.mkdir()
            yield snapshot

    @staticmethod
    def _remove_source_path(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
            metadata.st_mode
        ):
            shutil.rmtree(path)
        else:
            path.unlink()

    @staticmethod
    def _source_path_depth(relative_path: str) -> int:
        return len(PurePosixPath(relative_path).parts)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _fsync_source_tree(
        cls,
        root: Path,
        *,
        exclude_controller_metadata: bool = False,
    ) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | no_follow
        )
        file_flags = os.O_RDONLY | no_follow

        def sync_directory(descriptor: int) -> None:
            with os.scandir(descriptor) as scanned:
                entries = list(scanned)
            for entry in entries:
                if (
                    exclude_controller_metadata
                    and entry.name in {".git", ".repogents"}
                ):
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    child_descriptor = os.open(
                        entry.name,
                        directory_flags,
                        dir_fd=descriptor,
                    )
                    try:
                        opened = os.fstat(child_descriptor)
                        if (
                            not stat.S_ISDIR(opened.st_mode)
                            or opened.st_dev != metadata.st_dev
                            or opened.st_ino != metadata.st_ino
                        ):
                            raise RuntimeError(
                                "source directory changed while being flushed"
                            )
                        sync_directory(child_descriptor)
                    finally:
                        os.close(child_descriptor)
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    child_descriptor = os.open(
                        entry.name,
                        file_flags,
                        dir_fd=descriptor,
                    )
                    try:
                        opened = os.fstat(child_descriptor)
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or opened.st_dev != metadata.st_dev
                            or opened.st_ino != metadata.st_ino
                        ):
                            raise RuntimeError(
                                "source file changed while being flushed"
                            )
                        os.fsync(child_descriptor)
                    finally:
                        os.close(child_descriptor)
                    continue
                raise ValueError(
                    f"unsupported source path type while flushing: {entry.name}"
                )
            os.fsync(descriptor)

        root_descriptor = os.open(root, directory_flags)
        try:
            sync_directory(root_descriptor)
        finally:
            os.close(root_descriptor)

    @classmethod
    def _make_tree_removable(cls, root: Path) -> None:
        try:
            metadata = root.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(
            metadata.st_mode
        ):
            return
        os.chmod(
            root,
            stat.S_IMODE(metadata.st_mode)
            | stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR,
        )
        with os.scandir(root) as scanned:
            children = [Path(entry.path) for entry in scanned]
        for child in children:
            cls._make_tree_removable(child)

    def _source_import_journal_root(self) -> Path:
        return self.data_dir / "source-import-journals"

    def _create_source_import_journal(
        self,
        workspace: Path,
        repository_id: int,
        run_id: int,
        work_id: int,
    ) -> _SourceImportJournal:
        root = self._source_import_journal_root()
        root.mkdir(parents=True, exist_ok=True)
        self._fsync_directory(self.data_dir)
        journal_path = root / f"work-{work_id}"
        if os.path.lexists(journal_path):
            raise RuntimeError(
                f"source import recovery journal already exists for work {work_id}"
            )
        staging = Path(
            tempfile.mkdtemp(prefix=".pending-", dir=root)
        )
        try:
            backup = staging / "source"
            if workspace.exists():
                shutil.copytree(
                    workspace,
                    backup,
                    symlinks=True,
                    ignore=self._source_copy_ignore,
                    copy_function=os.link,
                )
            else:
                backup.mkdir()
            self._fsync_source_tree(backup)
            metadata = {
                "version": _SOURCE_IMPORT_JOURNAL_VERSION,
                "repository_id": repository_id,
                "run_id": run_id,
                "work_id": work_id,
            }
            intent = staging / "intent.json"
            with intent.open("x", encoding="utf-8") as intent_file:
                json.dump(
                    metadata,
                    intent_file,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                intent_file.flush()
                os.fsync(intent_file.fileno())
            self._fsync_directory(staging)
            os.replace(staging, journal_path)
            self._fsync_directory(root)
        except BaseException:
            if os.path.lexists(staging):
                self._make_tree_removable(staging)
                shutil.rmtree(staging)
            raise
        return _SourceImportJournal(
            path=journal_path,
            backup=journal_path / "source",
            repository_id=repository_id,
            run_id=run_id,
            work_id=work_id,
        )

    @staticmethod
    def _strict_source_import_id(value: object, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"source import journal {label} must be a positive integer"
            )
        return value

    def _load_source_import_journal(
        self,
        journal_path: Path,
    ) -> _SourceImportJournal:
        try:
            metadata = json.loads(
                (journal_path / "intent.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"invalid source import journal: {journal_path.name}"
            ) from error
        expected_keys = {
            "version",
            "repository_id",
            "run_id",
            "work_id",
        }
        if not isinstance(metadata, dict) or set(metadata) != expected_keys:
            raise ValueError(
                f"invalid source import journal identity: {journal_path.name}"
            )
        if metadata["version"] != _SOURCE_IMPORT_JOURNAL_VERSION:
            raise ValueError(
                f"unsupported source import journal version: {journal_path.name}"
            )
        repository_id = self._strict_source_import_id(
            metadata["repository_id"],
            "repository_id",
        )
        run_id = self._strict_source_import_id(metadata["run_id"], "run_id")
        work_id = self._strict_source_import_id(
            metadata["work_id"],
            "work_id",
        )
        if journal_path.name != f"work-{work_id}":
            raise ValueError(
                f"source import journal path does not match work {work_id}"
            )
        backup = journal_path / "source"
        try:
            backup_metadata = backup.lstat()
        except FileNotFoundError as error:
            raise ValueError(
                f"source import journal has no backup: {journal_path.name}"
            ) from error
        if not stat.S_ISDIR(backup_metadata.st_mode) or stat.S_ISLNK(
            backup_metadata.st_mode
        ):
            raise ValueError(
                f"source import journal backup is not a directory: {journal_path.name}"
            )
        return _SourceImportJournal(
            path=journal_path,
            backup=backup,
            repository_id=repository_id,
            run_id=run_id,
            work_id=work_id,
        )

    def _source_import_work_state(
        self,
        journal: _SourceImportJournal,
    ) -> str:
        repository = self.store.get_repository(journal.repository_id)
        run = self.store.get_run(journal.run_id)
        if (
            repository is None
            or run is None
            or run["repository_id"] != journal.repository_id
        ):
            raise ValueError(
                "source import journal repository/run identity mismatch: "
                f"work {journal.work_id}"
            )
        work = next(
            (
                item
                for item in self.store.list_work_items(journal.run_id)
                if item["id"] == journal.work_id
            ),
            None,
        )
        if work is None or work["run_id"] != journal.run_id:
            raise ValueError(
                "source import journal work identity mismatch: "
                f"work {journal.work_id}"
            )
        return cast(str, work["state"])

    def _restore_source_import_journal(
        self,
        journal: _SourceImportJournal,
    ) -> None:
        workspace = self._workspace(
            journal.repository_id,
            journal.run_id,
        )
        workspace.mkdir(parents=True, exist_ok=True)
        current = self._source_manifest(workspace)
        desired = self._source_manifest(journal.backup)
        self._import_source_delta(
            workspace,
            journal.backup,
            current,
            desired,
        )
        self._fsync_source_tree(
            workspace,
            exclude_controller_metadata=True,
        )

    def _discard_source_import_journal(
        self,
        journal_path: Path,
    ) -> None:
        self._make_tree_removable(journal_path)
        shutil.rmtree(journal_path)
        self._fsync_directory(self._source_import_journal_root())

    def _recover_source_import_journals(self) -> None:
        root = self._source_import_journal_root()
        if not root.exists():
            return
        with self._source_lock:
            with os.scandir(root) as scanned:
                paths = sorted(
                    (Path(entry.path) for entry in scanned),
                    key=lambda path: path.name,
                )
            for path in paths:
                if path.name.startswith(".pending-"):
                    self._make_tree_removable(path)
                    shutil.rmtree(path)
                    self._fsync_directory(root)
                    continue
                journal = self._load_source_import_journal(path)
                state = self._source_import_work_state(journal)
                if state not in _SOURCE_IMPORT_SUCCESS_STATES:
                    self._restore_source_import_journal(journal)
                self._discard_source_import_journal(path)

    @staticmethod
    def _atomic_replace_source_file(
        source: Path,
        destination: Path,
        mode: int,
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".repogents-import-",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                descriptor = -1
                with source.open("rb") as source_file:
                    shutil.copyfileobj(
                        source_file,
                        temporary_file,
                        length=1024 * 1024,
                    )
                temporary_file.flush()
                os.fchmod(temporary_file.fileno(), mode)
                os.fsync(temporary_file.fileno())
            os.replace(temporary, destination)
            Application._fsync_directory(destination.parent)
        finally:
            if os.path.lexists(temporary):
                temporary.unlink()

    @contextmanager
    def _durable_source_import(
        self,
        workspace: Path,
        desired_root: Path,
        baseline: dict[str, _SourceTreeEntry],
        desired: dict[str, _SourceTreeEntry],
        *,
        repository_id: int,
        run_id: int,
        work_id: int,
    ):
        changed_paths = {
            path
            for path in baseline.keys() | desired.keys()
            if baseline.get(path) != desired.get(path)
        }
        with self._source_lock:
            journal = None
            if changed_paths:
                self._source_manifest(workspace)
                journal = self._create_source_import_journal(
                    workspace,
                    repository_id,
                    run_id,
                    work_id,
                )
            try:
                applied_paths = self._import_source_delta(
                    workspace,
                    desired_root,
                    baseline,
                    desired,
                )
                if journal is not None:
                    self._fsync_source_tree(
                        workspace,
                        exclude_controller_metadata=True,
                    )
                yield applied_paths
            except BaseException:
                if journal is None:
                    raise
                state = self._source_import_work_state(journal)
                if state in _SOURCE_IMPORT_SUCCESS_STATES:
                    self._discard_source_import_journal(journal.path)
                    return
                self._restore_source_import_journal(journal)
                self._discard_source_import_journal(journal.path)
                raise
            else:
                if journal is not None:
                    self._discard_source_import_journal(journal.path)

    def _import_source_delta(
        self,
        workspace: Path,
        desired_root: Path,
        baseline: dict[str, _SourceTreeEntry],
        desired: dict[str, _SourceTreeEntry],
    ) -> list[str]:
        changed_paths = {
            path
            for path in baseline.keys() | desired.keys()
            if baseline.get(path) != desired.get(path)
        }
        if not changed_paths:
            return []

        with self._source_lock:
            current = self._source_manifest(workspace)
            checked_paths = set(changed_paths)
            for relative_path in changed_paths:
                for parent in PurePosixPath(relative_path).parents:
                    if parent != PurePosixPath("."):
                        checked_paths.add(parent.as_posix())
                baseline_entry = baseline.get(relative_path)
                desired_entry = desired.get(relative_path)
                if (
                    baseline_entry is not None
                    and baseline_entry.kind == "directory"
                    and (
                        desired_entry is None
                        or desired_entry.kind != "directory"
                    )
                ):
                    prefix = relative_path + "/"
                    checked_paths.update(
                        path for path in current if path.startswith(prefix)
                    )

            for relative_path in sorted(checked_paths):
                current_entry = current.get(relative_path)
                if current_entry not in {
                    baseline.get(relative_path),
                    desired.get(relative_path),
                }:
                    raise ValueError(
                        "stale overlapping source path: "
                        f"{relative_path}"
                    )

            paths_to_apply = {
                path
                for path in changed_paths
                if current.get(path) != desired.get(path)
            }
            if not paths_to_apply:
                return sorted(changed_paths)

            permission_directories: set[str] = set()
            for relative_path in paths_to_apply:
                for parent in PurePosixPath(relative_path).parents:
                    if parent == PurePosixPath("."):
                        continue
                    parent_path = parent.as_posix()
                    parent_entry = current.get(parent_path)
                    if (
                        parent_entry is not None
                        and parent_entry.kind == "directory"
                    ):
                        permission_directories.add(parent_path)
                current_entry = current.get(relative_path)
                if (
                    current_entry is not None
                    and current_entry.kind == "directory"
                ):
                    permission_directories.add(relative_path)

            for relative_path in sorted(
                permission_directories,
                key=self._source_path_depth,
            ):
                directory = self._manifest_path(workspace, relative_path)
                try:
                    metadata = directory.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    os.chmod(
                        directory,
                        stat.S_IMODE(metadata.st_mode)
                        | stat.S_IRUSR
                        | stat.S_IWUSR
                        | stat.S_IXUSR,
                    )

            desired_directories = [
                path
                for path in paths_to_apply
                if desired.get(path) is not None
                and cast(_SourceTreeEntry, desired[path]).kind
                == "directory"
            ]
            try:
                for relative_path in sorted(
                    paths_to_apply,
                    key=self._source_path_depth,
                    reverse=True,
                ):
                    current_entry = current.get(relative_path)
                    desired_entry = desired.get(relative_path)
                    if current_entry is None:
                        continue
                    if (
                        desired_entry is None
                        or current_entry.kind != desired_entry.kind
                        or (
                            current_entry.kind == "symlink"
                            and current_entry != desired_entry
                        )
                    ):
                        self._remove_source_path(
                            self._manifest_path(workspace, relative_path)
                        )

                for relative_path in sorted(
                    desired_directories,
                    key=self._source_path_depth,
                ):
                    directory = self._manifest_path(workspace, relative_path)
                    if not os.path.lexists(directory):
                        directory.mkdir()

                for relative_path in sorted(
                    paths_to_apply,
                    key=self._source_path_depth,
                ):
                    desired_entry = desired.get(relative_path)
                    if desired_entry is None:
                        continue
                    destination = self._manifest_path(
                        workspace,
                        relative_path,
                    )
                    if desired_entry.kind == "file":
                        self._atomic_replace_source_file(
                            self._manifest_path(
                                desired_root,
                                relative_path,
                            ),
                            destination,
                            desired_entry.mode,
                        )
                    elif desired_entry.kind == "symlink":
                        if not os.path.lexists(destination):
                            os.symlink(
                                cast(str, desired_entry.value),
                                destination,
                            )
            finally:
                directories_to_restore = permission_directories | set(
                    desired_directories
                )
                for relative_path in sorted(
                    directories_to_restore,
                    key=self._source_path_depth,
                    reverse=True,
                ):
                    desired_entry = desired.get(relative_path)
                    if (
                        desired_entry is None
                        or desired_entry.kind != "directory"
                    ):
                        continue
                    directory = self._manifest_path(
                        workspace,
                        relative_path,
                    )
                    try:
                        metadata = directory.lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        os.chmod(directory, desired_entry.mode)

        return sorted(changed_paths)

    @staticmethod
    def _validated_relative_path(value: object, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a nonempty relative path")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
            or any(part in {".git", ".repogents"} for part in path.parts)
        ):
            raise ValueError(f"{label} must be a normalized source path")
        return value

    @classmethod
    def _validated_operation_path_list(
        cls,
        operation_state: dict,
        field: str,
        *,
        allow_missing: bool = False,
    ) -> list[str]:
        if field not in operation_state and allow_missing:
            return []
        values = operation_state.get(field)
        label = field.replace("_", " ")
        if (
            not isinstance(values, list)
            or any(
                not isinstance(path, str) or not path
                for path in values
            )
            or values != sorted(set(values))
        ):
            raise ValueError(
                f"repository operation {label} must be sorted source paths"
            )
        for path in values:
            cls._validated_relative_path(
                path,
                f"repository operation {label.removesuffix('s')}",
            )
        return list(values)

    @classmethod
    def _validated_operation_artifact_manifest(
        cls,
        manifest: object,
        destination: Path,
    ) -> dict[str, dict[str, str]]:
        if not isinstance(manifest, dict):
            raise ValueError(
                "repository operation artifact manifest must be an object"
            )
        normalized: dict[str, dict[str, str]] = {}
        for semantic_path, artifacts in manifest.items():
            semantic_path = cls._validated_relative_path(
                semantic_path,
                "repository operation semantic path",
            )
            if not isinstance(artifacts, dict):
                raise ValueError(
                    "repository operation path artifacts must be an object"
                )
            unexpected_stages = set(artifacts) - {
                "base",
                "ours",
                "theirs",
            }
            if unexpected_stages:
                raise ValueError(
                    "repository operation artifact stage is invalid"
                )
            normalized_artifacts: dict[str, str] = {}
            for stage in ("base", "ours", "theirs"):
                if stage not in artifacts:
                    continue
                relative_path = cls._validated_relative_path(
                    artifacts[stage],
                    f"repository operation {stage} artifact",
                )
                artifact_path = cls._manifest_path(
                    destination,
                    relative_path,
                )
                try:
                    metadata = artifact_path.lstat()
                except FileNotFoundError as error:
                    raise ValueError(
                        "repository operation artifact is missing: "
                        f"{relative_path}"
                    ) from error
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(
                        "repository operation artifact must be a regular file: "
                        f"{relative_path}"
                    )
                normalized_artifacts[stage] = relative_path
            normalized[semantic_path] = normalized_artifacts
        return normalized

    def _operation_artifacts_directory(
        self,
        run_id: int,
        trigger: dict,
    ) -> Path:
        failed_pass_id = trigger.get("failed_pass_id")
        failed_stage = trigger.get("failed_stage")
        if (
            isinstance(failed_pass_id, bool)
            or not isinstance(failed_pass_id, int)
            or failed_stage
            not in {
                "continue_repository_operation",
                "prepare_publication",
            }
        ):
            raise ValueError("operation failure artifact identity is invalid")
        return (
            self.data_dir
            / "operation-artifacts"
            / str(run_id)
            / f"{failed_pass_id}-{failed_stage}"
        )

    def _work_evidence_directory(self, run_id: int, work_id: int) -> Path:
        return self.data_dir / "work-evidence" / str(run_id) / str(work_id)

    @staticmethod
    def _declared_work_evidence_paths(artifacts: list) -> list[str]:
        paths: list[str] = []
        for artifact in artifacts:
            if not isinstance(artifact, str) or not (
                artifact == ".repogents"
                or artifact.startswith(".repogents/")
            ):
                continue
            path = PurePosixPath(artifact)
            if (
                artifact == ".repogents"
                or path.as_posix() != artifact
                or len(path.parts) < 2
                or any(part in {"", ".", ".."} for part in path.parts)
                or path.parts[:2] == (".repogents", "dependencies")
            ):
                raise ValueError(
                    "declared work evidence must be a normalized file below "
                    ".repogents and outside its dependencies transport"
                )
            paths.append(artifact)
        if len(paths) != len(set(paths)):
            raise ValueError("declared work evidence paths must be unique")
        return sorted(paths)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source_file:
            while chunk := source_file.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _work_evidence_integrity(
        self,
        workspace: Path,
        artifacts: list,
    ) -> dict[Path, str]:
        integrity: dict[Path, str] = {}
        for declared_path in self._declared_work_evidence_paths(artifacts):
            source = workspace.joinpath(*PurePosixPath(declared_path).parts)
            try:
                metadata = source.lstat()
            except FileNotFoundError as error:
                raise ValueError(
                    f"declared work evidence is unavailable: {declared_path}"
                ) from error
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"declared work evidence must be a regular file: {declared_path}"
                )
            integrity[source] = self._file_sha256(source)
        return integrity

    def _load_work_evidence(
        self,
        run_id: int,
        work_id: int,
    ) -> dict[str, str]:
        root = self._work_evidence_directory(run_id, work_id)
        try:
            manifest = json.loads(
                (root / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"durable evidence for work {work_id} is unavailable"
            ) from error
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"version", "run_id", "work_id", "files"}
            or manifest["version"] != 1
            or manifest["run_id"] != run_id
            or manifest["work_id"] != work_id
            or not isinstance(manifest["files"], dict)
        ):
            raise ValueError(
                f"durable evidence manifest for work {work_id} is invalid"
            )
        files: dict[str, str] = {}
        for declared_path, expected_digest in manifest["files"].items():
            if (
                self._declared_work_evidence_paths([declared_path])
                != [declared_path]
                or not isinstance(expected_digest, str)
                or len(expected_digest) != 64
            ):
                raise ValueError(
                    f"durable evidence manifest for work {work_id} is invalid"
                )
            stored = root.joinpath(
                "files", *PurePosixPath(declared_path).parts[1:]
            )
            try:
                metadata = stored.lstat()
            except FileNotFoundError as error:
                raise ValueError(
                    f"durable evidence for work {work_id} is incomplete"
                ) from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or self._file_sha256(stored) != expected_digest
            ):
                raise ValueError(
                    f"durable evidence for work {work_id} failed integrity validation"
                )
            files[declared_path] = expected_digest

        expected_tree = {
            "manifest.json",
            *{
                "files/" + "/".join(PurePosixPath(path).parts[1:])
                for path in files
            },
        }
        expected_directories = {"files"} if files else set()
        for path in files:
            relative = PurePosixPath("files", *PurePosixPath(path).parts[1:])
            expected_directories.update(
                parent.as_posix()
                for parent in relative.parents
                if parent != PurePosixPath(".")
            )
        actual_files: set[str] = set()
        actual_directories: set[str] = set()
        for directory, dirnames, filenames in os.walk(root):
            directory_path = Path(directory)
            for dirname in dirnames:
                path = directory_path / dirname
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise ValueError(
                        f"durable evidence for work {work_id} contains an unsupported path"
                    )
                actual_directories.add(path.relative_to(root).as_posix())
            for filename in filenames:
                path = directory_path / filename
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError(
                        f"durable evidence for work {work_id} contains an unsupported path"
                    )
                actual_files.add(path.relative_to(root).as_posix())
        if (
            actual_files != expected_tree
            or actual_directories != expected_directories
        ):
            raise ValueError(
                f"durable evidence manifest for work {work_id} does not describe its tree"
            )
        return files

    def _capture_work_evidence(
        self,
        run_id: int,
        work_id: int,
        workspace: Path,
        artifacts: list,
    ) -> dict[str, str]:
        declared_paths = self._declared_work_evidence_paths(artifacts)
        if not declared_paths:
            return {}
        integrity = self._work_evidence_integrity(workspace, artifacts)
        files = {
            declared_path: integrity[
                workspace.joinpath(*PurePosixPath(declared_path).parts)
            ]
            for declared_path in declared_paths
        }

        destination = self._work_evidence_directory(run_id, work_id)
        if os.path.lexists(destination):
            if self._load_work_evidence(run_id, work_id) != files:
                raise ValueError(
                    f"durable evidence for work {work_id} conflicts with a prior attempt"
                )
            return files

        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        self._fsync_directory(parent)
        self._fsync_directory(parent.parent)
        self._fsync_directory(self.data_dir)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{work_id}.pending-", dir=parent)
        )
        try:
            for declared_path in declared_paths:
                source = workspace.joinpath(
                    *PurePosixPath(declared_path).parts
                )
                stored = staging.joinpath(
                    "files", *PurePosixPath(declared_path).parts[1:]
                )
                stored.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, stored)
            manifest = {
                "version": 1,
                "run_id": run_id,
                "work_id": work_id,
                "files": files,
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True),
                encoding="utf-8",
            )
            self._fsync_source_tree(staging)
            os.replace(staging, destination)
            self._fsync_directory(parent)
        except BaseException:
            if os.path.lexists(staging):
                shutil.rmtree(staging)
            raise
        return self._load_work_evidence(run_id, work_id)

    def _materialize_work_evidence(
        self,
        run_id: int,
        providers: list[dict],
        workspace: Path,
    ) -> tuple[list[dict], dict[Path, str]]:
        materialized: list[dict] = []
        integrity: dict[Path, str] = {}
        for provider in sorted(providers, key=lambda item: item["id"]):
            result = provider.get("result") or {}
            declared_paths = self._declared_work_evidence_paths(
                result.get("artifacts", [])
            )
            if not declared_paths:
                continue
            files = self._load_work_evidence(run_id, provider["id"])
            if set(files) != set(declared_paths):
                raise ValueError(
                    f"durable evidence for work {provider['id']} does not match its result"
                )
            provider_files = []
            for declared_path in declared_paths:
                source = self._work_evidence_directory(
                    run_id, provider["id"]
                ).joinpath(
                    "files", *PurePosixPath(declared_path).parts[1:]
                )
                relative = PurePosixPath(
                    ".repogents", "dependencies", f"work-{provider['id']}",
                    *PurePosixPath(declared_path).parts[1:],
                ).as_posix()
                destination = workspace.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                destination.chmod(0o444)
                integrity[destination] = files[declared_path]
                provider_files.append(
                    {
                        "declared_path": declared_path,
                        "workspace_path": relative,
                        "sha256": files[declared_path],
                    }
                )
            materialized.append(
                {
                    "work_id": provider["id"],
                    "work_key": provider["key"],
                    "files": provider_files,
                }
            )
        return materialized, integrity

    def _verify_materialized_work_evidence(
        self,
        integrity: dict[Path, str],
        *,
        label: str = "materialized dependency evidence",
    ) -> None:
        for path, expected_digest in integrity.items():
            try:
                metadata = path.lstat()
            except FileNotFoundError as error:
                raise ValueError(
                    f"{label} was removed"
                ) from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or self._file_sha256(path) != expected_digest
            ):
                raise ValueError(
                    f"{label} was modified"
                )

    def _export_operation_artifacts(
        self,
        run_id: int,
        workspace: Path,
        trigger: dict,
    ) -> None:
        destination = self._operation_artifacts_directory(run_id, trigger)
        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        self._fsync_directory(parent)
        self._fsync_directory(parent.parent)
        self._fsync_directory(self.data_dir)

        def validate_exact_tree(
            root: Path,
            manifest: dict[str, dict[str, str]],
            *,
            includes_manifest: bool,
        ) -> None:
            try:
                root_metadata = root.lstat()
            except FileNotFoundError as error:
                raise ValueError(
                    "repository operation artifact tree is unavailable"
                ) from error
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise ValueError(
                    "repository operation artifact tree must be a directory"
                )

            expected_files = {
                relative_path
                for artifacts in manifest.values()
                for relative_path in artifacts.values()
            }
            if ".manifest.json" in expected_files:
                raise ValueError(
                    "repository operation artifact conflicts with its manifest"
                )
            if includes_manifest:
                expected_files.add(".manifest.json")
            expected_directories: set[str] = set()
            for relative_path in expected_files:
                for ancestor in PurePosixPath(relative_path).parents:
                    if ancestor != PurePosixPath("."):
                        expected_directories.add(ancestor.as_posix())

            actual_files: set[str] = set()
            actual_directories: set[str] = set()

            def visit(
                directory: Path,
                parent_path: PurePosixPath | None = None,
            ) -> None:
                with os.scandir(directory) as scanned:
                    entries = list(scanned)
                for entry in entries:
                    relative = (
                        PurePosixPath(entry.name)
                        if parent_path is None
                        else parent_path / entry.name
                    )
                    relative_path = relative.as_posix()
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        actual_directories.add(relative_path)
                        visit(Path(entry.path), relative)
                    elif stat.S_ISREG(metadata.st_mode):
                        actual_files.add(relative_path)
                    else:
                        raise ValueError(
                            "repository operation artifact tree contains "
                            f"an unsupported path: {relative_path}"
                        )

            visit(root)
            if (
                actual_files != expected_files
                or actual_directories != expected_directories
            ):
                raise ValueError(
                    "repository operation artifact manifest does not exactly "
                    "describe its tree"
                )

        def validate_published_tree(
            root: Path,
        ) -> dict[str, dict[str, str]]:
            manifest_path = root / ".manifest.json"
            try:
                metadata = manifest_path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(
                        "repository operation artifact manifest must be a "
                        "regular file"
                    )
                manifest_bytes = manifest_path.read_bytes()
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(
                    "repository operation artifacts are unavailable"
                ) from error
            normalized = self._validated_operation_artifact_manifest(
                manifest,
                root,
            )
            expected_manifest = json.dumps(
                normalized,
                sort_keys=True,
            ).encode("utf-8")
            if manifest_bytes != expected_manifest:
                raise ValueError(
                    "repository operation artifact manifest is not exact"
                )
            validate_exact_tree(
                root,
                normalized,
                includes_manifest=True,
            )
            return normalized

        if os.path.lexists(destination):
            validate_published_tree(destination)
            self._fsync_source_tree(destination)
            self._fsync_directory(parent)
            return

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.pending-",
                dir=parent,
            )
        )
        try:
            manifest = self.github.export_repository_operation_artifacts(
                workspace,
                staging,
            )
            normalized = self._validated_operation_artifact_manifest(
                manifest,
                staging,
            )
            validate_exact_tree(
                staging,
                normalized,
                includes_manifest=False,
            )
            manifest_bytes = json.dumps(
                normalized,
                sort_keys=True,
            ).encode("utf-8")
            with (staging / ".manifest.json").open("xb") as manifest_file:
                manifest_file.write(manifest_bytes)
                manifest_file.flush()
                os.fsync(manifest_file.fileno())
            validate_published_tree(staging)
            self._fsync_source_tree(staging)
            os.replace(staging, destination)
            self._fsync_directory(parent)
        except Exception:
            if os.path.lexists(staging):
                self._make_tree_removable(staging)
                shutil.rmtree(staging)
            raise

    def _operation_artifact_manifest(
        self,
        run_id: int,
        trigger: dict,
    ) -> tuple[Path, dict[str, dict[str, str]]]:
        destination = self._operation_artifacts_directory(run_id, trigger)
        manifest_path = destination / ".manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise ValueError(
                "repository operation artifacts are unavailable"
            ) from error
        return destination, self._validated_operation_artifact_manifest(
            manifest,
            destination,
        )

    def _materialize_operation_artifacts(
        self,
        run_id: int,
        execution_pass: dict,
        snapshot: Path,
    ) -> tuple[dict[str, dict[str, str]], str]:
        source, manifest = self._operation_artifact_manifest(
            run_id,
            execution_pass["trigger_json"],
        )
        root_name = ".repogents-operation-artifacts"
        suffix = 0
        while os.path.lexists(snapshot / root_name):
            suffix += 1
            root_name = f".repogents-operation-artifacts-{suffix}"
        artifact_root = snapshot / root_name
        artifact_root.mkdir()
        materialized: dict[str, dict[str, str]] = {}
        for semantic_path, artifacts in manifest.items():
            materialized_artifacts: dict[str, str] = {}
            for stage, relative_path in artifacts.items():
                source_path = self._manifest_path(source, relative_path)
                destination_path = self._manifest_path(
                    artifact_root,
                    relative_path,
                )
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(
                    source_path,
                    destination_path,
                    follow_symlinks=False,
                )
                materialized_artifacts[stage] = (
                    PurePosixPath(root_name) / relative_path
                ).as_posix()
            materialized[semantic_path] = materialized_artifacts
        return materialized, root_name

    def _record_operation_failure(
        self,
        repository: dict,
        run: dict,
        failed_pass: dict,
        failed_stage: str,
        error: subprocess.CalledProcessError,
    ) -> None:
        workspace = self._workspace(repository["id"], run["id"])
        operation_state = self.github.repository_operation_state(workspace)
        if not isinstance(operation_state, dict):
            raise ValueError("repository operation state must be an object")
        rebase_in_progress = operation_state.get("rebase_in_progress")
        if not isinstance(rebase_in_progress, bool):
            raise ValueError(
                "repository operation rebase state must be boolean"
            )
        unmerged_paths = self._validated_operation_path_list(
            operation_state,
            "unmerged_paths",
        )
        staged_paths = self._validated_operation_path_list(
            operation_state,
            "staged_paths",
        )
        unstaged_paths = self._validated_operation_path_list(
            operation_state,
            "unstaged_paths",
        )
        untracked_paths = self._validated_operation_path_list(
            operation_state,
            "untracked_paths",
        )
        trigger = {
            "failed_stage": failed_stage,
            "command": error.cmd,
            "returncode": error.returncode,
            "stdout": error.stdout,
            "stderr": error.stderr,
            "target_branch": repository["target_branch"],
            "failed_pass_id": failed_pass["id"],
            "workspace": {
                "repository_id": repository["id"],
                "run_id": run["id"],
                "rebase_in_progress": rebase_in_progress,
                "unmerged_paths": unmerged_paths,
                "staged_paths": staged_paths,
                "unstaged_paths": unstaged_paths,
                "untracked_paths": untracked_paths,
            },
        }
        origin_feedback_pass_id = self._feedback_origin_pass_id(failed_pass)
        if origin_feedback_pass_id is not None:
            trigger["origin_feedback_pass_id"] = origin_feedback_pass_id
        self._export_operation_artifacts(
            run["id"],
            workspace,
            trigger,
        )
        self.store.create_pass(
            run["id"],
            "operation_failure",
            trigger,
        )
        self.store.transition_run(run["id"], "SPECIFYING")

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

    def _begin_run(self, repository: dict, run: dict) -> None:
        workspace = self._workspace(repository["id"], run["id"])
        self.github.checkout(
            repository["github_repository"], repository["target_branch"], workspace
        )
        if not self.store.list_passes(run["id"]):
            self.store.create_pass(run["id"], "issue", run["issue_json"])
        self.store.transition_run(run["id"], "SPECIFYING")

    @staticmethod
    def _feedback_origin_pass_id(execution_pass: dict) -> int | None:
        if execution_pass["trigger_type"] == "feedback":
            return execution_pass["id"]
        if execution_pass["trigger_type"] not in {
            "operation_failure",
            "publication_revalidation",
            "validation_failure",
            "work_failure",
        }:
            return None
        origin = execution_pass["trigger_json"].get("origin_feedback_pass_id")
        if isinstance(origin, bool) or not isinstance(origin, int):
            return None
        return origin

    def _pass_feedback_context(
        self,
        run_id: int,
        execution_pass: dict,
        *,
        in_scope_only: bool = True,
    ) -> tuple[list[dict], str | None]:
        origin_pass_id = self._feedback_origin_pass_id(execution_pass)
        if origin_pass_id is None:
            return [], None
        origin_pass = next(
            (
                item
                for item in self.store.list_passes(run_id)
                if item["id"] == origin_pass_id
                and item["trigger_type"] == "feedback"
            ),
            None,
        )
        if origin_pass is None:
            return [], None
        packages = origin_pass["trigger_json"].get("feedback", [])
        if not isinstance(packages, list):
            return [], None
        run = self.store.get_run(run_id)
        reference = None if run is None else run.get("pull_request")
        pull_request_diff = (
            reference.get("diff")
            if isinstance(reference, dict)
            else None
        )
        allowed_ids: set[str] | None = None
        if in_scope_only:
            scope_result = self.store.get_feedback_scope_result(
                run_id,
                origin_pass_id,
            )
            allowed_ids = {
                item["external_id"]
                for item in (scope_result or {}).get("dispositions", [])
                if isinstance(item, dict)
                and item.get("valid") is True
                and item.get("in_scope") is True
                and isinstance(item.get("external_id"), str)
            }
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
            if isinstance(package, dict)
            and (
                allowed_ids is None
                or package.get("external_id") in allowed_ids
            )
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

    @staticmethod
    def _specification_definition(specification: dict) -> dict:
        return {
            field: specification.get(field, [] if field in {
                "requirement_keys", "acceptance_traceability"
            } else None)
            for field in (
                "key",
                "title",
                "description",
                "acceptance_criteria",
                "requirement_keys",
                "acceptance_traceability",
                "dependencies",
                "dependency_evidence",
                "executable",
            )
        }

    @staticmethod
    def _work_identity_and_outcome(work: dict) -> dict:
        return {
            field: work[field]
            for field in (
                "key",
                "classification",
                "dependencies",
                "dependency_evidence",
                "requirement_keys",
                "acceptance_criteria",
                "evidence_requirements",
                "state",
            )
        }

    @staticmethod
    def _work_failure_evidence(work: dict) -> dict:
        return {
            field: work.get(field)
            for field in (
                "key",
                "title",
                "description",
                "classification",
                "dependencies",
                "dependency_evidence",
                "requirement_keys",
                "acceptance_criteria",
                "evidence_requirements",
                "state",
                "result",
                "handoff",
            )
        }

    @staticmethod
    def _validation_evidence(validation: dict) -> dict:
        result = validation.get("result")
        if not isinstance(result, dict):
            result = {}
        return {
            "pass_id": validation["pass_id"],
            "result": {
                field: result[field]
                for field in (
                    "passed",
                    "failed_specifications",
                    "failed_criteria",
                    "code_review_findings",
                    "requirement_results",
                    "criterion_results",
                    "integration_findings",
                    "explanation",
                    "evidence",
                )
                if field in result
            },
        }

    @classmethod
    def _related_prior_validation_failures(
        cls,
        validations: list[dict],
    ) -> list[dict]:
        if not validations:
            return []
        latest_result = validations[-1].get("result")
        if not isinstance(latest_result, dict):
            return []
        latest_criteria = {
            criterion
            for criterion in latest_result.get("failed_criteria", [])
            if isinstance(criterion, str) and criterion
        }
        if not latest_criteria:
            return []
        related = []
        for validation in validations[:-1]:
            result = validation.get("result")
            if not isinstance(result, dict) or result.get("passed") is not False:
                continue
            failed_criteria = result.get("failed_criteria", [])
            if not isinstance(failed_criteria, list):
                continue
            if latest_criteria.intersection(
                criterion
                for criterion in failed_criteria
                if isinstance(criterion, str)
            ):
                related.append(cls._validation_evidence(validation))
        return related

    @classmethod
    def _specification_dependency_closure(
        cls,
        specifications: list[dict],
        specification: dict,
    ) -> list[dict]:
        specifications_by_key = {
            item["key"]: item for item in specifications
        }
        dependency_keys: set[str] = set()
        pending = list(specification["dependencies"])
        while pending:
            dependency_key = pending.pop()
            if dependency_key in dependency_keys:
                continue
            dependency_keys.add(dependency_key)
            dependency = specifications_by_key.get(dependency_key)
            if dependency is not None:
                pending.extend(dependency["dependencies"])
        return [
            cls._specification_definition(item)
            for item in specifications
            if item["key"] in dependency_keys
        ]

    def _specify_context(
        self,
        repository: dict,
        run: dict,
        execution_pass: dict,
        *,
        in_scope_only: bool = True,
    ) -> dict:
        validations = self.store.list_validations(run["id"])
        feedback, pull_request_diff = self._pass_feedback_context(
            run["id"],
            execution_pass,
            in_scope_only=in_scope_only,
        )
        reference = run.get("pull_request")
        pull_request_head_sha = (
            reference.get("head_sha")
            if isinstance(reference, dict)
            else None
        )
        context = {
            "original_issue": run["issue_json"],
            "repository": repository,
            "feedback": feedback,
            "pull_request_diff": pull_request_diff,
            "pull_request_head_sha": pull_request_head_sha,
            "existing_specifications": [
                self._specification_definition(item)
                for item in self.store.list_specifications(run["id"])
            ],
            "existing_work": [
                self._work_identity_and_outcome(item)
                for item in self.store.list_work_items(run["id"])
            ],
            "relevant_validation": (
                self._validation_evidence(validations[-1])
                if validations
                else None
            ),
        }
        if execution_pass["trigger_type"] == "operation_failure":
            context["operation_failure"] = execution_pass["trigger_json"]
        if execution_pass["trigger_type"] == "work_failure":
            context["work_failure"] = execution_pass["trigger_json"]
        if execution_pass["trigger_type"] == "validation_failure":
            related = self._related_prior_validation_failures(validations)
            if related:
                context["related_prior_validation_failures"] = related
        return context

    @staticmethod
    def _specify_instruction() -> str:
        return (
            "Convert only the issue, controller operation failure, validation "
            "deficiency, worker failure, or feedback in context into atomic specifications, "
            "acceptance criteria, classified work items, and evidence-backed dependencies. "
            "Give every dependency exactly one reason and a nonempty evidence list; "
            "use empty dependency_evidence when there are no dependencies. "
            "When related prior validation failures are present, use their recurring "
            "evidence to identify the unresolved invariant or strategy class. Do not "
            "merely enumerate the latest example when the evidence shows that bounded "
            "variants leave the same criterion unsatisfied. "
            + _CLASSIFICATION_GUIDANCE
        )

    @staticmethod
    def _nonempty_unique_strings(value: object, name: str) -> list[str]:
        if (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(value) != len(set(value))
        ):
            raise ValueError(f"{name} must be unique nonempty strings")
        return list(value)

    @staticmethod
    def _require_acyclic_keys(graph: dict[str, list[str]], name: str) -> None:
        visited: set[str] = set()
        active: set[str] = set()

        def visit(key: str) -> None:
            if key in active:
                raise ValueError(f"{name} must be acyclic")
            if key in visited:
                return
            active.add(key)
            for dependency in graph[key]:
                visit(dependency)
            active.remove(key)
            visited.add(key)

        for key in graph:
            visit(key)

    @classmethod
    def _validated_issue_specification(cls, result: object) -> dict:
        if not isinstance(result, dict) or set(result) != {
            "requirements",
            "work_areas",
        }:
            raise ValueError(
                "Issue Specifier must return requirements and work_areas"
            )
        requirements = result["requirements"]
        work_areas = result["work_areas"]
        if not isinstance(requirements, list) or not requirements:
            raise ValueError("Issue Specifier requirements must be nonempty")
        if not isinstance(work_areas, list) or not work_areas:
            raise ValueError("Issue Specifier work_areas must be nonempty")

        normalized_requirements = []
        requirement_keys: set[str] = set()
        for requirement in requirements:
            if not isinstance(requirement, dict) or set(requirement) != {
                "key",
                "statement",
                "evidence",
            }:
                raise ValueError("issue requirement is incomplete")
            key = requirement["key"]
            statement = requirement["statement"]
            evidence = requirement["evidence"]
            if not isinstance(key, str) or not key.strip() or key in requirement_keys:
                raise ValueError("issue requirement keys must be unique and nonempty")
            if not isinstance(statement, str) or not statement.strip():
                raise ValueError("issue requirement statement must be nonempty")
            evidence = cls._nonempty_unique_strings(
                evidence, "issue requirement evidence"
            )
            requirement_keys.add(key)
            normalized_requirements.append(
                {"key": key, "statement": statement, "evidence": evidence}
            )

        normalized_areas = []
        area_keys: set[str] = set()
        covered_requirements: set[str] = set()
        for area in work_areas:
            required = {
                "key",
                "title",
                "description",
                "requirement_keys",
                "dependencies",
                "dependency_evidence",
            }
            if not isinstance(area, dict) or set(area) != required:
                raise ValueError("strategic work area is incomplete")
            key = area["key"]
            if not isinstance(key, str) or not key.strip() or key in area_keys:
                raise ValueError("work area keys must be unique and nonempty")
            for field in ("title", "description"):
                if not isinstance(area[field], str) or not area[field].strip():
                    raise ValueError(f"work area {field} must be nonempty")
            mapped = cls._nonempty_unique_strings(
                area["requirement_keys"], "work area requirement_keys"
            )
            if any(item not in requirement_keys for item in mapped):
                raise ValueError("work area references an unknown requirement")
            dependencies, dependency_evidence = cls._validated_dependency_contract(
                area["dependencies"],
                area["dependency_evidence"],
                "work area",
            )
            area_keys.add(key)
            covered_requirements.update(mapped)
            normalized_areas.append(
                {
                    "key": key,
                    "title": area["title"],
                    "description": area["description"],
                    "requirement_keys": mapped,
                    "dependencies": dependencies,
                    "dependency_evidence": dependency_evidence,
                }
            )
        if covered_requirements != requirement_keys:
            raise ValueError("every issue requirement must map to a work area")
        if any(
            dependency not in area_keys
            for area in normalized_areas
            for dependency in area["dependencies"]
        ):
            raise ValueError("work area dependency references an unknown work area")
        cls._require_acyclic_keys(
            {area["key"]: area["dependencies"] for area in normalized_areas},
            "work area dependency graph",
        )
        return {
            "requirements": normalized_requirements,
            "work_areas": normalized_areas,
        }

    @classmethod
    def _validated_work_specification(
        cls,
        result: object,
        issue_specification: dict,
        work_area: dict,
    ) -> dict:
        if not isinstance(result, dict) or set(result) != {
            "work_area_key",
            "specification",
        }:
            raise ValueError(
                "Work Specifier must return work_area_key and specification"
            )
        if result["work_area_key"] != work_area["key"]:
            raise ValueError("Work Specifier returned a different work area")
        specification = result["specification"]
        required_specification_fields = {
            "key",
            "title",
            "description",
            "requirement_keys",
            "acceptance_criteria",
            "work_items",
        }
        if (
            not isinstance(specification, dict)
            or set(specification) != required_specification_fields
            or specification["key"] != work_area["key"]
        ):
            raise ValueError("focused specification is incomplete or misidentified")
        for field in ("title", "description"):
            if (
                not isinstance(specification[field], str)
                or not specification[field].strip()
            ):
                raise ValueError(f"focused specification {field} must be nonempty")
        requirement_keys = cls._nonempty_unique_strings(
            specification["requirement_keys"],
            "focused specification requirement_keys",
        )
        if set(requirement_keys) != set(work_area["requirement_keys"]):
            raise ValueError(
                "focused specification must retain every work area requirement"
            )

        criteria = specification["acceptance_criteria"]
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("focused specification requires acceptance criteria")
        normalized_criteria = []
        criterion_keys: set[str] = set()
        criterion_requirement_coverage: set[str] = set()
        for criterion in criteria:
            if not isinstance(criterion, dict) or set(criterion) != {
                "key",
                "description",
                "requirement_keys",
            }:
                raise ValueError("focused acceptance criterion is incomplete")
            key = criterion["key"]
            description = criterion["description"]
            mapped = cls._nonempty_unique_strings(
                criterion["requirement_keys"], "criterion requirement_keys"
            )
            if (
                not isinstance(key, str)
                or not key.strip()
                or key in criterion_keys
                or not isinstance(description, str)
                or not description.strip()
            ):
                raise ValueError("criterion key and description must be valid")
            if any(item not in requirement_keys for item in mapped):
                raise ValueError("criterion references an unknown requirement")
            criterion_keys.add(key)
            criterion_requirement_coverage.update(mapped)
            normalized_criteria.append(
                {"key": key, "description": description, "requirement_keys": mapped}
            )
        if criterion_requirement_coverage != set(requirement_keys):
            raise ValueError("criteria must cover every focused requirement")

        work_items = specification["work_items"]
        if not isinstance(work_items, list) or not work_items:
            raise ValueError("focused specification requires work items")
        normalized_work = []
        work_keys: set[str] = set()
        covered_criteria: set[str] = set()
        covered_requirements: set[str] = set()
        for work in work_items:
            required_work_fields = {
                "key",
                "title",
                "description",
                "classification",
                "requirement_keys",
                "acceptance_criteria",
                "evidence_requirements",
                "dependencies",
                "dependency_evidence",
            }
            if not isinstance(work, dict) or set(work) != required_work_fields:
                raise ValueError("focused work item is incomplete")
            key = work["key"]
            if not isinstance(key, str) or not key.strip() or key in work_keys:
                raise ValueError("focused work keys must be unique and nonempty")
            for field in ("title", "description"):
                if not isinstance(work[field], str) or not work[field].strip():
                    raise ValueError(f"focused work {field} must be nonempty")
            mapped_requirements = cls._nonempty_unique_strings(
                work["requirement_keys"], "focused work requirement_keys"
            )
            mapped_criteria = cls._nonempty_unique_strings(
                work["acceptance_criteria"], "focused work acceptance_criteria"
            )
            evidence_requirements = cls._nonempty_unique_strings(
                work["evidence_requirements"], "focused work evidence_requirements"
            )
            if any(item not in requirement_keys for item in mapped_requirements):
                raise ValueError("work item references an unknown requirement")
            if any(item not in criterion_keys for item in mapped_criteria):
                raise ValueError("work item references an unknown criterion")
            dependencies, dependency_evidence = cls._validated_dependency_contract(
                work["dependencies"], work["dependency_evidence"], "focused work"
            )
            work_keys.add(key)
            covered_requirements.update(mapped_requirements)
            covered_criteria.update(mapped_criteria)
            normalized_work.append(
                {
                    "key": key,
                    "title": work["title"],
                    "description": work["description"],
                    "classification": validate_classification(work["classification"]),
                    "requirement_keys": mapped_requirements,
                    "acceptance_criteria": mapped_criteria,
                    "evidence_requirements": evidence_requirements,
                    "dependencies": dependencies,
                    "dependency_evidence": dependency_evidence,
                }
            )
        if covered_requirements != set(requirement_keys):
            raise ValueError("work items must cover every focused requirement")
        if covered_criteria != criterion_keys:
            raise ValueError("work items must cover every focused criterion")
        if any(
            dependency not in work_keys
            for work in normalized_work
            for dependency in work["dependencies"]
        ):
            raise ValueError("focused work dependency references another work area")
        cls._require_acyclic_keys(
            {work["key"]: work["dependencies"] for work in normalized_work},
            "focused work dependency graph",
        )
        normalized_result = {
            "work_area_key": work_area["key"],
            "specification": {
                "key": work_area["key"],
                "title": specification["title"],
                "description": specification["description"],
                "requirement_keys": requirement_keys,
                "acceptance_criteria": [
                    criterion["description"] for criterion in normalized_criteria
                ],
                "acceptance_traceability": normalized_criteria,
                "dependencies": work_area["dependencies"],
                "dependency_evidence": work_area["dependency_evidence"],
                "executable": True,
                "work_items": normalized_work,
            },
        }
        return normalized_result

    @classmethod
    def _validated_persisted_work_specification(
        cls,
        result: object,
        issue_specification: dict,
        work_area: dict,
    ) -> dict:
        if not isinstance(result, dict) or set(result) != {
            "work_area_key",
            "specification",
        }:
            raise ValueError("persisted focused specification is incomplete")
        specification = result["specification"]
        normalized_fields = {
            "key",
            "title",
            "description",
            "requirement_keys",
            "acceptance_criteria",
            "acceptance_traceability",
            "dependencies",
            "dependency_evidence",
            "executable",
            "work_items",
        }
        if not isinstance(specification, dict) or set(specification) != normalized_fields:
            raise ValueError("persisted focused specification has an invalid shape")
        raw_result = {
            "work_area_key": result["work_area_key"],
            "specification": {
                "key": specification["key"],
                "title": specification["title"],
                "description": specification["description"],
                "requirement_keys": specification["requirement_keys"],
                "acceptance_criteria": specification["acceptance_traceability"],
                "work_items": specification["work_items"],
            },
        }
        normalized = cls._validated_work_specification(
            raw_result,
            issue_specification,
            work_area,
        )
        if normalized != result:
            raise ValueError("persisted focused specification is inconsistent")
        return normalized

    def _run_focused_specification(
        self,
        repository: dict,
        run: dict,
        execution_pass: dict,
        source_workspace: Path,
        *,
        feedback_dispositions: dict | None = None,
    ) -> bool:
        issue_specification = self.store.get_issue_specification(
            run["id"], execution_pass["id"]
        )
        if issue_specification is None:
            context = self._specify_context(repository, run, execution_pass)
            if feedback_dispositions is not None:
                context["feedback_dispositions"] = feedback_dispositions[
                    "dispositions"
                ]
                context["feedback_proposed_specifications"] = feedback_dispositions[
                    "specifications"
                ]
            issue_specification = self.runtime.run(
                self._task(
                    "issue_specify",
                    "Interpret the complete current issue objective and this pass's "
                    "durable evidence. Produce explicit, independently identifiable "
                    "requirements covering every required outcome, constraint, and "
                    "required method. Preserve explicit requirements to use an "
                    "external system, source, tool, or environment as methods that "
                    "must themselves be evidenced, rather than treating a plausible "
                    "artifact as proof that the method occurred. Cite concrete "
                    "evidence for every requirement. "
                    "Organize all requirements into strategic work areas that describe "
                    "what must be achieved without prescribing implementation steps. "
                    "Include only causally necessary dependencies, each with a reason "
                    "and concrete evidence. Do not omit requirements merely because "
                    "existing work appears to address them.",
                    context,
                ),
                source_workspace,
                result_schema=_ISSUE_SPECIFY_SCHEMA,
                trajectory_path=self._trajectory(
                    run["id"], f"issue-specify-{execution_pass['id']}"
                ),
            )
            issue_specification = self._validated_issue_specification(
                issue_specification
            )
            issue_specification = self.store.record_issue_specification(
                run["id"], execution_pass["id"], issue_specification
            )
        else:
            issue_specification = self._validated_issue_specification(
                issue_specification
            )

        persisted = {
            item["work_area_key"]: item["result"]
            for item in self.store.list_work_specification_results(
                run["id"], execution_pass["id"]
            )
        }
        package_rejection_path = self._trajectory(
            run["id"],
            f"work-specify-{execution_pass['id']}-package-rejections",
        )
        prior_package_rejections = self._stage_rejections(
            package_rejection_path
        )
        specifications = []
        for work_area in issue_specification["work_areas"]:
            focused = persisted.get(work_area["key"])
            if focused is None:
                rejection_path = self._trajectory(
                    run["id"],
                    f"work-specify-{execution_pass['id']}-{work_area['key']}-rejections",
                )
                prior_rejections = self._stage_rejections(rejection_path)
                work_context = {
                    "original_issue": run["issue_json"],
                    "repository": repository,
                    "issue_specification": issue_specification,
                    "work_area": work_area,
                    "pass_evidence": self._specify_context(
                        repository, run, execution_pass
                    ),
                }
                if prior_rejections:
                    work_context["prior_specification_rejections"] = prior_rejections
                if prior_package_rejections:
                    work_context["prior_package_rejections"] = (
                        prior_package_rejections
                    )
                focused = self.runtime.run(
                    self._task(
                        "work_specify",
                        "Specify only the supplied strategic work area. Derive "
                        "observable acceptance criteria and focused work items that "
                        "retain traceability to every applicable issue requirement. "
                        "State the evidence each work item must produce. For every "
                        "mandated method or external interaction, require direct "
                        "execution evidence that can distinguish actually performing "
                        "it from producing the same artifact without it. Assign an "
                        "agent classification using repository evidence. Add only "
                        "causally necessary within-area dependencies, each supported "
                        "by a reason and concrete evidence. Define required outcomes "
                        "without prescribing a fixed procedure. When related prior "
                        "validation failures are present, identify the unresolved "
                        "invariant or strategy class demonstrated by their recurring "
                        "evidence instead of enumerating only the latest example. "
                        "Correct every supplied prior specification rejection without "
                        "dropping any requirement from the current work area. "
                        "Keep work keys independently identifiable and unique across "
                        "the complete issue specification. Correct every supplied "
                        "package rejection while changing only this work area's "
                        "specification. "
                        + _CLASSIFICATION_GUIDANCE,
                        work_context,
                    ),
                    source_workspace,
                    result_schema=_WORK_SPECIFY_SCHEMA,
                    trajectory_path=self._trajectory(
                        run["id"],
                        f"work-specify-{execution_pass['id']}-{work_area['key']}"
                        "-attempt-"
                        f"{len(prior_rejections) + len(prior_package_rejections) + 1}",
                    ),
                )
                try:
                    focused = self._validated_work_specification(
                        focused, issue_specification, work_area
                    )
                except (TypeError, ValueError) as error:
                    self._record_stage_rejection(rejection_path, focused, error)
                    return False
                focused = self.store.record_work_specification_result(
                    run["id"],
                    execution_pass["id"],
                    work_area["key"],
                    focused,
                )
            else:
                focused = self._validated_persisted_work_specification(
                    focused, issue_specification, work_area
                )
            specifications.append(focused["specification"])
        package = {"specifications": specifications}
        try:
            self.store.save_specification_package(
                run["id"],
                execution_pass["id"],
                package,
            )
        except (TypeError, ValueError) as error:
            self._record_stage_rejection(
                package_rejection_path,
                package,
                error,
            )
            self.store.clear_work_specification_results(
                run["id"], execution_pass["id"]
            )
            return False
        return True

    @staticmethod
    def _stage_rejections(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid persisted stage rejections: {path}") from error
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise RuntimeError(f"invalid persisted stage rejections: {path}")
        return value

    @classmethod
    def _record_stage_rejection(
        cls,
        path: Path,
        rejected_result: object,
        error: Exception,
    ) -> None:
        rejections = cls._stage_rejections(path)
        rejections.append(
            {
                "attempt": len(rejections) + 1,
                "error_type": type(error).__name__,
                "error": str(error),
                "rejected_result": rejected_result,
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(path.suffix + ".pending")
        with staging.open("w", encoding="utf-8") as output:
            json.dump(rejections, output, indent=2, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, path)

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
        if execution_pass["trigger_type"] == "feedback":
            packages = execution_pass["trigger_json"].get("feedback", [])
            result = self.store.get_feedback_scope_result(
                run["id"],
                execution_pass["id"],
            )
            reference = run.get("pull_request")
            head_sha = (
                reference.get("head_sha")
                if isinstance(reference, dict)
                else None
            )
            if not isinstance(head_sha, str) or not head_sha:
                raise RuntimeError(
                    "feedback Specify run has no current pull-request head"
                )
            if result is None:
                with self._source_snapshot(
                    self._workspace(repository["id"], run["id"])
                ) as source_workspace:
                    result = self.runtime.run(
                        self._task(
                            "specify",
                            "Disposition every claimed feedback item against the current pull-request head, original issue, accepted specifications, prior work, and validation evidence before creating correction work. Invalid feedback must set valid=false, in_scope=false, pr_regression=false, specification_keys=[], and follow_up_issue=null. A pull-request regression must be valid and in scope. Map only valid in-scope items to returned specifications. Give valid out-of-scope items a bounded follow-up issue and give invalid items no follow-up issue. "
                            + _CLASSIFICATION_GUIDANCE,
                            self._specify_context(
                                repository,
                                run,
                                execution_pass,
                                in_scope_only=False,
                            ),
                        ),
                        source_workspace,
                        result_schema=_FEEDBACK_SPECIFY_SCHEMA,
                        trajectory_path=self._trajectory(
                            run["id"], f"specify-{execution_pass['id']}"
                        ),
                    )
                result = self._validated_feedback_scope_result(
                    result,
                    packages,
                )
                result = dict(result)
                result["head_sha"] = head_sha
                result = self.store.record_feedback_scope_result(
                    run["id"],
                    execution_pass["id"],
                    result,
                )
            else:
                result = self._validated_feedback_scope_result(
                    result,
                    packages,
                )
                if result.get("head_sha") != head_sha:
                    raise RuntimeError(
                        "feedback scope result belongs to a different "
                        "pull-request head"
                    )
            for disposition in result["dispositions"]:
                self.store.record_feedback_disposition(
                    run["id"],
                    disposition["external_id"],
                    self._feedback_disposition_name(disposition),
                    disposition,
                )
            self._resolve_no_code_feedback(
                repository,
                run,
                result["dispositions"],
            )
            if not result["specifications"]:
                reference = run.get("pull_request")
                if not reference:
                    raise RuntimeError(
                        "feedback disposition run has no pull request"
                    )
                self.store.transition_run(
                    run["id"],
                    "PR_LISTENING",
                    branch=reference["branch"],
                    pull_request=reference,
                    pr_listening_since=self._clock(),
                )
                return
            if not existing:
                with self._source_snapshot(
                    self._workspace(repository["id"], run["id"])
                ) as source_workspace:
                    if not self._run_focused_specification(
                        repository,
                        run,
                        execution_pass,
                        source_workspace,
                        feedback_dispositions=result,
                    ):
                        return
        elif not existing:
            with self._source_snapshot(
                self._workspace(repository["id"], run["id"])
            ) as source_workspace:
                if not self._run_focused_specification(
                    repository,
                    run,
                    execution_pass,
                    source_workspace,
                ):
                    return
        self._route_unassigned(repository, run, execution_pass)
        self.store.transition_run(run["id"], "EXECUTING")
        self.store.transition_run(run["id"], "WAITING_FOR_WORK_COMPLETION")

    @staticmethod
    def _validated_feedback_scope_result(
        result: dict,
        packages: list[dict],
    ) -> dict:
        if (
            not isinstance(result, dict)
            or not {"dispositions", "specifications"}.issubset(result)
        ):
            raise ValueError(
                "feedback Specify must return dispositions and specifications"
            )
        dispositions = result["dispositions"]
        specifications = result["specifications"]
        if not isinstance(dispositions, list):
            raise ValueError("feedback dispositions must be a list")
        if not isinstance(specifications, list):
            raise ValueError("feedback specifications must be a list")
        if not isinstance(packages, list) or any(
            not isinstance(package, dict)
            or not isinstance(package.get("external_id"), str)
            or not package["external_id"].strip()
            for package in packages
        ):
            raise ValueError("feedback pass must contain claimed feedback items")
        claimed_ids = [package["external_id"] for package in packages]
        if len(claimed_ids) != len(set(claimed_ids)):
            raise ValueError("feedback pass external IDs must be unique")

        required_disposition_fields = {
            "external_id",
            "valid",
            "in_scope",
            "pr_regression",
            "explanation",
            "evidence",
            "specification_keys",
            "follow_up_issue",
        }
        seen_ids: set[str] = set()
        mapped_specification_keys: set[str] = set()
        for disposition in dispositions:
            if (
                not isinstance(disposition, dict)
                or not required_disposition_fields.issubset(disposition)
            ):
                raise ValueError(
                    "feedback disposition must contain every required field"
                )
            external_id = disposition["external_id"]
            if (
                not isinstance(external_id, str)
                or not external_id.strip()
                or external_id in seen_ids
            ):
                raise ValueError(
                    "feedback disposition external IDs must be nonempty and unique"
                )
            seen_ids.add(external_id)
            for field in ("valid", "in_scope", "pr_regression"):
                if not isinstance(disposition[field], bool):
                    raise ValueError(
                        f"feedback disposition {field} must be boolean"
                    )
            explanation = disposition["explanation"]
            if not isinstance(explanation, str) or not explanation.strip():
                raise ValueError(
                    "feedback disposition explanation must be nonempty"
                )
            evidence = disposition["evidence"]
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in evidence
                )
            ):
                raise ValueError(
                    "feedback disposition evidence must be nonempty strings"
                )
            specification_keys = disposition["specification_keys"]
            if (
                not isinstance(specification_keys, list)
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in specification_keys
                )
                or len(specification_keys) != len(set(specification_keys))
            ):
                raise ValueError(
                    "feedback disposition specification_keys must be unique strings"
                )
            if disposition["pr_regression"] and not (
                disposition["valid"] and disposition["in_scope"]
            ):
                raise ValueError(
                    "a pull-request regression must be valid and in scope"
                )
            if disposition["in_scope"] and not disposition["valid"]:
                raise ValueError("in-scope feedback must be valid")

            follow_up = disposition["follow_up_issue"]
            if disposition["valid"] and disposition["in_scope"]:
                if not specification_keys:
                    raise ValueError(
                        "in-scope feedback must map to a specification"
                    )
                if follow_up is not None:
                    raise ValueError(
                        "in-scope feedback cannot create a follow-up issue"
                    )
                mapped_specification_keys.update(specification_keys)
            elif disposition["valid"]:
                if specification_keys:
                    raise ValueError(
                        "out-of-scope feedback cannot map to specifications"
                    )
                if not isinstance(follow_up, dict):
                    raise ValueError(
                        "out-of-scope feedback requires a follow-up issue"
                    )
                required_follow_up_fields = {
                    "title",
                    "observed_defect",
                    "affected_behavior",
                    "affected_paths",
                    "acceptance_criteria",
                }
                if not required_follow_up_fields.issubset(follow_up):
                    raise ValueError(
                        "follow-up issue must contain every required field"
                    )
                for field in (
                    "title",
                    "observed_defect",
                    "affected_behavior",
                ):
                    if (
                        not isinstance(follow_up[field], str)
                        or not follow_up[field].strip()
                    ):
                        raise ValueError(
                            f"follow-up issue {field} must be nonempty"
                        )
                for field in ("affected_paths", "acceptance_criteria"):
                    values = follow_up[field]
                    if (
                        not isinstance(values, list)
                        or any(
                            not isinstance(item, str) or not item.strip()
                            for item in values
                        )
                    ):
                        raise ValueError(
                            f"follow-up issue {field} must be strings"
                        )
                if not follow_up["acceptance_criteria"]:
                    raise ValueError(
                        "follow-up issue acceptance_criteria must be nonempty"
                    )
            else:
                if disposition["in_scope"]:
                    raise ValueError("invalid feedback cannot be in scope")
                if specification_keys:
                    raise ValueError(
                        "invalid feedback cannot map to specifications"
                    )
                if follow_up is not None:
                    raise ValueError(
                        "invalid feedback cannot create a follow-up issue"
                    )

        if seen_ids != set(claimed_ids):
            raise ValueError(
                "feedback Specify must disposition every claimed item exactly once"
            )
        returned_specification_keys: set[str] = set()
        for specification in specifications:
            if (
                not isinstance(specification, dict)
                or not isinstance(specification.get("key"), str)
                or not specification["key"].strip()
                or specification["key"] in returned_specification_keys
            ):
                raise ValueError(
                    "feedback specification keys must be nonempty and unique"
                )
            returned_specification_keys.add(specification["key"])
        if mapped_specification_keys != returned_specification_keys:
            raise ValueError(
                "only in-scope feedback may map to returned specifications"
            )
        return dict(result)

    @staticmethod
    def _feedback_disposition_name(disposition: dict) -> str:
        if not disposition["valid"]:
            return "INVALID"
        if disposition["in_scope"]:
            return "IN_SCOPE"
        return "OUT_OF_SCOPE"

    @staticmethod
    def _github_feedback(feedback_row: dict) -> GitHubFeedback:
        package = feedback_row["package"]
        return GitHubFeedback(
            external_id=feedback_row["external_id"],
            kind=package["kind"],
            body=package["body"],
            path=package.get("path"),
            line=package.get("line"),
            review_thread_id=package.get("review_thread_id"),
            top_level_comment_id=package.get("top_level_comment_id"),
        )

    @staticmethod
    def _feedback_source_url(pull_url: str, feedback_row: dict) -> str:
        package = feedback_row["package"]
        comment_id = package.get("top_level_comment_id")
        if package.get("kind") == "inline" and isinstance(comment_id, int):
            return f"{pull_url}#discussion_r{comment_id}"
        external_id = feedback_row["external_id"]
        if package.get("kind") == "review" and external_id.startswith("review:"):
            return (
                f"{pull_url}#pullrequestreview-"
                f"{external_id.partition(':')[2]}"
            )
        if package.get("kind") == "comment" and external_id.startswith("comment:"):
            return f"{pull_url}#issuecomment-{external_id.partition(':')[2]}"
        return f"{pull_url}#feedback-{external_id}"

    @classmethod
    def _follow_up_body(
        cls,
        pull_url: str,
        feedback_row: dict,
        disposition: dict,
    ) -> str:
        follow_up = disposition["follow_up_issue"]
        paths = "\n".join(
            f"- `{path}`" for path in follow_up["affected_paths"]
        ) or "- No repository path was identified."
        evidence = "\n".join(
            f"- {item}" for item in disposition["evidence"]
        )
        criteria = "\n".join(
            f"- {item}" for item in follow_up["acceptance_criteria"]
        )
        feedback_url = cls._feedback_source_url(pull_url, feedback_row)
        return (
            "## Observed defect\n\n"
            f"{follow_up['observed_defect']}\n\n"
            "## Affected behavior\n\n"
            f"{follow_up['affected_behavior']}\n\n"
            "## Affected paths\n\n"
            f"{paths}\n\n"
            "## Supporting evidence\n\n"
            f"{evidence}\n\n"
            "## Acceptance criteria\n\n"
            f"{criteria}\n\n"
            "## Why this is outside the current issue\n\n"
            f"{disposition['explanation']}\n\n"
            "## Source\n\n"
            f"- Pull request: {pull_url}\n"
            f"- Feedback: {feedback_url}"
        )

    @staticmethod
    def _no_code_response(
        disposition: dict,
        follow_up_issue: dict | None,
    ) -> str:
        evidence = "\n".join(
            f"- {item}" for item in disposition["evidence"]
        )
        if follow_up_issue is not None:
            return (
                "This is valid feedback, but it is outside the current "
                "issue's scope.\n\n"
                f"Evidence:\n{evidence}\n\n"
                f"Scope reason: {disposition['explanation']}\n\n"
                f"Follow-up issue: {follow_up_issue['url']}"
            )
        return (
            "No current-branch change is needed because this feedback is "
            "invalid or no longer present.\n\n"
            f"Evidence:\n{evidence}\n\n"
            f"Conclusion: {disposition['explanation']}"
        )

    def _resolve_no_code_feedback(
        self,
        repository: dict,
        run: dict,
        dispositions: list[dict],
    ) -> None:
        reference = run.get("pull_request")
        if not reference:
            raise RuntimeError("feedback disposition run has no pull request")
        rows = {
            item["external_id"]: item
            for item in self.store.list_feedback(run["id"])
        }
        for disposition in dispositions:
            disposition_name = self._feedback_disposition_name(disposition)
            if disposition_name == "IN_SCOPE":
                continue
            external_id = disposition["external_id"]
            feedback_row = rows[external_id]
            if feedback_row["status"] != "PENDING":
                continue
            follow_up_issue = feedback_row.get("follow_up_issue")
            if disposition_name == "OUT_OF_SCOPE" and follow_up_issue is None:
                requested = disposition["follow_up_issue"]
                issue = self.github.ensure_follow_up_issue(
                    repository["github_repository"],
                    external_id,
                    requested["title"],
                    self._follow_up_body(
                        reference["url"],
                        feedback_row,
                        disposition,
                    ),
                )
                feedback_row = self.store.record_feedback_follow_up(
                    run["id"],
                    external_id,
                    asdict(issue),
                )
                follow_up_issue = feedback_row["follow_up_issue"]
            feedback = self._github_feedback(feedback_row)
            address = self.github.resolve_feedback_without_code(
                repository["github_repository"],
                int(reference["number"]),
                feedback,
                self._no_code_response(disposition, follow_up_issue),
            )
            self.store.mark_feedback_without_code(
                run["id"],
                external_id,
                address.status,
                address.response_url,
            )

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
                with self._source_snapshot(
                    self._workspace(repository["id"], run["id"])
                ) as source_workspace:
                    role_result = self.runtime.run(
                        self._task(
                            "node_role",
                            "Generate a flexible role prompt for this repository-reusable agent queue. Describe responsibilities broad enough to serve this classification across repository issues without prescribing a fixed implementation workflow. Use the current work only as context; do not narrow the role to this issue or work item.",
                            {
                                "classification": classification,
                                "work_item": work,
                                "repository": repository,
                            },
                        ),
                        source_workspace,
                        result_schema=_ROLE_SCHEMA,
                        trajectory_path=self._trajectory(
                            run["id"], f"role-{work['id']}"
                        ),
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
        work_items = self.store.list_work_items(run["id"], execution_pass["id"])
        if any(item["state"] == "FAILED" for item in work_items):
            settled = self.store.settle_failed_pass_work(
                run["id"],
                execution_pass["id"],
                {
                    "output": {
                        "error": "Work could not run because one of its dependencies failed",
                        "type": "BlockedByWorkFailure",
                    },
                    "artifacts": [],
                    "test_results": [],
                    "repository_state": {},
                },
            )
            if not settled:
                self._route_unassigned(repository, run, execution_pass)
                return
            settled_work = self.store.list_work_items(
                run["id"], execution_pass["id"]
            )
            trigger = {
                "failed_pass_id": execution_pass["id"],
                "failed_work": [
                    self._work_failure_evidence(item)
                    for item in settled_work
                    if item["state"] == "FAILED"
                ],
                "work_graph": [
                    self._work_failure_evidence(item)
                    for item in settled_work
                ],
            }
            origin_feedback_pass_id = self._feedback_origin_pass_id(execution_pass)
            if origin_feedback_pass_id is not None:
                trigger["origin_feedback_pass_id"] = origin_feedback_pass_id
            self.store.create_pass_and_transition(
                run["id"],
                execution_pass["id"],
                "work_failure",
                trigger,
                "SPECIFYING",
            )
            return
        self._route_unassigned(repository, run, execution_pass)
        if self.store.validation_barrier_ready(run["id"], execution_pass["id"]):
            self.store.transition_run(run["id"], "VALIDATING")

    def _start_workers(self, focused_run_ids: set[int]) -> None:
        for run_id in sorted(focused_run_ids):
            run = self.store.get_run(run_id)
            if run is None or run["state"] not in _SOURCE_ACTIVE_RUN_STATES:
                continue
            repository = self.store.get_repository(run["repository_id"])
            if repository is None:
                continue
            for node in self.store.list_dynamic_nodes(repository["id"]):
                with self._worker_lock:
                    if node["id"] in self._workers:
                        continue
                    work = self.store.claim_node_work(node["id"], run_id)
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

    @classmethod
    def _validated_dependency_contract(
        cls,
        dependencies: object,
        dependency_evidence: object,
        name: str,
    ) -> tuple[list[str], list[dict]]:
        if (
            not isinstance(dependencies, list)
            or any(
                not isinstance(dependency, str) or not dependency.strip()
                for dependency in dependencies
            )
            or len(set(dependencies)) != len(dependencies)
        ):
            raise ValueError(f"{name} dependencies must be unique nonempty strings")
        if not isinstance(dependency_evidence, list):
            raise ValueError(f"{name} dependency_evidence must be a list")
        normalized: list[dict] = []
        evidence_dependencies: list[str] = []
        for item in dependency_evidence:
            if not isinstance(item, dict) or not {
                "dependency",
                "reason",
                "evidence",
            }.issubset(item):
                raise ValueError(f"{name} dependency evidence is incomplete")
            dependency = item["dependency"]
            reason = item["reason"]
            evidence = item["evidence"]
            if not isinstance(dependency, str) or not dependency.strip():
                raise ValueError(f"{name} dependency evidence key is invalid")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"{name} dependency evidence reason is invalid")
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(
                    not isinstance(observation, str) or not observation.strip()
                    for observation in evidence
                )
            ):
                raise ValueError(f"{name} dependency evidence must be nonempty")
            evidence_dependencies.append(dependency)
            normalized.append(
                {
                    "dependency": dependency,
                    "reason": reason,
                    "evidence": list(evidence),
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
            item["dependency"]: item for item in normalized
        }
        return list(dependencies), [
            evidence_by_dependency[dependency] for dependency in dependencies
        ]

    @classmethod
    def _validated_work_result(
        cls,
        result: object,
        execution_pass: dict,
        work_keys: set[str],
    ) -> dict:
        required = {
            "outcome",
            "output",
            "artifacts",
            "test_results",
            "repository_state",
        }
        if not isinstance(result, dict) or not required.issubset(result):
            raise ValueError("work must return the complete work result")
        try:
            json.dumps(result)
        except (TypeError, ValueError) as error:
            raise ValueError("work result must be JSON-safe") from error
        if not isinstance(result["artifacts"], list):
            raise ValueError("work artifacts must be a list")
        if not isinstance(result["test_results"], list):
            raise ValueError("work test_results must be a list")
        if not isinstance(result["repository_state"], dict):
            raise ValueError("work repository_state must be an object")

        normalized = dict(result)
        outcome = result["outcome"]
        if outcome not in {"ready_for_validation", "continue_work"}:
            raise ValueError(
                "work outcome must be ready_for_validation or continue_work"
            )

        resolved_paths = result.get("resolved_paths", [])
        if not isinstance(resolved_paths, list):
            raise ValueError("work resolved_paths must be a list")
        normalized_resolved_paths: list[str] = []
        for resolved_path in resolved_paths:
            normalized_resolved_paths.append(
                cls._validated_relative_path(
                    resolved_path,
                    "work resolved path",
                )
            )
        normalized_resolved_paths = sorted(
            set(normalized_resolved_paths)
        )
        if (
            execution_pass["trigger_type"] != "operation_failure"
            and normalized_resolved_paths
        ):
            raise ValueError(
                "non-operation work may not return resolved_paths"
            )
        normalized["resolved_paths"] = normalized_resolved_paths

        if outcome == "continue_work":
            continuation_fields = {
                "classification",
                "context",
                "dependencies",
                "dependency_evidence",
                "blocking",
            }
            if not continuation_fields.issubset(result):
                raise ValueError(
                    "continue_work must return the complete handoff"
                )
            normalized["classification"] = validate_classification(
                result["classification"]
            )
            if not isinstance(result["context"], dict):
                raise ValueError("work handoff context must be an object")
            dependencies, dependency_evidence = cls._validated_dependency_contract(
                result["dependencies"],
                result["dependency_evidence"],
                "work handoff",
            )
            if any(dependency not in work_keys for dependency in dependencies):
                raise ValueError(
                    "work handoff dependencies must reference this pass"
                )
            normalized["dependencies"] = dependencies
            normalized["dependency_evidence"] = dependency_evidence
            if (
                result["blocking"] is not None
                and not isinstance(result["blocking"], dict)
            ):
                raise ValueError(
                    "work handoff blocking must be an object or null"
                )
        return normalized

    @classmethod
    def _validate_resolved_path_authorization(
        cls,
        resolved_paths: list[str],
        execution_pass: dict,
        baseline: dict[str, _SourceTreeEntry],
        desired: dict[str, _SourceTreeEntry],
    ) -> None:
        if execution_pass["trigger_type"] != "operation_failure":
            return
        trigger = execution_pass.get("trigger_json")
        operation_workspace = (
            trigger.get("workspace")
            if isinstance(trigger, dict)
            else None
        )
        if not isinstance(operation_workspace, dict):
            raise ValueError(
                "operation failure has no workspace path evidence"
            )
        unmerged_paths = cls._validated_operation_path_list(
            operation_workspace,
            "unmerged_paths",
        )
        cls._validated_operation_path_list(
            operation_workspace,
            "staged_paths",
            allow_missing=True,
        )
        unstaged_paths = cls._validated_operation_path_list(
            operation_workspace,
            "unstaged_paths",
            allow_missing=True,
        )
        untracked_paths = cls._validated_operation_path_list(
            operation_workspace,
            "untracked_paths",
            allow_missing=True,
        )
        changed_paths = {
            path
            for path in baseline.keys() | desired.keys()
            if baseline.get(path) != desired.get(path)
        }
        allowed_paths = (
            changed_paths
            | set(unmerged_paths)
            | set(unstaged_paths)
            | set(untracked_paths)
        )
        if any(path not in allowed_paths for path in resolved_paths):
            raise ValueError(
                "work resolved_paths must be evidenced by this operation "
                "failure or changed by this work"
            )

    @classmethod
    def _validated_work_validation_result(
        cls,
        result: object,
        work: dict,
    ) -> dict:
        required = {
            "passed",
            "requirement_results",
            "criterion_results",
            "findings",
            "explanation",
        }
        if not isinstance(result, dict) or set(result) != required:
            raise ValueError("Work Validator must return the complete focused result")
        if not isinstance(result["passed"], bool):
            raise ValueError("work validation passed must be boolean")
        if not isinstance(result["explanation"], str) or not result[
            "explanation"
        ].strip():
            raise ValueError("work validation explanation must be nonempty")
        findings = cls._nonempty_unique_strings(
            result["findings"], "work validation findings"
        )

        def dispositions(
            values: object,
            key_field: str,
            expected_keys: list[str],
            label: str,
        ) -> list[dict]:
            if not isinstance(values, list):
                raise ValueError(f"{label} must be a list")
            normalized = []
            seen: set[str] = set()
            for value in values:
                if not isinstance(value, dict) or set(value) != {
                    key_field,
                    "passed",
                    "evidence",
                }:
                    raise ValueError(f"{label} entry is incomplete")
                key = value[key_field]
                if (
                    not isinstance(key, str)
                    or key not in expected_keys
                    or key in seen
                    or not isinstance(value["passed"], bool)
                ):
                    raise ValueError(f"{label} entry is invalid or duplicated")
                evidence = cls._nonempty_unique_strings(
                    value["evidence"], f"{label} evidence"
                )
                if not evidence:
                    raise ValueError(f"{label} evidence must be nonempty")
                seen.add(key)
                normalized.append(
                    {key_field: key, "passed": value["passed"], "evidence": evidence}
                )
            if seen != set(expected_keys):
                raise ValueError(f"{label} must disposition every applicable key")
            return normalized

        requirement_results = dispositions(
            result["requirement_results"],
            "requirement_key",
            work["requirement_keys"],
            "work validation requirement_results",
        )
        criterion_results = dispositions(
            result["criterion_results"],
            "criterion_key",
            work["acceptance_criteria"],
            "work validation criterion_results",
        )
        has_failure = bool(
            findings
            or any(not item["passed"] for item in requirement_results)
            or any(not item["passed"] for item in criterion_results)
        )
        if result["passed"] == has_failure:
            raise ValueError("work validation outcome and dispositions are inconsistent")
        return {
            "passed": result["passed"],
            "requirement_results": requirement_results,
            "criterion_results": criterion_results,
            "findings": findings,
            "explanation": result["explanation"].strip(),
        }

    @staticmethod
    def _trajectory_evidence(path: Path) -> object:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid persisted work trajectory: {path}") from error

    @staticmethod
    def _focused_work_validation_schema(work: dict) -> dict:
        return {
            "passed": True,
            "requirement_results": [
                {
                    "requirement_key": key,
                    "passed": True,
                    "evidence": ["concrete evidence for this assigned requirement"],
                }
                for key in work["requirement_keys"]
            ],
            "criterion_results": [
                {
                    "criterion_key": key,
                    "passed": True,
                    "evidence": ["concrete evidence for this assigned criterion"],
                }
                for key in work["acceptance_criteria"]
            ],
            "findings": ["focused finding; empty when none"],
            "explanation": "nonempty focused explanation",
        }

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
        feedback, pull_request_diff = self._pass_feedback_context(
            run["id"],
            execution_pass,
        )
        pass_specifications = [
            item
            for item in self.store.list_specifications(run["id"])
            if item["pass_id"] == work["pass_id"]
        ]
        specifications = {item["id"]: item for item in pass_specifications}
        specification = specifications[work["specification_id"]]
        specification_dependencies = self._specification_dependency_closure(
            pass_specifications,
            specification,
        )
        all_work = self.store.list_work_items(run["id"], work["pass_id"])
        dependency_keys = set(work["dependencies"])
        dependencies = [
            item for item in all_work if item["key"] in dependency_keys
        ]
        specification_dependency_keys = {
            item["key"] for item in specification_dependencies
        }
        evidence_providers_by_id = {
            item["id"]: item
            for item in all_work
            if (
                item["key"] in dependency_keys
                or item["id"] == work["parent_work_id"]
                or specifications[item["specification_id"]]["key"]
                in specification_dependency_keys
            )
            and item["state"] in {"COMPLETED", "HANDED_OFF"}
        }
        evidence_providers = list(evidence_providers_by_id.values())
        proposed_result: dict | None = None
        try:
            workspace = self._workspace(repository["id"], run["id"])
            with self._source_snapshot(workspace) as source_workspace:
                baseline = self._source_manifest(source_workspace)
                excluded_roots: set[str] = set()
                dependency_artifacts, dependency_artifact_integrity = (
                    self._materialize_work_evidence(
                        run["id"], evidence_providers, source_workspace
                    )
                )
                work_context = {
                    "original_issue": run["issue_json"],
                    "issue_specification": self.store.get_issue_specification(
                        run["id"], execution_pass["id"]
                    ),
                    "repository": repository,
                    "specification": self._specification_definition(
                        specification
                    ),
                    "work_item": work,
                    "dependency_results": dependencies,
                    "dependency_artifacts": dependency_artifacts,
                    "specification_dependencies": (
                        specification_dependencies
                    ),
                    "feedback": feedback,
                    "pull_request_diff": pull_request_diff,
                }
                if execution_pass["trigger_type"] == "operation_failure":
                    operation_artifacts, artifact_root = (
                        self._materialize_operation_artifacts(
                            run["id"],
                            execution_pass,
                            source_workspace,
                        )
                    )
                    excluded_roots.add(artifact_root)
                    work_context["operation_failure"] = execution_pass[
                        "trigger_json"
                    ]
                    work_context["operation_artifacts"] = (
                        operation_artifacts
                    )
                work_trajectory = self._trajectory(run["id"], f"work-{work['id']}")
                result = self.runtime.run(
                    self._task(
                        "work",
                        "Use your tools and judgment flexibly to complete this bounded work. Return ready_for_validation when no more agent work is needed, or continue_work with the next classification and handoff context. For continue_work, blocking must be null or a JSON object, never a string. "
                        "Satisfy every evidence requirement in the work item. When an assigned requirement mandates a method or external interaction, actually perform it and preserve direct execution evidence; an artifact that could have been produced without that method is not evidence that it occurred. "
                        "List each private evidence file needed by causal dependents under .repogents/ in artifacts so the controller can preserve and deliver it without publishing it as repository source. "
                        + _CLASSIFICATION_GUIDANCE,
                        work_context,
                    ),
                    source_workspace,
                    role_prompt=node["role_prompt"],
                    result_schema=(
                        _OPERATION_WORK_SCHEMA
                        if execution_pass["trigger_type"] == "operation_failure"
                        else _WORK_SCHEMA
                    ),
                    trajectory_path=work_trajectory,
                )
                result = self._validated_work_result(
                    result,
                    execution_pass,
                    {item["key"] for item in all_work},
                )
                proposed_evidence_integrity = self._work_evidence_integrity(
                    source_workspace,
                    result["artifacts"],
                )
                self._verify_materialized_work_evidence(
                    dependency_artifact_integrity
                )
                desired = self._source_manifest(
                    source_workspace,
                    excluded_roots=excluded_roots,
                )
                self._validate_resolved_path_authorization(
                    result["resolved_paths"],
                    execution_pass,
                    baseline,
                    desired,
                )
                proposed_result = dict(result)
                changed_paths = sorted(
                    path
                    for path in baseline.keys() | desired.keys()
                    if baseline.get(path) != desired.get(path)
                )
                work_validation = None
                if result["outcome"] == "ready_for_validation":
                    issue_specification = self.store.get_issue_specification(
                        run["id"], execution_pass["id"]
                    )
                    if issue_specification is None:
                        if work["requirement_keys"] or work["acceptance_criteria"]:
                            raise RuntimeError(
                                "traced work validation has no issue specification"
                            )
                    else:
                        applicable_requirements = [
                            requirement
                            for requirement in issue_specification["requirements"]
                            if requirement["key"] in work["requirement_keys"]
                        ]
                        applicable_criteria = [
                            criterion
                            for criterion in specification["acceptance_traceability"]
                            if criterion["key"] in work["acceptance_criteria"]
                        ]
                        work_validation = self.runtime.run(
                            self._task(
                                "work_validate",
                                "Independently judge only this proposed work result. "
                                "Inspect the proposed artifacts in the workspace and "
                                "disposition every assigned issue requirement and "
                                "acceptance criterion using concrete evidence. Verify "
                                "required evidence, dependency outputs, and claimed tests. "
                                "For any assigned method or external interaction, "
                                "corroborate that it actually occurred from the "
                                "execution trajectory; do not accept artifact content, "
                                "citations, or the proposal's claim as a substitute for "
                                "direct execution evidence. "
                                "Return exactly the supplied requirement and criterion "
                                "keys once each; do not disposition any other issue "
                                "requirement or criterion. "
                                "Do not implement corrections or judge unrelated work. "
                                "Fail the result for any unsupported, incomplete, or "
                                "incorrect assigned outcome.",
                                {
                                    "applicable_requirements": applicable_requirements,
                                    "applicable_criteria": applicable_criteria,
                                    "work_item": work,
                                    "dependency_results": dependencies,
                                    "dependency_artifacts": (
                                        dependency_artifacts
                                    ),
                                    "proposed_result": result,
                                    "changed_paths": changed_paths,
                                    "execution_trajectory": self._trajectory_evidence(
                                        work_trajectory
                                    ),
                                },
                            ),
                            source_workspace,
                            result_schema=self._focused_work_validation_schema(work),
                            trajectory_path=self._trajectory(
                                run["id"], f"work-validate-{work['id']}"
                            ),
                        )
                        work_validation = self._validated_work_validation_result(
                            work_validation, work
                        )
                        self._verify_materialized_work_evidence(
                            dependency_artifact_integrity
                        )
                        self._verify_materialized_work_evidence(
                            proposed_evidence_integrity,
                            label="declared work evidence",
                        )
                        work_validation = self.store.record_work_validation(
                            run["id"],
                            execution_pass["id"],
                            work["id"],
                            work_validation,
                        )["result"]
                        if not work_validation["passed"]:
                            failed_result = dict(result)
                            failed_result["work_validation"] = work_validation
                            failed_result["execution_trajectory_path"] = str(
                                work_trajectory
                            )
                            self.store.fail_work(work["id"], failed_result)
                            return
                self._verify_materialized_work_evidence(
                    proposed_evidence_integrity,
                    label="declared work evidence",
                )
                self._capture_work_evidence(
                    run["id"],
                    work["id"],
                    source_workspace,
                    result["artifacts"],
                )
                with self._durable_source_import(
                    workspace,
                    source_workspace,
                    baseline,
                    desired,
                    repository_id=repository["id"],
                    run_id=run["id"],
                    work_id=work["id"],
                ) as applied_paths:
                    repository_state = dict(result["repository_state"])
                    repository_state["_repogents"] = {
                        "applied_paths": applied_paths,
                        "resolved_paths": result["resolved_paths"],
                    }
                    persisted_result = dict(result)
                    persisted_result["repository_state"] = repository_state
                    persisted_result["execution_trajectory_path"] = str(
                        work_trajectory
                    )
                    if work_validation is not None:
                        persisted_result["work_validation"] = work_validation
                    if result["outcome"] == "ready_for_validation":
                        self.store.complete_work(
                            work["id"],
                            persisted_result,
                        )
                    else:
                        handoff = {
                            "classification": result["classification"],
                            "context": result["context"],
                            "artifacts": result["artifacts"],
                            "dependencies": result["dependencies"],
                            "dependency_evidence": result[
                                "dependency_evidence"
                            ],
                            "blocking": result["blocking"],
                        }
                        self.store.complete_work(
                            work["id"],
                            persisted_result,
                            handoff,
                        )
            self.store.record_node_success(
                node["id"], run["id"], self.config.promotion_threshold
            )
        except Exception as error:
            failure = {
                "output": {
                    "error": str(error),
                    "type": type(error).__name__,
                },
                "artifacts": [],
                "test_results": [],
                "repository_state": {},
            }
            if proposed_result is not None:
                failure["proposed_result"] = proposed_result
            try:
                self.store.fail_work(work["id"], failure)
            except (KeyError, ValueError):
                pass

    def _validation_context(
        self,
        repository: dict,
        run: dict,
        execution_pass: dict,
        candidate_diff: str,
    ) -> dict:
        feedback, _ = self._pass_feedback_context(
            run["id"],
            execution_pass,
        )
        validations = self.store.list_validations(run["id"])
        all_specifications = self.store.list_specifications(run["id"])
        return {
            "original_issue": run["issue_json"],
            "repository": repository,
            "issue_specification": self.store.get_issue_specification(
                run["id"], execution_pass["id"]
            ),
            "specifications": [
                self._specification_definition(item)
                for item in all_specifications
            ],
            "current_specifications": [
                self._specification_definition(item)
                for item in all_specifications
                if item["pass_id"] == execution_pass["id"]
            ],
            "work_items": self.store.list_work_items(
                run["id"],
                execution_pass["id"],
            ),
            "work_validations": self.store.list_work_validations(
                run["id"], execution_pass["id"]
            ),
            "latest_prior_validation": (
                self._validation_evidence(validations[-1])
                if validations
                else None
            ),
            "feedback": feedback,
            "candidate_diff": candidate_diff,
        }

    @classmethod
    def _validated_validation_result(
        cls,
        result: dict,
        issue_specification: dict | None = None,
        specifications: list[dict] | None = None,
    ) -> dict:
        required = {
            "passed",
            "failed_specifications",
            "failed_criteria",
            "code_review_findings",
            "explanation",
            "evidence",
            "repository_state",
            "completed_work",
            "commit_message",
        }
        if not isinstance(result, dict) or not required.issubset(result):
            raise ValueError("Validate must return the complete validation result")
        if not isinstance(result["passed"], bool):
            raise ValueError("validation passed must be boolean")
        for field in (
            "failed_specifications",
            "failed_criteria",
            "code_review_findings",
        ):
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
        if (
            not isinstance(result["commit_message"], str)
            or not result["commit_message"].strip()
            or "\n" in result["commit_message"].strip()
        ):
            raise ValueError("validation commit_message must be a nonempty subject line")
        has_failures = bool(
            result["failed_specifications"]
            or result["failed_criteria"]
            or result["code_review_findings"]
        )
        if issue_specification is None and result["passed"] == has_failures:
            raise ValueError("validation outcome and failures are inconsistent")
        normalized = dict(result)
        if issue_specification is None:
            return normalized

        requirements = issue_specification.get("requirements")
        if not isinstance(requirements, list):
            raise ValueError("Issue Validator has no valid issue requirements")
        requirement_keys = [item.get("key") for item in requirements]
        criterion_keys = [
            criterion.get("key")
            for specification in specifications or []
            for criterion in specification.get("acceptance_traceability", [])
            if isinstance(criterion, dict)
        ]

        def dispositions(
            field: str,
            key_field: str,
            expected: list[object],
        ) -> list[dict]:
            values = result.get(field)
            if not isinstance(values, list):
                raise ValueError(f"Issue Validator must return {field}")
            expected_keys = {
                key for key in expected if isinstance(key, str) and key
            }
            if len(expected_keys) != len(expected):
                raise ValueError(f"Issue Validator expected {field} keys are invalid")
            seen: set[str] = set()
            normalized_values = []
            for value in values:
                if not isinstance(value, dict) or set(value) != {
                    key_field,
                    "passed",
                    "evidence",
                }:
                    raise ValueError(f"Issue Validator {field} entry is incomplete")
                key = value[key_field]
                if (
                    not isinstance(key, str)
                    or key not in expected_keys
                    or key in seen
                    or not isinstance(value["passed"], bool)
                ):
                    raise ValueError(f"Issue Validator {field} entry is invalid")
                evidence = cls._nonempty_unique_strings(
                    value["evidence"], f"Issue Validator {field} evidence"
                )
                if not evidence:
                    raise ValueError(
                        f"Issue Validator {field} evidence must be nonempty"
                    )
                seen.add(key)
                normalized_values.append(
                    {key_field: key, "passed": value["passed"], "evidence": evidence}
                )
            if seen != expected_keys:
                raise ValueError(
                    f"Issue Validator must disposition every key in {field}"
                )
            return normalized_values

        requirement_results = dispositions(
            "requirement_results", "requirement_key", requirement_keys
        )
        criterion_results = dispositions(
            "criterion_results", "criterion_key", criterion_keys
        )
        integration_findings = cls._nonempty_unique_strings(
            result.get("integration_findings"),
            "Issue Validator integration_findings",
        )
        traced_failure = bool(
            integration_findings
            or any(not item["passed"] for item in requirement_results)
            or any(not item["passed"] for item in criterion_results)
        )
        if result["passed"] == (has_failures or traced_failure):
            raise ValueError(
                "Issue Validator outcome and traceability dispositions are inconsistent"
            )
        normalized["requirement_results"] = requirement_results
        normalized["criterion_results"] = criterion_results
        normalized["integration_findings"] = integration_findings
        return normalized

    @staticmethod
    def _is_current_persisted_validation(result: dict) -> bool:
        return (
            isinstance(result, dict)
            and "code_review_findings" in result
            and "publication_candidate" in result
        )

    def _pass_has_specifications(self, run_id: int, pass_id: int) -> bool:
        return any(
            item["pass_id"] == pass_id
            for item in self.store.list_specifications(run_id)
        )

    def _operation_failure_paths(
        self,
        run_id: int,
        pass_id: int,
    ) -> list[str]:
        paths: set[str] = set()
        for work in self.store.list_work_items(run_id, pass_id):
            result = work.get("result")
            if not isinstance(result, dict):
                continue
            repository_state = result.get("repository_state")
            if not isinstance(repository_state, dict):
                continue
            controller_state = repository_state.get("_repogents")
            if not isinstance(controller_state, dict):
                continue
            values = controller_state.get("resolved_paths")
            if (
                not isinstance(values, list)
                or any(
                    not isinstance(value, str) or not value
                    for value in values
                )
            ):
                raise ValueError(
                    "controller work state resolved_paths is invalid"
                )
            paths.update(
                self._validated_relative_path(
                    value,
                    "controller work state resolved_paths",
                )
                for value in values
            )
        return sorted(paths)

    def _validate(self, repository: dict, run: dict) -> None:
        execution_passes = self.store.list_passes(run["id"])
        if not execution_passes:
            raise RuntimeError("validating run has no execution pass")
        execution_pass = execution_passes[-1]
        if (
            execution_pass["trigger_type"]
            in {"feedback", "operation_failure", "validation_failure", "work_failure"}
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
            workspace = self._workspace(repository["id"], run["id"])
            if execution_pass["trigger_type"] == "operation_failure":
                continuation_paths = self._operation_failure_paths(
                    run["id"],
                    execution_pass["id"],
                )
                try:
                    self.github.continue_repository_operation(
                        workspace,
                        continuation_paths,
                    )
                except subprocess.CalledProcessError as error:
                    self._record_operation_failure(
                        repository,
                        run,
                        execution_pass,
                        "continue_repository_operation",
                        error,
                    )
                    return
            try:
                candidate, _ = self.github.prepare_publication(
                    run["issue_number"],
                    repository["target_branch"],
                    workspace,
                )
            except subprocess.CalledProcessError as error:
                self._record_operation_failure(
                    repository,
                    run,
                    execution_pass,
                    "prepare_publication",
                    error,
                )
                return

            with tempfile.TemporaryDirectory(
                prefix="repogents-validate-controller-",
                dir=self.data_dir,
            ) as temporary_directory:
                controller_workspace = (
                    Path(temporary_directory) / "workspace"
                )
                with self._source_lock:
                    shutil.copytree(
                        workspace,
                        controller_workspace,
                        symlinks=True,
                    )
                candidate_diff = self.github.candidate_diff(
                    repository["target_branch"],
                    controller_workspace,
                    candidate=candidate,
                )

            with self._source_snapshot(workspace) as validation_workspace:
                result = self.runtime.run(
                    self._task(
                        "validate",
                        "Act as the Issue Validator. Review all individually accepted "
                        "work as one integrated result. Disposition every original "
                        "issue requirement and every focused acceptance criterion "
                        "with concrete evidence. Check interoperability, required "
                        "methods, cross-work assumptions, regressions, and alignment "
                        "with the complete original issue. Independently review the "
                        "complete staged target-to-candidate diff for branch-introduced "
                        "correctness defects, regressions, and changes not mapped to "
                        "the issue, specifications, necessary prerequisites, or current "
                        "in-scope feedback. Do not audit unrelated pre-existing code. "
                        "Do not modify repository "
                        "files or implement corrections. Fail for any unsupported requirement, "
                        "criterion, integration finding, regression, or review finding. "
                        "Return a concise imperative commit_message subject describing "
                        "the actual completed repository change.",
                        self._validation_context(
                            repository,
                            run,
                            execution_pass,
                            candidate_diff,
                        ),
                    ),
                    validation_workspace,
                    result_schema=_VALIDATION_SCHEMA,
                    trajectory_path=self._trajectory(
                        run["id"], f"validate-{execution_pass['id']}"
                    ),
                )
            issue_specification = self.store.get_issue_specification(
                run["id"], execution_pass["id"]
            )
            pass_specifications = [
                item
                for item in self.store.list_specifications(run["id"])
                if item["pass_id"] == execution_pass["id"]
            ]
            result = self._validated_validation_result(
                result,
                issue_specification,
                pass_specifications,
            )
            if result["passed"]:
                try:
                    candidate = self.github.amend_publication(
                        run["issue_number"],
                        workspace,
                        candidate,
                        result["commit_message"].strip(),
                    )
                except subprocess.CalledProcessError as error:
                    self._record_operation_failure(
                        repository,
                        run,
                        execution_pass,
                        "amend_publication",
                        error,
                    )
                    return
            result["publication_candidate"] = asdict(candidate)
            self.store.record_validation(
                run["id"], execution_pass["id"], result
            )
        else:
            if not self._is_current_persisted_validation(recorded):
                self._start_publication_revalidation(
                    run,
                    execution_passes,
                    None,
                )
                return
            issue_specification = self.store.get_issue_specification(
                run["id"], execution_pass["id"]
            )
            pass_specifications = [
                item
                for item in self.store.list_specifications(run["id"])
                if item["pass_id"] == execution_pass["id"]
            ]
            result = self._validated_validation_result(
                recorded,
                issue_specification,
                pass_specifications,
            )
        if not result["passed"]:
            latest_pass = self.store.list_passes(run["id"])[-1]
            if latest_pass["id"] == execution_pass["id"]:
                trigger = dict(result)
                origin_feedback_pass_id = self._feedback_origin_pass_id(
                    execution_pass
                )
                if origin_feedback_pass_id is not None:
                    trigger["origin_feedback_pass_id"] = (
                        origin_feedback_pass_id
                    )
                self.store.create_pass(
                    run["id"],
                    "validation_failure",
                    trigger,
                )
            self.store.transition_run(run["id"], "SPECIFYING")
            return
        self.store.transition_run(run["id"], "CREATING_PR")

    def _publication_feedback_ids(
        self,
        run_id: int,
        execution_pass: dict,
    ) -> set[str]:
        origin_pass_id = self._feedback_origin_pass_id(execution_pass)
        if origin_pass_id is None:
            return set()
        result = self.store.get_feedback_scope_result(
            run_id,
            origin_pass_id,
        )
        return {
            item["external_id"]
            for item in (result or {}).get("dispositions", [])
            if isinstance(item, dict)
            and item.get("valid") is True
            and item.get("in_scope") is True
            and isinstance(item.get("external_id"), str)
        }

    @staticmethod
    def _validated_publication_candidate(
        validations: list[dict],
    ) -> PublicationCandidate | None:
        if (
            not validations
            or validations[-1]["result"].get("passed") is not True
        ):
            raise RuntimeError("publication requires a successful validation")
        payload = validations[-1]["result"].get("publication_candidate")
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise RuntimeError("validation publication candidate is invalid")
        branch = payload.get("branch")
        head_sha = payload.get("head_sha")
        target_head_sha = payload.get("target_head_sha")
        remote_head_sha = payload.get("remote_head_sha")
        if (
            not isinstance(branch, str)
            or not branch
            or not isinstance(head_sha, str)
            or not head_sha
            or not isinstance(target_head_sha, str)
            or not target_head_sha
            or not isinstance(remote_head_sha, str)
        ):
            raise RuntimeError("validation publication candidate is invalid")
        return PublicationCandidate(
            branch=branch,
            head_sha=head_sha,
            target_head_sha=target_head_sha,
            remote_head_sha=remote_head_sha,
        )

    def _start_publication_revalidation(
        self,
        run: dict,
        execution_passes: list[dict],
        candidate: PublicationCandidate | None,
    ) -> None:
        latest_pass = execution_passes[-1]
        has_latest_validation = any(
            item["pass_id"] == latest_pass["id"]
            for item in self.store.list_validations(run["id"])
        )
        if (
            latest_pass["trigger_type"] != "publication_revalidation"
            or has_latest_validation
        ):
            trigger: dict[str, Any] = {}
            if candidate is not None:
                trigger["publication_candidate"] = asdict(candidate)
            origin_feedback_pass_id = self._feedback_origin_pass_id(
                latest_pass
            )
            if origin_feedback_pass_id is not None:
                trigger["origin_feedback_pass_id"] = origin_feedback_pass_id
            self.store.create_pass(
                run["id"],
                "publication_revalidation",
                trigger,
            )
        self.store.transition_run(run["id"], "VALIDATING")

    def _publish(self, repository: dict, run: dict) -> None:
        execution_passes = self.store.list_passes(run["id"])
        if not execution_passes:
            raise RuntimeError("publication requires an execution pass")
        latest_pass_id = execution_passes[-1]["id"]
        validations = [
            item
            for item in self.store.list_validations(run["id"])
            if item["pass_id"] == latest_pass_id
        ]
        if not validations or not self._is_current_persisted_validation(
            validations[-1]["result"]
        ):
            self._start_publication_revalidation(
                run,
                execution_passes,
                None,
            )
            return
        candidate = self._validated_publication_candidate(validations)
        if candidate is None:
            self._start_publication_revalidation(
                run,
                execution_passes,
                None,
            )
            return
        existing = run.get("pull_request")
        existing_number = None if existing is None else int(existing["number"])
        pull = self.github.publish_prepared(
            repository["github_repository"],
            run["issue_number"],
            repository["target_branch"],
            self._workspace(repository["id"], run["id"]),
            candidate,
            existing_pr=existing_number,
        )
        if pull is None:
            self._start_publication_revalidation(
                run,
                execution_passes,
                candidate,
            )
            return
        claimed_feedback_ids = self._publication_feedback_ids(
            run["id"],
            execution_passes[-1],
        )
        for feedback_row in self.store.list_feedback(run["id"]):
            if (
                feedback_row["status"] != "PENDING"
                or feedback_row["external_id"] not in claimed_feedback_ids
            ):
                continue
            feedback = self._github_feedback(feedback_row)
            address = self.github.address_feedback(
                repository["github_repository"],
                pull.number,
                feedback,
                candidate.head_sha,
            )
            self.store.mark_feedback_addressed(
                run["id"],
                feedback.external_id,
                address.status,
                candidate.head_sha,
                address.response_url,
            )
        pull_request = asdict(pull)
        pull_request["validated_head_sha"] = candidate.head_sha
        self.store.transition_run(
            run["id"],
            "PR_LISTENING",
            branch=pull.branch,
            pull_request=pull_request,
            pr_listening_since=self._clock(),
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

    @staticmethod
    def _refreshed_pull_request(pull: PullRequest, reference: dict) -> dict:
        refreshed = asdict(pull)
        refreshed["validated_head_sha"] = reference.get("validated_head_sha")
        return refreshed

    def _poll_pull_request(self, repository: dict, run: dict) -> None:
        reference = run.get("pull_request")
        if not reference:
            raise RuntimeError("PR_LISTENING run has no pull request")
        pull = self.github.pull_request(
            repository["github_repository"], int(reference["number"])
        )
        pull_request = self._refreshed_pull_request(pull, reference)
        if pull.merged:
            self.store.transition_run(
                run["id"],
                "COMPLETED",
                branch=pull.branch,
                pull_request=pull_request,
            )
            self.store.adapt_nodes_after_run(
                run["id"], self.config.stale_run_threshold
            )
            return
        if pull.state == "closed":
            self.store.transition_run(
                run["id"],
                "CLOSED",
                branch=pull.branch,
                pull_request=pull_request,
            )
            self.store.adapt_nodes_after_run(
                run["id"], self.config.stale_run_threshold
            )
            return

        transition_fields: dict[str, Any] = {
            "branch": pull.branch,
            "pull_request": pull_request,
        }
        if (
            run["state"] == "PR_LISTENING"
            and run.get("pr_listening_since") is None
        ):
            transition_fields["pr_listening_since"] = self._clock()
        self.store.transition_run(
            run["id"],
            run["state"],
            **transition_fields,
        )
        specifications = self.store.list_specifications(run["id"])
        work_items = self.store.list_work_items(run["id"])
        validations = self.store.list_validations(run["id"])
        for feedback in self.github.list_feedback(
            repository["github_repository"], pull.number
        ):
            package = self._feedback_package(
                feedback,
                pull,
                run,
                specifications,
                work_items,
                validations,
            )
            self.store.add_feedback(run["id"], feedback.external_id, package)
