from __future__ import annotations

import json
import multiprocessing
import stat
import subprocess
import threading
import time
from collections import defaultdict, deque

import pytest
from pathlib import Path

from repogents.application import Application, ApplicationConfig
from repogents.github import (
    FeedbackAddress,
    GitHubClient,
    GitHubFeedback,
    GitHubIssue,
    PublicationCandidate,
    PullRequest,
)
from repogents.semantic import SemanticRouter
from repogents.store import Store


class MapEmbedder:
    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors

    def embed(self, label: str) -> list[float]:
        return list(self.vectors[label])


def test_feedback_source_url_points_to_pull_request_conversation_comment():
    pull_url = "https://github.test/acme/widget/pull/17"
    feedback_row = {
        "external_id": "comment:301",
        "package": {"kind": "comment"},
    }

    assert Application._feedback_source_url(pull_url, feedback_row) == (
        f"{pull_url}#issuecomment-301"
    )


class FakeGitHub:
    def __init__(self):
        self.issues: list[GitHubIssue] = []
        self.issues_by_repository: dict[str, list[GitHubIssue]] = {}
        self.feedback: list[GitHubFeedback] = []
        self.feedback_by_pull: dict[tuple[str, int], list[GitHubFeedback]] = {}
        self.pull = PullRequest(
            number=17,
            url="https://github.test/acme/widget/pull/17",
            branch="agent/issue-7",
            state="open",
            merged=False,
            diff="diff --git a/file b/file",
            head_sha="head-before-publication",
        )
        self.pulls: dict[tuple[str, int], PullRequest] = {}
        self.publish_existing: list[int | None] = []
        self.publish_head_shas: deque[str] = deque()
        self.prepare_publication_calls: list[tuple[int, str, Path]] = []
        self.amend_publication_calls: list[
            tuple[int, Path, PublicationCandidate, str]
        ] = []
        self.prepare_publication_overrides: deque[
            tuple[PublicationCandidate, str]
        ] = deque()
        self.publish_prepared_calls: list[
            tuple[
                str,
                int,
                str,
                Path,
                PublicationCandidate,
                int | None,
            ]
        ] = []
        self.publish_prepared_overrides: deque[PullRequest | None] = deque()
        self.publish_validated_to_target_result = False
        self.publish_validated_to_target_confirms_merge = True
        self.publish_validated_to_target_calls: list[
            tuple[str, str, Path, str]
        ] = []
        self.publish_validated_issue_branches: list[str | None] = []
        self.effect_calls: list[tuple] = []
        self.address_calls: list[tuple[str, int, GitHubFeedback, str]] = []
        self.no_code_calls: list[tuple[str, int, GitHubFeedback, str]] = []
        self.no_code_addresses: dict[tuple[str, int, str], FeedbackAddress] = {}
        self.no_code_response_urls: dict[tuple[str, int, str], str] = {}
        self.no_code_resolved_threads: set[tuple[str, int, str]] = set()
        self.no_code_response_effects: list[tuple[str, int, str]] = []
        self.no_code_thread_resolution_effects: list[tuple[str, int, str]] = []
        self.no_code_interrupt_after: str | None = None
        self.checkout_calls: list[tuple[str, str, Path]] = []
        self.pull_request_calls: list[tuple[str, int]] = []
        self.feedback_calls: list[tuple[str, int]] = []
        self.candidate_diff_calls: list[tuple[str, Path]] = []
        self.candidate_diff_candidates: list[PublicationCandidate | None] = []
        self.candidate_diff_text = "diff --git a/file.py b/file.py\n+candidate change"
        self.mutate_candidate_workspace = False
        self.follow_up_issues: dict[str, GitHubIssue] = {}
        self.follow_up_requests: list[tuple[str, str, str, str]] = []
        self.prepare_publication_failures: deque[BaseException] = deque()
        self.repository_operation_state_result = {
            "rebase_in_progress": False,
            "unmerged_paths": [],
            "staged_paths": [],
            "unstaged_paths": [],
            "untracked_paths": [],
        }
        self.repository_operation_state_calls: list[Path] = []
        self.export_repository_operation_artifacts_calls: list[
            tuple[Path, Path]
        ] = []
        self.repository_operation_artifact_contents: dict[
            str, dict[str, str]
        ] = {}
        self.continue_repository_operation_calls: list[
            tuple[Path, list[str]]
        ] = []
        self.continue_repository_operation_results: deque[
            bool | BaseException
        ] = deque()
        self.repository_operation_events: list[tuple[str, Path]] = []

    def repository(self, github_repository: str) -> dict:
        return {
            "full_name": github_repository,
            "default_branch": "main",
            "clone_url": f"https://github.test/{github_repository}.git",
        }

    def list_open_issues(self, github_repository: str) -> list[GitHubIssue]:
        return list(self.issues_by_repository.get(github_repository, self.issues))

    def checkout(
        self,
        github_repository: str,
        target_branch: str,
        workspace: str | Path,
    ) -> Path:
        path = Path(workspace)
        path.mkdir(parents=True, exist_ok=True)
        self.checkout_calls.append((github_repository, target_branch, path))
        return path

    def repository_operation_state(
        self,
        workspace: str | Path,
    ) -> dict:
        path = Path(workspace)
        self.repository_operation_state_calls.append(path)
        self.repository_operation_events.append(
            ("repository_operation_state", path)
        )
        return {
            "rebase_in_progress": self.repository_operation_state_result[
                "rebase_in_progress"
            ],
            "unmerged_paths": list(
                self.repository_operation_state_result["unmerged_paths"]
            ),
            "staged_paths": list(
                self.repository_operation_state_result["staged_paths"]
            ),
            "unstaged_paths": list(
                self.repository_operation_state_result["unstaged_paths"]
            ),
            "untracked_paths": list(
                self.repository_operation_state_result["untracked_paths"]
            ),
        }

    def export_repository_operation_artifacts(
        self,
        workspace: str | Path,
        destination: str | Path,
    ) -> dict:
        workspace_path = Path(workspace)
        destination_path = Path(destination)
        self.export_repository_operation_artifacts_calls.append(
            (workspace_path, destination_path)
        )
        self.repository_operation_events.append(
            ("export_repository_operation_artifacts", workspace_path)
        )
        manifest = {}
        for semantic_path in self.repository_operation_state_result[
            "unmerged_paths"
        ]:
            contents = self.repository_operation_artifact_contents.get(
                semantic_path,
                {
                    stage: f"{stage} version of {semantic_path}\n"
                    for stage in ("base", "ours", "theirs")
                },
            )
            artifacts = {}
            for stage in ("base", "ours", "theirs"):
                relative_path = Path(stage) / semantic_path
                artifact_path = destination_path / relative_path
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(contents[stage])
                artifacts[stage] = relative_path.as_posix()
            manifest[semantic_path] = artifacts
        return manifest

    def continue_repository_operation(
        self,
        workspace: str | Path,
        paths: list[str],
    ) -> bool:
        workspace_path = Path(workspace)
        copied_paths = list(paths)
        self.continue_repository_operation_calls.append(
            (workspace_path, copied_paths)
        )
        self.repository_operation_events.append(
            ("continue_repository_operation", workspace_path)
        )
        outcome = (
            self.continue_repository_operation_results.popleft()
            if self.continue_repository_operation_results
            else bool(
                self.repository_operation_state_result["rebase_in_progress"]
            )
        )
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome:
            self.repository_operation_state_result = {
                "rebase_in_progress": False,
                "unmerged_paths": [],
                "staged_paths": [],
                "unstaged_paths": [],
                "untracked_paths": [],
            }
        return outcome

    def candidate_diff(
        self,
        target_branch: str,
        workspace: str | Path,
        candidate: PublicationCandidate | None = None,
    ) -> str:
        path = Path(workspace)
        self.candidate_diff_calls.append((target_branch, path))
        self.candidate_diff_candidates.append(candidate)
        if self.mutate_candidate_workspace:
            (path / "candidate-diff-staging.txt").write_text("validation copy only")
        return self.candidate_diff_text

    def prepare_publication(
        self,
        issue_number: int,
        target_branch: str,
        workspace: str | Path,
    ) -> tuple[PublicationCandidate, str]:
        path = Path(workspace)
        self.prepare_publication_calls.append(
            (issue_number, target_branch, path)
        )
        self.repository_operation_events.append(
            ("prepare_publication", path)
        )
        if self.prepare_publication_failures:
            raise self.prepare_publication_failures.popleft()
        if self.prepare_publication_overrides:
            return self.prepare_publication_overrides.popleft()
        head_sha = (
            self.publish_head_shas[0]
            if self.publish_head_shas
            else self.pull.head_sha
        )
        return (
            PublicationCandidate(
                branch=f"agent/issue-{issue_number}",
                head_sha=head_sha,
                target_head_sha="target-head-sha",
                remote_head_sha=self.pull.head_sha,
            ),
            self.candidate_diff_text,
        )

    def publish_prepared(
        self,
        github_repository: str,
        issue_number: int,
        target_branch: str,
        workspace: str | Path,
        candidate: PublicationCandidate,
        existing_pr: int | None = None,
    ) -> PullRequest | None:
        self.publish_prepared_calls.append(
            (
                github_repository,
                issue_number,
                target_branch,
                Path(workspace),
                candidate,
                existing_pr,
            )
        )
        if self.publish_prepared_overrides:
            pull = self.publish_prepared_overrides.popleft()
            if pull is None:
                return None
        else:
            pull = PullRequest(
                number=self.pull.number,
                url=self.pull.url,
                branch=candidate.branch,
                state=self.pull.state,
                merged=self.pull.merged,
                diff=self.pull.diff,
                head_sha=candidate.head_sha,
            )
        if self.publish_head_shas:
            self.publish_head_shas.popleft()
        self.pull = pull
        self.publish_existing.append(existing_pr)
        self.effect_calls.append(("publish", existing_pr, pull.head_sha))
        return pull

    def amend_publication(
        self,
        issue_number: int,
        workspace: str | Path,
        candidate: PublicationCandidate,
        commit_message: str,
    ) -> PublicationCandidate:
        self.amend_publication_calls.append(
            (issue_number, Path(workspace), candidate, commit_message)
        )
        return candidate

    def publish(
        self,
        github_repository: str,
        issue_number: int,
        target_branch: str,
        workspace: str | Path,
        existing_pr: int | None = None,
    ) -> PullRequest:
        head_sha = (
            self.publish_head_shas.popleft()
            if self.publish_head_shas
            else self.pull.head_sha
        )
        self.pull = PullRequest(
            number=self.pull.number,
            url=self.pull.url,
            branch=self.pull.branch,
            state=self.pull.state,
            merged=self.pull.merged,
            diff=self.pull.diff,
            head_sha=head_sha,
        )
        self.publish_existing.append(existing_pr)
        self.effect_calls.append(("publish", existing_pr, head_sha))
        return self.pull
    def publish_validated_to_target(
        self,
        github_repository: str,
        target_branch: str,
        workspace: str | Path,
        expected_head: str,
        issue_branch: str | None = None,
    ) -> bool:
        self.publish_validated_to_target_calls.append(
            (
                github_repository,
                target_branch,
                Path(workspace),
                expected_head,
            )
        )
        self.publish_validated_issue_branches.append(issue_branch)
        if (
            self.publish_validated_to_target_result
            and self.publish_validated_to_target_confirms_merge
        ):
            self.pull = PullRequest(
                number=self.pull.number,
                url=self.pull.url,
                branch=self.pull.branch,
                state="closed",
                merged=True,
                diff=self.pull.diff,
                head_sha=self.pull.head_sha,
            )
        return self.publish_validated_to_target_result


    def pull_request(self, github_repository: str, number: int) -> PullRequest:
        self.pull_request_calls.append((github_repository, number))
        pull = self.pulls.get((github_repository, number), self.pull)
        assert number == pull.number
        return pull

    def list_feedback(
        self,
        github_repository: str,
        pull_number: int,
    ) -> list[GitHubFeedback]:
        self.feedback_calls.append((github_repository, pull_number))
        return list(
            self.feedback_by_pull.get(
                (github_repository, pull_number),
                self.feedback,
            )
        )

    def address_feedback(
        self,
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
        head_sha: str,
    ) -> FeedbackAddress:
        status = "RESOLVED" if feedback.kind == "inline" else "ACKNOWLEDGED"
        response_url = f"{self.pull.url}#response-{feedback.external_id}"
        self.address_calls.append(
            (github_repository, pull_number, feedback, head_sha)
        )
        self.effect_calls.append(
            ("address_feedback", pull_number, feedback.external_id, head_sha)
        )
        return FeedbackAddress(status=status, response_url=response_url)

    def ensure_follow_up_issue(
        self,
        github_repository: str,
        external_id: str,
        title: str,
        body: str,
    ) -> GitHubIssue:
        self.follow_up_requests.append(
            (github_repository, external_id, title, body)
        )
        issue = self.follow_up_issues.get(external_id)
        if issue is None:
            number = 100 + len(self.follow_up_issues)
            issue = GitHubIssue(
                number=number,
                title=title,
                body=body,
                url=f"https://github.test/{github_repository}/issues/{number}",
            )
            self.follow_up_issues[external_id] = issue
            self.effect_calls.append(("create_follow_up", external_id, issue.url))
        return issue

    def resolve_feedback_without_code(
        self,
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
        response: str,
    ) -> FeedbackAddress:
        key = (github_repository, pull_number, feedback.external_id)
        self.no_code_calls.append(
            (github_repository, pull_number, feedback, response)
        )
        response_url = self.no_code_response_urls.get(key)
        if response_url is None:
            response_url = f"{self.pull.url}#response-{feedback.external_id}"
            self.no_code_response_urls[key] = response_url
            self.no_code_response_effects.append(key)
        if self.no_code_interrupt_after == "response":
            self.no_code_interrupt_after = None
            raise RuntimeError("interrupted at response")
        if feedback.kind == "inline" and key not in self.no_code_resolved_threads:
            self.no_code_resolved_threads.add(key)
            self.no_code_thread_resolution_effects.append(key)
        if self.no_code_interrupt_after == "thread":
            self.no_code_interrupt_after = None
            raise RuntimeError("interrupted at thread")
        address = self.no_code_addresses.get(key)
        if address is None:
            status = "RESOLVED" if feedback.kind == "inline" else "ACKNOWLEDGED"
            address = FeedbackAddress(status=status, response_url=response_url)
            self.no_code_addresses[key] = address
            self.effect_calls.append(
                ("resolve_without_code", pull_number, feedback.external_id)
            )
        return address


class ScriptedRuntime:
    def __init__(self):
        self.results: dict[str, deque[dict]] = defaultdict(deque)
        self.calls: list[dict] = []
        self.stage_calls: list[dict] = []
        self._legacy_specifications: dict[str, dict] = {}
        self._feedback_work_area_keys: set[str] = set()

    def queue(self, kind: str, *results: dict) -> None:
        self.results[kind].extend(results)

    def run(
        self,
        task: str,
        workspace: str | Path,
        *,
        role_prompt: str = "",
        result_schema: dict | None = None,
        trajectory_path: str | Path | None = None,
    ) -> dict:
        payload = json.loads(task)
        call = {
            "payload": payload,
            "workspace": str(workspace),
            "role_prompt": role_prompt,
            "result_schema": result_schema,
            "trajectory_path": None if trajectory_path is None else str(trajectory_path),
        }
        self.stage_calls.append(call)
        kind = payload["kind"]
        if kind == "issue_specify":
            if self.results[kind]:
                return self.results[kind].popleft()
            legacy = payload["context"].get("feedback_proposed_specifications")
            if legacy is None:
                legacy = self.results["specify"].popleft()["specifications"]
            else:
                self._feedback_work_area_keys = {
                    specification["key"] for specification in legacy
                }
            self._legacy_specifications = {
                specification["key"]: specification for specification in legacy
            }
            requirements = []
            work_areas = []
            for specification in legacy:
                requirement_keys = []
                for index, criterion in enumerate(
                    specification["acceptance_criteria"], start=1
                ):
                    key = f"{specification['key']}-requirement-{index}"
                    requirement_keys.append(key)
                    requirements.append(
                        {
                            "key": key,
                            "statement": criterion,
                            "evidence": [specification["description"]],
                        }
                    )
                work_areas.append(
                    {
                        "key": specification["key"],
                        "title": specification["title"],
                        "description": specification["description"],
                        "requirement_keys": requirement_keys,
                        "dependencies": specification["dependencies"],
                        "dependency_evidence": specification["dependency_evidence"],
                    }
                )
            return {"requirements": requirements, "work_areas": work_areas}
        if kind == "work_specify":
            if self.results[kind]:
                return self.results[kind].popleft()
            area = payload["context"]["work_area"]
            specification = self._legacy_specifications[area["key"]]
            if area["key"] not in self._feedback_work_area_keys:
                legacy_call = dict(call)
                legacy_call["payload"] = {
                    **payload,
                    "kind": "specify",
                    "context": payload["context"]["pass_evidence"],
                }
                legacy_call["result_schema"] = {
                    "specifications": [
                        {
                            "work_items": [
                                {
                                    "classification": result_schema["specification"][
                                        "work_items"
                                    ][0]["classification"]
                                }
                            ]
                        }
                    ]
                }
                self.calls.append(legacy_call)
            else:
                self._feedback_work_area_keys.remove(area["key"])
            criteria = [
                {
                    "key": f"{area['key']}-criterion-{index}",
                    "description": description,
                    "requirement_keys": [area["requirement_keys"][index - 1]],
                }
                for index, description in enumerate(
                    specification["acceptance_criteria"], start=1
                )
            ]
            criterion_keys = [item["key"] for item in criteria]
            work_items = []
            for work in specification["work_items"]:
                work_items.append(
                    {
                        **work,
                        "requirement_keys": list(area["requirement_keys"]),
                        "acceptance_criteria": criterion_keys,
                        "evidence_requirements": [
                            "Evidence that the focused acceptance criteria are satisfied."
                        ],
                    }
                )
            return {
                "work_area_key": area["key"],
                "specification": {
                    "key": area["key"],
                    "title": specification["title"],
                    "description": specification["description"],
                    "requirement_keys": list(area["requirement_keys"]),
                    "acceptance_criteria": criteria,
                    "work_items": work_items,
                },
            }
        if kind == "work_validate":
            if self.results[kind]:
                return self.results[kind].popleft()
            work = payload["context"]["work_item"]
            return {
                "passed": True,
                "requirement_results": [
                    {
                        "requirement_key": key,
                        "passed": True,
                        "evidence": ["The proposed focused result satisfies this requirement."],
                    }
                    for key in work["requirement_keys"]
                ],
                "criterion_results": [
                    {
                        "criterion_key": key,
                        "passed": True,
                        "evidence": ["The proposed artifacts satisfy this criterion."],
                    }
                    for key in work["acceptance_criteria"]
                ],
                "findings": [],
                "explanation": "The focused work is supported by its evidence.",
            }
        self.calls.append(call)
        result = self.results[kind].popleft()
        if kind == "validate" and payload["context"].get("issue_specification"):
            result = dict(result)
            passed = result["passed"]
            result.setdefault(
                "requirement_results",
                [
                    {
                        "requirement_key": item["key"],
                        "passed": passed,
                        "evidence": ["The integrated result was inspected."],
                    }
                    for item in payload["context"]["issue_specification"]["requirements"]
                ],
            )
            result.setdefault(
                "criterion_results",
                [
                    {
                        "criterion_key": criterion["key"],
                        "passed": passed,
                        "evidence": ["The integrated criterion was inspected."],
                    }
                    for specification in payload["context"]["current_specifications"]
                    for criterion in specification["acceptance_traceability"]
                ],
            )
            result.setdefault("integration_findings", [] if passed else ["Validation failed."])
        return result


class WorkspaceScriptedRuntime(ScriptedRuntime):
    def __init__(self):
        super().__init__()
        self.workspace_actions: dict[str, deque] = defaultdict(deque)
        self.workspace_observations: list[dict] = []

    def queue_workspace_action(self, kind: str, *actions) -> None:
        self.workspace_actions[kind].extend(actions)

    def run(self, task: str, workspace: str | Path, **kwargs) -> dict:
        payload = json.loads(task)
        path = Path(workspace)
        self.workspace_observations.append(
            {
                "kind": payload["kind"],
                "path": path,
                "has_git": (path / ".git").exists(),
                "has_controller_metadata": (path / ".repogents").exists(),
            }
        )
        action_kind = "specify" if payload["kind"] == "issue_specify" else payload["kind"]
        if self.workspace_actions[action_kind]:
            self.workspace_actions[action_kind].popleft()(path)
        return super().run(task, workspace, **kwargs)


class BlockingWorkRuntime(ScriptedRuntime):
    def __init__(self):
        super().__init__()
        self.started: list[str] = []
        self.release = threading.Event()

    def run(self, task: str, workspace: str | Path, **kwargs) -> dict:
        payload = json.loads(task)
        if payload["kind"] != "work":
            return super().run(task, workspace, **kwargs)
        key = payload["context"]["work_item"]["key"]
        self.started.append(key)
        self.release.wait(timeout=3)
        return {
            "outcome": "ready_for_validation",
            "output": f"completed {key}",
            "artifacts": [],
            "test_results": [],
            "repository_state": {"key": key},
        }


class MutatingValidationRuntime(ScriptedRuntime):
    def __init__(self):
        super().__init__()
        self.validation_workspaces: list[Path] = []

    def run(self, task: str, workspace: str | Path, **kwargs) -> dict:
        payload = json.loads(task)
        if payload["kind"] == "validate":
            validation_workspace = Path(workspace)
            (validation_workspace / "validator-write.txt").write_text("must be discarded")
            self.validation_workspaces.append(validation_workspace)
        return super().run(task, workspace, **kwargs)


def dependency_evidence(*dependencies):
    return [
        {
            "dependency": dependency,
            "reason": f"{dependency} must provide its outcome first.",
            "evidence": [f"The graph declares {dependency} as a prerequisite."],
        }
        for dependency in dependencies
    ]


def package(*works: tuple[str, str], prefix: str = "spec") -> dict:
    specifications = []
    for index, (key, classification) in enumerate(works, start=1):
        specifications.append(
            {
                "key": f"{prefix}-{index}",
                "title": f"Specification {index}",
                "description": f"Complete {key}",
                "acceptance_criteria": [f"{key} is complete"],
                "dependencies": [],
                "dependency_evidence": [],
                "executable": True,
                "work_items": [
                    {
                        "key": key,
                        "title": f"Work {key}",
                        "description": f"Implement {key}",
                        "classification": classification,
                        "dependencies": [],
                        "dependency_evidence": [],
                    }
                ],
            }
        )
    return {"specifications": specifications}


def ready_result(output: str) -> dict:
    return {
        "outcome": "ready_for_validation",
        "output": output,
        "artifacts": [],
        "test_results": ["focused checks passed"],
        "repository_state": {"clean": False},
    }


def validation(
    passed: bool,
    explanation: str,
    *,
    failed_specifications: list[str] | None = None,
    failed_criteria: list[str] | None = None,
    code_review_findings: list[str] | None = None,
) -> dict:
    if failed_specifications is None:
        failed_specifications = [] if passed else ["spec-1"]
    if failed_criteria is None:
        failed_criteria = [] if passed else ["criterion"]
    return {
        "passed": passed,
        "failed_specifications": failed_specifications,
        "failed_criteria": failed_criteria,
        "code_review_findings": list(code_review_findings or []),
        "explanation": explanation,
        "evidence": ["observed result"],
        "repository_state": {"workspace": "captured"},
        "completed_work": [{"key": "work", "state": "COMPLETED"}],
        "commit_message": "Implement validated repository change",
    }


def issue_order(
    *issue_numbers: int,
    dependencies: dict[int, list[int]] | None = None,
) -> dict:
    dependencies = dependencies or {}
    return {
        "ordered_issues": [
            {
                "issue_number": issue_number,
                "reason": f"Issue {issue_number} belongs at this position.",
                "evidence": [f"Issue {issue_number} supplies ordering evidence."],
                "dependencies": [
                    {
                        "issue_number": dependency,
                        "reason": f"Issue {dependency} is a causal prerequisite.",
                        "evidence": [
                            f"Issue {issue_number} depends on issue {dependency}."
                        ],
                    }
                    for dependency in dependencies.get(issue_number, [])
                ],
            }
            for issue_number in issue_numbers
        ]
    }


def test_related_prior_validation_failures_follow_overlapping_criteria_without_cap():
    validations = [
        {"pass_id": 1, "result": validation(False, "first", failed_criteria=["a"])},
        {"pass_id": 2, "result": validation(False, "unrelated", failed_criteria=["b"])},
        {"pass_id": 3, "result": validation(False, "second", failed_criteria=["a"])},
        {"pass_id": 4, "result": validation(False, "latest", failed_criteria=["a"])},
    ]

    related = Application._related_prior_validation_failures(validations)

    assert [item["pass_id"] for item in related] == [1, 3]
    assert [item["result"]["explanation"] for item in related] == [
        "first",
        "second",
    ]


def feedback_disposition(
    external_id: str,
    *,
    valid: bool,
    in_scope: bool,
    pr_regression: bool = False,
    explanation: str = "Disposition follows current-head evidence.",
    evidence: list[str] | None = None,
    specification_keys: list[str] | None = None,
    follow_up_issue: dict | None = None,
) -> dict:
    return {
        "external_id": external_id,
        "valid": valid,
        "in_scope": in_scope,
        "pr_regression": pr_regression,
        "explanation": explanation,
        "evidence": list(evidence or ["Current pull-request head was inspected."]),
        "specification_keys": list(specification_keys or []),
        "follow_up_issue": follow_up_issue,
    }


def feedback_specify(
    dispositions: list[dict],
    *works: tuple[str, str],
    prefix: str = "feedback",
) -> dict:
    return {
        "dispositions": dispositions,
        "specifications": package(*works, prefix=prefix)["specifications"] if works else [],
    }


def lean_specification_definition(specification: dict) -> dict:
    return {
        field: specification[field]
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


class ControlledClock:
    def __init__(self, value: float):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def drive_until(app: Application, predicate, attempts: int = 200) -> None:
    for _ in range(attempts):
        app.poll_once()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("application did not reach expected state")


def make_app(
    tmp_path,
    runtime=None,
    vectors=None,
    *,
    store=None,
    github=None,
    clock=None,
    **config_overrides,
):
    store = store or Store(tmp_path / "state.sqlite3")
    github = github or FakeGitHub()
    runtime = runtime or ScriptedRuntime()
    router = SemanticRouter(MapEmbedder(vectors or {}))
    config = ApplicationConfig(data_dir=tmp_path / "runtime", **config_overrides)
    app = Application(store, github, runtime, router, config, clock=clock)
    return app, store, github, runtime


def test_repository_tracking_state_and_history_preserving_removal(tmp_path):
    app, store, github, _ = make_app(tmp_path)

    repository = app.add_repository("acme/widget")

    assert repository["target_branch"] == "main"
    state = app.state()
    assert [item["github_repository"] for item in state["repositories"]] == ["acme/widget"]
    assert [node["classification"] for node in state["repositories"][0]["nodes"]] == [
        "Issue Specifier",
        "Work Specifier",
        "Work Validator",
        "Issue Validator",
    ]
    app.remove_repository(repository["id"])
    assert app.state()["repositories"] == []
    assert store.get_repository(repository["id"])["tracked"] is False
    app.close()


def test_agent_prompts_request_repository_reusable_capabilities(tmp_path):
    runtime = ScriptedRuntime()
    classification = "frontend/mutation-reconciliation"
    runtime.queue("specify", package(("mutation", classification)))
    runtime.queue(
        "node_role",
        {"role_prompt": "Handle repository mutation reconciliation flexibly."},
    )
    runtime.queue("work", ready_result("mutation reconciliation complete"))
    app, _, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
    )
    app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [
        GitHubIssue(7, "Reconcile mutations", "Handle uncertain outcomes", "https://issue/7")
    ]

    drive_until(
        app,
        lambda: {"specify", "node_role", "work"}.issubset(
            {call["payload"]["kind"] for call in runtime.calls}
        ),
    )

    calls = {call["payload"]["kind"]: call for call in runtime.calls}
    for kind in ("specify", "work"):
        instruction = calls[kind]["payload"]["instruction"]
        assert "repository-reusable agent queue, not the current task" in instruction
        assert "first level names the concise kind of action" in instruction
        assert "second level names the broad stable repository capability" in instruction
        assert "shortest lowercase label" in instruction
        assert "meaningfully different suitable agent" in instruction
        assert "durable area of repository ownership" in instruction
        assert "not the object, technology, or deliverable" in instruction
        assert "Related action levels should use the same capability" in instruction
        assert "stable repository ownership boundary" in instruction
        assert "Prefer the repository subsystem or professional discipline that owns the work" in instruction
        assert "Different issue outcomes in the same ownership boundary should share the capability" in instruction
        assert "Verification of a change should keep the changed area's capability" in instruction
        assert "unless it requires a genuinely different specialist" in instruction
        assert "no vocabulary or taxonomy is prescribed" in instruction

    specify_classification_schema = calls["specify"]["result_schema"]["specifications"][0][
        "work_items"
    ][0]["classification"]
    assert specify_classification_schema == "agent-chosen concise action/capability"
    assert (
        calls["work"]["result_schema"]["classification"]
        == "agent-chosen concise action/capability required only for continue_work"
    )

    role_instruction = calls["node_role"]["payload"]["instruction"]
    assert "repository-reusable agent queue" in role_instruction
    assert "serve this classification across repository issues" in role_instruction
    assert "do not narrow the role to this issue or work item" in role_instruction
    app.close()


