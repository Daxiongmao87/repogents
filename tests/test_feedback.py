from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from repogents.database import Database
from repogents.feedback import FeedbackDecision, FeedbackService, MiniSweFeedbackEvaluator
from repogents.github import FeedbackItem, FeedbackOutput, PullRequestInfo
from repogents.lifecycle import RunLifecycle, RunState
from repogents.sandbox import SandboxManager


class NoActivationClient:
    def list_ready_events(self, owner: str, name: str) -> list[object]:
        return []

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        return "a" * 40


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
        if (
            run["state"] == RunState.PUBLISHING.value
            and target == RunState.CLOSED
        ):
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
        self.pull = PullRequestInfo(
            node_id="PR1",
            number=11,
            url="pr-url",
            state="open",
            merged=False,
            head_branch="agent/issue-3-run-1",
            head_sha="b" * 40,
            base_branch="main",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.crash_after_post = False
        self.inject_after_first_poll: FeedbackItem | None = None

    def get_pull_request(
        self, owner: str, name: str, number: int
    ) -> PullRequestInfo:
        self.status_calls += 1
        return self.pull

    def list_feedback(self, owner: str, name: str, pull_number: int) -> list[FeedbackItem]:
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
            feedback_type="inline_comment" if feedback.feedback_type == "inline_comment" else "comment",
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


class FakeEvaluator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def evaluate(self, context: dict[str, object]) -> FeedbackDecision:
        body = str(context["feedback"]["body"])
        self.calls.append(body)
        if "change" in body.lower():
            return FeedbackDecision("revise", "valid in-scope change", "Implemented and validated the requested change.")
        if "?" in body:
            return FeedbackDecision("answer", "relevant question", "The behavior follows the repository contract.")
        if "wrong" in body.lower():
            return FeedbackDecision("decline", "request contradicts issue scope", "Declined because it contradicts the issue scope.")
        return FeedbackDecision("answer", "relevant feedback", "Acknowledged and resolved.")


class FakeExecutor:
    def __init__(self, lifecycle: RunLifecycle) -> None:
        self.lifecycle = lifecycle
        self.calls: list[tuple[str, str | None]] = []

    def execute(self, run_id: str, *, additional_context: str | None = None) -> str:
        self.calls.append((run_id, additional_context))
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        with self.lifecycle.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id=?", ("c" * 40, run_id)
            )
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        return "c" * 40


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
        if self.fail_once:
            self.fail_once = False
            return None
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE pull_requests SET validated_head_sha=?, remote_head_sha=?, updated_at=?
                   WHERE run_id=?""",
                ("c" * 40, "c" * 40, "2026-01-01T00:06:00Z", run_id),
            )
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        self.gateway.pull = replace(
            self.gateway.pull,
            head_sha="c" * 40,
            updated_at="2026-01-01T00:06:00Z",
        )
        return object()


class FakeQuietStarter:
    def __init__(self, lifecycle: RunLifecycle) -> None:
        self.lifecycle = lifecycle
        self.calls: list[str] = []

    def start(self, run_id: str) -> None:
        self.calls.append(run_id)
        state = self.lifecycle.get_run(run_id)["state"]
        if state == "resolving_feedback":
            self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        if self.lifecycle.get_run(run_id)["state"] == "waiting_for_feedback":
            self.lifecycle.transition(run_id, RunState.QUIET_PERIOD)


class MiniSweFeedbackEvaluatorTests(unittest.TestCase):
    def test_passes_stored_model_base_url_state_directory_supervisor_and_run_id(self) -> None:
        observed: dict[str, object] = {}
        state_root = Path("/model-state/feedback")

        class FakeSupervisor:
            pass

        supervisor = FakeSupervisor()

        def fake_infer(self, *, system_prompt: str, prompt: str, response_schema: dict, state_directory: Path) -> dict:
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
        self.assertEqual(prompt_data["response_schema"]["action"], "revise|answer|decline|ignore")
        self.assertTrue(
            "Return exactly one JSON object" in observed["system_prompt"]
            or "Return one JSON" in observed["system_prompt"]
        )

    def test_prompt_is_file_backed_and_not_in_system_prompt(self) -> None:
        observed: dict[str, object] = {}
        large_feedback = {"body": "x" * 100_000}

        def fake_infer(self, *, system_prompt: str, prompt: str, response_schema: dict, state_directory: Path) -> dict:
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

    def test_requires_explicit_model_when_no_constructor_or_stored_override(self) -> None:
        evaluator = MiniSweFeedbackEvaluator()
        with self.assertRaises(RuntimeError) as raised:
            evaluator.evaluate({"feedback": {"body": "Hello"}})
        self.assertIn("model", str(raised.exception).lower())

    def test_persists_durable_state_directory_per_run(self) -> None:
        state_root = Path("/model-state/feedback")
        observed_dirs: list[Path] = []

        def fake_infer(self, *, system_prompt: str, prompt: str, response_schema: dict, state_directory: Path) -> dict:
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
        checkout = self.data_root / "repositories" / "repo-1" / "runs" / "run-1" / "checkout"
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
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own', '[]',
                           'mini-swe-agent', 'openai/gpt-stored', '')"""
            )
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
        self.quiet = FakeQuietStarter(self.lifecycle)
        self.service = FeedbackService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            evaluator=self.evaluator,
            executor=self.executor,
            publisher=self.publisher,
            quiet=self.quiet,
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

    def test_context_collection_failure_leaves_feedback_pending_for_next_cycle(
        self,
    ) -> None:
        self.gateway.items = [
            self.item("comment", "context-retry", "v1", "Why is this correct?")
        ]
        with mock.patch.object(
            self.service,
            "_evaluation_context",
            side_effect=RuntimeError("temporary git read failure"),
        ):
            self.assertEqual(self.service.resolve_run("run-1"), 0)

        with self.db.connect() as connection:
            pending = connection.execute(
                "SELECT state, decision_json, processed_at FROM feedback_versions"
            ).fetchone()
        self.assertEqual(tuple(pending), ("pending", None, None))
        self.assertEqual(self.gateway.post_calls, 0)

        self.assertEqual(self.service.resolve_run("run-1"), 1)
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
            str(row["github_object_id"]): row
            for row in self.service._pending("run-1")
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

    def test_ingests_mixed_and_edited_feedback_once_but_filters_recorded_outputs(self) -> None:
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
            self.item("comment", "app-output", "v1", "Application response", author="configured-user"),
            self.item("comment", "manual-same-author", "v1", "Manual feedback", author="configured-user"),
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
            [row["github_version"] for row in rows if row["github_object_id"] == "comment-1"],
            ["v1", "v2"],
        )

    def test_answer_is_posted_once_and_never_reingested(self) -> None:
        self.gateway.items = [self.item("comment", "question-1", "v1", "Why is this correct?")]
        self.assertEqual(self.service.resolve_run("run-1"), 1)
        self.assertEqual(self.gateway.post_calls, 1)
        self.gateway.items.append(
            self.item("comment", "output-1", "2026-01-01T00:05:00Z", self.gateway.outputs[0].body)
        )
        self.assertEqual(self.service.poll_run("run-1"), 0)
        with self.db.connect() as connection:
            row = connection.execute("SELECT state FROM feedback_versions").fetchone()
            outputs = connection.execute("SELECT COUNT(*) FROM application_outputs").fetchone()[0]
        self.assertEqual(row["state"], "answered")
        self.assertEqual(outputs, 1)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.quiet.calls, ["run-1"])

    def test_response_loss_reconciles_on_next_attempt_without_duplicate_post(self) -> None:
        self.gateway.items = [self.item("comment", "question-1", "v1", "Why?")]
        self.gateway.crash_after_post = True
        self.assertEqual(self.service.resolve_run("run-1"), 0)
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"], "resolving_feedback"
        )
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
        self.assertEqual(dict(pull), {
            "state": "closed",
            "remote_head_sha": "d" * 40,
            "updated_at": "2026-01-01T00:07:00Z",
        })
        self.assertEqual(feedback_count, 0)

    def test_cancellation_during_feedback_inference_prevents_later_effects(self) -> None:
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

    def test_source_change_is_implemented_validated_and_published_to_same_pull_request(self) -> None:
        self.gateway.items = [self.item("inline_comment", "inline-1", "v1", "Please change this behavior")]
        self.assertEqual(self.service.resolve_run("run-1"), 1)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertIn("Please change", self.executor.calls[0][1])
        self.assertEqual(self.publisher.calls, ["run-1"])
        self.assertEqual(self.gateway.post_calls, 1)
        with self.db.connect() as connection:
            feedback = connection.execute("SELECT state, source_sha FROM feedback_versions").fetchone()
            pull_count = connection.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0]
            pull = connection.execute("SELECT remote_head_sha FROM pull_requests").fetchone()
        self.assertEqual(feedback["state"], "resolved")
        self.assertEqual(feedback["source_sha"], "c" * 40)
        self.assertEqual(pull_count, 1)
        self.assertEqual(pull["remote_head_sha"], "c" * 40)

    def test_external_close_during_publication_stops_before_reconciliation(self) -> None:
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

    def test_crash_after_validation_checkpoints_sha_before_publication_without_reexecution(self) -> None:
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
        self.assertEqual(self.gateway.post_calls, 1)

    def test_immediate_repoll_processes_feedback_arriving_during_resolution(self) -> None:
        first = self.item("comment", "first", "v1", "First question?")
        second = self.item("comment", "second", "v1", "Second question?")
        self.gateway.items = [first]
        self.gateway.inject_after_first_poll = second
        self.assertEqual(self.service.resolve_run("run-1"), 2)
        self.assertEqual(self.gateway.post_calls, 2)
        self.assertEqual(self.quiet.calls, ["run-1"])


if __name__ == "__main__":
    unittest.main()
