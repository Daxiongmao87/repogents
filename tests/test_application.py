from __future__ import annotations

import json
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

    def repository(self, github_repository: str) -> dict:
        return {
            "full_name": github_repository,
            "default_branch": "main",
            "clone_url": f"https://github.test/{github_repository}.git",
        }

    def list_ready_issues(self, github_repository: str) -> list[GitHubIssue]:
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
        self.calls.append(
            {
                "payload": payload,
                "workspace": str(workspace),
                "role_prompt": role_prompt,
                "result_schema": result_schema,
                "trajectory_path": None if trajectory_path is None else str(trajectory_path),
            }
        )
        return self.results[payload["kind"]].popleft()


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
                "executable": True,
                "work_items": [
                    {
                        "key": key,
                        "title": f"Work {key}",
                        "description": f"Implement {key}",
                        "classification": classification,
                        "dependencies": [],
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
    }


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
            "dependencies",
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
        "Specify",
        "Validate",
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
    app.add_repository("acme/widget")
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
    repository = app.add_repository("acme/widget")
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
            "state": "COMPLETED",
        },
        "latest-prior-work": {
            "key": "latest-prior-work",
            "classification": "backend/context",
            "dependencies": [],
            "state": "COMPLETED",
        },
    }
    relevant_validation = context["relevant_validation"]
    assert relevant_validation["pass_id"] == latest_validation["pass_id"]
    assert relevant_validation["result"] == {
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
                "executable": False,
                "work_items": [],
            },
            {
                "key": "closure-middle",
                "title": "Closure middle",
                "description": "WORK_CLOSURE_MIDDLE_DEFINITION",
                "acceptance_criteria": ["Middle evidence remains available."],
                "dependencies": ["closure-root"],
                "executable": False,
                "work_items": [],
            },
            {
                "key": "current-target",
                "title": "Current target",
                "description": "WORK_CURRENT_TARGET_DEFINITION",
                "acceptance_criteria": ["The current handoff is completed."],
                "dependencies": ["closure-middle"],
                "executable": True,
                "work_items": [
                    {
                        "key": "declared-dependency",
                        "title": "Declared dependency",
                        "description": "Prepare the declared dependency.",
                        "classification": "prepare/current",
                        "dependencies": [],
                    },
                    {
                        "key": "current-parent",
                        "title": "Current parent",
                        "description": "Produce the durable handoff.",
                        "classification": "implement/current",
                        "dependencies": ["declared-dependency"],
                    },
                ],
            },
            {
                "key": "unrelated-current",
                "title": "Unrelated current specification",
                "description": unrelated_specification_sentinel,
                "acceptance_criteria": ["Unrelated work is complete."],
                "dependencies": [],
                "executable": True,
                "work_items": [
                    {
                        "key": "unrelated-current-work",
                        "title": "Unrelated current work",
                        "description": unrelated_work_sentinel,
                        "classification": "unrelated/current",
                        "dependencies": [],
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
    repository = app.add_repository("acme/widget")
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
    repository = app.add_repository("acme/widget")
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
    repository = app.add_repository("acme/widget")
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
    repository = app.add_repository("acme/widget")
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
    repository = app.add_repository("acme/widget")
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
    repository = app.add_repository("acme/widget")
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
    repository = app.add_repository("acme/widget")
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
    repository = app.add_repository("acme/widget")
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
    assert github.publish_existing == []
    assert store.list_validations(run["id"])[0]["result"][
        "code_review_findings"
    ] == ["Deleted fallback leaves callers without a result."]
    drive_until(
        app,
        lambda: store.get_run(run["id"])["state"] == "PR_LISTENING",
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
    repository = app.add_repository("acme/widget")
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
    repository = app.add_repository("acme/widget")
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


def test_same_repository_ready_issues_persist_but_only_oldest_gains_focus(
    tmp_path,
):
    app, store, github, _ = make_app(tmp_path)
    repository = app.add_repository("acme/widget")
    github.issues = [
        GitHubIssue(7, "First", "First body", "https://github.test/issues/7"),
        GitHubIssue(8, "Second", "Second body", "https://github.test/issues/8"),
    ]

    app.poll_once()

    runs = store.list_runs(repository["id"])
    assert [(run["issue_number"], run["state"]) for run in runs] == [
        (7, "SPECIFYING"),
        (8, "QUEUED"),
    ]
    assert [
        (repository_name, target_branch)
        for repository_name, target_branch, _ in github.checkout_calls
    ] == [("acme/widget", "main")]
    app.close()


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


def test_silence_initialization_restart_and_exact_configured_boundary(
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
    assert store.get_run(queued["id"])["state"] == "SPECIFYING"
    assert store.get_run(listener["id"])["state"] == "COMPLETED"
    assert store.get_run(listener["id"])["pr_listening_since"] == 1000.0
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
            return self.client.publish_validated_to_target(
                github_repository,
                target_branch,
                workspace,
                expected_head,
                issue_branch=issue_branch,
            )

    github = RealTargetGitHub()
    app, _, _, _ = make_app(
        tmp_path,
        store=store,
        github=github,
        clock=ControlledClock(1060.0),
        pr_silence_seconds=60.0,
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