def test_intake_specify_multi_item_semantic_routing_role_creation_and_deduplication(tmp_path):
    runtime = ScriptedRuntime()
    runtime.queue("specify", package(("frontend", "frontend/component"), ("database", "database/migration")))
    runtime.queue("node_role", {"role_prompt": "Handle database migration work flexibly."})
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"frontend/component": [1.0, 0.0], "database/migration": [0.0, 1.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    existing = store.create_dynamic_node(
        repository["id"], "frontend/react", [1.0, 0.0], "Handle React work."
    )
    github.issues = [GitHubIssue(7, "Build feature", "Implement both parts", "https://issue/7")]

    app.poll_once()
    app.poll_once()

    runs = store.list_runs(repository["id"])
    assert len(runs) == 1
    assert runs[0]["state"] == "WAITING_FOR_WORK_COMPLETION"
    work = store.list_work_items(runs[0]["id"])
    assert len(work) == 2
    assert next(item for item in work if item["key"] == "frontend")["node_id"] == existing["id"]
    created = next(node for node in store.list_dynamic_nodes(repository["id"]) if node["id"] != existing["id"])
    assert created["classification"] == "database/migration"
    assert created["role_prompt"] == "Handle database migration work flexibly."
    assert next(item for item in work if item["key"] == "database")["node_id"] == created["id"]
    role_context = next(call["payload"]["context"] for call in runtime.calls if call["payload"]["kind"] == "node_role")
    assert role_context["classification"] == "database/migration"
    assert role_context["repository"]["github_repository"] == "acme/widget"

    app.poll_once()
    assert len(store.list_runs(repository["id"])) == 1
    app.close()


def test_feedback_specify_context_keeps_only_lean_current_evidence(tmp_path):
    old_title = "SPECIFY_OLD_WORK_TITLE_SENTINEL_" + ("L" * 2048)
    old_description = "SPECIFY_OLD_WORK_DESCRIPTION_SENTINEL_" + ("D" * 2048)
    old_artifact = "SPECIFY_OLD_ARTIFACT_SENTINEL_" + ("A" * 2048)
    old_test_result = "SPECIFY_OLD_TEST_RESULT_SENTINEL_" + ("T" * 2048)
    old_repository_state = "SPECIFY_OLD_REPOSITORY_STATE_SENTINEL_" + ("R" * 2048)
    obsolete_validation_evidence = (
        "SPECIFY_OBSOLETE_VALIDATION_SENTINEL_" + ("V" * 2048)
    )
    latest_validation_snapshot = (
        "SPECIFY_LATEST_VALIDATION_SNAPSHOT_SENTINEL_" + ("S" * 2048)
    )
    latest_completed_work = (
        "SPECIFY_LATEST_COMPLETED_WORK_SENTINEL_" + ("C" * 2048)
    )
    current_diff = "SPECIFY_CURRENT_PULL_REQUEST_DIFF_SENTINEL"
    current_head = "specify-current-head"
    current_feedback_body = "SPECIFY_CURRENT_FEEDBACK_BODY_SENTINEL"
    latest_validation_explanation = "SPECIFY_LATEST_PRIOR_VALIDATION_EVIDENCE"

    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    issue = {
        "number": 7,
        "title": "Bound feedback context",
        "body": "Keep only evidence needed for the feedback pass.",
        "url": "https://issue/7",
    }
    run, _ = store.create_run(repository["id"], 7, issue)
    old_pass = store.create_pass(run["id"], "issue", issue)
    old_package = package(
        ("legacy-rich-work", "backend/context"),
        prefix="accepted-base",
    )
    old_package["specifications"][0]["work_items"][0]["title"] = old_title
    old_package["specifications"][0]["work_items"][0][
        "description"
    ] = old_description
    old_saved = store.save_specification_package(
        run["id"],
        old_pass["id"],
        old_package,
    )
    node = store.create_dynamic_node(
        repository["id"],
        "backend/context",
        [1.0, 0.0],
        "Own backend context work.",
    )
    old_work = old_saved["work_items"][0]
    store.assign_work(old_work["id"], node["id"])
    claimed = store.claim_node_work(node["id"], run["id"])
    assert claimed is not None and claimed["id"] == old_work["id"]
    store.complete_work(
        claimed["id"],
        {
            "output": "SPECIFY_PRIOR_WORK_OUTCOME",
            "artifacts": [old_artifact],
            "test_results": [old_test_result],
            "repository_state": {"snapshot": old_repository_state},
        },
    )
    obsolete_validation = validation(False, obsolete_validation_evidence)
    obsolete_validation["repository_state"] = {
        "snapshot": "SPECIFY_OBSOLETE_VALIDATION_REPOSITORY_STATE"
    }
    obsolete_validation["completed_work"] = [
        {"marker": "SPECIFY_OBSOLETE_VALIDATION_COMPLETED_WORK"}
    ]
    store.record_validation(run["id"], old_pass["id"], obsolete_validation)

    latest_pass = store.create_pass(
        run["id"], "validation_failure", obsolete_validation
    )
    latest_saved = store.save_specification_package(
        run["id"],
        latest_pass["id"],
        package(
            ("latest-prior-work", "backend/context"),
            prefix="accepted-correction",
        ),
    )
    latest_work = latest_saved["work_items"][0]
    store.assign_work(latest_work["id"], node["id"])
    claimed = store.claim_node_work(node["id"], run["id"])
    assert claimed is not None and claimed["id"] == latest_work["id"]
    store.complete_work(
        claimed["id"],
        {
            "output": "SPECIFY_LATEST_PRIOR_WORK_OUTCOME",
            "artifacts": [],
            "test_results": [],
            "repository_state": {},
        },
    )
    latest_validation_result = validation(
        True,
        latest_validation_explanation,
    )
    latest_validation_result["repository_state"] = {
        "snapshot": latest_validation_snapshot
    }
    latest_validation_result["completed_work"] = [
        {"marker": latest_completed_work}
    ]
    latest_validation = store.record_validation(
        run["id"],
        latest_pass["id"],
        latest_validation_result,
    )

    feedback_package = {
        "external_id": "inline:bounded-specify",
        "kind": "inline",
        "body": current_feedback_body,
        "path": "context.py",
        "line": 41,
        "review_thread_id": "PRRT_bounded_specify",
        "top_level_comment_id": 901,
        "diff": current_diff,
    }
    store.add_feedback(
        run["id"],
        feedback_package["external_id"],
        feedback_package,
    )
    store.create_pass(
        run["id"],
        "feedback",
        {"feedback": [feedback_package]},
    )
    pull_request = {
        "number": 17,
        "url": "https://github.test/acme/widget/pull/17",
        "branch": "agent/issue-7",
        "state": "open",
        "merged": False,
        "diff": current_diff,
        "head_sha": current_head,
    }
    store.transition_run(
        run["id"],
        "SPECIFYING",
        branch=pull_request["branch"],
        pull_request=pull_request,
    )
    github = FakeGitHub()
    github.pull = PullRequest(
        number=17,
        url=pull_request["url"],
        branch=pull_request["branch"],
        state="open",
        merged=False,
        diff=current_diff,
        head_sha=current_head,
    )
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        feedback_specify(
            [
                feedback_disposition(
                    feedback_package["external_id"],
                    valid=True,
                    in_scope=True,
                    specification_keys=["feedback-current-1"],
                )
            ],
            ("current-feedback-fix", "backend/context"),
            prefix="feedback-current",
        ),
    )
    runtime.queue("work", ready_result("current feedback correction"))
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        runtime=runtime,
        vectors={"backend/context": [1.0, 0.0]},
    )

    app.poll_once()

    specify_call = next(
        call
        for call in runtime.calls
        if call["payload"]["kind"] == "specify"
    )
    context = specify_call["payload"]["context"]
    assert context["original_issue"] == issue
    assert context["repository"]["github_repository"] == "acme/widget"
    assert context["existing_specifications"] == [
        lean_specification_definition(old_saved["specifications"][0]),
        lean_specification_definition(latest_saved["specifications"][0]),
    ]
    prior_work = {item["key"]: item for item in context["existing_work"]}
    assert prior_work == {
        "legacy-rich-work": {
            "key": "legacy-rich-work",
            "classification": "backend/context",
            "dependencies": [],
            "dependency_evidence": [],
            "requirement_keys": [],
            "acceptance_criteria": [],
            "evidence_requirements": [],
            "state": "COMPLETED",
        },
        "latest-prior-work": {
            "key": "latest-prior-work",
            "classification": "backend/context",
            "dependencies": [],
            "dependency_evidence": [],
            "requirement_keys": [],
            "acceptance_criteria": [],
            "evidence_requirements": [],
            "state": "COMPLETED",
        },
    }
    relevant_validation = context["relevant_validation"]
    assert relevant_validation["pass_id"] == latest_validation["pass_id"]
    assert relevant_validation["result"] == {
        field: latest_validation_result.get(field)
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
        if field in latest_validation_result
    }
    assert context["feedback"] == [
        {
            "external_id": feedback_package["external_id"],
            "kind": "inline",
            "body": current_feedback_body,
            "path": "context.py",
            "line": 41,
            "review_thread_id": "PRRT_bounded_specify",
            "top_level_comment_id": 901,
        }
    ]
    assert context["pull_request_diff"] == current_diff
    assert context["pull_request_head_sha"] == current_head
    assert "prior_validation_failures" not in context
    assert "validation_history" not in context
    serialized_context = json.dumps(context, sort_keys=True)
    assert serialized_context.count(current_diff) == 1
    assert serialized_context.count(latest_validation_explanation) == 1
    for excluded in (
        old_title,
        old_description,
        "SPECIFY_PRIOR_WORK_OUTCOME",
        "SPECIFY_LATEST_PRIOR_WORK_OUTCOME",
        old_artifact,
        old_test_result,
        old_repository_state,
        obsolete_validation_evidence,
        latest_validation_snapshot,
        latest_completed_work,
        "SPECIFY_OBSOLETE_VALIDATION_REPOSITORY_STATE",
        "SPECIFY_OBSOLETE_VALIDATION_COMPLETED_WORK",
    ):
        assert excluded not in serialized_context
    app.close()


def test_node_role_context_contains_only_current_routing_inputs(tmp_path):
    old_run_sentinel = "NODE_ROLE_OLD_RUN_SENTINEL_" + ("H" * 2048)
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    issue = {"number": 7, "title": "Route work", "body": "Create a reusable role."}
    run, _ = store.create_run(repository["id"], 7, issue)
    old_pass = store.create_pass(run["id"], "issue", issue)
    old_saved = store.save_specification_package(
        run["id"],
        old_pass["id"],
        package(("old-role-history", "legacy/context"), prefix="old-role"),
    )
    old_node = store.create_dynamic_node(
        repository["id"],
        "legacy/context",
        [0.0, 1.0],
        "Own legacy context work.",
    )
    old_work = old_saved["work_items"][0]
    store.assign_work(old_work["id"], old_node["id"])
    claimed = store.claim_node_work(old_node["id"], run["id"])
    assert claimed is not None and claimed["id"] == old_work["id"]
    store.complete_work(
        claimed["id"],
        {
            "output": old_run_sentinel,
            "artifacts": [old_run_sentinel],
            "test_results": [old_run_sentinel],
            "repository_state": {"snapshot": old_run_sentinel},
        },
    )
    store.record_validation(
        run["id"],
        old_pass["id"],
        validation(False, old_run_sentinel),
    )
    current_pass = store.create_pass(
        run["id"],
        "validation_failure",
        {"explanation": "Route the current correction."},
    )
    current_saved = store.save_specification_package(
        run["id"],
        current_pass["id"],
        package(("current-role-work", "routing/current"), prefix="current-role"),
    )
    current_work = current_saved["work_items"][0]
    store.transition_run(run["id"], "SPECIFYING")
    runtime = ScriptedRuntime()
    runtime.queue(
        "node_role",
        {"role_prompt": "Own current routing work across repository issues."},
    )
    runtime.queue("work", ready_result("current routed work"))
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        runtime=runtime,
        vectors={"routing/current": [1.0, 0.0]},
    )

    app.poll_once()

    role_call = next(
        call
        for call in runtime.calls
        if call["payload"]["kind"] == "node_role"
    )
    context = role_call["payload"]["context"]
    assert context == {
        "classification": "routing/current",
        "work_item": current_work,
        "repository": repository,
    }
    assert old_run_sentinel not in json.dumps(context, sort_keys=True)
    app.close()


def test_reopened_work_context_uses_current_pass_dependency_closure(tmp_path):
    old_specification_sentinel = "WORK_OLD_PASS_SPECIFICATION_SENTINEL"
    old_work_sentinel = "WORK_OLD_PASS_RESULT_SENTINEL_" + ("O" * 2048)
    unrelated_specification_sentinel = (
        "WORK_UNRELATED_CURRENT_SPECIFICATION_SENTINEL"
    )
    unrelated_work_sentinel = "WORK_UNRELATED_CURRENT_WORK_SENTINEL"
    dependency_result_sentinel = "WORK_DECLARED_DEPENDENCY_RESULT_SENTINEL"
    handoff_context_sentinel = "WORK_CURRENT_HANDOFF_CONTEXT_SENTINEL"
    current_diff = "WORK_CURRENT_FEEDBACK_DIFF_SENTINEL"
    database = tmp_path / "state.sqlite3"
    store = Store(database)
    repository = store.add_repository("acme/widget", "main", 0.75)
    issue = {
        "number": 7,
        "title": "Resume bounded work",
        "body": "Use only current-pass dependencies after restart.",
    }
    run, _ = store.create_run(repository["id"], 7, issue)
    old_pass = store.create_pass(run["id"], "issue", issue)
    old_package = package(("old-pass-work", "legacy/context"), prefix="old-pass")
    old_package["specifications"][0]["description"] = old_specification_sentinel
    old_saved = store.save_specification_package(
        run["id"],
        old_pass["id"],
        old_package,
    )
    old_node = store.create_dynamic_node(
        repository["id"],
        "legacy/context",
        [0.0, 0.0, 1.0, 0.0],
        "Own legacy context work.",
    )
    old_work = old_saved["work_items"][0]
    store.assign_work(old_work["id"], old_node["id"])
    claimed = store.claim_node_work(old_node["id"], run["id"])
    assert claimed is not None and claimed["id"] == old_work["id"]
    store.complete_work(
        claimed["id"],
        {
            "output": old_work_sentinel,
            "artifacts": [old_work_sentinel],
            "test_results": [old_work_sentinel],
            "repository_state": {"snapshot": old_work_sentinel},
        },
    )

    feedback_package = {
        "external_id": "inline:bounded-work",
        "kind": "inline",
        "body": "WORK_CURRENT_FEEDBACK_BODY_SENTINEL",
        "path": "worker.py",
        "line": 73,
        "review_thread_id": "PRRT_bounded_work",
        "top_level_comment_id": 902,
        "diff": current_diff,
    }
    store.add_feedback(
        run["id"],
        feedback_package["external_id"],
        feedback_package,
    )
    current_pass = store.create_pass(
        run["id"],
        "feedback",
        {"feedback": [feedback_package]},
    )
    current_package = {
        "specifications": [
            {
                "key": "closure-root",
                "title": "Closure root",
                "description": "WORK_CLOSURE_ROOT_DEFINITION",
                "acceptance_criteria": ["Root evidence remains available."],
                "dependencies": [],
                "dependency_evidence": [],
                "executable": False,
                "work_items": [],
            },
            {
                "key": "closure-middle",
                "title": "Closure middle",
                "description": "WORK_CLOSURE_MIDDLE_DEFINITION",
                "acceptance_criteria": ["Middle evidence remains available."],
                "dependencies": ["closure-root"],
                "dependency_evidence": dependency_evidence("closure-root"),
                "executable": False,
                "work_items": [],
            },
            {
                "key": "current-target",
                "title": "Current target",
                "description": "WORK_CURRENT_TARGET_DEFINITION",
                "acceptance_criteria": ["The current handoff is completed."],
                "dependencies": ["closure-middle"],
                "dependency_evidence": dependency_evidence("closure-middle"),
                "executable": True,
                "work_items": [
                    {
                        "key": "declared-dependency",
                        "title": "Declared dependency",
                        "description": "Prepare the declared dependency.",
                        "classification": "prepare/current",
                        "dependencies": [],
                        "dependency_evidence": [],
                    },
                    {
                        "key": "current-parent",
                        "title": "Current parent",
                        "description": "Produce the durable handoff.",
                        "classification": "implement/current",
                        "dependencies": ["declared-dependency"],
                        "dependency_evidence": dependency_evidence(
                            "declared-dependency"
                        ),
                    },
                ],
            },
            {
                "key": "unrelated-current",
                "title": "Unrelated current specification",
                "description": unrelated_specification_sentinel,
                "acceptance_criteria": ["Unrelated work is complete."],
                "dependencies": [],
                "dependency_evidence": [],
                "executable": True,
                "work_items": [
                    {
                        "key": "unrelated-current-work",
                        "title": "Unrelated current work",
                        "description": unrelated_work_sentinel,
                        "classification": "unrelated/current",
                        "dependencies": [],
                        "dependency_evidence": [],
                    }
                ],
            },
        ]
    }
    current_saved = store.save_specification_package(
        run["id"],
        current_pass["id"],
        current_package,
    )
    current_work = {item["key"]: item for item in current_saved["work_items"]}
    dependency_node = store.create_dynamic_node(
        repository["id"],
        "prepare/current",
        [0.0, 0.0, 0.0, 1.0],
        "Prepare current dependencies.",
    )
    parent_node = store.create_dynamic_node(
        repository["id"],
        "implement/current",
        [0.0, 0.0, 1.0, 1.0],
        "Implement current work.",
    )
    store.assign_work(
        current_work["declared-dependency"]["id"],
        dependency_node["id"],
    )
    claimed = store.claim_node_work(dependency_node["id"], run["id"])
    assert claimed is not None
    store.complete_work(
        claimed["id"],
        {
            "output": dependency_result_sentinel,
            "artifacts": ["WORK_DECLARED_DEPENDENCY_ARTIFACT"],
            "test_results": ["WORK_DECLARED_DEPENDENCY_TEST_RESULT"],
            "repository_state": {"state": "dependency-ready"},
        },
    )
    store.assign_work(current_work["current-parent"]["id"], parent_node["id"])
    claimed = store.claim_node_work(parent_node["id"], run["id"])
    assert claimed is not None
    handoff = {
        "classification": "verify/current",
        "context": {"request": handoff_context_sentinel},
        "artifacts": ["WORK_CURRENT_HANDOFF_ARTIFACT"],
        "dependencies": ["declared-dependency"],
        "dependency_evidence": dependency_evidence("declared-dependency"),
        "blocking": {"reason": "WORK_CURRENT_HANDOFF_BLOCKING_EVIDENCE"},
    }
    child = store.complete_work(
        claimed["id"],
        {
            "output": "WORK_CURRENT_PARENT_RESULT",
            "artifacts": ["WORK_CURRENT_PARENT_ARTIFACT"],
            "test_results": ["WORK_CURRENT_PARENT_TEST_RESULT"],
            "repository_state": {"state": "handoff-ready"},
        },
        handoff,
    )
    assert child is not None
    store.create_dynamic_node(
        repository["id"],
        "verify/current",
        [1.0, 0.0, 0.0, 0.0],
        "Verify current work.",
    )
    store.create_dynamic_node(
        repository["id"],
        "unrelated/current",
        [0.0, 1.0, 0.0, 0.0],
        "Own unrelated current work.",
    )
    current_head = "work-current-head"
    store.record_feedback_scope_result(
        run["id"],
        current_pass["id"],
        {
            "dispositions": [
                feedback_disposition(
                    feedback_package["external_id"],
                    valid=True,
                    in_scope=True,
                )
            ],
            "specifications": [],
            "head_sha": current_head,
        },
    )
    pull_request = {
        "number": 17,
        "url": "https://github.test/acme/widget/pull/17",
        "branch": "agent/issue-7",
        "state": "open",
        "merged": False,
        "diff": current_diff,
        "head_sha": current_head,
    }
    store.transition_run(
        run["id"],
        "WAITING_FOR_WORK_COMPLETION",
        branch=pull_request["branch"],
        pull_request=pull_request,
    )

    reopened_store = Store(database)
    github = FakeGitHub()
    github.pull = PullRequest(
        number=17,
        url=pull_request["url"],
        branch=pull_request["branch"],
        state="open",
        merged=False,
        diff=current_diff,
        head_sha=current_head,
    )
    runtime = ScriptedRuntime()
    runtime.queue(
        "work",
        ready_result("WORK_CURRENT_CHILD_RESULT"),
        ready_result("WORK_UNRELATED_RUNTIME_RESULT"),
    )
    app, _, _, _ = make_app(
        tmp_path,
        store=reopened_store,
        github=github,
        runtime=runtime,
        vectors={
            "verify/current": [1.0, 0.0, 0.0, 0.0],
            "unrelated/current": [0.0, 1.0, 0.0, 0.0],
        },
        max_workers=1,
    )

    drive_until(
        app,
        lambda: any(
            call["payload"]["kind"] == "work"
            and call["payload"]["context"]["work_item"]["key"] == child["key"]
            for call in runtime.calls
        ),
    )

    work_call = next(
        call
        for call in runtime.calls
        if call["payload"]["kind"] == "work"
        and call["payload"]["context"]["work_item"]["key"] == child["key"]
    )
    context = work_call["payload"]["context"]
    assert context["work_item"]["key"] == child["key"]
    assert context["work_item"]["handoff"] == handoff
    assert lean_specification_definition(context["specification"]) == (
        lean_specification_definition(
            next(
                item
                for item in current_saved["specifications"]
                if item["key"] == "current-target"
            )
        )
    )
    assert context["specification_dependencies"] == [
        lean_specification_definition(
            next(
                item
                for item in current_saved["specifications"]
                if item["key"] == key
            )
        )
        for key in ("closure-root", "closure-middle")
    ]
    assert [item["key"] for item in context["dependency_results"]] == [
        "declared-dependency"
    ]
    assert (
        context["dependency_results"][0]["result"]["output"]
        == dependency_result_sentinel
    )
    assert context["feedback"] == [
        {
            "external_id": feedback_package["external_id"],
            "kind": "inline",
            "body": "WORK_CURRENT_FEEDBACK_BODY_SENTINEL",
            "path": "worker.py",
            "line": 73,
            "review_thread_id": "PRRT_bounded_work",
            "top_level_comment_id": 902,
        }
    ]
    assert context["pull_request_diff"] == current_diff
    assert "prior_specifications" not in context
    assert "prior_work" not in context
    serialized_context = json.dumps(context, sort_keys=True)
    for excluded in (
        old_specification_sentinel,
        old_work_sentinel,
        unrelated_specification_sentinel,
        unrelated_work_sentinel,
    ):
        assert excluded not in serialized_context
    app.close()


