from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from repogents.database import Database
from repogents.execution import ExecutionService, ScriptedRuntime
from repogents.lifecycle import RunLifecycle, RunState
from repogents.sandbox import SandboxManager
from repogents.team import TeamService


class NoActivationClient:
    def list_ready_events(self, owner: str, name: str) -> list[object]:
        return []

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        return "a" * 40


class NoCheckoutManager:
    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        return None


class RuleAwareRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def next_action(self, context: str, state_directory: Path) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            target = "allowed.py" if "Do not modify locked.py" in context else "locked.py"
            return {
                "action": "replace",
                "path": target,
                "old": "return 1",
                "new": "return 2",
                "count": 1,
            }
        if self.calls == 2:
            return {
                "action": "replace",
                "path": "value.py",
                "old": "return 1",
                "new": "return 2",
                "count": 1,
            }
        return {"action": "finish", "summary": "repository instruction honored"}


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.checkout = self.data_root / "repositories" / "repo-1" / "runs" / "run-1" / "checkout"
        self.checkout.mkdir(parents=True)
        (self.checkout / "value.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (self.checkout / "test_value.py").write_text(
            "import unittest\nfrom value import value\n\n"
            "class ValueTest(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(value(), 2)\n",
            encoding="utf-8",
        )
        self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        self._git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "base")
        self.base_sha = self._git("rev-parse", "HEAD").strip()
        self.sandbox_root = self.data_root / "repositories" / "repo-1" / "sandbox" / "1"
        self.sandbox_root.mkdir(parents=True)
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        run_root = self.checkout.parent
        policy = {
            "persistent_root": str(self.sandbox_root),
            "allowed_host_paths": [],
            "allowed_services": [],
            "secret_bindings": [],
        }
        now = "2026-01-01T00:00:00Z"
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
                   VALUES ('sandbox-1', 'repo-1', 1, ?, ?, ?, ?)""",
                (
                    str(self.sandbox_root),
                    json.dumps(policy),
                    json.dumps({"instruction_files": [], "summary": "small Python fixture"}),
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO validation_commands
                   (id, sandbox_version_id, position, command_json, source, required)
                   VALUES ('validation-command-1', 'sandbox-1', 0, ?, 'fixture', 1)""",
                (json.dumps(["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]),),
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
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own result',
                           '[\"read\",\"write\",\"run\",\"git_diff\",\"git_commit\"]',
                           'mini-swe-agent', 'test/stored', '')"""
            )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 3, 'issue-url',
                           'Return two', 'Make value() return 2', '[]', ?)""",
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
                    base_sha, state, checkout_path, run_path, created_at, updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'activation-1',
                           'sandbox-1', 'team-1', 'main', ?, 'queued', ?, ?, ?, ?)""",
                (self.base_sha, str(self.checkout), str(run_root), now, now),
            )
            connection.execute(
                """INSERT INTO agent_assignments
                   (id, run_id, team_member_id, reasoning, assigned_at)
                   VALUES ('fixture-assignment', 'run-1', 'lead-1',
                           'Explicit fixture lead assignment', ?)""",
                (now,),
            )
            connection.execute(
                """UPDATE repositories SET current_sandbox_version_id='sandbox-1',
                                             current_team_version_id='team-1'
                   WHERE id='repo-1'"""
            )
        self.sandbox = SandboxManager()
        self.lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.checkout,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout

    def service(self, actions: list[dict[str, object]]) -> tuple[ExecutionService, ScriptedRuntime]:
        runtime = ScriptedRuntime(actions)
        return (
            ExecutionService(
                database=self.db,
                lifecycle=self.lifecycle,
                teams=TeamService(self.db),
                sandbox=self.sandbox,
                runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: runtime,
                max_actions=20,
                max_revision_cycles=3,
            ),
            runtime,
        )

    def test_agent_edits_only_isolated_checkout_and_validates_exact_commit(self) -> None:
        service, runtime = self.service(
            [
                {"action": "read", "path": "value.py"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "run", "argv": ["python3", "-m", "unittest", "test_value.py"]},
                {"action": "finish", "summary": "value now returns two"},
            ]
        )
        validated_sha = service.execute("run-1")
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "publishing")
        self.assertEqual(run["validated_sha"], validated_sha)
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), validated_sha)
        self.assertEqual((self.checkout / "value.py").read_text(encoding="utf-8"), "def value():\n    return 2\n")
        with self.db.connect() as connection:
            validations = connection.execute("SELECT * FROM validation_results").fetchall()
            assignments = connection.execute("SELECT * FROM agent_assignments").fetchall()
        self.assertEqual(len(validations), 1)
        self.assertEqual(validations[0]["commit_sha"], validated_sha)
        self.assertEqual(validations[0]["exit_status"], 0)
        self.assertEqual(len(assignments), 1)
        self.assertTrue(runtime.contexts)

    def test_inspection_note_survives_action_quantum_and_guides_edit(self) -> None:
        first_runtime = ScriptedRuntime(
            [
                {
                    "action": "note",
                    "summary": "value.py returns one; replace it with two next.",
                }
            ]
        )
        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: first_runtime,
            max_actions=1,
        )

        with mock.patch.object(
            first.tools,
            "execute",
            wraps=first.tools.execute,
        ) as tool_execute:
            self.assertIsNone(first.execute("run-1"))
        tool_execute.assert_not_called()
        second_runtime = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented stored note"},
            ]
        )
        restarted = ExecutionService(
            database=Database(self.db.path),
            lifecycle=self.lifecycle,
            teams=TeamService(Database(self.db.path)),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: second_runtime,
            max_actions=10,
        )

        self.assertIsNotNone(restarted.execute("run-1"))
        self.assertIn(
            "Lead note: value.py returns one; replace it with two next.",
            second_runtime.contexts[0],
        )

    def test_repeated_note_without_repository_mutation_is_rejected(self) -> None:
        service, runtime = self.service(
            [
                {
                    "action": "note",
                    "summary": "value.py returns one; replace it with two next.",
                },
                {
                    "action": "assign",
                    "members": ["lead"],
                    "reason": "repeat the existing assignment",
                },
                {"action": "read", "path": "value.py"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 1",
                    "count": 1,
                },
                {
                    "action": "write",
                    "path": "value.py",
                    "content": "def value():\n    return 1\n",
                },
                {
                    "action": "note",
                    "summary": "value.py still returns one; replace it with two next.",
                },
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {
                    "action": "note",
                    "summary": "value.py now returns two; finish next.",
                },
                {"action": "finish", "summary": "implemented the recorded next action"},
            ]
        )

        self.assertIsNotNone(service.execute("run-1"))
        self.assertEqual(
            (self.checkout / "value.py").read_text(encoding="utf-8"),
            "def value():\n    return 2\n",
        )
        self.assertTrue(
            any(
                "a coordination note already records an exact next action"
                in context
                for context in runtime.contexts
            )
        )
        self.assertIn(
            "Lead note: value.py now returns two; finish next.",
            runtime.contexts[-1],
        )

    def test_uses_runtime_and_model_recorded_on_stored_lead(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE team_members SET runtime='mini-swe-agent', "
                "model='openai/stored-model', action_timeout_seconds=321 "
                "WHERE id='lead-1'"
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-2', 'repo-1', 2, '{}', ?)""",
                ("2026-01-02T00:00:00Z",),
            )
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('lead-2', 'team-2', 'lead', 'lead', 'Own later runs',
                           '["read"]', 'mini-swe-agent', 'openai/new-model', '', 777)"""
            )
            connection.execute(
                """UPDATE repositories SET current_team_version_id='team-2'
                   WHERE id='repo-1'"""
            )
        runtime = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "used stored model"},
            ]
        )
        requested: list[tuple[str, str, float]] = []

        def runtime_factory(
            stored_runtime: str,
            stored_model: str,
            stored_timeout: float,
        ) -> ScriptedRuntime:
            requested.append(
                (stored_runtime, stored_model, stored_timeout)
            )
            return runtime

        restarted_database = Database(self.db.path)
        restarted_database.initialize()
        service = ExecutionService(
            database=restarted_database,
            lifecycle=self.lifecycle,
            teams=TeamService(restarted_database),
            sandbox=self.sandbox,
            runtime_factory=runtime_factory,
            max_actions=10,
        )
        self.assertIsNotNone(service.execute("run-1"))
        self.assertEqual(
            requested,
            [("mini-swe-agent", "openai/stored-model", 321)],
        )
        self.assertTrue(runtime.contexts)

    def test_rejects_unsupported_stored_runtime(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE team_members SET runtime='shell', model='unsafe' WHERE id='lead-1'"
            )
        factory = mock.Mock()
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=factory,
            max_actions=10,
        )
        with self.assertRaisesRegex(ValueError, "unsupported stored model runtime"):
            service.execute("run-1")
        factory.assert_not_called()


    def test_cancellation_during_inference_prevents_later_effects(self) -> None:
        lifecycle = self.lifecycle

        class CancelDuringInference:
            def next_action(
                self, context: str, state_directory: Path
            ) -> dict[str, object]:
                lifecycle.transition(
                    "run-1", RunState.CANCELED, reason="user canceled during inference"
                )
                return {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                }

        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: CancelDuringInference(),
            max_actions=10,
        )
        self.assertIsNone(service.execute("run-1"))
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "canceled")
        self.assertEqual(
            (self.checkout / "value.py").read_text(encoding="utf-8"),
            "def value():\n    return 1\n",
        )
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), self.base_sha)
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM validation_results").fetchone()[0],
                0,
            )

    def test_command_scoped_secret_is_authorized_redacted_and_not_persisted(self) -> None:
        command = [
            "python3",
            "-c",
            "import os; print(os.environ['FIXTURE_TOKEN'])",
        ]
        fixture_secret = 'fixture-"canary"\\line\nvalue'
        bindings = [
            {
                "name": "FIXTURE_TOKEN",
                "reference": "secret://fixture",
                "commands": [command],
            },
            {
                "name": "OTHER_TOKEN",
                "reference": "secret://other",
                "commands": [["python3", "-c", "print('other')"]],
            },
        ]
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT policy_json FROM sandbox_versions WHERE id='sandbox-1'"
            ).fetchone()
            policy = json.loads(row["policy_json"])
            policy["secret_bindings"] = bindings
            connection.execute(
                "UPDATE sandbox_versions SET policy_json=? WHERE id='sandbox-1'",
                (json.dumps(policy),),
            )
        runtime = ScriptedRuntime(
            [
                {"action": "run", "argv": command},
                {
                    "action": "note",
                    "summary": (
                        f"Observed {fixture_secret}; edit next. "
                        + ("x" * 5_000)
                    ),
                },
                {
                    "action": "read",
                    "path": f"{fixture_secret}/../../escape.txt",
                },
            ]
        )
        resolved: list[str] = []

        def resolve(reference: str) -> str:
            resolved.append(reference)
            return {
                "secret://fixture": fixture_secret,
                "secret://other": "other-canary-value",
            }[reference]

        original_run = self.sandbox.run
        dispatched: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

        def record_run(
            policy: object,
            layout: object,
            argv: tuple[str, ...],
            **kwargs: object,
        ) -> object:
            secrets = kwargs.get("secrets")
            dispatched.append((argv, secrets if isinstance(secrets, dict) else None))
            return original_run(policy, layout, argv, **kwargs)

        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: runtime,
            secret_resolver=resolve,
            max_actions=3,
        )
        with mock.patch.object(self.sandbox, "run", side_effect=record_run):
            self.assertIsNone(service.execute("run-1"))
        history_path = self.checkout.parent / "agent-state" / "action-history.json"
        stored_history = json.loads(history_path.read_text(encoding="utf-8"))
        note_entry = next(
            entry for entry in stored_history if entry.startswith("Lead note:")
        )
        self.assertLessEqual(len(note_entry), 2_020)
        restarted_runtime = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "secret stayed scoped"},
            ]
        )
        restarted_database = Database(self.db.path)
        restarted = ExecutionService(
            database=restarted_database,
            lifecycle=self.lifecycle,
            teams=TeamService(restarted_database),
            sandbox=self.sandbox,
            runtime_factory=(
                lambda _stored_runtime, _stored_model, _stored_timeout:
                restarted_runtime
            ),
            secret_resolver=resolve,
            max_actions=10,
        )
        with mock.patch.object(self.sandbox, "run", side_effect=record_run):
            self.assertIsNotNone(restarted.execute("run-1"))
        self.assertEqual(resolved, ["secret://fixture"])
        self.assertIn(
            (tuple(command), {"FIXTURE_TOKEN": fixture_secret}),
            dispatched,
        )
        self.assertFalse(
            any(
                secrets and "OTHER_TOKEN" in secrets
                for _dispatched_command, secrets in dispatched
            )
        )
        observed = "\n".join(
            [
                *stored_history,
                *runtime.contexts,
                *restarted_runtime.contexts,
                *(
                    path.read_text(encoding="utf-8")
                    for path in (self.checkout.parent / "logs").glob(
                        "command-*.json"
                    )
                ),
            ]
        )
        self.assertIn("[REDACTED]", observed)
        self.assertNotIn(fixture_secret, observed)
        self.assertNotIn(json.dumps(fixture_secret)[1:-1], observed)

    def test_resolved_command_secret_returns_to_agent_for_automatic_correction(self) -> None:
        command = [
            "python3",
            "-c",
            "import os; print(os.environ['FIXTURE_TOKEN'])",
        ]
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT policy_json FROM sandbox_versions WHERE id='sandbox-1'"
            ).fetchone()
            policy = json.loads(row["policy_json"])
            policy["secret_bindings"] = [
                {
                    "name": "FIXTURE_TOKEN",
                    "reference": "secret://fixture",
                    "commands": [command],
                }
            ]
            connection.execute(
                "UPDATE sandbox_versions SET policy_json=? WHERE id='sandbox-1'",
                (json.dumps(policy),),
            )
        runtime = ScriptedRuntime(
            [
                {"action": "run", "argv": command},
                {
                    "action": "write",
                    "path": "value.py",
                    "content": "fixture-canary-value\n",
                },
                {"action": "finish", "summary": "attempted secret leak"},
                {
                    "action": "write",
                    "path": "value.py",
                    "content": "def value():\n    return 2\n",
                },
                {"action": "finish", "summary": "removed the secret"},
            ]
        )
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: runtime,
            secret_resolver=lambda reference: {
                "secret://fixture": "fixture-canary-value"
            }[reference],
            max_actions=10,
        )

        validated_sha = service.execute("run-1")

        run = self.lifecycle.get_run("run-1")
        self.assertIsNotNone(validated_sha)
        self.assertEqual(run["state"], "publishing")
        self.assertNotEqual(self._git("rev-parse", "HEAD").strip(), self.base_sha)
        self.assertTrue(
            any("potential secret" in context for context in runtime.contexts)
        )
        self.assertTrue(
            all("fixture-canary-value" not in context for context in runtime.contexts)
        )

    def test_stored_repository_instruction_constrains_agent_edit(self) -> None:
        (self.checkout / "allowed.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        (self.checkout / "locked.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT evidence_json FROM sandbox_versions WHERE id='sandbox-1'"
            ).fetchone()
            evidence = json.loads(row["evidence_json"])
            evidence["instructions"] = [
                ["AGENTS.md", "Do not modify locked.py; use allowed.py instead."]
            ]
            connection.execute(
                "UPDATE sandbox_versions SET evidence_json=? WHERE id='sandbox-1'",
                (json.dumps(evidence),),
            )
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: RuleAwareRuntime(),
            max_actions=10,
        )
        self.assertIsNotNone(service.execute("run-1"))
        self.assertIn(
            "return 2",
            (self.checkout / "allowed.py").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "return 1",
            (self.checkout / "locked.py").read_text(encoding="utf-8"),
        )

    def test_assignment_reason_redacts_prior_command_secret_and_is_bounded(
        self,
    ) -> None:
        command = [
            "python3",
            "-c",
            "import os; print(os.environ['FIXTURE_TOKEN'])",
        ]
        fixture_secret = 'assignment-"secret"\\line\nvalue'
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM agent_assignments WHERE run_id='run-1'"
            )
            row = connection.execute(
                "SELECT policy_json FROM sandbox_versions WHERE id='sandbox-1'"
            ).fetchone()
            policy = json.loads(row["policy_json"])
            policy["secret_bindings"] = [
                {
                    "name": "FIXTURE_TOKEN",
                    "reference": "secret://fixture",
                    "commands": [command],
                }
            ]
            connection.execute(
                "UPDATE sandbox_versions SET policy_json=? WHERE id='sandbox-1'",
                (json.dumps(policy),),
            )
        runtime = ScriptedRuntime(
            [
                {"action": "run", "argv": command},
                {
                    "action": "assign",
                    "members": ["lead"],
                    "reason": (
                        f"Observed {fixture_secret}; lead-only issue. "
                        + ("x" * 5_000)
                    ),
                },
            ]
        )
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: runtime,
            secret_resolver=lambda _reference: fixture_secret,
            max_actions=3,
        )

        self.assertIsNone(service.execute("run-1"))

        assignments = TeamService(self.db).assignments_for_run("run-1")
        self.assertEqual(len(assignments), 1)
        self.assertLessEqual(len(assignments[0].reasoning), 2_000)
        history = (
            self.checkout.parent / "agent-state" / "action-history.json"
        ).read_text(encoding="utf-8")
        observed = assignments[0].reasoning + "\n" + history
        self.assertIn("[REDACTED]", observed)
        self.assertNotIn(fixture_secret, observed)
        self.assertNotIn(json.dumps(fixture_secret)[1:-1], observed)

    def test_stored_lead_explicitly_selects_lead_only_for_small_issue(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM agent_assignments WHERE run_id='run-1'"
            )
        reason = "The one-line source change and its focused test need only the lead."
        assigning = ScriptedRuntime(
            [{"action": "assign", "members": ["lead"], "reason": reason}]
        )
        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _stored_timeout: assigning,
        )

        self.assertIsNone(first.execute("run-1"))

        assignments = TeamService(self.db).assignments_for_run("run-1")
        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead"],
        )
        self.assertEqual(assignments[0].reasoning, reason)
        service, _runtime = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented the focused change"},
            ]
        )
        self.assertIsNotNone(service.execute("run-1"))

    def test_stored_lead_assigns_and_runs_selected_team_members_across_restart(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM agent_assignments WHERE run_id='run-1'")
            connection.execute(
                "UPDATE team_members SET model='test/lead' WHERE id='lead-1'"
            )
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES
                   ('implementation-1', 'team-1', 'implementation', 'implementer',
                    'Implement the bounded source change', '[\"read\",\"write\",\"run\"]',
                    'mini-swe-agent', 'test/implementation', 'Follow implementation instructions'),
                   ('verification-1', 'team-1', 'verification', 'verifier',
                    'Independently verify behavior', '[\"read\",\"run\"]',
                    'mini-swe-agent', 'test/verification', 'Follow verification instructions')"""
            )
        assignment_reason = (
            "Implementation changes the function; verification independently runs its test."
        )
        assigning_lead = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": ["lead", "implementation", "verification"],
                    "reason": assignment_reason,
                }
            ]
        )
        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _stored_timeout: assigning_lead,
            max_actions=5,
        )

        self.assertIsNone(first.execute("run-1"))

        assignments = TeamService(self.db).assignments_for_run("run-1")
        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead", "implementation", "verification"],
        )
        self.assertTrue(
            all(assignment.reasoning == assignment_reason for assignment in assignments)
        )
        self.assertIn('"stable_key": "implementation"', assigning_lead.contexts[0])
        self.assertIn('"repository_evidence"', assigning_lead.contexts[0])

        lead = ScriptedRuntime(
            [{"action": "finish", "summary": "integrated member work"}]
        )
        implementation = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented return value"},
            ]
        )
        class FailOnceRuntime(ScriptedRuntime):
            def __init__(self, actions: list[dict[str, object]]) -> None:
                super().__init__(actions)
                self.failed = False

            def next_action(
                self, context: str, state_directory: Path
            ) -> dict[str, object]:
                if not self.failed:
                    self.failed = True
                    self.contexts.append(context)
                    raise TimeoutError("verification inference interrupted")
                return super().next_action(context, state_directory)

        verification = FailOnceRuntime(
            [
                {
                    "action": "run",
                    "argv": ["python3", "-m", "unittest", "test_value.py"],
                },
                {"action": "finish", "summary": "verified return value"},
            ]
        )
        runtimes = {
            "test/lead": lead,
            "test/implementation": implementation,
            "test/verification": verification,
        }
        runtime_requests: list[tuple[str, str, float]] = []

        def runtime_factory(
            stored_runtime: str,
            stored_model: str,
            stored_timeout: float,
        ):
            runtime_requests.append(
                (stored_runtime, stored_model, stored_timeout)
            )
            return runtimes[stored_model]

        restarted = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=runtime_factory,
            max_actions=10,
        )

        with self.assertRaisesRegex(TimeoutError, "verification inference interrupted"):
            restarted.execute("run-1")
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")

        resumed = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=runtime_factory,
            max_actions=10,
        )
        validated_sha = resumed.execute("run-1")

        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertEqual(
            runtime_requests,
            [
                ("mini-swe-agent", "test/implementation", 300),
                ("mini-swe-agent", "test/verification", 300),
                ("mini-swe-agent", "test/verification", 300),
                ("mini-swe-agent", "test/lead", 300),
            ],
        )
        self.assertIn('"stable_key": "implementation"', implementation.contexts[0])
        self.assertIn("Follow implementation instructions", implementation.contexts[0])
        self.assertIn('"stable_key": "verification"', verification.contexts[0])
        self.assertIn("Follow verification instructions", verification.contexts[0])
        self.assertIn(
            "Member implementation finished: implemented return value",
            lead.contexts[0],
        )
        self.assertIn(
            "Member verification finished: verified return value",
            lead.contexts[0],
        )

    def test_action_history_is_bounded_for_model_context(self) -> None:
        actions: list[dict[str, object]] = [
            {
                "action": "run",
                "argv": ["python3", "-c", "print('x' * 20000)"],
                "timeout": 30,
            }
            for _ in range(16)
        ]
        actions.extend(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "bounded history"},
            ]
        )
        service, runtime = self.service(actions)
        self.assertIsNotNone(service.execute("run-1"))
        self.assertLess(max(len(context) for context in runtime.contexts), 120_000)

    def test_action_quantum_yields_and_restart_continues_same_run_without_block(
        self,
    ) -> None:
        first_runtime = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                }
            ]
        )
        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: first_runtime,
            max_actions=1,
            max_revision_cycles=3,
        )

        self.assertIsNone(first.execute("run-1"))
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")
        with self.db.connect() as connection:
            blocked = connection.execute(
                "SELECT COUNT(*) FROM run_transitions WHERE run_id=? AND to_state='blocked'",
                ("run-1",),
            ).fetchone()[0]
        self.assertEqual(blocked, 0)

        second_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "continued after execution quantum"}]
        )
        restarted = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: second_runtime,
            max_actions=1,
            max_revision_cycles=3,
        )

        validated_sha = restarted.execute("run-1")

        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertIn("Action history:", second_runtime.contexts[0])
        self.assertIn('"action":"replace"', second_runtime.contexts[0])

    def test_transient_inference_failure_preserves_run_for_next_execution(self) -> None:
        class FailOnceRuntime:
            def __init__(self) -> None:
                self.calls = 0

            def next_action(
                self, context: str, state_directory: Path
            ) -> dict[str, object]:
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("temporary model timeout")
                if self.calls == 2:
                    return {
                        "action": "replace",
                        "path": "value.py",
                        "old": "return 1",
                        "new": "return 2",
                        "count": 1,
                    }
                return {"action": "finish", "summary": "recovered automatically"}

        runtime = FailOnceRuntime()
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: runtime,
            max_actions=10,
        )

        with self.assertRaisesRegex(TimeoutError, "temporary model timeout"):
            service.execute("run-1")

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")
        with self.db.connect() as connection:
            blocked = connection.execute(
                "SELECT COUNT(*) FROM run_transitions WHERE run_id=? AND to_state='blocked'",
                ("run-1",),
            ).fetchone()[0]
        self.assertEqual(blocked, 0)
        self.assertIsNotNone(service.execute("run-1"))
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")

    def test_transient_validation_infrastructure_failure_resumes_validation(
        self,
    ) -> None:
        first, _runtime = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "ready for validation"},
            ]
        )
        with mock.patch.object(
            first, "_validate", side_effect=RuntimeError("temporary validator outage")
        ):
            with self.assertRaisesRegex(RuntimeError, "temporary validator outage"):
                first.execute("run-1")

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "validating")
        restarted, runtime = self.service([])
        self.assertIsNotNone(restarted.execute("run-1"))
        self.assertEqual(runtime.contexts, [])
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")

    def test_source_revision_reason_reaches_agent_context(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='implementing',
                       reason='scope review rejected: Docker cache optimization is unrelated'
                   WHERE id='run-1'"""
            )
        service, runtime = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "removed unrelated change"},
            ]
        )
        self.assertIsNotNone(service.execute("run-1"))
        self.assertIn("Docker cache optimization is unrelated", runtime.contexts[0])

    def test_missing_repository_path_is_returned_to_agent_for_recovery(self) -> None:
        service, runtime = self.service(
            [
                {"action": "list", "path": "spec"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "recovered from absent optional path"},
            ]
        )
        validated_sha = service.execute("run-1")
        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertTrue(
            any("repository tool failed" in context for context in runtime.contexts)
        )

    def test_restart_from_validating_resumes_dirty_checkout_without_reasking_agent(self) -> None:
        (self.checkout / "value.py").write_text(
            "def value():\n    return 2\n", encoding="utf-8"
        )
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='validating', last_completed_state='implementing'
                   WHERE id='run-1'"""
            )
        service, runtime = self.service([])
        validated_sha = service.execute("run-1")
        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), validated_sha)
        self.assertEqual(runtime.contexts, [])
        with self.db.connect() as connection:
            result = connection.execute(
                "SELECT commit_sha, exit_status FROM validation_results"
            ).fetchone()
        self.assertEqual((result["commit_sha"], result["exit_status"]), (validated_sha, 0))

    def test_failed_validation_returns_to_agent_then_records_new_passing_sha(self) -> None:
        service, _ = self.service(
            [
                {"action": "replace", "path": "value.py", "old": "return 1", "new": "return 3", "count": 1},
                {"action": "finish", "summary": "first attempt"},
                {"action": "replace", "path": "value.py", "old": "return 3", "new": "return 2", "count": 1},
                {"action": "finish", "summary": "corrected after validation"},
            ]
        )
        passing_sha = service.execute("run-1")
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT commit_sha, exit_status FROM validation_results ORDER BY started_at"
            ).fetchall()
        self.assertEqual([row["exit_status"] for row in rows], [1, 0])
        self.assertEqual(rows[-1]["commit_sha"], passing_sha)
        self.assertNotEqual(rows[0]["commit_sha"], rows[1]["commit_sha"])

    def test_missing_validation_commands_block_without_autonomous_resume(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM validation_commands")
        service, _ = self.service(
            [
                {"action": "replace", "path": "value.py", "old": "return 1", "new": "return 2", "count": 1},
                {"action": "finish", "summary": "implementation complete"},
            ]
        )
        self.assertIsNone(service.execute("run-1"))
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("validation commands", run["reason"])

    def test_validation_mutation_cannot_pass_and_returns_to_agent(self) -> None:
        mutating_command = [
            "python3",
            "-c",
            "from pathlib import Path; Path('value.py').write_text('def value():\\n    return 99\\n')",
        ]
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE validation_commands SET command_json=?",
                (json.dumps(mutating_command),),
            )
        service, _ = self.service(
            [
                {"action": "replace", "path": "value.py", "old": "return 1", "new": "return 2", "count": 1},
                {"action": "finish", "summary": "implementation complete"},
            ]
        )
        service.max_revision_cycles = 1
        self.assertIsNone(service.execute("run-1"))
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "implementing")
        self.assertIsNone(run["validated_sha"])
        self.assertEqual(self._git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual((self.checkout / "value.py").read_text(encoding="utf-8"), "def value():\n    return 2\n")
        history = json.loads(
            (self.checkout.parent / "agent-state" / "action-history.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(any("Validation for commit" in entry for entry in history))

    def test_path_escape_error_is_returned_to_agent_for_safe_correction(self) -> None:
        service, runtime = self.service(
            [
                {"action": "read", "path": "../../etc/passwd"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "used an isolated path"},
            ]
        )

        result = service.execute("run-1")

        self.assertIsNotNone(result)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertTrue(
            any(
                '"action":"read","path":"../../etc/passwd"' in context
                for context in runtime.contexts
            )
        )
        self.assertNotIn("root:", "\n".join(runtime.contexts))

    def test_large_rejected_write_retains_action_and_path(self) -> None:
        service, runtime = self.service(
            [
                {
                    "action": "write",
                    "path": "../../escape.txt",
                    "content": "x" * 5_000,
                },
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "corrected rejected write"},
            ]
        )

        self.assertIsNotNone(service.execute("run-1"))
        self.assertIn(
            '"action":"write","path":"../../escape.txt"',
            runtime.contexts[1],
        )

    def test_model_block_action_records_irreducible_reason(self) -> None:
        service, _ = self.service(
            [{"action": "block", "reason": "Required licensed SDK is not available"}]
        )
        self.assertIsNone(service.execute("run-1"))
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("licensed SDK", run["reason"])





class MiniSweExecutionIntegrationTests(unittest.TestCase):
    """Tests for mini-SWE runtime integration: persistence, rejection, cancellation."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'repo-url', 'main', 'ready', ?, ?)""",
                (now, now),
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
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own result',
                           '[\"read\",\"write\",\"run\"]',
                           'mini-swe-agent', 'openai/gpt-4', '', 321)"""
            )

    def test_stored_runtime_model_timeout_survives_database_reopen(self) -> None:
        reopened = Database(self.db.path)
        reopened.initialize()
        with reopened.connect() as connection:
            row = connection.execute(
                """SELECT runtime, model, action_timeout_seconds
                   FROM team_members WHERE id='lead-1'"""
            ).fetchone()
        self.assertEqual(row["runtime"], "mini-swe-agent")
        self.assertEqual(row["model"], "openai/gpt-4")
        self.assertEqual(row["action_timeout_seconds"], 321)

    def test_obsolete_omp_runtime_rejected_without_fallback(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('legacy-1', 'team-1', 'legacy', 'verifier', 'Legacy member',
                           '[\"read\"]', 'omp', 'openai/legacy', '', 300)"""
            )
        reopened = Database(self.db.path)
        reopened.initialize()
        with reopened.connect() as connection:
            row = connection.execute(
                """SELECT runtime FROM team_members WHERE id='legacy-1'"""
            ).fetchone()
        self.assertEqual(row["runtime"], "omp")


