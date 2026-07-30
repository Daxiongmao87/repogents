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
    AcceptanceUnavailable,
    _VERIFIER_RESPONSE_SCHEMA,
    _VERIFIER_SYSTEM_PROMPT,
    load_acceptance_artifact,
    render_acceptance_markdown,
)
from repogents.database import Database
from repogents.execution import AgentToolExecutor, ScriptedRuntime
from repogents.github import ActivationEvent
from repogents.lifecycle import RunLifecycle
from repogents.specification import SpecificationService, SpecificationUnavailable
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


class ReviewingRuntime(ScriptedRuntime):
    def __init__(
        self,
        actions: Sequence[dict[str, object]],
        reviews: Sequence[dict[str, object] | BaseException],
    ) -> None:
        super().__init__(actions)
        self.reviews = list(reviews)
        self.image_calls: list[dict[str, object]] = []

    def inspect_image(
        self,
        *,
        system_prompt: str,
        prompt: str,
        response_schema: dict[str, object],
        image_path: Path,
        state_directory: Path,
    ) -> dict[str, object]:
        del response_schema, state_directory
        body = image_path.read_bytes()
        self.image_calls.append(
            {
                "system_prompt": system_prompt,
                "prompt": prompt,
                "path": image_path,
                "sha256": hashlib.sha256(body).hexdigest(),
                "body": body,
            }
        )
        if not self.reviews:
            raise AssertionError("unexpected screenshot review request")
        review = self.reviews.pop(0)
        if isinstance(review, BaseException):
            raise review
        return dict(review)