def test_validate_context_keeps_current_pass_and_latest_prior_evidence(tmp_path):
    old_artifact = "VALIDATE_OLD_WORK_ARTIFACT_SENTINEL_" + ("A" * 2048)
    old_test_result = "VALIDATE_OLD_WORK_TEST_SENTINEL_" + ("T" * 2048)
    old_repository_state = "VALIDATE_OLD_WORK_REPOSITORY_SENTINEL_" + ("R" * 2048)
    obsolete_validation = "VALIDATE_OBSOLETE_VALIDATION_SENTINEL_" + ("V" * 2048)
    latest_validation_snapshot = (
        "VALIDATE_LATEST_VALIDATION_SNAPSHOT_SENTINEL_" + ("S" * 2048)
    )
    latest_completed_work = (
        "VALIDATE_LATEST_COMPLETED_WORK_SENTINEL_" + ("C" * 2048)
    )
    latest_validation_explanation = "VALIDATE_LATEST_PRIOR_VALIDATION_EVIDENCE"
    duplicate_pull_request_diff = "VALIDATE_DUPLICATE_PULL_REQUEST_DIFF_SENTINEL"
    complete_candidate_diff = (
        "diff --git a/current.py b/current.py\n"
        "+VALIDATE_COMPLETE_CANDIDATE_DIFF_SENTINEL"
    )
    current_head = "validate-current-head"

    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    issue = {
        "number": 7,
        "title": "Validate bounded context",
        "body": "Review the complete candidate with bounded history.",
    }
    run, _ = store.create_run(repository["id"], 7, issue)
    node = store.create_dynamic_node(
        repository["id"],
        "backend/validation",
        [1.0, 0.0],
        "Own validation setup work.",
    )

    old_pass = store.create_pass(run["id"], "issue", issue)
    old_saved = store.save_specification_package(
        run["id"],
        old_pass["id"],
        package(("validate-old-work", "backend/validation"), prefix="accepted-old"),
    )
    old_work = old_saved["work_items"][0]
    store.assign_work(old_work["id"], node["id"])
    claimed = store.claim_node_work(node["id"], run["id"])
    assert claimed is not None
    store.complete_work(
        claimed["id"],
        {
            "output": "VALIDATE_OLD_WORK_OUTPUT",
            "artifacts": [old_artifact],
            "test_results": [old_test_result],
            "repository_state": {"snapshot": old_repository_state},
        },
    )
    old_validation_result = validation(False, obsolete_validation)
    store.record_validation(run["id"], old_pass["id"], old_validation_result)

    prior_pass = store.create_pass(
        run["id"],
        "validation_failure",
        old_validation_result,
    )
    prior_saved = store.save_specification_package(
        run["id"],
        prior_pass["id"],
        package(
            ("validate-prior-work", "backend/validation"),
            prefix="accepted-prior",
        ),
    )
    prior_work = prior_saved["work_items"][0]
    store.assign_work(prior_work["id"], node["id"])
    claimed = store.claim_node_work(node["id"], run["id"])
    assert claimed is not None
    store.complete_work(
        claimed["id"],
        {
            "output": "VALIDATE_PRIOR_WORK_OUTPUT",
            "artifacts": [],
            "test_results": [],
            "repository_state": {},
        },
    )
    latest_validation_result = validation(
        True,
        latest_validation_explanation,
    )
    latest_validation_result["repository_state"] = {
        "snapshot": latest_validation_snapshot
    }
    latest_validation_result["completed_work"] = [
        {"marker": latest_completed_work}
    ]
    latest_validation = store.record_validation(
        run["id"],
        prior_pass["id"],
        latest_validation_result,
    )

    feedback_package = {
        "external_id": "inline:bounded-validate",
        "kind": "inline",
        "body": "VALIDATE_CURRENT_FEEDBACK_BODY_SENTINEL",
        "path": "current.py",
        "line": 88,
        "review_thread_id": "PRRT_bounded_validate",
        "top_level_comment_id": 903,
        "diff": duplicate_pull_request_diff,
    }
    store.add_feedback(
        run["id"],
        feedback_package["external_id"],
        feedback_package,
    )
    current_pass = store.create_pass(
        run["id"],
        "feedback",
        {"feedback": [feedback_package]},
    )
    current_saved = store.save_specification_package(
        run["id"],
        current_pass["id"],
        package(
            ("validate-current-work", "backend/validation"),
            prefix="accepted-current",
        ),
    )
    current_work = current_saved["work_items"][0]
    store.assign_work(current_work["id"], node["id"])
    claimed = store.claim_node_work(node["id"], run["id"])
    assert claimed is not None
    store.complete_work(
        claimed["id"],
        {
            "output": "VALIDATE_CURRENT_PASS_WORK_OUTPUT",
            "artifacts": ["VALIDATE_CURRENT_PASS_ARTIFACT"],
            "test_results": ["VALIDATE_CURRENT_PASS_TEST_RESULT"],
            "repository_state": {"state": "current-pass-ready"},
        },
    )
    store.record_feedback_scope_result(
        run["id"],
        current_pass["id"],
        {
            "dispositions": [
                feedback_disposition(
                    feedback_package["external_id"],
                    valid=True,
                    in_scope=True,
                )
            ],
            "specifications": [],
            "head_sha": current_head,
        },
    )
    pull_request = {
        "number": 17,
        "url": "https://github.test/acme/widget/pull/17",
        "branch": "agent/issue-7",
        "state": "open",
        "merged": False,
        "diff": duplicate_pull_request_diff,
        "head_sha": current_head,
    }
    store.transition_run(
        run["id"],
        "VALIDATING",
        branch=pull_request["branch"],
        pull_request=pull_request,
    )
    (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    ).mkdir(parents=True)
    github = FakeGitHub()
    github.pull = PullRequest(
        number=17,
        url=pull_request["url"],
        branch=pull_request["branch"],
        state="open",
        merged=False,
        diff=duplicate_pull_request_diff,
        head_sha=current_head,
    )
    github.candidate_diff_text = complete_candidate_diff
    runtime = ScriptedRuntime()
    runtime.queue(
        "validate",
        validation(True, "VALIDATE_CURRENT_RESULT"),
    )
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        runtime=runtime,
    )

    app.poll_once()

    validate_call = next(
        call
        for call in runtime.calls
        if call["payload"]["kind"] == "validate"
    )
    context = validate_call["payload"]["context"]
    assert context["original_issue"] == issue
    assert context["repository"]["github_repository"] == "acme/widget"
    assert context["specifications"] == [
        lean_specification_definition(item)
        for item in (
            old_saved["specifications"][0],
            prior_saved["specifications"][0],
            current_saved["specifications"][0],
        )
    ]
    assert [item["key"] for item in context["work_items"]] == [
        "validate-current-work"
    ]
    assert (
        context["work_items"][0]["result"]["output"]
        == "VALIDATE_CURRENT_PASS_WORK_OUTPUT"
    )
    latest_prior_validation = context["latest_prior_validation"]
    assert latest_prior_validation["pass_id"] == latest_validation["pass_id"]
    assert latest_prior_validation["result"] == {
        field: latest_validation_result[field]
        for field in (
            "passed",
            "failed_specifications",
            "failed_criteria",
            "code_review_findings",
            "explanation",
            "evidence",
        )
    }
    assert context["feedback"] == [
        {
            "external_id": feedback_package["external_id"],
            "kind": "inline",
            "body": "VALIDATE_CURRENT_FEEDBACK_BODY_SENTINEL",
            "path": "current.py",
            "line": 88,
            "review_thread_id": "PRRT_bounded_validate",
            "top_level_comment_id": 903,
        }
    ]
    assert context["candidate_diff"] == complete_candidate_diff
    assert "pull_request_diff" not in context
    assert "validation_history" not in context
    serialized_context = json.dumps(context, sort_keys=True)
    assert serialized_context.count(latest_validation_explanation) == 1
    for excluded in (
        old_artifact,
        old_test_result,
        old_repository_state,
        obsolete_validation,
        latest_validation_snapshot,
        latest_completed_work,
        duplicate_pull_request_diff,
    ):
        assert excluded not in serialized_context
    app.close()


def test_dynamic_nodes_run_concurrently_keep_busy_queue_and_hold_validation_barrier(tmp_path):
    runtime = BlockingWorkRuntime()
    runtime.queue(
        "specify",
        package(("a-first", "domain/a"), ("a-second", "domain/a"), ("b-first", "domain/b")),
    )
    runtime.queue("validate", validation(True, "all work complete"))
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"domain/a": [1.0, 0.0], "domain/b": [0.0, 1.0]},
        max_workers=4,
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    store.create_dynamic_node(repository["id"], "domain/a", [1.0, 0.0], "A role")
    store.create_dynamic_node(repository["id"], "domain/b", [0.0, 1.0], "B role")
    github.issues = [GitHubIssue(7, "Concurrent", "Run work", "https://issue/7")]

    app.poll_once()
    app.poll_once()
    for _ in range(100):
        app.poll_once()
        if set(runtime.started) == {"a-first", "b-first"}:
            break
        time.sleep(0.01)

    assert set(runtime.started) == {"a-first", "b-first"}
    run = store.list_runs(repository["id"])[0]
    queued = {item["key"]: item["state"] for item in store.list_work_items(run["id"])}
    assert queued["a-second"] == "QUEUED"
    assert not any(call["payload"]["kind"] == "validate" for call in runtime.calls)

    runtime.release.set()
    drive_until(
        app,
        lambda: any(call["payload"]["kind"] == "validate" for call in runtime.calls),
    )
    assert "a-second" in runtime.started
    assert all(item["state"] == "COMPLETED" for item in store.list_work_items(run["id"]))
    app.close()


def test_agent_turns_use_gitless_disposable_snapshots_and_import_only_work_delta(
    tmp_path,
):
    classification = "change/source-area"
    runtime = WorkspaceScriptedRuntime()
    runtime.queue(
        "specify",
        package(("source-delta", classification), prefix="workspace"),
    )
    runtime.queue(
        "node_role",
        {"role_prompt": "Own agent-selected source changes."},
    )
    runtime.queue("work", ready_result("source delta completed"))
    runtime.queue("validate", validation(True, "source delta is valid"))
    work_views = []
    validation_views = []

    def write_during_specify(agent_workspace: Path) -> None:
        (agent_workspace / "src").mkdir(parents=True, exist_ok=True)
        (agent_workspace / "specify-only.txt").write_text("discard me\n")
        (agent_workspace / "src" / "existing.py").write_text(
            "specify write\n"
        )

    def write_during_node_role(agent_workspace: Path) -> None:
        (agent_workspace / "src" / "role-only.py").write_text("discard me\n")

    def write_during_work(agent_workspace: Path) -> None:
        work_views.append(
            {
                "existing": (
                    agent_workspace / "src" / "existing.py"
                ).read_text(),
                "role_write_exists": (
                    agent_workspace / "src" / "role-only.py"
                ).exists(),
            }
        )
        (agent_workspace / "src" / "existing.py").write_text("edited by work\n")
        (agent_workspace / "src" / "added.py").write_text("added by work\n")
        (agent_workspace / "src" / "obsolete.py").unlink()

    def write_during_validate(agent_workspace: Path) -> None:
        validation_views.append(
            {
                "existing": (
                    agent_workspace / "src" / "existing.py"
                ).read_text(),
                "added": (agent_workspace / "src" / "added.py").read_text(),
                "obsolete_exists": (
                    agent_workspace / "src" / "obsolete.py"
                ).exists(),
            }
        )
        (agent_workspace / "src" / "existing.py").write_text(
            "validate write\n"
        )
        (agent_workspace / "validate-only.txt").write_text("discard me\n")

    runtime.queue_workspace_action("specify", write_during_specify)
    runtime.queue_workspace_action("node_role", write_during_node_role)
    runtime.queue_workspace_action("work", write_during_work)
    runtime.queue_workspace_action("validate", write_during_validate)
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [
        GitHubIssue(
            7,
            "Change source",
            "Edit, add, and delete source files.",
            "https://issue/7",
        )
    ]

    app.poll_once()
    run = store.list_runs(repository["id"])[0]
    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "src").mkdir()
    (workspace / "src" / "existing.py").write_text("original\n")
    (workspace / "src" / "obsolete.py").write_text("remove me\n")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("credential = controller-only\n")
    (workspace / ".repogents").mkdir()
    (workspace / ".repogents" / "operation.json").write_text(
        '{"owner":"controller"}\n'
    )

    drive_until(
        app,
        lambda: store.get_run(run["id"])["state"] == "PR_LISTENING",
    )

    assert [
        observation["kind"]
        for observation in runtime.workspace_observations
    ] == [
        "issue_specify",
        "work_specify",
        "node_role",
        "work",
        "work_validate",
        "validate",
    ]
    assert all(
        not observation["has_git"]
        and not observation["has_controller_metadata"]
        and observation["path"] != workspace
        for observation in runtime.workspace_observations
    )
    assert all(
        not observation["path"].exists()
        for observation in runtime.workspace_observations
    )
    assert work_views == [
        {
            "existing": "original\n",
            "role_write_exists": False,
        }
    ]
    assert validation_views == [
        {
            "existing": "edited by work\n",
            "added": "added by work\n",
            "obsolete_exists": False,
        }
    ]
    assert (workspace / "src" / "existing.py").read_text() == "edited by work\n"
    assert (workspace / "src" / "added.py").read_text() == "added by work\n"
    assert not (workspace / "src" / "obsolete.py").exists()
    assert not (workspace / "specify-only.txt").exists()
    assert not (workspace / "src" / "role-only.py").exists()
    assert not (workspace / "validate-only.txt").exists()
    assert (
        workspace / ".git" / "config"
    ).read_text() == "credential = controller-only\n"
    assert (
        workspace / ".repogents" / "operation.json"
    ).read_text() == '{"owner":"controller"}\n'
    completed_work = store.list_work_items(run["id"])[0]
    assert completed_work["result"]["artifacts"] == []
    app.close()


def test_invalid_work_output_discards_disposable_source_delta(tmp_path):
    classification = "change/invalid-output-area"
    runtime = WorkspaceScriptedRuntime()
    runtime.queue(
        "specify",
        package(("invalid-delta", classification), prefix="invalid"),
    )
    runtime.queue(
        "work",
        {
            "outcome": "ready_for_validation",
            "output": "must not commit this delta",
            "artifacts": "not-a-list",
            "test_results": [],
            "repository_state": {},
        },
    )

    def write_before_invalid_result(agent_workspace: Path) -> None:
        (agent_workspace / "source.py").write_text("invalid edit\n")
        (agent_workspace / "invalid-addition.py").write_text(
            "must not import\n"
        )

    runtime.queue_workspace_action("work", write_before_invalid_result)
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Own source changes.",
    )
    github.issues = [
        GitHubIssue(7, "Invalid output", "Change source", "https://issue/7")
    ]

    app.poll_once()
    run = store.list_runs(repository["id"])[0]
    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "source.py").write_text("original\n")
    app.poll_once()
    drive_until(
        app,
        lambda: bool(store.list_work_items(run["id"]))
        and store.list_work_items(run["id"])[0]["state"]
        in {"COMPLETED", "FAILED"},
    )

    failed_work = store.list_work_items(run["id"])[0]
    assert failed_work["state"] == "FAILED"
    assert (workspace / "source.py").read_text() == "original\n"
    assert not (workspace / "invalid-addition.py").exists()
    work_observation = next(
        observation
        for observation in runtime.workspace_observations
        if observation["kind"] == "work"
    )
    assert work_observation["path"] != workspace
    assert store.list_validations(run["id"]) == []
    app.close()
    assert not work_observation["path"].exists()


def test_failed_work_creates_adaptive_failure_pass_without_validation_or_publication(
    tmp_path,
):
    classification = "change/adaptive-failure"
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        package(("broken-work", classification), prefix="initial"),
        package(("replanned-work", classification), prefix="replanned"),
    )
    runtime.queue(
        "work",
        {
            "outcome": "ready_for_validation",
            "output": "invalid worker result",
            "artifacts": "not-a-list",
            "test_results": [],
            "repository_state": {},
        },
        ready_result("replanned work"),
    )
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Adapt repository work after failures.",
    )
    github.issues = [
        GitHubIssue(7, "Adaptive failure", "Complete the work", "https://issue/7")
    ]

    app.poll_once()
    run = store.list_runs(repository["id"])[0]
    app.poll_once()
    drive_until(
        app,
        lambda: any(
            item["trigger_type"] == "work_failure"
            for item in store.list_passes(run["id"])
        ),
    )

    work_failure_pass = store.list_passes(run["id"])[-1]
    assert work_failure_pass["trigger_type"] == "work_failure"
    assert work_failure_pass["trigger_json"]["failed_pass_id"] == (
        store.list_passes(run["id"])[0]["id"]
    )
    failed_work = work_failure_pass["trigger_json"]["failed_work"]
    assert [item["key"] for item in failed_work] == ["broken-work"]
    assert failed_work[0]["result"]["output"]["type"] == "ValueError"
    assert not any(call["payload"]["kind"] == "validate" for call in runtime.calls)
    assert github.prepare_publication_calls == []
    assert github.publish_prepared_calls == []

    app.poll_once()
    specify_calls = [
        call for call in runtime.calls if call["payload"]["kind"] == "specify"
    ]
    assert specify_calls[-1]["payload"]["context"]["work_failure"] == (
        work_failure_pass["trigger_json"]
    )
    app.close()


def test_failed_work_preserves_and_executes_independent_sibling_before_replanning(
    tmp_path,
):
    classification = "change/adaptive-independent"
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        package(
            ("broken-work", classification),
            ("independent-work", classification),
            prefix="initial",
        ),
        package(("replanned-work", classification), prefix="replanned"),
    )
    runtime.queue(
        "work",
        {
            "outcome": "ready_for_validation",
            "output": "invalid worker result",
            "artifacts": "not-a-list",
            "test_results": [],
            "repository_state": {},
        },
        ready_result("independent work completed"),
        ready_result("replanned work completed"),
    )
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Adapt repository work while preserving independent outcomes.",
    )
    github.issues = [
        GitHubIssue(7, "Adaptive siblings", "Complete both parts", "https://issue/7")
    ]

    app.poll_once()
    run = store.list_runs(repository["id"])[0]
    app.poll_once()
    drive_until(
        app,
        lambda: any(
            item["trigger_type"] == "work_failure"
            for item in store.list_passes(run["id"])
        ),
    )

    original_work = {
        item["key"]: item
        for item in store.list_work_items(run["id"], store.list_passes(run["id"])[0]["id"])
    }
    assert original_work["broken-work"]["state"] == "FAILED"
    assert original_work["independent-work"]["state"] == "COMPLETED"
    assert original_work["independent-work"]["result"]["output"] == (
        "independent work completed"
    )
    work_failure_pass = store.list_passes(run["id"])[-1]
    work_graph = {
        item["key"]: item
        for item in work_failure_pass["trigger_json"]["work_graph"]
    }
    assert work_graph["broken-work"]["state"] == "FAILED"
    assert work_graph["independent-work"]["result"]["output"] == (
        "independent work completed"
    )
    work_calls = [
        call["payload"]["context"]["work_item"]["key"]
        for call in runtime.calls
        if call["payload"]["kind"] == "work"
    ]
    assert work_calls == ["broken-work", "independent-work"]
    assert not any(call["payload"]["kind"] == "validate" for call in runtime.calls)
    assert github.prepare_publication_calls == []
    assert github.publish_prepared_calls == []
    app.close()


def test_work_failure_preserves_feedback_origin():
    assert Application._feedback_origin_pass_id(
        {
            "trigger_type": "work_failure",
            "trigger_json": {"origin_feedback_pass_id": 37},
        }
    ) == 37


def test_concurrent_disposable_work_preserves_disjoint_deltas_and_rejects_stale_overlap(
    tmp_path,
):
    first_key = "first-delta"
    disjoint_key = "disjoint-delta"
    stale_key = "stale-overlap"

    class ConcurrentDeltaRuntime(ScriptedRuntime):
        def __init__(self):
            super().__init__()
            self.snapshot_barrier = threading.Barrier(3)
            self.all_started = threading.Event()
            self.release_disjoint = threading.Event()
            self.release_stale = threading.Event()
            self.workspaces: dict[str, Path] = {}
            self.initial_shared_contents: dict[str, str] = {}

        def run(self, task: str, workspace: str | Path, **kwargs) -> dict:
            payload = json.loads(task)
            if payload["kind"] != "work":
                return super().run(task, workspace, **kwargs)
            key = payload["context"]["work_item"]["key"]
            agent_workspace = Path(workspace)
            self.workspaces[key] = agent_workspace
            self.initial_shared_contents[key] = (
                agent_workspace / "shared.txt"
            ).read_text()
            barrier_index = self.snapshot_barrier.wait(timeout=5)
            if barrier_index == 0:
                self.all_started.set()
            cache = agent_workspace / "repogents" / "__pycache__"
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "module.cpython-310.pyc").write_bytes(key.encode())
            if key == first_key:
                (agent_workspace / "shared.txt").write_text("first wins\n")
                (agent_workspace / "first.txt").write_text("first delta\n")
            elif key == disjoint_key:
                self.release_disjoint.wait(timeout=5)
                (agent_workspace / "disjoint.txt").write_text(
                    "disjoint delta\n"
                )
            elif key == stale_key:
                self.release_stale.wait(timeout=5)
                (agent_workspace / "shared.txt").write_text(
                    "stale overwrite\n"
                )
            return ready_result(f"completed {key}")

    classifications = {
        first_key: "change/first-area",
        disjoint_key: "change/disjoint-area",
        stale_key: "change/stale-area",
    }
    vectors = {
        classifications[first_key]: [1.0, 0.0],
        classifications[disjoint_key]: [0.0, 1.0],
        classifications[stale_key]: [-1.0, 0.0],
    }
    runtime = ConcurrentDeltaRuntime()
    runtime.queue(
        "specify",
        package(
            (first_key, classifications[first_key]),
            (disjoint_key, classifications[disjoint_key]),
            (stale_key, classifications[stale_key]),
            prefix="concurrent-delta",
        ),
    )
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors=vectors,
        max_workers=4,
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    for key, classification in classifications.items():
        store.create_dynamic_node(
            repository["id"],
            classification,
            vectors[classification],
            f"Own {key} work.",
        )
    github.issues = [
        GitHubIssue(
            7,
            "Concurrent deltas",
            "Apply disjoint work without stale overwrite.",
            "https://issue/7",
        )
    ]

    app.poll_once()
    run = store.list_runs(repository["id"])[0]
    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "shared.txt").write_text("base\n")
    app.poll_once()
    assert runtime.all_started.wait(timeout=5)

    def work_state(key: str) -> str | None:
        return next(
            (
                item["state"]
                for item in store.list_work_items(run["id"])
                if item["key"] == key
            ),
            None,
        )

    drive_until(app, lambda: work_state(first_key) == "COMPLETED")
    assert (workspace / "shared.txt").read_text() == "first wins\n"
    assert (workspace / "first.txt").read_text() == "first delta\n"

    runtime.release_disjoint.set()
    drive_until(
        app,
        lambda: work_state(disjoint_key) in {"COMPLETED", "FAILED"},
    )
    assert work_state(disjoint_key) == "COMPLETED"
    assert (workspace / "shared.txt").read_text() == "first wins\n"
    assert (workspace / "first.txt").read_text() == "first delta\n"
    assert (
        workspace / "disjoint.txt"
    ).read_text() == "disjoint delta\n"
    assert not (workspace / "repogents" / "__pycache__").exists()

    runtime.release_stale.set()
    drive_until(
        app,
        lambda: work_state(stale_key) in {"COMPLETED", "FAILED"},
    )

    assert work_state(stale_key) == "FAILED"
    assert (workspace / "shared.txt").read_text() == "first wins\n"
    assert (workspace / "first.txt").read_text() == "first delta\n"
    assert (
        workspace / "disjoint.txt"
    ).read_text() == "disjoint delta\n"
    assert set(runtime.initial_shared_contents.values()) == {"base\n"}
    assert len(set(runtime.workspaces.values())) == 3
    assert store.list_validations(run["id"]) == []
    app.close()
    assert all(
        agent_workspace != workspace and not agent_workspace.exists()
        for agent_workspace in runtime.workspaces.values()
    )


def test_successful_work_replaces_read_only_file_and_preserves_read_only_modes(
    tmp_path,
):
    classification = "change/read-only-source"
    runtime = WorkspaceScriptedRuntime()
    runtime.queue(
        "specify",
        package(("read-only-delta", classification), prefix="read-only"),
    )
    runtime.queue("work", ready_result("read-only source delta completed"))

    def replace_read_only_source(agent_workspace: Path) -> None:
        read_only_file = agent_workspace / "read-only.txt"
        read_only_file.unlink()
        read_only_file.write_bytes(b"replacement\n")
        read_only_file.chmod(0o440)
        read_only_directory = agent_workspace / "read-only-directory"
        read_only_directory.mkdir()
        (read_only_directory / "payload.txt").write_bytes(b"payload\n")
        read_only_directory.chmod(0o550)

    runtime.queue_workspace_action("work", replace_read_only_source)
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Own read-only source changes.",
    )
    github.issues = [
        GitHubIssue(
            7,
            "Update read-only source",
            "Replace a file and create a directory.",
            "https://issue/7",
        )
    ]

    app.poll_once()
    run = store.list_runs(repository["id"])[0]
    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    read_only_file = workspace / "read-only.txt"
    read_only_file.write_bytes(b"original\n")
    read_only_file.chmod(0o400)

    app.poll_once()
    drive_until(
        app,
        lambda: store.list_work_items(run["id"])[0]["state"]
        in {"COMPLETED", "FAILED"},
    )

    completed_work = store.list_work_items(run["id"])[0]
    read_only_directory = workspace / "read-only-directory"
    assert completed_work["state"] == "COMPLETED"
    assert read_only_file.read_bytes() == b"replacement\n"
    assert stat.S_IMODE(read_only_file.stat().st_mode) == 0o440
    assert (read_only_directory / "payload.txt").read_bytes() == b"payload\n"
    assert stat.S_IMODE(read_only_directory.stat().st_mode) == 0o550
    app.close()


@pytest.mark.parametrize(
    "link_target",
    ["../outside-source.py", ".git/config"],
    ids=["parent-escape", "controller-metadata"],
)
def test_work_delta_with_unsafe_symlink_rejects_the_entire_import(
    tmp_path,
    link_target,
):
    classification = "change/link-boundary"
    runtime = WorkspaceScriptedRuntime()
    runtime.queue(
        "specify",
        package(("unsafe-link-delta", classification), prefix="link"),
    )
    runtime.queue("work", ready_result("source and link updated"))

    def edit_source_and_create_link(agent_workspace: Path) -> None:
        (agent_workspace / "source.py").write_bytes(b"untrusted edit\n")
        (agent_workspace / "untrusted-link").symlink_to(link_target)

    runtime.queue_workspace_action("work", edit_source_and_create_link)
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Own bounded source changes.",
    )
    github.issues = [
        GitHubIssue(
            7,
            "Reject unsafe link",
            "Edit source without crossing controller boundaries.",
            "https://issue/7",
        )
    ]

    app.poll_once()
    run = store.list_runs(repository["id"])[0]
    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "source.py").write_bytes(b"original\n")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_bytes(b"controller config\n")

    app.poll_once()
    drive_until(
        app,
        lambda: store.list_work_items(run["id"])[0]["state"]
        in {"COMPLETED", "FAILED"},
    )

    failed_work = store.list_work_items(run["id"])[0]
    assert failed_work["state"] == "FAILED"
    assert (workspace / "source.py").read_bytes() == b"original\n"
    with pytest.raises(FileNotFoundError):
        (workspace / "untrusted-link").lstat()
    assert (workspace / ".git" / "config").read_bytes() == b"controller config\n"
    assert store.list_validations(run["id"]) == []
    app.close()


def test_complete_work_failure_rolls_back_the_entire_imported_source_delta(
    tmp_path,
    monkeypatch,
):
    classification = "change/persistence-boundary"
    runtime = WorkspaceScriptedRuntime()
    runtime.queue(
        "specify",
        package(("persistence-delta", classification), prefix="persistence"),
    )
    runtime.queue("work", ready_result("source delta ready to persist"))

    def change_source(agent_workspace: Path) -> None:
        (agent_workspace / "edited.txt").write_bytes(b"edited\n")
        (agent_workspace / "created.txt").write_bytes(b"created\n")
        (agent_workspace / "deleted.txt").unlink()

    runtime.queue_workspace_action("work", change_source)
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Own persisted source changes.",
    )
    github.issues = [
        GitHubIssue(
            7,
            "Persist source result",
            "Apply the source delta atomically.",
            "https://issue/7",
        )
    ]

    app.poll_once()
    run = store.list_runs(repository["id"])[0]
    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "edited.txt").write_bytes(b"original\n")
    (workspace / "deleted.txt").write_bytes(b"restore me\n")
    completion_views = []

    def fail_completion(work_id, result, handoff=None):
        completion_views.append(
            {
                "edited": (workspace / "edited.txt").read_bytes(),
                "created": (workspace / "created.txt").read_bytes(),
                "deleted_exists": (workspace / "deleted.txt").exists(),
            }
        )
        raise RuntimeError("durable work result unavailable")

    monkeypatch.setattr(store, "complete_work", fail_completion)
    app.poll_once()
    drive_until(
        app,
        lambda: store.list_work_items(run["id"])[0]["state"]
        in {"COMPLETED", "FAILED"},
    )

    assert completion_views == [
        {
            "edited": b"edited\n",
            "created": b"created\n",
            "deleted_exists": False,
        }
    ]
    assert store.list_work_items(run["id"])[0]["state"] == "FAILED"
    assert (workspace / "edited.txt").read_bytes() == b"original\n"
    assert (workspace / "deleted.txt").read_bytes() == b"restore me\n"
    assert not (workspace / "created.txt").exists()
    assert store.list_validations(run["id"]) == []
    app.close()


