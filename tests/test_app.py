from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repogents.app import ApplicationActions, Orchestrator, build_runtime
from repogents.database import Database
from repogents.github import GitHubError
from repogents.quiet import TransientQuietCheckError


class FakeOnboarding:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def onboard(self, identity: str, inputs: dict[str, object]) -> str:
        self.calls.append(("onboard", identity, inputs))
        return "repo-2"

    def reonboard(
        self, repository_id: str, inputs: dict[str, object]
    ) -> str:
        self.calls.append(("reonboard", repository_id, inputs))
        return repository_id


class FakeLifecycle:
    def reconcile_nonterminal_runs(self) -> tuple[str, ...]:
        self.calls.append(("reconcile",))
        return ()

    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[tuple[object, ...]] = []

    def poll_repository(self, repository_id: str) -> tuple[str, ...]:
        self.calls.append(("poll_repository", repository_id))
        return ()

    def get_run(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            return dict(connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def transition(self, run_id: str, target: object, *, reason: str | None = None) -> None:
        value = getattr(target, "value", str(target))
        with self.database.transaction() as connection:
            connection.execute("UPDATE runs SET state=?, reason=? WHERE id=?", (value, reason, run_id))


    def cancel(self, run_id: str, reason: str) -> None:
        self.calls.append(("cancel", run_id, reason))


class FakeExecution:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []

    def execute(self, run_id: str) -> str:
        self.calls.append(run_id)
        with self.database.transaction() as connection:
            connection.execute("UPDATE runs SET state='publishing' WHERE id=?", (run_id,))
        return "b" * 40


class FakePublication:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []

    def publish(self, run_id: str) -> object:
        self.calls.append(run_id)
        with self.database.transaction() as connection:
            connection.execute("UPDATE runs SET state='waiting_for_feedback' WHERE id=?", (run_id,))
        return object()


class FakeFeedback:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []

    def resolve_run(self, run_id: str) -> int:
        self.calls.append(run_id)
        with self.database.transaction() as connection:
            connection.execute("UPDATE runs SET state='quiet_period' WHERE id=?", (run_id,))
        return 0


class FakeQuiet:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.acks: list[str] = []

    def check_due(self, run_id: str) -> None:
        self.calls.append(run_id)
        return None

    def acknowledge(self, notification_id: str) -> None:
        self.acks.append(notification_id)

    def list_notifications(self) -> list[dict[str, object]]:
        return [{"id": "notice-1", "read_at": None}]


class FakeScheduler:
    def __init__(self) -> None:
        self.requests = 0

    def request_tick(self) -> None:
        self.requests += 1


class ApplicationTests(unittest.TestCase):
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
                    onboarding_state, inputs_json, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo',
                           'https://github.com/owner/repo', 'main', 'ready', '{}', ?, ?)""",
                (now, now),
            )
            connection.execute(
                "UPDATE repositories SET inputs_json=? WHERE id='repo-1'",
                (
                    json.dumps(
                        {
                            "allowed_services": ["packages.example:443"],
                            "secret_bindings": [
                                {
                                    "name": "PACKAGE_TOKEN",
                                    "reference": "secret://package-token",
                                    "commands": [["python3", "provision.py"]],
                                }
                            ],
                        }
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, ?, '{}', '{}', ?)""",
                (str(self.root / "sandbox"), now),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, '{}', ?)""",
                (now,),
            )
            connection.execute(
                """UPDATE repositories
                   SET current_sandbox_version_id='sandbox-1', current_team_version_id='team-1'
                   WHERE id='repo-1'"""
            )
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own', '["read"]', 'test', 'test', '')"""
            )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 3,
                           'https://github.com/owner/repo/issues/3', 'Fix scrolling', 'Body', '[]', ?)""",
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
                ("a" * 40, str(self.root / "checkout"), str(self.root / "run"), now, now),
            )

    def test_tick_polls_ready_inventory_and_advances_composed_run_path(self) -> None:
        lifecycle = FakeLifecycle(self.db)
        execution = FakeExecution(self.db)
        publication = FakePublication(self.db)
        feedback = FakeFeedback(self.db)
        quiet = FakeQuiet()
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=execution,
            publication=publication,
            feedback=feedback,
            quiet=quiet,
        )
        orchestrator.tick()
        self.assertEqual(
            lifecycle.calls,
            [("reconcile",), ("poll_repository", "repo-1")],
        )
        self.assertEqual(execution.calls, ["run-1"])
        self.assertEqual(publication.calls, ["run-1"])
        self.assertEqual(feedback.calls, ["run-1", "run-1"])
        self.assertEqual(quiet.calls, ["run-1"])

    def test_application_actions_expose_durable_state_and_controller_operations(self) -> None:
        onboarding = FakeOnboarding()
        lifecycle = FakeLifecycle(self.db)
        quiet = FakeQuiet()
        scheduler = FakeScheduler()
        actions = ApplicationActions(
            database=self.db,
            onboarding=onboarding,
            lifecycle=lifecycle,
            quiet=quiet,
            scheduler=scheduler,
        )
        state = actions.state()
        repository = state["repositories"][0]
        run = state["runs"][0]
        self.assertEqual(repository["identity"], "owner/repo")
        self.assertEqual(repository["sandbox_version"], 1)
        self.assertEqual(repository["sandbox_version_id"], "sandbox-1")
        self.assertEqual(repository["team_version_id"], "team-1")
        self.assertEqual(repository["team_version"], 1)
        self.assertEqual(
            repository["display_inputs"],
            {
                "allowed_services": ["packages.example:443"],
                "secret_bindings": [
                    {
                        "name": "PACKAGE_TOKEN",
                        "reference": "secret://package-token",
                        "commands": [["python3", "provision.py"]],
                    }
                ],
            },
        )
        self.assertEqual(run["issue_url"], "https://github.com/owner/repo/issues/3")
        self.assertEqual(state["notifications"], [{"id": "notice-1", "read_at": None}])
        self.assertIsNone(run["last_completed_state"])
        self.assertEqual(run["sandbox_version_id"], "sandbox-1")
        self.assertEqual(run["team_version_id"], "team-1")
        self.assertEqual(run["validation_results"], [])
        self.assertEqual(run["assignments"], [])
        self.assertEqual(actions.add_repository("owner/second", {"allowed_services": []}), "repo-2")
        actions.reonboard("repo-1", {"allowed_services": ["api.github.com:443"]})
        with self.db.connect() as connection:
            stored = json.loads(connection.execute("SELECT inputs_json FROM repositories WHERE id='repo-1'").fetchone()[0])
        self.assertEqual(stored["allowed_services"], ["packages.example:443"])
        actions.cancel("run-1")
        actions.acknowledge("notice-1")
        actions.poll()
        self.assertEqual(
            onboarding.calls[-1],
            ("reonboard", "repo-1", {"allowed_services": ["api.github.com:443"]}),
        )
        self.assertIn(("cancel", "run-1", "canceled by user"), lifecycle.calls)
        self.assertEqual(quiet.acks, ["notice-1"])
        self.assertEqual(scheduler.requests, 3)

    def test_processing_feedback_in_publishing_routes_through_feedback_recovery(self) -> None:
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='publishing' WHERE id='run-1'"
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-1', 'run-1', 'PR1', 7, 'pr-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?, 'open', ?, ?)""",
                ("a" * 40, "b" * 40, "b" * 40, now, now),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, decision_json, observed_at)
                   VALUES ('feedback-1', 'pr-1', 'comment', '901', 'v1',
                           'reviewer', 'Please revise', 'processing',
                           '{"action":"revise","reason":"valid","response":"done"}', ?)""",
                (now,),
            )
        lifecycle = FakeLifecycle(self.db)
        publication = FakePublication(self.db)
        feedback = FakeFeedback(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=FakeExecution(self.db),
            publication=publication,
            feedback=feedback,
            quiet=FakeQuiet(),
        )

        orchestrator._advance("run-1")

        self.assertEqual(publication.calls, [])
        self.assertGreaterEqual(feedback.calls.count("run-1"), 1)

    def test_transient_quiet_check_preserves_active_run_for_next_tick(self) -> None:
        class FailingQuiet(FakeQuiet):
            def check_due(self, run_id: str) -> None:
                self.calls.append(run_id)
                raise TransientQuietCheckError(run_id, "GitHub status poll")

        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='quiet_period', reason=NULL WHERE id='run-1'"
            )
        lifecycle = FakeLifecycle(self.db)
        quiet = FailingQuiet()
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=FakeExecution(self.db),
            publication=FakePublication(self.db),
            feedback=FakeFeedback(self.db),
            quiet=quiet,
        )

        orchestrator._advance("run-1")

        self.assertEqual(lifecycle.get_run("run-1")["state"], "quiet_period")
        self.assertEqual(quiet.calls, ["run-1"])
        self.assertIn("GitHub status poll", orchestrator.last_errors[0])

    def test_transient_feedback_poll_preserves_quiet_run_for_next_tick(self) -> None:
        class FailingFeedback(FakeFeedback):
            def resolve_run(self, run_id: str) -> int:
                self.calls.append(run_id)
                raise GitHubError("GitHub unavailable")

        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='quiet_period', reason=NULL WHERE id='run-1'"
            )
        lifecycle = FakeLifecycle(self.db)
        feedback = FailingFeedback(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=FakeExecution(self.db),
            publication=FakePublication(self.db),
            feedback=feedback,
            quiet=FakeQuiet(),
        )

        orchestrator._advance("run-1")

        self.assertEqual(lifecycle.get_run("run-1")["state"], "quiet_period")
        self.assertEqual(feedback.calls, ["run-1"])
        self.assertIn("feedback poll", orchestrator.last_errors[0])

    def test_transient_orchestration_failure_preserves_run_for_next_tick(self) -> None:
        class FailingExecution(FakeExecution):
            def execute(self, run_id: str) -> str:
                self.calls.append(run_id)
                raise TimeoutError("temporary controller boundary failure")

        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='implementing', reason=NULL WHERE id='run-1'"
            )
        lifecycle = FakeLifecycle(self.db)
        execution = FailingExecution(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=execution,
            publication=FakePublication(self.db),
            feedback=FakeFeedback(self.db),
            quiet=FakeQuiet(),
        )

        orchestrator._advance("run-1")

        run = lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "implementing")
        self.assertIsNone(run["reason"])
        self.assertEqual(execution.calls, ["run-1"])
        self.assertIn("temporary controller boundary failure", orchestrator.last_errors[0])

    def test_build_runtime_rejects_missing_model_without_ambient_discovery(
        self,
    ) -> None:
        ambient_configuration = {
            "HOME": str(self.root / "poisoned-home"),
            "OMP_MODEL": "ambient/omp-model",
            "PI_MODEL": "ambient/pi-model",
            "MSWEA_GLOBAL_CONFIG_DIR": str(self.root / "ambient-mini-swe"),
        }
        with (
            patch.dict(os.environ, ambient_configuration, clear=True),
            patch("subprocess.run") as ambient_process,
        ):
            with self.assertRaisesRegex(ValueError, "explicit.*model"):
                build_runtime(self.root / "missing-model", model=None)

        ambient_process.assert_not_called()

    def test_build_runtime_passes_explicit_mini_swe_configuration_and_recovers(
        self,
    ) -> None:
        runtime_root = self.root / "runtime"
        runtime_root.mkdir()
        database = Database(runtime_root / "repogents.sqlite3")
        database.initialize()
        now = "2026-01-01T00:00:00Z"
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, inputs_json, created_at, updated_at)
                   VALUES ('interrupted', 'R2', 'owner', 'interrupted',
                           'https://github.com/owner/interrupted', 'main',
                           'inspecting', '{}', ?, ?)""",
                (now, now),
            )
        with (
            patch(
                "repogents.app.MiniSweRepositoryEvidenceAnalyzer"
            ) as onboarding_analyzer_type,
            patch("repogents.app.MiniSweModelRuntime") as execution_runtime_type,
            patch("repogents.app.MiniSweScopeReviewer") as scope_reviewer_type,
            patch("repogents.app.MiniSweFeedbackEvaluator") as feedback_evaluator_type,
            patch.dict(
                os.environ,
                {"REPOGENTS_SECRET_PACKAGE_TOKEN": "canary-value"},  # pragma: allowlist secret
                clear=False,
            ),
        ):
            runtime = build_runtime(
                runtime_root,
                github_token="configured-token",
                model="openai/gpt-stored",
                model_base_url="https://models.example.test/v1",
            )
            execution_boundary = runtime.execution.runtime_factory(
                "mini-swe-agent",
                "openai/gpt-stored",
                777,
            )

        self.assertIs(
            execution_boundary,
            execution_runtime_type.return_value,
        )
        onboarding_arguments = onboarding_analyzer_type.call_args.kwargs
        execution_arguments = execution_runtime_type.call_args.kwargs
        scope_arguments = scope_reviewer_type.call_args.kwargs
        feedback_arguments = feedback_evaluator_type.call_args.kwargs
        self.assertEqual(
            onboarding_arguments["model"],
            "openai/gpt-stored",
        )
        self.assertEqual(
            execution_arguments["model"],
            "openai/gpt-stored",
        )
        self.assertEqual(execution_arguments["timeout"], 777)
        for arguments in (
            onboarding_arguments,
            execution_arguments,
            scope_arguments,
            feedback_arguments,
        ):
            self.assertEqual(
                arguments["base_url"],
                "https://models.example.test/v1",
            )
        resolved_root = runtime_root.resolve()
        expected_state_roots = {
            onboarding_analyzer_type: resolved_root
            / "model-state"
            / "onboarding",
            scope_reviewer_type: resolved_root
            / "model-state"
            / "scope-review",
            feedback_evaluator_type: resolved_root
            / "model-state"
            / "feedback",
        }
        observed_state_roots: set[Path] = set()
        for boundary_type, expected_state_root in expected_state_roots.items():
            state_root = Path(
                boundary_type.call_args.kwargs["state_root"]
            ).resolve()
            self.assertEqual(state_root, expected_state_root)
            self.assertTrue(state_root.is_relative_to(resolved_root))
            observed_state_roots.add(state_root)
        self.assertEqual(
            len(observed_state_roots),
            len(expected_state_roots),
        )
        self.assertEqual(
            runtime.onboarding.team_formulator.runtime,
            "mini-swe-agent",
        )
        self.assertEqual(
            runtime.onboarding.team_formulator.model,
            "openai/gpt-stored",
        )
        self.assertEqual(runtime.onboarding.sources.token, "configured-token")
        self.assertEqual(runtime.lifecycle.checkouts.token, "configured-token")
        self.assertEqual(runtime.publication.gateway.token, "configured-token")
        self.assertEqual(
            runtime.execution.secret_resolver("secret://package-token"),
            "canary-value",
        )
        with database.connect() as connection:
            recovered = connection.execute(
                "SELECT onboarding_state, blocking_reason FROM repositories WHERE id='interrupted'"
            ).fetchone()
        self.assertEqual(recovered["onboarding_state"], "blocked")
        self.assertIn("interrupted during inspecting", recovered["blocking_reason"])


if __name__ == "__main__":
    unittest.main()
