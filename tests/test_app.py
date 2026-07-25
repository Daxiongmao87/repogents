from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from repogents.app import ApplicationActions, Orchestrator, Scheduler, build_runtime
from repogents.database import Database
from repogents.github import GitHubError
from repogents.lifecycle import RunState


class FakeOnboarding:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def onboard(self, identity: str, inputs: dict[str, object]) -> str:
        self.calls.append(("onboard", identity, inputs))
        return "repo-2"

    def reonboard(self, repository_id: str, inputs: dict[str, object]) -> str:
        self.calls.append(("reonboard", repository_id, inputs))
        return repository_id


class FakeLifecycle:
    def reconcile_nonterminal_runs(self) -> tuple[str, ...]:
        self.calls.append(("reconcile",))
        return ()

    def reconcile_recoverable_blocked_runs(self) -> tuple[str, ...]:
        self.calls.append(("recover_blocked",))
        with self.database.transaction() as connection:
            for run_id in self.recoverable_blocked:
                connection.execute(
                    "UPDATE runs SET state='publishing', reason=NULL WHERE id=?",
                    (run_id,),
                )
        return self.recoverable_blocked

    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[tuple[object, ...]] = []
        self.recoverable_blocked: tuple[str, ...] = ()

    def poll_repository(self, repository_id: str) -> tuple[str, ...]:
        self.calls.append(("poll_repository", repository_id))
        return ()

    def poll_issue_revision(self, run_id: str) -> bool:
        self.calls.append(("poll_run_issue", run_id))
        return False

    def get_run(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            return dict(
                connection.execute(
                    "SELECT * FROM runs WHERE id=?", (run_id,)
                ).fetchone()
            )

    def transition(
        self, run_id: str, target: object, *, reason: str | None = None
    ) -> None:
        value = getattr(target, "value", str(target))
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state=?, reason=? WHERE id=?", (value, reason, run_id)
            )

    def cancel(self, run_id: str, reason: str) -> None:
        self.calls.append(("cancel", run_id, reason))

    def set_repository_paused(
        self, repository_id: str, paused: bool
    ) -> tuple[str, ...]:
        self.calls.append(("set_repository_paused", repository_id, paused))
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET enabled=? WHERE id=?",
                (int(not paused), repository_id),
            )
            rows = connection.execute(
                """SELECT id FROM runs
                   WHERE repository_id=?
                     AND state NOT IN ('canceled', 'closed')
                   ORDER BY id""",
                (repository_id,),
            ).fetchall()
        return tuple(str(row["id"]) for row in rows)


class FakeExecution:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []

    def execute(self, run_id: str) -> str:
        self.calls.append(run_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='publishing' WHERE id=?", (run_id,)
            )
        return "b" * 40


class BlockingExecution:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, run_id: str) -> str:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test did not release blocked execution")
        return "b" * 40


class ControlledExecution:
    def __init__(
        self,
        database: Database,
        blocked_runs: set[str],
        *,
        held_after_completion: set[str] | None = None,
    ) -> None:
        self.database = database
        self.blocked_runs = blocked_runs
        self.held_after_completion = set(held_after_completion or ())
        self.calls: list[str] = []
        self._completed: set[str] = set()
        self._condition = threading.Condition()
        self._releases = {run_id: threading.Event() for run_id in blocked_runs}
        self._completion_releases = {
            run_id: threading.Event() for run_id in self.held_after_completion
        }

    def execute(self, run_id: str) -> str:
        with self._condition:
            self.calls.append(run_id)
            self._condition.notify_all()
        if run_id in self.blocked_runs:
            if not self._releases[run_id].wait(timeout=5):
                raise RuntimeError(f"test did not release {run_id}")
        else:
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE runs SET state='blocked' WHERE id=?",
                    (run_id,),
                )
        with self._condition:
            self._completed.add(run_id)
            self._condition.notify_all()
        if run_id in self.held_after_completion:
            if not self._completion_releases[run_id].wait(timeout=5):
                raise RuntimeError(f"test did not finish {run_id}")
        return "b" * 40

    def wait_for_call(self, run_id: str, timeout: float = 1) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: run_id in self.calls,
                timeout=timeout,
            )

    def wait_for_completion(self, run_id: str, timeout: float = 1) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: run_id in self._completed,
                timeout=timeout,
            )

    def release(self, run_id: str) -> None:
        self._releases[run_id].set()

    def release_completion(self, run_id: str) -> None:
        self._completion_releases[run_id].set()


class SignalingLifecycle(FakeLifecycle):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self._poll_condition = threading.Condition()
        self.poll_count = 0
        self.issue_poll_count = 0

    def poll_repository(self, repository_id: str) -> tuple[str, ...]:
        result = super().poll_repository(repository_id)
        with self._poll_condition:
            self.poll_count += 1
            self._poll_condition.notify_all()
        return result

    def poll_issue_revision(self, run_id: str) -> bool:
        result = super().poll_issue_revision(run_id)
        with self._poll_condition:
            self.issue_poll_count += 1
            self._poll_condition.notify_all()
        return result

    def wait_for_issue_poll_count(self, target: int, timeout: float) -> bool:
        with self._poll_condition:
            return self._poll_condition.wait_for(
                lambda: self.issue_poll_count >= target,
                timeout=timeout,
            )

    def wait_for_poll_count(self, target: int, timeout: float) -> bool:
        with self._poll_condition:
            return self._poll_condition.wait_for(
                lambda: self.poll_count >= target,
                timeout=timeout,
            )


class SignalingScheduler(Scheduler):
    def __init__(self, orchestrator: Orchestrator, *, interval: float = 10.0) -> None:
        super().__init__(orchestrator, interval=interval)
        self._batch_condition = threading.Condition()
        self.batch_count = 0

    def _start_repository_threads(self, repository_ids: tuple[str, ...]) -> None:
        super()._start_repository_threads(repository_ids)
        with self._batch_condition:
            self.batch_count += 1
            self._batch_condition.notify_all()

    def wait_for_batch_count(self, target: int, timeout: float = 1) -> bool:
        with self._batch_condition:
            return self._batch_condition.wait_for(
                lambda: self.batch_count >= target,
                timeout=timeout,
            )


class FakePublication:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []

    def publish(self, run_id: str) -> object:
        self.calls.append(run_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='waiting_for_feedback' WHERE id=?", (run_id,)
            )
        return object()