def test_startup_restores_interrupted_import_before_requeue_and_preserves_metadata(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "state.sqlite3"
    data_dir = tmp_path / "runtime"
    store = Store(database)
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {
            "number": 7,
            "title": "Interrupt imported source",
            "body": "Recover the pre-import tree on restart.",
        },
    )
    execution_pass = store.create_pass(run["id"], "activation", {})
    saved = store.save_specification_package(
        run["id"],
        execution_pass["id"],
        package(
            ("interrupted-source-delta", "change/interrupted-source"),
            prefix="interrupt",
        ),
    )
    node = store.create_dynamic_node(
        repository["id"],
        "change/interrupted-source",
        [1.0, 0.0],
        "Own interruption-safe source changes.",
    )
    assigned_work = store.assign_work(saved["work_items"][0]["id"], node["id"])
    store.transition_run(run["id"], "WAITING_FOR_WORK_COMPLETION")

    workspace = (
        data_dir
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    workspace.mkdir(parents=True)
    edited_path = workspace / "edited.txt"
    deleted_path = workspace / "deleted.txt"
    created_path = workspace / "created.txt"
    edited_path.write_bytes(b"original editable source\n")
    edited_path.chmod(0o640)
    deleted_path.write_bytes(b"original deleted source\n")
    deleted_path.chmod(0o640)
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_bytes(b"canonical git metadata\n")
    (workspace / ".repogents").mkdir()
    (workspace / ".repogents" / "controller.json").write_bytes(
        b'{"owner":"controller"}\n'
    )

    def source_state() -> dict:
        def regular_file_state(path: Path):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return None
            return path.read_bytes(), stat.S_IMODE(metadata.st_mode)

        return {
            "edited": regular_file_state(edited_path),
            "deleted": regular_file_state(deleted_path),
            "created": regular_file_state(created_path),
        }

    pre_import_source = source_state()
    fork_context = multiprocessing.get_context("fork")
    completion_reached = fork_context.Event()
    keep_child_at_completion = fork_context.Event()

    def run_until_work_completion() -> None:
        child_store = Store(database)
        child_runtime = WorkspaceScriptedRuntime()
        child_runtime.queue(
            "work",
            ready_result("interrupted source delta completed"),
        )

        def import_changed_source(agent_workspace: Path) -> None:
            child_edited = agent_workspace / "edited.txt"
            child_edited.unlink()
            child_edited.write_bytes(b"imported editable source\n")
            child_edited.chmod(0o600)
            (agent_workspace / "deleted.txt").unlink()
            (agent_workspace / "created.txt").write_bytes(
                b"imported created source\n"
            )
            (agent_workspace / "created.txt").chmod(0o640)

        child_runtime.queue_workspace_action("work", import_changed_source)
        child_app = Application(
            child_store,
            FakeGitHub(),
            child_runtime,
            SemanticRouter(MapEmbedder({})),
            ApplicationConfig(data_dir=data_dir),
        )
        claimed_work = child_store.claim_node_work(node["id"], run["id"])
        if claimed_work is None:
            raise RuntimeError("seeded work was not claimable")

        def stop_at_complete_work(work_id, result, handoff=None):
            completion_reached.set()
            keep_child_at_completion.wait()

        child_store.complete_work = stop_at_complete_work
        child_app._run_work(node, claimed_work)

    child = fork_context.Process(target=run_until_work_completion)
    child.start()
    try:
        assert completion_reached.wait(timeout=5)
        assert source_state() == {
            "edited": (b"imported editable source\n", 0o600),
            "deleted": None,
            "created": (b"imported created source\n", 0o640),
        }
        assert store.list_work_items(run["id"])[0]["state"] == "RUNNING"
    finally:
        if child.is_alive():
            child.terminate()
        child.join(timeout=5)
        if child.is_alive():
            child.kill()
            child.join(timeout=5)
    assert not child.is_alive()
    assert store.list_work_items(run["id"])[0]["state"] == "RUNNING"

    restarted_store = Store(database)
    original_recover_interrupted_work = (
        restarted_store.recover_interrupted_work
    )
    before_requeue = []

    def observe_source_before_requeue():
        before_requeue.append(
            {
                "source": source_state(),
                "work_state": restarted_store.list_work_items(run["id"])[0][
                    "state"
                ],
            }
        )
        return original_recover_interrupted_work()

    monkeypatch.setattr(
        restarted_store,
        "recover_interrupted_work",
        observe_source_before_requeue,
    )
    restarted = Application(
        restarted_store,
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=data_dir),
    )
    try:
        assert before_requeue == [
            {
                "source": pre_import_source,
                "work_state": "RUNNING",
            }
        ]
        assert source_state() == pre_import_source
        assert restarted_store.list_work_items(run["id"])[0]["state"] == "QUEUED"
        assert (
            workspace / ".git" / "config"
        ).read_bytes() == b"canonical git metadata\n"
        assert (
            workspace / ".repogents" / "controller.json"
        ).read_bytes() == b'{"owner":"controller"}\n'
        assert assigned_work["id"] == restarted_store.list_work_items(
            run["id"]
        )[0]["id"]
    finally:
        restarted.close()


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        ("ready_for_validation", "COMPLETED"),
        ("continue_work", "HANDED_OFF"),
    ],
    ids=["completed", "handed-off"],
)
def test_post_commit_completion_exception_preserves_imported_source_and_node_success(
    tmp_path,
    monkeypatch,
    outcome,
    expected_state,
):
    database = tmp_path / "state.sqlite3"
    data_dir = tmp_path / "runtime"
    store = Store(database)
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {
            "number": 7,
            "title": "Retain committed source",
            "body": "Treat a committed work result as durable success.",
        },
    )
    execution_pass = store.create_pass(run["id"], "activation", {})
    classification = "change/post-commit-source"
    saved = store.save_specification_package(
        run["id"],
        execution_pass["id"],
        package(
            ("post-commit-source", classification),
            prefix="post-commit",
        ),
    )
    node = store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Own transaction-safe source changes.",
    )
    store.assign_work(saved["work_items"][0]["id"], node["id"])
    store.transition_run(run["id"], "WAITING_FOR_WORK_COMPLETION")

    workspace = (
        data_dir
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    workspace.mkdir(parents=True)
    edited_path = workspace / "edited.bin"
    created_path = workspace / "created.bin"
    edited_path.write_bytes(b"original source\n")
    edited_path.chmod(0o640)

    runtime = WorkspaceScriptedRuntime()
    result = ready_result("durable source result")
    result["outcome"] = outcome
    if outcome == "continue_work":
        result.update(
            {
                "classification": "verify/post-commit-source",
                "context": {"reason": "verify imported source"},
                "dependencies": [],
                "dependency_evidence": [],
                "blocking": None,
            }
        )
    runtime.queue("work", result)

    def import_source(agent_workspace: Path) -> None:
        agent_edited = agent_workspace / "edited.bin"
        agent_edited.unlink()
        agent_edited.write_bytes(b"\x00imported source\n")
        agent_edited.chmod(0o600)
        agent_created = agent_workspace / "created.bin"
        agent_created.write_bytes(b"\xffcreated source\n")
        agent_created.chmod(0o440)

    runtime.queue_workspace_action("work", import_source)
    app = Application(
        store,
        FakeGitHub(),
        runtime,
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(
            data_dir=data_dir,
            promotion_threshold=1,
        ),
    )
    claimed_work = store.claim_node_work(node["id"], run["id"])
    assert claimed_work is not None
    real_complete_work = store.complete_work

    def commit_work_then_raise(work_id, persisted_result, handoff=None):
        real_complete_work(work_id, persisted_result, handoff)
        raise RuntimeError("completion response lost after commit")

    monkeypatch.setattr(store, "complete_work", commit_work_then_raise)

    app._run_work(node, claimed_work)

    work_items = store.list_work_items(run["id"])
    completed_work = next(
        item for item in work_items if item["id"] == claimed_work["id"]
    )
    assert completed_work["state"] == expected_state
    assert completed_work["result"]["output"] == "durable source result"
    children = [
        item
        for item in work_items
        if item["parent_work_id"] == claimed_work["id"]
    ]
    assert len(children) == (1 if outcome == "continue_work" else 0)
    if children:
        assert children[0]["state"] == "UNASSIGNED"
        assert children[0]["classification"] == "verify/post-commit-source"
    assert edited_path.read_bytes() == b"\x00imported source\n"
    assert stat.S_IMODE(edited_path.stat().st_mode) == 0o600
    assert created_path.read_bytes() == b"\xffcreated source\n"
    assert stat.S_IMODE(created_path.stat().st_mode) == 0o440
    persisted_node = next(
        item
        for item in store.list_dynamic_nodes(repository["id"])
        if item["id"] == node["id"]
    )
    assert persisted_node["success_count"] == 1
    assert persisted_node["persistence"] == "PERSISTENT"
    app.close()


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        ("ready_for_validation", "COMPLETED"),
        ("continue_work", "HANDED_OFF"),
    ],
    ids=["completed", "handed-off"],
)
def test_startup_preserves_post_commit_import_and_clears_journal_before_work_recovery(
    tmp_path,
    monkeypatch,
    outcome,
    expected_state,
):
    database = tmp_path / "state.sqlite3"
    data_dir = tmp_path / "runtime"
    store = Store(database)
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {
            "number": 7,
            "title": "Recover committed source",
            "body": "Keep source whose successful work result is durable.",
        },
    )
    execution_pass = store.create_pass(run["id"], "activation", {})
    classification = "change/fork-committed-source"
    saved = store.save_specification_package(
        run["id"],
        execution_pass["id"],
        package(
            ("fork-committed-source", classification),
            prefix="fork-commit",
        ),
    )
    node = store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Own interruption-safe committed source.",
    )
    assigned_work = store.assign_work(
        saved["work_items"][0]["id"],
        node["id"],
    )
    store.transition_run(run["id"], "WAITING_FOR_WORK_COMPLETION")

    workspace = (
        data_dir
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    workspace.mkdir(parents=True)
    edited_path = workspace / "edited.bin"
    created_path = workspace / "created.bin"
    edited_path.write_bytes(b"original source\n")
    edited_path.chmod(0o640)
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_bytes(
        b"canonical git metadata\n"
    )
    (workspace / ".repogents").mkdir()
    (workspace / ".repogents" / "controller.json").write_bytes(
        b'{"owner":"controller"}\n'
    )
    journal_path = (
        data_dir
        / "source-import-journals"
        / f"work-{assigned_work['id']}"
    )

    def source_state() -> dict:
        def regular_file_state(path: Path):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return None
            return path.read_bytes(), stat.S_IMODE(metadata.st_mode)

        return {
            "edited": regular_file_state(edited_path),
            "created": regular_file_state(created_path),
        }

    imported_source = {
        "edited": (b"\x00committed source\n", 0o600),
        "created": (b"\xffcommitted creation\n", 0o440),
    }
    fork_context = multiprocessing.get_context("fork")
    completion_committed = fork_context.Event()
    keep_child_after_commit = fork_context.Event()

    def run_until_post_commit_cleanup() -> None:
        child_store = Store(database)
        child_runtime = WorkspaceScriptedRuntime()
        result = ready_result("committed before interruption")
        result["outcome"] = outcome
        if outcome == "continue_work":
            result.update(
                {
                    "classification": "verify/fork-committed-source",
                    "context": {"reason": "verify committed source"},
                    "dependencies": [],
                    "dependency_evidence": [],
                    "blocking": None,
                }
            )
        child_runtime.queue("work", result)

        def import_source(agent_workspace: Path) -> None:
            child_edited = agent_workspace / "edited.bin"
            child_edited.unlink()
            child_edited.write_bytes(b"\x00committed source\n")
            child_edited.chmod(0o600)
            child_created = agent_workspace / "created.bin"
            child_created.write_bytes(b"\xffcommitted creation\n")
            child_created.chmod(0o440)

        child_runtime.queue_workspace_action("work", import_source)
        child_app = Application(
            child_store,
            FakeGitHub(),
            child_runtime,
            SemanticRouter(MapEmbedder({})),
            ApplicationConfig(data_dir=data_dir),
        )
        claimed_work = child_store.claim_node_work(
            node["id"],
            run["id"],
        )
        if claimed_work is None:
            raise RuntimeError("seeded work was not claimable")
        real_complete_work = child_store.complete_work

        def commit_work_then_block(work_id, persisted_result, handoff=None):
            child = real_complete_work(work_id, persisted_result, handoff)
            completion_committed.set()
            keep_child_after_commit.wait()
            return child

        child_store.complete_work = commit_work_then_block
        child_app._run_work(node, claimed_work)

    child = fork_context.Process(target=run_until_post_commit_cleanup)
    child.start()
    try:
        assert completion_committed.wait(timeout=5)
        assert source_state() == imported_source
        assert journal_path.is_dir()
        committed_work = next(
            item
            for item in store.list_work_items(run["id"])
            if item["id"] == assigned_work["id"]
        )
        assert committed_work["state"] == expected_state
    finally:
        if child.is_alive():
            child.terminate()
        child.join(timeout=5)
        if child.is_alive():
            child.kill()
            child.join(timeout=5)
    assert not child.is_alive()
    assert journal_path.is_dir()

    restarted_store = Store(database)
    real_recover_interrupted_work = (
        restarted_store.recover_interrupted_work
    )
    recovery_views = []

    def observe_recovery_order():
        work = next(
            item
            for item in restarted_store.list_work_items(run["id"])
            if item["id"] == assigned_work["id"]
        )
        recovery_views.append(
            {
                "source": source_state(),
                "work_state": work["state"],
                "journal_exists": journal_path.exists(),
                "git_metadata": (
                    workspace / ".git" / "config"
                ).read_bytes(),
                "controller_metadata": (
                    workspace / ".repogents" / "controller.json"
                ).read_bytes(),
            }
        )
        return real_recover_interrupted_work()

    monkeypatch.setattr(
        restarted_store,
        "recover_interrupted_work",
        observe_recovery_order,
    )
    restarted = Application(
        restarted_store,
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=data_dir),
    )
    try:
        assert recovery_views == [
            {
                "source": imported_source,
                "work_state": expected_state,
                "journal_exists": False,
                "git_metadata": b"canonical git metadata\n",
                "controller_metadata": b'{"owner":"controller"}\n',
            }
        ]
        assert source_state() == imported_source
        assert not journal_path.exists()
        recovered_work_items = restarted_store.list_work_items(run["id"])
        recovered_work = next(
            item
            for item in recovered_work_items
            if item["id"] == assigned_work["id"]
        )
        assert recovered_work["state"] == expected_state
        recovered_children = [
            item
            for item in recovered_work_items
            if item["parent_work_id"] == assigned_work["id"]
        ]
        assert len(recovered_children) == (
            1 if outcome == "continue_work" else 0
        )
        if recovered_children:
            assert recovered_children[0]["state"] == "UNASSIGNED"
            assert (
                recovered_children[0]["classification"]
                == "verify/fork-committed-source"
            )
    finally:
        restarted.close()



def test_validation_failure_feedback_reentry_same_pr_update_and_merge_completion(tmp_path):
    runtime = MutatingValidationRuntime()
    runtime.queue(
        "specify",
        package(("initial", "backend/api"), prefix="initial"),
        package(("validation-fix", "backend/api"), prefix="fix"),
        feedback_specify(
            [
                feedback_disposition(
                    "inline:44",
                    valid=True,
                    in_scope=True,
                    specification_keys=["feedback-1"],
                )
            ],
            ("feedback-fix", "backend/api"),
            prefix="feedback",
        ),
    )
    runtime.queue("node_role", {"role_prompt": "Own backend API outcomes."})
    runtime.queue(
        "work",
        ready_result("initial implementation"),
        ready_result("validation correction"),
        ready_result("feedback correction"),
    )
    runtime.queue(
        "validate",
        validation(False, "criterion missing"),
        validation(True, "corrected"),
        validation(True, "feedback addressed"),
    )
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"backend/api": [1.0, 0.0]},
        promotion_threshold=3,
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [GitHubIssue(7, "API", "Build endpoint", "https://issue/7")]

    drive_until(app, lambda: store.list_runs(repository["id"])[0]["state"] == "PR_LISTENING")
    run = store.list_runs(repository["id"])[0]
    assert len(store.list_passes(run["id"])) == 2
    assert len(store.list_validations(run["id"])) == 2
    assert github.publish_existing == [None]
    second_specify = [call for call in runtime.calls if call["payload"]["kind"] == "specify"][1]
    assert second_specify["payload"]["context"]["relevant_validation"]["result"]["passed"] is False
    assert second_specify["payload"]["context"]["existing_work"]

    github.feedback = [GitHubFeedback("inline:44", "inline", "Please change this", "api.py", 12)]
    drive_until(app, lambda: len(github.publish_existing) == 2)
    assert github.publish_existing == [None, github.pull.number]
    feedback = store.list_feedback(run["id"])
    assert len(feedback) == 1
    assert feedback[0]["package"]["path"] == "api.py"
    assert feedback[0]["package"]["line"] == 12
    assert feedback[0]["package"]["diff"] == github.pull.diff
    third_specify = [call for call in runtime.calls if call["payload"]["kind"] == "specify"][2]
    feedback_context = third_specify["payload"]["context"]
    assert [
        item["external_id"] for item in feedback_context["feedback"]
    ] == ["inline:44"]
    assert "package" not in feedback_context["feedback"][0]
    assert "diff" not in feedback_context["feedback"][0]
    assert feedback_context["pull_request_diff"] == github.pull.diff
    assert feedback_context["relevant_validation"]
    validation_calls = [
        call for call in runtime.calls if call["payload"]["kind"] == "validate"
    ]
    assert validation_calls
    assert all(
        "Do not modify repository files or implement corrections."
        in call["payload"]["instruction"]
        for call in validation_calls
    )
    real_workspace = tmp_path / "runtime" / "workspaces" / str(repository["id"]) / str(run["id"])
    assert not (real_workspace / "validator-write.txt").exists()
    assert len(runtime.validation_workspaces) == len(validation_calls)
    assert all(workspace != real_workspace for workspace in runtime.validation_workspaces)
    assert all(not workspace.exists() for workspace in runtime.validation_workspaces)

    github.pull = PullRequest(
        number=17,
        url=github.pull.url,
        branch=github.pull.branch,
        state="closed",
        merged=True,
        diff=github.pull.diff,
    )
    drive_until(app, lambda: store.get_run(run["id"])["state"] == "COMPLETED")
    dynamic = store.list_dynamic_nodes(repository["id"])
    assert len(dynamic) == 1
    assert dynamic[0]["persistence"] == "PERSISTENT"
    assert not hasattr(app, "merge")
    app.close()


