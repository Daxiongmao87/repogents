from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

import pytest
from pathlib import Path

from repogents.application import Application, ApplicationConfig
from repogents.errors import RepositoryLookupTimeoutError
from repogents.http_api import HttpService
from repogents.github import FeedbackAddress, GitHubFeedback, GitHubIssue, PullRequest
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
        self.feedback: list[GitHubFeedback] = []
        self.pull = PullRequest(
            number=17,
            url="https://github.test/acme/widget/pull/17",
            branch="agent/issue-7",
            state="open",
            merged=False,
            diff="diff --git a/file b/file",
            head_sha="head-before-publication",
        )
        self.publish_existing: list[int | None] = []
        self.publish_head_shas: deque[str] = deque()
        self.effect_calls: list[tuple] = []
        self.address_calls: list[tuple[str, int, GitHubFeedback, str]] = []
        self.checkout_calls: list[tuple[str, str, Path]] = []

    def repository(self, github_repository: str) -> dict:
        return {
            "full_name": github_repository,
            "default_branch": "main",
            "clone_url": f"https://github.test/{github_repository}.git",
        }

    def list_ready_issues(self, github_repository: str) -> list[GitHubIssue]:
        return list(self.issues)

    def checkout(self, github_repository: str, target_branch: str, workspace: str | Path) -> Path:
        path = Path(workspace)
        path.mkdir(parents=True, exist_ok=True)
        self.checkout_calls.append((github_repository, target_branch, path))
        return path

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

    def pull_request(self, github_repository: str, number: int) -> PullRequest:
        assert number == self.pull.number
        return self.pull

    def list_feedback(self, github_repository: str, pull_number: int) -> list[GitHubFeedback]:
        return list(self.feedback)

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


def validation(passed: bool, explanation: str) -> dict:
    return {
        "passed": passed,
        "failed_specifications": [] if passed else ["spec-1"],
        "failed_criteria": [] if passed else ["criterion"],
        "explanation": explanation,
        "evidence": ["observed result"],
        "repository_state": {"workspace": "captured"},
        "completed_work": [{"key": "work", "state": "COMPLETED"}],
    }


def drive_until(app: Application, predicate, attempts: int = 200) -> None:
    for _ in range(attempts):
        app.poll_once()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("application did not reach expected state")


def make_app(tmp_path, runtime=None, vectors=None, **config_overrides):
    store = Store(tmp_path / "state.sqlite3")
    github = FakeGitHub()
    runtime = runtime or ScriptedRuntime()
    router = SemanticRouter(MapEmbedder(vectors or {}))
    config = ApplicationConfig(data_dir=tmp_path / "runtime", **config_overrides)
    app = Application(store, github, runtime, router, config)
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
        package(("feedback-fix", "backend/api"), prefix="feedback"),
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
    assert second_specify["payload"]["context"]["prior_validation_failures"][0]["result"]["passed"] is False
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
    assert feedback_context["validation_history"]
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
        package(("first-feedback", "backend/api"), prefix="first-feedback"),
        package(("validation-fix", "backend/api"), prefix="validation-fix"),
        package(("later-feedback", "backend/api"), prefix="later-feedback"),
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
        assert context["validation_history"]
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
        assert context["prior_specifications"]
        assert context["prior_work"]
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
        assert context["pull_request_diff"] == github.pull.diff
        assert context["original_issue"]["number"] == 7
        assert context["specifications"]
        assert context["work_items"]
        assert context["validation_history"]
        assert all("package" not in item and "diff" not in item for item in context["feedback"])
    app.close()