class FakeFeedback:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []
        self.poll_calls: list[str] = []

    def poll_run(self, run_id: str) -> int:
        self.poll_calls.append(run_id)
        return 0

    def resolve_run(self, run_id: str) -> int:
        self.calls.append(run_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='waiting_for_feedback' WHERE id=?", (run_id,)
            )
        return 0


class SignalingFeedback(FakeFeedback):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.entered = threading.Event()
        self._poll_condition = threading.Condition()

    def poll_run(self, run_id: str) -> int:
        result = super().poll_run(run_id)
        with self._poll_condition:
            self._poll_condition.notify_all()
        return result

    def wait_for_poll_count(self, run_id: str, target: int, timeout: float = 1) -> bool:
        with self._poll_condition:
            return self._poll_condition.wait_for(
                lambda: self.poll_calls.count(run_id) >= target,
                timeout=timeout,
            )

    def resolve_run(self, run_id: str) -> int:
        self.entered.set()
        return super().resolve_run(run_id)


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
            connection.execute("""UPDATE repositories
                   SET current_sandbox_version_id='sandbox-1', current_team_version_id='team-1'
                   WHERE id='repo-1'""")
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead',
                           'delivery coordinator', 'Own the result', '["read"]',
                           'test', 'test', 'You are the repository lead.')""")
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
                (
                    "a" * 40,
                    str(self.root / "checkout"),
                    str(self.root / "run"),
                    now,
                    now,
                ),
            )

    def seed_second_run(self) -> None:
        now = "2026-01-01T00:01:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-2', 'repo-1', 'I2', 4,
                           'https://github.com/owner/repo/issues/4',
                           'Fix priority', 'Body', '[]', ?)""",
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
                           'sandbox-1', 'team-1', 'main', ?, 'queued', ?, ?, ?,
                           ?)""",
                (
                    "c" * 40,
                    str(self.root / "checkout-2"),
                    str(self.root / "run-2"),
                    now,
                    now,
                ),
            )

    def seed_second_repository_run(self, *, state: str = "queued") -> None:
        now = "2026-01-01T00:02:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, inputs_json, created_at, updated_at)
                   VALUES ('repo-2', 'R2', 'owner', 'second',
                           'https://github.com/owner/second', 'main',
                           'ready', '{}', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json,
                    evidence_json, created_at)
                   VALUES ('sandbox-2', 'repo-2', 1, ?, '{}', '{}', ?)""",
                (str(self.root / "sandbox-2"), now),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-2', 'repo-2', 1, '{}', ?)""",
                (now,),
            )
            connection.execute("""UPDATE repositories
                   SET current_sandbox_version_id='sandbox-2',
                       current_team_version_id='team-2'
                   WHERE id='repo-2'""")
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-3', 'repo-2', 'I3', 5,
                           'https://github.com/owner/second/issues/5',
                           'Resolve feedback', 'Body', '[]', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-3', 'repo-2', 'issue-3', 'event-3', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, checkout_path, run_path, created_at,
                    updated_at)
                   VALUES ('run-3', 'repo-2', 'issue-3', 'activation-3',
                           'sandbox-2', 'team-2', 'main', ?, ?, ?, ?, ?, ?)""",
                (
                    "d" * 40,
                    state,
                    str(self.root / "checkout-3"),
                    str(self.root / "run-3"),
                    now,
                    now,
                ),
            )

    def test_state_exposes_validation_baseline_and_delta_verdict(self) -> None:
        findings = [
            {
                "tool": "eslint",
                "rule": "no-explicit-any",
                "path": "client/src/App.tsx",
                "context": "",
                "message": "Unexpected any.",
            }
        ]
        comparison = {
            "baseline_count": 1,
            "candidate_count": 1,
            "new_count": 0,
            "resolved_count": 0,
            "unchanged_count": 1,
        }
        with self.db.transaction() as connection:
            connection.execute("""INSERT INTO validation_commands
                   (id, sandbox_version_id, position, command_json,
                    source, required)
                   VALUES ('command-1', 'sandbox-1', 0,
                           '["npm","run","lint"]', 'repository inference', 1)""")
            connection.execute(
                """INSERT INTO validation_baselines
                   (id, run_id, validation_command_id, command_json,
                    base_sha, mode, started_at, completed_at, exit_status,
                    log_path, findings_json)
                   VALUES ('baseline-1', 'run-1', 'command-1',
                           '["npm","run","lint"]', ?, 'delta',
                           '2026-01-01T00:00:00Z',
                           '2026-01-01T00:00:01Z', 1,
                           '/logs/base.json', ?)""",
                ("a" * 40, json.dumps(findings)),
            )
            connection.execute(
                """INSERT INTO validation_results
                   (id, run_id, validation_command_id, commit_sha,
                    command_json, started_at, completed_at, exit_status,
                    log_path, verdict, findings_json, comparison_json)
                   VALUES ('result-1', 'run-1', 'command-1', ?,
                           '["npm","run","lint"]',
                           '2026-01-01T00:01:00Z',
                           '2026-01-01T00:01:01Z', 1,
                           '/logs/candidate.json', 'pass', ?, ?)""",
                ("b" * 40, json.dumps(findings), json.dumps(comparison)),
            )
        actions = ApplicationActions(
            database=self.db,
            onboarding=FakeOnboarding(),
            lifecycle=FakeLifecycle(self.db),
            scheduler=FakeScheduler(),
        )

        run = actions.state()["runs"][0]

        self.assertEqual(run["validation_baselines"][0]["mode"], "delta")
        self.assertEqual(
            run["validation_baselines"][0]["findings"],
            findings,
        )
        self.assertEqual(run["validation_results"][0]["verdict"], "pass")
        self.assertEqual(
            run["validation_results"][0]["comparison"],
            comparison,
        )

    def test_tick_polls_ready_inventory_and_advances_composed_run_path(self) -> None:
        lifecycle = FakeLifecycle(self.db)
        execution = FakeExecution(self.db)
        publication = FakePublication(self.db)
        feedback = FakeFeedback(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=execution,
            publication=publication,
            feedback=feedback,
        )
        orchestrator.tick()
        self.assertEqual(
            lifecycle.calls,
            [
                ("poll_run_issue", "run-1"),
                ("reconcile",),
                ("recover_blocked",),
                ("poll_repository", "repo-1"),
            ],
        )
        self.assertEqual(execution.calls, ["run-1"])
        self.assertEqual(publication.calls, ["run-1"])
        self.assertEqual(feedback.calls, ["run-1"])

    def test_scheduler_keeps_polling_while_issue_execution_is_active(self) -> None:
        lifecycle = SignalingLifecycle(self.db)
        execution = BlockingExecution()
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=execution,
            publication=FakePublication(self.db),
            feedback=FakeFeedback(self.db),
        )
        scheduler = Scheduler(orchestrator, interval=60)
        scheduler.start()

        def stop_scheduler() -> None:
            execution.release.set()
            scheduler.stop()

        self.addCleanup(stop_scheduler)
        self.assertTrue(execution.entered.wait(timeout=1))
        observed_polls = lifecycle.poll_count
        scheduler.request_tick()

        self.assertTrue(
            lifecycle.wait_for_poll_count(observed_polls + 1, timeout=1),
            "ready-label polling stalled behind active issue execution",
        )

    def test_scheduler_reconciles_issue_edits_while_execution_is_active(
        self,
    ) -> None:
        lifecycle = SignalingLifecycle(self.db)
        execution = BlockingExecution()
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=execution,
            publication=FakePublication(self.db),
            feedback=FakeFeedback(self.db),
        )
        scheduler = Scheduler(orchestrator, interval=60)
        scheduler.start()

        def stop_scheduler() -> None:
            execution.release.set()
            scheduler.stop()

        self.addCleanup(stop_scheduler)
        self.assertTrue(execution.entered.wait(timeout=1))
        observed_polls = lifecycle.issue_poll_count
        scheduler.request_tick()

        self.assertTrue(
            lifecycle.wait_for_issue_poll_count(observed_polls + 1, timeout=1),
            "issue revision polling stalled behind active issue execution",
        )

    def test_scheduler_detects_feedback_while_same_repository_lane_is_busy(
        self,
    ) -> None:
        self.seed_second_run()
        now = "2026-01-01T00:02:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='waiting_for_feedback',
                       last_completed_state='publishing',
                       validated_sha=?
                   WHERE id='run-2'""",
                ("e" * 40,),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-2', 'run-2', 'PR2', 9,
                           'https://github.com/owner/repo/pull/9',
                           'agent/issue-4-run-2', 'main', ?, ?, ?,
                           'open', ?, ?)""",
                ("c" * 40, "e" * 40, "e" * 40, now, now),
            )
        execution = ControlledExecution(self.db, {"run-1"})
        feedback = SignalingFeedback(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=SignalingLifecycle(self.db),
            execution=execution,
            publication=FakePublication(self.db),
            feedback=feedback,
        )
        scheduler = Scheduler(orchestrator, interval=60)
        scheduler.start()

        def stop_scheduler() -> None:
            execution.release("run-1")
            scheduler.stop()

        self.addCleanup(stop_scheduler)
        self.assertTrue(execution.wait_for_call("run-1"))
        observed_polls = feedback.poll_calls.count("run-2")

        scheduler.request_tick()

        self.assertTrue(
            feedback.wait_for_poll_count("run-2", observed_polls + 1),
            "feedback detection stalled behind same-repository agent work",
        )
        self.assertNotIn(
            "run-2",
            feedback.calls,
            "same-repository agentic feedback work ran concurrently",
        )

    def test_feedback_poll_reconciles_persisted_closed_pull_after_restart(
        self,
    ) -> None:
        now = "2026-01-01T00:02:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='waiting_for_feedback', validated_sha=?
                   WHERE id='run-1'""",
                ("d" * 40,),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-1', 'run-1', 'PR1', 8, 'pr-1-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?,
                           'closed', ?, ?)""",
                ("a" * 40, "d" * 40, "d" * 40, now, now),
            )
        feedback = FakeFeedback(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=SignalingLifecycle(self.db),
            execution=FakeExecution(self.db),
            publication=FakePublication(self.db),
            feedback=feedback,
        )

        orchestrator.poll_feedback()

        self.assertEqual(feedback.poll_calls, ["run-1"])

    def test_feedback_poll_failure_does_not_block_other_open_pulls(self) -> None:
        self.seed_second_run()
        now = "2026-01-01T00:02:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='waiting_for_feedback', validated_sha=?
                   WHERE id='run-1'""",
                ("d" * 40,),
            )
            connection.execute(
                """UPDATE runs
                   SET state='waiting_for_feedback', validated_sha=?
                   WHERE id='run-2'""",
                ("e" * 40,),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-1', 'run-1', 'PR1', 8, 'pr-1-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?,
                           'open', ?, ?)""",
                ("a" * 40, "d" * 40, "d" * 40, now, now),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-2', 'run-2', 'PR2', 9, 'pr-2-url',
                           'agent/issue-4-run-2', 'main', ?, ?, ?,
                           'open', ?, ?)""",
                ("c" * 40, "e" * 40, "e" * 40, now, now),
            )

        class SelectiveFailingFeedback(FakeFeedback):
            def poll_run(self, run_id: str) -> int:
                result = super().poll_run(run_id)
                if run_id == "run-1":
                    raise GitHubError(("GitHub unavailable " * 100) + "\ntrace")
                return result

        feedback = SelectiveFailingFeedback(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=SignalingLifecycle(self.db),
            execution=FakeExecution(self.db),
            publication=FakePublication(self.db),
            feedback=feedback,
        )

        orchestrator.poll_feedback()

        self.assertEqual(feedback.poll_calls, ["run-1", "run-2"])
        self.assertEqual(len(orchestrator.last_errors), 1)
        self.assertIn("feedback poll run-1", orchestrator.last_errors[0])
        self.assertLessEqual(len(orchestrator.last_errors[0]), 450)

    def test_issue_poll_failure_does_not_block_other_active_runs(self) -> None:
        self.seed_second_run()

        class SelectiveFailingLifecycle(FakeLifecycle):
            def poll_issue_revision(self, run_id: str) -> bool:
                result = super().poll_issue_revision(run_id)
                if run_id == "run-1":
                    raise GitHubError(("GitHub unavailable " * 100) + "\ntrace")
                return result

        lifecycle = SelectiveFailingLifecycle(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=FakeExecution(self.db),
            publication=FakePublication(self.db),
            feedback=FakeFeedback(self.db),
        )

        orchestrator.poll_issue_revisions()

        self.assertEqual(
            lifecycle.calls,
            [
                ("poll_run_issue", "run-1"),
                ("poll_run_issue", "run-2"),
            ],
        )
        self.assertEqual(len(orchestrator.last_errors), 1)
        self.assertIn("issue poll run-1", orchestrator.last_errors[0])
        self.assertLessEqual(len(orchestrator.last_errors[0]), 450)

    def test_scheduler_runs_repositories_concurrently_and_each_queue_serially(
        self,
    ) -> None:
        self.seed_second_run()
        self.seed_second_repository_run(state="waiting_for_feedback")
        lifecycle = SignalingLifecycle(self.db)
        execution = ControlledExecution(self.db, {"run-1", "run-2"})
        feedback = SignalingFeedback(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=execution,
            publication=FakePublication(self.db),
            feedback=feedback,
        )
        scheduler = Scheduler(orchestrator, interval=60)
        scheduler.start()

        def stop_scheduler() -> None:
            execution.release("run-1")
            execution.release("run-2")
            scheduler.stop()

        self.addCleanup(stop_scheduler)
        self.assertTrue(execution.wait_for_call("run-1"))
        self.assertTrue(
            feedback.entered.wait(timeout=1),
            "repository feedback stalled behind another repository's issue",
        )
        self.assertNotIn("run-2", execution.calls)

        execution.release("run-1")

        self.assertTrue(
            execution.wait_for_call("run-2"),
            "second issue did not advance after its repository predecessor",
        )

    def test_scheduler_reschedules_idle_repository_while_other_lane_is_blocked(
        self,
    ) -> None:
        self.seed_second_repository_run()
        lifecycle = SignalingLifecycle(self.db)
        execution = ControlledExecution(
            self.db,
            {"run-3"},
            held_after_completion={"run-1"},
        )
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=execution,
            publication=FakePublication(self.db),
            feedback=FakeFeedback(self.db),
        )
        scheduler = SignalingScheduler(orchestrator, interval=60)
        scheduler.start()

        def stop_scheduler() -> None:
            execution.release_completion("run-1")
            execution.release("run-3")
            scheduler.stop()

        self.addCleanup(stop_scheduler)
        self.assertTrue(execution.wait_for_completion("run-1"))
        self.assertTrue(execution.wait_for_call("run-3"))
        self.seed_second_run()
        prior_batches = scheduler.batch_count

        scheduler.request_tick()

        self.assertTrue(scheduler.wait_for_batch_count(prior_batches + 1))
        execution.release_completion("run-1")
        self.assertTrue(
            execution.wait_for_call("run-2"),
            "idle repository waited for another repository's active lane",
        )

    def test_scheduler_keeps_other_repository_running_when_one_is_focused(
        self,
    ) -> None:
        self.seed_second_repository_run()
        with self.db.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET force_requested_at='2026-01-01T00:03:00Z'
                   WHERE id='run-1'""")
        execution = ControlledExecution(self.db, {"run-1", "run-3"})
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=SignalingLifecycle(self.db),
            execution=execution,
            publication=FakePublication(self.db),
            feedback=FakeFeedback(self.db),
        )
        scheduler = Scheduler(orchestrator, interval=60)
        scheduler.start()

        def stop_scheduler() -> None:
            execution.release("run-1")
            execution.release("run-3")
            scheduler.stop()

        self.addCleanup(stop_scheduler)
        self.assertTrue(execution.wait_for_call("run-1"))
        self.assertTrue(
            execution.wait_for_call("run-3"),
            "a focused run blocked another repository's lane",
        )

    def test_run_force_is_scoped_to_repository(self) -> None:
        self.seed_second_run()
        self.seed_second_repository_run()
        scheduler = FakeScheduler()
        actions = ApplicationActions(
            database=self.db,
            onboarding=FakeOnboarding(),
            lifecycle=FakeLifecycle(self.db),
            scheduler=scheduler,
        )

        actions.set_run_forced("run-1", True)
        actions.set_run_forced("run-3", True)

        forced = {
            str(run["id"]): bool(run["forced"]) for run in actions.state()["runs"]
        }
        self.assertEqual(
            forced,
            {"run-1": True, "run-2": False, "run-3": True},
        )

        actions.set_run_forced("run-2", True)

        transferred = {
            str(run["id"]): bool(run["forced"]) for run in actions.state()["runs"]
        }
        self.assertEqual(
            transferred,
            {"run-1": False, "run-2": True, "run-3": True},
        )

    def test_tick_automatically_advances_a_recoverable_blocked_run(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='blocked', reason='legacy visual block'"
            )
        lifecycle = FakeLifecycle(self.db)
        lifecycle.recoverable_blocked = ("run-1",)
        publication = FakePublication(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=FakeExecution(self.db),
            publication=publication,
            feedback=FakeFeedback(self.db),
        )

        orchestrator.tick()

        self.assertIn(("recover_blocked",), lifecycle.calls)
        self.assertEqual(publication.calls, ["run-1"])

    def test_tick_pauses_one_repository_while_another_advances(self) -> None:
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute("UPDATE repositories SET enabled=0 WHERE id='repo-1'")
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, inputs_json, created_at, updated_at)
                   VALUES ('repo-2', 'R2', 'owner', 'other',
                           'https://github.com/owner/other', 'main', 'ready',
                           '{}', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-2', 'repo-2', 'I2', 4,
                           'https://github.com/owner/other/issues/4',
                           'Other issue', 'Body', '[]', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-2', 'repo-2', 'issue-2', 'event-2', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, checkout_path, run_path, created_at,
                    updated_at)
                   VALUES ('run-2', 'repo-2', 'issue-2', 'activation-2',
                           'sandbox-1', 'team-1', 'main', ?, 'queued', ?, ?, ?,
                           ?)""",
                (
                    "c" * 40,
                    str(self.root / "checkout-2"),
                    str(self.root / "run-2"),
                    now,
                    now,
                ),
            )
        lifecycle = FakeLifecycle(self.db)
        execution = FakeExecution(self.db)
        publication = FakePublication(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=execution,
            publication=publication,
            feedback=FakeFeedback(self.db),
        )

        orchestrator.tick()

        self.assertIn(("poll_repository", "repo-2"), lifecycle.calls)
        self.assertNotIn(("poll_repository", "repo-1"), lifecycle.calls)
        self.assertEqual(execution.calls, ["run-2"])
        self.assertEqual(publication.calls, ["run-2"])
        self.assertEqual(
            FakeLifecycle(self.db).get_run("run-1")["state"],
            "queued",
        )

    def test_repository_controls_archive_only_inactive_inventory(self) -> None:
        scheduler = FakeScheduler()
        lifecycle = FakeLifecycle(self.db)
        actions = ApplicationActions(
            database=self.db,
            onboarding=FakeOnboarding(),
            lifecycle=lifecycle,
            scheduler=scheduler,
        )

        actions.set_repository_enabled("repo-1", False)
        with self.db.connect() as connection:
            enabled = connection.execute(
                "SELECT enabled FROM repositories WHERE id='repo-1'"
            ).fetchone()[0]
        self.assertEqual(enabled, 0)
        self.assertIn(("set_repository_paused", "repo-1", True), lifecycle.calls)

        restarted_lifecycle = FakeLifecycle(self.db)
        actions = ApplicationActions(
            database=self.db,
            onboarding=FakeOnboarding(),
            lifecycle=restarted_lifecycle,
            scheduler=scheduler,
        )
        actions.set_repository_enabled("repo-1", True)
        self.assertIn(
            ("set_repository_paused", "repo-1", False),
            restarted_lifecycle.calls,
        )
        with self.assertRaisesRegex(RuntimeError, "active run"):
            actions.remove_repository("repo-1")

        with self.db.transaction() as connection:
            connection.execute("UPDATE runs SET state='closed' WHERE id='run-1'")
        actions.remove_repository("repo-1")

        with self.db.connect() as connection:
            repository = connection.execute(
                "SELECT enabled, removed_at FROM repositories WHERE id='repo-1'"
            ).fetchone()
            run_count = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE repository_id='repo-1'"
            ).fetchone()[0]
        self.assertEqual(repository["enabled"], 0)
        self.assertIsNotNone(repository["removed_at"])
        self.assertEqual(run_count, 1)
        self.assertEqual(actions.state()["repositories"], [])
        self.assertEqual(scheduler.requests, 3)

    def test_state_bounds_run_reason_while_run_log_retains_detail(self) -> None:
        first_line = "x" * 450
        full_reason = first_line + "\n" + ("diagnostic detail " * 1_000)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET reason=? WHERE id='run-1'",
                (full_reason,),
            )
        actions = ApplicationActions(
            database=self.db,
            onboarding=FakeOnboarding(),
            lifecycle=FakeLifecycle(self.db),
            scheduler=FakeScheduler(),
        )

        run = actions.state()["runs"][0]

        self.assertEqual(run["reason"], ("x" * 400) + "…")
        self.assertIs(run["reason_truncated"], True)
        log_messages = "\n".join(
            str(entry["message"]) for entry in actions.run_log("run-1")["entries"]
        )
        self.assertIn(full_reason, log_messages)

        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET reason='short failure' WHERE id='run-1'"
            )
        short_run = actions.state()["runs"][0]
        self.assertEqual(short_run["reason"], "short failure")
        self.assertIs(short_run["reason_truncated"], False)

    def test_state_exposes_activity_team_and_bounded_live_log(self) -> None:
        agent_state = self.root / "run" / "agent-state"
        agent_state.mkdir(parents=True)
        (agent_state / "action-history.json").write_text(
            json.dumps(
                [
                    "Lead note: inspect repository behavior",
                    'Action {"action":"read","path":"src/app.py"}\\n'
                    'Result {"content":"bounded"}',
                ]
            ),
            encoding="utf-8",
        )
        with self.db.transaction() as connection:
            for index in range(205):
                minute, second = divmod(index, 60)
                connection.execute(
                    """INSERT INTO run_transitions
                       (run_id, from_state, to_state, reason, occurred_at)
                       VALUES ('run-1', 'queued', 'implementing', ?, ?)""",
                    (
                        f"progress {index}",
                        f"2026-01-01T00:{minute:02d}:{second:02d}Z",
                    ),
                )
        actions = ApplicationActions(
            database=self.db,
            onboarding=FakeOnboarding(),
            lifecycle=FakeLifecycle(self.db),
            scheduler=FakeScheduler(),
        )

        state = actions.state()
        repository = state["repositories"][0]

        self.assertIs(repository["enabled"], True)
        self.assertIs(repository["active"], True)
        self.assertEqual(repository["active_run_count"], 1)
        self.assertEqual(repository["latest_run_state"], "queued")
        self.assertEqual(repository["latest_activity_at"], "2026-01-01T00:03:24Z")
        self.assertEqual(state["runs"][0]["repository_id"], "repo-1")
        self.assertEqual(repository["team"]["id"], "team-1")
        self.assertEqual(repository["team"]["version"], 1)
        member = repository["team"]["members"][0]
        self.assertEqual(member["stable_key"], "lead")
        self.assertEqual(member["role"], "delivery coordinator")
        self.assertEqual(member["execution_class"], "lead")
        self.assertEqual(member["responsibilities"], "Own the result")
        self.assertEqual(member["runtime"], "test")
        self.assertEqual(member["model"], "test")
        self.assertEqual(member["instructions"], "You are the repository lead.")

        log = actions.repository_log("repo-1")
        self.assertEqual(log["repository_id"], "repo-1")
        self.assertEqual(log["run_id"], "run-1")
        self.assertIs(log["active"], True)
        self.assertLessEqual(len(log["entries"]), 200)
        messages = "\n".join(entry["message"] for entry in log["entries"])
        self.assertIn("progress 204", messages)
        self.assertIn("inspect repository behavior", messages)

    def test_run_log_isolates_each_visible_historical_issue_run(self) -> None:
        first_state = self.root / "run" / "agent-state"
        second_root = self.root / "run-2"
        second_state = second_root / "agent-state"
        first_state.mkdir(parents=True)
        second_state.mkdir(parents=True)
        raw_secret = "controller-only-history-secret"  # pragma: allowlist secret
        (first_state / "action-history.json").write_text(
            json.dumps([f"run one action {raw_secret}"]),
            encoding="utf-8",
        )
        (second_state / "action-history.json").write_text(
            json.dumps(["run two historical action"]),
            encoding="utf-8",
        )
        with self.db.transaction() as connection:
            connection.execute("""INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-2', 'repo-1', 'I2', 4,
                           'https://github.com/owner/repo/issues/4',
                           'Historical issue', 'Body', '[]',
                           '2025-12-31T23:00:00Z')""")
            connection.execute("""INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-2', 'repo-1', 'issue-2', 'event-2',
                           '2025-12-31T23:00:00Z')""")
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, last_completed_state, reason,
                    checkout_path, run_path, created_at, updated_at, closed_at)
                   VALUES ('run-2', 'repo-1', 'issue-2', 'activation-2',
                           'sandbox-1', 'team-1', 'main', ?, 'closed',
                           'waiting_for_feedback', 'completed externally',
                           ?, ?, ?, ?, ?)""",
                (
                    "b" * 40,
                    str(second_root / "checkout"),
                    str(second_root),
                    "2025-12-31T23:00:00Z",
                    "2025-12-31T23:05:00Z",
                    "2025-12-31T23:05:00Z",
                ),
            )
            connection.execute("""INSERT INTO run_transitions
                   (run_id, from_state, to_state, reason, occurred_at)
                   VALUES ('run-1', 'created', 'queued', 'run one only',
                           '2026-01-01T00:00:00Z')""")
            connection.execute("""INSERT INTO run_transitions
                   (run_id, from_state, to_state, reason, occurred_at)
                   VALUES ('run-2', 'waiting_for_feedback', 'closed',
                           'run two only', '2025-12-31T23:05:00Z')""")
        actions = ApplicationActions(
            database=self.db,
            onboarding=FakeOnboarding(),
            lifecycle=FakeLifecycle(self.db),
            scheduler=FakeScheduler(),
            known_secret_values=lambda selected_run_id: (
                (raw_secret,) if selected_run_id == "run-1" else ()
            ),
        )

        first = actions.run_log("run-1")
        second = actions.run_log("run-2")

        self.assertEqual(
            first["issue"],
            {
                "id": "issue-1",
                "number": 3,
                "title": "Fix scrolling",
                "url": "https://github.com/owner/repo/issues/3",
            },
        )
        self.assertEqual(first["state"], "queued")
        self.assertIs(first["active"], True)
        self.assertEqual(second["issue"]["number"], 4)
        self.assertEqual(second["state"], "closed")
        self.assertIs(second["active"], False)
        first_messages = "\n".join(entry["message"] for entry in first["entries"])
        second_messages = "\n".join(entry["message"] for entry in second["entries"])
        self.assertIn("run one only", first_messages)
        self.assertIn("run one action [REDACTED]", first_messages)
        self.assertNotIn(raw_secret, first_messages)
        self.assertNotIn("run two", first_messages)
        self.assertIn("run two only", second_messages)
        self.assertIn("run two historical action", second_messages)
        self.assertNotIn("run one", second_messages)
        with self.assertRaises(KeyError):
            actions.run_log("missing-run")

        with self.db.transaction() as connection:
            connection.execute("""UPDATE repositories
                   SET removed_at='2026-01-01T01:00:00Z'
                   WHERE id='repo-1'""")
        with self.assertRaises(KeyError):
            actions.run_log("run-1")

    def test_application_actions_expose_durable_state_and_controller_operations(
        self,
    ) -> None:
        artifact_path = self.root / "acceptance-proof.png"
        artifact_body = b"\x89PNG\r\n\x1a\napplication-proof"
        artifact_path.write_bytes(artifact_body)
        commit_sha = "b" * 40
        report = {
            "id": "acceptance-1",
            "run_id": "run-1",
            "commit_sha": commit_sha,
            "state": "passed",
            "summary": "Scrolling works.",
            "claims": [
                {
                    "key": "scroll",
                    "claim": "Wheel input navigates history.",
                    "result": "pass",
                    "observed": "Rows changed.",
                    "evidence": [1],
                }
            ],
            "scope": [],
            "screenshot_decision": {"required": True, "reason": "Visual issue."},
            "artifacts": [
                {
                    "id": "artifact-1",
                    "claim_key": "scroll",
                    "kind": "screenshot",
                    "path": str(artifact_path),
                    "sha256": hashlib.sha256(artifact_body).hexdigest(),
                    "media_type": "image/png",
                    "description": "Scrolled history.",
                    "commit_sha": commit_sha,
                    "metadata": {},
                }
            ],
            "limitations": [],
        }
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO issue_versions
                   (id, issue_id, version, previous_version_id,
                    github_updated_at, content_sha256, title, body,
                    discussion_json, observed_at)
                   SELECT 'issue-version-ui', id, 1, NULL, updated_at, ?,
                          title, body, discussion_json,
                          '2026-01-01T00:00:00Z'
                   FROM issues WHERE id='issue-1'""",
                ("a" * 64,),
            )
            connection.execute(
                """UPDATE issues SET current_version_id='issue-version-ui'
                   WHERE id='issue-1'"""
            )
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES ('verifier-1', 'team-1', 'verification', 'verifier',
                           'behavior verifier', 'Verify', '["read","run"]',
                           'mini-swe-agent', 'test', '')""")
            connection.execute(
                """UPDATE runs
                   SET validated_sha=?,
                       validated_issue_version_id='issue-version-ui'
                   WHERE id='run-1'""",
                (commit_sha,),
            )
            connection.execute(
                """INSERT INTO acceptance_verifications
                   (id, run_id, commit_sha, issue_version_id, attempt,
                    verifier_member_id, state, claims_json,
                    screenshot_decision_json, report_json, started_at,
                    completed_at)
                   VALUES ('acceptance-1', 'run-1', ?, 'issue-version-ui', 1,
                           'verifier-1', 'passed', ?, ?, ?, ?, ?)""",
                (
                    commit_sha,
                    json.dumps(report["claims"]),
                    json.dumps(report["screenshot_decision"]),
                    json.dumps(report),
                    "2026-01-01T00:01:00Z",
                    "2026-01-01T00:02:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO acceptance_artifacts
                   (id, verification_id, claim_key, kind, path, sha256,
                    media_type, description, metadata_json, created_at)
                   VALUES ('artifact-1', 'acceptance-1', 'scroll', 'screenshot',
                           ?, ?, 'image/png', 'Scrolled history.', '{}',
                           '2026-01-01T00:02:00Z')""",
                (
                    str(artifact_path),
                    report["artifacts"][0]["sha256"],
                ),
            )
        onboarding = FakeOnboarding()
        lifecycle = FakeLifecycle(self.db)
        scheduler = FakeScheduler()
        actions = ApplicationActions(
            database=self.db,
            onboarding=onboarding,
            lifecycle=lifecycle,
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
        self.assertNotIn("notifications", state)
        self.assertIsNone(run["last_completed_state"])
        self.assertEqual(run["sandbox_version_id"], "sandbox-1")
        self.assertEqual(run["team_version_id"], "team-1")
        self.assertEqual(run["validation_results"], [])
        self.assertEqual(run["assignments"], [])
        self.assertEqual(
            run["acceptance_verification"]["commit_sha"],
            commit_sha,
        )
        self.assertEqual(
            run["acceptance_verification"]["claims"][0]["key"],
            "scroll",
        )
        body, media_type = actions.acceptance_artifact("artifact-1")
        self.assertEqual(body, artifact_body)
        self.assertEqual(media_type, "image/png")
        self.assertEqual(
            actions.add_repository("owner/second", {"allowed_services": []}), "repo-2"
        )
        actions.reonboard("repo-1", {"allowed_services": ["api.github.com:443"]})
        with self.db.connect() as connection:
            stored = json.loads(
                connection.execute(
                    "SELECT inputs_json FROM repositories WHERE id='repo-1'"
                ).fetchone()[0]
            )
        self.assertEqual(stored["allowed_services"], ["packages.example:443"])
        actions.cancel("run-1")
        actions.poll()
        self.assertEqual(
            onboarding.calls[-1],
            ("reonboard", "repo-1", {"allowed_services": ["api.github.com:443"]}),
        )
        self.assertIn(("cancel", "run-1", "canceled by user"), lifecycle.calls)
        self.assertEqual(scheduler.requests, 3)

    def test_processing_feedback_in_publishing_routes_through_feedback_recovery(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute("UPDATE runs SET state='publishing' WHERE id='run-1'")
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
        )

        orchestrator._advance("run-1")

        self.assertEqual(publication.calls, [])
        self.assertGreaterEqual(feedback.calls.count("run-1"), 1)

    def test_transient_feedback_resolution_preserves_waiting_run_for_next_tick(
        self,
    ) -> None:
        class FailingFeedback(FakeFeedback):
            def resolve_run(self, run_id: str) -> int:
                self.calls.append(run_id)
                raise GitHubError("GitHub unavailable")

        with self.db.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET state='waiting_for_feedback', reason=NULL
                   WHERE id='run-1'""")
        lifecycle = FakeLifecycle(self.db)
        feedback = FailingFeedback(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=FakeExecution(self.db),
            publication=FakePublication(self.db),
            feedback=feedback,
        )

        orchestrator._advance("run-1")

        self.assertEqual(
            lifecycle.get_run("run-1")["state"],
            RunState.WAITING_FOR_FEEDBACK.value,
        )
        self.assertEqual(feedback.calls, ["run-1"])
        self.assertIn("GitHub unavailable", orchestrator.last_errors[0])

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
        )

        orchestrator._advance("run-1")

        run = lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "implementing")
        self.assertIsNone(run["reason"])
        self.assertEqual(execution.calls, ["run-1"])
        self.assertIn(
            "temporary controller boundary failure", orchestrator.last_errors[0]
        )

    def test_build_runtime_starts_unconfigured_without_ambient_model_discovery(
        self,
    ) -> None:
        ambient_configuration = {
            "HOME": str(self.root / "poisoned-home"),
            "OMP_MODEL": "ambient/omp-model",
            "PI_MODEL": "ambient/pi-model",
            "MSWEA_GLOBAL_CONFIG_DIR": str(self.root / "ambient-mini-swe"),
        }
        with patch.dict(os.environ, ambient_configuration, clear=True):
            runtime = build_runtime(self.root / "missing-model", model=None)

        self.assertEqual(
            runtime.actions.state()["model_configuration"],
            {
                "configured": False,
                "api_endpoint": None,
                "default_model": None,
                "lead_model": None,
                "implementer_model": None,
                "verifier_model": None,
                "api_key_configured": False,
                "api_key_required": False,
                "api_key_source": None,
            },
        )
        with self.assertRaisesRegex(RuntimeError, "Model provider.*not configured"):
            runtime.model_configuration.model_for_role("lead")

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
                {
                    "REPOGENTS_SECRET_PACKAGE_TOKEN": "canary-value"  # pragma: allowlist secret
                },
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
            runtime.actions.configure_model(
                {
                    "api_endpoint": "https://updated.example.test/v1",
                    "api_key": "updated-dashboard-key",  # pragma: allowlist secret
                    "default_model": "openai/new-default",
                    "lead_model": "openai/new-lead",
                    "implementer_model": "openai/new-implementer",
                    "verifier_model": "openai/new-verifier",
                }
            )
            updated_execution_boundary = runtime.execution.runtime_factory(
                "mini-swe-agent",
                "openai/gpt-stored",
                778,
            )

        self.assertIs(
            execution_boundary,
            execution_runtime_type.return_value,
        )
        onboarding_arguments = onboarding_analyzer_type.call_args.kwargs
        execution_arguments = execution_runtime_type.call_args_list[0].kwargs
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
        self.assertIs(
            updated_execution_boundary,
            execution_runtime_type.return_value,
        )
        updated_execution_arguments = execution_runtime_type.call_args_list[-1].kwargs
        self.assertEqual(
            updated_execution_arguments["model"],
            "openai/gpt-stored",
        )
        self.assertEqual(
            updated_execution_arguments["base_url"],
            "https://updated.example.test/v1",
        )
        self.assertEqual(
            updated_execution_arguments["api_key"],
            "updated-dashboard-key",
        )
        self.assertEqual(updated_execution_arguments["timeout"], 778)
        public_configuration = runtime.actions.state()["model_configuration"]
        self.assertEqual(public_configuration["default_model"], "openai/new-default")
        self.assertNotIn(
            "updated-dashboard-key",
            json.dumps(public_configuration),
        )
        with runtime.database.connect() as connection:
            database_dump = "\n".join(connection.iterdump())
        self.assertNotIn("updated-dashboard-key", database_dump)
        self.assertEqual(
            onboarding_arguments["configuration_resolver"](),
            (
                "openai/new-default",
                "https://updated.example.test/v1",
                "updated-dashboard-key",
            ),
        )
        for arguments in (scope_arguments, feedback_arguments):
            self.assertEqual(
                arguments["connection_resolver"]("openai/gpt-stored"),
                (
                    "https://updated.example.test/v1",
                    "updated-dashboard-key",
                ),
            )
        self.assertEqual(
            runtime.onboarding.team_formulator.model_resolver("lead"),
            "openai/new-lead",
        )
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
            onboarding_analyzer_type: resolved_root / "model-state" / "onboarding",
            scope_reviewer_type: resolved_root / "model-state" / "scope-review",
            feedback_evaluator_type: resolved_root / "model-state" / "feedback",
        }
        observed_state_roots: set[Path] = set()
        for boundary_type, expected_state_root in expected_state_roots.items():
            state_root = Path(boundary_type.call_args.kwargs["state_root"]).resolve()
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

    def test_run_priority_controls_reorder_force_and_release(self) -> None:
        self.seed_second_run()
        scheduler = FakeScheduler()
        actions = ApplicationActions(
            database=self.db,
            onboarding=FakeOnboarding(),
            lifecycle=FakeLifecycle(self.db),
            scheduler=scheduler,
        )

        actions.reorder_runs(["run-2", "run-1"])

        state = actions.state()
        self.assertEqual([run["id"] for run in state["runs"]], ["run-2", "run-1"])
        self.assertEqual([run["priority"] for run in state["runs"]], [0, 1])
        self.assertEqual(scheduler.requests, 1)
        reopened = Database(self.db.path)
        reopened.initialize()
        reopened_actions = ApplicationActions(
            database=reopened,
            onboarding=FakeOnboarding(),
            lifecycle=FakeLifecycle(reopened),
            scheduler=FakeScheduler(),
        )
        self.assertEqual(
            [run["id"] for run in reopened_actions.state()["runs"]],
            ["run-2", "run-1"],
        )

        actions.set_run_forced("run-1", True)

        focused = actions.state()["runs"]
        self.assertEqual([run["id"] for run in focused], ["run-1", "run-2"])
        self.assertIs(focused[0]["forced"], True)
        self.assertIs(focused[1]["forced"], False)
        self.assertEqual(scheduler.requests, 2)

        actions.set_run_forced("run-2", True)
        transferred = actions.state()["runs"]
        self.assertEqual([run["id"] for run in transferred], ["run-2", "run-1"])
        self.assertIs(transferred[0]["forced"], True)
        self.assertIs(transferred[1]["forced"], False)
        self.assertEqual(scheduler.requests, 3)

        actions.set_run_forced("run-2", False)
        self.assertFalse(actions.state()["runs"][0]["forced"])
        self.assertEqual(scheduler.requests, 4)

        with self.assertRaises(ValueError):
            actions.reorder_runs(["run-1", "run-1"])
        with self.assertRaises(KeyError):
            actions.reorder_runs(["run-1", "missing"])
        with self.assertRaises(ValueError):
            actions.reorder_runs(["run-1"])
        with self.db.transaction() as connection:
            connection.execute("UPDATE runs SET state='closed' WHERE id='run-2'")
        with self.assertRaises(ValueError):
            actions.set_run_forced("run-2", True)
        with self.db.transaction() as connection:
            connection.execute("UPDATE runs SET state='closed'")
        actions.remove_repository("repo-1")
        with self.assertRaises(KeyError):
            actions.reorder_runs(["run-1", "run-2"])
        with self.assertRaises(KeyError):
            actions.set_run_forced("run-1", True)

    def test_terminal_runs_leave_the_active_queue_without_losing_history(
        self,
    ) -> None:
        self.seed_second_run()
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='closed', reason='merged on GitHub', priority=0,
                       updated_at='2026-01-01T00:03:00Z'
                   WHERE id='run-1'"""
            )
            connection.execute(
                """UPDATE runs
                   SET state='queued', reason='requirements changed', priority=7
                   WHERE id='run-2'"""
            )
        scheduler = FakeScheduler()
        actions = ApplicationActions(
            database=self.db,
            onboarding=FakeOnboarding(),
            lifecycle=FakeLifecycle(self.db),
            scheduler=scheduler,
        )

        state = actions.state()

        self.assertEqual([run["id"] for run in state["runs"]], ["run-2"])
        self.assertEqual([run["queue_position"] for run in state["runs"]], [1])
        self.assertEqual(state["runs"][0]["reason_severity"], "neutral")
        repository = state["repositories"][0]
        self.assertEqual(repository["latest_run_state"], "queued")
        self.assertIs(repository["active"], True)
        with self.db.connect() as connection:
            closed = connection.execute(
                "SELECT state, reason, priority FROM runs WHERE id='run-1'"
            ).fetchone()
        self.assertEqual(tuple(closed), ("closed", "merged on GitHub", 0))

        with self.assertRaises(KeyError):
            actions.reorder_runs(["run-1", "run-2"])
        actions.reorder_runs(["run-2"])
        with self.db.connect() as connection:
            priorities = connection.execute(
                "SELECT id, priority FROM runs ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in priorities],
            [("run-1", 0), ("run-2", 0)],
        )

        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='blocked', reason='operator input required'
                   WHERE id='run-2'"""
            )
        blocked = actions.state()
        self.assertEqual(blocked["runs"][0]["reason_severity"], "error")
        self.assertEqual(blocked["repositories"][0]["latest_run_state"], "blocked")
        self.assertIs(blocked["repositories"][0]["active"], False)

        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='canceled' WHERE id='run-2'"
            )
        terminal = actions.state()
        self.assertEqual(terminal["runs"], [])
        self.assertIsNone(terminal["repositories"][0]["latest_run_state"])
        with self.db.connect() as connection:
            durable_count = connection.execute(
                "SELECT COUNT(*) FROM runs"
            ).fetchone()[0]
        self.assertEqual(durable_count, 2)

    def test_tick_uses_priority_and_exclusive_forced_run(self) -> None:
        self.seed_second_run()
        with self.db.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET priority=CASE id WHEN 'run-1' THEN 1 ELSE 0 END,
                       force_requested_at=CASE
                           WHEN id='run-1' THEN '2026-01-01T00:02:00Z'
                           ELSE NULL
                       END""")
        lifecycle = FakeLifecycle(self.db)
        execution = FakeExecution(self.db)
        orchestrator = Orchestrator(
            database=self.db,
            lifecycle=lifecycle,
            execution=execution,
            publication=FakePublication(self.db),
            feedback=FakeFeedback(self.db),
        )

        orchestrator.tick()

        self.assertEqual(execution.calls, ["run-1"])
        with self.db.connect() as connection:
            forced = connection.execute(
                "SELECT force_requested_at FROM runs WHERE id='run-1'"
            ).fetchone()[0]
        self.assertIsNone(forced)

        with self.db.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET state='queued', reason=NULL, force_requested_at=NULL""")
        execution.calls.clear()

        orchestrator.tick()

        self.assertEqual(execution.calls, ["run-2", "run-1"])


if __name__ == "__main__":
    unittest.main()
