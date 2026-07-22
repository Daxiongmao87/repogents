from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from repogents.acceptance import (
    AcceptanceService,
    load_acceptance_artifact,
    render_acceptance_markdown,
)
from repogents.database import Database
from repogents.execution import AgentToolExecutor, ScriptedRuntime
from repogents.github import ActivationEvent
from repogents.lifecycle import RunLifecycle
from repogents.sandbox import RunLayout, SandboxManager
from repogents.team import TeamService


class NoActivationClient:
    def list_ready_events(self, owner: str, name: str) -> list[ActivationEvent]:
        return []

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        return "a" * 40


class NoCheckoutManager:
    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        return None


class NoSandbox:
    pass


class RuntimeQueue:
    def __init__(self, runtimes: Sequence[ScriptedRuntime]) -> None:
        self.runtimes = list(runtimes)
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, runtime: str, model: str, timeout: float) -> ScriptedRuntime:
        self.calls.append((runtime, model, timeout))
        if not self.runtimes:
            raise AssertionError("unexpected verifier runtime request")
        return self.runtimes.pop(0)


class RecordingTools:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.actions: list[dict[str, object]] = []
        self.write_screenshot = False
        self.checkout_writable: list[bool] = []

    def execute(
        self,
        member: object,
        policy: object,
        layout: RunLayout,
        action: dict[str, object],
        secrets: dict[str, str] | None = None,
        checkout_writable: bool = True,
    ) -> str:
        del member, policy, secrets
        self.checkout_writable.append(checkout_writable)
        with self.database.connect() as connection:
            verification = connection.execute(
                """SELECT claims_json FROM acceptance_verifications
                   WHERE run_id=? AND state='verifying'
                   ORDER BY attempt DESC LIMIT 1""",
                (layout.run_id,),
            ).fetchone()
        if action.get("action") == "run":
            if verification is None or json.loads(verification["claims_json"]) == []:
                raise AssertionError(
                    "acceptance plan was not durable before command execution"
                )
            if self.write_screenshot:
                target = layout.temp / "acceptance" / "proof.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"\x89PNG\r\n\x1a\nfixture-controller-screenshot")
        self.actions.append(dict(action))
        if action.get("action") == "read":
            return "VALUE = 2"
        return json.dumps(
            {
                "returncode": 0,
                "stdout": "VALUE=2",
                "stderr": "",
                "timed_out": False,
                "log_path": str(layout.logs / "acceptance.log"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class AcceptanceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.sandbox_root = self.data_root / "repositories" / "repo-1" / "sandbox" / "1"
        self.sandbox_root.mkdir(parents=True)
        self.layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        (self.layout.checkout / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.database_path = self.root / "repogents.sqlite3"
        self.db = Database(self.database_path)
        self.db.initialize()
        self.commit_sha = "b" * 40
        now = "2026-07-22T00:00:00Z"
        evidence = json.dumps(
            {
                "summary": "fixture repository",
                "instructions": [["README.md", "Run the fixture command."]],
            },
            separators=(",", ":"),
        )
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'repo-url', 'main',
                           'ready', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json,
                    evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, ?, '{}', ?, ?)""",
                (str(self.sandbox_root), evidence, now),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, ?, ?)""",
                (evidence, now),
            )
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own result',
                           '["read","write","run"]', 'mini-swe-agent',
                           'openai/gpt-fixture', '', 321)""")
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('verifier-1', 'team-1', 'verification', 'verifier',
                           'Independently verify issue behavior and scope',
                           '["read","run","git_diff"]', 'mini-swe-agent',
                           'openai/gpt-verifier', '', 432)""")
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 7, 'issue-url',
                           'Set value', 'Set VALUE to 2 and prove the output.',
                           '[{"author":"reviewer","body":"Use the real command."}]', ?)""",
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
                           'sandbox-1', 'team-1', 'main', ?, 'publishing',
                           'validating', ?, ?, ?, ?, ?)""",
                (
                    "a" * 40,
                    self.commit_sha,
                    str(self.layout.checkout),
                    str(self.layout.root),
                    now,
                    now,
                ),
            )
        self.lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=NoSandbox(),  # type: ignore[arg-type]
        )
        self.tools = RecordingTools(self.db)

    @staticmethod
    def plan(*, screenshot_required: bool = False) -> dict[str, object]:
        return {
            "action": "acceptance_plan",
            "claims": [
                {
                    "key": "value-output",
                    "claim": "The repository command emits VALUE=2.",
                    "expected": "stdout contains VALUE=2",
                    "method": "run the repository command and inspect stdout",
                }
            ],
            "screenshot_decision": {
                "required": screenshot_required,
                "reason": (
                    "The issue is visual."
                    if screenshot_required
                    else "The observable contract is nonvisual command output."
                ),
            },
        }

    def completion(
        self,
        *,
        commit_sha: str | None = None,
        evidence: list[int] | None = None,
        scope: list[dict[str, object]] | None = None,
        screenshots: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "action": "verify",
            "verdict": "pass",
            "commit_sha": commit_sha or self.commit_sha,
            "summary": "The exact commit emits the issue-required value.",
            "claim_results": [
                {
                    "key": "value-output",
                    "result": "pass",
                    "observed": "Command exited 0 and stdout contained VALUE=2.",
                    "evidence": [1] if evidence is None else evidence,
                }
            ],
            "scope": (
                [
                    {
                        "path": "app.py",
                        "claim_keys": ["value-output"],
                        "necessity": "Implements the requested value change.",
                        "result": "pass",
                    }
                ]
                if scope is None
                else scope
            ),
            "screenshots": [] if screenshots is None else screenshots,
            "limitations": [],
        }

    def service(
        self,
        runtimes: Sequence[ScriptedRuntime],
        *,
        max_actions: int = 40,
    ) -> tuple[AcceptanceService, RuntimeQueue]:
        factory = RuntimeQueue(runtimes)
        service = AcceptanceService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=NoSandbox(),  # type: ignore[arg-type]
            data_root=self.data_root,
            runtime_factory=factory,
            tools=self.tools,  # type: ignore[arg-type]
            max_actions=max_actions,
        )
        return service, factory

    def clear_attempts(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM acceptance_artifacts")
            connection.execute("DELETE FROM acceptance_evidence")
            connection.execute("DELETE FROM acceptance_verifications")

    def test_restart_resumes_durable_plan_and_observations_for_exact_sha(self) -> None:
        first_runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
            ]
        )
        service, factory = self.service([first_runtime], max_actions=2)

        with self.assertRaisesRegex(RuntimeError, "without a final verdict"):
            service.verify("run-1", self.commit_sha, ("app.py",))

        reopened = Database(self.database_path)
        reopened.initialize()
        with reopened.connect() as connection:
            attempt = connection.execute(
                "SELECT * FROM acceptance_verifications"
            ).fetchone()
            evidence = connection.execute(
                "SELECT * FROM acceptance_evidence"
            ).fetchall()
        self.assertEqual(attempt["state"], "verifying")
        self.assertEqual(attempt["attempt"], 1)
        self.assertEqual(json.loads(attempt["claims_json"])[0]["key"], "value-output")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(self.tools.checkout_writable, [False])

        second_runtime = ScriptedRuntime([self.completion()])
        service, second_factory = self.service([second_runtime])
        result = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(result["state"], "passed")
        self.assertEqual(result["commit_sha"], self.commit_sha)
        claims = cast(list[dict[str, object]], result["claims"])
        self.assertEqual(claims[0]["evidence"], [1])
        self.assertIn("VALUE=2", second_runtime.contexts[0])
        self.assertEqual(
            factory.calls[0], ("mini-swe-agent", "openai/gpt-verifier", 432.0)
        )
        self.assertEqual(second_factory.calls[0][1], "openai/gpt-verifier")
        self.assertIn(
            "Issue acceptance verification", render_acceptance_markdown(result)
        )

        cached, empty_factory = self.service([])
        self.assertEqual(cached.verify("run-1", self.commit_sha, ("app.py",)), result)
        self.assertEqual(empty_factory.calls, [])

    def test_real_sandbox_smoke_proves_committed_fixture_revision(self) -> None:
        checkout = self.layout.checkout

        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", *arguments],
                cwd=checkout,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()

        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.com")
        (checkout / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        git("add", "app.py")
        git("commit", "-qm", "base")
        base_sha = git("rev-parse", "HEAD")
        (checkout / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        git("add", "app.py")
        git("commit", "-qm", "implement fixture issue")
        candidate_sha = git("rev-parse", "HEAD")
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs SET base_sha=?, validated_sha=?
                   WHERE id='run-1'""",
                (base_sha, candidate_sha),
            )
        runtime = ScriptedRuntime(
            [
                self.plan(),
                {
                    "action": "run",
                    "argv": [
                        "python3",
                        "-c",
                        (
                            "from pathlib import Path; "
                            "value=Path('app.py').read_text(); "
                            "assert value == 'VALUE = 2\\n'; "
                            "print('VALUE=2')"
                        ),
                    ],
                },
                self.completion(commit_sha=candidate_sha),
            ]
        )
        factory = RuntimeQueue([runtime])
        sandbox = SandboxManager()
        service = AcceptanceService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=sandbox,
            data_root=self.data_root,
            runtime_factory=factory,
            tools=AgentToolExecutor(sandbox),
        )

        report = service.verify("run-1", candidate_sha, ("app.py",))
        proof = render_acceptance_markdown(report)

        self.assertEqual(report["state"], "passed")
        self.assertEqual(report["commit_sha"], candidate_sha)
        self.assertIn("VALUE=2", proof)
        self.assertIn("exit=0", proof)
        self.assertIn(candidate_sha, proof)

    def test_rejects_incomplete_or_stale_pass_reports(self) -> None:
        invalid_reports = (
            (self.completion(evidence=[]), "evidence"),
            (self.completion(evidence=[99]), "unknown evidence"),
            (self.completion(scope=[]), "changed file"),
            (self.completion(commit_sha="c" * 40), "commit SHA"),
        )
        for report, message in invalid_reports:
            with self.subTest(message=message):
                self.clear_attempts()
                runtime = ScriptedRuntime(
                    [
                        self.plan(),
                        {"action": "run", "argv": ["python3", "app.py"]},
                        report,
                    ]
                )
                service, _ = self.service([runtime])
                with self.assertRaisesRegex(RuntimeError, message):
                    service.verify("run-1", self.commit_sha, ("app.py",))
                with self.db.connect() as connection:
                    states = connection.execute(
                        "SELECT state FROM acceptance_verifications"
                    ).fetchall()
                self.assertEqual([row["state"] for row in states], ["verifying"])

    def test_passing_claim_requires_successful_behavior_command(self) -> None:
        runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "read", "path": "app.py", "start": 1, "end": 20},
                self.completion(),
            ]
        )
        service, _ = self.service([runtime])

        with self.assertRaisesRegex(RuntimeError, "behavior command"):
            service.verify("run-1", self.commit_sha, ("app.py",))

    def test_required_screenshot_is_copied_hashed_and_sha_bound(self) -> None:
        self.tools.write_screenshot = True
        screenshot = {
            "claim_key": "value-output",
            "path": "/run-data/temp/acceptance/proof.png",
            "description": "Visible result after the controller action.",
            "metadata": {"viewport": "1280x720", "scenario": "fixture output"},
        }
        runtime = ScriptedRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(screenshots=[screenshot]),
            ]
        )
        service, _ = self.service([runtime])

        result = service.verify("run-1", self.commit_sha, ("app.py",))

        artifacts = cast(list[dict[str, object]], result["artifacts"])
        self.assertEqual(len(artifacts), 1)
        artifact = artifacts[0]
        body, media_type = load_acceptance_artifact(
            self.db,
            str(artifact["id"]),
        )
        self.assertEqual(body, b"\x89PNG\r\n\x1a\nfixture-controller-screenshot")
        self.assertEqual(media_type, "image/png")
        self.assertEqual(artifact["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(artifact["commit_sha"], self.commit_sha)
        self.assertNotIn(str(self.layout.checkout), str(artifact["path"]))

    def test_superseded_screenshot_cannot_satisfy_new_sha(self) -> None:
        screenshot = {
            "claim_key": "value-output",
            "path": "/run-data/temp/acceptance/proof.png",
            "description": "Visible result after the controller action.",
            "metadata": {"viewport": "1280x720", "scenario": "fixture output"},
        }
        self.tools.write_screenshot = True
        first = ScriptedRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(screenshots=[screenshot]),
            ]
        )
        service, _ = self.service([first])
        self.assertEqual(
            service.verify("run-1", self.commit_sha, ("app.py",))["state"],
            "passed",
        )

        next_sha = "c" * 40
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id='run-1'",
                (next_sha,),
            )
        self.tools.write_screenshot = False
        second = ScriptedRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(
                    commit_sha=next_sha,
                    screenshots=[screenshot],
                ),
            ]
        )
        service, _ = self.service([second])

        with self.assertRaisesRegex(RuntimeError, "current acceptance attempt"):
            service.verify("run-1", next_sha, ("app.py",))

    def test_missing_required_screenshot_cannot_pass(self) -> None:
        runtime = ScriptedRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(),
            ]
        )
        service, _ = self.service([runtime])

        with self.assertRaisesRegex(RuntimeError, "required screenshot"):
            service.verify("run-1", self.commit_sha, ("app.py",))

    def test_missing_required_screenshot_can_block_with_evidence(self) -> None:
        blocked = self.completion()
        blocked["verdict"] = "blocked"
        blocked["summary"] = "Browser capture is unavailable on the required host."
        blocked["limitations"] = ["No compatible browser capture tool is installed."]
        blocked["claim_results"][0]["result"] = "fail"  # type: ignore[index]
        runtime = ScriptedRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                blocked,
            ]
        )
        service, _ = self.service([runtime])

        result = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["artifacts"], [])
        self.assertIn("browser capture", str(result["summary"]).lower())

    def test_feedback_commit_invalidates_prior_proof_without_deleting_history(
        self,
    ) -> None:
        first = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(),
            ]
        )
        service, _ = self.service([first])
        service.verify("run-1", self.commit_sha, ("app.py",))
        next_sha = "d" * 40
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id='run-1'", (next_sha,)
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pull-1', 'run-1', 'PR1', 9, 'pull-url',
                           'agent/issue-7-run-1', 'main', ?, ?, ?, 'open',
                           '2026-07-22T00:00:00Z',
                           '2026-07-22T00:00:00Z')""",
                ("a" * 40, next_sha, next_sha),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at, source_sha)
                   VALUES ('feedback-1', 'pull-1', 'comment', 'C1', 'v1',
                           'reviewer', 'Please revise the value behavior.',
                           'processing', '2026-07-22T00:01:00Z', ?)""",
                (next_sha,),
            )
        second_report = self.completion(commit_sha=next_sha)
        second = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                second_report,
            ]
        )
        service, _ = self.service([second])

        current = service.verify("run-1", next_sha, ("app.py",))

        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT commit_sha, state FROM acceptance_verifications
                   ORDER BY started_at, commit_sha"""
            ).fetchall()
        self.assertEqual(
            {(row["commit_sha"], row["state"]) for row in rows},
            {(self.commit_sha, "superseded"), (next_sha, "passed")},
        )
        self.assertEqual(current["commit_sha"], next_sha)


if __name__ == "__main__":
    unittest.main()