def test_feedback_publication_addresses_only_feedback_claimed_by_feedback_pass(
    tmp_path,
):
    runtime = ScriptedRuntime()
    runtime.queue(
        "specify",
        package(("initial", "backend/api"), prefix="initial"),
        package(("claimed-feedback", "backend/api"), prefix="claimed-feedback"),
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
    claimed = store.claim_node_work(node["id"])
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
    app.acquire_service_ownership()

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
    claimed = store.claim_node_work(node["id"])
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
    repository = store.add_repository("acme/widget", "main", 0.75)
    failure = validation(False, "criterion is missing")
    success = validation(True, "all criteria pass")
    runs = []
    for issue_number in (7, 8, 9):
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
        result = success if issue_number == 9 else failure
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


def test_feedback_recovery_creates_or_resumes_exactly_one_unprocessed_pass(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    repository = store.add_repository("acme/widget", "main", 0.75)
    runs = []
    for issue_number in (7, 8):
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


@pytest.mark.parametrize(
    "timeout",
    [float("nan"), float("inf"), float("-inf"), 0.0, -0.01],
)
def test_application_config_rejects_non_finite_or_non_positive_repository_lookup_timeout(
    tmp_path, timeout
):
    with pytest.raises(
        ValueError, match="add_repository_lookup_timeout must be finite and positive"
    ):
        ApplicationConfig(
            data_dir=tmp_path / "runtime",
            add_repository_lookup_timeout=timeout,
        )


@pytest.mark.parametrize("timeout", [0.001, 0.5, 14.0])
def test_application_config_accepts_finite_positive_repository_lookup_timeout(
    tmp_path, timeout
):
    config = ApplicationConfig(
        data_dir=tmp_path / "runtime",
        add_repository_lookup_timeout=timeout,
    )

    assert config.add_repository_lookup_timeout == timeout


def test_add_repository_lookup_timeout_establishes_non_commit_boundary(tmp_path):
    """A GitHub response arriving after the local deadline cannot reach storage."""
    app, store, github, _ = make_app(
        tmp_path, add_repository_lookup_timeout=0.05
    )
    lookup_started = threading.Event()
    release_lookup = threading.Event()

    lookup_finished = threading.Event()

    def blocked_repository(github_repository):
        lookup_started.set()
        assert release_lookup.wait(timeout=2)
        lookup_finished.set()
        return {
            "full_name": github_repository,
            "default_branch": "main",
            "clone_url": f"https://github.test/{github_repository}.git",
        }

    github.repository = blocked_repository
    started = time.monotonic()
    with pytest.raises(
        TimeoutError,
        match="metadata lookup timed out.*no repository was added",
    ):
        app.add_repository("acme/late")
    elapsed = time.monotonic() - started

    assert lookup_started.is_set()
    assert elapsed < 0.5
    assert store.list_repositories() == []

    # Let the upstream call finish well after the caller crossed its deadline.
    # The daemon lookup has no persistence authority, so the repository stays absent.
    release_lookup.set()
    assert lookup_finished.wait(timeout=2)
    assert store.list_repositories() == []
    app.close()


def test_add_repository_commits_normally_before_lookup_boundary(tmp_path):
    app, store, github, _ = make_app(
        tmp_path, add_repository_lookup_timeout=0.5
    )

    repository = app.add_repository("acme/on-time", "release")

    assert repository["github_repository"] == "acme/on-time"
    assert repository["target_branch"] == "release"
    assert [item["github_repository"] for item in store.list_repositories()] == [
        "acme/on-time"
    ]
    app.close()


def test_repository_add_operation_stays_pending_until_delayed_storage_commit(tmp_path):
    """Storage contention cannot expose non-commit before the final transaction."""
    app, store, github, _ = make_app(tmp_path, add_repository_lookup_timeout=0.5)
    commit_entered = threading.Event()
    release_commit = threading.Event()
    original_commit = store.add_repository_for_operation

    def delayed_commit(*args, **kwargs):
        commit_entered.set()
        assert release_commit.wait(timeout=2)
        return original_commit(*args, **kwargs)

    store.add_repository_for_operation = delayed_commit
    outcome = {}

    def add():
        try:
            outcome["repository"] = app.add_repository(
                "acme/storage-delayed", "release", operation_id="operation-storage-delay"
            )
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=add)
    thread.start()
    assert commit_entered.wait(timeout=2)

    pending = app.repository_add_operation("operation-storage-delay")
    assert {
        key: pending[key]
        for key in (
            "operation_id", "github_repository", "target_branch", "state",
            "repository_id", "error", "repository",
        )
    } == {
        "operation_id": "operation-storage-delay",
        "github_repository": "acme/storage-delayed",
        "target_branch": "release",
        "state": "PENDING",
        "repository_id": None,
        "error": None,
        "repository": None,
    }
    assert pending["created_at"] <= pending["updated_at"]
    assert pending["terminal_at"] is None
    assert store.list_repositories() == []

    release_commit.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["repository"]["github_repository"] == "acme/storage-delayed"

    committed = app.repository_add_operation("operation-storage-delay")
    assert committed["state"] == "COMMITTED"
    assert committed["repository_id"] == outcome["repository"]["id"]
    assert committed["repository"] == outcome["repository"]

    # Replaying the same operation identity is idempotent and cannot insert twice.
    replayed = app.add_repository(
        "acme/storage-delayed", "release", operation_id="operation-storage-delay"
    )
    assert replayed == outcome["repository"]
    assert [item["github_repository"] for item in store.list_repositories()] == [
        "acme/storage-delayed"
    ]
    assert app._repository_add_locks == {}
    app.close()


def test_repository_add_operation_failure_is_terminal_before_late_lookup_finishes(tmp_path):
    """A FAILED operation cannot later transition to a repository commit."""
    app, store, github, _ = make_app(
        tmp_path, add_repository_lookup_timeout=0.05
    )
    lookup_started = threading.Event()
    release_lookup = threading.Event()
    lookup_finished = threading.Event()

    def blocked_repository(github_repository):
        lookup_started.set()
        assert release_lookup.wait(timeout=2)
        lookup_finished.set()
        return {
            "full_name": github_repository,
            "default_branch": "main",
            "clone_url": f"https://github.test/{github_repository}.git",
        }

    github.repository = blocked_repository
    with pytest.raises(RepositoryLookupTimeoutError):
        app.add_repository(
            "acme/terminal-failure", operation_id="operation-terminal-failure"
        )
    assert lookup_started.is_set()

    failed = app.repository_add_operation("operation-terminal-failure")
    assert failed["state"] == "FAILED"
    assert failed["repository_id"] is None
    assert "metadata lookup timed out" in failed["error"]
    assert failed["repository"] is None

    release_lookup.set()
    assert lookup_finished.wait(timeout=2)
    assert app.repository_add_operation("operation-terminal-failure")["state"] == "FAILED"
    assert store.list_repositories() == []
    assert app._repository_add_locks == {}
    app.close()


def test_application_restart_makes_interrupted_repository_add_definitively_failed(tmp_path):
    """No prior-process PENDING operation can retain imaginary commit authority."""
    store = Store(tmp_path / "state.sqlite3")
    store.begin_repository_add_operation(
        "operation-interrupted", "acme/interrupted", None
    )
    github = FakeGitHub()
    runtime = ScriptedRuntime()
    router = SemanticRouter(MapEmbedder({}))
    app = Application(
        store,
        github,
        runtime,
        router,
        ApplicationConfig(data_dir=tmp_path / "runtime"),
    )
    app.acquire_service_ownership()

    operation = app.repository_add_operation("operation-interrupted")
    assert operation["state"] == "FAILED"
    assert operation["repository_id"] is None
    assert operation["error"] == "repository add was interrupted before storage commit"
    assert operation["repository"] is None
    assert store.list_repositories() == []
    app.close()


def test_concurrent_repository_add_replay_waits_for_authoritative_first_outcome(tmp_path):
    """One operation identity cannot race itself into conflicting terminal states."""
    app, store, github, _ = make_app(tmp_path, add_repository_lookup_timeout=0.5)
    first_lookup_started = threading.Event()
    release_first_lookup = threading.Event()
    lookup_calls = 0
    lookup_lock = threading.Lock()

    def repository_metadata(github_repository):
        nonlocal lookup_calls
        with lookup_lock:
            lookup_calls += 1
        first_lookup_started.set()
        assert release_first_lookup.wait(timeout=2)
        return {
            "full_name": github_repository,
            "default_branch": "main",
            "clone_url": f"https://github.test/{github_repository}.git",
        }

    github.repository = repository_metadata
    results = []
    errors = []

    def add():
        try:
            results.append(
                app.add_repository(
                    "acme/idempotent", operation_id="operation-idempotent"
                )
            )
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=add)
    second = threading.Thread(target=add)
    first.start()
    assert first_lookup_started.wait(timeout=2)
    second.start()
    release_first_lookup.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert lookup_calls == 1
    assert len(store.list_repositories()) == 1
    assert app.repository_add_operation("operation-idempotent")["state"] == "COMMITTED"
    assert app._repository_add_locks == {}
    app.close()