def test_feedback_validation_failure_then_two_successful_same_pr_passes(tmp_path):
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        package(("initial", "backend/api"), prefix="initial"),
        feedback_specify(
            [
                feedback_disposition(
                    "inline:44",
                    valid=True,
                    in_scope=True,
                    specification_keys=["first-feedback-1"],
                )
            ],
            ("first-feedback", "backend/api"),
            prefix="first-feedback",
        ),
        package(("validation-fix", "backend/api"), prefix="validation-fix"),
        feedback_specify(
            [
                feedback_disposition(
                    "review:91",
                    valid=True,
                    in_scope=True,
                    specification_keys=["later-feedback-1"],
                )
            ],
            ("later-feedback", "backend/api"),
            prefix="later-feedback",
        ),
    )
    runtime.queue("node_role", {"role_prompt": "Own backend API outcomes."})
    runtime.queue(
        "work",
        ready_result("initial implementation"),
        ready_result("first feedback correction"),
        ready_result("validation correction"),
        ready_result("later feedback correction"),
    )
    runtime.queue(
        "validate",
        validation(True, "initial implementation is valid"),
        validation(False, "first feedback correction is incomplete"),
        validation(True, "first feedback is now addressed"),
        validation(True, "later feedback is addressed"),
    )
    app, store, github, _ = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"backend/api": [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.publish_head_shas.extend(
        [
            "initial-head-sha",
            "first-feedback-head-sha",
            "later-feedback-head-sha",
        ]
    )
    github.issues = [GitHubIssue(7, "API", "Build endpoint", "https://issue/7")]

    drive_until(
        app,
        lambda: store.list_runs(repository["id"])[0]["state"] == "PR_LISTENING",
    )
    run = store.list_runs(repository["id"])[0]
    assert len(store.list_passes(run["id"])) == 1
    assert github.publish_existing == [None]
    assert github.effect_calls == [("publish", None, "initial-head-sha")]
    assert store.get_run(run["id"])["pull_request"]["head_sha"] == "initial-head-sha"
    assert (
        store.get_run(run["id"])["pull_request"]["validated_head_sha"]
        == "initial-head-sha"
    )

    first_feedback = GitHubFeedback(
        external_id="inline:44",
        kind="inline",
        body="Please change this",
        path="api.py",
        line=12,
        review_thread_id="PRRT_first",
        top_level_comment_id=44,
    )
    github.feedback = [first_feedback]
    drive_until(
        app,
        lambda: (
            len(store.list_validations(run["id"])) == 2
            and store.get_run(run["id"])["state"] == "SPECIFYING"
        ),
    )

    assert github.publish_existing == [None]
    assert github.address_calls == []
    assert github.effect_calls == [("publish", None, "initial-head-sha")]
    pending = store.list_feedback(run["id"])
    assert len(pending) == 1
    assert pending[0]["status"] == "PENDING"
    assert pending[0]["addressed_sha"] is None
    assert pending[0]["response_url"] is None
    assert [item["trigger_type"] for item in store.list_passes(run["id"])] == [
        "issue",
        "feedback",
        "validation_failure",
    ]

    drive_until(
        app,
        lambda: (
            len(github.address_calls) == 1
            and store.get_run(run["id"])["state"] == "PR_LISTENING"
        ),
    )

    first_listening = store.get_run(run["id"])
    assert first_listening["state"] == "PR_LISTENING"
    assert first_listening["pull_request"]["number"] == github.pull.number
    assert first_listening["pull_request"]["state"] == "open"
    assert first_listening["pull_request"]["head_sha"] == "first-feedback-head-sha"
    assert github.publish_existing == [None, github.pull.number]
    assert github.effect_calls == [
        ("publish", None, "initial-head-sha"),
        ("publish", github.pull.number, "first-feedback-head-sha"),
        (
            "address_feedback",
            github.pull.number,
            "inline:44",
            "first-feedback-head-sha",
        ),
    ]
    first_row = store.list_feedback(run["id"])[0]
    assert first_row["status"] == "RESOLVED"
    assert first_row["addressed_sha"] == "first-feedback-head-sha"
    assert (
        first_row["response_url"]
        == f"{github.pull.url}#response-inline:44"
    )

    later_feedback = GitHubFeedback(
        external_id="review:91",
        kind="review",
        body="Request changes",
        review_thread_id=None,
    )
    github.feedback = [first_feedback, later_feedback]
    drive_until(
        app,
        lambda: (
            len(github.address_calls) == 2
            and store.get_run(run["id"])["state"] == "PR_LISTENING"
        ),
    )

    final_run = store.get_run(run["id"])
    assert final_run["state"] == "PR_LISTENING"
    assert final_run["pull_request"]["number"] == github.pull.number
    assert final_run["pull_request"]["branch"] == "agent/issue-7"
    assert final_run["pull_request"]["state"] == "open"
    assert final_run["pull_request"]["merged"] is False
    assert final_run["pull_request"]["head_sha"] == "later-feedback-head-sha"
    assert github.publish_existing == [None, github.pull.number, github.pull.number]
    assert github.effect_calls == [
        ("publish", None, "initial-head-sha"),
        ("publish", github.pull.number, "first-feedback-head-sha"),
        (
            "address_feedback",
            github.pull.number,
            "inline:44",
            "first-feedback-head-sha",
        ),
        ("publish", github.pull.number, "later-feedback-head-sha"),
        (
            "address_feedback",
            github.pull.number,
            "review:91",
            "later-feedback-head-sha",
        ),
    ]
    assert [
        (
            github_repository,
            pull_number,
            feedback.external_id,
            head_sha,
        )
        for github_repository, pull_number, feedback, head_sha in github.address_calls
    ] == [
        ("acme/widget", github.pull.number, "inline:44", "first-feedback-head-sha"),
        ("acme/widget", github.pull.number, "review:91", "later-feedback-head-sha"),
    ]
    feedback_rows = {
        item["external_id"]: item for item in store.list_feedback(run["id"])
    }
    assert feedback_rows["inline:44"]["status"] == "RESOLVED"
    assert feedback_rows["inline:44"]["addressed_sha"] == "first-feedback-head-sha"
    assert feedback_rows["review:91"]["status"] == "ACKNOWLEDGED"
    assert feedback_rows["review:91"]["addressed_sha"] == "later-feedback-head-sha"
    assert (
        feedback_rows["review:91"]["response_url"]
        == f"{github.pull.url}#response-review:91"
    )
    assert [item["trigger_type"] for item in store.list_passes(run["id"])] == [
        "issue",
        "feedback",
        "validation_failure",
        "feedback",
    ]
    specify_calls = [
        call for call in runtime.calls if call["payload"]["kind"] == "specify"
    ]
    assert len(specify_calls) == 4
    first_feedback_context = specify_calls[1]["payload"]["context"]
    later_feedback_context = specify_calls[3]["payload"]["context"]
    assert [
        item["external_id"] for item in first_feedback_context["feedback"]
    ] == ["inline:44"]
    assert [
        item["external_id"] for item in later_feedback_context["feedback"]
    ] == ["review:91"]
    for context in (first_feedback_context, later_feedback_context):
        assert context["pull_request_diff"] == github.pull.diff
        assert context["original_issue"]["number"] == 7
        assert context["existing_specifications"]
        assert context["existing_work"]
        assert context["relevant_validation"]
        assert all("package" not in item and "diff" not in item for item in context["feedback"])
    work_calls = [
        call for call in runtime.calls if call["payload"]["kind"] == "work"
    ]
    assert len(work_calls) == 4
    first_feedback_work = work_calls[1]["payload"]["context"]
    later_feedback_work = work_calls[3]["payload"]["context"]
    assert [
        item["external_id"] for item in first_feedback_work["feedback"]
    ] == ["inline:44"]
    assert [
        item["external_id"] for item in later_feedback_work["feedback"]
    ] == ["review:91"]
    for context in (first_feedback_work, later_feedback_work):
        assert context["pull_request_diff"] == github.pull.diff
        assert context["original_issue"]["number"] == 7
        assert context["specification"]
        assert context["specification_dependencies"] == []
        assert "prior_specifications" not in context
        assert "prior_work" not in context
        assert all("package" not in item and "diff" not in item for item in context["feedback"])
    validation_calls = [
        call for call in runtime.calls if call["payload"]["kind"] == "validate"
    ]
    assert len(validation_calls) == 4
    first_feedback_validation = validation_calls[1]["payload"]["context"]
    later_feedback_validation = validation_calls[3]["payload"]["context"]
    assert [
        item["external_id"] for item in first_feedback_validation["feedback"]
    ] == ["inline:44"]
    assert [
        item["external_id"] for item in later_feedback_validation["feedback"]
    ] == ["review:91"]
    for context in (first_feedback_validation, later_feedback_validation):
        assert "pull_request_diff" not in context
        assert context["candidate_diff"] == github.candidate_diff_text
        assert context["original_issue"]["number"] == 7
        assert context["specifications"]
        assert context["work_items"]
        assert context["latest_prior_validation"]
        assert all("package" not in item and "diff" not in item for item in context["feedback"])
    app.close()


def test_feedback_publication_addresses_only_feedback_claimed_by_feedback_pass(
    tmp_path,
):
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        package(("initial", "backend/api"), prefix="initial"),
        feedback_specify(
            [
                feedback_disposition(
                    "inline:44",
                    valid=True,
                    in_scope=True,
                    specification_keys=["claimed-feedback-1"],
                )
            ],
            ("claimed-feedback", "backend/api"),
            prefix="claimed-feedback",
        ),
        package(("validation-fix", "backend/api"), prefix="validation-fix"),
    )
    runtime.queue("node_role", {"role_prompt": "Own backend API outcomes."})
    runtime.queue(
        "work",
        ready_result("initial implementation"),
        ready_result("claimed feedback correction"),
        ready_result("validation correction"),
    )
    runtime.queue(
        "validate",
        validation(True, "initial implementation is valid"),
        validation(False, "claimed feedback correction is incomplete"),
        validation(True, "claimed feedback is now addressed"),
    )
    app, store, github, _ = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"backend/api": [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.publish_head_shas.extend(
        ["initial-head-sha", "claimed-feedback-head-sha"]
    )
    github.issues = [GitHubIssue(7, "API", "Build endpoint", "https://issue/7")]

    drive_until(
        app,
        lambda: store.list_runs(repository["id"])[0]["state"] == "PR_LISTENING",
    )
    run = store.list_runs(repository["id"])[0]
    claimed_feedback = GitHubFeedback(
        external_id="inline:44",
        kind="inline",
        body="Please change this",
        path="api.py",
        line=12,
        review_thread_id="PRRT_claimed",
        top_level_comment_id=44,
    )
    github.feedback = [claimed_feedback]
    drive_until(
        app,
        lambda: (
            len(store.list_validations(run["id"])) == 2
            and store.get_run(run["id"])["state"] == "SPECIFYING"
        ),
    )

    claimed_row = store.list_feedback(run["id"])[0]
    unclaimed_package = dict(claimed_row["package"])
    unclaimed_package.update(
        {
            "external_id": "review:unclaimed",
            "kind": "review",
            "body": "This feedback was not claimed by the execution pass",
            "path": None,
            "line": None,
            "review_thread_id": None,
            "top_level_comment_id": None,
        }
    )
    assert store.add_feedback(
        run["id"], "review:unclaimed", unclaimed_package
    ) is True
    feedback_passes = [
        execution_pass
        for execution_pass in store.list_passes(run["id"])
        if execution_pass["trigger_type"] == "feedback"
    ]
    assert [
        [
            feedback["external_id"]
            for feedback in execution_pass["trigger_json"]["feedback"]
        ]
        for execution_pass in feedback_passes
    ] == [["inline:44"]]

    drive_until(
        app,
        lambda: (
            store.get_run(run["id"])["state"] == "PR_LISTENING"
            and store.get_run(run["id"])["pull_request"]["head_sha"]
            == "claimed-feedback-head-sha"
        ),
    )

    assert [
        (
            github_repository,
            pull_number,
            feedback.external_id,
            head_sha,
        )
        for github_repository, pull_number, feedback, head_sha in github.address_calls
    ] == [
        (
            "acme/widget",
            github.pull.number,
            "inline:44",
            "claimed-feedback-head-sha",
        )
    ]
    feedback_rows = {
        item["external_id"]: item for item in store.list_feedback(run["id"])
    }
    assert feedback_rows["inline:44"]["status"] == "RESOLVED"
    assert (
        feedback_rows["inline:44"]["addressed_sha"]
        == "claimed-feedback-head-sha"
    )
    assert (
        feedback_rows["inline:44"]["response_url"]
        == f"{github.pull.url}#response-inline:44"
    )
    assert feedback_rows["review:unclaimed"]["status"] == "PENDING"
    assert feedback_rows["review:unclaimed"]["addressed_sha"] is None
    assert feedback_rows["review:unclaimed"]["response_url"] is None
    app.close()


def test_closed_unmerged_pull_request_closes_run(tmp_path):
    app, store, github, _ = make_app(tmp_path)
    repository = app.add_repository("acme/widget")
    run, _ = store.create_run(
        repository["id"],
        7,
        {"number": 7, "title": "Closed", "body": "No merge"},
    )
    store.transition_run(
        run["id"],
        "PR_LISTENING",
        branch="agent/issue-7",
        pull_request={
            "number": 17,
            "url": github.pull.url,
            "branch": "agent/issue-7",
            "state": "open",
            "merged": False,
            "diff": github.pull.diff,
        },
    )
    github.pull = PullRequest(
        number=17,
        url=github.pull.url,
        branch="agent/issue-7",
        state="closed",
        merged=False,
        diff=github.pull.diff,
    )

    app.poll_once()

    assert store.get_run(run["id"])["state"] == "CLOSED"
    app.close()


def test_pending_merge_completes_only_after_github_reports_user_merge(tmp_path):
    app, store, github, _ = make_app(tmp_path)
    repository = app.add_repository("acme/widget")
    run = seed_listening_run(
        store, repository, 7, github.pull, pr_listening_since=0.0
    )
    store.transition_run(run["id"], "PENDING_MERGE")
    github.pull = PullRequest(
        number=github.pull.number,
        url=github.pull.url,
        branch=github.pull.branch,
        state="closed",
        merged=True,
        diff=github.pull.diff,
        head_sha=github.pull.head_sha,
    )

    app.poll_once()

    assert store.get_run(run["id"])["state"] == "COMPLETED"
    assert github.publish_validated_to_target_calls == []
    app.close()


def test_pending_merge_returns_to_work_when_new_feedback_arrives(tmp_path):
    app, store, github, _ = make_app(tmp_path)
    repository = app.add_repository("acme/widget")
    run = seed_listening_run(
        store, repository, 7, github.pull, pr_listening_since=0.0
    )
    store.transition_run(run["id"], "PENDING_MERGE")
    github.feedback = [
        GitHubFeedback("review:pending", "review", "Address this before merge")
    ]

    app.poll_once()

    assert store.get_run(run["id"])["state"] == "SPECIFYING"
    assert store.list_passes(run["id"])[-1]["trigger_type"] == "feedback"
    assert github.publish_validated_to_target_calls == []
    app.close()


def test_continue_work_handoff_reclassifies_through_another_dynamic_node(tmp_path):
    runtime = ScriptedRuntime()
    runtime.queue("specify", package(("handoff-start", "backend/api")))
    runtime.queue(
        "node_role",
        {"role_prompt": "Own backend API work."},
        {"role_prompt": "Own integration test work."},
    )
    runtime.queue(
        "work",
        {
            "outcome": "continue_work",
            "output": "API implementation complete",
            "artifacts": ["api.py"],
            "test_results": [],
            "repository_state": {"api.py": "changed"},
            "classification": "testing/integration",
            "context": {"need": "verify the endpoint"},
            "dependencies": ["handoff-start"],
            "dependency_evidence": dependency_evidence("handoff-start"),
            "blocking": None,
        },
        ready_result("integration verification complete"),
    )
    runtime.queue("validate", validation(True, "handoff chain satisfied the issue"))
    app, store, github, _ = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"backend/api": [1.0, 0.0], "testing/integration": [0.0, 1.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [GitHubIssue(7, "API", "Implement and verify", "https://issue/7")]

    drive_until(app, lambda: store.list_runs(repository["id"])[0]["state"] == "PR_LISTENING")
    work_calls = [call for call in runtime.calls if call["payload"]["kind"] == "work"]
    assert work_calls
    assert all(call["result_schema"]["context"] == {} for call in work_calls)

    run = store.list_runs(repository["id"])[0]
    work = store.list_work_items(run["id"])
    assert [(item["state"], item["classification"]) for item in work] == [
        ("HANDED_OFF", "backend/api"),
        ("COMPLETED", "testing/integration"),
    ]
    assert work[1]["parent_work_id"] == work[0]["id"]
    assert work[1]["handoff"]["context"] == {"need": "verify the endpoint"}
    assert {node["classification"] for node in store.list_dynamic_nodes(repository["id"])} == {
        "backend/api",
        "testing/integration",
    }
    assert len(store.list_validations(run["id"])) == 1
    app.close()


def test_startup_recovers_running_work_and_state_exposes_durable_history(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(repository["id"], 7, {"number": 7, "title": "Resume", "body": "work"})
    execution_pass = store.create_pass(run["id"], "issue", {"number": 7})
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], package(("resume-work", "backend/api"))
    )
    node = store.create_dynamic_node(repository["id"], "backend/api", [1.0, 0.0], "API role")
    store.assign_work(saved["work_items"][0]["id"], node["id"])
    claimed = store.claim_node_work(node["id"], run["id"])
    assert claimed["state"] == "RUNNING"

    github = FakeGitHub()
    runtime = ScriptedRuntime()
    app = Application(
        store,
        github,
        runtime,
        SemanticRouter(MapEmbedder({"backend/api": [1.0, 0.0]})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    assert store.list_work_items(run["id"])[0]["state"] == "QUEUED"
    duplicate, created = store.create_run(repository["id"], 7, run["issue_json"])
    assert created is False
    assert duplicate["id"] == run["id"]
    state_run = app.state()["repositories"][0]["runs"][0]
    assert state_run["work_items"][0]["key"] == "resume-work"
    assert state_run["passes"][0]["trigger_type"] == "issue"
    app.close()


@pytest.mark.parametrize("threshold", [-0.01, 1.0])
def test_application_config_rejects_similarity_threshold_outside_routing_domain(
    tmp_path, threshold
):
    with pytest.raises(ValueError, match="default_similarity_threshold"):
        ApplicationConfig(
            data_dir=tmp_path,
            default_similarity_threshold=threshold,
        )


def test_recovery_reuses_saved_current_pass_package_without_respecifying(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"], 7, {"number": 7, "title": "Resume", "body": "work"}
    )
    execution_pass = store.create_pass(run["id"], "issue", run["issue_json"])
    store.save_specification_package(
        run["id"], execution_pass["id"], package(("resume-work", "backend/api"))
    )
    store.transition_run(run["id"], "SPECIFYING")
    (
        tmp_path / "runtime" / "workspaces" / str(repository["id"]) / str(run["id"])
    ).mkdir(parents=True)
    runtime = ScriptedRuntime()
    runtime.queue("node_role", {"role_prompt": "Own API work."})
    runtime.queue("work", ready_result("resumed implementation"))
    runtime.queue("validate", validation(True, "resumed result is valid"))
    github = FakeGitHub()
    app = Application(
        store,
        github,
        runtime,
        SemanticRouter(MapEmbedder({"backend/api": [1.0, 0.0]})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    drive_until(app, lambda: store.get_run(run["id"])["state"] == "PR_LISTENING")

    assert not [call for call in runtime.calls if call["payload"]["kind"] == "specify"]
    assert len(store.list_passes(run["id"])) == 1
    assert len(store.list_specifications(run["id"])) == 1
    app.close()


def test_validation_requires_complete_typed_failure_evidence(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"], 7, {"number": 7, "title": "Validate", "body": "work"}
    )
    execution_pass = store.create_pass(run["id"], "issue", run["issue_json"])
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], package(("validate-work", "backend/api"))
    )
    node = store.create_dynamic_node(
        repository["id"], "backend/api", [1.0, 0.0], "Own API work."
    )
    store.assign_work(saved["work_items"][0]["id"], node["id"])
    claimed = store.claim_node_work(node["id"], run["id"])
    store.complete_work(
        claimed["id"],
        {
            "output": "done",
            "artifacts": [],
            "test_results": [],
            "repository_state": {},
        },
    )
    store.transition_run(run["id"], "VALIDATING")
    (
        tmp_path / "runtime" / "workspaces" / str(repository["id"]) / str(run["id"])
    ).mkdir(parents=True)
    runtime = ScriptedRuntime()
    runtime.queue(
        "validate",
        {
            "passed": False,
            "failed_specifications": ["spec-1"],
            "failed_criteria": ["criterion"],
            "explanation": "missing required durable evidence",
            "evidence": [],
        },
    )
    app = Application(
        store,
        FakeGitHub(),
        runtime,
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    with pytest.raises(ValueError, match="complete validation result"):
        app.poll_once()

    assert store.list_validations(run["id"]) == []
    app.close()


def test_operation_failure_artifact_export_is_atomic_and_retry_persists_one_pass(
    tmp_path,
    monkeypatch,
):
    store = Store(tmp_path / "state.sqlite3")
    data_dir = tmp_path / "runtime"
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {
            "number": 7,
            "title": "Export operation artifacts",
            "body": "Publish an exact atomic operation handoff.",
        },
    )
    failed_pass = store.create_pass(
        run["id"],
        "feedback",
        {"feedback": []},
    )
    store.transition_run(run["id"], "VALIDATING")
    workspace = (
        data_dir
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "conflict.py").write_bytes(
        b"canonical conflicted source\n"
    )
    (workspace / ".git" / "rebase-merge").mkdir(parents=True)

    github = FakeGitHub()
    github.repository_operation_state_result = {
        "rebase_in_progress": True,
        "unmerged_paths": ["src/conflict.py"],
        "staged_paths": [],
        "unstaged_paths": [],
        "untracked_paths": [],
    }
    expected_artifacts = {
        "base": b"exact base\n",
        "ours": b"exact ours\n",
        "theirs": b"exact theirs\n",
    }
    github.repository_operation_artifact_contents = {
        "src/conflict.py": {
            stage: contents.decode()
            for stage, contents in expected_artifacts.items()
        }
    }
    app = Application(
        store,
        github,
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=data_dir),
    )
    command_error = subprocess.CalledProcessError(
        41,
        ["git", "operation-export-sentinel"],
        output="export stdout\n",
        stderr="export stderr\n",
    )
    final_directory = (
        data_dir
        / "operation-artifacts"
        / str(run["id"])
        / f"{failed_pass['id']}-prepare_publication"
    )

    def write_partial_artifact_then_raise(
        operation_workspace,
        destination,
    ):
        destination_path = Path(destination)
        partial = destination_path / "base" / "src" / "conflict.py"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"partial base only\n")
        raise RuntimeError("artifact export interrupted")

    with monkeypatch.context() as export_failure:
        export_failure.setattr(
            github,
            "export_repository_operation_artifacts",
            write_partial_artifact_then_raise,
        )
        with pytest.raises(
            RuntimeError,
            match="artifact export interrupted",
        ):
            app._record_operation_failure(
                repository,
                run,
                failed_pass,
                "prepare_publication",
                command_error,
            )

    assert not final_directory.exists()
    assert [
        item
        for item in store.list_passes(run["id"])
        if item["trigger_type"] == "operation_failure"
    ] == []
    assert store.get_run(run["id"])["state"] == "VALIDATING"

    real_create_pass = store.create_pass
    artifacts_at_pass_commit = []

    def create_pass_after_artifact_publication(
        run_id,
        trigger_type,
        trigger_json,
    ):
        if trigger_type == "operation_failure":
            artifacts_at_pass_commit.append(
                {
                    stage: (
                        final_directory
                        / stage
                        / "src"
                        / "conflict.py"
                    ).read_bytes()
                    for stage in ("base", "ours", "theirs")
                }
            )
        return real_create_pass(run_id, trigger_type, trigger_json)

    monkeypatch.setattr(
        store,
        "create_pass",
        create_pass_after_artifact_publication,
    )
    app._record_operation_failure(
        repository,
        run,
        failed_pass,
        "prepare_publication",
        command_error,
    )

    assert artifacts_at_pass_commit == [expected_artifacts]
    assert {
        stage: (
            final_directory / stage / "src" / "conflict.py"
        ).read_bytes()
        for stage in ("base", "ours", "theirs")
    } == expected_artifacts
    operation_failure_passes = [
        item
        for item in store.list_passes(run["id"])
        if item["trigger_type"] == "operation_failure"
    ]
    assert len(operation_failure_passes) == 1
    assert (
        operation_failure_passes[0]["trigger_json"]["failed_pass_id"]
        == failed_pass["id"]
    )
    assert store.get_run(run["id"])["state"] == "SPECIFYING"
    app.close()


def test_first_operation_failure_artifact_fsyncs_ancestors_before_pass_persistence(
    tmp_path,
    monkeypatch,
):
    store = Store(tmp_path / "state.sqlite3")
    data_dir = tmp_path / "runtime"
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {
            "number": 7,
            "title": "Export operation artifacts",
            "body": "Publish an exact durable operation handoff.",
        },
    )
    failed_pass = store.create_pass(
        run["id"],
        "feedback",
        {"feedback": []},
    )
    store.transition_run(run["id"], "VALIDATING")
    github = FakeGitHub()
    github.repository_operation_state_result = {
        "rebase_in_progress": True,
        "unmerged_paths": ["src/conflict.py"],
        "staged_paths": [],
        "unstaged_paths": [],
        "untracked_paths": [],
    }
    github.repository_operation_artifact_contents = {
        "src/conflict.py": {
            "base": "exact base\n",
            "ours": "exact ours\n",
            "theirs": "exact theirs\n",
        }
    }
    app = Application(
        store,
        github,
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=data_dir),
    )
    assert not (data_dir / "operation-artifacts").exists()

    fsynced_directories = []
    real_fsync_directory = Application._fsync_directory

    def record_fsync_directory(directory):
        fsynced_directories.append(Path(directory).resolve())
        real_fsync_directory(directory)

    monkeypatch.setattr(
        Application,
        "_fsync_directory",
        staticmethod(record_fsync_directory),
    )
    real_create_pass = store.create_pass
    fsyncs_before_operation_failure_pass = None

    def create_pass_after_durable_artifact_publication(
        run_id,
        trigger_type,
        trigger_json,
    ):
        nonlocal fsyncs_before_operation_failure_pass
        if trigger_type == "operation_failure":
            fsyncs_before_operation_failure_pass = tuple(fsynced_directories)
        return real_create_pass(run_id, trigger_type, trigger_json)

    monkeypatch.setattr(
        store,
        "create_pass",
        create_pass_after_durable_artifact_publication,
    )
    app._record_operation_failure(
        repository,
        run,
        failed_pass,
        "prepare_publication",
        subprocess.CalledProcessError(
            41,
            ["git", "operation-export-sentinel"],
            output="export stdout\n",
            stderr="export stderr\n",
        ),
    )

    assert fsyncs_before_operation_failure_pass is not None
    artifact_root = data_dir / "operation-artifacts"
    run_artifact_directory = artifact_root / str(run["id"])
    assert {
        data_dir.resolve(),
        artifact_root.resolve(),
        run_artifact_directory.resolve(),
    } <= set(fsyncs_before_operation_failure_pass)
    app.close()


def test_completed_git_failure_handoff_survives_restart_and_retries_from_persisted_paths(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    target_branch = "release-operation-target"
    repository = store.add_repository("acme/widget", target_branch, 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {"number": 7, "title": "Validate", "body": "work"},
    )
    feedback_item = {
        "external_id": "review:operation-origin",
        "kind": "review",
        "body": "Preserve the feedback origin across controller work.",
        "path": None,
        "line": None,
        "review_thread_id": None,
        "top_level_comment_id": None,
    }
    failed_pass = store.create_pass(
        run["id"],
        "feedback",
        {"feedback": [feedback_item]},
    )
    saved = store.save_specification_package(
        run["id"],
        failed_pass["id"],
        package(("completed-before-controller-failure", "maintain/existing")),
    )
    existing_node = store.create_dynamic_node(
        repository["id"],
        "maintain/existing",
        [1.0, 0.0],
        "Own existing source work.",
    )
    store.assign_work(saved["work_items"][0]["id"], existing_node["id"])
    claimed = store.claim_node_work(existing_node["id"], run["id"])
    store.complete_work(
        claimed["id"],
        {
            "output": "source work completed",
            "artifacts": [],
            "test_results": [],
            "repository_state": {},
        },
    )
    store.transition_run(run["id"], "VALIDATING")

    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "conflict.py").write_text(
        "<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n"
    )
    (workspace / ".git").mkdir()
    (workspace / ".git" / "rebase-merge").mkdir()

    command = [
        "git",
        "operation-command-sentinel",
        "--target",
        target_branch,
    ]
    command_failure = subprocess.CalledProcessError(
        23,
        command,
        output="operation stdout sentinel\n",
        stderr="operation stderr sentinel\n",
    )
    github = FakeGitHub()
    github.prepare_publication_failures.append(command_failure)
    github.repository_operation_state_result = {
        "rebase_in_progress": True,
        "unmerged_paths": ["src/conflict.py"],
        "staged_paths": [],
        "unstaged_paths": [],
        "untracked_paths": [],
    }
    github.repository_operation_artifact_contents = {
        "src/conflict.py": {
            "base": "base\n",
            "ours": "ours\n",
            "theirs": "theirs\n",
        }
    }
    github.continue_repository_operation_results.append(True)
    first_runtime = ScriptedRuntime()
    app = Application(
        store,
        github,
        first_runtime,
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )
    original_dynamic_node_ids = {
        item["id"] for item in store.list_dynamic_nodes(repository["id"])
    }

    app.poll_once()

    expected_trigger = {
        "failed_stage": "prepare_publication",
        "command": command,
        "returncode": 23,
        "stdout": "operation stdout sentinel\n",
        "stderr": "operation stderr sentinel\n",
        "target_branch": target_branch,
        "failed_pass_id": failed_pass["id"],
        "workspace": {
            "repository_id": repository["id"],
            "run_id": run["id"],
            "rebase_in_progress": True,
            "unmerged_paths": ["src/conflict.py"],
            "staged_paths": [],
            "unstaged_paths": [],
            "untracked_paths": [],
        },
        "origin_feedback_pass_id": failed_pass["id"],
    }
    operation_failure_passes = [
        item
        for item in store.list_passes(run["id"])
        if item["trigger_type"] == "operation_failure"
    ]
    assert len(operation_failure_passes) == 1
    operation_failure_pass = operation_failure_passes[0]
    assert operation_failure_pass["trigger_json"] == expected_trigger
    assert store.list_validations(run["id"]) == []
    assert store.get_run(run["id"])["state"] == "SPECIFYING"
    assert first_runtime.calls == []
    assert {
        item["id"] for item in store.list_dynamic_nodes(repository["id"])
    } == original_dynamic_node_ids
    assert github.repository_operation_state_calls
    assert set(github.repository_operation_state_calls) == {workspace}
    assert github.export_repository_operation_artifacts_calls
    assert {
        operation_workspace
        for operation_workspace, _ in (
            github.export_repository_operation_artifacts_calls
        )
    } == {workspace}
    assert (workspace / "src" / "conflict.py").read_text() == (
        "<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n"
    )
    assert (workspace / ".git" / "rebase-merge").is_dir()
    assert github.continue_repository_operation_calls == []
    app.close()

    chosen_classification = "agent-choice/repository-area"
    chosen_role = "Apply agent-selected source changes for this repository area."
    class ArtifactReadingRuntime(WorkspaceScriptedRuntime):
        def __init__(self):
            super().__init__()
            self.operation_artifact_bytes = []

        def run(self, task: str, workspace: str | Path, **kwargs) -> dict:
            payload = json.loads(task)
            if payload["kind"] == "work":
                self.operation_artifact_bytes.append(
                    {
                        semantic_path: {
                            stage: (
                                Path(workspace) / relative_path
                            ).read_bytes()
                            for stage, relative_path in artifacts.items()
                        }
                        for semantic_path, artifacts in payload["context"][
                            "operation_artifacts"
                        ].items()
                    }
                )
            return super().run(task, workspace, **kwargs)

    resolution_runtime = ArtifactReadingRuntime()
    resolution_runtime.queue(
        "specify",
        package(
            ("agent-selected-resolution", chosen_classification),
            prefix="operation",
        ),
    )
    resolution_runtime.queue("node_role", {"role_prompt": chosen_role})
    resolution_result = ready_result("controller operation source is resolved")
    resolution_result["repository_state"] = {"agent_observation": "resolved"}
    resolution_result["resolved_paths"] = ["src/conflict.py"]
    resolution_runtime.queue("work", resolution_result)

    def resolve_source(agent_workspace: Path) -> None:
        (agent_workspace / "src" / "conflict.py").write_text("resolved\n")
        (agent_workspace / "src" / "helper.py").write_text("helper\n")

    resolution_runtime.queue_workspace_action("work", resolve_source)
    restarted = Application(
        store,
        github,
        resolution_runtime,
        SemanticRouter(MapEmbedder({chosen_classification: [0.0, 1.0]})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    restarted.poll_once()

    specify_call = next(
        call
        for call in resolution_runtime.calls
        if call["payload"]["kind"] == "specify"
    )
    assert specify_call["payload"]["context"]["operation_failure"] == expected_trigger
    for absent_key in (
        "operation_target",
        "remedy",
        "command_sequence",
        "classification",
        "node",
    ):
        assert absent_key not in specify_call["payload"]["context"]
    role_call = next(
        call
        for call in resolution_runtime.calls
        if call["payload"]["kind"] == "node_role"
    )
    assert role_call["payload"]["context"]["classification"] == chosen_classification
    chosen_node = next(
        item
        for item in store.list_dynamic_nodes(repository["id"])
        if item["classification"] == chosen_classification
    )
    assert chosen_node["role_prompt"] == chosen_role
    assert len(store.list_dynamic_nodes(repository["id"])) == (
        len(original_dynamic_node_ids) + 1
    )

    drive_until(
        restarted,
        lambda: any(
            item["key"] == "agent-selected-resolution"
            and item["state"] == "COMPLETED"
            for item in store.list_work_items(run["id"])
        ),
    )
    assert resolution_runtime.operation_artifact_bytes == [
        {
            "src/conflict.py": {
                "base": b"base\n",
                "ours": b"ours\n",
                "theirs": b"theirs\n",
            }
        }
    ]

    completed_resolution = next(
        item
        for item in store.list_work_items(run["id"])
        if item["key"] == "agent-selected-resolution"
    )
    controller_state = completed_resolution["result"]["repository_state"][
        "_repogents"
    ]
    assert set(controller_state["applied_paths"]) == {
        "src/conflict.py",
        "src/helper.py",
    }
    assert controller_state["resolved_paths"] == ["src/conflict.py"]
    assert (workspace / "src" / "conflict.py").read_text() == "resolved\n"
    assert (workspace / "src" / "helper.py").read_text() == "helper\n"
    assert github.continue_repository_operation_calls == []
    assert len(
        [
            item
            for item in store.list_passes(run["id"])
            if item["trigger_type"] == "operation_failure"
        ]
    ) == 1
    restarted.close()

    validation_runtime = ScriptedRuntime()
    validation_runtime.queue(
        "validate",
        validation(True, "controller-prepared candidate is valid"),
    )
    resumed_again = Application(
        store,
        github,
        validation_runtime,
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    drive_until(
        resumed_again,
        lambda: any(
            call["payload"]["kind"] == "validate"
            for call in validation_runtime.calls
        ),
    )

    assert len(github.continue_repository_operation_calls) == 1
    continuation_workspace, continuation_paths = (
        github.continue_repository_operation_calls[0]
    )
    assert continuation_workspace == workspace
    assert continuation_paths == ["src/conflict.py"]
    assert github.prepare_publication_calls == [
        (7, target_branch, workspace),
        (7, target_branch, workspace),
    ]
    operation_events = [
        event for event, _ in github.repository_operation_events
    ]
    prepare_positions = [
        index
        for index, event in enumerate(operation_events)
        if event == "prepare_publication"
    ]
    continuation_position = operation_events.index(
        "continue_repository_operation"
    )
    assert len(prepare_positions) == 2
    assert prepare_positions[0] < continuation_position < prepare_positions[1]
    validations = store.list_validations(run["id"])
    assert len(validations) == 1
    assert validations[0]["pass_id"] == operation_failure_pass["id"]
    assert validations[0]["result"]["passed"] is True
    assert len(
        [
            item
            for item in store.list_passes(run["id"])
            if item["trigger_type"] == "operation_failure"
        ]
    ) == 1
    resumed_again.close()


def test_operation_failure_dirty_evidence_allows_only_explicit_staging_paths(
    tmp_path,
):
    classification = "resolve/repository-operation"
    work_key = "select-controller-staging-paths"
    runtime = WorkspaceScriptedRuntime()
    runtime.queue(
        "specify",
        package((work_key, classification), prefix="dirty-operation"),
    )
    work_result = ready_result("explicit controller staging paths selected")
    work_result["repository_state"] = {
        "agent_observation": "dirty operation source inspected"
    }
    expected_resolved_paths = [
        "src/already-unstaged.py",
        "src/already-untracked.py",
        "src/newly-created.py",
    ]
    work_result["resolved_paths"] = list(expected_resolved_paths)
    runtime.queue("work", work_result)
    runtime.queue(
        "validate",
        validation(True, "controller-staged candidate is valid"),
    )

    def change_only_new_source_and_helper(agent_workspace: Path) -> None:
        (agent_workspace / "src" / "newly-created.py").write_text(
            "new source from this work turn\n"
        )
        (agent_workspace / "src" / "helper.py").write_text(
            "helper changed without a staging report\n"
        )

    runtime.queue_workspace_action(
        "work",
        change_only_new_source_and_helper,
    )
    github = FakeGitHub()
    app, store, _, _ = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
        github=github,
    )
    target_branch = "release-dirty-operation"
    repository = store.add_repository("acme/widget", target_branch, 0.75)
    store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Own bounded repository-operation source resolution.",
    )
    run, _ = store.create_run(
        repository["id"],
        7,
        {
            "number": 7,
            "title": "Continue a dirty repository operation",
            "body": "Stage only paths explicitly reported by operation work.",
        },
    )
    failed_pass = store.create_pass(run["id"], "issue", run["issue_json"])
    store.transition_run(run["id"], "VALIDATING")
    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "src").mkdir(parents=True)
    source_contents = {
        "already-staged.py": "staged before the failure\n",
        "already-unstaged.py": "unstaged before the failure\n",
        "already-untracked.py": "untracked before the failure\n",
        "helper.py": "helper before operation work\n",
    }
    for name, contents in source_contents.items():
        (workspace / "src" / name).write_text(contents)
    (workspace / ".git" / "rebase-merge").mkdir(parents=True)

    github.repository_operation_state_result = {
        "rebase_in_progress": True,
        "unmerged_paths": [],
        "staged_paths": ["src/already-staged.py"],
        "unstaged_paths": ["src/already-unstaged.py"],
        "untracked_paths": ["src/already-untracked.py"],
    }
    github.continue_repository_operation_results.append(True)
    command = ["git", "rebase", "--continue"]
    command_error = subprocess.CalledProcessError(
        1,
        command,
        output="",
        stderr="related source edits remain unstaged\n",
    )

    app._record_operation_failure(
        repository,
        run,
        failed_pass,
        "continue_repository_operation",
        command_error,
    )

    expected_trigger = {
        "failed_stage": "continue_repository_operation",
        "command": command,
        "returncode": 1,
        "stdout": "",
        "stderr": "related source edits remain unstaged\n",
        "target_branch": target_branch,
        "failed_pass_id": failed_pass["id"],
        "workspace": {
            "repository_id": repository["id"],
            "run_id": run["id"],
            "rebase_in_progress": True,
            "unmerged_paths": [],
            "staged_paths": ["src/already-staged.py"],
            "unstaged_paths": ["src/already-unstaged.py"],
            "untracked_paths": ["src/already-untracked.py"],
        },
    }
    operation_pass = store.list_passes(run["id"])[-1]
    assert operation_pass["trigger_type"] == "operation_failure"
    assert operation_pass["trigger_json"] == expected_trigger

    drive_until(
        app,
        lambda: any(
            item["key"] == work_key
            and item["state"] in {"COMPLETED", "FAILED"}
            for item in store.list_work_items(
                run["id"],
                operation_pass["id"],
            )
        ),
    )

    completed_work = next(
        item
        for item in store.list_work_items(run["id"], operation_pass["id"])
        if item["key"] == work_key
    )
    assert completed_work["state"] == "COMPLETED", completed_work["result"]
    for kind in ("specify", "work"):
        call = next(
            item
            for item in runtime.calls
            if item["payload"]["kind"] == kind
        )
        context = call["payload"]["context"]
        assert context["operation_failure"] == expected_trigger
        assert "remedy" not in context
        assert "command_sequence" not in context
    operation_work_call = next(
        item
        for item in runtime.calls
        if item["payload"]["kind"] == "work"
    )
    assert operation_work_call["result_schema"]["resolved_paths"] == [
        "operation_failure-only source path intentionally ready for controller staging"
    ]

    controller_state = completed_work["result"]["repository_state"][
        "_repogents"
    ]
    assert set(controller_state["applied_paths"]) == {
        "src/helper.py",
        "src/newly-created.py",
    }
    assert controller_state["resolved_paths"] == expected_resolved_paths
    assert (workspace / "src" / "already-unstaged.py").read_text() == (
        source_contents["already-unstaged.py"]
    )
    assert (workspace / "src" / "already-untracked.py").read_text() == (
        source_contents["already-untracked.py"]
    )
    assert (workspace / "src" / "newly-created.py").read_text() == (
        "new source from this work turn\n"
    )
    assert (workspace / "src" / "helper.py").read_text() == (
        "helper changed without a staging report\n"
    )
    assert github.continue_repository_operation_calls == []

    drive_until(
        app,
        lambda: any(
            call["payload"]["kind"] == "validate"
            for call in runtime.calls
        ),
    )

    assert github.continue_repository_operation_calls == [
        (workspace, expected_resolved_paths)
    ]
    app.close()


