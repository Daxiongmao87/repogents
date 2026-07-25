from __future__ import annotations

import json
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
from repogents.github import ActivationEvent, GitHubError, IssueInfo
from repogents.lifecycle import (
    GitCheckoutManager,
    RunLifecycle,
    RunState,
    allowed_transition,
)


@dataclass
class FakeActivationClient:
    events: list[ActivationEvent]
    current_issue: IssueInfo | None = None
    base_sha: str = "b" * 40
    polls: int = 0
    issue_polls: int = 0

    def list_ready_events(self, owner: str, name: str) -> list[ActivationEvent]:
        self.polls += 1
        return list(self.events)

    def get_issue(self, owner: str, name: str, number: int) -> IssueInfo:
        self.issue_polls += 1
        issue = self.current_issue or self.events[-1].issue
        if issue.number != number:
            raise AssertionError(f"unexpected issue number {number}")
        return issue

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


class FakeProcessSupervisor:
    def __init__(self) -> None:
        self.paused: list[str] = []
        self.resumed: list[str] = []

    def pause(self, run_id: str) -> None:
        self.paused.append(run_id)

    def resume(self, run_id: str) -> None:
        self.resumed.append(run_id)


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

    def test_fetches_and_retains_exact_base_from_origin_across_source_refresh(
        self,
    ) -> None:
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
            self.assertEqual(
                self._git("rev-parse", "HEAD", cwd=self.checkout), base_sha
            )
            self.assertEqual(
                self._git(
                    "rev-parse", f"refs/repogents/bases/{base_sha}", cwd=self.checkout
                ),
                base_sha,
            )

            shutil.rmtree(self.checkout)
            self._git("clone", "--depth", "1", self.source.as_uri(), str(self.checkout))
            self.assertNotEqual(
                self._git("rev-parse", "HEAD", cwd=self.checkout), base_sha
            )
            manager.create(self.source, base_sha, self.checkout)
            self.assertEqual(
                self._git("rev-parse", "HEAD", cwd=self.checkout), base_sha
            )

            self._push_commit("re-onboarded\n", "later source")
            shutil.rmtree(self.source)
            self._git("clone", "--depth", "1", self.remote.as_uri(), str(self.source))
            self.assertNotEqual(
                self._git("rev-parse", "HEAD", cwd=self.source), base_sha
            )
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
        self.assertEqual(
            [token for token, _ in observed_environments], ["configured-token"] * 3
        )
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
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own result',
                           '[\"read\"]', 'test', 'test', '')""")
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
        self.assertTrue(
            allowed_transition(
                RunState.WAITING_FOR_FEEDBACK,
                RunState.RESOLVING_FEEDBACK,
            )
        )
        self.assertTrue(allowed_transition(RunState.BLOCKED, RunState.CLOSED))
        self.assertTrue(allowed_transition(RunState.PUBLISHING, RunState.CLOSED))
        self.assertFalse(allowed_transition(RunState.QUEUED, RunState.PUBLISHING))
        self.assertFalse(allowed_transition(RunState.CANCELED, RunState.QUEUED))
        self.assertFalse(
            allowed_transition(RunState.CLOSED, RunState.RESOLVING_FEEDBACK)
        )

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
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1
            )
            run = connection.execute("SELECT * FROM runs").fetchone()
        self.assertEqual(run["sandbox_version_id"], "sandbox-1")
        self.assertEqual(run["team_version_id"], "team-1")
        self.assertEqual(run["intended_base_branch"], "main")
        self.assertEqual(run["base_sha"], "b" * 40)
        self.assertTrue(Path(run["checkout_path"]).is_dir())
        self.assertTrue(Path(run["run_path"]).is_dir())
        self.assertEqual(len(self.checkouts.calls), 1)

    def test_activation_binds_one_immutable_initial_issue_version(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]

        with self.db.connect() as connection:
            version = connection.execute(
                """SELECT issue_versions.*, issues.current_version_id,
                          activation_events.issue_version_id AS activation_version_id
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN activation_events
                     ON activation_events.id=runs.activation_event_id
                   JOIN issue_versions
                     ON issue_versions.id=issues.current_version_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()

        self.assertEqual(version["version"], 1)
        self.assertEqual(version["title"], "Terse issue")
        self.assertEqual(version["body"], "Fix it")
        self.assertEqual(
            json.loads(version["discussion_json"]),
            [{"author": "reviewer", "body": "context", "id": 1}],
        )
        self.assertEqual(version["current_version_id"], version["id"])
        self.assertEqual(version["activation_version_id"], version["id"])
        self.assertEqual(len(version["content_sha256"]), 64)

    def test_changed_issue_wakes_blocked_run_and_supersedes_stale_proof(
        self,
    ) -> None:
        processes = FakeProcessSupervisor()
        lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=self.github,
            checkouts=self.checkouts,
            sandbox=self.sandbox,
            processes=processes,
        )
        run_id = lifecycle.poll_repository("repo-1")[0]
        lifecycle.transition(run_id, RunState.IMPLEMENTING)
        lifecycle.transition(run_id, RunState.VALIDATING)
        lifecycle.transition(run_id, RunState.PUBLISHING)
        with self.db.transaction() as connection:
            initial_version_id = connection.execute(
                "SELECT current_version_id FROM issues WHERE number=3"
            ).fetchone()[0]
            connection.execute(
                """UPDATE runs
                   SET validated_sha=?, validated_issue_version_id=?
                   WHERE id=?""",
                ("c" * 40, initial_version_id, run_id),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    validated_issue_version_id, remote_head_sha, state,
                    created_at, updated_at)
                   VALUES ('pull-1', ?, 'PR1', 9, 'pull-url',
                           'agent/issue-3-run', 'main', ?, ?, ?, ?, 'open',
                           '2026-01-01T00:01:00Z',
                           '2026-01-01T00:01:00Z')""",
                (
                    run_id,
                    "b" * 40,
                    "c" * 40,
                    initial_version_id,
                    "c" * 40,
                ),
            )
            connection.execute(
                """INSERT INTO acceptance_verifications
                   (id, run_id, commit_sha, issue_version_id, attempt,
                    verifier_member_id, state, claims_json,
                    screenshot_decision_json, report_json, started_at,
                    completed_at)
                   VALUES ('verification-1', ?, ?, ?, 1, 'lead-1', 'blocked',
                           '[]', '{}', '{"summary":"obsolete requirement"}',
                           '2026-01-01T00:01:00Z',
                           '2026-01-01T00:02:00Z')""",
                (run_id, "c" * 40, initial_version_id),
            )
        lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason="publication blocked: issue acceptance verification blocked",
        )
        self.github.current_issue = IssueInfo(
            node_id="I3",
            number=3,
            url="https://github.com/owner/repo/issues/3",
            title="Remove obsolete dependency",
            body="Use the direct provider. The external artifact is not a dependency.",
            discussion=(
                {"id": 1, "author": "reviewer", "body": "context"},
                {
                    "id": 2,
                    "author": "owner",
                    "body": "Do not require the external artifact.",
                },
            ),
            updated_at="2026-01-02T00:00:00Z",
        )

        self.assertTrue(lifecycle.poll_issue_revision(run_id))
        self.assertFalse(lifecycle.poll_issue_revision(run_id))

        with self.db.connect() as connection:
            run = connection.execute(
                """SELECT state, reason, validated_sha,
                          validated_issue_version_id
                   FROM runs WHERE id=?""",
                (run_id,),
            ).fetchone()
            versions = connection.execute(
                """SELECT id, version, previous_version_id, title, body,
                          discussion_json
                   FROM issue_versions
                   WHERE issue_id=(SELECT issue_id FROM runs WHERE id=?)
                   ORDER BY version""",
                (run_id,),
            ).fetchall()
            proof_state = connection.execute(
                """SELECT state FROM acceptance_verifications
                   WHERE id='verification-1'"""
            ).fetchone()[0]
            counts = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM runs WHERE id=?) AS runs,
                     (SELECT COUNT(*) FROM pull_requests WHERE run_id=?) AS pulls,
                     (SELECT COUNT(*) FROM activation_events
                        WHERE issue_id=(SELECT issue_id FROM runs WHERE id=?))
                       AS activations""",
                (run_id, run_id, run_id),
            ).fetchone()

        self.assertEqual(run["state"], RunState.IMPLEMENTING.value)
        self.assertIn("requirements changed", run["reason"])
        self.assertEqual(run["validated_sha"], "c" * 40)
        self.assertEqual(run["validated_issue_version_id"], versions[0]["id"])
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[1]["previous_version_id"], versions[0]["id"])
        self.assertEqual(versions[1]["title"], "Remove obsolete dependency")
        self.assertIn("not a dependency", versions[1]["body"])
        self.assertEqual(
            json.loads(versions[1]["discussion_json"])[1]["body"],
            "Do not require the external artifact.",
        )
        self.assertEqual(proof_state, "superseded")
        self.assertEqual(tuple(counts), (1, 1, 1))
        self.assertEqual(processes.paused, [run_id])
        self.assertEqual(processes.resumed, [run_id])
        self.assertEqual(self.sandbox.canceled, [run_id])
        self.assertEqual(self.github.issue_polls, 2)

    def test_rapid_issue_edits_fence_stale_validation_and_terminal_runs(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        with self.db.connect() as connection:
            initial_version_id = connection.execute(
                "SELECT current_version_id FROM issues WHERE number=3"
            ).fetchone()[0]

        self.github.current_issue = IssueInfo(
            node_id="I3",
            number=3,
            url="https://github.com/owner/repo/issues/3",
            title="First revision",
            body="Use the direct provider.",
            discussion=(),
            updated_at="2026-01-02T00:00:00Z",
        )
        self.assertTrue(self.lifecycle.poll_issue_revision(run_id))
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.assertFalse(
            self.lifecycle.record_validated_revision(
                run_id,
                "c" * 40,
                initial_version_id,
            )
        )

        self.github.current_issue = IssueInfo(
            node_id="I3",
            number=3,
            url="https://github.com/owner/repo/issues/3",
            title="Second revision",
            body="Use the direct provider without an external artifact.",
            discussion=(
                {
                    "id": 2,
                    "author": "owner",
                    "body": "The external artifact is optional.",
                },
            ),
            updated_at="2026-01-03T00:00:00Z",
        )
        self.assertTrue(self.lifecycle.poll_issue_revision(run_id))
        self.lifecycle.cancel(run_id, "fixture complete")
        self.github.current_issue = IssueInfo(
            node_id="I3",
            number=3,
            url="https://github.com/owner/repo/issues/3",
            title="Ignored terminal revision",
            body="A terminal run must stay terminal.",
            discussion=(),
            updated_at="2026-01-04T00:00:00Z",
        )
        self.assertFalse(self.lifecycle.poll_issue_revision(run_id))

        with self.db.connect() as connection:
            versions = connection.execute(
                """SELECT id, version, previous_version_id, title
                   FROM issue_versions
                   WHERE issue_id=(SELECT issue_id FROM runs WHERE id=?)
                   ORDER BY version""",
                (run_id,),
            ).fetchall()
        self.assertEqual(
            [(row["version"], row["title"]) for row in versions],
            [
                (1, "Terse issue"),
                (2, "First revision"),
                (3, "Second revision"),
            ],
        )
        self.assertEqual(versions[0]["id"], initial_version_id)
        self.assertEqual(versions[1]["previous_version_id"], versions[0]["id"])
        self.assertEqual(versions[2]["previous_version_id"], versions[1]["id"])
        self.assertEqual(
            self.lifecycle.get_run(run_id)["state"],
            RunState.CANCELED.value,
        )

    def test_new_issue_run_appends_after_existing_queue_entries(self) -> None:
        first_run = self.lifecycle.poll_repository("repo-1")[0]
        second_issue = IssueInfo(
            node_id="I4",
            number=4,
            url="https://github.com/owner/repo/issues/4",
            title="Second issue",
            body="Fix the other thing",
            discussion=(),
            updated_at="2026-01-02T00:00:00Z",
        )
        self.github.events.append(
            ActivationEvent(
                event_id="event-2",
                applied_at="2026-01-02T00:00:00Z",
                issue=second_issue,
            )
        )

        second_run = self.lifecycle.poll_repository("repo-1")[0]

        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id, priority FROM runs ORDER BY priority"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [(first_run, 0), (second_run, 1)],
        )

    def test_pause_survives_restart_and_resumes_same_run_identity(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        processes = FakeProcessSupervisor()
        lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=self.github,
            checkouts=self.checkouts,
            sandbox=self.sandbox,
            processes=processes,
        )
        with self.db.connect() as connection:
            transition_count = connection.execute(
                "SELECT COUNT(*) FROM run_transitions WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]

        self.assertEqual(lifecycle.set_repository_paused("repo-1", True), (run_id,))
        self.assertEqual(processes.paused, [run_id])
        self.assertEqual(self.sandbox.canceled, [run_id])
        self.assertEqual(lifecycle.poll_repository("repo-1"), ())
        self.assertEqual(lifecycle.reconcile_nonterminal_runs(), ())

        restarted_processes = FakeProcessSupervisor()
        restarted = RunLifecycle(
            database=Database(self.root / "db.sqlite3"),
            data_root=self.data_root,
            github=self.github,
            checkouts=self.checkouts,
            sandbox=self.sandbox,
            processes=restarted_processes,
        )
        restarted.database.initialize()
        self.assertEqual(
            restarted.set_repository_paused("repo-1", False),
            (run_id,),
        )

        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT id, state, checkout_path FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            repository_enabled = connection.execute(
                "SELECT enabled FROM repositories WHERE id='repo-1'"
            ).fetchone()[0]
            run_count = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE repository_id='repo-1'"
            ).fetchone()[0]
            resumed_transition_count = connection.execute(
                "SELECT COUNT(*) FROM run_transitions WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(tuple(run)[:2], (run_id, RunState.IMPLEMENTING.value))
        self.assertTrue(Path(run["checkout_path"]).is_dir())
        self.assertEqual(repository_enabled, 1)
        self.assertEqual(run_count, 1)
        self.assertEqual(resumed_transition_count, transition_count)
        self.assertEqual(restarted_processes.resumed, [run_id])

    def test_new_issue_event_is_detected_after_an_empty_poll(self) -> None:
        event = self.github.events.pop()
        self.assertEqual(self.lifecycle.poll_repository("repo-1"), ())

        restarted = RunLifecycle(
            database=Database(self.root / "db.sqlite3"),
            data_root=self.data_root,
            github=self.github,
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )
        restarted.database.initialize()
        self.github.events.append(event)
        created = restarted.poll_repository("repo-1")

        self.assertEqual(len(created), 1)
        with self.db.connect() as connection:
            issue = connection.execute(
                """SELECT issues.number, issues.title
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   WHERE runs.id=?""",
                (created[0],),
            ).fetchone()
        self.assertEqual(tuple(issue), (3, "Terse issue"))

    def test_transient_event_poll_failure_does_not_skip_later_event(self) -> None:
        with patch.object(
            self.github,
            "list_ready_events",
            side_effect=[GitHubError("temporary outage"), [self.event]],
        ):
            with self.assertRaisesRegex(GitHubError, "temporary outage"):
                self.lifecycle.poll_repository("repo-1")
            with self.db.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                    0,
                )

            created = self.lifecycle.poll_repository("repo-1")

        self.assertEqual(len(created), 1)

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

    def test_legacy_unchallenged_visual_block_is_requeued_once_with_history(
        self,
    ) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id=?",
                ("c" * 40, run_id),
            )
            connection.execute(
                """INSERT INTO acceptance_verifications
                   (id, run_id, commit_sha, attempt, verifier_member_id, state,
                    claims_json, screenshot_decision_json, report_json,
                    started_at, completed_at)
                   VALUES ('verification-1', ?, ?, 1, 'lead-1', 'blocked',
                           '[]', ?, ?, ?, ?)""",
                (
                    run_id,
                    "c" * 40,
                    json.dumps({"required": True}),
                    json.dumps(
                        {
                            "state": "blocked",
                            "summary": (
                                "The browser scenario remained on Loading Layout "
                                "until its socket probe timed out."
                            ),
                            "claims": [
                                {
                                    "method": "browser scenario",
                                    "result": "fail",
                                    "observed": "Dashboard remained loading.",
                                }
                            ],
                            "limitations": ["Required screenshots were not produced."],
                        }
                    ),
                    "2026-01-01T01:00:00Z",
                    "2026-01-01T01:01:00Z",
                ),
            )
            connection.execute("""INSERT INTO acceptance_evidence
                   (id, verification_id, sequence, action_json, result_json,
                    started_at, completed_at)
                   VALUES ('evidence-1', 'verification-1', 1,
                           '{"action":"run"}', '{"returncode":1}',
                           '2026-01-01T01:00:00Z', '2026-01-01T01:01:00Z')""")
        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason=(
                "publication blocked: issue acceptance verification blocked: "
                "browser scenario timed out"
            ),
        )

        self.assertEqual(
            self.lifecycle.reconcile_recoverable_blocked_runs(),
            (run_id,),
        )
        self.assertEqual(
            self.lifecycle.get_run(run_id)["state"],
            RunState.PUBLISHING.value,
        )
        with self.db.connect() as connection:
            history = connection.execute("""SELECT state, report_json
                   FROM acceptance_verifications
                   WHERE id='verification-1'""").fetchone()
            evidence_count = connection.execute(
                """SELECT COUNT(*) FROM acceptance_evidence
                   WHERE verification_id='verification-1'"""
            ).fetchone()[0]
            recovery_count = connection.execute(
                """SELECT COUNT(*) FROM run_transitions
                   WHERE run_id=? AND from_state='blocked'
                     AND to_state='publishing'""",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(history["state"], "blocked")
        self.assertIn("Loading Layout", history["report_json"])
        self.assertEqual(evidence_count, 1)
        self.assertEqual(recovery_count, 1)

        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason="publication blocked: corrected acceptance remained blocked",
        )
        self.assertEqual(
            self.lifecycle.reconcile_recoverable_blocked_runs(),
            (),
        )
        self.assertEqual(
            self.lifecycle.get_run(run_id)["state"],
            RunState.BLOCKED.value,
        )

    def test_legacy_feedback_conflict_assignment_block_is_requeued_once(
        self,
    ) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        self.lifecycle.transition(run_id, RunState.RESOLVING_FEEDBACK)
        now = "2026-01-01T01:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES ('conflict-member', 'team-1', 'conflict-owner',
                           'implementer', 'conflict owner',
                           'Resolve repository conflicts',
                           '["read","write","run","git_diff"]',
                           'mini-swe-agent', 'configured', '')"""
            )
            connection.execute(
                """INSERT INTO agent_assignments
                   (id, run_id, team_member_id, reasoning, assigned_at)
                   VALUES ('lead-assignment', ?, 'lead-1',
                           'Original issue assignment', ?)""",
                (run_id, now),
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pull-1', ?, 'PR1', 6, 'pull-url',
                           'agent/issue-3-run-1', 'main', ?, ?, ?, 'open', ?, ?)""",
                (run_id, "a" * 40, "b" * 40, "b" * 40, now, now),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at,
                    decision_json)
                   VALUES ('conflict-feedback', 'pull-1', 'base_conflict',
                           'PR1', ?, 'repogents', 'merge current main',
                           'processing', ?, ?)""",
                (
                    f"{'b' * 40}:{'c' * 40}",
                    now,
                    json.dumps(
                        {
                            "action": "revise",
                            "reason": "The pull request conflicts with current main.",
                            "response": "Resolved the current base conflict.",
                        }
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state,
                    created_at)
                   VALUES ('revision-batch', ?, 'feedback_revision_batch',
                           'batch-key', ?, 'pending', ?)""",
                (
                    run_id,
                    json.dumps({"feedback_ids": ["conflict-feedback"]}),
                    now,
                ),
            )
        legacy_reason = (
            "The remaining fetched-base conflicts require repository writes, "
            "but the active stored lead is permitted only read/git_diff. "
            "Assignment to conflict-owner is now rejected because issue work "
            "already began, and no permitted controller action can resolve "
            "or validate the merge."
        )
        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason=legacy_reason,
        )

        self.assertEqual(
            self.lifecycle.reconcile_recoverable_blocked_runs(),
            (run_id,),
        )
        recovered = self.lifecycle.get_run(run_id)
        self.assertEqual(recovered["state"], RunState.RESOLVING_FEEDBACK.value)
        self.assertIn("stored-team expansion", recovered["reason"])
        with self.db.connect() as connection:
            durable = connection.execute(
                """SELECT feedback_versions.state AS feedback_state,
                          feedback_versions.source_sha,
                          outbound_operations.state AS operation_state,
                          (SELECT COUNT(*) FROM agent_assignments
                           WHERE run_id=?) AS assignment_count,
                          (SELECT COUNT(*) FROM run_transitions
                           WHERE run_id=? AND from_state='blocked'
                             AND to_state='resolving_feedback') AS recovery_count
                   FROM feedback_versions
                   JOIN outbound_operations
                     ON outbound_operations.run_id=?
                    AND outbound_operations.kind='feedback_revision_batch'
                   WHERE feedback_versions.id='conflict-feedback'""",
                (run_id, run_id, run_id),
            ).fetchone()
        self.assertEqual(durable["feedback_state"], "processing")
        self.assertIsNone(durable["source_sha"])
        self.assertEqual(durable["operation_state"], "pending")
        self.assertEqual(durable["assignment_count"], 1)
        self.assertEqual(durable["recovery_count"], 1)
        self.assertEqual(self.lifecycle.reconcile_recoverable_blocked_runs(), ())
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO agent_assignments
                   (id, run_id, team_member_id, reasoning, assigned_at)
                   VALUES ('conflict-assignment', ?, 'conflict-member',
                           'Expanded conflict assignment', ?)""",
                (run_id, now),
            )
        handoff_reason = (
            "The recorded next action is to restore unrelated paths while "
            "preserving the issue changes, but the stored lead cannot run or "
            "write. Every stored implementation member is already selected, "
            "so the assignment cannot be expanded through a strict superset, "
            "and no controller action exists to execute a repository mutation "
            "as an already-assigned member."
        )
        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason=handoff_reason,
        )

        self.assertEqual(
            self.lifecycle.reconcile_recoverable_blocked_runs(),
            (run_id,),
        )
        recovered = self.lifecycle.get_run(run_id)
        self.assertEqual(recovered["state"], RunState.RESOLVING_FEEDBACK.value)
        self.assertIn("assigned-member handoff", recovered["reason"])
        with self.db.connect() as connection:
            durable = connection.execute(
                """SELECT feedback_versions.state AS feedback_state,
                          feedback_versions.source_sha,
                          outbound_operations.state AS operation_state,
                          (SELECT COUNT(*) FROM agent_assignments
                           WHERE run_id=?) AS assignment_count,
                          (SELECT COUNT(*) FROM run_transitions
                           WHERE run_id=? AND from_state='blocked'
                             AND to_state='resolving_feedback'
                             AND reason LIKE
                                 '%assigned-member handoff%') AS recovery_count
                   FROM feedback_versions
                   JOIN outbound_operations
                     ON outbound_operations.run_id=?
                    AND outbound_operations.kind='feedback_revision_batch'
                   WHERE feedback_versions.id='conflict-feedback'""",
                (run_id, run_id, run_id),
            ).fetchone()
        self.assertEqual(durable["feedback_state"], "processing")
        self.assertIsNone(durable["source_sha"])
        self.assertEqual(durable["operation_state"], "pending")
        self.assertEqual(durable["assignment_count"], 2)
        self.assertEqual(durable["recovery_count"], 1)

        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason=handoff_reason,
        )
        self.assertEqual(self.lifecycle.reconcile_recoverable_blocked_runs(), ())
        self.assertEqual(
            self.lifecycle.get_run(run_id)["state"],
            RunState.BLOCKED.value,
        )

        validation_base_reason = (
            "The issue-scoped candidate is complete, but required validation "
            "cannot pass: strict validation flags source code that is inherited "
            "unchanged from the controller-required fetched base with zero "
            "base-to-HEAD diff."
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET reason=? WHERE id=? AND state='blocked'",
                (validation_base_reason, run_id),
            )
        self.assertEqual(
            self.lifecycle.reconcile_recoverable_blocked_runs(),
            (run_id,),
        )
        recovered = self.lifecycle.get_run(run_id)
        self.assertEqual(recovered["state"], RunState.RESOLVING_FEEDBACK.value)
        self.assertIn("prepared base", recovered["reason"])
        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason=validation_base_reason,
        )
        self.assertEqual(self.lifecycle.reconcile_recoverable_blocked_runs(), ())


        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs SET reason=?
                   WHERE id=? AND state='blocked'""",
                ("irreducible external dependency is unavailable", run_id),
            )
        self.assertEqual(self.lifecycle.reconcile_recoverable_blocked_runs(), ())
        self.assertEqual(
            self.lifecycle.get_run(run_id)["state"],
            RunState.BLOCKED.value,
        )

    def test_classified_irreducible_acceptance_block_is_not_requeued(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id=?",
                ("d" * 40, run_id),
            )
            connection.execute(
                """INSERT INTO acceptance_verifications
                   (id, run_id, commit_sha, attempt, verifier_member_id, state,
                    claims_json, screenshot_decision_json, report_json,
                    started_at, completed_at)
                   VALUES ('verification-irreducible', ?, ?, 1, 'lead-1',
                           'blocked', '[]', ?, ?, ?, ?)""",
                (
                    run_id,
                    "d" * 40,
                    json.dumps({"required": True}),
                    json.dumps(
                        {
                            "state": "blocked",
                            "summary": "The controller-owned browser is unavailable.",
                            "claims": [{"result": "fail"}],
                            "limitations": ["No browser executable is available."],
                            "blocker": {
                                "kind": "irreducible",
                                "reason": "No browser executable is available.",
                            },
                        }
                    ),
                    "2026-01-01T01:00:00Z",
                    "2026-01-01T01:01:00Z",
                ),
            )
        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason=(
                "publication blocked: issue acceptance verification blocked: "
                "controller-owned browser unavailable"
            ),
        )

        self.assertEqual(
            self.lifecycle.reconcile_recoverable_blocked_runs(),
            (),
        )
        self.assertEqual(
            self.lifecycle.get_run(run_id)["state"],
            RunState.BLOCKED.value,
        )

    def test_cancel_is_durable_before_signaling_and_rejects_transition_race(
        self,
    ) -> None:
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
                "SELECT to_state FROM run_transitions WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        self.assertEqual(
            [row["to_state"] for row in transitions][-1], RunState.CANCELED.value
        )

    def test_reconcile_recreates_missing_run_storage_without_new_identity(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        record = self.lifecycle.get_run(run_id)
        marker = Path(record["checkout_path"]) / ".base-sha"
        marker.unlink()
        reconciled = self.lifecycle.reconcile_nonterminal_runs()
        self.assertEqual(reconciled, (run_id,))
        self.assertTrue(marker.exists())
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1
            )

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