def test_repository_add_lock_retirement_waits_for_all_replay_callers(tmp_path):
    """The shared entry survives its owner and every waiter, then retires."""
    app, store, github, _ = make_app(tmp_path, add_repository_lookup_timeout=0.5)
    lookup_started = threading.Event()
    release_lookup = threading.Event()
    lookup_calls = 0
    lookup_guard = threading.Lock()

    def repository_metadata(github_repository):
        nonlocal lookup_calls
        with lookup_guard:
            lookup_calls += 1
        lookup_started.set()
        assert release_lookup.wait(timeout=2)
        return {
            "full_name": github_repository,
            "default_branch": "main",
            "clone_url": f"https://github.test/{github_repository}.git",
        }

    github.repository = repository_metadata
    caller_count = 6
    results = []
    errors = []

    def add():
        try:
            results.append(
                app.add_repository(
                    "acme/many-replays", operation_id="operation-many-replays"
                )
            )
        except BaseException as error:
            errors.append(error)

    callers = [threading.Thread(target=add) for _ in range(caller_count)]
    callers[0].start()
    assert lookup_started.wait(timeout=2)
    for caller in callers[1:]:
        caller.start()

    # Observe the intentional registry contract rather than relying on scheduler
    # timing: the owner and every waiter have registered a reference before release.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with app._repository_add_lock_guard:
            entry = app._repository_add_locks.get("operation-many-replays")
            references = entry.references if entry is not None else 0
        if references == caller_count:
            break
        time.sleep(0.005)
    assert references == caller_count

    release_lookup.set()
    for caller in callers:
        caller.join(timeout=2)
        assert not caller.is_alive()

    assert errors == []
    assert len(results) == caller_count
    assert all(result == results[0] for result in results)
    assert lookup_calls == 1
    assert len(store.list_repositories()) == 1
    assert app._repository_add_locks == {}
    app.close()