def test_operation_failure_rejects_unevidenced_unchanged_resolution_before_mutation(
    tmp_path,
):
    classification = "resolve/repository-operation"
    work_key = "reject-unrelated-staging-path"
    runtime = WorkspaceScriptedRuntime()
    runtime.queue(
        "specify",
        package((work_key, classification), prefix="invalid-operation"),
    )
    work_result = ready_result("reported one unrelated controller path")
    work_result["resolved_paths"] = [
        "src/actual-turn-delta.py",
        "src/not-evidenced-or-changed.py",
    ]
    runtime.queue("work", work_result)

    def create_valid_delta_but_not_reported_unrelated_path(
        agent_workspace: Path,
    ) -> None:
        (agent_workspace / "src" / "actual-turn-delta.py").write_text(
            "created in this work turn\n"
        )
        (agent_workspace / "src" / "helper.py").write_text(
            "disposable helper mutation\n"
        )

    runtime.queue_workspace_action(
        "work",
        create_valid_delta_but_not_reported_unrelated_path,
    )
    github = FakeGitHub()
    app, store, _, _ = make_app(
        tmp_path,
        runtime=runtime,
        vectors={classification: [1.0, 0.0]},
        github=github,
    )
    repository = store.add_repository("acme/widget", "main", 0.75)
    store.create_dynamic_node(
        repository["id"],
        classification,
        [1.0, 0.0],
        "Own bounded repository-operation source resolution.",
    )
    run, _ = store.create_run(
        repository["id"],
        7,
        {
            "number": 7,
            "title": "Reject an unrelated controller staging path",
            "body": "Do not mutate canonical source for an invalid work result.",
        },
    )
    failed_pass = store.create_pass(run["id"], "issue", run["issue_json"])
    store.transition_run(run["id"], "VALIDATING")
    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "helper.py").write_text("canonical helper\n")
    (workspace / "src" / "evidenced-unstaged.py").write_text(
        "canonical unstaged source\n"
    )
    (workspace / ".git" / "rebase-merge").mkdir(parents=True)
    github.repository_operation_state_result = {
        "rebase_in_progress": True,
        "unmerged_paths": [],
        "staged_paths": [],
        "unstaged_paths": ["src/evidenced-unstaged.py"],
        "untracked_paths": [],
    }

    app._record_operation_failure(
        repository,
        run,
        failed_pass,
        "continue_repository_operation",
        subprocess.CalledProcessError(
            1,
            ["git", "rebase", "--continue"],
            output="",
            stderr="continuation requires more source staging\n",
        ),
    )
    operation_pass = store.list_passes(run["id"])[-1]

    drive_until(
        app,
        lambda: any(
            item["key"] == work_key
            and item["state"] in {"COMPLETED", "FAILED"}
            for item in store.list_work_items(
                run["id"],
                operation_pass["id"],
            )
        ),
    )

    failed_work = next(
        item
        for item in store.list_work_items(run["id"], operation_pass["id"])
        if item["key"] == work_key
    )
    assert failed_work["state"] == "FAILED"
    assert failed_work["result"]["output"]["type"] == "ValueError"
    assert (workspace / "src" / "helper.py").read_text() == (
        "canonical helper\n"
    )
    assert not (workspace / "src" / "actual-turn-delta.py").exists()
    assert not (workspace / "src" / "not-evidenced-or-changed.py").exists()
    assert github.continue_repository_operation_calls == []
    assert store.list_validations(run["id"]) == []
    app.close()


def test_continuation_failure_creates_one_new_operation_failure_without_validation(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    target_branch = "release-continuation-target"
    repository = store.add_repository("acme/widget", target_branch, 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {
            "number": 7,
            "title": "Continue repository operation",
            "body": "Continue after completed resolution work.",
        },
    )
    origin_pass = store.create_pass(run["id"], "issue", run["issue_json"])
    operation_pass = store.create_pass(
        run["id"],
        "operation_failure",
        {
            "failed_stage": "prepare_publication",
            "command": ["git", "rebase", target_branch],
            "returncode": 1,
            "stdout": "",
            "stderr": "initial conflict\n",
            "target_branch": target_branch,
            "failed_pass_id": origin_pass["id"],
            "workspace": {
                "repository_id": repository["id"],
                "run_id": run["id"],
                "rebase_in_progress": True,
                "unmerged_paths": ["src/conflict.py"],
                "staged_paths": [],
                "unstaged_paths": [],
                "untracked_paths": [],
            },
        },
    )
    saved = store.save_specification_package(
        run["id"],
        operation_pass["id"],
        package(
            ("completed-resolution", "resolve/repository-operation"),
            prefix="continuation",
        ),
    )
    node = store.create_dynamic_node(
        repository["id"],
        "resolve/repository-operation",
        [1.0, 0.0],
        "Own semantic repository-operation resolution.",
    )
    store.assign_work(saved["work_items"][0]["id"], node["id"])
    claimed = store.claim_node_work(node["id"], run["id"])
    assert claimed is not None
    store.complete_work(
        claimed["id"],
        {
            "output": "resolution completed",
            "artifacts": [],
            "test_results": [],
            "repository_state": {
                "_repogents": {
                    "applied_paths": ["src/conflict.py"],
                    "resolved_paths": ["src/conflict.py"],
                }
            },
        },
    )
    store.transition_run(run["id"], "VALIDATING")

    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "conflict.py").write_bytes(b"resolved\n")
    (workspace / ".git" / "rebase-merge").mkdir(parents=True)
    continuation_command = ["git", "rebase", "--continue"]
    github = FakeGitHub()
    github.repository_operation_state_result = {
        "rebase_in_progress": True,
        "unmerged_paths": ["src/conflict.py"],
        "staged_paths": [],
        "unstaged_paths": [],
        "untracked_paths": [],
    }
    github.repository_operation_artifact_contents = {
        "src/conflict.py": {
            "base": "base after continuation\n",
            "ours": "ours after continuation\n",
            "theirs": "theirs after continuation\n",
        }
    }
    github.continue_repository_operation_results.append(
        subprocess.CalledProcessError(
            37,
            continuation_command,
            output="continuation stdout\n",
            stderr="continuation failed\n",
        )
    )
    runtime = ScriptedRuntime()
    app = Application(
        store,
        github,
        runtime,
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )
    operation_pass_ids_before = {
        item["id"]
        for item in store.list_passes(run["id"])
        if item["trigger_type"] == "operation_failure"
    }

    app.poll_once()

    operation_failure_passes = [
        item
        for item in store.list_passes(run["id"])
        if item["trigger_type"] == "operation_failure"
    ]
    new_passes = [
        item
        for item in operation_failure_passes
        if item["id"] not in operation_pass_ids_before
    ]
    assert len(new_passes) == 1
    assert len(operation_failure_passes) == 2
    new_trigger = new_passes[0]["trigger_json"]
    assert new_trigger["failed_stage"] == "continue_repository_operation"
    assert new_trigger["failed_pass_id"] == operation_pass["id"]
    assert new_trigger["command"] == continuation_command
    assert new_trigger["returncode"] == 37
    assert new_trigger["stdout"] == "continuation stdout\n"
    assert new_trigger["stderr"] == "continuation failed\n"
    assert github.continue_repository_operation_calls == [
        (workspace, ["src/conflict.py"])
    ]
    assert store.get_run(run["id"])["state"] == "SPECIFYING"
    assert store.list_validations(run["id"]) == []
    assert runtime.calls == []
    app.close()


