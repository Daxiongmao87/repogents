from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from repogents.database import Database
from repogents.controller import git_environment as controller_git_environment
from repogents.github import ActivationEvent, IssueInfo
from repogents.lifecycle import GitCheckoutManager, RunLifecycle, RunState, allowed_transition


@dataclass
class FakeActivationClient:
    events: list[ActivationEvent]
    base_sha: str = "b" * 40
    polls: int = 0

    def list_ready_issues(self, owner: str, name: str) -> tuple[IssueInfo, ...]:
        return tuple(event.issue for event in self.events)

    def list_ready_events(self, owner: str, name: str) -> list[ActivationEvent]:
        self.polls += 1
        return list(self.events)

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        return self.base_sha


class FakeCheckoutManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []

    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        self.calls.append((str(source), base_sha, destination))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ".base-sha").write_text(base_sha, encoding="utf-8")


class FailingCheckoutManager:
    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        raise RuntimeError("stored source is unavailable")


class FakeSandboxManager:
    def __init__(self) -> None:
        self.canceled: list[str] = []

    def cancel(self, run_id: str) -> bool:
        self.canceled.append(run_id)
        return True


class RacingFailingSandboxManager:
    def __init__(self) -> None:
        self.lifecycle: RunLifecycle | None = None
        self.state_when_signaled: str | None = None
        self.transition_error: Exception | None = None

    def cancel(self, run_id: str) -> bool:
        if self.lifecycle is None:
            raise AssertionError("lifecycle was not attached")
        self.state_when_signaled = str(self.lifecycle.get_run(run_id)["state"])
        try:
            self.lifecycle.transition(run_id, RunState.VALIDATING)
        except Exception as error:
            self.transition_error = error
        raise RuntimeError("sandbox signaling failed")


class GitCheckoutManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.admin = self.root / "admin"
        self.source = self.root / "source"
        self.checkout = self.root / "checkout"
        self._git("init", "--bare", str(self.remote))
        self._git("init", "-b", "main", str(self.admin))
        self._git("config", "user.name", "Test", cwd=self.admin)
        self._git("config", "user.email", "test@example.com", cwd=self.admin)
        self._git("remote", "add", "origin", self.remote.as_uri(), cwd=self.admin)
        (self.admin / "value.txt").write_text("onboarding\n", encoding="utf-8")
        self._git("add", "value.txt", cwd=self.admin)
        self._git("commit", "-m", "onboarding snapshot", cwd=self.admin)
        self._git("push", "-u", "origin", "main", cwd=self.admin)
        self._git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        self._git("clone", "--depth", "1", self.remote.as_uri(), str(self.source))

    def _git(self, *arguments: str, cwd: Path | None = None, check: bool = True) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"git {' '.join(arguments)} failed: {result.stderr}")
        return result.stdout.strip()

    def _push_commit(self, content: str, message: str) -> str:
        (self.admin / "value.txt").write_text(content, encoding="utf-8")
        self._git("add", "value.txt", cwd=self.admin)
        self._git("commit", "-m", message, cwd=self.admin)
        self._git("push", "origin", "main", cwd=self.admin)
        return self._git("rev-parse", "HEAD", cwd=self.admin)

    def test_fetches_and_retains_exact_base_from_origin_across_source_refresh(self) -> None:
        base_sha = self._push_commit("activation\n", "activation base")
        self.assertNotEqual(
            subprocess.run(
                ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"],
                cwd=self.source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode,
            0,
        )
        observed_environments: list[tuple[str | None, dict[str, str]]] = []

        @contextmanager
        def recording_environment(token: str | None):
            ambient = dict(os.environ)
            ambient.update({"GH_TOKEN": "ambient-gh", "GITHUB_TOKEN": "ambient-github"})
            with controller_git_environment(token, source=ambient) as environment:
                observed_environments.append((token, dict(environment)))
                yield environment

        with patch("repogents.lifecycle.git_environment", recording_environment):
            manager = GitCheckoutManager(token="configured-token")
            manager.create(self.source, base_sha, self.checkout)
            self.assertEqual(self._git("rev-parse", "HEAD", cwd=self.checkout), base_sha)
            self.assertEqual(
                self._git("rev-parse", f"refs/repogents/bases/{base_sha}", cwd=self.checkout),
                base_sha,
            )

            shutil.rmtree(self.checkout)
            self._git("clone", "--depth", "1", self.source.as_uri(), str(self.checkout))
            self.assertNotEqual(self._git("rev-parse", "HEAD", cwd=self.checkout), base_sha)
            manager.create(self.source, base_sha, self.checkout)
            self.assertEqual(self._git("rev-parse", "HEAD", cwd=self.checkout), base_sha)

            self._push_commit("re-onboarded\n", "later source")
            shutil.rmtree(self.source)
            self._git("clone", "--depth", "1", self.remote.as_uri(), str(self.source))
            self.assertNotEqual(self._git("rev-parse", "HEAD", cwd=self.source), base_sha)
            self.assertNotEqual(
                subprocess.run(
                    ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"],
                    cwd=self.source,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode,
                0,
            )
            shutil.rmtree(self.checkout)
            manager.create(self.source, base_sha, self.checkout)

        self.assertEqual(self._git("rev-parse", "HEAD", cwd=self.checkout), base_sha)
        self.assertEqual([token for token, _ in observed_environments], ["configured-token"] * 3)
        for _, environment in observed_environments:
            self.assertNotIn("GH_TOKEN", environment)
            self.assertNotIn("GITHUB_TOKEN", environment)
            self.assertEqual(environment["REPOGENTS_GITHUB_TOKEN"], "configured-token")


class RunLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        repository_root = self.data_root / "repositories" / "repo-1"
        (repository_root / "source").mkdir(parents=True)
        sandbox_root = repository_root / "sandbox" / "1"
        sandbox_root.mkdir(parents=True)
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, current_sandbox_version_id,
                    current_team_version_id, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'repo-url', 'main',
                           'ready', NULL, NULL, ?, ?)""",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, ?, '{}', '{}', ?)""",
                (str(sandbox_root), "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, '{}', ?)""",
                ("2026-01-01T00:00:00Z",),
            )
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own result',
                           '[\"read\"]', 'test', 'test', '')"""
            )
            connection.execute(
                """UPDATE repositories SET current_sandbox_version_id='sandbox-1',
                                             current_team_version_id='team-1'
                   WHERE id='repo-1'"""
            )
        issue = IssueInfo(
            node_id="I3",
            number=3,
            url="https://github.com/owner/repo/issues/3",
            title="Terse issue",
            body="Fix it",
            discussion=({"id": 1, "author": "reviewer", "body": "context"},),
            updated_at="2026-01-01T00:00:00Z",
        )
        self.event = ActivationEvent(
            event_id="event-1",
            applied_at="2026-01-01T00:00:00Z",
            issue=issue,
        )
        self.github = FakeActivationClient([self.event])
        self.checkouts = FakeCheckoutManager()
        self.sandbox = FakeSandboxManager()
        self.lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=self.github,
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )

    def test_transition_table_covers_expected_paths_and_rejects_invalid(self) -> None:
        self.assertTrue(allowed_transition(RunState.QUEUED, RunState.IMPLEMENTING))
        self.assertTrue(allowed_transition(RunState.VALIDATING, RunState.PUBLISHING))
        self.assertTrue(allowed_transition(RunState.QUIET_PERIOD, RunState.RESOLVING_FEEDBACK))
        self.assertTrue(allowed_transition(RunState.NOTIFIED, RunState.CLOSED))
        self.assertTrue(allowed_transition(RunState.PUBLISHING, RunState.CLOSED))
        self.assertFalse(allowed_transition(RunState.QUEUED, RunState.PUBLISHING))
        self.assertFalse(allowed_transition(RunState.CANCELED, RunState.QUEUED))
        self.assertFalse(allowed_transition(RunState.CLOSED, RunState.RESOLVING_FEEDBACK))

    def test_repeated_poll_and_restart_create_exactly_one_run(self) -> None:
        first = self.lifecycle.poll_repository("repo-1")
        second = self.lifecycle.poll_repository("repo-1")
        restarted = RunLifecycle(
            database=Database(self.root / "db.sqlite3"),
            data_root=self.data_root,
            github=self.github,
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )
        restarted.database.initialize()
        third = restarted.poll_repository("repo-1")
        self.assertEqual(len(first), 1)
        self.assertEqual(second, ())
        self.assertEqual(third, ())
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
            run = connection.execute("SELECT * FROM runs").fetchone()
        self.assertEqual(run["sandbox_version_id"], "sandbox-1")
        self.assertEqual(run["team_version_id"], "team-1")
        self.assertEqual(run["intended_base_branch"], "main")
        self.assertEqual(run["base_sha"], "b" * 40)
        self.assertTrue(Path(run["checkout_path"]).is_dir())
        self.assertTrue(Path(run["run_path"]).is_dir())
        self.assertEqual(len(self.checkouts.calls), 1)

    def test_new_activation_waits_for_prior_terminal_run(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        second_event = ActivationEvent(
            event_id="event-2",
            applied_at="2026-01-02T00:00:00Z",
            issue=self.event.issue,
        )
        self.github.events.append(second_event)
        self.assertEqual(self.lifecycle.poll_repository("repo-1"), ())
        self.lifecycle.transition(run_id, RunState.CANCELED, reason="user canceled")
        created = self.lifecycle.poll_repository("repo-1")
        self.assertEqual(len(created), 1)
        self.assertNotEqual(created[0], run_id)

    def test_block_is_durable_and_cancel_preserves_run_evidence(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(
            run_id, RunState.BLOCKED, reason="irreducible dependency unavailable"
        )
        with self.assertRaises(ValueError):
            self.lifecycle.transition(run_id, RunState.QUEUED)
        self.assertEqual(self.lifecycle.reconcile_nonterminal_runs(), ())
        self.lifecycle.cancel(run_id, "user requested cancellation")
        record = self.lifecycle.get_run(run_id)
        self.assertEqual(record["state"], "canceled")
        self.assertIn("user requested", record["reason"])
        self.assertEqual(self.sandbox.canceled, [run_id])
        self.assertTrue(Path(record["run_path"]).is_dir())

    def test_cancel_is_durable_before_signaling_and_rejects_transition_race(self) -> None:
        sandbox = RacingFailingSandboxManager()
        lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=self.github,
            checkouts=self.checkouts,
            sandbox=sandbox,
        )
        sandbox.lifecycle = lifecycle
        run_id = lifecycle.poll_repository("repo-1")[0]
        lifecycle.transition(run_id, RunState.IMPLEMENTING)

        with self.assertRaisesRegex(RuntimeError, "sandbox signaling failed"):
            lifecycle.cancel(run_id, "operator cancellation")

        record = lifecycle.get_run(run_id)
        self.assertEqual(record["id"], run_id)
        self.assertEqual(record["state"], RunState.CANCELED.value)
        self.assertEqual(record["reason"], "operator cancellation")
        self.assertIsNotNone(record["canceled_at"])
        self.assertEqual(sandbox.state_when_signaled, RunState.CANCELED.value)
        self.assertIsInstance(sandbox.transition_error, ValueError)
        with self.db.connect() as connection:
            transitions = connection.execute(
                "SELECT to_state FROM run_transitions WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        self.assertEqual([row["to_state"] for row in transitions][-1], RunState.CANCELED.value)


    def test_ready_issue_discovery_records_successful_empty_inventory(self) -> None:
        self.github.events = []

        self.assertEqual(self.lifecycle.poll_repository("repo-1"), ())

        with self.db.connect() as connection:
            discovery = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(discovery["status"], "available")
        self.assertEqual(discovery["issues_json"], "[]")
        self.assertIsNotNone(discovery["last_success_at"])
        self.assertIsNone(discovery["error"])

    def test_ready_issue_discovery_records_unavailable_without_cache(self) -> None:
        from repogents.github import GitHubError

        class FailingDiscoveryClient(FakeActivationClient):
            def list_ready_issues(self, owner: str, name: str) -> tuple[IssueInfo, ...]:
                raise GitHubError("rate limited")

        lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=FailingDiscoveryClient([]),
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )

        self.assertEqual(lifecycle.poll_repository("repo-1"), ())

        with self.db.connect() as connection:
            discovery = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(discovery["status"], "unavailable")
        self.assertEqual(discovery["issues_json"], "[]")
        self.assertIsNone(discovery["last_success_at"])
        self.assertEqual(discovery["error"], "GitHub ready-issue discovery failed")

    def test_ready_issue_discovery_retains_stale_cache_after_failure(self) -> None:
        from repogents.github import GitHubError

        self.lifecycle.poll_repository("repo-1")

        class FailingDiscoveryClient(FakeActivationClient):
            def list_ready_issues(self, owner: str, name: str) -> tuple[IssueInfo, ...]:
                raise GitHubError("server unavailable")

        lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=FailingDiscoveryClient([]),
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )
        lifecycle.poll_repository("repo-1")

        with self.db.connect() as connection:
            discovery = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(discovery["status"], "stale")
        self.assertIn('"number": 3', discovery["issues_json"])
        self.assertIsNotNone(discovery["last_success_at"])
        self.assertEqual(discovery["error"], "GitHub ready-issue discovery failed")

    def test_reconcile_recreates_missing_run_storage_without_new_identity(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        record = self.lifecycle.get_run(run_id)
        marker = Path(record["checkout_path"]) / ".base-sha"
        marker.unlink()
        reconciled = self.lifecycle.reconcile_nonterminal_runs()
        self.assertEqual(reconciled, (run_id,))
        self.assertTrue(marker.exists())
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
    def test_transient_restart_reconciliation_preserves_run_for_next_tick(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        restarted = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=self.github,
            checkouts=FailingCheckoutManager(),
            sandbox=self.sandbox,
        )

        with self.assertRaisesRegex(RuntimeError, "stored source is unavailable"):
            restarted.reconcile_nonterminal_runs()

        record = restarted.get_run(run_id)
        self.assertEqual(record["state"], "queued")
        self.assertIsNone(record["reason"])
        with self.db.connect() as connection:
            blocked = connection.execute(
                """SELECT COUNT(*) FROM run_transitions
                   WHERE run_id=? AND to_state='blocked'""",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(blocked, 0)



if __name__ == "__main__":
    unittest.main()