def test_repository_add_lock_retirement_serializes_new_arrival_at_delete_boundary(tmp_path):
    """A new caller cannot acquire a replacement lock while retirement is in flight."""
    app, _, _, _ = make_app(tmp_path, add_repository_lookup_timeout=0.5)
    deletion_started = threading.Event()
    allow_deletion = threading.Event()
    first_execution_started = threading.Event()
    release_first_execution = threading.Event()
    execution_guard = threading.Lock()
    active_executions = 0
    maximum_active_executions = 0
    lock_entry_ids = []

    class RetirementBoundaryRegistry(dict):
        def __delitem__(self, key):
            deletion_started.set()
            assert allow_deletion.wait(timeout=2)
            super().__delitem__(key)

    app._repository_add_locks = RetirementBoundaryRegistry()

    def execute(operation_id, github_repository, target_branch):
        nonlocal active_executions, maximum_active_executions
        with execution_guard:
            active_executions += 1
            maximum_active_executions = max(
                maximum_active_executions, active_executions
            )
            lock_entry_ids.append(id(app._repository_add_locks[operation_id]))
        if not first_execution_started.is_set():
            first_execution_started.set()
            assert release_first_execution.wait(timeout=2)
        with execution_guard:
            active_executions -= 1
        return {"github_repository": github_repository}

    app._execute_repository_add = execute
    results = []
    errors = []

    def add():
        try:
            results.append(
                app.add_repository(
                    "acme/retirement-boundary",
                    operation_id="operation-retirement-boundary",
                )
            )
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=add)
    owner.start()
    assert first_execution_started.wait(timeout=2)
    release_first_execution.set()
    assert deletion_started.wait(timeout=2)

    # Retirement holds the registry guard. The arriving replay cannot create or
    # acquire another lock until the old execution has finished and deletion commits.
    arriving_replay = threading.Thread(target=add)
    arriving_replay.start()
    assert arriving_replay.is_alive()
    allow_deletion.set()

    owner.join(timeout=2)
    arriving_replay.join(timeout=2)
    assert not owner.is_alive()
    assert not arriving_replay.is_alive()
    assert errors == []
    assert len(results) == 2
    assert maximum_active_executions == 1
    assert len(set(lock_entry_ids)) == 2
    assert app._repository_add_locks == {}
    app.close()

def test_empty_repository_lookup_error_records_nonempty_terminal_operation_failure(tmp_path):
    """An empty lookup exception remains original while durable status becomes FAILED."""
    app, store, github, _ = make_app(tmp_path, add_repository_lookup_timeout=0.5)
    original_error = TimeoutError()

    def empty_failure(_github_repository):
        raise original_error

    github.repository = empty_failure
    with pytest.raises(TimeoutError) as captured:
        app.add_repository("acme/empty-lookup", operation_id="empty-lookup")

    assert captured.value is original_error
    operation = app.repository_add_operation("empty-lookup")
    assert operation["state"] == "FAILED"
    assert operation["repository_id"] is None
    assert operation["repository"] is None
    assert operation["error"] == "TimeoutError: repository add failed"
    assert store.list_repositories() == []
    app.close()


def test_whitespace_repository_lookup_error_uses_exception_type_fallback(tmp_path):
    """Whitespace-only exception text is not accepted as a durable diagnostic."""
    app, _, github, _ = make_app(tmp_path, add_repository_lookup_timeout=0.5)

    class WhitespaceError(Exception):
        def __str__(self):
            return "  \t "

    original_error = WhitespaceError()

    def whitespace_failure(_github_repository):
        raise original_error

    github.repository = whitespace_failure
    with pytest.raises(WhitespaceError) as captured:
        app.add_repository("acme/whitespace", operation_id="whitespace-error")

    assert captured.value is original_error
    operation = app.repository_add_operation("whitespace-error")
    assert operation["state"] == "FAILED"
    assert operation["error"] == "WhitespaceError: repository add failed"
    app.close()


def test_empty_storage_error_records_nonempty_terminal_operation_failure(tmp_path):
    """An empty storage-adjacent exception cannot leave the operation PENDING."""
    app, store, github, _ = make_app(tmp_path, add_repository_lookup_timeout=0.5)
    original_error = RuntimeError()

    def empty_failure(*_args, **_kwargs):
        raise original_error

    store.add_repository_for_operation = empty_failure
    with pytest.raises(RuntimeError) as captured:
        app.add_repository("acme/empty-storage", operation_id="empty-storage")

    assert captured.value is original_error
    operation = app.repository_add_operation("empty-storage")
    assert operation["state"] == "FAILED"
    assert operation["repository_id"] is None
    assert operation["repository"] is None
    assert operation["error"] == "RuntimeError: repository add failed"
    assert store.list_repositories() == []
    app.close()


def test_repository_add_failure_normalization_preserves_meaningful_message(tmp_path):
    """Existing useful diagnostics are stored byte-for-byte rather than rewritten."""
    app, _, github, _ = make_app(tmp_path, add_repository_lookup_timeout=0.5)
    original_error = ValueError("repository metadata was rejected")

    def meaningful_failure(_github_repository):
        raise original_error

    github.repository = meaningful_failure
    with pytest.raises(ValueError) as captured:
        app.add_repository("acme/meaningful", operation_id="meaningful-error")

    assert captured.value is original_error
    operation = app.repository_add_operation("meaningful-error")
    assert operation["state"] == "FAILED"
    assert operation["error"] == "repository metadata was rejected"
    app.close()



