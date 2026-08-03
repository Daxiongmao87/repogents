from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque

import pytest
from pathlib import Path

from repogents.application import Application, ApplicationConfig
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