class RecordingTools:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.actions: list[dict[str, object]] = []
        self.write_screenshot = False
        self.checkout_writable: list[bool] = []
        self.screenshot_bodies: list[bytes] = []
        self.remediation_stdout: str | None = None

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
            target = layout.temp / "acceptance" / "proof.png"
            if self.screenshot_bodies:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.screenshot_bodies.pop(0))
            elif self.write_screenshot:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"\x89PNG\r\n\x1a\nfixture-controller-screenshot")
        self.actions.append(dict(action))
        if action.get("action") == "read":
            if action.get("path") == "client-endpoint.txt":
                return "PORT=3000"
            return "VALUE = 2"
        payload: dict[str, object] = {
            "returncode": 0,
            "stdout": "VALUE=2",
            "stderr": "",
            "timed_out": False,
            "log_path": str(layout.logs / "acceptance.log"),
        }
        remediation = action.get("remediation")
        if isinstance(remediation, dict):
            environment = cast(dict[str, str], remediation["environment"])
            payload["configured_environment"] = {
                environment["name"]: environment["value"]
            }
            payload["stdout"] = (
                self.remediation_stdout
                if self.remediation_stdout is not None
                else (
                    "REPOGENTS_PROBE_OBSERVATION="
                    f'{{"target":"{environment["value"]}","connected":true}}'
                )
            )
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


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
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions, action_timeout_seconds)
                   VALUES
                   ('lead-1', 'team-1', 'lead', 'lead',
                    'repository delivery planner', 'Coordinate issue delivery',
                    '["read","git_diff","git_commit"]', 'mini-swe-agent',
                    'openai/gpt-fixture', '', 321),
                   ('implementation-1', 'team-1', 'implementation', 'implementer',
                    'python implementation maintainer', 'Implement source changes',
                    '["read","write","run","git_diff"]', 'mini-swe-agent',
                    'openai/gpt-fixture', '', 321),
                   ('verifier-1', 'team-1', 'verification', 'verifier',
                    'repository behavior reviewer',
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
        issue_version_id = self.lifecycle.current_issue_version("run-1")
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE activation_events SET issue_version_id=?
                   WHERE id='activation-1'""",
                (issue_version_id,),
            )
            connection.execute(
                """UPDATE runs SET validated_issue_version_id=?
                   WHERE id='run-1'""",
                (issue_version_id,),
            )
        self.specifications = SpecificationService(self.db)
        self.specification = self.specifications.submit(
            run_id="run-1",
            author_member_id="lead-1",
            issue_version_id=issue_version_id,
            items=self.specification_items(),
            reason="Specify the observable issue behavior before acceptance.",
        )
        self.specification_review = self.specifications.record_review(
            run_id="run-1",
            specification_revision_id=str(self.specification["id"]),
            reviewer_member_id="verifier-1",
            reviewer_model="openai/gpt-verifier",
            rubric_version=1,
            verdict="approved",
            summary="The issue specification is complete and independently verifiable.",
            findings=[],
        )
        self.tools = RecordingTools(self.db)

    @staticmethod
    def specification_items() -> list[dict[str, object]]:
        return [
            {
                "key": "value-output",
                "title": "Expose the requested value",
                "objective": (
                    "The repository command exposes VALUE 2 through public behavior."
                ),
                "acceptance_criteria": [
                    {
                        "key": "value-command-output",
                        "requirement": (
                            "Running the repository command exposes the requested value."
                        ),
                        "expected": (
                            "The command exits successfully and stdout contains VALUE=2."
                        ),
                    }
                ],
                "verification": [
                    {
                        "key": "run-public-command",
                        "criterion_keys": ["value-command-output"],
                        "scenario": (
                            "Run the repository command and inspect exit status and stdout."
                        ),
                    }
                ],
            }
        ]

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
                    "criterion_keys": ["value-command-output"],
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

    @staticmethod
    def pixel_review(
        *,
        verdict: str = "pass",
        observed: str = "The expected user-visible state is unobscured.",
    ) -> dict[str, object]:
        return {
            "action": "image_review",
            "verdict": verdict,
            "observed": observed,
            "reason": (
                "The pixels directly show the expected state."
                if verdict == "pass"
                else "The pixels do not show the expected state."
            ),
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

    def test_missing_current_specification_prevents_verifier_start(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM run_specification_reviews")
            connection.execute("DELETE FROM run_specification_revisions")
        runtime = ScriptedRuntime([self.plan()])
        service, _ = self.service([runtime])

        with self.assertRaises(SpecificationUnavailable):
            service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(runtime.contexts, [])
        with self.db.connect() as connection:
            attempts = connection.execute(
                "SELECT COUNT(*) FROM acceptance_verifications"
            ).fetchone()[0]
        self.assertEqual(attempts, 0)
    def test_unapproved_current_specification_prevents_verifier_start(self) -> None:
        next_items = self.specification_items()
        next_items[0]["objective"] = (
            "The command exposes VALUE 2 without unrelated output."
        )
        self.specifications.submit(
            run_id="run-1",
            author_member_id="lead-1",
            issue_version_id=str(self.specification["issue_version_id"]),
            items=next_items,
            reason="Clarify the output boundary before acceptance.",
        )
        runtime = ScriptedRuntime([self.plan()])
        service, _ = self.service([runtime])

        with self.assertRaises(SpecificationUnavailable):
            service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(runtime.contexts, [])


    def test_acceptance_plan_must_cover_every_specification_criterion(self) -> None:
        incomplete = self.plan()
        incomplete["claims"][0]["criterion_keys"] = []  # type: ignore[index]
        runtime = ScriptedRuntime([incomplete])
        service, _ = self.service([runtime], max_actions=1)

        with self.assertRaisesRegex(RuntimeError, "specification criteria"):
            service.verify("run-1", self.commit_sha, ("app.py",))

    def test_passing_report_maps_every_specification_criterion_to_evidence(
        self,
    ) -> None:
        runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(),
            ]
        )
        service, _ = self.service([runtime])

        report = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(report["state"], "passed")
        specification = cast(dict[str, object], report["specification"])
        self.assertEqual(specification["id"], self.specification["id"])
        items = cast(list[dict[str, object]], specification["items"])
        self.assertEqual(items[0]["result"], "pass")
        criteria = cast(
            list[dict[str, object]],
            items[0]["acceptance_criteria"],
        )
        self.assertEqual(criteria[0]["result"], "pass")
        self.assertEqual(criteria[0]["claim_keys"], ["value-output"])

    def test_new_specification_revision_invalidates_cached_pass(self) -> None:
        first_runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(),
            ]
        )
        first, _ = self.service([first_runtime])
        passed = first.verify("run-1", self.commit_sha, ("app.py",))
        next_items = self.specification_items()
        next_items[0]["objective"] = (
            "The repository command exposes VALUE 2 without unrelated output."
        )
        next_specification = self.specifications.submit(
            run_id="run-1",
            author_member_id="lead-1",
            issue_version_id=str(self.specification["issue_version_id"]),
            items=next_items,
            reason="Clarify the required output boundary.",
        )
        self.specifications.record_review(
            run_id="run-1",
            specification_revision_id=str(next_specification["id"]),
            reviewer_member_id="verifier-1",
            reviewer_model="openai/gpt-verifier",
            rubric_version=1,
            verdict="approved",
            summary="The clarified specification is independently verifiable.",
            findings=[],
        )
        next_runtime = ScriptedRuntime([self.plan()])
        restarted, _ = self.service([next_runtime], max_actions=1)

        with self.assertRaisesRegex(RuntimeError, "without a final verdict"):
            restarted.verify("run-1", self.commit_sha, ("app.py",))

        passed_specification = cast(dict[str, object], passed["specification"])
        self.assertNotEqual(
            passed_specification["id"],
            next_specification["id"],
        )
        prompt = json.loads(next_runtime.contexts[0])
        self.assertEqual(
            prompt["specification"]["id"],
            next_specification["id"],
        )
        with self.db.connect() as connection:
            states = connection.execute(
                """SELECT state FROM acceptance_verifications
                   ORDER BY attempt"""
            ).fetchall()
        self.assertEqual([row["state"] for row in states], ["superseded", "verifying"])


    def test_verifier_prompt_requires_internal_target_evidence_integrity(self) -> None:
        runtime = ScriptedRuntime([self.plan()])
        service, _ = self.service([runtime], max_actions=1)

        with self.assertRaisesRegex(RuntimeError, "without a final verdict"):
            service.verify("run-1", self.commit_sha, ("app.py",))

        prompt = json.loads(runtime.contexts[0])
        self.assertEqual(
            prompt["specification"]["review"]["verdict"],
            "approved",
        )
        constraints = "\n".join(cast(list[str], prompt["constraints"]))
        self.assertIn("exists", constraints)
        self.assertIn("maps to the application object under test", constraints)
        self.assertIn("User-visible claims", constraints)
        self.assertIn("primary evidence", constraints)
        self.assertIn("Reconcile", constraints)
        self.assertIn("CHROME_BIN", constraints)
        self.assertIn("dependency_services", constraints)
        self.assertIn("/run-data/dependency-delta", constraints)
        self.assertIn(
            "dependency_services",
            _VERIFIER_RESPONSE_SCHEMA["properties"],
        )
        self.assertIn("dependency_services", _VERIFIER_SYSTEM_PROMPT)
        self.assertIn("/run-data/dependency-delta", _VERIFIER_SYSTEM_PROMPT)
        self.assertIn(
            "Never require production source changes solely to make an internal acceptance probe easier",
            constraints,
        )
        self.assertIn("repository-defined client/server endpoint", constraints)
        self.assertIn("probe configuration mismatch", constraints)
        self.assertIn("correct the probe and retry", constraints)

    def test_verifier_receives_authoritative_unchanged_baseline_debt(self) -> None:
        command = ["python", "-m", "unittest"]
        findings = [
            "unittest|fail|tests.test_proxy.ProxyTests.test_external_gateway"
        ]
        now = "2026-07-22T00:00:01Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO validation_commands
                   (id, sandbox_version_id, position, command_json, source, required)
                   VALUES ('validation-command-1', 'sandbox-1', 0, ?, 'fixture', 1)""",
                (json.dumps(command, separators=(",", ":")),),
            )
            connection.execute(
                """INSERT INTO validation_baselines
                   (id, run_id, validation_command_id, command_json, base_sha,
                    mode, started_at, completed_at, exit_status, log_path,
                    findings_json)
                   VALUES ('validation-baseline-1', 'run-1',
                           'validation-command-1', ?, ?, 'delta', ?, ?, 1,
                           '/tmp/baseline.log', ?)""",
                (
                    json.dumps(command, separators=(",", ":")),
                    "a" * 40,
                    now,
                    now,
                    json.dumps(findings, separators=(",", ":")),
                ),
            )
            connection.execute(
                """INSERT INTO validation_results
                   (id, run_id, validation_command_id, commit_sha, command_json,
                    started_at, completed_at, exit_status, log_path, verdict,
                    findings_json, comparison_json)
                   VALUES ('validation-result-1', 'run-1',
                           'validation-command-1', ?, ?, ?, ?, 1,
                           '/tmp/candidate.log', 'pass', ?, ?)""",
                (
                    self.commit_sha,
                    json.dumps(command, separators=(",", ":")),
                    now,
                    now,
                    json.dumps(findings, separators=(",", ":")),
                    json.dumps(
                        {
                            "mode": "delta",
                            "baseline_count": 1,
                            "candidate_count": 1,
                            "new_count": 0,
                            "resolved_count": 0,
                            "unchanged_count": 1,
                            "new_findings": [],
                            "contract_changed": [],
                            "weakening_detected": [],
                            "output_usable": True,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )
        runtime = ScriptedRuntime([self.plan()])
        service, _ = self.service([runtime], max_actions=1)

        with self.assertRaisesRegex(RuntimeError, "without a final verdict"):
            service.verify("run-1", self.commit_sha, ("app.py",))

        prompt = json.loads(runtime.contexts[0])
        comparison = prompt["validation_comparison"]
        self.assertEqual(comparison["base_sha"], "a" * 40)
        self.assertEqual(comparison["candidate_sha"], self.commit_sha)
        self.assertEqual(len(comparison["commands"]), 1)
        command_comparison = comparison["commands"][0]
        self.assertEqual(command_comparison["baseline"]["mode"], "delta")
        self.assertEqual(command_comparison["baseline"]["findings"], findings)
        self.assertEqual(command_comparison["candidate"]["findings"], findings)
        self.assertEqual(command_comparison["new_findings"], [])
        self.assertEqual(command_comparison["resolved_findings"], [])
        self.assertEqual(command_comparison["unchanged_findings"], findings)
        self.assertEqual(command_comparison["controller_verdict"], "pass")
        self.assertEqual(command_comparison["weakening_detected"], [])
        constraints = "\n".join(cast(list[str], prompt["constraints"]))
        self.assertIn("authoritative repository regression verdict", constraints)
        self.assertIn("unchanged baseline findings", constraints)
        self.assertIn("must not reject", constraints)
        self.assertIn("new findings", constraints)
        self.assertIn("validation weakening", constraints)

    def test_initial_blocked_verdict_requires_remediation(self) -> None:
        blocked = self.completion(evidence=[1])
        blocked["verdict"] = "blocked"
        blocked["summary"] = "The first browser probe used the wrong port."
        blocked["claim_results"][0]["result"] = "fail"  # type: ignore[index]
        blocked["limitations"] = ["The browser probe endpoint did not match."]
        blocked["blocker"] = {
            "kind": "probe_configuration",
            "reason": "The browser scenario used a source-incompatible port.",
            "target": "3221",
        }
        corrected = self.completion(evidence=[4])
        runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                blocked,
                {
                    "action": "read",
                    "path": "client-endpoint.txt",
                    "start": 1,
                    "end": 20,
                },
                {
                    "action": "run",
                    "argv": ["python3", "app.py"],
                    "remediation": {
                        "kind": "probe_configuration",
                        "challenge_sequence": 2,
                        "previous_target": "3221",
                        "corrected_target": "3000",
                        "environment": {"name": "PORT", "value": "3000"},
                        "source_evidence": [3],
                    },
                },
                corrected,
            ]
        )
        service, _ = self.service([runtime])

        result = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(result["state"], "passed")
        evidence = cast(list[dict[str, object]], result["evidence"])
        rejection_action = cast(dict[str, object], evidence[1]["action"])
        self.assertEqual(rejection_action["action"], "verdict_rejected")
        self.assertEqual(rejection_action["attempted_action"], "verify")
        self.assertEqual(
            cast(dict[str, object], rejection_action["blocker"])["target"],
            "3221",
        )
        self.assertIn("remediation", str(evidence[1]["result"]))

    def test_unrelated_actions_do_not_remediate_probe_blocker(self) -> None:
        blocked = self.completion(evidence=[1])
        blocked["verdict"] = "blocked"
        blocked["summary"] = "The browser probe used the wrong endpoint."
        blocked["claim_results"][0]["result"] = "fail"  # type: ignore[index]
        blocked["limitations"] = ["The probe target did not match the client."]
        blocked["blocker"] = {
            "kind": "probe_configuration",
            "reason": "The browser scenario used a source-incompatible port.",
            "target": "3221",
        }
        runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                blocked,
                {"action": "read", "path": "app.py", "start": 1, "end": 20},
                {"action": "run", "argv": ["python3", "app.py"]},
                blocked,
            ]
        )
        service, _ = self.service([runtime], max_actions=6)

        with self.assertRaisesRegex(RuntimeError, "without a final verdict"):
            service.verify("run-1", self.commit_sha, ("app.py",))

    def test_remediation_must_observe_the_corrected_target(self) -> None:
        blocked = self.completion(evidence=[1])
        blocked["verdict"] = "blocked"
        blocked["summary"] = "The browser probe used the wrong endpoint."
        blocked["claim_results"][0]["result"] = "fail"  # type: ignore[index]
        blocked["limitations"] = ["The probe target did not match the client."]
        blocked["blocker"] = {
            "kind": "probe_configuration",
            "reason": "The browser scenario used a source-incompatible port.",
            "target": "3221",
        }
        runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                blocked,
                {
                    "action": "read",
                    "path": "client-endpoint.txt",
                    "start": 1,
                    "end": 20,
                },
                {
                    "action": "run",
                    "argv": ["python3", "app.py", "3000"],
                    "remediation": {
                        "kind": "probe_configuration",
                        "challenge_sequence": 2,
                        "previous_target": "3221",
                        "corrected_target": "3000",
                        "environment": {"name": "PORT", "value": "3000"},
                        "source_evidence": [3],
                    },
                },
                self.completion(evidence=[4]),
            ]
        )
        self.tools.remediation_stdout = "CONNECTED=true"
        service, _ = self.service([runtime], max_actions=6)

        with self.assertRaisesRegex(RuntimeError, "without a final verdict"):
            service.verify("run-1", self.commit_sha, ("app.py",))

    def test_irreducible_blocked_verdict_is_terminal_immediately(self) -> None:
        blocked = self.completion(evidence=[1])
        blocked["verdict"] = "blocked"
        blocked["summary"] = "The required external service is unavailable."
        blocked["claim_results"][0]["result"] = "fail"  # type: ignore[index]
        blocked["limitations"] = ["The required external service is unavailable."]
        blocked["blocker"] = {
            "kind": "irreducible",
            "reason": "The required external service cannot be reached.",
        }
        runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                blocked,
            ]
        )
        service, _ = self.service([runtime])

        result = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(result["state"], "blocked")
        evidence = cast(list[dict[str, object]], result["evidence"])
        self.assertEqual(len(evidence), 1)

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
                            "import os; from pathlib import Path; "
                            "value=Path('app.py').read_text(); "
                            "assert value == 'VALUE = 2\\n'; "
                            "assert os.environ['PORT'] == '3000'; "
                            "print('VALUE=2'); "
                            "print('REPOGENTS_PROBE_OBSERVATION="
                            '{"target":"3000","connected":true}\')'
                        ),
                    ],
                    "remediation": {
                        "kind": "probe_configuration",
                        "challenge_sequence": 99,
                        "previous_target": "3221",
                        "corrected_target": "3000",
                        "environment": {"name": "PORT", "value": "3000"},
                        "source_evidence": [1],
                    },
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

    def test_semantically_invalid_pass_reports_are_rejected_then_corrected(
        self,
    ) -> None:
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
                        self.completion(evidence=[1]),
                    ]
                )
                service, _ = self.service([runtime])

                result = service.verify("run-1", self.commit_sha, ("app.py",))

                self.assertEqual(result["state"], "passed")
                with self.db.connect() as connection:
                    attempts = connection.execute(
                        "SELECT state FROM acceptance_verifications"
                    ).fetchall()
                    evidence = connection.execute(
                        """SELECT sequence, action_json, result_json
                           FROM acceptance_evidence ORDER BY sequence"""
                    ).fetchall()
                self.assertEqual([row["state"] for row in attempts], ["passed"])
                self.assertEqual([row["sequence"] for row in evidence], [1, 2])
                self.assertEqual(
                    json.loads(evidence[1]["action_json"]),
                    {
                        "action": "verdict_rejected",
                        "attempted_action": "verify",
                    },
                )
                rejection = json.loads(evidence[1]["result_json"])
                self.assertIn(message, rejection["error"])
                self.assertIn(message, runtime.contexts[3])

    def test_passing_claim_without_behavior_becomes_feedback_then_recovers(
        self,
    ) -> None:
        runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "read", "path": "app.py", "start": 1, "end": 20},
                self.completion(),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(evidence=[3]),
            ]
        )
        service, _ = self.service([runtime])

        result = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(result["state"], "passed")
        evidence = cast(list[dict[str, object]], result["evidence"])
        self.assertEqual(
            [observation["sequence"] for observation in evidence],
            [1, 2, 3],
        )
        self.assertEqual(
            evidence[1]["action"],
            {"action": "verdict_rejected", "attempted_action": "verify"},
        )
        self.assertFalse(evidence[1]["successful"])
        rejection = cast(dict[str, object], evidence[1]["result"])
        self.assertIn("behavior command", str(rejection["error"]))
        self.assertIn("behavior command", runtime.contexts[3])
        claims = cast(list[dict[str, object]], result["claims"])
        self.assertEqual(claims[0]["evidence"], [3])
        with self.db.connect() as connection:
            attempt_count = connection.execute(
                "SELECT COUNT(*) FROM acceptance_verifications"
            ).fetchone()[0]
        self.assertEqual(attempt_count, 1)

    def test_required_screenshot_is_copied_hashed_and_sha_bound(self) -> None:
        self.tools.write_screenshot = True
        screenshot = {
            "claim_key": "value-output",
            "path": "/run-data/temp/acceptance/proof.png",
            "description": "Visible result after the controller action.",
            "metadata": {"viewport": "1280x720", "scenario": "fixture output"},
        }
        runtime = ReviewingRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(screenshots=[screenshot]),
            ],
            [self.pixel_review()],
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
        metadata = cast(dict[str, object], artifact["metadata"])
        review = cast(dict[str, object], metadata["pixel_review"])
        self.assertEqual(review["verdict"], "pass")
        self.assertEqual(review["sha256"], artifact["sha256"])
        self.assertEqual(review["evidence_sequence"], 2)
        markdown = render_acceptance_markdown(result)
        self.assertIn("Pixel review: **pass**", markdown)
        self.assertIn("evidence #2", markdown)

    def test_temporal_screenshot_reviews_the_visible_checkpoint(self) -> None:
        self.tools.write_screenshot = True
        plan = self.plan(screenshot_required=True)
        claims = cast(list[dict[str, object]], plan["claims"])
        claims[0].update(
            {
                "claim": "Disabled-card state persists across reconnects.",
                "expected": (
                    "The card is absent after disabling and after reconnect, "
                    "then present after enabling and a second reconnect."
                ),
                "method": (
                    "Exercise both transitions and record each visible "
                    "checkpoint in the same browser scenario."
                ),
            }
        )
        screenshot = {
            "claim_key": "value-output",
            "path": "/run-data/temp/acceptance/proof.png",
            "description": (
                "After reconnect, the Cleaner card remains absent and is "
                "offered by the visible Enable Card control."
            ),
            "metadata": {
                "viewport": "1280x720",
                "scenario": "disabled checkpoint after reconnect",
            },
        }
        runtime = ReviewingRuntime(
            [
                plan,
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(screenshots=[screenshot]),
            ],
            [
                self.pixel_review(
                    observed=(
                        "Cleaner is absent and the open Enable Card control "
                        "visibly offers Cleaner."
                    )
                )
            ],
        )
        service, _ = self.service([runtime])

        result = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(result["state"], "passed")
        review_call = runtime.image_calls[0]
        request = json.loads(str(review_call["prompt"]))
        self.assertEqual(
            request["review_scope"],
            "claim_relevant_visible_checkpoint",
        )
        self.assertEqual(
            request["transition_evidence"],
            "controller_recorded_actions",
        )
        self.assertEqual(
            request["submitted_description"],
            screenshot["description"],
        )
        self.assertIn(
            "still image",
            str(review_call["system_prompt"]),
        )
        self.assertIn(
            "For temporal claims",
            runtime.contexts[2],
        )
        self.assertIn(
            "controller-recorded action evidence",
            runtime.contexts[2],
        )

    def test_auth_modal_screenshot_is_rejected_then_corrected(self) -> None:
        auth_modal = b"\x89PNG\r\n\x1a\nauth-modal-pixels"
        dashboard = b"\x89PNG\r\n\x1a\ndashboard-card-pixels"
        self.tools.screenshot_bodies = [auth_modal, dashboard]
        screenshot = {
            "claim_key": "value-output",
            "path": "/run-data/temp/acceptance/proof.png",
            "description": "Visible result after the controller action.",
            "metadata": {"viewport": "1280x720", "scenario": "fixture output"},
        }
        runtime = ReviewingRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(screenshots=[screenshot]),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(evidence=[4], screenshots=[screenshot]),
            ],
            [
                self.pixel_review(
                    verdict="fail",
                    observed=(
                        "Only a blocking Secure Your Dashboard setup modal is "
                        "visible; the claimed dashboard state is obscured."
                    ),
                ),
                self.pixel_review(
                    observed="The dashboard visibly shows the expected card state."
                ),
            ],
        )
        service, _ = self.service([runtime])

        result = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(result["state"], "passed")
        evidence = cast(list[dict[str, object]], result["evidence"])
        self.assertEqual(
            [observation["sequence"] for observation in evidence],
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(
            cast(dict[str, object], evidence[1]["action"])["action"],
            "inspect_screenshot",
        )
        self.assertFalse(evidence[1]["successful"])
        self.assertEqual(
            evidence[2]["action"],
            {"action": "verdict_rejected", "attempted_action": "verify"},
        )
        self.assertIn(
            "Secure Your Dashboard",
            str(cast(dict[str, object], evidence[1]["result"])["observed"]),
        )
        self.assertIn("Secure Your Dashboard", runtime.contexts[3])
        self.assertTrue(evidence[4]["successful"])
        artifacts = cast(list[dict[str, object]], result["artifacts"])
        self.assertEqual(len(artifacts), 1)
        body, _ = load_acceptance_artifact(self.db, str(artifacts[0]["id"]))
        self.assertEqual(body, dashboard)
        metadata = cast(dict[str, object], artifacts[0]["metadata"])
        review = cast(dict[str, object], metadata["pixel_review"])
        self.assertEqual(review["evidence_sequence"], 5)
        self.assertEqual(review["sha256"], hashlib.sha256(dashboard).hexdigest())
        self.assertEqual(
            [call["body"] for call in runtime.image_calls],
            [auth_modal, dashboard],
        )
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM acceptance_artifacts"
                ).fetchone()[0],
                1,
            )

    def test_unavailable_pixel_review_cannot_pass_required_screenshot(
        self,
    ) -> None:
        self.tools.screenshot_bodies = [b"\x89PNG\r\n\x1a\nunreviewed-dashboard-pixels"]
        screenshot = {
            "claim_key": "value-output",
            "path": "/run-data/temp/acceptance/proof.png",
            "description": "Visible result after the controller action.",
            "metadata": {"viewport": "1280x720", "scenario": "fixture output"},
        }
        blocked = self.completion()
        blocked["verdict"] = "blocked"
        blocked["summary"] = "Required pixel review is unavailable."
        blocked["limitations"] = ["The configured vision model is unavailable."]
        blocked["claim_results"][0]["result"] = "fail"  # type: ignore[index]
        blocked["blocker"] = {
            "kind": "irreducible",
            "reason": "The controller-owned vision endpoint is unavailable.",
        }
        runtime = ReviewingRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(screenshots=[screenshot]),
                blocked,
            ],
            [RuntimeError("vision endpoint unavailable")],
        )
        service, _ = self.service([runtime])

        result = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["artifacts"], [])
        evidence = cast(list[dict[str, object]], result["evidence"])
        self.assertFalse(evidence[1]["successful"])
        self.assertIn(
            "vision endpoint unavailable",
            str(cast(dict[str, object], evidence[1]["result"])["error"]),
        )
        self.assertEqual(
            evidence[2]["action"],
            {"action": "verdict_rejected", "attempted_action": "verify"},
        )

    def test_legacy_cached_screenshot_pass_is_reverified(self) -> None:
        self.tools.write_screenshot = True
        screenshot = {
            "claim_key": "value-output",
            "path": "/run-data/temp/acceptance/proof.png",
            "description": "Visible result after the controller action.",
            "metadata": {"viewport": "1280x720", "scenario": "fixture output"},
        }
        first = ReviewingRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(screenshots=[screenshot]),
            ],
            [self.pixel_review()],
        )
        service, _ = self.service([first])
        original = service.verify("run-1", self.commit_sha, ("app.py",))
        legacy = json.loads(json.dumps(original))
        legacy["artifacts"][0]["metadata"].pop("pixel_review")
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE acceptance_verifications SET report_json=?
                   WHERE id=?""",
                (json.dumps(legacy), original["id"]),
            )
            connection.execute(
                """UPDATE acceptance_artifacts SET metadata_json='{}'
                   WHERE verification_id=?""",
                (original["id"],),
            )

        second = ReviewingRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(screenshots=[screenshot]),
            ],
            [self.pixel_review(observed="Fresh pixels prove the visible state.")],
        )
        service, _ = self.service([second])
        result = service.verify("run-1", self.commit_sha, ("app.py",))
        artifacts = cast(list[dict[str, object]], result["artifacts"])

        self.assertNotEqual(result["id"], original["id"])
        self.assertEqual(
            cast(dict[str, object], artifacts[0]["metadata"])["pixel_review"],
            {
                "verdict": "pass",
                "observed": "Fresh pixels prove the visible state.",
                "reason": "The pixels directly show the expected state.",
                "sha256": artifacts[0]["sha256"],
                "evidence_sequence": 2,
            },
        )
        self.assertGreater(len(second.contexts), 0)
        with self.db.connect() as connection:
            attempts = connection.execute(
                """SELECT attempt, state FROM acceptance_verifications
                   WHERE run_id='run-1' AND commit_sha=?
                   ORDER BY attempt""",
                (self.commit_sha,),
            ).fetchall()
        self.assertEqual(
            [(row["attempt"], row["state"]) for row in attempts],
            [(1, "passed"), (2, "passed")],
        )

    def test_superseded_screenshot_cannot_satisfy_new_sha(self) -> None:
        screenshot = {
            "claim_key": "value-output",
            "path": "/run-data/temp/acceptance/proof.png",
            "description": "Visible result after the controller action.",
            "metadata": {"viewport": "1280x720", "scenario": "fixture output"},
        }
        self.tools.write_screenshot = True
        first = ReviewingRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(screenshots=[screenshot]),
            ],
            [self.pixel_review()],
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

    def test_missing_required_screenshot_pass_is_rejected_then_corrected(
        self,
    ) -> None:
        self.tools.write_screenshot = True
        screenshot = {
            "claim_key": "value-output",
            "path": "/run-data/temp/acceptance/proof.png",
            "description": "Visible result after the controller action.",
            "metadata": {"viewport": "1280x720", "scenario": "fixture output"},
        }
        runtime = ReviewingRuntime(
            [
                self.plan(screenshot_required=True),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(),
                self.completion(evidence=[1], screenshots=[screenshot]),
            ],
            [self.pixel_review()],
        )
        service, _ = self.service([runtime])

        result = service.verify("run-1", self.commit_sha, ("app.py",))

        self.assertEqual(result["state"], "passed")
        self.assertEqual(len(cast(list[dict[str, object]], result["artifacts"])), 1)
        self.assertIn("required screenshot", runtime.contexts[3])

    def test_missing_required_screenshot_can_block_with_evidence(self) -> None:
        blocked = self.completion()
        blocked["verdict"] = "blocked"
        blocked["summary"] = "Browser capture is unavailable on the required host."
        blocked["limitations"] = ["No compatible browser capture tool is installed."]
        blocked["claim_results"][0]["result"] = "fail"  # type: ignore[index]
        blocked["blocker"] = {
            "kind": "irreducible",
            "reason": "No compatible browser executable is available.",
        }
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

    def test_same_sha_acceptance_is_not_reused_after_issue_revision(self) -> None:
        first_runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(),
            ]
        )
        first_service, _ = self.service([first_runtime])
        first = first_service.verify("run-1", self.commit_sha, ("app.py",))

        with self.db.transaction() as connection:
            initial_version_id = connection.execute(
                """SELECT current_version_id FROM issues WHERE id='issue-1'"""
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO issue_versions
                   (id, issue_id, version, previous_version_id,
                    github_updated_at, content_sha256, title, body,
                    discussion_json, observed_at)
                   VALUES ('issue-version-2', 'issue-1', 2, ?,
                           '2026-07-22T01:00:00Z', ?, 'Set value precisely',
                           'Set VALUE to 2 without an external artifact.',
                           '[{"author":"owner","body":"No artifact dependency."}]',
                           '2026-07-22T01:00:01Z')""",
                (initial_version_id, "2" * 64),
            )
            connection.execute("""UPDATE issues
                   SET current_version_id='issue-version-2',
                       title='Set value precisely',
                       body='Set VALUE to 2 without an external artifact.',
                       discussion_json=
                         '[{"author":"owner","body":"No artifact dependency."}]',
                       updated_at='2026-07-22T01:00:00Z'
                   WHERE id='issue-1'""")

        stale_service, stale_factory = self.service([])
        with self.assertRaisesRegex(
            AcceptanceUnavailable,
            "validated issue version",
        ):
            stale_service.verify("run-1", self.commit_sha, ("app.py",))
        self.assertEqual(stale_factory.calls, [])

        with self.db.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET validated_issue_version_id='issue-version-2'
                   WHERE id='run-1'""")
        revised_specification = self.specifications.submit(
            run_id="run-1",
            author_member_id="lead-1",
            issue_version_id="issue-version-2",
            items=self.specification_items(),
            reason="Bind acceptance to the revised issue contract.",
        )
        self.specifications.record_review(
            run_id="run-1",
            specification_revision_id=str(revised_specification["id"]),
            reviewer_member_id="verifier-1",
            reviewer_model="openai/gpt-verifier",
            rubric_version=1,
            verdict="approved",
            summary="The revised issue specification remains complete.",
            findings=[],
        )
        second_runtime = ScriptedRuntime(
            [
                self.plan(),
                {"action": "run", "argv": ["python3", "app.py"]},
                self.completion(),
            ]
        )
        second_service, _ = self.service([second_runtime])
        second = second_service.verify("run-1", self.commit_sha, ("app.py",))

        with self.db.connect() as connection:
            attempts = connection.execute(
                """SELECT attempt, issue_version_id, state
                   FROM acceptance_verifications
                   WHERE run_id='run-1' AND commit_sha=?
                   ORDER BY attempt""",
                (self.commit_sha,),
            ).fetchall()

        self.assertEqual(first["issue_version_id"], initial_version_id)
        self.assertEqual(second["issue_version_id"], "issue-version-2")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(
            [tuple(row) for row in attempts],
            [
                (1, initial_version_id, "superseded"),
                (2, "issue-version-2", "passed"),
            ],
        )
        revised_prompt = json.loads(second_runtime.contexts[0])
        self.assertEqual(
            revised_prompt["issue"]["body"],
            "Set VALUE to 2 without an external artifact.",
        )

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