def test_http_empty_message_repository_add_failures_expose_terminal_status(tmp_path):
    """POST keeps original 500 semantics while status lookup exposes durable FAILED."""

    def exercise(case_name, configure_failure, expected_error):
        case_path = tmp_path / case_name
        case_path.mkdir(parents=True, exist_ok=True)
        app, store, github, _ = make_app(
            case_path, add_repository_lookup_timeout=0.5
        )
        original_error = configure_failure(store, github)
        service = HttpService(app, "127.0.0.1", 0, 60)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        host, port = service.address
        base = f"http://{host}:{port}"
        operation_id = f"empty-http-{case_name}"
        request = urllib.request.Request(
            base + "/api/repositories",
            data=json.dumps({"github_repository": f"acme/{case_name}"}).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Repogents-Operation-Id": operation_id,
            },
        )
        try:
            with pytest.raises(urllib.error.HTTPError) as captured:
                urllib.request.urlopen(request, timeout=3)
            assert captured.value.code == 500
            # The transport still reflects the original empty-message exception;
            # durable normalization must not replace or mask that failure path.
            assert json.loads(captured.value.read()) == {"error": ""}

            with urllib.request.urlopen(
                base + f"/api/repository-add-operations/{operation_id}", timeout=3
            ) as response:
                operation = json.loads(response.read())
            assert operation["state"] == "FAILED"
            assert operation["repository_id"] is None
            assert operation["repository"] is None
            assert operation["error"] == expected_error
            assert operation["error"].strip()
            assert store.list_repositories() == []

            # Replaying the same durable identity cannot revive a FAILED operation.
            with pytest.raises(urllib.error.HTTPError) as replayed:
                urllib.request.urlopen(request, timeout=3)
            assert replayed.value.code == 400
            assert store.list_repositories() == []
            assert app.repository_add_operation(operation_id)["state"] == "FAILED"
            assert original_error is not None
        finally:
            service.shutdown()
            thread.join(timeout=3)
            app.close()

    def empty_lookup(_store, github):
        error = TimeoutError()

        def fail(_repository):
            raise error

        github.repository = fail
        return error

    def empty_storage(store, _github):
        error = RuntimeError()

        def fail(*_args, **_kwargs):
            raise error

        store.add_repository_for_operation = fail
        return error

    exercise("lookup", empty_lookup, "TimeoutError: repository add failed")
    exercise("storage", empty_storage, "RuntimeError: repository add failed")


def test_empty_post_commit_error_does_not_overwrite_committed_operation(tmp_path):
    """An exceptional response after atomic commit still returns durable COMMITTED state."""
    app, store, github, _ = make_app(tmp_path, add_repository_lookup_timeout=0.5)
    original_commit = store.add_repository_for_operation
    late_error = RuntimeError()

    def commit_then_fail(*args, **kwargs):
        original_commit(*args, **kwargs)
        raise late_error

    store.add_repository_for_operation = commit_then_fail
    repository = app.add_repository(
        "acme/committed-before-error", operation_id="committed-before-empty-error"
    )

    operation = app.repository_add_operation("committed-before-empty-error")
    assert operation["state"] == "COMMITTED"
    assert operation["error"] is None
    assert operation["repository_id"] == repository["id"]
    assert operation["repository"] == repository
    assert [item["id"] for item in store.list_repositories()] == [repository["id"]]
    app.close()


def test_application_cleanup_trigger_expires_terminal_operations_in_batches(tmp_path):
    now = [3000.0]
    store = Store(tmp_path / "retention-application.sqlite3", clock=lambda: now[0])
    github = FakeGitHub()
    runtime = ScriptedRuntime()
    router = SemanticRouter(MapEmbedder({}))
    config = ApplicationConfig(
        data_dir=tmp_path / "runtime-retention",
        repository_add_operation_retention_seconds=100.0,
        repository_add_operation_cleanup_batch_size=2,
    )
    app = Application(store, github, runtime, router, config)
    app.acquire_service_ownership()

    for index in range(5):
        operation_id = f"expired-{index}"
        store.begin_repository_add_operation(operation_id, f"acme/expired-{index}", None)
        store.fail_repository_add_operation(operation_id, "not found")
    store.begin_repository_add_operation("old-pending", "acme/pending", None)

    now[0] = 3101.0
    # Status lookup is a centrally owned cleanup trigger and deletes only one
    # configured batch before serving the retained operation result.
    retained = app.repository_add_operation("expired-4")
    assert retained is not None
    with sqlite3.connect(store.path) as connection:
        terminal_count = connection.execute(
            "SELECT COUNT(*) FROM repository_add_operations WHERE state != 'PENDING'"
        ).fetchone()[0]
    assert terminal_count == 3
    assert store.get_repository_add_operation("old-pending")["state"] == "PENDING"

    # Registration triggers another bounded batch, then the new operation commits.
    repository = app.add_repository(
        "acme/current", "main", operation_id="current-operation"
    )
    assert repository["github_repository"] == "acme/current"
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            "SELECT operation_id, state FROM repository_add_operations ORDER BY operation_id"
        ).fetchall()
    assert ("old-pending", "PENDING") in rows
    assert ("current-operation", "COMMITTED") in rows
    assert sum(state in {"COMMITTED", "FAILED"} for _, state in rows) == 2
    app.close()


def test_application_config_rejects_invalid_operation_retention_policy(tmp_path):
    for overrides in (
        {"repository_add_operation_retention_seconds": 0},
        {"repository_add_operation_cleanup_batch_size": 0},
    ):
        with pytest.raises(ValueError):
            ApplicationConfig(data_dir=tmp_path, **overrides)