def test_validation_recovery_reuses_result_and_existing_followup_pass(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    failure = validation(False, "criterion is missing")
    success = validation(True, "all criteria pass")
    runs = []
    repositories = [
        store.add_repository(f"acme/widget-{issue_number}", "main", 0.75)
        for issue_number in (7, 8, 9)
    ]
    for issue_number, repository in zip((7, 8, 9), repositories):
        run, _ = store.create_run(
            repository["id"],
            issue_number,
            {"number": issue_number, "title": "Validate", "body": "work"},
        )
        execution_pass = store.create_pass(run["id"], "issue", run["issue_json"])
        store.save_specification_package(
            run["id"],
            execution_pass["id"],
            package((f"work-{issue_number}", "backend/api")),
        )
        result = dict(success if issue_number == 9 else failure)
        result["publication_candidate"] = {
            "branch": f"agent/issue-{issue_number}",
            "head_sha": f"{issue_number}" * 40,
            "target_head_sha": "a" * 40,
            "remote_head_sha": "",
        }
        store.record_validation(run["id"], execution_pass["id"], result)
        if issue_number == 8:
            store.create_pass(run["id"], "validation_failure", result)
        store.transition_run(run["id"], "VALIDATING")
        runs.append(run)
    runtime = ScriptedRuntime()
    app = Application(
        store,
        FakeGitHub(),
        runtime,
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    app.poll_once()

    assert [store.get_run(run["id"])["state"] for run in runs] == [
        "SPECIFYING",
        "SPECIFYING",
        "CREATING_PR",
    ]
    assert [len(store.list_passes(run["id"])) for run in runs] == [2, 2, 1]
    assert runtime.calls == []
    app.close()


def test_legacy_creating_pr_revalidates_before_any_publication(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {"number": 7, "title": "Validate", "body": "work"},
    )
    execution_pass = store.create_pass(
        run["id"],
        "issue",
        run["issue_json"],
    )
    store.record_validation(
        run["id"],
        execution_pass["id"],
        validation(True, "legacy validation passed"),
    )
    store.transition_run(run["id"], "CREATING_PR")
    github = FakeGitHub()
    app = Application(
        store,
        github,
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    app.poll_once()

    assert store.get_run(run["id"])["state"] == "VALIDATING"
    assert [
        item["trigger_type"] for item in store.list_passes(run["id"])
    ] == ["issue", "publication_revalidation"]
    assert github.publish_prepared_calls == []
    assert github.effect_calls == []
    app.close()


def test_unvalidated_publication_revalidation_returns_to_validating_before_publication(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {"number": 7, "title": "Validate", "body": "work"},
    )
    issue_pass = store.create_pass(run["id"], "issue", run["issue_json"])
    candidate_payload = {
        "branch": "agent/issue-7",
        "head_sha": "validated-head",
        "target_head_sha": "validated-target",
        "remote_head_sha": "validated-remote",
    }
    older_success = validation(True, "The earlier candidate passed.")
    older_success["publication_candidate"] = candidate_payload
    store.record_validation(run["id"], issue_pass["id"], older_success)
    store.create_pass(
        run["id"],
        "publication_revalidation",
        {"publication_candidate": candidate_payload},
    )
    store.transition_run(run["id"], "CREATING_PR")
    github = FakeGitHub()
    app = Application(
        store,
        github,
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    app.poll_once()

    assert store.get_run(run["id"])["state"] == "VALIDATING"
    assert [
        item["trigger_type"] for item in store.list_passes(run["id"])
    ] == ["issue", "publication_revalidation"]
    assert [
        item["pass_id"] for item in store.list_validations(run["id"])
    ] == [issue_pass["id"]]
    assert github.publish_prepared_calls == []
    assert github.effect_calls == []
    app.close()


def test_legacy_validation_result_starts_fresh_publication_revalidation(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"],
        7,
        {"number": 7, "title": "Validate", "body": "work"},
    )
    issue_pass = store.create_pass(run["id"], "issue", run["issue_json"])
    legacy_result = validation(True, "Legacy validation passed.")
    del legacy_result["code_review_findings"]
    store.record_validation(run["id"], issue_pass["id"], legacy_result)
    store.transition_run(run["id"], "VALIDATING")
    github = FakeGitHub()
    runtime = ScriptedRuntime()
    app = Application(
        store,
        github,
        runtime,
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    app.poll_once()

    assert store.get_run(run["id"])["state"] == "VALIDATING"
    assert [
        item["trigger_type"] for item in store.list_passes(run["id"])
    ] == ["issue", "publication_revalidation"]
    assert [
        item["pass_id"] for item in store.list_validations(run["id"])
    ] == [issue_pass["id"]]
    assert runtime.calls == []
    assert github.prepare_publication_calls == []
    assert github.publish_prepared_calls == []
    assert github.effect_calls == []
    app.close()


def test_feedback_recovery_creates_or_resumes_exactly_one_unprocessed_pass(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    runs = []
    repositories = [
        store.add_repository(f"acme/widget-{issue_number}", "main", 0.75)
        for issue_number in (7, 8)
    ]
    for issue_number, repository in zip((7, 8), repositories):
        run, _ = store.create_run(
            repository["id"],
            issue_number,
            {"number": issue_number, "title": "Feedback", "body": "work"},
        )
        store.create_pass(run["id"], "issue", run["issue_json"])
        package_value = {
            "external_id": f"review:{issue_number}",
            "kind": "review",
            "body": "Request changes",
        }
        store.add_feedback(run["id"], package_value["external_id"], package_value)
        if issue_number == 8:
            store.create_pass(
                run["id"], "feedback", {"feedback": [package_value]}
            )
        store.transition_run(
            run["id"],
            "PR_LISTENING",
            branch="agent/issue-7",
            pull_request={
                "number": 17,
                "url": "https://github.test/acme/widget/pull/17",
                "branch": "agent/issue-7",
                "state": "open",
                "merged": False,
                "diff": "diff",
            },
        )
        runs.append(run)
    runtime = ScriptedRuntime()
    app = Application(
        store,
        FakeGitHub(),
        runtime,
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    app.poll_once()

    assert [store.get_run(run["id"])["state"] for run in runs] == [
        "SPECIFYING",
        "SPECIFYING",
    ]
    assert [len(store.list_passes(run["id"])) for run in runs] == [2, 2]
    app.close()


def test_terminal_run_adaptation_is_reconciled_after_transition(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    run, _ = store.create_run(
        repository["id"], 7, {"number": 7, "title": "Done", "body": "work"}
    )
    store.create_dynamic_node(
        repository["id"], "backend/api", [1.0, 0.0], "Own API work."
    )
    store.transition_run(run["id"], "COMPLETED")
    app = Application(
        store,
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    app.poll_once()
    app.poll_once()

    assert store.list_dynamic_nodes(repository["id"]) == []
    app.close()


def test_routing_reuses_persisted_classification_vector_without_embedding(tmp_path):
    class FailingEmbedder:
        def embed(self, label):
            raise AssertionError(f"unexpected embedding for {label}")

    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    store.save_classification_vector(repository["id"], "backend/api", [1.0, 0.0])
    node = store.create_dynamic_node(
        repository["id"], "backend/api", [1.0, 0.0], "Own API work."
    )
    run, _ = store.create_run(
        repository["id"], 7, {"number": 7, "title": "Route", "body": "work"}
    )
    execution_pass = store.create_pass(run["id"], "issue", run["issue_json"])
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], package(("route-work", "backend/api"))
    )
    app = Application(
        store,
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(FailingEmbedder()),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )

    app._route_unassigned(repository, run, execution_pass)

    routed = store.list_work_items(run["id"])[0]
    assert routed["id"] == saved["work_items"][0]["id"]
    assert routed["node_id"] == node["id"]
    app.close()


def pull_payload(
    pull: PullRequest,
    *,
    include_validated_head: bool = True,
) -> dict:
    payload = {
        "number": pull.number,
        "url": pull.url,
        "branch": pull.branch,
        "state": pull.state,
        "merged": pull.merged,
        "diff": pull.diff,
        "head_sha": pull.head_sha,
    }
    if include_validated_head:
        payload["validated_head_sha"] = pull.head_sha
    return payload


def seed_listening_run(
    store: Store,
    repository: dict,
    issue_number: int,
    pull: PullRequest,
    *,
    pr_listening_since: float | None,
    include_validated_head: bool = True,
) -> dict:
    run, _ = store.create_run(
        repository["id"],
        issue_number,
        {
            "number": issue_number,
            "title": f"Issue {issue_number}",
            "body": f"Body for issue {issue_number}",
            "url": f"https://github.test/issues/{issue_number}",
        },
    )
    store.create_pass(run["id"], "issue", run["issue_json"])
    return store.transition_run(
        run["id"],
        "PR_LISTENING",
        branch=pull.branch,
        pull_request=pull_payload(
            pull,
            include_validated_head=include_validated_head,
        ),
        pr_listening_since=pr_listening_since,
    )


def test_mixed_feedback_scope_outcomes_order_links_and_filter_downstream_context(
    tmp_path,
):
    clock = ControlledClock(100.0)
    runtime = ScriptedRuntime()
    follow_up = {
        "title": "Track unrelated cache invalidation defect",
        "observed_defect": "Cache entries survive a completed update.",
        "affected_behavior": "Readers can observe stale values.",
        "affected_paths": ["cache.py", "service.py"],
        "acceptance_criteria": [
            "A completed update invalidates the matching cache entry.",
            "Unrelated cache entries remain available.",
        ],
    }
    runtime.queue(
        "specify",
        package(("initial-work", "backend/api"), prefix="initial"),
        feedback_specify(
            [
                feedback_disposition(
                    "inline:in-scope",
                    valid=True,
                    in_scope=True,
                    pr_regression=True,
                    explanation="The pull request introduced this regression.",
                    evidence=["The candidate branch returns the old value."],
                    specification_keys=["feedback-1"],
                ),
                feedback_disposition(
                    "inline:out-of-scope",
                    valid=True,
                    in_scope=False,
                    explanation="The defect predates and is unrelated to issue #7.",
                    evidence=["The target branch reproduces the stale cache."],
                    follow_up_issue=follow_up,
                ),
                feedback_disposition(
                    "review:invalid",
                    valid=False,
                    in_scope=False,
                    explanation="The reported call path no longer exists.",
                    evidence=["Current-head search and execution show no such call."],
                ),
            ],
            ("feedback-work", "backend/api"),
            prefix="feedback",
        ),
    )
    runtime.queue("node_role", {"role_prompt": "Own backend API outcomes."})
    runtime.queue(
        "work",
        ready_result("initial implementation"),
        ready_result("in-scope feedback correction"),
    )
    runtime.queue(
        "validate",
        validation(True, "initial implementation is valid"),
        validation(True, "in-scope correction is valid"),
    )
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"backend/api": [1.0, 0.0]},
        clock=clock,
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [
        GitHubIssue(7, "API", "Build endpoint", "https://github.test/issues/7")
    ]

    drive_until(
        app,
        lambda: store.list_runs(repository["id"])[0]["state"] == "PR_LISTENING",
    )
    run = store.list_runs(repository["id"])[0]
    assert store.get_run(run["id"])["pr_listening_since"] == 100.0
    clock.value = 200.0
    github.feedback = [
        GitHubFeedback(
            "inline:in-scope",
            "inline",
            "The branch introduced a stale response",
            "api.py",
            20,
            "PRRT_in_scope",
            201,
        ),
        GitHubFeedback(
            "inline:out-of-scope",
            "inline",
            "This existing cache problem should also be fixed",
            "cache.py",
            12,
            "PRRT_out_of_scope",
            202,
        ),
        GitHubFeedback(
            "review:invalid",
            "review",
            "The removed legacy call still looks unsafe",
        ),
    ]

    ordering: list[tuple[str, str]] = []
    original_record_follow_up = store.record_feedback_follow_up

    def record_follow_up(*args, **kwargs):
        row = original_record_follow_up(*args, **kwargs)
        ordering.append(("relationship", args[1]))
        return row

    store.record_feedback_follow_up = record_follow_up
    original_no_code = github.resolve_feedback_without_code

    def resolve_without_code(repository_name, pull_number, feedback, response):
        ordering.append(("response", feedback.external_id))
        return original_no_code(
            repository_name,
            pull_number,
            feedback,
            response,
        )

    github.resolve_feedback_without_code = resolve_without_code
    drive_until(
        app,
        lambda: (
            len(github.address_calls) == 1
            and len(github.no_code_calls) == 2
            and store.get_run(run["id"])["state"] == "PR_LISTENING"
        ),
    )

    feedback_pass = next(
        item
        for item in store.list_passes(run["id"])
        if item["trigger_type"] == "feedback"
    )
    recorded_scope = store.get_feedback_scope_result(
        run["id"],
        feedback_pass["id"],
    )
    assert [
        item["external_id"] for item in recorded_scope["dispositions"]
    ] == [
        "inline:in-scope",
        "inline:out-of-scope",
        "review:invalid",
    ]
    rows = {
        item["external_id"]: item
        for item in store.list_feedback(run["id"])
    }
    assert rows["inline:in-scope"]["disposition"] == "IN_SCOPE"
    assert rows["inline:out-of-scope"]["disposition"] == "OUT_OF_SCOPE"
    assert rows["review:invalid"]["disposition"] == "INVALID"
    assert rows["inline:out-of-scope"]["follow_up_issue"]["url"].endswith(
        "/issues/100"
    )
    assert rows["review:invalid"]["follow_up_issue"] is None
    assert rows["inline:in-scope"]["addressed_sha"] == github.pull.head_sha
    assert rows["inline:out-of-scope"]["addressed_sha"] is None
    assert rows["review:invalid"]["addressed_sha"] is None
    assert [
        feedback.external_id
        for _, _, feedback, _ in github.address_calls
    ] == ["inline:in-scope"]
    assert [
        feedback.external_id
        for _, _, feedback, _ in github.no_code_calls
    ] == ["inline:out-of-scope", "review:invalid"]
    assert ordering.index(("relationship", "inline:out-of-scope")) < ordering.index(
        ("response", "inline:out-of-scope")
    )

    _, _, _, follow_up_body = github.follow_up_requests[0]
    for required_content in (
        "## Observed defect",
        follow_up["observed_defect"],
        "## Affected behavior",
        follow_up["affected_behavior"],
        "cache.py",
        "service.py",
        "## Supporting evidence",
        "The target branch reproduces the stale cache.",
        "## Acceptance criteria",
        "## Why this is outside the current issue",
        "The defect predates and is unrelated to issue #7.",
        github.pull.url,
        f"{github.pull.url}#discussion_r202",
    ):
        assert required_content in follow_up_body
    out_of_scope_response = next(
        response
        for _, _, feedback, response in github.no_code_calls
        if feedback.external_id == "inline:out-of-scope"
    )
    assert rows["inline:out-of-scope"]["follow_up_issue"]["url"] in out_of_scope_response
    assert "The target branch reproduces the stale cache." in out_of_scope_response
    assert "The defect predates and is unrelated to issue #7." in out_of_scope_response

    specify_calls = [
        call for call in runtime.calls if call["payload"]["kind"] == "specify"
    ]
    assert [
        item["external_id"]
        for item in specify_calls[1]["payload"]["context"]["feedback"]
    ] == [
        "inline:in-scope",
        "inline:out-of-scope",
        "review:invalid",
    ]
    assert "dispositions" in specify_calls[1]["result_schema"]
    assert (
        specify_calls[1]["result_schema"]["dispositions"][0]["in_scope"]
        == "boolean; false whenever valid is false"
    )
    assert (
        "Invalid feedback must set valid=false, in_scope=false, "
        "pr_regression=false, specification_keys=[], and follow_up_issue=null."
        in specify_calls[1]["payload"]["instruction"]
    )
    work_context = [
        call["payload"]["context"]
        for call in runtime.calls
        if call["payload"]["kind"] == "work"
    ][-1]
    validation_context = [
        call["payload"]["context"]
        for call in runtime.calls
        if call["payload"]["kind"] == "validate"
    ][-1]
    for context in (work_context, validation_context):
        assert [
            item["external_id"] for item in context["feedback"]
        ] == ["inline:in-scope"]
    assert github.publish_existing == [None, github.pull.number]
    assert store.get_run(run["id"])["pr_listening_since"] == 200.0
    app.close()


def test_no_code_scope_recovery_reuses_decision_and_relationship_without_duplicates(
    tmp_path,
):
    clock = ControlledClock(500.0)
    database = tmp_path / "state.sqlite3"
    store = Store(database)
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    run = seed_listening_run(
        store,
        repository,
        7,
        github.pull,
        pr_listening_since=400.0,
    )
    feedback = GitHubFeedback(
        "inline:follow-up",
        "inline",
        "This separate defect is real",
        "cache.py",
        8,
        "PRRT_follow_up",
        303,
    )
    github.feedback = [feedback]
    first_runtime = ScriptedRuntime()
    first_runtime.queue(
        "specify",
        feedback_specify(
            [
                feedback_disposition(
                    feedback.external_id,
                    valid=True,
                    in_scope=False,
                    explanation="This defect is outside issue #7.",
                    evidence=["The target branch reproduces the defect."],
                    follow_up_issue={
                        "title": "Repair cache invalidation",
                        "observed_defect": "Updates leave stale cache entries.",
                        "affected_behavior": "Reads can return stale values.",
                        "affected_paths": ["cache.py"],
                        "acceptance_criteria": [
                            "Updates invalidate the corresponding cache entry."
                        ],
                    },
                )
            ]
        ),
    )
    app, _, _, _ = make_app(
        tmp_path,
        runtime=first_runtime,
        store=store,
        github=github,
        clock=clock,
    )

    app.poll_once()
    assert store.get_run(run["id"])["state"] == "SPECIFYING"
    original_resolve = github.resolve_feedback_without_code

    def interrupted_response(*args, **kwargs):
        raise RuntimeError("interrupted after relationship persistence")

    github.resolve_feedback_without_code = interrupted_response
    with pytest.raises(
        RuntimeError,
        match="interrupted after relationship persistence",
    ):
        app.poll_once()
    feedback_pass = store.list_passes(run["id"])[-1]
    assert store.get_feedback_scope_result(run["id"], feedback_pass["id"]) is not None
    interrupted_row = store.list_feedback(run["id"])[0]
    assert interrupted_row["follow_up_issue"]["url"].endswith("/issues/100")
    assert interrupted_row["status"] == "PENDING"
    assert len(first_runtime.calls) == 1
    assert len(github.follow_up_requests) == 1
    app.close()

    github.resolve_feedback_without_code = original_resolve
    replay_runtime = ScriptedRuntime()
    replay_app, replay_store, _, _ = make_app(
        tmp_path,
        runtime=replay_runtime,
        store=Store(database),
        github=github,
        clock=clock,
    )
    replay_app.poll_once()

    completed = replay_store.get_run(run["id"])
    row = replay_store.list_feedback(run["id"])[0]
    assert completed["state"] == "PR_LISTENING"
    assert completed["pr_listening_since"] == 500.0
    assert row["status"] == "RESOLVED"
    assert row["addressed_sha"] is None
    assert replay_runtime.calls == []
    assert len(github.follow_up_requests) == 1
    assert len(github.follow_up_issues) == 1
    assert [
        call
        for call in github.effect_calls
        if call[0] == "resolve_without_code"
    ] == [("resolve_without_code", github.pull.number, feedback.external_id)]
    assert github.publish_existing == []
    replay_app.close()


@pytest.mark.parametrize(
    ("boundary", "kind", "out_of_scope", "expected_status", "interrupt_after"),
    [
        pytest.param(
            "follow-up-created-before-relationship",
            "review",
            True,
            "ACKNOWLEDGED",
            None,
            id="follow-up-created-before-relationship",
        ),
        pytest.param(
            "response-posted-before-address-recorded",
            "inline",
            False,
            "RESOLVED",
            "response",
            id="response-posted-before-address-recorded",
        ),
        pytest.param(
            "inline-thread-resolved-before-completion",
            "inline",
            False,
            "RESOLVED",
            "thread",
            id="inline-thread-resolved-before-completion",
        ),
    ],
)
def test_no_code_scope_restart_recovers_external_effect_boundary(
    tmp_path,
    boundary,
    kind,
    out_of_scope,
    expected_status,
    interrupt_after,
):
    clock = ControlledClock(700.0)
    database = tmp_path / "state.sqlite3"
    store = Store(database)
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    run = seed_listening_run(
        store,
        repository,
        7,
        github.pull,
        pr_listening_since=600.0,
    )
    external_id = f"{kind}:{boundary}"
    feedback = GitHubFeedback(
        external_id,
        kind,
        "No current-branch change is needed",
        "cache.py" if kind == "inline" else None,
        8 if kind == "inline" else None,
        "PRRT_restart_boundary" if kind == "inline" else None,
        404 if kind == "inline" else None,
    )
    github.feedback = [feedback]
    follow_up = (
        {
            "title": "Repair cache invalidation",
            "observed_defect": "Updates leave stale cache entries.",
            "affected_behavior": "Reads can return stale values.",
            "affected_paths": ["cache.py"],
            "acceptance_criteria": [
                "Updates invalidate the corresponding cache entry."
            ],
        }
        if out_of_scope
        else None
    )
    first_runtime = ScriptedRuntime()
    first_runtime.queue(
        "specify",
        feedback_specify(
            [
                feedback_disposition(
                    external_id,
                    valid=out_of_scope,
                    in_scope=False,
                    explanation=(
                        "This defect is outside issue #7."
                        if out_of_scope
                        else "Current-head evidence shows no defect."
                    ),
                    evidence=["The current pull-request head was inspected."],
                    follow_up_issue=follow_up,
                )
            ]
        ),
    )
    app, _, _, _ = make_app(
        tmp_path,
        runtime=first_runtime,
        store=store,
        github=github,
        clock=clock,
    )

    app.poll_once()
    assert store.get_run(run["id"])["state"] == "SPECIFYING"
    interrupted = False
    if out_of_scope:
        original_record_follow_up = store.record_feedback_follow_up

        def interrupt_after_remote_effect(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise RuntimeError(f"interrupted at {boundary}")
            return original_record_follow_up(*args, **kwargs)

        store.record_feedback_follow_up = interrupt_after_remote_effect
    else:
        github.no_code_interrupt_after = interrupt_after

    with pytest.raises(RuntimeError, match=interrupt_after or boundary):
        app.poll_once()

    feedback_pass = store.list_passes(run["id"])[-1]
    durable_scope = store.get_feedback_scope_result(
        run["id"],
        feedback_pass["id"],
    )
    interrupted_row = store.list_feedback(run["id"])[0]
    assert durable_scope is not None
    assert interrupted_row["disposition"] == (
        "OUT_OF_SCOPE" if out_of_scope else "INVALID"
    )
    assert interrupted_row["status"] == "PENDING"
    assert interrupted_row["response_url"] is None
    assert len(first_runtime.calls) == 1
    if out_of_scope:
        assert interrupted_row["follow_up_issue"] is None
        assert github.follow_up_issues[external_id].url.endswith("/issues/100")
        assert github.no_code_calls == []
    else:
        remote_key = ("acme/widget", github.pull.number, external_id)
        assert github.no_code_response_urls[remote_key].endswith(
            f"#response-{external_id}"
        )
        assert (remote_key in github.no_code_resolved_threads) is (
            interrupt_after == "thread"
        )
        assert remote_key not in github.no_code_addresses
        assert interrupted_row["follow_up_issue"] is None
    app.close()

    replay_runtime = ScriptedRuntime()
    replay_store = Store(database)
    replay_app, _, _, _ = make_app(
        tmp_path,
        runtime=replay_runtime,
        store=replay_store,
        github=github,
        clock=clock,
    )
    replay_app.poll_once()

    completed = replay_store.get_run(run["id"])
    row = replay_store.list_feedback(run["id"])[0]
    assert completed["state"] == "PR_LISTENING"
    assert completed["pr_listening_since"] == 700.0
    assert row["status"] == expected_status
    assert row["addressed_sha"] is None
    assert row["response_url"] == github.no_code_addresses[
        ("acme/widget", github.pull.number, external_id)
    ].response_url
    assert row["disposition"] == interrupted_row["disposition"]
    assert row["disposition_result"] == interrupted_row["disposition_result"]
    assert replay_store.get_feedback_scope_result(
        run["id"],
        feedback_pass["id"],
    ) == durable_scope
    assert replay_runtime.calls == []
    follow_up_effects = [
        call for call in github.effect_calls if call[0] == "create_follow_up"
    ]
    no_code_effects = [
        call for call in github.effect_calls if call[0] == "resolve_without_code"
    ]
    if out_of_scope:
        assert row["follow_up_issue"]["url"] == github.follow_up_issues[
            external_id
        ].url
        assert len(github.follow_up_requests) == 2
        assert follow_up_effects == [
            ("create_follow_up", external_id, row["follow_up_issue"]["url"])
        ]
        assert len(github.no_code_calls) == 1
    else:
        assert row["follow_up_issue"] is None
        assert github.follow_up_requests == []
        assert follow_up_effects == []
        assert len(github.no_code_calls) == 2
        assert github.no_code_calls[0][3] == github.no_code_calls[1][3]
    remote_key = ("acme/widget", github.pull.number, external_id)
    assert github.no_code_response_effects == [remote_key]
    assert github.no_code_thread_resolution_effects == (
        [remote_key] if kind == "inline" else []
    )
    assert no_code_effects == [
        ("resolve_without_code", github.pull.number, external_id)
    ]
    assert github.publish_existing == []
    replay_app.close()


@pytest.mark.parametrize(
    ("valid", "in_scope"),
    [(False, False), (True, False)],
)
def test_feedback_scope_rejects_deferred_pull_request_regression(
    tmp_path,
    valid,
    in_scope,
):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    run = seed_listening_run(
        store,
        repository,
        7,
        github.pull,
        pr_listening_since=0.0,
    )
    github.feedback = [
        GitHubFeedback(
            "inline:regression",
            "inline",
            "The pull request broke this behavior",
            "api.py",
            9,
            "PRRT_regression",
            404,
        )
    ]
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        feedback_specify(
            [
                feedback_disposition(
                    "inline:regression",
                    valid=valid,
                    in_scope=in_scope,
                    pr_regression=True,
                    explanation="The result attempts to defer a branch regression.",
                )
            ]
        ),
    )
    app, _, _, _ = make_app(
        tmp_path,
        runtime=runtime,
        store=store,
        github=github,
        clock=ControlledClock(100.0),
    )

    app.poll_once()
    assert store.get_run(run["id"])["state"] == "SPECIFYING"
    with pytest.raises(ValueError, match="pull-request regression"):
        app.poll_once()

    feedback_pass = store.list_passes(run["id"])[-1]
    assert store.get_feedback_scope_result(run["id"], feedback_pass["id"]) is None
    assert github.effect_calls == []
    app.close()


def test_feedback_origin_survives_consecutive_validation_failure_passes(
    tmp_path,
):
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        package(("initial", "backend/api"), prefix="initial"),
        feedback_specify(
            [
                feedback_disposition(
                    "inline:origin",
                    valid=True,
                    in_scope=True,
                    specification_keys=["feedback-1"],
                )
            ],
            ("feedback-change", "backend/api"),
            prefix="feedback",
        ),
        package(("first-correction", "backend/api"), prefix="correction-one"),
        package(("second-correction", "backend/api"), prefix="correction-two"),
    )
    runtime.queue("node_role", {"role_prompt": "Own backend API outcomes."})
    runtime.queue(
        "work",
        ready_result("initial"),
        ready_result("feedback attempt"),
        ready_result("first validation correction"),
        ready_result("second validation correction"),
    )
    runtime.queue(
        "validate",
        validation(True, "initial passes"),
        validation(False, "feedback attempt is incomplete"),
        validation(False, "first correction remains incomplete"),
        validation(True, "second correction completes the feedback"),
    )
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"backend/api": [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [
        GitHubIssue(7, "API", "Build endpoint", "https://github.test/issues/7")
    ]
    drive_until(
        app,
        lambda: store.list_runs(repository["id"])[0]["state"] == "PR_LISTENING",
    )
    run = store.list_runs(repository["id"])[0]
    github.feedback = [
        GitHubFeedback(
            "inline:origin",
            "inline",
            "The endpoint response is incomplete",
            "api.py",
            30,
            "PRRT_origin",
            505,
        )
    ]

    drive_until(
        app,
        lambda: (
            len(github.address_calls) == 1
            and store.get_run(run["id"])["state"] == "PR_LISTENING"
        ),
    )

    passes = store.list_passes(run["id"])
    assert [item["trigger_type"] for item in passes] == [
        "issue",
        "feedback",
        "validation_failure",
        "validation_failure",
    ]
    feedback_pass = passes[1]
    assert [
        item["trigger_json"]["origin_feedback_pass_id"]
        for item in passes[2:]
    ] == [feedback_pass["id"], feedback_pass["id"]]
    for kind in ("specify", "work", "validate"):
        contexts = [
            call["payload"]["context"]
            for call in runtime.calls
            if call["payload"]["kind"] == kind
        ]
        for context in contexts[1:]:
            assert [
                item["external_id"] for item in context["feedback"]
            ] == ["inline:origin"]
    specify_calls = [
        call
        for call in runtime.calls
        if call["payload"]["kind"] == "specify"
    ]
    assert "related_prior_validation_failures" not in specify_calls[-2][
        "payload"
    ]["context"]
    recurrence = specify_calls[-1]["payload"]["context"][
        "related_prior_validation_failures"
    ]
    assert len(recurrence) == 1
    assert recurrence[0]["result"]["explanation"] == (
        "feedback attempt is incomplete"
    )
    assert "unresolved invariant or strategy class" in specify_calls[-1][
        "payload"
    ]["instruction"]
    assert [
        feedback.external_id
        for _, _, feedback, _ in github.address_calls
    ] == ["inline:origin"]
    app.close()


def test_complete_candidate_diff_review_finding_blocks_then_clean_review_publishes(
    tmp_path,
):
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        package(("initial", "backend/api"), prefix="initial"),
        package(("review-fix", "backend/api"), prefix="review-fix"),
    )
    runtime.queue("node_role", {"role_prompt": "Own backend API outcomes."})
    runtime.queue(
        "work",
        ready_result("initial candidate"),
        ready_result("review correction"),
    )
    runtime.queue(
        "validate",
        validation(
            False,
            "The full diff contains a branch-introduced defect.",
            failed_specifications=[],
            failed_criteria=[],
            code_review_findings=["Deleted fallback leaves callers without a result."],
        ),
        validation(True, "The corrected full diff is clean."),
    )
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"backend/api": [1.0, 0.0]},
    )
    github.mutate_candidate_workspace = True
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [
        GitHubIssue(7, "API", "Build endpoint", "https://github.test/issues/7")
    ]

    drive_until(
        app,
        lambda: (
            store.list_runs(repository["id"])[0]["state"] == "SPECIFYING"
            and len(
                store.list_validations(
                    store.list_runs(repository["id"])[0]["id"]
                )
            )
            == 1
        ),
    )
    run = store.list_runs(repository["id"])[0]
    first_validate_call = next(
        call for call in runtime.calls if call["payload"]["kind"] == "validate"
    )
    assert first_validate_call["result_schema"]["code_review_findings"] == ["string"]
    assert "commit_message" in first_validate_call["result_schema"]
    validations = store.list_validations(run["id"])
    assert len(validations) == 1
    assert validations[0]["result"]["code_review_findings"] == [
        "Deleted fallback leaves callers without a result."
    ]
    assert [
        item["trigger_type"] for item in store.list_passes(run["id"])
    ] == ["issue", "validation_failure"]
    prior_pass_preparation_calls = list(github.prepare_publication_calls)
    assert len(prior_pass_preparation_calls) == 1

    app.poll_once()

    assert github.prepare_publication_calls == prior_pass_preparation_calls
    assert len(store.list_validations(run["id"])) == 1
    assert [
        item["trigger_type"] for item in store.list_passes(run["id"])
    ] == ["issue", "validation_failure"]
    assert github.publish_existing == []
    assert store.list_validations(run["id"])[0]["result"][
        "code_review_findings"
    ] == ["Deleted fallback leaves callers without a result."]
    drive_until(
        app,
        lambda: store.get_run(run["id"])["state"] == "PR_LISTENING",
    )
    assert [call[3] for call in github.amend_publication_calls] == [
        "Implement validated repository change"
    ]

    work_calls = [
        call for call in runtime.calls if call["payload"]["kind"] == "work"
    ]
    assert len(work_calls) == 2
    assert all(
        "resolved_paths" not in call["result_schema"]
        for call in work_calls
    )

    validate_calls = [
        call for call in runtime.calls if call["payload"]["kind"] == "validate"
    ]
    assert len(validate_calls) == 2
    assert all(
        call["payload"]["context"]["candidate_diff"]
        == github.candidate_diff_text
        for call in validate_calls
    )
    for call in validate_calls:
        instruction = call["payload"]["instruction"]
        assert "complete staged target-to-candidate diff" in instruction
        assert "branch-introduced correctness defects" in instruction
        assert "changes not mapped to" in instruction
        assert "Do not audit unrelated pre-existing code" in instruction
    durable_workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    assert not (durable_workspace / "candidate-diff-staging.txt").exists()
    assert len(github.candidate_diff_calls) == 2
    assert all(path != durable_workspace for _, path in github.candidate_diff_calls)
    assert all(not path.exists() for _, path in github.candidate_diff_calls)
    assert github.publish_existing == [None]
    app.close()


def test_publication_candidate_movement_revalidates_before_publish(tmp_path):
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        package(("initial", "backend/api"), prefix="initial"),
    )
    runtime.queue("node_role", {"role_prompt": "Own backend API outcomes."})
    runtime.queue("work", ready_result("initial candidate"))
    runtime.queue(
        "validate",
        validation(True, "The first prepared candidate is clean."),
        validation(True, "The newly prepared candidate is clean."),
    )
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"backend/api": [1.0, 0.0]},
    )
    first_candidate = PublicationCandidate(
        branch="agent/issue-7",
        head_sha="candidate-before-movement",
        target_head_sha="target-before-movement",
        remote_head_sha="remote-before-movement",
    )
    second_candidate = PublicationCandidate(
        branch="agent/issue-7",
        head_sha="candidate-after-movement",
        target_head_sha="target-after-movement",
        remote_head_sha="remote-after-movement",
    )
    github.prepare_publication_overrides.extend(
        [
            (first_candidate, github.candidate_diff_text),
            (second_candidate, github.candidate_diff_text),
        ]
    )
    github.publish_prepared_overrides.append(None)
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [
        GitHubIssue(7, "API", "Build endpoint", "https://github.test/issues/7")
    ]

    drive_until(app, lambda: len(github.publish_prepared_calls) == 1)
    run = store.list_runs(repository["id"])[0]
    durable_workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )
    first_validation = store.list_validations(run["id"])[0]
    assert first_validation["result"]["publication_candidate"] == {
        "branch": first_candidate.branch,
        "head_sha": first_candidate.head_sha,
        "target_head_sha": first_candidate.target_head_sha,
        "remote_head_sha": first_candidate.remote_head_sha,
    }
    assert store.get_run(run["id"])["state"] == "VALIDATING"
    assert [
        execution_pass["trigger_type"]
        for execution_pass in store.list_passes(run["id"])
    ] == ["issue", "publication_revalidation"]
    assert github.publish_prepared_calls[0][4] == first_candidate
    assert github.publish_existing == []
    assert github.effect_calls == []

    drive_until(
        app,
        lambda: store.get_run(run["id"])["state"] == "PR_LISTENING",
    )

    validations = store.list_validations(run["id"])
    assert [
        item["result"]["publication_candidate"] for item in validations
    ] == [
        {
            "branch": first_candidate.branch,
            "head_sha": first_candidate.head_sha,
            "target_head_sha": first_candidate.target_head_sha,
            "remote_head_sha": first_candidate.remote_head_sha,
        },
        {
            "branch": second_candidate.branch,
            "head_sha": second_candidate.head_sha,
            "target_head_sha": second_candidate.target_head_sha,
            "remote_head_sha": second_candidate.remote_head_sha,
        },
    ]
    assert [
        call["payload"]["context"]["candidate_diff"]
        for call in runtime.calls
        if call["payload"]["kind"] == "validate"
    ] == [github.candidate_diff_text, github.candidate_diff_text]
    assert github.prepare_publication_calls == [
        (7, "main", durable_workspace),
        (7, "main", durable_workspace),
    ]
    assert len(github.candidate_diff_calls) == 2
    assert github.candidate_diff_candidates == [
        first_candidate,
        second_candidate,
    ]
    assert all(
        path != durable_workspace
        for _, path in github.candidate_diff_calls
    )
    assert all(not path.exists() for _, path in github.candidate_diff_calls)
    assert [
        call[4] for call in github.publish_prepared_calls
    ] == [first_candidate, second_candidate]
    assert github.publish_existing == [None]
    assert github.effect_calls == [
        ("publish", None, second_candidate.head_sha)
    ]
    assert store.get_run(run["id"])["pull_request"][
        "validated_head_sha"
    ] == second_candidate.head_sha
    app.close()


def test_feedback_publication_revalidation_retains_origin_and_addresses_only_that_feedback(
    tmp_path,
):
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        package(("initial", "backend/api"), prefix="initial"),
        feedback_specify(
            [
                feedback_disposition(
                    "inline:origin",
                    valid=True,
                    in_scope=True,
                    specification_keys=["feedback-1"],
                )
            ],
            ("feedback-change", "backend/api"),
            prefix="feedback",
        ),
    )
    runtime.queue("node_role", {"role_prompt": "Own backend API outcomes."})
    runtime.queue(
        "work",
        ready_result("initial"),
        ready_result("feedback correction"),
    )
    runtime.queue(
        "validate",
        validation(True, "Initial candidate passes."),
        validation(True, "Feedback correction passes."),
        validation(True, "Reprepared feedback correction passes."),
    )
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"backend/api": [1.0, 0.0]},
    )
    github.publish_head_shas.extend(
        ["initial-head-sha", "feedback-head-sha"]
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [
        GitHubIssue(7, "API", "Build endpoint", "https://github.test/issues/7")
    ]
    drive_until(
        app,
        lambda: (
            store.list_runs(repository["id"])[0]["state"] == "PR_LISTENING"
        ),
    )
    run = store.list_runs(repository["id"])[0]
    github.publish_prepared_overrides.append(None)
    github.feedback = [
        GitHubFeedback(
            "inline:origin",
            "inline",
            "The endpoint response is incomplete",
            "api.py",
            30,
            "PRRT_origin",
            505,
        )
    ]

    drive_until(app, lambda: len(github.publish_prepared_calls) == 2)

    passes = store.list_passes(run["id"])
    assert [item["trigger_type"] for item in passes] == [
        "issue",
        "feedback",
        "publication_revalidation",
    ]
    feedback_pass, publication_revalidation = passes[1:]
    assert publication_revalidation["trigger_json"][
        "origin_feedback_pass_id"
    ] == feedback_pass["id"]
    assert store.get_run(run["id"])["state"] == "VALIDATING"
    assert github.publish_existing == [None]
    assert github.address_calls == []
    assert github.effect_calls == [
        ("publish", None, "initial-head-sha")
    ]

    drive_until(
        app,
        lambda: (
            store.get_run(run["id"])["state"] == "PR_LISTENING"
            and len(github.address_calls) == 1
        ),
    )

    assert [
        item["trigger_type"] for item in store.list_passes(run["id"])
    ] == ["issue", "feedback", "publication_revalidation"]
    revalidation_calls = [
        call
        for call in runtime.calls
        if call["payload"]["kind"] == "validate"
        and call["payload"]["context"]["feedback"]
    ]
    assert [
        item["external_id"]
        for item in revalidation_calls[-1]["payload"]["context"]["feedback"]
    ] == ["inline:origin"]
    assert [
        feedback.external_id
        for _, _, feedback, _ in github.address_calls
    ] == ["inline:origin"]
    assert github.publish_existing == [None, github.pull.number]
    assert github.effect_calls == [
        ("publish", None, "initial-head-sha"),
        ("publish", github.pull.number, "feedback-head-sha"),
        (
            "address_feedback",
            github.pull.number,
            "inline:origin",
            "feedback-head-sha",
        ),
    ]
    app.close()


def test_validation_rejects_pass_with_code_review_findings():
    result = validation(
        True,
        "Incorrectly claims success despite a review finding.",
        code_review_findings=["A branch-introduced defect remains."],
    )

    with pytest.raises(ValueError, match="outcome and failures are inconsistent"):
        Application._validated_validation_result(result)


class PendingFuture:
    def __init__(self):
        self.completed = False

    def done(self) -> bool:
        return self.completed

    def result(self):
        if not self.completed:
            raise AssertionError("a pending future has no result")
        return None


class DeferredExecutor:
    def __init__(self):
        self.submissions: list[tuple] = []
        self.futures: list[PendingFuture] = []

    def submit(self, function, *args):
        self.submissions.append((function, args))
        future = PendingFuture()
        self.futures.append(future)
        return future


def test_issue_intake_defaults_to_explicit_label_authorization(tmp_path):
    app, store, github, runtime = make_app(tmp_path)
    repository = app.add_repository("acme/widget")
    github.issues = [
        GitHubIssue(7, "Unlabeled", "Not authorized", "https://issue/7"),
        GitHubIssue(
            8,
            "Authorized",
            "Explicitly ready",
            "https://issue/8",
            ("agent:ready",),
        ),
    ]

    app.poll_once()

    assert repository["autonomous_issue_intake"] is False
    assert [run["issue_number"] for run in store.list_runs(repository["id"])] == [8]
    assert not any(
        call["payload"]["kind"] == "issue_order" for call in runtime.calls
    )
    app.close()


def test_out_of_graph_agent_orders_all_open_issues_by_evidenced_dependency(
    tmp_path,
):
    runtime = ScriptedRuntime()
    runtime.queue(
        "issue_order",
        issue_order(8, 7, dependencies={7: [8]}),
    )
    app, store, github, runtime = make_app(tmp_path, runtime=runtime)
    repository = app.add_repository(
        "acme/widget", autonomous_issue_intake=True
    )
    github.issues = [
        GitHubIssue(
            7,
            "Dependent",
            "Requires the foundation from #8.",
            "https://github.test/issues/7",
        ),
        GitHubIssue(
            8,
            "Foundation",
            "Provides the prerequisite for #7.",
            "https://github.test/issues/8",
            ("priority:high",),
        ),
    ]

    app.poll_once()

    runs = store.list_runs(repository["id"])
    assert [(run["issue_number"], run["state"]) for run in runs] == [
        (8, "SPECIFYING"),
        (7, "QUEUED"),
    ]
    order_call = next(
        call for call in runtime.calls if call["payload"]["kind"] == "issue_order"
    )
    assert [
        issue["number"] for issue in order_call["payload"]["context"]["open_issues"]
    ] == [7, 8]
    assert order_call["payload"]["context"]["open_issues"][1]["labels"] == [
        "priority:high"
    ]
    assert "priority or dependency taxonomy" in order_call["payload"]["instruction"]
    plan = store.get_issue_order_plan(repository["id"])
    assert [
        item["issue_number"] for item in plan["result"]["ordered_issues"]
    ] == [8, 7]
    assert plan["result"]["ordered_issues"][1]["dependencies"][0][
        "issue_number"
    ] == 8
    assert [node["classification"] for node in store.list_nodes(repository["id"])] == [
        "Issue Specifier",
        "Work Specifier",
        "Work Validator",
        "Issue Validator",
    ]
    assert [
        (repository_name, target_branch)
        for repository_name, target_branch, _ in github.checkout_calls
    ] == [("acme/widget", "main")]
    app.close()


def test_issue_order_plan_is_reused_until_open_issue_snapshot_changes(tmp_path):
    runtime = ScriptedRuntime()
    runtime.queue(
        "issue_order",
        issue_order(8, 7, dependencies={7: [8]}),
        issue_order(9, 8, 7, dependencies={7: [8]}),
    )
    app, store, _, runtime = make_app(tmp_path, runtime=runtime)
    repository = app.add_repository("acme/widget")
    first_snapshot = [
        GitHubIssue(7, "Dependent", "Needs #8", "https://issue/7"),
        GitHubIssue(8, "Foundation", "Required by #7", "https://issue/8"),
    ]

    first = app._ordered_open_issues(repository, first_snapshot)
    reused = app._ordered_open_issues(repository, list(reversed(first_snapshot)))

    assert [issue.number for issue in first] == [8, 7]
    assert [issue.number for issue in reused] == [8, 7]
    assert [
        call["payload"]["kind"] for call in runtime.calls
    ].count("issue_order") == 1

    changed = app._ordered_open_issues(
        repository,
        [
            *first_snapshot,
            GitHubIssue(9, "Urgent", "Independent priority", "https://issue/9"),
        ],
    )

    assert [issue.number for issue in changed] == [9, 8, 7]
    assert [
        call["payload"]["kind"] for call in runtime.calls
    ].count("issue_order") == 2
    assert store.get_issue_order_plan(repository["id"])["issue_snapshot"][2][
        "number"
    ] == 9
    app.close()


def test_issue_order_rejects_dependency_after_dependent():
    issues = [
        GitHubIssue(7, "Dependent", "Needs #8", "https://issue/7"),
        GitHubIssue(8, "Foundation", "Required by #7", "https://issue/8"),
    ]

    with pytest.raises(ValueError, match="precede"):
        Application._validated_issue_order_result(
            issue_order(7, 8, dependencies={7: [8]}),
            issues,
        )


def test_active_run_does_not_preempt_and_other_listener_is_still_polled(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    github.publish_validated_to_target_result = True
    listener = seed_listening_run(
        store,
        repository,
        7,
        github.pull,
        pr_listening_since=0.0,
    )
    active, _ = store.create_run(
        repository["id"],
        8,
        {"number": 8, "title": "Active", "body": "Active source work"},
    )
    active_pass = store.create_pass(active["id"], "issue", active["issue_json"])
    store.save_specification_package(
        active["id"],
        active_pass["id"],
        package(("active-work", "backend/api")),
    )
    active_validation = validation(True, "Recorded validation is clean.")
    active_validation["publication_candidate"] = {
        "branch": "agent/issue-8",
        "head_sha": "8" * 40,
        "target_head_sha": "a" * 40,
        "remote_head_sha": "",
    }
    store.record_validation(
        active["id"],
        active_pass["id"],
        active_validation,
    )
    store.transition_run(active["id"], "VALIDATING")
    queued, _ = store.create_run(
        repository["id"],
        9,
        {"number": 9, "title": "Queued", "body": "Wait for focus"},
    )
    app, _, _, runtime = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=ControlledClock(10.0),
        pr_silence_seconds=1.0,
    )

    app.poll_once()

    assert github.pull_request_calls == [("acme/widget", github.pull.number)]
    assert github.publish_validated_to_target_calls == []
    assert github.feedback_calls == [("acme/widget", github.pull.number)]
    assert store.get_run(listener["id"])["state"] == "PR_LISTENING"
    assert store.get_run(active["id"])["state"] == "CREATING_PR"
    assert store.get_run(queued["id"])["state"] == "QUEUED"
    assert [
        item["trigger_type"] for item in store.list_passes(listener["id"])
    ] == ["issue"]
    assert runtime.calls == []
    app.close()


def test_unfocused_source_active_run_with_open_pull_is_passively_polled(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    focused, _ = store.create_run(
        repository["id"],
        7,
        {"number": 7, "title": "Focused", "body": "Active source work"},
    )
    focused_pass = store.create_pass(
        focused["id"],
        "issue",
        focused["issue_json"],
    )
    focused_validation = validation(True, "Focused validation is complete.")
    focused_validation["publication_candidate"] = {
        "branch": "agent/issue-7",
        "head_sha": "focused-head",
        "target_head_sha": "focused-target",
        "remote_head_sha": "focused-remote",
    }
    store.record_validation(
        focused["id"],
        focused_pass["id"],
        focused_validation,
    )
    store.transition_run(focused["id"], "VALIDATING")
    unfocused, _ = store.create_run(
        repository["id"],
        8,
        {"number": 8, "title": "Recovery", "body": "Persisted open PR"},
    )
    unfocused_pass = store.create_pass(
        unfocused["id"],
        "issue",
        unfocused["issue_json"],
    )
    saved = store.save_specification_package(
        unfocused["id"],
        unfocused_pass["id"],
        package(("unfocused-work", "backend/api")),
    )
    node = store.create_dynamic_node(
        repository["id"],
        "backend/api",
        [1.0, 0.0],
        "Own backend API work.",
    )
    work = store.assign_work(saved["work_items"][0]["id"], node["id"])
    store.transition_run(
        unfocused["id"],
        "WAITING_FOR_WORK_COMPLETION",
        branch=github.pull.branch,
        pull_request=pull_payload(github.pull),
    )
    github.feedback = [
        GitHubFeedback(
            "review:passive-recovery",
            "review",
            "Observe this feedback without advancing source work.",
        )
    ]
    app, _, _, runtime = make_app(
        tmp_path,
        store=store,
        github=github,
    )

    app.poll_once()

    assert github.pull_request_calls == [("acme/widget", github.pull.number)]
    assert github.feedback_calls == [("acme/widget", github.pull.number)]
    assert [
        item["external_id"] for item in store.list_feedback(unfocused["id"])
    ] == ["review:passive-recovery"]
    assert store.get_run(unfocused["id"])["state"] == "WAITING_FOR_WORK_COMPLETION"
    assert store.list_work_items(unfocused["id"])[0]["id"] == work["id"]
    assert store.list_work_items(unfocused["id"])[0]["state"] == "QUEUED"
    assert [
        item["trigger_type"] for item in store.list_passes(unfocused["id"])
    ] == ["issue"]
    assert store.get_run(focused["id"])["state"] == "CREATING_PR"
    assert runtime.calls == []
    app.close()


def test_feedback_specify_uses_refreshed_pull_head_and_diff_after_ingestion(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    ingestion_pull = PullRequest(
        number=github.pull.number,
        url=github.pull.url,
        branch=github.pull.branch,
        state="open",
        merged=False,
        diff="diff at ingestion head H1",
        head_sha="H1",
    )
    github.pull = ingestion_pull
    listener = seed_listening_run(
        store,
        repository,
        7,
        ingestion_pull,
        pr_listening_since=100.0,
    )
    active, _ = store.create_run(
        repository["id"],
        8,
        {"number": 8, "title": "Active", "body": "Active source work"},
    )
    active_pass = store.create_pass(active["id"], "issue", active["issue_json"])
    store.record_validation(
        active["id"],
        active_pass["id"],
        validation(True, "Recorded validation is clean."),
    )
    store.transition_run(active["id"], "VALIDATING")
    github.feedback = [
        GitHubFeedback(
            "inline:fresh-head",
            "inline",
            "This finding may differ on the refreshed head",
            "api.py",
            15,
            "PRRT_fresh_head",
            607,
        )
    ]
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        feedback_specify(
            [
                feedback_disposition(
                    "inline:fresh-head",
                    valid=False,
                    in_scope=False,
                )
            ]
        ),
    )
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        runtime=runtime,
        clock=ControlledClock(100.0),
        pr_silence_seconds=60.0,
    )

    app.poll_once()

    ingested = store.list_feedback(listener["id"])
    assert ingested[0]["package"]["diff"] == ingestion_pull.diff
    assert [item["trigger_type"] for item in store.list_passes(listener["id"])] == [
        "issue"
    ]

    refreshed_pull = PullRequest(
        number=ingestion_pull.number,
        url=ingestion_pull.url,
        branch=ingestion_pull.branch,
        state="open",
        merged=False,
        diff="diff at refreshed head H2",
        head_sha="H2",
    )
    github.pull = refreshed_pull
    store.transition_run(active["id"], "COMPLETED")

    app.poll_once()
    app.poll_once()

    specify_call = next(
        call for call in runtime.calls if call["payload"]["kind"] == "specify"
    )
    context = specify_call["payload"]["context"]
    assert context["pull_request_diff"] == refreshed_pull.diff
    assert context["pull_request_head_sha"] == refreshed_pull.head_sha
    feedback_pass = store.list_passes(listener["id"])[-1]
    scope_result = store.get_feedback_scope_result(
        listener["id"],
        feedback_pass["id"],
    )
    assert scope_result["head_sha"] == refreshed_pull.head_sha
    app.close()


def test_pending_feedback_wins_over_oldest_queued_issue_when_repository_idle(
    tmp_path,
):
    clock = ControlledClock(1000.0)
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    github.publish_validated_to_target_result = True
    listener = seed_listening_run(
        store,
        repository,
        7,
        github.pull,
        pr_listening_since=0.0,
    )
    queued, _ = store.create_run(
        repository["id"],
        8,
        {"number": 8, "title": "Queued", "body": "Queued body"},
    )
    github.feedback = [
        GitHubFeedback(
            "review:priority",
            "review",
            "Address this before starting another issue",
        )
    ]
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=clock,
        pr_silence_seconds=60.0,
    )

    app.poll_once()
    assert github.publish_validated_to_target_calls == []

    assert store.get_run(listener["id"])["state"] == "SPECIFYING"
    assert store.get_run(queued["id"])["state"] == "QUEUED"
    assert [
        item["trigger_type"] for item in store.list_passes(listener["id"])
    ] == ["issue", "feedback"]
    assert store.list_passes(listener["id"])[-1]["trigger_json"]["feedback"][0][
        "external_id"
    ] == "review:priority"
    assert github.checkout_calls == []
    app.close()