class MiniSweRejectionTests(unittest.TestCase):
    """Tests that obsolete 'omp' runtime is rejected at execution time."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.checkout = self.data_root / "repositories" / "repo-1" / "runs" / "run-1" / "checkout"
        self.checkout.mkdir(parents=True)
        (self.checkout / "value.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        self._git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "base")
        self.base_sha = self._git("rev-parse", "HEAD").strip()
        self.sandbox_root = self.data_root / "repositories" / "repo-1" / "sandbox" / "1"
        self.sandbox_root.mkdir(parents=True)
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        run_root = self.checkout.parent
        policy = {
            "persistent_root": str(self.sandbox_root),
            "allowed_host_paths": [],
            "allowed_services": [],
            "secret_bindings": [],
        }
        now = "2026-01-01T00:00:00Z"
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
                   VALUES ('sandbox-1', 'repo-1', 1, ?, ?, ?, ?)""",
                (
                    str(self.sandbox_root),
                    json.dumps(policy),
                    json.dumps({"instruction_files": [], "summary": "small Python fixture"}),
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO validation_commands
                   (id, sandbox_version_id, position, command_json, source, required)
                   VALUES ('validation-command-1', 'sandbox-1', 0, ?, 'fixture', 1)""",
                (json.dumps(["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]),),
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
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own result',
                           '[\"read\",\"write\",\"run\",\"git_diff\",\"git_commit\"]',
                           'omp', 'test/stored', '')"""
            )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 3, 'issue-url',
                           'Return two', 'Make value() return 2', '[]', ?)""",
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
                    base_sha, state, checkout_path, run_path, created_at, updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'activation-1',
                           'sandbox-1', 'team-1', 'main', ?, 'queued', ?, ?, ?, ?)""",
                (self.base_sha, str(self.checkout), str(run_root), now, now),
            )
            connection.execute(
                """INSERT INTO agent_assignments
                   (id, run_id, team_member_id, reasoning, assigned_at)
                   VALUES ('fixture-assignment', 'run-1', 'lead-1',
                           'Explicit fixture lead assignment', ?)""",
                (now,),
            )
            connection.execute(
                """UPDATE repositories SET current_sandbox_version_id='sandbox-1',
                                             current_team_version_id='team-1'
                   WHERE id='repo-1'"""
            )
        self.sandbox = SandboxManager()
        self.lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.checkout,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout

    def test_obsolete_omp_stored_runtime_rejected_at_execution(self) -> None:
        """Stored team member with runtime='omp' must be rejected without fallback."""
        def factory(runtime: str, model: str, timeout: float) -> object:
            self.fail(
                f"obsolete runtime reached factory instead of being rejected: "
                f"{runtime!r}, {model!r}, {timeout!r}"
            )

        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=factory,
            max_actions=10,
        )
        with self.assertRaisesRegex(ValueError, "unsupported stored model runtime"):
            service.execute("run-1")


if __name__ == "__main__":
    unittest.main()