def test_application_cleanup_drains_large_expired_failure_history_in_bounded_batches(tmp_path):
    """Normal application activity steadily bounds hostile terminal history growth."""
    now = [4000.0]
    store = Store(tmp_path / "retention-growth.sqlite3", clock=lambda: now[0])
    for index in range(105):
        operation_id = f"hostile-failure-{index:03d}"
        store.begin_repository_add_operation(
            operation_id, f"acme/hostile-{index}", None
        )
        store.fail_repository_add_operation(operation_id, "repository unavailable")

    now[0] = 4101.0
    github = FakeGitHub()
    app = Application(
        store,
        github,
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(
            data_dir=tmp_path / "runtime-retention-growth",
            repository_add_operation_retention_seconds=100.0,
            repository_add_operation_cleanup_batch_size=25,
        ),
    )
    app.acquire_service_ownership()

    # Startup owns one bounded batch, not an unbounded writer lock.
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM repository_add_operations"
        ).fetchone()[0] == 80

    store.begin_repository_add_operation("active-pending", "acme/active", None)
    store.begin_repository_add_operation("recent-status", "acme/recent", None)
    store.fail_repository_add_operation("recent-status", "recent failure")

    remaining_expired = []
    for _ in range(4):
        assert app.repository_add_operation("recent-status")["state"] == "FAILED"
        with sqlite3.connect(store.path) as connection:
            remaining_expired.append(
                connection.execute(
                    """SELECT COUNT(*) FROM repository_add_operations
                    WHERE operation_id LIKE 'hostile-failure-%'"""
                ).fetchone()[0]
            )

    assert remaining_expired == [55, 30, 5, 0]
    assert app.repository_add_operation("active-pending")["state"] == "PENDING"
    assert app.repository_add_operation("recent-status")["error"] == "recent failure"
    app.close()