def test_silence_initialization_restart_and_exact_boundary_enters_pending_merge(
    tmp_path,
):
    clock = ControlledClock(1000.0)
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    github.publish_validated_to_target_result = True
    listener = seed_listening_run(
        store,
        repository,
        7,
        github.pull,
        pr_listening_since=None,
    )
    queued, _ = store.create_run(
        repository["id"],
        8,
        {"number": 8, "title": "Queued", "body": "Queued body"},
    )
    first_app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=clock,
        pr_silence_seconds=60.0,
    )

    first_app.poll_once()

    assert store.get_run(listener["id"])["pr_listening_since"] == 1000.0
    assert store.get_run(queued["id"])["state"] == "QUEUED"
    assert github.publish_validated_to_target_calls == []
    first_app.close()

    clock.value = 1059.999
    restarted_app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=clock,
        pr_silence_seconds=60.0,
    )
    restarted_app.poll_once()
    assert store.get_run(queued["id"])["state"] == "QUEUED"
    assert store.get_run(listener["id"])["pr_listening_since"] == 1000.0
    assert github.publish_validated_to_target_calls == []

    clock.value = 1060.0
    restarted_app.poll_once()
    assert store.get_run(queued["id"])["state"] == "QUEUED"
    assert store.get_run(listener["id"])["state"] == "PENDING_MERGE"
    pending_pull = store.get_run(listener["id"])["pull_request"]
    assert pending_pull["state"] == "open"
    assert pending_pull["merged"] is False
    assert pending_pull["validated_head_sha"] == github.pull.head_sha
    assert store.get_run(listener["id"])["pr_listening_since"] == 1000.0
    assert github.publish_validated_to_target_calls == []
    assert len(github.pull_request_calls) == 3
    restarted_app.close()


def test_declined_direct_target_publication_keeps_listener_and_next_run_queued(tmp_path):
    clock = ControlledClock(1060.0)
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    github.publish_validated_to_target_result = False
    listener = seed_listening_run(
        store,
        repository,
        7,
        github.pull,
        pr_listening_since=1000.0,
    )
    queued, _ = store.create_run(
        repository["id"],
        8,
        {"number": 8, "title": "Queued", "body": "Queued body"},
    )
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=clock,
        pr_silence_seconds=60.0,
        auto_merge=True,
    )

    app.poll_once()

    assert github.publish_validated_to_target_calls == [
        (
            "acme/widget",
            "main",
            tmp_path
            / "runtime"
            / "workspaces"
            / str(repository["id"])
            / str(listener["id"]),
            github.pull.head_sha,
        )
    ]
    assert github.publish_validated_issue_branches == [
        listener["pull_request"]["branch"]
    ]
    retained = store.get_run(listener["id"])
    assert retained["state"] == "PR_LISTENING"
    assert store.get_run(queued["id"])["state"] == "QUEUED"
    assert retained["pull_request"]["validated_head_sha"] == github.pull.head_sha
    app.close()


def test_direct_target_publication_waits_for_remote_merge_confirmation(tmp_path):
    clock = ControlledClock(1060.0)
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    github.publish_validated_to_target_result = True
    github.publish_validated_to_target_confirms_merge = False
    listener = seed_listening_run(
        store,
        repository,
        7,
        github.pull,
        pr_listening_since=1000.0,
    )
    queued, _ = store.create_run(
        repository["id"],
        8,
        {"number": 8, "title": "Queued", "body": "Queued body"},
    )
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=clock,
        pr_silence_seconds=60.0,
        auto_merge=True,
    )

    app.poll_once()

    retained = store.get_run(listener["id"])
    assert retained["state"] == "PR_LISTENING"
    assert retained["pull_request"]["merged"] is False
    assert retained["pull_request"]["validated_head_sha"] == github.pull.head_sha
    assert store.get_run(queued["id"])["state"] == "QUEUED"
    assert len(github.publish_validated_to_target_calls) == 1
    assert len(github.pull_request_calls) == 2
    app.close()


def test_refreshed_unvalidated_pull_head_is_never_directly_published(tmp_path):
    clock = ControlledClock(1059.999)
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    github = FakeGitHub()
    validated_pull = github.pull
    listener = seed_listening_run(
        store,
        repository,
        7,
        validated_pull,
        pr_listening_since=1000.0,
        include_validated_head=False,
    )
    github.pull = PullRequest(
        number=validated_pull.number,
        url=validated_pull.url,
        branch=validated_pull.branch,
        state=validated_pull.state,
        merged=validated_pull.merged,
        diff=validated_pull.diff,
        head_sha="unvalidated-current-head",
    )
    github.publish_validated_to_target_result = True
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=clock,
        pr_silence_seconds=60.0,
    )

    app.poll_once()

    refreshed = store.get_run(listener["id"])
    assert refreshed["pull_request"]["head_sha"] == "unvalidated-current-head"
    assert refreshed["pull_request"]["validated_head_sha"] is None
    assert github.publish_validated_to_target_calls == []

    clock.value = 1060.0
    app.poll_once()

    assert github.publish_validated_to_target_calls == []
    assert store.get_run(listener["id"])["state"] == "PR_LISTENING"
    app.close()


def test_silent_validated_run_pushes_real_target_and_advances_next_issue(
    tmp_path,
):
    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(seed))
    git("config", "user.name", "Repogents Test", cwd=seed)
    git("config", "user.email", "repogents@example.test", cwd=seed)
    (seed / "README.md").write_text("base\n")
    git("add", "README.md", cwd=seed)
    git("commit", "-m", "Base", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "--set-upstream", "origin", "main", cwd=seed)

    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    listener, _ = store.create_run(
        repository["id"],
        7,
        {"number": 7, "title": "Issue 7", "body": "Change the file"},
    )
    store.create_pass(listener["id"], "issue", listener["issue_json"])
    workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(listener["id"])
    )
    workspace.parent.mkdir(parents=True)
    git("clone", "--branch", "main", str(remote), str(workspace))
    git("config", "user.name", "Repogents Test", cwd=workspace)
    git("config", "user.email", "repogents@example.test", cwd=workspace)
    git("checkout", "-b", "agent/issue-7", cwd=workspace)
    (workspace / "README.md").write_text("validated change\n")
    git("add", "README.md", cwd=workspace)
    git("commit", "-m", "Resolve issue 7", cwd=workspace)
    validated_head = git("rev-parse", "HEAD", cwd=workspace)
    git(
        "push",
        "--set-upstream",
        "origin",
        f"{validated_head}:refs/heads/agent/issue-7",
        cwd=workspace,
    )
    pull = PullRequest(
        number=17,
        url="https://github.test/acme/widget/pull/17",
        branch="agent/issue-7",
        state="open",
        merged=False,
        diff="validated diff",
        head_sha=validated_head,
    )
    store.transition_run(
        listener["id"],
        "PR_LISTENING",
        branch=pull.branch,
        pull_request=pull_payload(pull),
        pr_listening_since=1000.0,
    )
    queued, _ = store.create_run(
        repository["id"],
        8,
        {"number": 8, "title": "Issue 8", "body": "Next issue"},
    )

    class RealTargetGitHub(FakeGitHub):
        def __init__(self):
            super().__init__()
            self.pull = pull

            def request(method, path, *, query=None, json_body=None):
                if method == "GET" and path == "/repos/acme/widget":
                    return {"node_id": "R_kgDOExample"}
                if method == "POST" and path == "/graphql":
                    updates = json_body["variables"]["input"]["refUpdates"]
                    transaction = [
                        "start",
                        *[
                            "update "
                            f"{update['name']} "
                            f"{update['afterOid']} "
                            f"{update['beforeOid']}"
                            for update in updates
                        ],
                        "prepare",
                        "commit",
                    ]
                    subprocess.run(
                        [
                            "git",
                            "--git-dir",
                            str(remote),
                            "update-ref",
                            "--stdin",
                        ],
                        input="\n".join(transaction) + "\n",
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    return {"data": {"updateRefs": {}}}
                raise AssertionError(f"unexpected request: {method} {path}")

            self.client = GitHubClient(
                "placeholder-token",
                request=request,
            )

        def publish_validated_to_target(
            self,
            github_repository: str,
            target_branch: str,
            workspace: str | Path,
            expected_head: str,
            *,
            issue_branch: str,
        ) -> bool:
            self.publish_validated_to_target_calls.append(
                (
                    github_repository,
                    target_branch,
                    Path(workspace),
                    expected_head,
                )
            )
            published = self.client.publish_validated_to_target(
                github_repository,
                target_branch,
                workspace,
                expected_head,
                issue_branch=issue_branch,
            )
            if published:
                self.pull = PullRequest(
                    number=self.pull.number,
                    url=self.pull.url,
                    branch=self.pull.branch,
                    state="closed",
                    merged=True,
                    diff=self.pull.diff,
                    head_sha=self.pull.head_sha,
                )
            return published

    github = RealTargetGitHub()
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=ControlledClock(1060.0),
        pr_silence_seconds=60.0,
        auto_merge=True,
    )

    app.poll_once()

    remote_head = git(
        "--git-dir",
        str(remote),
        "rev-parse",
        "refs/heads/main",
    )
    assert remote_head == validated_head
    assert store.get_run(listener["id"])["state"] == "COMPLETED"
    assert store.get_run(queued["id"])["state"] == "SPECIFYING"
    app.close()


def test_recent_listener_blocks_only_its_repository(
    tmp_path,
):
    clock = ControlledClock(1000.0)
    store = Store(tmp_path / "state.sqlite3")
    first_repository = store.add_repository("acme/first", "main", 0.75)
    second_repository = store.add_repository("acme/second", "main", 0.75)
    github = FakeGitHub()
    listener = seed_listening_run(
        store,
        first_repository,
        7,
        github.pull,
        pr_listening_since=990.0,
    )
    first_queued, _ = store.create_run(
        first_repository["id"],
        8,
        {"number": 8, "title": "First queued", "body": "Wait"},
    )
    second_queued, _ = store.create_run(
        second_repository["id"],
        9,
        {"number": 9, "title": "Second queued", "body": "Advance"},
    )
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=clock,
        pr_silence_seconds=60.0,
    )

    app.poll_once()

    assert store.get_run(listener["id"])["state"] == "PR_LISTENING"
    assert store.get_run(first_queued["id"])["state"] == "QUEUED"
    assert store.get_run(second_queued["id"])["state"] == "SPECIFYING"
    assert [
        repository_name for repository_name, _, _ in github.checkout_calls
    ] == ["acme/second"]
    app.close()


def test_workers_claim_work_only_for_the_repository_focused_run(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    node = store.create_dynamic_node(
        repository["id"],
        "backend/api",
        [1.0, 0.0],
        "Own backend API work.",
    )
    runs = []
    for issue_number in (7, 8):
        run, _ = store.create_run(
            repository["id"],
            issue_number,
            {
                "number": issue_number,
                "title": f"Issue {issue_number}",
                "body": "Work",
            },
        )
        execution_pass = store.create_pass(
            run["id"],
            "issue",
            run["issue_json"],
        )
        saved = store.save_specification_package(
            run["id"],
            execution_pass["id"],
            package((f"work-{issue_number}", "backend/api")),
        )
        store.assign_work(saved["work_items"][0]["id"], node["id"])
        store.transition_run(run["id"], "WAITING_FOR_WORK_COMPLETION")
        runs.append(run)
    claim_calls: list[tuple[int, int]] = []
    original_claim = store.claim_node_work

    def recording_claim(node_id, run_id):
        claim_calls.append((node_id, run_id))
        return original_claim(node_id, run_id)

    store.claim_node_work = recording_claim
    executor = DeferredExecutor()
    app = Application(
        store,
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "runtime"),
        executor=executor,
        clock=ControlledClock(0.0),
    )

    app.poll_once()

    first_work = store.list_work_items(runs[0]["id"])[0]
    second_work = store.list_work_items(runs[1]["id"])[0]
    assert first_work["state"] == "RUNNING"
    assert second_work["state"] == "QUEUED"
    assert claim_calls == [(node["id"], runs[0]["id"])]
    assert len(executor.submissions) == 1
    executor.futures[0].completed = True
    app.close()


def test_application_config_defaults_and_rejects_nonpositive_pr_silence(
    tmp_path,
):
    assert ApplicationConfig(data_dir=tmp_path).pr_silence_seconds == 3600
    for value in (0, -0.001):
        with pytest.raises(ValueError, match="pr_silence_seconds must be positive"):
            ApplicationConfig(
                data_dir=tmp_path,
                pr_silence_seconds=value,
            )


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_application_config_rejects_non_finite_pr_silence(tmp_path, value):
    with pytest.raises(ValueError):
        ApplicationConfig(
            data_dir=tmp_path,
            pr_silence_seconds=value,
        )


def focused_issue_specification() -> dict:
    return {
        "requirements": [
            {
                "key": "requirement-1",
                "statement": "Produce the requested repository outcome.",
                "evidence": ["The issue explicitly requests the outcome."],
            }
        ],
        "work_areas": [
            {
                "key": "area-1",
                "title": "Requested outcome",
                "description": "Establish the requested repository behavior.",
                "requirement_keys": ["requirement-1"],
                "dependencies": [],
                "dependency_evidence": [],
            }
        ],
    }


def focused_work_specification() -> dict:
    return {
        "work_area_key": "area-1",
        "specification": {
            "key": "area-1",
            "title": "Requested outcome",
            "description": "Produce an independently verifiable result.",
            "requirement_keys": ["requirement-1"],
            "acceptance_criteria": [
                {
                    "key": "criterion-1",
                    "description": "The requested outcome is observable.",
                    "requirement_keys": ["requirement-1"],
                }
            ],
            "work_items": [
                {
                    "key": "work-1",
                    "title": "Produce requested outcome",
                    "description": "Create the bounded repository result.",
                    "classification": "change/repository",
                    "requirement_keys": ["requirement-1"],
                    "acceptance_criteria": ["criterion-1"],
                    "evidence_requirements": ["Artifact and focused check evidence."],
                    "dependencies": [],
                    "dependency_evidence": [],
                }
            ],
        },
    }


def focused_work_validation(passed: bool) -> dict:
    return {
        "passed": passed,
        "requirement_results": [
            {
                "requirement_key": "requirement-1",
                "passed": True,
                "evidence": ["The proposed result addresses the assigned requirement."],
            }
        ],
        "criterion_results": [
            {
                "criterion_key": "criterion-1",
                "passed": passed,
                "evidence": [
                    "The proposed artifact was independently inspected."
                ],
            }
        ],
        "findings": [] if passed else ["The observable artifact is incomplete."],
        "explanation": (
            "The focused result is supported."
            if passed
            else "The focused result does not satisfy its criterion."
        ),
    }


def focused_issue_validation() -> dict:
    result = validation(True, "The integrated result satisfies the issue.")
    result.update(
        {
            "requirement_results": [
                {
                    "requirement_key": "requirement-1",
                    "passed": True,
                    "evidence": ["The complete candidate satisfies the requirement."],
                }
            ],
            "criterion_results": [
                {
                    "criterion_key": "criterion-1",
                    "passed": True,
                    "evidence": ["The complete candidate satisfies the criterion."],
                }
            ],
            "integration_findings": [],
        }
    )
    return result


def test_focused_workflow_persists_traceability_through_issue_validation(tmp_path):
    runtime = ScriptedRuntime()
    runtime.queue("issue_specify", focused_issue_specification())
    runtime.queue("work_specify", focused_work_specification())
    runtime.queue("node_role", {"role_prompt": "Own repository changes."})
    runtime.queue("work", ready_result("bounded result"))
    runtime.queue("work_validate", focused_work_validation(True))
    runtime.queue("validate", focused_issue_validation())
    app, store, github, runtime = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"change/repository": [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [
        GitHubIssue(28, "Focused workflow", "Produce the outcome", "https://issue/28")
    ]

    drive_until(
        app,
        lambda: store.list_runs(repository["id"])[0]["state"] == "PR_LISTENING",
    )

    run = store.list_runs(repository["id"])[0]
    execution_pass = store.list_passes(run["id"])[0]
    assert [call["payload"]["kind"] for call in runtime.stage_calls] == [
        "issue_specify",
        "work_specify",
        "node_role",
        "work",
        "work_validate",
        "validate",
    ]
    focused_validation_call = next(
        call
        for call in runtime.stage_calls
        if call["payload"]["kind"] == "work_validate"
    )
    assert set(focused_validation_call["payload"]["context"]) == {
        "applicable_requirements",
        "applicable_criteria",
        "work_item",
        "dependency_results",
        "proposed_result",
        "changed_paths",
        "execution_trajectory",
    }
    assert focused_validation_call["payload"]["context"][
        "applicable_requirements"
    ] == focused_issue_specification()["requirements"]
    assert focused_validation_call["payload"]["context"][
        "applicable_criteria"
    ] == focused_work_specification()["specification"]["acceptance_criteria"]
    assert focused_validation_call["result_schema"]["requirement_results"][0][
        "requirement_key"
    ] == "requirement-1"
    assert focused_validation_call["result_schema"]["criterion_results"][0][
        "criterion_key"
    ] == "criterion-1"
    assert "do not disposition any other issue requirement" in (
        focused_validation_call["payload"]["instruction"]
    )
    assert store.get_issue_specification(run["id"], execution_pass["id"]) == (
        focused_issue_specification()
    )
    work = store.list_work_items(run["id"], execution_pass["id"])[0]
    assert work["requirement_keys"] == ["requirement-1"]
    assert work["acceptance_criteria"] == ["criterion-1"]
    assert work["evidence_requirements"] == [
        "Artifact and focused check evidence."
    ]
    assert work["result"]["outcome"] == "ready_for_validation"
    assert work["result"]["work_validation"]["passed"] is True
    assert store.list_work_validations(run["id"], execution_pass["id"])[0][
        "result"
    ] == focused_work_validation(True)
    final_validation = store.list_validations(run["id"])[0]["result"]
    assert final_validation["requirement_results"][0]["requirement_key"] == (
        "requirement-1"
    )
    assert final_validation["criterion_results"][0]["criterion_key"] == (
        "criterion-1"
    )
    projected_run = app.state()["repositories"][0]["runs"][0]
    assert projected_run["issue_specifications"]
    assert projected_run["work_specification_results"]
    assert projected_run["work_validations"]
    app.close()


def test_failed_work_validation_blocks_source_import_and_drives_adaptive_recovery(
    tmp_path,
):
    runtime = WorkspaceScriptedRuntime()
    runtime.queue("issue_specify", focused_issue_specification())
    runtime.queue("work_specify", focused_work_specification())
    runtime.queue("node_role", {"role_prompt": "Own repository changes."})
    runtime.queue("work", ready_result("unsupported result"))
    runtime.queue("work_validate", focused_work_validation(False))

    def incomplete_change(workspace: Path) -> None:
        (workspace / "artifact.txt").write_text("incomplete\n")

    runtime.queue_workspace_action("work", incomplete_change)
    app, store, github, _ = make_app(
        tmp_path,
        runtime=runtime,
        vectors={"change/repository": [1.0, 0.0]},
    )
    repository = app.add_repository("acme/widget", autonomous_issue_intake=True)
    github.issues = [
        GitHubIssue(28, "Focused recovery", "Produce the outcome", "https://issue/28")
    ]
    app.poll_once()
    run = store.list_runs(repository["id"])[0]
    durable_workspace = (
        tmp_path
        / "runtime"
        / "workspaces"
        / str(repository["id"])
        / str(run["id"])
    )

    drive_until(
        app,
        lambda: (
            len(store.list_passes(run["id"])) == 2
            and store.get_run(run["id"])["state"] == "SPECIFYING"
        ),
    )

    first_pass, recovery_pass = store.list_passes(run["id"])
    failed_work = store.list_work_items(run["id"], first_pass["id"])[0]
    assert failed_work["state"] == "FAILED"
    assert failed_work["result"]["work_validation"] == focused_work_validation(False)
    assert failed_work["result"]["outcome"] == "ready_for_validation"
    assert not (durable_workspace / "artifact.txt").exists()
    assert recovery_pass["trigger_type"] == "work_failure"
    assert recovery_pass["trigger_json"]["failed_work"][0]["result"][
        "work_validation"
    ]["findings"] == ["The observable artifact is incomplete."]
    assert store.list_validations(run["id"]) == []
    assert github.prepare_publication_calls == []
    app.close()
