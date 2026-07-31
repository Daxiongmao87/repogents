from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest import mock

from repogents.app import Orchestrator

from repogents.database import Database
from repogents.execution import ExecutionService, ScriptedRuntime
from repogents.feedback import (
    FeedbackDecision,
    FeedbackService,
    MiniSweFeedbackEvaluator,
)
from repogents.github import FeedbackItem, FeedbackOutput, PullRequestInfo
from repogents.lifecycle import RunLifecycle, RunState
from repogents.team import TeamService
from repogents.publication import PublicationBaseChanged
from repogents.sandbox import RunLayout, SandboxManager
from repogents.specification import SpecificationService


class NoActivationClient:
    def list_ready_events(self, owner: str, name: str) -> list[object]:
        return []

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        return "a" * 40

    issue_state = "open"

    def get_issue(self, owner: str, name: str, number: int) -> object:
        return SimpleNamespace(
            node_id="I1",
            number=number,
            url=f"https://github.com/{owner}/{name}/issues/{number}",
            title="Issue",
            body="Body",
            discussion=(),
            updated_at="2026-01-01T00:08:00Z",
            state=self.issue_state,
        )


class NoCheckoutManager:
    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        return None


class FeedbackLifecycle(RunLifecycle):
    def transition(
        self,
        run_id: str,
        target: RunState,
        *,
        reason: str | None = None,
    ) -> None:
        run = self.get_run(run_id)
        if run["state"] == RunState.PUBLISHING.value and target == RunState.CLOSED:
            if not (reason or "").strip():
                raise ValueError("closed transition requires a reason")
            now = "2026-01-01T00:07:00Z"
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE runs
                       SET state='closed', last_completed_state='publishing',
                           reason=?, updated_at=?, closed_at=?
                       WHERE id=?""",
                    (reason, now, now, run_id),
                )
                connection.execute(
                    """INSERT INTO run_transitions
                       (run_id, from_state, to_state, reason, occurred_at)
                       VALUES (?, 'publishing', 'closed', ?, ?)""",
                    (run_id, reason, now),
                )
            return
        super().transition(run_id, target, reason=reason)


class FakeFeedbackGateway:
    def __init__(self) -> None:
        self.items: list[FeedbackItem] = []
        self.outputs: list[FeedbackOutput] = []
        self.post_calls = 0
        self.polls = 0
        self.status_calls = 0
        self.application_author = "configured-user"
        self.pull = PullRequestInfo(
            node_id="PR1",
            number=11,
            url="pr-url",
            state="open",
            merged=False,
            head_branch="agent/issue-3-run-1",
            head_sha="b" * 40,
            base_branch="main",
            base_sha="a" * 40,
            mergeable=True,
            updated_at="2026-01-01T00:00:00Z",
        )
        self.crash_after_post = False
        self.inject_after_first_poll: FeedbackItem | None = None
        self.resolve_thread_calls: list[str] = []
        self.resolve_thread_head_shas: list[str] = []
        self.resolve_thread_output_counts: list[int] = []
        self.fail_before_thread_resolution = False
        self.crash_after_thread_resolution = False
        self.remote_base_head: str | None = None
        self.base_head_reads: list[tuple[str, str, str]] = []

    def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestInfo:
        self.status_calls += 1
        return self.pull

    def get_remote_branch_head(
        self, owner: str, name: str, branch: str
    ) -> str | None:
        self.base_head_reads.append((owner, name, branch))
        if self.remote_base_head is not None:
            return self.remote_base_head
        return self.pull.base_sha

    def application_login(self) -> str:
        return self.application_author

    def list_feedback(
        self, owner: str, name: str, pull_number: int
    ) -> list[FeedbackItem]:
        self.polls += 1
        values = list(self.items)
        if self.polls >= 2 and self.inject_after_first_poll is not None:
            values.append(self.inject_after_first_poll)
        return values

    def find_response(
        self,
        owner: str,
        name: str,
        pull_number: int,
        feedback: FeedbackItem,
        body: str,
        attempted_at: str,
    ) -> FeedbackOutput | None:
        return next(
            (
                output
                for output in self.outputs
                if output.target_object_id == feedback.object_id and output.body == body
            ),
            None,
        )

    def post_response(
        self,
        owner: str,
        name: str,
        pull_number: int,
        feedback: FeedbackItem,
        body: str,
    ) -> FeedbackOutput:
        self.post_calls += 1
        output = FeedbackOutput(
            feedback_type=(
                "inline_comment"
                if feedback.feedback_type == "inline_comment"
                else "comment"
            ),
            object_id=f"output-{self.post_calls}",
            target_object_id=feedback.object_id,
            body=body,
            url=f"output-url-{self.post_calls}",
            created_at="2026-01-01T00:05:00Z",
        )
        self.outputs.append(output)
        if self.crash_after_post:
            self.crash_after_post = False
            raise RuntimeError("connection dropped after response was accepted")
        return output
    def resolve_review_thread(self, thread_id: str) -> None:
        self.resolve_thread_calls.append(thread_id)
        self.resolve_thread_head_shas.append(self.pull.head_sha)
        self.resolve_thread_output_counts.append(len(self.outputs))
        if self.fail_before_thread_resolution:
            self.fail_before_thread_resolution = False
            raise RuntimeError("connection dropped before thread was resolved")
        self.items = [
            (
                replace(item, review_thread_resolved=True)
                if item.review_thread_id == thread_id
                else item
            )
            for item in self.items
        ]
        if self.crash_after_thread_resolution:
            self.crash_after_thread_resolution = False
            raise RuntimeError("connection dropped after thread was resolved")



class FakeEvaluator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def evaluate(self, context: dict[str, object]) -> FeedbackDecision:
        body = str(context["feedback"]["body"])
        self.calls.append(body)
        if "change" in body.lower():
            return FeedbackDecision(
                "revise",
                "valid in-scope change",
                "Implemented and validated the requested change.",
            )
        if "?" in body:
            return FeedbackDecision(
                "answer",
                "relevant question",
                "The behavior follows the repository contract.",
            )
        if "wrong" in body.lower():
            return FeedbackDecision(
                "decline",
                "request contradicts issue scope",
                "Declined because it contradicts the issue scope.",
            )
        return FeedbackDecision(
            "answer", "relevant feedback", "Acknowledged and resolved."
        )


class FakeExecutor:
    def __init__(self, lifecycle: RunLifecycle) -> None:
        self.lifecycle = lifecycle
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.validation_only_calls: list[bool] = []
        self.source_sha = "c" * 40
        self.after_execute: Callable[[], None] | None = None

    def execute(
        self,
        run_id: str,
        *,
        additional_context: str | None = None,
        comparison_base_sha: str | None = None,
    ) -> str:
        self.calls.append((run_id, additional_context, comparison_base_sha))
        self.validation_only_calls.append(
            RunState(str(self.lifecycle.get_run(run_id)["state"]))
            == RunState.VALIDATING
        )
        if self.after_execute is not None:
            callback = self.after_execute
            self.after_execute = None
            callback()
        if RunState(str(self.lifecycle.get_run(run_id)["state"])) != RunState.VALIDATING:
            self.lifecycle.transition(run_id, RunState.VALIDATING)
        with self.lifecycle.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id=?",
                (self.source_sha, run_id),
            )
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        return self.source_sha


class FakePublisher:
    def __init__(
        self,
        database: Database,
        lifecycle: RunLifecycle,
        gateway: FakeFeedbackGateway,
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle
        self.gateway = gateway
        self.calls: list[str] = []
        self.source_shas_at_call: list[str | None] = []
        self.fail_once = False
        self.prepared_bases: list[tuple[str, str]] = []
        self.base_change_once: str | None = None
        self.revision_required_once = False

    def prepare_base_revision(self, run_id: str, expected_base_sha: str) -> str:
        self.prepared_bases.append((run_id, expected_base_sha))
        if self.base_change_once is not None:
            actual_base_sha = self.base_change_once
            self.base_change_once = None
            self.gateway.remote_base_head = actual_base_sha
            raise PublicationBaseChanged(expected_base_sha, actual_base_sha)
        return expected_base_sha

    def publish(self, run_id: str) -> object | None:
        self.calls.append(run_id)
        with self.database.connect() as connection:
            feedback = connection.execute(
                """SELECT source_sha FROM feedback_versions
                   JOIN pull_requests
                     ON pull_requests.id=feedback_versions.pull_request_id
                   WHERE pull_requests.run_id=? AND feedback_versions.state='processing'""",
                (run_id,),
            ).fetchone()
        self.source_shas_at_call.append(
            str(feedback["source_sha"]) if feedback and feedback["source_sha"] else None
        )
        if self.revision_required_once:
            self.revision_required_once = False
            self.lifecycle.transition(
                run_id,
                RunState.IMPLEMENTING,
                reason=(
                    "publication revision required: "
                    "scope review rejected publication"
                ),
            )
            return None
        if self.lifecycle.get_run(run_id)["state"] != RunState.PUBLISHING.value:
            return None
        if self.fail_once:
            self.fail_once = False
            return None
        with self.database.transaction() as connection:
            validated_sha = str(
                connection.execute(
                    "SELECT validated_sha FROM runs WHERE id=?", (run_id,)
                ).fetchone()["validated_sha"]
            )
            connection.execute(
                """UPDATE pull_requests SET validated_head_sha=?, remote_head_sha=?, updated_at=?
                   WHERE run_id=?""",
                (
                    validated_sha,
                    validated_sha,
                    "2026-01-01T00:06:00Z",
                    run_id,
                ),
            )
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        self.gateway.pull = replace(
            self.gateway.pull,
            head_sha=validated_sha,
            mergeable=True,
            updated_at="2026-01-01T00:06:00Z",
        )
        return object()


class MiniSweFeedbackEvaluatorTests(unittest.TestCase):
    def test_passes_stored_model_base_url_state_directory_supervisor_and_run_id(
        self,
    ) -> None:
        observed: dict[str, object] = {}
        state_root = Path("/model-state/feedback")

        class FakeSupervisor:
            pass

        supervisor = FakeSupervisor()

        def fake_infer(
            self,
            *,
            system_prompt: str,
            prompt: str,
            response_schema: dict,
            state_directory: Path,
        ) -> dict:
            observed["model"] = self.model
            observed["base_url"] = self.base_url
            observed["timeout"] = self.timeout
            observed["supervisor"] = self.supervisor
            observed["run_id"] = self.run_id
            observed["system_prompt"] = system_prompt
            observed["prompt"] = prompt
            observed["response_schema"] = response_schema
            observed["state_directory"] = state_directory
            return {"action": "answer", "reason": "relevant", "response": "Done."}

        with (
            mock.patch.object(
                MiniSweFeedbackEvaluator,
                "_build_inference",
                side_effect=lambda self_: self_,
            ),
            mock.patch(
                "repogents.feedback.MiniSweInference.infer",
                autospec=True,
                side_effect=fake_infer,
            ),
        ):
            evaluator = MiniSweFeedbackEvaluator(
                base_url="https://custom.example.com/v1",
                state_root=state_root,
                processes=supervisor,
            )
            decision = evaluator.evaluate(
                {
                    "run_id": "run-42",
                    "stored_lead": {
                        "runtime": "mini-swe-agent",
                        "model": "openai/gpt-4.1",
                    },
                    "feedback": {"body": "Question?"},
                }
            )

        self.assertEqual(decision.action, "answer")
        self.assertEqual(decision.reason, "relevant")
        self.assertEqual(decision.response, "Done.")
        self.assertEqual(observed["model"], "openai/gpt-4.1")
        self.assertEqual(observed["base_url"], "https://custom.example.com/v1")
        self.assertEqual(observed["timeout"], 600)
        self.assertEqual(observed["supervisor"], supervisor)
        self.assertEqual(observed["run_id"], "run-42")
        self.assertTrue(observed["state_directory"].is_absolute())
        self.assertIn("feedback", str(observed["state_directory"]))
        prompt_data = json.loads(observed["prompt"])
        self.assertIn("response_schema", prompt_data)
        self.assertEqual(
            prompt_data["response_schema"]["action"], "revise|answer|decline|ignore"
        )
        routing = json.dumps(prompt_data["routing"])
        self.assertIn("context.application_login", routing)
        self.assertIn("leading @login", routing)
        self.assertIn("concrete pull-request", routing)
        self.assertIn("begins with another account mention", routing)
        self.assertIn("Never invent a review or status summary", routing)
        self.assertTrue(
            "Return exactly one JSON object" in observed["system_prompt"]
            or "Return one JSON" in observed["system_prompt"]
        )

    def test_prompt_is_file_backed_and_not_in_system_prompt(self) -> None:
        observed: dict[str, object] = {}
        large_feedback = {"body": "x" * 100_000}

        def fake_infer(
            self,
            *,
            system_prompt: str,
            prompt: str,
            response_schema: dict,
            state_directory: Path,
        ) -> dict:
            observed["system_prompt"] = system_prompt
            observed["prompt"] = prompt
            return {"action": "answer", "reason": "ok", "response": "Handled."}

        with mock.patch(
            "repogents.feedback.MiniSweInference.infer",
            autospec=True,
            side_effect=fake_infer,
        ):
            evaluator = MiniSweFeedbackEvaluator()
            evaluator.evaluate(
                {
                    "run_id": "run-1",
                    "stored_lead": {
                        "runtime": "mini-swe-agent",
                        "model": "openai/gpt-4.1",
                    },
                    "feedback": large_feedback,
                }
            )

        self.assertIn("x" * 100, observed["prompt"])
        self.assertNotIn("x" * 100, observed["system_prompt"])
        self.assertLess(len(observed["system_prompt"]), 1_000)

    def test_rejects_obsolete_omp_runtime(self) -> None:
        evaluator = MiniSweFeedbackEvaluator()
        with self.assertRaises(RuntimeError) as raised:
            evaluator.evaluate(
                {
                    "stored_lead": {
                        "runtime": "omp",
                        "model": "openai/gpt-3.5-turbo",
                    },
                    "feedback": {"body": "Hello"},
                }
            )
        self.assertIn("omp", str(raised.exception))

    def test_requires_explicit_model_when_no_constructor_or_stored_override(
        self,
    ) -> None:
        evaluator = MiniSweFeedbackEvaluator()
        with self.assertRaises(RuntimeError) as raised:
            evaluator.evaluate({"feedback": {"body": "Hello"}})
        self.assertIn("model", str(raised.exception).lower())

    def test_persists_durable_state_directory_per_run(self) -> None:
        state_root = Path("/model-state/feedback")
        observed_dirs: list[Path] = []

        def fake_infer(
            self,
            *,
            system_prompt: str,
            prompt: str,
            response_schema: dict,
            state_directory: Path,
        ) -> dict:
            observed_dirs.append(state_directory)
            return {"action": "ignore", "reason": "n/a", "response": ""}

        with mock.patch(
            "repogents.feedback.MiniSweInference.infer",
            autospec=True,
            side_effect=fake_infer,
        ):
            evaluator = MiniSweFeedbackEvaluator(state_root=state_root)
            evaluator.evaluate(
                {
                    "run_id": "run-alpha",
                    "stored_lead": {
                        "runtime": "mini-swe-agent",
                        "model": "openai/gpt-4.1",
                    },
                    "feedback": {"body": "First"},
                }
            )
            evaluator.evaluate(
                {
                    "run_id": "run-beta",
                    "stored_lead": {
                        "runtime": "mini-swe-agent",
                        "model": "openai/gpt-4.1",
                    },
                    "feedback": {"body": "Second"},
                }
            )

        self.assertEqual(len(observed_dirs), 2)
        self.assertNotEqual(observed_dirs[0], observed_dirs[1])
        self.assertIn("alpha", str(observed_dirs[0]))
        self.assertIn("beta", str(observed_dirs[1]))


class FeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        now = "2026-01-01T00:00:00Z"
        sandbox_root = self.data_root / "repositories" / "repo-1" / "sandbox" / "1"
        sandbox_root.mkdir(parents=True)
        checkout = (
            self.data_root / "repositories" / "repo-1" / "runs" / "run-1" / "checkout"
        )
        checkout.mkdir(parents=True)
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'repo-url', 'main', 'ready', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, ?, '{}', '{}', ?)""",
                (str(sandbox_root), now),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, '{}', ?)""",
                (now,),
            )
            connection.execute("""UPDATE repositories
                   SET current_sandbox_version_id='sandbox-1',
                       current_team_version_id='team-1'
                   WHERE id='repo-1'""")
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own', '[]',
                           'mini-swe-agent', 'openai/gpt-stored', '')""")
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 3, 'issue-url', 'Issue', 'Body', '[]', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-1', 'repo-1', 'issue-1', 'event-1', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, last_completed_state, validated_sha,
                    checkout_path, run_path, created_at, updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'activation-1',
                           'sandbox-1', 'team-1', 'main', ?, 'waiting_for_feedback',
                           'publishing', ?, ?, ?, ?, ?)""",
                ("a" * 40, "b" * 40, str(checkout), str(checkout.parent), now, now),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-1', 'run-1', 'PR1', 11, 'pr-url', 'agent/issue-3-run-1',
                           'main', ?, ?, ?, 'open', ?, ?)""",
                ("a" * 40, "b" * 40, "b" * 40, now, now),
            )
        self.sandbox = SandboxManager()
        self.lifecycle = FeedbackLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )
        self.gateway = FakeFeedbackGateway()
        self.evaluator = FakeEvaluator()
        self.executor = FakeExecutor(self.lifecycle)
        self.publisher = FakePublisher(self.db, self.lifecycle, self.gateway)
        self.service = FeedbackService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            evaluator=self.evaluator,
            executor=self.executor,
            publisher=self.publisher,
        )
        self._seed_commit_history()

    def _seed_commit_history(self) -> tuple[Path, str, str]:
        checkout = Path(str(self.lifecycle.get_run("run-1")["checkout_path"]))
        if (checkout / ".git").is_dir():
            with self.db.connect() as connection:
                row = connection.execute(
                    "SELECT base_sha, validated_sha FROM runs WHERE id='run-1'"
                ).fetchone()
            return checkout, str(row["base_sha"]), str(row["validated_sha"])

        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", *arguments],
                cwd=checkout,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()

        git("init", "-q", "-b", "main")
        (checkout / "app.py").write_text(
            "".join(f"line {number} original\n" for number in range(1, 16)),
            encoding="utf-8",
        )
        (checkout / "deleted.txt").write_text("removed source\n", encoding="utf-8")
        git("add", "-A")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "base",
        )
        base_sha = git("rev-parse", "HEAD")
        lines = (checkout / "app.py").read_text(encoding="utf-8").splitlines()
        lines[9] = "line 10 corrected implementation"
        (checkout / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (checkout / "deleted.txt").unlink()
        (checkout / "binary.bin").write_bytes(b"\x00\xff\x01")
        git("add", "-A")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "validated",
        )
        validated_sha = git("rev-parse", "HEAD")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET base_sha=?, validated_sha=? WHERE id='run-1'",
                (base_sha, validated_sha),
            )
            connection.execute(
                """UPDATE pull_requests
                   SET base_sha=?, validated_head_sha=?, remote_head_sha=?
                   WHERE run_id='run-1'""",
                (base_sha, validated_sha, validated_sha),
            )
        self.gateway.pull = replace(self.gateway.pull, head_sha=validated_sha)
        return checkout, base_sha, validated_sha

    @staticmethod
    def item(
        feedback_type: str,
        object_id: str,
        version: str,
        body: str,
        *,
        author: str = "reviewer",
        review_thread_id: str | None = None,
        review_thread_resolved: bool | None = None,
    ) -> FeedbackItem:
        return FeedbackItem(
            feedback_type=feedback_type,
            object_id=object_id,
            version=version,
            author=author,
            body=body,
            path="app.py" if feedback_type == "inline_comment" else None,
            line=10 if feedback_type == "inline_comment" else None,
            url=f"url-{object_id}",
            created_at="2026-01-01T00:01:00Z",
            updated_at=version,
            review_thread_id=review_thread_id,
            review_thread_resolved=review_thread_resolved,
        )

    def test_command_addressed_to_other_account_is_ignored_without_work(
        self,
    ) -> None:
        self.gateway.items = [
            self.item(
                "comment",
                "codex-command",
                "v1",
                "@codex review",
                author="configured-user",
            ),
            self.item(
                "comment",
                "codex-address-command",
                "v1",
                "@codex address that feedback",
                author="configured-user",
            ),
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 2)

        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT state, decision_json FROM feedback_versions
                   ORDER BY github_object_id"""
            ).fetchall()
            operation_count = connection.execute(
                "SELECT COUNT(*) FROM outbound_operations"
            ).fetchone()[0]
        decisions = [json.loads(str(row["decision_json"])) for row in rows]
        self.assertEqual([row["state"] for row in rows], ["resolved", "resolved"])
        self.assertEqual(
            [decision["action"] for decision in decisions],
            ["ignore", "ignore"],
        )
        self.assertEqual(
            [decision["response"] for decision in decisions],
            ["", ""],
        )
        self.assertEqual(self.evaluator.calls, [])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self.gateway.post_calls, 0)
        self.assertEqual(operation_count, 0)

    def test_configured_addressee_and_incidental_mention_remain_evaluable(
        self,
    ) -> None:
        self.gateway.items = [
            self.item(
                "comment",
                "addressed-question",
                "v1",
                "@configured-user why is this correct?",
            ),
            self.item(
                "comment",
                "incidental-mention",
                "v1",
                "Why does the note from @alice matter?",
            ),
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 2)

        self.assertEqual(
            self.evaluator.calls,
            [
                "@configured-user why is this correct?",
                "Why does the note from @alice matter?",
            ],
        )
        self.assertEqual(self.gateway.post_calls, 2)

    def test_actionable_feedback_with_other_leading_addressee_is_evaluated(
        self,
    ) -> None:
        body = "@alice, please change the null handling."
        self.gateway.items = [
            self.item(
                "comment",
                "other-addressee-actionable",
                "v1",
                body,
            )
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 1)

        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT state, decision_json FROM feedback_versions"
            ).fetchone()
        decision = json.loads(str(row["decision_json"]))
        self.assertEqual(row["state"], "resolved")
        self.assertEqual(decision["action"], "revise")
        self.assertEqual(self.evaluator.calls, [body])
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.publisher.calls, ["run-1"])
        self.assertEqual(self.executor.validation_only_calls, [False])

    def test_validation_base_recovery_skips_stale_agent_replay(self) -> None:
        self.lifecycle.transition("run-1", RunState.RESOLVING_FEEDBACK)
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET last_completed_state='validating',
                       reason=?
                   WHERE id='run-1'""",
                (
                    "automatic feedback validation retry "
                    "against prepared base",
                ),
            )
        self.gateway.items = [
            self.item(
                "inline_comment",
                "validation-base-recovery",
                "v1",
                "Please change this behavior.",
            )
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 1)

        self.assertEqual(self.executor.validation_only_calls, [True])
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.publisher.calls, ["run-1"])

    def test_validation_base_recovery_survives_publishing_interruption(
        self,
    ) -> None:
        recovery_reason = (
            "automatic feedback validation retry against prepared base "
            "without agent replay"
        )
        self.lifecycle.transition("run-1", RunState.RESOLVING_FEEDBACK)
        self.lifecycle.transition(
            "run-1",
            RunState.VALIDATING,
            reason=recovery_reason,
        )
        self.lifecycle.transition(
            "run-1",
            RunState.PUBLISHING,
            reason=recovery_reason,
        )
        self.gateway.items = [
            self.item(
                "inline_comment",
                "interrupted-validation-base-recovery",
                "v1",
                "Please change this behavior.",
            )
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 1)

        self.assertEqual(self.executor.validation_only_calls, [True])
        with self.db.connect() as connection:
            transitions = connection.execute(
                """SELECT from_state, to_state
                   FROM run_transitions
                   WHERE run_id='run-1' AND reason=?
                   ORDER BY occurred_at""",
                (recovery_reason,),
            ).fetchall()
        self.assertEqual(
            [(row["from_state"], row["to_state"]) for row in transitions],
            [
                ("resolving_feedback", "validating"),
                ("validating", "publishing"),
                ("publishing", "implementing"),
                ("implementing", "validating"),
            ],
        )

    def test_publication_revision_reopens_feedback_batch_after_restart(
        self,
    ) -> None:
        self.publisher.revision_required_once = True
        self.gateway.items = [
            self.item(
                "inline_comment",
                "publication-revision",
                "v1",
                "Please change this behavior.",
            )
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 0)

        with self.db.connect() as connection:
            initial_feedback = connection.execute(
                """SELECT state, source_sha FROM feedback_versions
                   WHERE github_object_id='publication-revision'"""
            ).fetchone()
            initial_operation = connection.execute(
                """SELECT id, state, external_id
                   FROM outbound_operations
                   WHERE kind='feedback_revision_batch'"""
            ).fetchone()
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            RunState.IMPLEMENTING.value,
        )
        self.assertEqual(initial_feedback["state"], "processing")
        self.assertEqual(initial_feedback["source_sha"], "c" * 40)
        self.assertEqual(initial_operation["state"], "completed")
        self.assertEqual(initial_operation["external_id"], "c" * 40)

        restarted_executor = FakeExecutor(self.lifecycle)
        restarted_executor.source_sha = "d" * 40
        reopened: dict[str, object] = {}

        def capture_reopened_batch() -> None:
            with self.db.connect() as connection:
                feedback = connection.execute(
                    """SELECT source_sha FROM feedback_versions
                       WHERE github_object_id='publication-revision'"""
                ).fetchone()
                operation = connection.execute(
                    """SELECT state, external_id, completed_at
                       FROM outbound_operations
                       WHERE kind='feedback_revision_batch'"""
                ).fetchone()
            reopened.update(
                source_sha=feedback["source_sha"],
                operation_state=operation["state"],
                external_id=operation["external_id"],
                completed_at=operation["completed_at"],
            )

        restarted_executor.after_execute = capture_reopened_batch
        restarted_publisher = FakePublisher(
            self.db,
            self.lifecycle,
            self.gateway,
        )
        restarted_evaluator = FakeEvaluator()
        restarted = FeedbackService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            evaluator=restarted_evaluator,
            executor=restarted_executor,
            publisher=restarted_publisher,
        )

        self.assertEqual(restarted.resolve_run("run-1"), 1)

        with self.db.connect() as connection:
            revised_feedback = connection.execute(
                """SELECT state, source_sha FROM feedback_versions
                   WHERE github_object_id='publication-revision'"""
            ).fetchone()
            revised_operations = connection.execute(
                """SELECT id, state, external_id
                   FROM outbound_operations
                   WHERE kind='feedback_revision_batch'"""
            ).fetchall()
        self.assertEqual(
            reopened,
            {
                "source_sha": None,
                "operation_state": "pending",
                "external_id": None,
                "completed_at": None,
            },
        )
        self.assertEqual(restarted_evaluator.calls, [])
        self.assertEqual(len(restarted_executor.calls), 1)
        self.assertEqual(restarted_publisher.calls, ["run-1"])
        self.assertEqual(restarted_publisher.source_shas_at_call, ["d" * 40])
        self.assertEqual(revised_feedback["state"], "resolved")
        self.assertEqual(revised_feedback["source_sha"], "d" * 40)
        self.assertEqual(len(revised_operations), 1)
        self.assertEqual(
            revised_operations[0]["id"],
            initial_operation["id"],
        )
        self.assertEqual(revised_operations[0]["state"], "completed")
        self.assertEqual(revised_operations[0]["external_id"], "d" * 40)

    def test_resolved_inline_thread_is_ignored_without_work(self) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "resolved-inline",
                "v1",
                "Change this implementation.",
                review_thread_id="THREAD-resolved",
                review_thread_resolved=True,
            )
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 1)

        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT state, decision_json FROM feedback_versions"
            ).fetchone()
            operation_count = connection.execute(
                "SELECT COUNT(*) FROM outbound_operations"
            ).fetchone()[0]
        decision = json.loads(str(row["decision_json"]))
        self.assertEqual(row["state"], "resolved")
        self.assertEqual(decision["action"], "ignore")
        self.assertEqual(decision["response"], "")
        self.assertEqual(self.evaluator.calls, [])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self.gateway.resolve_thread_calls, [])
        self.assertEqual(self.gateway.post_calls, 0)
        self.assertEqual(operation_count, 0)

    def test_unknown_mergeability_does_not_create_conflict_feedback(
        self,
    ) -> None:
        self.gateway.pull = replace(
            self.gateway.pull,
            base_sha="d" * 40,
            mergeable=None,
        )

        self.assertEqual(self.service.poll_run("run-1"), 0)

        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM feedback_versions"
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            RunState.WAITING_FOR_FEEDBACK.value,
        )

    def test_idle_open_pull_does_not_churn_waiting_state(self) -> None:
        with self.db.connect() as connection:
            before = connection.execute(
                "SELECT updated_at FROM runs WHERE id='run-1'"
            ).fetchone()[0]
            transitions_before = connection.execute(
                "SELECT COUNT(*) FROM run_transitions WHERE run_id='run-1'"
            ).fetchone()[0]

        self.assertEqual(self.service.resolve_run("run-1"), 0)

        with self.db.connect() as connection:
            after = connection.execute(
                "SELECT updated_at FROM runs WHERE id='run-1'"
            ).fetchone()[0]
            transitions_after = connection.execute(
                "SELECT COUNT(*) FROM run_transitions WHERE run_id='run-1'"
            ).fetchone()[0]
            quiet_count = connection.execute(
                "SELECT COUNT(*) FROM quiet_periods"
            ).fetchone()[0]
            notification_count = connection.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0]
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            RunState.WAITING_FOR_FEEDBACK.value,
        )
        self.assertEqual(after, before)
        self.assertEqual(transitions_after, transitions_before)
        self.assertEqual(quiet_count, 0)
        self.assertEqual(notification_count, 0)

    def test_later_feedback_after_idle_poll_uses_same_run(self) -> None:
        self.assertEqual(self.service.poll_run("run-1"), 0)
        self.gateway.items = [
            self.item(
                "comment",
                "later-feedback",
                "v1",
                "Why does this remain active?",
            )
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 1)

        with self.db.connect() as connection:
            run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            pull_count = connection.execute(
                "SELECT COUNT(*) FROM pull_requests WHERE run_id='run-1'"
            ).fetchone()[0]
            feedback_state = connection.execute("""SELECT state FROM feedback_versions
                   WHERE github_object_id='later-feedback'""").fetchone()[0]
            quiet_count = connection.execute(
                "SELECT COUNT(*) FROM quiet_periods"
            ).fetchone()[0]
        self.assertEqual(run_count, 1)
        self.assertEqual(pull_count, 1)
        self.assertEqual(feedback_state, "answered")
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            RunState.WAITING_FOR_FEEDBACK.value,
        )
        self.assertEqual(quiet_count, 0)

    def test_review_thread_resolution_reconciles_after_accepted_mutation_crash(
        self,
    ) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "thread-feedback",
                "v1",
                "Please change this behavior.",
                review_thread_id="THREAD-1",
                review_thread_resolved=False,
            )
        ]
        self.gateway.crash_after_thread_resolution = True

        with self.assertRaisesRegex(
            RuntimeError,
            "connection dropped after thread was resolved",
        ):
            self.service.resolve_run("run-1")

        with self.db.connect() as connection:
            first = connection.execute(
                """SELECT runs.state, feedback_versions.state AS feedback_state,
                          feedback_versions.review_thread_resolved,
                          outbound_operations.state AS operation_state
                   FROM runs
                   JOIN pull_requests ON pull_requests.run_id=runs.id
                   JOIN feedback_versions
                     ON feedback_versions.pull_request_id=pull_requests.id
                   JOIN outbound_operations
                     ON outbound_operations.run_id=runs.id
                    AND outbound_operations.kind='resolve_review_thread'
                   WHERE runs.id='run-1'"""
            ).fetchone()
        self.assertEqual(
            tuple(first),
            ("resolving_feedback", "resolved", 0, "pending"),
        )
        self.assertEqual(self.gateway.resolve_thread_calls, ["THREAD-1"])
        self.assertEqual(self.gateway.resolve_thread_head_shas, ["c" * 40])

        restarted_database = Database(self.root / "db.sqlite3")
        restarted_database.initialize()
        restarted_lifecycle = FeedbackLifecycle(
            database=restarted_database,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )
        restarted = FeedbackService(
            database=restarted_database,
            lifecycle=restarted_lifecycle,
            gateway=self.gateway,
            evaluator=self.evaluator,
            executor=FakeExecutor(restarted_lifecycle),
            publisher=FakePublisher(
                restarted_database,
                restarted_lifecycle,
                self.gateway,
            ),
        )

        self.assertEqual(restarted.resolve_run("run-1"), 0)

        with restarted_database.connect() as connection:
            final = connection.execute(
                """SELECT runs.state, feedback_versions.review_thread_resolved,
                          outbound_operations.state
                   FROM runs
                   JOIN pull_requests ON pull_requests.run_id=runs.id
                   JOIN feedback_versions
                     ON feedback_versions.pull_request_id=pull_requests.id
                   JOIN outbound_operations
                     ON outbound_operations.run_id=runs.id
                    AND outbound_operations.kind='resolve_review_thread'
                   WHERE runs.id='run-1'"""
            ).fetchone()
        self.assertEqual(tuple(final), ("waiting_for_feedback", 1, "completed"))
        self.assertEqual(self.gateway.resolve_thread_calls, ["THREAD-1"])

    def test_one_thread_resolution_waits_for_all_local_responses(self) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "thread-question",
                "v1",
                "Why does this behave this way?",
                review_thread_id="THREAD-SHARED",
                review_thread_resolved=False,
            ),
            self.item(
                "inline_comment",
                "thread-decline",
                "v1",
                "This is wrong",
                review_thread_id="THREAD-SHARED",
                review_thread_resolved=False,
            ),
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 2)

        with self.db.connect() as connection:
            states = tuple(
                row[0]
                for row in connection.execute(
                    """SELECT state FROM feedback_versions
                       ORDER BY github_object_id"""
                )
            )
            operations = connection.execute(
                """SELECT COUNT(*), MIN(state)
                   FROM outbound_operations
                   WHERE kind='resolve_review_thread'"""
            ).fetchone()
        self.assertEqual(states, ("declined", "answered"))
        self.assertEqual(tuple(operations), (1, "completed"))
        self.assertEqual(self.gateway.post_calls, 2)
        self.assertEqual(self.gateway.resolve_thread_calls, ["THREAD-SHARED"])
        self.assertEqual(self.gateway.resolve_thread_output_counts, [2])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.publisher.calls, [])

    def test_review_thread_resolution_retries_after_pre_mutation_crash(
        self,
    ) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "pre-mutation-crash",
                "v1",
                "Please change this behavior.",
                review_thread_id="THREAD-RETRY",
                review_thread_resolved=False,
            )
        ]
        self.gateway.fail_before_thread_resolution = True

        with self.assertRaisesRegex(
            RuntimeError,
            "connection dropped before thread was resolved",
        ):
            self.service.resolve_run("run-1")
        self.assertEqual(self.gateway.resolve_thread_calls, ["THREAD-RETRY"])
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "resolving_feedback")
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.publisher.calls, ["run-1"])

        restarted_database = Database(self.root / "db.sqlite3")
        restarted_database.initialize()
        restarted_lifecycle = FeedbackLifecycle(
            database=restarted_database,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )
        restarted_executor = FakeExecutor(restarted_lifecycle)
        restarted_publisher = FakePublisher(
            restarted_database,
            restarted_lifecycle,
            self.gateway,
        )
        restarted = FeedbackService(
            database=restarted_database,
            lifecycle=restarted_lifecycle,
            gateway=self.gateway,
            evaluator=self.evaluator,
            executor=restarted_executor,
            publisher=restarted_publisher,
        )

        self.assertEqual(restarted.resolve_run("run-1"), 0)

        with restarted_database.connect() as connection:
            operation = connection.execute(
                """SELECT COUNT(*), MIN(state)
                   FROM outbound_operations
                   WHERE kind='resolve_review_thread'"""
            ).fetchone()
        self.assertEqual(tuple(operation), (1, "completed"))
        self.assertEqual(
            self.gateway.resolve_thread_calls,
            ["THREAD-RETRY", "THREAD-RETRY"],
        )
        self.assertEqual(restarted_executor.calls, [])
        self.assertEqual(restarted_publisher.calls, [])
        self.assertEqual(
            restarted_lifecycle.get_run("run-1")["state"],
            "waiting_for_feedback",
        )

    def test_historical_completed_feedback_is_matched_to_thread_in_place(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, path, line, url, state,
                    observed_at, processed_at)
                   VALUES ('historical-feedback', 'pr-1', 'inline_comment',
                           'historical-comment', 'v1', 'reviewer',
                           'Already handled', 'app.py', 10, 'comment-url',
                           'resolved', '2026-01-01T00:01:00Z',
                           '2026-01-01T00:02:00Z')"""
            )
        self.gateway.items = [
            self.item(
                "inline_comment",
                "historical-comment",
                "v1",
                "Already handled",
                review_thread_id="THREAD-HISTORICAL",
                review_thread_resolved=False,
            )
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 0)

        with self.db.connect() as connection:
            feedback = connection.execute(
                """SELECT id, review_thread_id, review_thread_resolved
                   FROM feedback_versions
                   WHERE github_object_id='historical-comment'"""
            ).fetchall()
            operation_state = connection.execute(
                """SELECT state FROM outbound_operations
                   WHERE kind='resolve_review_thread'"""
            ).fetchone()[0]
        self.assertEqual(
            [tuple(row) for row in feedback],
            [("historical-feedback", "THREAD-HISTORICAL", 1)],
        )
        self.assertEqual(operation_state, "completed")
        self.assertEqual(self.gateway.resolve_thread_calls, ["THREAD-HISTORICAL"])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self.gateway.post_calls, 0)

    def test_closed_unmerged_pull_restarts_open_issue_exactly_once(self) -> None:
        self.gateway.pull = replace(
            self.gateway.pull,
            state="closed",
            merged=False,
            updated_at="2026-01-01T00:08:00Z",
        )

        self.assertEqual(self.service.poll_run("run-1"), 0)
        restarted_database = Database(self.root / "db.sqlite3")
        restarted_database.initialize()
        restarted_lifecycle = FeedbackLifecycle(
            database=restarted_database,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )
        restarted = FeedbackService(
            database=restarted_database,
            lifecycle=restarted_lifecycle,
            gateway=self.gateway,
            evaluator=self.evaluator,
            executor=FakeExecutor(restarted_lifecycle),
            publisher=FakePublisher(
                restarted_database,
                restarted_lifecycle,
                self.gateway,
            ),
        )
        self.assertEqual(restarted.poll_run("run-1"), 0)
        self.assertEqual(self.service.poll_run("run-1"), 0)

        with self.db.connect() as connection:
            runs = connection.execute(
                """SELECT id, state, reason, sandbox_version_id, team_version_id,
                          intended_base_branch, base_sha
                   FROM runs ORDER BY created_at, id"""
            ).fetchall()
            activations = connection.execute("""SELECT github_event_id, kind
                   FROM activation_events ORDER BY applied_at, id""").fetchall()
            prior = connection.execute("""SELECT state FROM pull_requests
                   WHERE run_id='run-1'""").fetchone()

        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["state"], RunState.CLOSED.value)
        self.assertIn("closed without merge", runs[0]["reason"])
        self.assertEqual(runs[1]["state"], RunState.QUEUED.value)
        self.assertEqual(runs[1]["sandbox_version_id"], "sandbox-1")
        self.assertEqual(runs[1]["team_version_id"], "team-1")
        self.assertEqual(runs[1]["intended_base_branch"], "main")
        self.assertEqual(runs[1]["base_sha"], "a" * 40)
        self.assertEqual(prior["state"], "closed")
        self.assertEqual(len(activations), 2)
        self.assertEqual(activations[1]["kind"], "closed_pr_restart")
        self.assertIn("PR1", activations[1]["github_event_id"])

    def test_closed_unmerged_pull_does_not_restart_closed_issue(self) -> None:
        self.lifecycle.github.issue_state = "closed"
        self.gateway.pull = replace(
            self.gateway.pull,
            state="closed",
            merged=False,
            updated_at="2026-01-01T00:08:00Z",
        )

        self.assertEqual(self.service.poll_run("run-1"), 0)

        with self.db.connect() as connection:
            runs = connection.execute(
                "SELECT state, reason FROM runs ORDER BY created_at, id"
            ).fetchall()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["state"], RunState.CLOSED.value)
        self.assertIn("issue is closed", runs[0]["reason"])

    def test_feedback_discovery_waits_for_same_repository_lane(self) -> None:
        now = "2026-01-01T00:02:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-2', 'repo-1', 'I2', 4, 'issue-2-url',
                           'Other issue', 'Body', '[]', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-2', 'repo-1', 'issue-2', 'event-2', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, checkout_path, run_path, created_at,
                    updated_at)
                   VALUES ('run-2', 'repo-1', 'issue-2', 'activation-2',
                           'sandbox-1', 'team-1', 'main', ?, 'implementing',
                           '/tmp/run-2/checkout', '/tmp/run-2', ?, ?)""",
                ("a" * 40, now, now),
            )
        self.gateway.items = [
            self.item(
                "inline_comment",
                "comment-while-sibling-active",
                "v1",
                "Please change this behavior",
            )
        ]

        self.assertEqual(self.service.poll_run("run-1"), 1)

        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT state, resume_state FROM runs WHERE id='run-1'"
            ).fetchone()
            feedback_state = connection.execute(
                """SELECT state FROM feedback_versions
                   WHERE github_object_id='comment-while-sibling-active'"""
            ).fetchone()[0]
        self.assertEqual(tuple(run), ("queued", "resolving_feedback"))
        self.assertEqual(feedback_state, "pending")
        self.assertEqual(self.evaluator.calls, [])
        self.assertEqual(self.executor.calls, [])

        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='waiting_for_feedback' WHERE id='run-2'"
            )
        self.assertEqual(self.service.poll_run("run-1"), 0)
        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT state, resume_state FROM runs WHERE id='run-1'"
            ).fetchone()
        self.assertEqual(tuple(run), ("resolving_feedback", None))

    def test_polling_feedback_activates_resolution_without_agent_work(
        self,
    ) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "comment-during-monitoring",
                "v1",
                "Please change this behavior",
            )
        ]

        self.assertEqual(self.service.poll_run("run-1"), 1)
        self.assertEqual(self.service.poll_run("run-1"), 0)

        with self.db.connect() as connection:
            feedback_count = connection.execute(
                "SELECT COUNT(*) FROM feedback_versions"
            ).fetchone()[0]
            quiet_count = connection.execute(
                "SELECT COUNT(*) FROM quiet_periods"
            ).fetchone()[0]
            notification_count = connection.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0]
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            RunState.RESOLVING_FEEDBACK.value,
        )
        self.assertEqual(feedback_count, 1)
        self.assertEqual(quiet_count, 0)
        self.assertEqual(notification_count, 0)
        self.assertEqual(self.evaluator.calls, [])
        self.assertEqual(self.executor.calls, [])

    def test_base_advance_during_preparation_retries_only_latest_generation(
        self,
    ) -> None:
        observed_base_sha = "d" * 40
        latest_base_sha = "e" * 40
        pull_head_sha = self.gateway.pull.head_sha
        self.gateway.remote_base_head = observed_base_sha
        self.gateway.pull = replace(
            self.gateway.pull,
            base_sha="a" * 40,
            mergeable=False,
        )
        self.publisher.base_change_once = latest_base_sha

        processed = self.service.resolve_run("run-1")

        with self.db.connect() as connection:
            conflicts = connection.execute(
                """SELECT id, github_version, state, source_sha, superseded_at,
                          superseded_by_feedback_id
                   FROM feedback_versions
                   WHERE pull_request_id='pr-1'
                     AND feedback_type='base_conflict'"""
            ).fetchall()
            operations = connection.execute(
                """SELECT state FROM outbound_operations
                   WHERE run_id='run-1'
                     AND kind='feedback_revision_batch'
                   ORDER BY created_at, id"""
            ).fetchall()
        by_version = {str(row["github_version"]): row for row in conflicts}
        observed_version = f"{pull_head_sha}:{observed_base_sha}"
        latest_version = f"{pull_head_sha}:{latest_base_sha}"
        observed = by_version[observed_version]
        latest = by_version[latest_version]

        self.assertEqual(processed, 1)
        self.assertEqual(
            self.publisher.prepared_bases,
            [
                ("run-1", observed_base_sha),
                ("run-1", latest_base_sha),
            ],
        )
        self.assertEqual(len(self.executor.calls), 1)
        context = str(self.executor.calls[0][1])
        self.assertIn(latest_base_sha, context)
        self.assertNotIn(observed_base_sha, context)
        self.assertEqual(self.executor.calls[0][2], latest_base_sha)
        self.assertEqual(observed["state"], "resolved")
        self.assertIsNotNone(observed["superseded_at"])
        self.assertEqual(
            observed["superseded_by_feedback_id"],
            latest["id"],
        )
        self.assertIsNone(observed["source_sha"])
        self.assertEqual(latest["state"], "resolved")
        self.assertEqual(latest["source_sha"], "c" * 40)
        self.assertIsNone(latest["superseded_at"])
        self.assertEqual(
            sorted(str(row["state"]) for row in operations),
            ["completed", "reconciled"],
        )
        self.assertEqual(self.gateway.post_calls, 0)
        self.assertEqual(self.publisher.calls, ["run-1"])

    def test_restart_supersedes_stale_mixed_batch_and_reuses_other_feedback(
        self,
    ) -> None:
        stale_base_sha = "d" * 40
        current_base_sha = "e" * 40
        pull_head_sha = self.gateway.pull.head_sha
        self.gateway.remote_base_head = stale_base_sha
        self.gateway.pull = replace(
            self.gateway.pull,
            base_sha="a" * 40,
            mergeable=False,
        )
        self.gateway.items = [
            self.item("inline_comment", "finding-1", "v1", "Change the first path"),
            self.item("inline_comment", "finding-2", "v1", "Change the second path"),
        ]
        self.assertEqual(self.service.poll_run("run-1"), 3)
        decision_json = json.dumps(
            asdict(
                FeedbackDecision(
                    "revise",
                    "valid in-scope change",
                    "Implemented and validated the requested change.",
                )
            ),
            sort_keys=True,
        )
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE feedback_versions
                   SET state='processing',
                       decision_json=COALESCE(decision_json, ?)
                   WHERE pull_request_id='pr-1'""",
                (decision_json,),
            )
            original_rows = connection.execute(
                """SELECT id, feedback_type, github_version
                   FROM feedback_versions
                   WHERE pull_request_id='pr-1'
                   ORDER BY id"""
            ).fetchall()
            original_ids = [str(row["id"]) for row in original_rows]
            connection.execute(
                """INSERT INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state,
                    created_at)
                   VALUES ('stale-batch', 'run-1', 'feedback_revision_batch',
                           'stale-batch-key', ?, 'pending',
                           '2026-01-01T00:02:00Z')""",
                (json.dumps({"feedback_ids": original_ids}),),
            )
        stale_conflict = next(
            row
            for row in original_rows
            if row["feedback_type"] == "base_conflict"
        )
        ordinary_ids = {
            str(row["id"])
            for row in original_rows
            if row["feedback_type"] == "inline_comment"
        }
        self.gateway.remote_base_head = current_base_sha

        restarted_database = Database(self.db.path)
        restarted_database.initialize()
        restarted_lifecycle = FeedbackLifecycle(
            database=restarted_database,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )
        restarted_executor = FakeExecutor(restarted_lifecycle)
        restarted_publisher = FakePublisher(
            restarted_database,
            restarted_lifecycle,
            self.gateway,
        )
        restarted_service = FeedbackService(
            database=restarted_database,
            lifecycle=restarted_lifecycle,
            gateway=self.gateway,
            evaluator=self.evaluator,
            executor=restarted_executor,
            publisher=restarted_publisher,
        )

        processed = restarted_service.resolve_run("run-1")

        with restarted_database.connect() as connection:
            rows = connection.execute(
                """SELECT id, feedback_type, github_version, state, source_sha,
                          superseded_at, superseded_by_feedback_id
                   FROM feedback_versions
                   WHERE pull_request_id='pr-1'"""
            ).fetchall()
            operations = connection.execute(
                """SELECT id, request_json, state
                   FROM outbound_operations
                   WHERE run_id='run-1'
                     AND kind='feedback_revision_batch'
                   ORDER BY created_at, id"""
            ).fetchall()
            pull_count = connection.execute(
                "SELECT COUNT(*) FROM pull_requests WHERE run_id='run-1'"
            ).fetchone()[0]
        current_version = f"{pull_head_sha}:{current_base_sha}"
        current_conflict = next(
            row
            for row in rows
            if row["feedback_type"] == "base_conflict"
            and row["github_version"] == current_version
        )
        stale_after = next(
            row for row in rows if row["id"] == stale_conflict["id"]
        )
        completed_operation = next(
            row
            for row in operations
            if row["id"] != "stale-batch" and row["state"] == "completed"
        )
        completed_ids = set(
            json.loads(str(completed_operation["request_json"]))["feedback_ids"]
        )

        self.assertEqual(processed, 3)
        self.assertEqual(len(restarted_executor.calls), 1)
        self.assertEqual(restarted_publisher.prepared_bases, [("run-1", current_base_sha)])
        self.assertEqual(restarted_publisher.calls, ["run-1"])
        self.assertEqual(self.evaluator.calls, [])
        self.assertEqual(stale_after["state"], "resolved")
        self.assertIsNotNone(stale_after["superseded_at"])
        self.assertEqual(
            stale_after["superseded_by_feedback_id"],
            current_conflict["id"],
        )
        self.assertEqual(
            next(row for row in operations if row["id"] == "stale-batch")["state"],
            "reconciled",
        )
        self.assertTrue(ordinary_ids.issubset(completed_ids))
        self.assertIn(str(current_conflict["id"]), completed_ids)
        self.assertNotIn(str(stale_conflict["id"]), completed_ids)
        self.assertTrue(
            all(
                row["state"] == "resolved" and row["source_sha"] == "c" * 40
                for row in rows
                if row["id"] in ordinary_ids
                or row["id"] == current_conflict["id"]
            )
        )
        self.assertEqual(pull_count, 1)
        self.assertEqual(self.gateway.post_calls, 0)

    def test_confirmed_mergeability_supersedes_unfinished_conflict(self) -> None:
        conflict_base_sha = "d" * 40
        self.gateway.remote_base_head = conflict_base_sha
        self.gateway.pull = replace(self.gateway.pull, mergeable=False)
        self.assertEqual(self.service.poll_run("run-1"), 1)
        self.gateway.pull = replace(self.gateway.pull, mergeable=True)

        self.assertEqual(self.service.poll_run("run-1"), 0)

        with self.db.connect() as connection:
            conflict = connection.execute(
                """SELECT state, superseded_at, superseded_by_feedback_id
                   FROM feedback_versions
                   WHERE pull_request_id='pr-1'
                     AND feedback_type='base_conflict'"""
            ).fetchone()
        self.assertEqual(conflict["state"], "resolved")
        self.assertIsNotNone(conflict["superseded_at"])
        self.assertIsNone(conflict["superseded_by_feedback_id"])
        self.assertEqual(len(self.executor.calls), 0)

    def test_unknown_mergeability_preserves_unfinished_conflict(self) -> None:
        conflict_base_sha = "d" * 40
        self.gateway.remote_base_head = conflict_base_sha
        self.gateway.pull = replace(self.gateway.pull, mergeable=False)
        self.assertEqual(self.service.poll_run("run-1"), 1)
        self.gateway.pull = replace(self.gateway.pull, mergeable=None)
        self.gateway.remote_base_head = "e" * 40

        self.assertEqual(self.service.poll_run("run-1"), 0)

        with self.db.connect() as connection:
            conflict = connection.execute(
                """SELECT state, superseded_at, superseded_by_feedback_id
                   FROM feedback_versions
                   WHERE pull_request_id='pr-1'
                     AND feedback_type='base_conflict'"""
            ).fetchone()
        self.assertEqual(conflict["state"], "pending")
        self.assertIsNone(conflict["superseded_at"])
        self.assertIsNone(conflict["superseded_by_feedback_id"])
        self.assertEqual(
            self.gateway.base_head_reads,
            [("owner", "repo", "main")],
        )

    def test_conflicting_head_base_generation_is_durable_across_restart(
        self,
    ) -> None:
        base_sha = "d" * 40
        self.gateway.pull = replace(
            self.gateway.pull,
            base_sha=base_sha,
            mergeable=False,
        )

        self.assertEqual(self.service.poll_run("run-1"), 1)
        restarted = FeedbackService(
            database=Database(self.root / "db.sqlite3"),
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            evaluator=self.evaluator,
            executor=self.executor,
            publisher=self.publisher,
        )
        self.assertEqual(restarted.poll_run("run-1"), 0)

        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT feedback_type, github_object_id, github_version,
                          state, decision_json
                   FROM feedback_versions"""
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["feedback_type"], "base_conflict")
        self.assertEqual(rows[0]["github_object_id"], "PR1")
        self.assertEqual(
            rows[0]["github_version"],
            f"{self.gateway.pull.head_sha}:{base_sha}",
        )
        self.assertEqual(rows[0]["state"], "pending")
        self.assertEqual(
            json.loads(rows[0]["decision_json"])["action"],
            "revise",
        )

    def test_merged_run_and_conflicting_run_are_polled_independently(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title,
                    body, discussion_json, updated_at)
                   VALUES ('issue-2', 'repo-1', 'I2', 4, 'issue-2-url',
                           'Second issue', 'Second body', '[]', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-2', 'repo-1', 'issue-2', 'event-2', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id,
                    intended_base_branch, base_sha, state,
                    last_completed_state, validated_sha, checkout_path,
                    run_path, created_at, updated_at)
                   VALUES ('run-2', 'repo-1', 'issue-2', 'activation-2',
                           'sandbox-1', 'team-1', 'main', ?,
                           'waiting_for_feedback', 'publishing', ?, ?, ?, ?, ?)""",
                (
                    "a" * 40,
                    "e" * 40,
                    str(self.root / "run-2" / "checkout"),
                    str(self.root / "run-2"),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-2', 'run-2', 'PR2', 12, 'pr-2-url',
                           'agent/issue-4-run-2', 'main', ?, ?, ?,
                           'open', ?, ?)""",
                ("a" * 40, "e" * 40, "e" * 40, now, now),
            )

        class MultiRunGateway(FakeFeedbackGateway):
            def __init__(self) -> None:
                super().__init__()
                self.pulls = {
                    11: replace(
                        self.pull,
                        state="closed",
                        merged=True,
                        mergeable=True,
                    ),
                    12: replace(
                        self.pull,
                        node_id="PR2",
                        number=12,
                        url="pr-2-url",
                        head_branch="agent/issue-4-run-2",
                        head_sha="e" * 40,
                        base_sha="d" * 40,
                        mergeable=False,
                    ),
                }

            def get_pull_request(
                self,
                owner: str,
                name: str,
                number: int,
            ) -> PullRequestInfo:
                del owner, name
                self.status_calls += 1
                return self.pulls[number]

        gateway = MultiRunGateway()
        publisher = FakePublisher(self.db, self.lifecycle, gateway)
        service = FeedbackService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=gateway,
            evaluator=self.evaluator,
            executor=self.executor,
            publisher=publisher,
        )

        self.assertEqual(service.poll_run("run-1"), 0)
        self.assertEqual(service.poll_run("run-2"), 1)

        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            RunState.CLOSED.value,
        )
        self.assertEqual(
            self.lifecycle.get_run("run-2")["state"],
            RunState.RESOLVING_FEEDBACK.value,
        )
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT pull_requests.run_id,
                          feedback_versions.feedback_type
                   FROM feedback_versions
                   JOIN pull_requests
                     ON pull_requests.id=feedback_versions.pull_request_id"""
            ).fetchall()
        self.assertEqual(
            [(row["run_id"], row["feedback_type"]) for row in rows],
            [("run-2", "base_conflict")],
        )

    def test_conflict_revision_reuses_existing_run_checkout_branch_and_pull(
        self,
    ) -> None:
        base_sha = "d" * 40
        self.gateway.pull = replace(
            self.gateway.pull,
            base_sha=base_sha,
            mergeable=False,
        )

        self.assertEqual(self.service.resolve_run("run-1"), 1)

        self.assertEqual(
            self.publisher.prepared_bases,
            [("run-1", base_sha)],
        )
        self.assertEqual(len(self.executor.calls), 1)
        self.assertIn(base_sha, str(self.executor.calls[0][1]))
        self.assertEqual(self.executor.calls[0][2], base_sha)
        self.assertEqual(self.evaluator.calls, [])
        self.assertEqual(self.publisher.calls, ["run-1"])
        with self.db.connect() as connection:
            pull_count = connection.execute(
                "SELECT COUNT(*) FROM pull_requests WHERE run_id='run-1'"
            ).fetchone()[0]
            branch = connection.execute(
                "SELECT branch_name FROM pull_requests WHERE run_id='run-1'"
            ).fetchone()[0]
        self.assertEqual(pull_count, 1)
        self.assertEqual(branch, "agent/issue-3-run-1")

    def test_later_feedback_reuses_latest_integrated_conflict_base(self) -> None:
        integrated_base_sha = "d" * 40
        now = "2026-01-01T00:02:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, decision_json,
                    source_sha, observed_at)
                   VALUES ('integrated-conflict', 'pr-1', 'base_conflict',
                           'conflict-generation', ?, 'github',
                           'Merge conflict with current intended base',
                           'resolved',
                           '{"action":"revise","reason":"conflict","response":""}',
                           ?, ?)""",
                (
                    f"{self.gateway.pull.head_sha}:{integrated_base_sha}",
                    self.gateway.pull.head_sha,
                    now,
                ),
            )
        self.gateway.items = [
            self.item(
                "comment",
                "later-change",
                "v1",
                "Please change the related behavior",
            )
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 1)

        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.executor.calls[0][2], integrated_base_sha)

    def test_conflict_revision_expands_team_after_work_starts_and_survives_restart(
        self,
    ) -> None:
        checkout, base_sha, prior_validated_sha = self._seed_commit_history()

        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", *arguments],
                cwd=checkout,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()

        git("branch", "-M", "agent/issue-3-run-1")
        git("checkout", "-b", "fixture-current-base", base_sha)
        (checkout / "app.py").write_text(
            "line 10 changed on current base\n", encoding="utf-8"
        )
        git("add", "app.py")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-m",
            "advance fixture base",
        )
        current_base_sha = git("rev-parse", "HEAD")
        git("checkout", "agent/issue-3-run-1")

        baseline_log = self.root / "baseline.log"
        baseline_log.write_text("fixture baseline passed\n", encoding="utf-8")
        policy = {
            "persistent_root": str(
                self.data_root / "repositories" / "repo-1" / "sandbox" / "1"
            ),
            "allowed_host_paths": [],
            "allowed_services": [],
            "secret_bindings": [],
        }
        command = [
            "python3",
            "-c",
            "compile(open('app.py', encoding='utf-8').read(), 'app.py', 'exec')",
        ]
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO issue_versions
                   (id, issue_id, version, previous_version_id,
                    github_updated_at, content_sha256, title, body,
                    discussion_json, observed_at)
                   VALUES ('issue-version-1', 'issue-1', 1, NULL, ?, ?,
                           'Issue', 'Body', '[]', ?)""",
                (now, "f" * 64, now),
            )
            connection.execute(
                """UPDATE issues SET current_version_id='issue-version-1'
                   WHERE id='issue-1'"""
            )
            connection.execute(
                """UPDATE activation_events
                   SET issue_version_id='issue-version-1'
                   WHERE id='activation-1'"""
            )
            connection.execute(
                """UPDATE runs
                   SET validated_issue_version_id='issue-version-1'
                   WHERE id='run-1'"""
            )
            connection.execute(
                """UPDATE sandbox_versions SET policy_json=?
                   WHERE id='sandbox-1'""",
                (json.dumps(policy),),
            )
            connection.execute(
                """INSERT INTO validation_commands
                   (id, sandbox_version_id, position, command_json, source, required)
                   VALUES ('validation-command-1', 'sandbox-1', 0, ?, 'fixture', 1)""",
                (json.dumps(command),),
            )
            connection.execute(
                """INSERT INTO validation_baselines
                   (id, run_id, validation_command_id, command_json, base_sha,
                    mode, started_at, completed_at, exit_status, log_path,
                    findings_json)
                   VALUES ('baseline-1', 'run-1', 'validation-command-1', ?, ?,
                           'strict', ?, ?, 0, ?, '[]')""",
                (
                    json.dumps(command),
                    base_sha,
                    now,
                    now,
                    str(baseline_log),
                ),
            )
            connection.execute("""UPDATE team_members
                   SET permitted_tools_json='["read","git_diff","git_commit"]',
                       model='test/lead'
                   WHERE id='lead-1'""")
            connection.executemany(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES (?, 'team-1', ?, ?, ?, ?, ?,
                           'mini-swe-agent', ?, '')""",
                (
                    (
                        "interface-1",
                        "interface",
                        "implementer",
                        "interface maintainer",
                        "Resolve interface feedback",
                        '["read","write","run","git_diff"]',
                        "test/interface",
                    ),
                    (
                        "conflict-resolution-1",
                        "conflict-resolution",
                        "implementer",
                        "conflict resolver",
                        "Resolve source conflicts",
                        '["read","write","run","git_diff"]',
                        "test/conflict-resolution",
                    ),
                    (
                        "verification-1",
                        "verification",
                        "verifier",
                        "behavior verifier",
                        "Independently review the conflict resolution",
                        '["read","run","git_diff"]',
                        "test/verification",
                    ),
                ),
            )
            connection.executemany(
                """INSERT INTO agent_assignments
                   (id, run_id, team_member_id, reasoning, assigned_at)
                   VALUES (?, 'run-1', ?, 'Resolve the original interface issue', ?)""",
                (
                    ("lead-assignment", "lead-1", now),
                    ("interface-assignment", "interface-1", now),
                ),
            )

        completed_interface = ScriptedRuntime(
            [
                {
                    "action": "finish",
                    "summary": (
                        "completed assigned interface work; the remaining conflict "
                        "belongs to the stored conflict resolver"
                    ),
                }
            ]
        )
        expansion_reason = (
            "The fetched-base conflict requires the stored conflict resolver."
        )
        assigning_lead = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": [
                        "lead",
                        "interface",
                        "conflict-resolution",
                        "verification",
                    ],
                    "reason": expansion_reason,
                }
            ]
        )
        unused = ScriptedRuntime([])
        first_executor = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=(
                lambda _runtime, model, _timeout: (
                    assigning_lead
                    if model == "test/lead"
                    else completed_interface
                    if model == "test/interface"
                    else unused
                )
            ),
            max_actions=10,
            max_revision_cycles=1,
        )
        first_publisher = FakePublisher(self.db, self.lifecycle, self.gateway)
        first_service = FeedbackService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            evaluator=self.evaluator,
            executor=first_executor,
            publisher=first_publisher,
        )
        self.gateway.pull = replace(
            self.gateway.pull,
            head_sha=prior_validated_sha,
            base_sha=current_base_sha,
            mergeable=False,
        )

        first_service.poll_run("run-1")
        pending = first_service._pending("run-1")
        for row in pending:
            first_service._decision("run-1", row)
        pending = first_service._pending("run-1")
        revision_context, _ = first_service._revision_context(
            "run-1",
            pending,
        )
        first_publisher.prepared_bases.clear()
        specifications = SpecificationService(self.db)
        current_specification = specifications.submit(
            "run-1",
            "lead-1",
            "issue-version-1",
            [
                {
                    "key": "conflict-resolution",
                    "title": "Resolve the current base conflict",
                    "objective": (
                        "Preserve the issue behavior while integrating "
                        "the fetched base."
                    ),
                    "acceptance_criteria": [
                        {
                            "key": "merged-behavior",
                            "requirement": (
                                "The current base and issue behavior are "
                                "both retained."
                            ),
                            "expected": (
                                "Validation passes on a commit containing "
                                "both histories."
                            ),
                        }
                    ],
                    "verification": [
                        {
                            "key": "validate-merge",
                            "scenario": (
                                "Validate the resolved commit and inspect "
                                "its ancestry."
                            ),
                            "criterion_keys": ["merged-behavior"],
                        }
                    ],
                }
            ],
            "Define the conflict-resolution behavior before revising source.",
        )
        specifications.record_review(
            "run-1",
            str(current_specification["id"]),
            "verification-1",
            "test/verification",
            1,
            "approved",
            "The conflict behavior and verification are observable.",
            [],
        )
        specifications.bind_context(
            run_id="run-1",
            issue_version_id="issue-version-1",
            context_sha256=hashlib.sha256(
                revision_context.encode("utf-8")
            ).hexdigest(),
            specification_revision_id=str(current_specification["id"]),
        )

        self.assertEqual(first_service.resolve_run("run-1"), 0)
        assignments = TeamService(self.db).assignments_for_run("run-1")
        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead", "conflict-resolution", "interface", "verification"],
        )
        reasons = {
            assignment.member.stable_key: assignment.reasoning
            for assignment in assignments
        }
        self.assertEqual(reasons["conflict-resolution"], expansion_reason)
        self.assertEqual(
            reasons["interface"],
            "Resolve the original interface issue",
        )
        with self.db.connect() as connection:
            interrupted = connection.execute(
                """SELECT runs.state, feedback_versions.source_sha,
                          outbound_operations.state AS operation_state
                   FROM runs
                   JOIN pull_requests ON pull_requests.run_id=runs.id
                   JOIN feedback_versions
                     ON feedback_versions.pull_request_id=pull_requests.id
                   JOIN outbound_operations
                     ON outbound_operations.run_id=runs.id
                    AND outbound_operations.kind='feedback_revision_batch'
                   WHERE runs.id='run-1'"""
            ).fetchone()
        self.assertEqual(interrupted["state"], RunState.RESOLVING_FEEDBACK.value)
        self.assertIsNone(interrupted["source_sha"])
        self.assertEqual(interrupted["operation_state"], "pending")

        layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        interrupted_history = first_executor._load_transcript(layout)
        interrupted_history.extend(
            f"Action retained recovery evidence {index}\nResult observed"
            for index in range(30)
        )
        first_executor._store_transcript(layout, interrupted_history)

        resumed_db = Database(self.db.path)
        resumed_lifecycle = FeedbackLifecycle(
            database=resumed_db,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )
        conflict_resolution = ScriptedRuntime(
            [
                {
                    "action": "run",
                    "argv": [
                        "git",
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=f@example.com",
                        "merge",
                        "--no-edit",
                        current_base_sha,
                    ],
                },
                {
                    "action": "write",
                    "path": "app.py",
                    "content": "value = 'resolved from feature and current base'\n",
                },
                {
                    "action": "finish",
                    "summary": "resolved the current base conflict",
                },
            ]
        )
        lead = ScriptedRuntime(
            [{"action": "finish", "summary": "integrated conflict resolution"}]
        )
        verification = ScriptedRuntime(
            [{"action": "finish", "summary": "conflict resolution approved"}]
        )
        previously_completed = ScriptedRuntime([])
        resumed_executor = ExecutionService(
            database=resumed_db,
            lifecycle=resumed_lifecycle,
            teams=TeamService(resumed_db),
            sandbox=self.sandbox,
            runtime_factory=(
                lambda _runtime, model, _timeout: (
                    lead
                    if model == "test/lead"
                    else conflict_resolution
                    if model == "test/conflict-resolution"
                    else verification
                    if model == "test/verification"
                    else previously_completed
                )
            ),
            max_actions=10,
            max_revision_cycles=1,
        )
        resumed_publisher = FakePublisher(
            resumed_db,
            resumed_lifecycle,
            self.gateway,
        )
        resumed_service = FeedbackService(
            database=resumed_db,
            lifecycle=resumed_lifecycle,
            gateway=self.gateway,
            evaluator=self.evaluator,
            executor=resumed_executor,
            publisher=resumed_publisher,
        )

        self.assertEqual(resumed_service.resolve_run("run-1"), 1)

        with resumed_db.connect() as connection:
            run = connection.execute(
                "SELECT state, validated_sha FROM runs WHERE id='run-1'"
            ).fetchone()
            validation = connection.execute(
                """SELECT commit_sha, exit_status, verdict, log_path
                   FROM validation_results
                   WHERE run_id='run-1' AND commit_sha=?""",
                (run["validated_sha"],),
            ).fetchone()
            pull = connection.execute(
                """SELECT github_node_id, branch_name, remote_head_sha
                   FROM pull_requests WHERE run_id='run-1'"""
            ).fetchone()
            pull_count = connection.execute(
                "SELECT COUNT(*) FROM pull_requests WHERE run_id='run-1'"
            ).fetchone()[0]
            run_count = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE id='run-1'"
            ).fetchone()[0]
        self.assertEqual(previously_completed.contexts, [])
        self.assertNotEqual(run["validated_sha"], prior_validated_sha)
        self.assertEqual(run["state"], RunState.WAITING_FOR_FEEDBACK.value)
        self.assertIsNotNone(validation)
        self.assertEqual(validation["commit_sha"], run["validated_sha"])
        self.assertEqual(validation["exit_status"], 0)
        self.assertEqual(validation["verdict"], "pass")
        self.assertTrue(Path(validation["log_path"]).is_file())
        self.assertEqual(
            first_publisher.prepared_bases,
            [("run-1", current_base_sha)],
        )
        self.assertEqual(
            resumed_publisher.prepared_bases,
            [("run-1", current_base_sha)],
        )
        self.assertEqual(resumed_publisher.calls, ["run-1"])
        self.assertEqual(run_count, 1)
        self.assertEqual(pull_count, 1)
        self.assertEqual(pull["github_node_id"], "PR1")
        self.assertEqual(pull["branch_name"], "agent/issue-3-run-1")
        self.assertEqual(pull["remote_head_sha"], run["validated_sha"])
        git(
            "merge-base",
            "--is-ancestor",
            current_base_sha,
            str(run["validated_sha"]),
        )

    def test_evaluation_context_uses_the_runs_stored_lead_configuration(self) -> None:
        self.gateway.items = [
            self.item("comment", "question-model", "v1", "Which model?")
        ]
        self.assertEqual(self.service.poll_run("run-1"), 1)
        row = self.service._pending("run-1")[0]

        context = self.service._evaluation_context("run-1", row)

        self.assertEqual(
            context["stored_lead"],
            {"runtime": "mini-swe-agent", "model": "openai/gpt-stored"},
        )
        self.assertIn("+line 10 corrected implementation", context["current_diff"])
        self.assertEqual(context["repository_evidence"], {})
        self.assertEqual(context["addressed_source"], {"status": "not_applicable"})

    def test_evaluation_context_uses_committed_diff_and_inline_source(self) -> None:
        checkout, base_sha, validated_sha = self._seed_commit_history()
        self.gateway.items = [
            self.item(
                "inline_comment",
                "implementation-context",
                "v1",
                "Why is line 10 correct?",
            )
        ]
        self.assertEqual(self.service.poll_run("run-1"), 1)
        row = self.service._pending("run-1")[0]
        (checkout / "app.py").write_text(
            "mutable working tree must not be evaluated\n", encoding="utf-8"
        )

        context = self.service._evaluation_context("run-1", row)

        self.assertEqual(context["run_id"], "run-1")
        self.assertEqual(context["current_base_sha"], base_sha)
        self.assertEqual(context["current_validated_sha"], validated_sha)
        self.assertIn("+line 10 corrected implementation", context["current_diff"])
        source = context["addressed_source"]
        self.assertEqual(source["status"], "available")
        self.assertEqual(source["path"], "app.py")
        self.assertEqual(source["line"], 10)
        self.assertIn("line 10 corrected implementation", source["content"])
        self.assertNotIn("mutable working tree", source["content"])

    def test_context_collection_failure_uses_durable_controller_retry(
        self,
    ) -> None:
        self.gateway.items = [
            self.item("comment", "context-retry", "v1", "Why is this correct?")
        ]
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=self.lifecycle,
            execution=self.executor,
            publication=self.publisher,
            feedback=self.service,
        )
        with mock.patch.object(
            self.service,
            "_evaluation_context",
            side_effect=RuntimeError("temporary git read failure"),
        ):
            orchestrator._advance("run-1")

        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], RunState.RESOLVING_FEEDBACK.value)
        self.assertEqual(run["retry_attempt_count"], 1)
        self.assertEqual(run["retry_operation"], "feedback_resolution")
        self.assertIsNotNone(run["retry_next_at"])
        self.assertIn("temporary git read failure", run["retry_last_error"])
        with self.db.connect() as connection:
            pending = connection.execute(
                "SELECT state, decision_json, processed_at FROM feedback_versions"
            ).fetchone()
        self.assertEqual(tuple(pending), ("pending", None, None))
        self.assertEqual(self.gateway.post_calls, 0)

        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs SET retry_next_at='2000-01-01T00:00:00Z'
                   WHERE id='run-1'"""
            )
        orchestrator._advance("run-1")

        recovered = self.lifecycle.get_run("run-1")
        self.assertEqual(recovered["state"], RunState.WAITING_FOR_FEEDBACK.value)
        self.assertEqual(recovered["retry_attempt_count"], 0)
        self.assertIsNone(recovered["retry_operation"])
        self.assertIsNone(recovered["retry_next_at"])
        self.assertIsNone(recovered["retry_last_error"])
        self.assertEqual(self.gateway.post_calls, 1)

    def test_inline_context_handles_untrusted_deleted_and_binary_paths(self) -> None:
        self._seed_commit_history()
        invalid = replace(
            self.item("inline_comment", "invalid-path", "v1", "Traversal?"),
            path="../../etc/passwd",
            line=1,
        )
        deleted = replace(
            self.item("inline_comment", "deleted-path", "v1", "Deleted?"),
            path="deleted.txt",
            line=1,
        )
        binary = replace(
            self.item("inline_comment", "binary-path", "v1", "Binary?"),
            path="binary.bin",
            line=1,
        )
        self.gateway.items = [invalid, deleted, binary]
        self.assertEqual(self.service.poll_run("run-1"), 3)
        rows = {
            str(row["github_object_id"]): row for row in self.service._pending("run-1")
        }

        contexts = {
            key: self.service._evaluation_context("run-1", row)
            for key, row in rows.items()
        }

        self.assertEqual(
            contexts["invalid-path"]["addressed_source"]["status"],
            "invalid_path",
        )
        self.assertEqual(
            contexts["deleted-path"]["addressed_source"]["status"],
            "unavailable",
        )
        self.assertEqual(
            contexts["binary-path"]["addressed_source"]["status"],
            "binary",
        )
        self.assertNotIn(
            "root:",
            json.dumps(contexts["invalid-path"]["addressed_source"]),
        )

    def test_ingests_mixed_and_edited_feedback_once_but_filters_recorded_outputs(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state, created_at)
                   VALUES ('output-op', 'run-1', 'post_feedback_response', 'output-op', '{}', 'completed', ?)""",
                ("2026-01-01T00:00:00Z",),
            )
            connection.execute(
                """INSERT INTO application_outputs
                   (id, pull_request_id, feedback_type, github_object_id,
                    operation_id, created_at)
                   VALUES ('known-output', 'pr-1', 'comment', 'app-output', 'output-op', ?)""",
                ("2026-01-01T00:00:00Z",),
            )
        self.gateway.items = [
            self.item("review", "review-1", "v1", "Review body"),
            self.item("inline_comment", "inline-1", "v1", "Inline body"),
            self.item("comment", "comment-1", "v1", "First version"),
            self.item("comment", "comment-1", "v2", "Edited version"),
            self.item(
                "comment",
                "app-output",
                "v1",
                "Application response",
                author="configured-user",
            ),
            self.item(
                "comment",
                "manual-same-author",
                "v1",
                "Manual feedback",
                author="configured-user",
            ),
        ]
        self.assertEqual(self.service.poll_run("run-1"), 5)
        self.assertEqual(self.service.poll_run("run-1"), 0)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT github_object_id, github_version FROM feedback_versions ORDER BY github_object_id, github_version"
            ).fetchall()
        self.assertEqual(len(rows), 5)
        self.assertNotIn("app-output", [row["github_object_id"] for row in rows])
        self.assertEqual(
            [
                row["github_version"]
                for row in rows
                if row["github_object_id"] == "comment-1"
            ],
            ["v1", "v2"],
        )

    def test_answer_is_posted_once_and_never_reingested(self) -> None:
        self.gateway.items = [
            self.item("comment", "question-1", "v1", "Why is this correct?")
        ]
        self.assertEqual(self.service.resolve_run("run-1"), 1)
        self.assertEqual(self.gateway.post_calls, 1)
        self.gateway.items.append(
            self.item(
                "comment",
                "output-1",
                "2026-01-01T00:05:00Z",
                self.gateway.outputs[0].body,
            )
        )
        self.assertEqual(self.service.poll_run("run-1"), 0)
        with self.db.connect() as connection:
            row = connection.execute("SELECT state FROM feedback_versions").fetchone()
            outputs = connection.execute(
                "SELECT COUNT(*) FROM application_outputs"
            ).fetchone()[0]
        self.assertEqual(row["state"], "answered")
        self.assertEqual(outputs, 1)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            RunState.WAITING_FOR_FEEDBACK.value,
        )

    def test_response_loss_reconciles_on_next_attempt_without_duplicate_post(
        self,
    ) -> None:
        self.gateway.items = [self.item("comment", "question-1", "v1", "Why?")]
        self.gateway.crash_after_post = True
        with self.assertRaisesRegex(
            RuntimeError,
            "connection dropped after response was accepted",
        ):
            self.service.resolve_run("run-1")
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "resolving_feedback")
        output = self.gateway.outputs[0]
        self.gateway.items.append(
            self.item(
                output.feedback_type,
                output.object_id,
                str(output.created_at),
                output.body,
                author="configured-user",
            )
        )
        self.assertEqual(self.service.resolve_run("run-1"), 1)
        self.assertEqual(self.gateway.post_calls, 1)
        self.assertEqual(len(self.gateway.outputs), 1)
        with self.db.connect() as connection:
            feedback_count = connection.execute(
                "SELECT COUNT(*) FROM feedback_versions"
            ).fetchone()[0]
        self.assertEqual(feedback_count, 1)
        self.assertEqual(self.evaluator.calls, ["Why?"])

    def test_external_close_stops_before_ingesting_or_mutating_feedback(self) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "ignored-after-close",
                "v1",
                "Please change this behavior",
            )
        ]
        self.gateway.pull = PullRequestInfo(
            node_id="PR1",
            number=11,
            url="pr-url",
            state="closed",
            merged=False,
            head_branch="agent/issue-3-run-1",
            head_sha="d" * 40,
            base_branch="main",
            updated_at="2026-01-01T00:07:00Z",
        )

        self.assertEqual(self.service.resolve_run("run-1"), 0)

        self.assertEqual(self.gateway.status_calls, 1)
        self.assertEqual(self.gateway.polls, 0)
        self.assertEqual(self.evaluator.calls, [])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self.gateway.post_calls, 0)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "closed")
        with self.db.connect() as connection:
            pull = connection.execute(
                "SELECT state, remote_head_sha, updated_at FROM pull_requests"
            ).fetchone()
            feedback_count = connection.execute(
                "SELECT COUNT(*) FROM feedback_versions"
            ).fetchone()[0]
        self.assertEqual(
            dict(pull),
            {
                "state": "closed",
                "remote_head_sha": "d" * 40,
                "updated_at": "2026-01-01T00:07:00Z",
            },
        )
        self.assertEqual(feedback_count, 0)

    def test_cancellation_during_feedback_inference_prevents_later_effects(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingEvaluator(FakeEvaluator):
            def evaluate(self, context: dict[str, object]) -> FeedbackDecision:
                started.set()
                self.assert_release(release)
                return FeedbackDecision(
                    "answer",
                    "relevant question",
                    "The behavior follows the repository contract.",
                )

            @staticmethod
            def assert_release(event: threading.Event) -> None:
                if not event.wait(5):
                    raise AssertionError("feedback evaluator was not released")

        self.gateway.items = [
            self.item("comment", "cancel-inference", "v1", "Why is this correct?")
        ]
        self.service.evaluator = BlockingEvaluator()
        thread = threading.Thread(target=lambda: self.service.resolve_run("run-1"))
        thread.start()
        self.assertTrue(started.wait(5))

        self.lifecycle.cancel("run-1", "canceled during feedback inference")
        release.set()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "canceled")
        self.assertEqual(self.gateway.post_calls, 0)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.publisher.calls, [])
        with self.db.connect() as connection:
            feedback = connection.execute(
                "SELECT state, decision_json, processed_at FROM feedback_versions"
            ).fetchone()
        self.assertEqual(
            tuple(feedback),
            ("pending", None, None),
        )

    def test_external_close_during_feedback_inference_prevents_later_effects(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingEvaluator(FakeEvaluator):
            def evaluate(self, context: dict[str, object]) -> FeedbackDecision:
                started.set()
                if not release.wait(5):
                    raise AssertionError("feedback evaluator was not released")
                return FeedbackDecision(
                    "answer",
                    "relevant question",
                    "The behavior follows the repository contract.",
                )

        self.gateway.items = [
            self.item("comment", "close-inference", "v1", "Why is this correct?")
        ]
        self.service.evaluator = BlockingEvaluator()
        thread = threading.Thread(target=lambda: self.service.resolve_run("run-1"))
        thread.start()
        self.assertTrue(started.wait(5))
        self.gateway.pull = replace(
            self.gateway.pull,
            state="closed",
            updated_at="2026-01-01T00:07:00Z",
        )
        release.set()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "closed")
        self.assertEqual(self.gateway.post_calls, 0)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.publisher.calls, [])
        with self.db.connect() as connection:
            feedback = connection.execute(
                "SELECT state, decision_json, processed_at FROM feedback_versions"
            ).fetchone()
        self.assertEqual(
            tuple(feedback),
            ("pending", None, None),
        )

    def test_known_revision_feedback_is_combined_before_one_publication(self) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "inline-1",
                "v1",
                "Please change this behavior",
            ),
            self.item(
                "comment",
                "comment-2",
                "v1",
                "Please change the related fallback",
            ),
        ]

        self.assertEqual(self.service.resolve_run("run-1"), 2)

        self.assertEqual(len(self.executor.calls), 1)
        revision_context = str(self.executor.calls[0][1])
        self.assertIn("Please change this behavior", revision_context)
        self.assertIn("Please change the related fallback", revision_context)
        self.assertEqual(self.publisher.calls, ["run-1"])
        self.assertEqual(self.gateway.post_calls, 0)
        with self.db.connect() as connection:
            feedback = connection.execute(
                """SELECT state, source_sha FROM feedback_versions
                   ORDER BY github_object_id"""
            ).fetchall()
            pull_count = connection.execute(
                "SELECT COUNT(*) FROM pull_requests"
            ).fetchone()[0]
            pull = connection.execute(
                "SELECT remote_head_sha FROM pull_requests"
            ).fetchone()
        self.assertEqual(
            [(row["state"], row["source_sha"]) for row in feedback],
            [("resolved", "c" * 40), ("resolved", "c" * 40)],
        )
        self.assertEqual(pull_count, 1)
        self.assertEqual(pull["remote_head_sha"], "c" * 40)

    def test_feedback_arriving_before_publication_prevents_intermediate_push(
        self,
    ) -> None:
        first = self.item(
            "inline_comment",
            "first-revision",
            "v1",
            "Please change the first behavior",
        )
        second = self.item(
            "inline_comment",
            "second-revision",
            "v1",
            "Please change the second behavior",
        )
        self.gateway.items = [first]

        def inject_second() -> None:
            self.gateway.items.append(second)
            self.service.poll_run("run-1")
            decision = FeedbackDecision(
                "revise",
                "valid in-scope change",
                "Implemented and validated the requested change.",
            )
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE feedback_versions
                       SET state='processing', decision_json=?
                       WHERE github_object_id='second-revision'""",
                    (json.dumps(asdict(decision), sort_keys=True),),
                )

        self.executor.after_execute = inject_second

        self.assertEqual(self.service.resolve_run("run-1"), 2)

        self.assertEqual(len(self.executor.calls), 2)
        self.assertIn("Please change the first behavior", self.executor.calls[0][1])
        self.assertIn("Please change the first behavior", self.executor.calls[1][1])
        self.assertIn("Please change the second behavior", self.executor.calls[1][1])
        self.assertEqual(self.publisher.calls, ["run-1"])
        self.assertEqual(self.gateway.post_calls, 0)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT state, source_sha FROM feedback_versions ORDER BY github_object_id"
            ).fetchall()
        self.assertEqual(
            [(row["state"], row["source_sha"]) for row in rows],
            [("resolved", "c" * 40), ("resolved", "c" * 40)],
        )

    def test_external_close_during_publication_stops_before_reconciliation(
        self,
    ) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "inline-closed-during-publication",
                "v1",
                "Please change this behavior",
            )
        ]
        self.assertEqual(self.service.poll_run("run-1"), 1)
        decision = FeedbackDecision(
            "revise",
            "valid in-scope change",
            "Implemented and validated the requested change.",
        )
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE feedback_versions
                   SET state='processing', decision_json=?""",
                (json.dumps(decision.__dict__, sort_keys=True),),
            )
            connection.execute(
                """UPDATE runs
                   SET state='publishing', validated_sha=?, updated_at=?
                   WHERE id='run-1'""",
                ("c" * 40, "2026-01-01T00:05:00Z"),
            )
        self.gateway.pull = PullRequestInfo(
            node_id="PR1",
            number=11,
            url="pr-url",
            state="closed",
            merged=False,
            head_branch="agent/issue-3-run-1",
            head_sha="c" * 40,
            base_branch="main",
            updated_at="2026-01-01T00:07:00Z",
        )

        self.assertEqual(self.service.resolve_run("run-1"), 0)

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "closed")
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self.gateway.post_calls, 0)

    def test_crash_after_validation_checkpoints_sha_before_publication_without_reexecution(
        self,
    ) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "inline-crash-window",
                "v1",
                "Please change this behavior",
            )
        ]
        self.assertEqual(self.service.poll_run("run-1"), 1)
        decision = FeedbackDecision(
            "revise",
            "valid in-scope change",
            "Implemented and validated the requested change.",
        )
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE feedback_versions
                   SET state='processing', decision_json=?""",
                (json.dumps(decision.__dict__, sort_keys=True),),
            )
            connection.execute(
                """UPDATE runs
                   SET state='publishing', validated_sha=?, updated_at=?
                   WHERE id='run-1'""",
                ("c" * 40, "2026-01-01T00:05:00Z"),
            )
            feedback_id = connection.execute(
                "SELECT id FROM feedback_versions"
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state,
                    created_at)
                   VALUES ('revision-batch-1', 'run-1',
                           'feedback_revision_batch', 'revision-batch-1', ?,
                           'pending', ?)""",
                (
                    json.dumps({"feedback_ids": [feedback_id]}, sort_keys=True),
                    "2026-01-01T00:04:00Z",
                ),
            )

        self.assertEqual(self.service.resolve_run("run-1"), 1)

        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.publisher.calls, ["run-1"])
        self.assertEqual(self.publisher.source_shas_at_call, ["c" * 40])
        with self.db.connect() as connection:
            feedback = connection.execute(
                "SELECT state, source_sha FROM feedback_versions"
            ).fetchone()
        self.assertEqual(feedback["state"], "resolved")
        self.assertEqual(feedback["source_sha"], "c" * 40)

    def test_publication_interruption_does_not_repeat_feedback_execution(self) -> None:
        self.gateway.items = [
            self.item(
                "inline_comment",
                "inline-interrupted",
                "v1",
                "Please change this behavior",
            )
        ]
        self.publisher.fail_once = True

        self.assertEqual(self.service.resolve_run("run-1"), 0)
        self.assertEqual(len(self.executor.calls), 1)
        with self.db.connect() as connection:
            feedback = connection.execute(
                "SELECT state, source_sha FROM feedback_versions"
            ).fetchone()
        self.assertEqual(feedback["state"], "processing")
        self.assertEqual(feedback["source_sha"], "c" * 40)

        self.assertEqual(self.service.resolve_run("run-1"), 1)
        self.assertEqual(self.publisher.calls, ["run-1", "run-1"])
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.gateway.post_calls, 0)

    def test_immediate_repoll_processes_feedback_arriving_during_resolution(
        self,
    ) -> None:
        first = self.item("comment", "first", "v1", "First question?")
        second = self.item("comment", "second", "v1", "Second question?")
        self.gateway.items = [first]
        self.gateway.inject_after_first_poll = second
        self.assertEqual(self.service.resolve_run("run-1"), 2)
        self.assertEqual(self.gateway.post_calls, 2)
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            RunState.WAITING_FOR_FEEDBACK.value,
        )


if __name__ == "__main__":
    unittest.main()