def test_legacy_terminal_operations_receive_full_window_before_startup_expiration(tmp_path):
    """Migration age starts locally and later application startup may expire it."""
    path = tmp_path / "legacy-retention-startup.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE repositories (
            id INTEGER PRIMARY KEY,
            github_repository TEXT NOT NULL UNIQUE,
            target_branch TEXT NOT NULL,
            similarity_threshold REAL NOT NULL,
            tracked INTEGER NOT NULL DEFAULT 1 CHECK (tracked IN (0, 1))
        );
        CREATE TABLE repository_add_operations (
            operation_id TEXT PRIMARY KEY,
            github_repository TEXT NOT NULL,
            target_branch TEXT,
            state TEXT NOT NULL CHECK (state IN ('PENDING', 'COMMITTED', 'FAILED')),
            repository_id INTEGER REFERENCES repositories(id),
            error TEXT
        );
        INSERT INTO repository_add_operations VALUES
            ('legacy-failed', 'acme/legacy-failed', NULL, 'FAILED', NULL, 'missing');
        """
    )
    connection.commit()
    connection.close()

    now = [5000.0]
    store = Store(path, clock=lambda: now[0])
    config = ApplicationConfig(
        data_dir=tmp_path / "runtime-legacy-retention",
        repository_add_operation_retention_seconds=100.0,
        repository_add_operation_cleanup_batch_size=10,
    )
    app = Application(
        store, FakeGitHub(), ScriptedRuntime(), SemanticRouter(MapEmbedder({})), config
    )
    app.acquire_service_ownership()
    assert app.repository_add_operation("legacy-failed")["error"] == "missing"
    app.close()

    now[0] = 5100.0
    restarted = Application(
        store, FakeGitHub(), ScriptedRuntime(), SemanticRouter(MapEmbedder({})), config
    )
    restarted.acquire_service_ownership()
    assert restarted.repository_add_operation("legacy-failed") is None
    restarted.close()


def test_application_retains_committed_lookup_and_replay_then_expires_only_operation(tmp_path):
    """Application status and idempotent replay remain authoritative until expiry."""
    now = [6000.0]
    store = Store(tmp_path / "retention-replay.sqlite3", clock=lambda: now[0])
    app = Application(
        store,
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(
            data_dir=tmp_path / "runtime-retention-replay",
            repository_add_operation_retention_seconds=100.0,
            repository_add_operation_cleanup_batch_size=10,
        ),
    )
    repository = app.add_repository(
        "acme/replay-window", "release", operation_id="retained-commit"
    )

    now[0] = 6099.999
    operation = app.repository_add_operation("retained-commit")
    assert operation["state"] == "COMMITTED"
    assert operation["repository"] == repository
    assert app.add_repository(
        "acme/replay-window", "release", operation_id="retained-commit"
    ) == repository

    now[0] = 6100.0
    assert app.repository_add_operation("retained-commit") is None
    assert store.get_repository(repository["id"])["github_repository"] == (
        "acme/replay-window"
    )
    assert len(store.list_nodes(repository["id"])) == 2
    app.close()


def test_duplicate_service_startup_does_not_recover_live_pending_add(tmp_path):
    """Only a listener-owning data-directory owner may recover pending adds."""
    database = tmp_path / "owned-state.sqlite3"
    data_dir = tmp_path / "owned-runtime"

    def application():
        return Application(
            Store(database),
            FakeGitHub(),
            ScriptedRuntime(),
            SemanticRouter(MapEmbedder({})),
            ApplicationConfig(data_dir=data_dir),
        )

    live_app = application()
    live_service = HttpService(live_app, "127.0.0.1", 0, 60)
    live_thread = threading.Thread(
        target=live_service.serve_forever, daemon=True
    )
    live_thread.start()
    host, port = live_service.address

    # Model a mutation registered by the active owner after startup recovery.
    live_app.store.begin_repository_add_operation(
        "live-pending", "acme/live", None
    )
    assert live_app.repository_add_operation("live-pending")["state"] == "PENDING"

    competing_app = application()
    with pytest.raises(OSError):
        HttpService(competing_app, host, port, 60)
    # Binding failed before ownership acquisition, so the live operation and all
    # durable repository state remain untouched. The failed app was also closed.
    assert live_app.repository_add_operation("live-pending")["state"] == "PENDING"
    assert competing_app._closed is True

    # Even a different listener cannot serve or mutate the same data directory.
    ownership_competitor = application()
    with pytest.raises(RuntimeError, match="data directory is already owned"):
        HttpService(ownership_competitor, "127.0.0.1", 0, 60)
    assert ownership_competitor._closed is True
    assert live_app.repository_add_operation("live-pending")["state"] == "PENDING"

    live_service.shutdown()
    live_thread.join(timeout=3)
    assert not live_thread.is_alive()

    restarted_app = application()
    restarted_service = HttpService(restarted_app, host, port, 60)
    try:
        recovered = restarted_app.repository_add_operation("live-pending")
        assert recovered["state"] == "FAILED"
        assert recovered["error"] == (
            "repository add was interrupted before storage commit"
        )
        assert restarted_app.store.list_repositories() == []
    finally:
        # serve_forever was never entered, so close the bound listener and owner
        # explicitly to prove a later restart can reacquire both boundaries.
        restarted_service._server.server_close()
        restarted_app.close()

    final_app = application()
    final_service = HttpService(final_app, host, port, 60)
    final_service._server.server_close()
    final_app.close()


@pytest.mark.parametrize("workers", [0, -1, True, 1.5])
def test_application_config_rejects_invalid_repository_lookup_worker_capacity(
    tmp_path, workers
):
    with pytest.raises(
        ValueError, match="add_repository_lookup_max_workers must be a positive integer"
    ):
        ApplicationConfig(
            data_dir=tmp_path / "runtime",
            add_repository_lookup_max_workers=workers,
        )


def test_repository_metadata_lookup_workers_bound_hung_requests_and_recover_capacity(
    tmp_path,
):
    """Hung metadata calls consume only fixed capacity and late results never commit."""
    capacity = 2
    app, store, github, _ = make_app(
        tmp_path,
        add_repository_lookup_timeout=0.08,
        add_repository_lookup_max_workers=capacity,
    )
    release = threading.Event()
    started = threading.Barrier(capacity + 1)
    lookup_calls = 0
    lookup_lock = threading.Lock()

    def blocked_repository(github_repository):
        nonlocal lookup_calls
        with lookup_lock:
            lookup_calls += 1
            call = lookup_calls
        if call <= capacity:
            started.wait(timeout=2)
            assert release.wait(timeout=3)
        return {
            "full_name": github_repository,
            "default_branch": "main",
            "clone_url": f"https://github.test/{github_repository}.git",
        }

    github.repository = blocked_repository
    errors = []
    begun = time.monotonic()

    def add(index):
        try:
            app.add_repository(
                f"acme/hung-{index}", operation_id=f"bounded-hung-{index}"
            )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=add, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    started.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()

    elapsed = time.monotonic() - begun
    assert elapsed < 0.6
    assert len(errors) == 6
    assert all(isinstance(error, RepositoryLookupTimeoutError) for error in errors)
    assert lookup_calls == capacity
    assert len(app._repository_lookup_pool._workers) == capacity
    assert sum(worker.is_alive() for worker in app._repository_lookup_pool._workers) == capacity
    assert app._repository_lookup_pool._tasks.qsize() == 0
    assert store.list_repositories() == []
    assert all(
        app.repository_add_operation(f"bounded-hung-{index}")["state"] == "FAILED"
        for index in range(6)
    )

    # Releasing the fixed blocked calls cannot commit their abandoned results, but
    # it does return both slots for ordinary later repository additions.
    release.set()
    deadline = time.monotonic() + 2
    while lookup_calls < capacity or app._repository_lookup_pool._capacity._value < capacity:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert store.list_repositories() == []

    recovered = app.add_repository(
        "acme/recovered-capacity", operation_id="bounded-capacity-recovered"
    )
    assert recovered["github_repository"] == "acme/recovered-capacity"
    assert app.repository_add_operation("bounded-capacity-recovered")["state"] == "COMMITTED"
    app.close()


def test_repository_metadata_lookup_pool_stops_admission_after_application_close(tmp_path):
    app, store, github, _ = make_app(
        tmp_path,
        add_repository_lookup_timeout=0.1,
        add_repository_lookup_max_workers=1,
    )
    workers = list(app._repository_lookup_pool._workers)
    app.close()

    for worker in workers:
        worker.join(timeout=1)
        assert not worker.is_alive()
    with pytest.raises(RuntimeError, match="application is closed"):
        app.add_repository("acme/after-close", operation_id="after-close")
    assert store.list_repositories() == []


def test_repository_metadata_lookup_released_after_close_cannot_commit(tmp_path):
    app, store, github, _ = make_app(
        tmp_path,
        add_repository_lookup_timeout=1.0,
        add_repository_lookup_max_workers=1,
    )
    started = threading.Event()
    release = threading.Event()

    def blocked_repository(github_repository):
        started.set()
        assert release.wait(timeout=2)
        return {
            "full_name": github_repository,
            "default_branch": "main",
            "clone_url": f"https://github.test/{github_repository}.git",
        }

    github.repository = blocked_repository
    outcome = []

    def add():
        try:
            app.add_repository("acme/closing", operation_id="closing-lookup")
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=add)
    thread.start()
    assert started.wait(timeout=2)
    app.close()
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], RuntimeError)
    assert str(outcome[0]) == "application is closed"
    assert store.list_repositories() == []
    assert store.get_repository_add_operation("closing-lookup")["state"] == "FAILED"


def test_service_startup_recovers_pending_add_after_clock_rollback(tmp_path):
    """Ownership-gated startup recovery tolerates a host clock moving backward."""
    now = [1000.0]
    database = tmp_path / "rollback-startup.sqlite3"
    store = Store(database, clock=lambda: now[0])
    store.begin_repository_add_operation(
        "rollback-startup", "acme/rollback-startup", None
    )
    now[0] = 900.0
    app = Application(
        store,
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "rollback-runtime"),
    )
    service = HttpService(app, "127.0.0.1", 0, 60)
    try:
        recovered = app.repository_add_operation("rollback-startup")
        assert recovered["state"] == "FAILED"
        assert recovered["created_at"] == 1000.0
        assert recovered["updated_at"] == 1000.0
        assert recovered["terminal_at"] == 1000.0
        assert recovered["repository"] is None
        assert store.list_repositories() == []
    finally:
        service._server.server_close()
        app.close()


def test_failed_startup_recovery_releases_portable_service_ownership(tmp_path):
    """Recovery failure closes ownership so a later rightful owner can start."""
    database = tmp_path / "failed-recovery.sqlite3"
    data_dir = tmp_path / "failed-recovery-runtime"

    failed_app = Application(
        Store(database),
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=data_dir),
    )

    def fail_recovery():
        raise RuntimeError("recovery failed")

    failed_app.store.recover_interrupted_work = fail_recovery
    with pytest.raises(RuntimeError, match="recovery failed"):
        failed_app.acquire_service_ownership()
    assert failed_app._service_ownership is None
    failed_app.close()

    rightful_app = Application(
        Store(database),
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=data_dir),
    )
    rightful_app.acquire_service_ownership()
    assert rightful_app._service_ownership is not None
    rightful_app.close()


def test_borrowed_executor_workers_finish_before_service_ownership_release(tmp_path):
    """Close joins tracked work but leaves the injected executor caller-owned."""
    from repogents.service_ownership import (
        ServiceOwnership,
        ServiceOwnershipUnavailableError,
    )

    store = Store(tmp_path / "borrowed-worker.sqlite3")
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="borrowed-test")
    app = Application(
        store,
        FakeGitHub(),
        ScriptedRuntime(),
        SemanticRouter(MapEmbedder({})),
        ApplicationConfig(data_dir=tmp_path / "borrowed-runtime"),
        executor=executor,
    )
    app.acquire_service_ownership()
    ownership_path = app.data_dir / ".repogents-service.lock"
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    mutation_order = []
    order_guard = threading.Lock()

    def mutation_capable_work():
        worker_started.set()
        assert release_worker.wait(timeout=3)
        repository = store.add_repository("acme/old-generation", "main", 0.75)
        with order_guard:
            mutation_order.append("old-worker")
        worker_finished.set()
        return repository

    future = executor.submit(mutation_capable_work)
    with app._worker_lock:
        app._workers[101] = future
    assert worker_started.wait(timeout=2)

    close_thread = threading.Thread(target=app.close)
    concurrent_close_thread = threading.Thread(target=app.close)
    close_thread.start()
    concurrent_close_thread.start()
    close_thread.join(timeout=0.05)
    concurrent_close_thread.join(timeout=0.05)
    assert close_thread.is_alive()
    assert concurrent_close_thread.is_alive()
    assert app._service_ownership is not None

    competitor = ServiceOwnership(ownership_path)
    with pytest.raises(ServiceOwnershipUnavailableError):
        competitor.acquire()

    release_worker.set()
    close_thread.join(timeout=2)
    concurrent_close_thread.join(timeout=2)
    assert not close_thread.is_alive()
    assert not concurrent_close_thread.is_alive()
    assert worker_finished.is_set()
    assert app._close_complete.is_set()
    assert app._service_ownership is None
    assert app._workers == {}

    replacement = ServiceOwnership(ownership_path)
    replacement.acquire()
    with order_guard:
        mutation_order.append("replacement-acquired")
    assert [item["github_repository"] for item in store.list_repositories()] == [
        "acme/old-generation"
    ]
    assert mutation_order == ["old-worker", "replacement-acquired"]

    # Application.close does not take lifecycle ownership of the borrowed pool.
    assert executor.submit(lambda: "still-usable").result(timeout=2) == "still-usable"
    replacement.close()
    executor.shutdown(wait=True)
