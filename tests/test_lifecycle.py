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

    def list_ready_issues(self, owner: str, name: str) -> tuple[IssueInfo, ...]:
        return tuple(event.issue for event in self.events)

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

    def get_repository_merge_method(self, owner: str, name: str) -> str:
        return "merge"


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

    def test_issue_revision_discovery_waits_for_same_repository_lane(self) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason="waiting for corrected issue requirements",
        )
        now = "2026-01-01T00:01:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-2', 'repo-1', 'I4', 4, 'issue-4-url',
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
                           'sandbox-1', 'team-1', 'main', ?, 'validating',
                           '/tmp/run-2/checkout', '/tmp/run-2', ?, ?)""",
                ("b" * 40, now, now),
            )
        self.github.current_issue = IssueInfo(
            node_id="I3",
            number=3,
            url="https://github.com/owner/repo/issues/3",
            title="Corrected issue",
            body="Use the corrected durable behavior.",
            discussion=(),
            updated_at="2026-01-02T00:00:00Z",
        )

        self.assertTrue(self.lifecycle.poll_issue_revision(run_id))

        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT state, resume_state FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            versions = connection.execute(
                """SELECT COUNT(*) FROM issue_versions
                   WHERE issue_id=(SELECT issue_id FROM runs WHERE id=?)""",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(tuple(run), ("queued", "implementing"))
        self.assertEqual(versions, 2)

    def test_preempted_lane_owner_resumes_only_after_sibling_is_idle(
        self,
    ) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        now = "2026-01-01T00:01:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-2', 'repo-1', 'I4', 4, 'issue-4-url',
                           'Forced issue', 'Body', '[]', ?)""",
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
                           'sandbox-1', 'team-1', 'main', ?, 'queued',
                           '/tmp/run-2/checkout', '/tmp/run-2', ?, ?)""",
                ("b" * 40, now, now),
            )

        self.assertEqual(
            self.lifecycle.suspend_for_preemption(run_id),
            RunState.IMPLEMENTING.value,
        )
        self.assertTrue(self.lifecycle.activate_queued("run-2"))
        self.assertIsNone(self.lifecycle.resume_suspended(run_id))
        with self.db.connect() as connection:
            occupied = connection.execute(
                """SELECT id, state, resume_state, checkout_path
                   FROM runs ORDER BY id"""
            ).fetchall()
        self.assertEqual(
            [(row["id"], row["state"], row["resume_state"]) for row in occupied],
            [
                (run_id, "queued", "implementing"),
                ("run-2", "implementing", None),
            ],
        )
        self.assertTrue(all(row["checkout_path"] for row in occupied))

        self.lifecycle.transition("run-2", RunState.VALIDATING)
        self.lifecycle.transition("run-2", RunState.PUBLISHING)
        self.lifecycle.transition("run-2", RunState.WAITING_FOR_FEEDBACK)
        self.assertEqual(
            self.lifecycle.resume_suspended(run_id),
            RunState.IMPLEMENTING.value,
        )
        with self.db.connect() as connection:
            resumed = connection.execute(
                "SELECT state, resume_state FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(tuple(resumed), ("implementing", None))

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

    def test_retry_blocked_run_restores_the_state_that_entered_block(self) -> None:
        paths = (
            (RunState.QUEUED, ()),
            (RunState.IMPLEMENTING, (RunState.IMPLEMENTING,)),
            (
                RunState.VALIDATING,
                (RunState.IMPLEMENTING, RunState.VALIDATING),
            ),
            (
                RunState.PUBLISHING,
                (
                    RunState.IMPLEMENTING,
                    RunState.VALIDATING,
                    RunState.PUBLISHING,
                ),
            ),
            (
                RunState.WAITING_FOR_FEEDBACK,
                (
                    RunState.IMPLEMENTING,
                    RunState.VALIDATING,
                    RunState.PUBLISHING,
                    RunState.WAITING_FOR_FEEDBACK,
                ),
            ),
            (
                RunState.RESOLVING_FEEDBACK,
                (
                    RunState.IMPLEMENTING,
                    RunState.VALIDATING,
                    RunState.PUBLISHING,
                    RunState.WAITING_FOR_FEEDBACK,
                    RunState.RESOLVING_FEEDBACK,
                ),
            ),
        )
        run_ids: list[str] = []
        for index, (expected, path) in enumerate(paths, start=1):
            if index > 1:
                self.github.events.append(
                    ActivationEvent(
                        event_id=f"retry-event-{index}",
                        applied_at=f"2026-01-{index + 1:02d}T00:00:00Z",
                        issue=self.event.issue,
                    )
                )
            run_id = self.lifecycle.poll_repository("repo-1")[0]
            run_ids.append(run_id)
            for state in path:
                self.lifecycle.transition(run_id, state)
            self.lifecycle.transition(
                run_id,
                RunState.BLOCKED,
                reason=f"recoverable failure from {expected.value}",
            )

            resumed = self.lifecycle.retry(run_id)

            self.assertEqual(resumed, expected.value)
            record = self.lifecycle.get_run(run_id)
            self.assertEqual(record["state"], expected.value)
            self.assertEqual(record["reason"], "retry requested by user")
            self.lifecycle.cancel(run_id, "fixture complete")

        with self.assertRaisesRegex(ValueError, "no retry is pending"):
            self.lifecycle.retry(run_ids[-1])

    def test_retry_pending_automatic_failure_clears_delay_without_state_change(
        self,
    ) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET retry_attempt_count=2,
                       retry_next_at='2099-01-01T00:00:00Z',
                       retry_last_error='temporary network failure'
                   WHERE id=?""",
                (run_id,),
            )

        resumed = self.lifecycle.retry(run_id)

        self.assertEqual(resumed, "implementing")
        run = self.lifecycle.get_run(run_id)
        self.assertEqual(run["state"], "implementing")
        self.assertEqual(run["retry_attempt_count"], 2)
        self.assertIsNone(run["retry_next_at"])
        self.assertEqual(run["retry_last_error"], "temporary network failure")
        with self.db.connect() as connection:
            transition = connection.execute(
                """SELECT from_state, to_state, reason
                   FROM run_transitions WHERE run_id=? ORDER BY id DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        self.assertEqual(
            tuple(transition),
            ("implementing", "implementing", "retry requested by user"),
        )
        self.lifecycle.cancel(run_id, "fixture complete")
        terminal = self.lifecycle.get_run(run_id)
        self.assertEqual(terminal["retry_attempt_count"], 0)
        self.assertIsNone(terminal["retry_next_at"])
        self.assertIsNone(terminal["retry_last_error"])

    def test_restart_canceled_run_is_idempotent_and_uses_current_versions(self) -> None:
        source_run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.cancel(source_run_id, "operator canceled")
        sandbox_root = self.data_root / "repositories" / "repo-1" / "sandbox" / "2"
        sandbox_root.mkdir(parents=True)
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json,
                    evidence_json, created_at)
                   VALUES ('sandbox-2', 'repo-1', 2, ?, '{}', '{}',
                           '2026-01-02T00:00:00Z')""",
                (str(sandbox_root),),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-2', 'repo-1', 2, '{}',
                           '2026-01-02T00:00:00Z')"""
            )
            connection.execute(
                """UPDATE repositories
                   SET current_sandbox_version_id='sandbox-2',
                       current_team_version_id='team-2'
                   WHERE id='repo-1'"""
            )
        self.github.current_issue = IssueInfo(
            node_id=self.event.issue.node_id,
            number=self.event.issue.number,
            url=self.event.issue.url,
            title="Current issue title",
            body="Current requirements",
            discussion=self.event.issue.discussion,
            updated_at="2026-01-02T00:00:00Z",
            state="open",
        )
        self.github.base_sha = "c" * 40

        replacement_run_id = self.lifecycle.restart(source_run_id)
        repeated_run_id = self.lifecycle.restart(source_run_id)

        self.assertEqual(repeated_run_id, replacement_run_id)
        self.assertNotEqual(replacement_run_id, source_run_id)
        with self.db.connect() as connection:
            source = connection.execute(
                "SELECT state, reason FROM runs WHERE id=?", (source_run_id,)
            ).fetchone()
            replacement = connection.execute(
                """SELECT runs.*, issues.title, activation_events.github_event_id
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN activation_events
                     ON activation_events.id=runs.activation_event_id
                   WHERE runs.id=?""",
                (replacement_run_id,),
            ).fetchone()
            run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        self.assertEqual(tuple(source), ("canceled", "operator canceled"))
        self.assertEqual(replacement["state"], "queued")
        self.assertEqual(replacement["sandbox_version_id"], "sandbox-2")
        self.assertEqual(replacement["team_version_id"], "team-2")
        self.assertEqual(replacement["base_sha"], "c" * 40)
        self.assertEqual(replacement["title"], "Current issue title")
        self.assertEqual(
            replacement["github_event_id"],
            f"manual-restart:{source_run_id}",
        )
        self.assertEqual(run_count, 2)
        self.assertEqual(len(self.checkouts.calls), 2)
        with self.assertRaisesRegex(ValueError, "only canceled runs can be restarted"):
            self.lifecycle.restart(replacement_run_id)

    def test_restart_rejects_closed_github_issue_without_creating_a_run(self) -> None:
        source_run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.cancel(source_run_id, "operator canceled")
        self.github.current_issue = IssueInfo(
            node_id=self.event.issue.node_id,
            number=self.event.issue.number,
            url=self.event.issue.url,
            title=self.event.issue.title,
            body=self.event.issue.body,
            discussion=self.event.issue.discussion,
            updated_at="2026-01-02T00:00:00Z",
            state="closed",
        )

        with self.assertRaisesRegex(ValueError, "GitHub issue is not open"):
            self.lifecycle.restart(source_run_id)

        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                1,
            )

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

    def test_legacy_validation_block_recovers_after_conflict_replacement(
        self,
    ) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        self.lifecycle.transition(run_id, RunState.RESOLVING_FEEDBACK)
        now = "2026-01-01T01:00:00Z"
        current_feedback_id = "current-conflict-feedback"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES ('implementation-member', 'team-1', 'implementation',
                           'implementer', 'source maintainer',
                           'Implement source revisions',
                           '["read","write","run","git_diff"]',
                           'mini-swe-agent', 'configured', '')"""
            )
            connection.execute(
                """INSERT INTO agent_assignments
                   (id, run_id, team_member_id, reasoning, assigned_at)
                   VALUES ('implementation-assignment', ?,
                           'implementation-member',
                           'Implement the feedback revision', ?)""",
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
                    github_version, author, body, state, observed_at)
                   VALUES (?, 'pull-1', 'base_conflict', 'PR1-current', ?,
                           'repogents', 'merge newest main', 'pending', ?)""",
                (
                    current_feedback_id,
                    f"{'b' * 40}:{'d' * 40}",
                    "2026-01-01T01:01:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at,
                    decision_json, superseded_at, superseded_by_feedback_id)
                   VALUES ('prior-conflict-feedback', 'pull-1',
                           'base_conflict', 'PR1-prior', ?, 'repogents',
                           'merge prior main', 'resolved', ?, ?, ?, ?)""",
                (
                    f"{'b' * 40}:{'c' * 40}",
                    now,
                    json.dumps(
                        {
                            "action": "revise",
                            "reason": "The pull request conflicts with prior main.",
                            "response": "Resolve the prior base conflict.",
                        }
                    ),
                    "2026-01-01T01:01:00Z",
                    current_feedback_id,
                ),
            )
            connection.execute(
                """INSERT INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state,
                    external_id, created_at, completed_at)
                   VALUES ('prior-revision-batch', ?,
                           'feedback_revision_batch', 'prior-batch', ?,
                           'reconciled', ?, ?, ?)""",
                (
                    run_id,
                    json.dumps({"feedback_ids": ["prior-conflict-feedback"]}),
                    current_feedback_id,
                    now,
                    "2026-01-01T01:01:00Z",
                ),
            )
        reason = (
            "The issue-scoped candidate is complete, but required validation "
            "cannot pass: strict validation flags source code that is inherited "
            "unchanged from the controller-required fetched base with zero "
            "base-to-HEAD diff."
        )
        self.lifecycle.transition(run_id, RunState.BLOCKED, reason=reason)

        self.assertEqual(
            self.lifecycle.reconcile_recoverable_blocked_runs(),
            (run_id,),
        )
        recovered = self.lifecycle.get_run(run_id)
        self.assertEqual(recovered["state"], RunState.RESOLVING_FEEDBACK.value)
        self.assertIn("prepared base", recovered["reason"])
        self.assertEqual(self.lifecycle.reconcile_recoverable_blocked_runs(), ())

        post_replay_reason = (
            "Required controller validation cannot pass without out-of-scope "
            "changes: repogents/execution.py is reported as adding broad "
            "source suppression, but it has zero diff from the fetched "
            "conflict base dddff524770d8d3bfcc856009996a71fff9a9699 to clean "
            "candidate e7ea7f5, so removing it would roll back inherited base "
            "behavior. The full suite also has two irreducible "
            "restricted-proxy failures because the sandbox returns 403 for "
            "api.github.com; weakening proxy policy is not valid. "
            "Issue-specific compileall, 28 app tests, 8 interface tests, and "
            "git diff --check pass, and the requested feedback is implemented, "
            "but strict required validation remains externally blocked."
        )
        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason=post_replay_reason,
        )
        self.assertEqual(
            self.lifecycle.reconcile_recoverable_blocked_runs(),
            (run_id,),
        )
        recovered = self.lifecycle.get_run(run_id)
        self.assertEqual(recovered["state"], RunState.RESOLVING_FEEDBACK.value)
        self.assertIn("without agent replay", recovered["reason"])
        self.lifecycle.transition(
            run_id,
            RunState.BLOCKED,
            reason=post_replay_reason,
        )
        self.assertEqual(self.lifecycle.reconcile_recoverable_blocked_runs(), ())

    def test_legacy_publication_merge_base_block_is_requeued_once(
        self,
    ) -> None:
        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        validated_sha = "d" * 40
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id=?",
                (validated_sha, run_id),
            )
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        blocker = (
            "publication blocked: validated commit has a merge conflict with "
            "the current intended-base head"
        )
        self.lifecycle.transition(run_id, RunState.BLOCKED, reason=blocker)

        self.assertEqual(
            self.lifecycle.reconcile_recoverable_blocked_runs(),
            (run_id,),
        )
        recovered = self.lifecycle.get_run(run_id)
        self.assertEqual(recovered["state"], RunState.PUBLISHING.value)
        self.assertEqual(recovered["validated_sha"], validated_sha)
        self.assertIn("candidate/current merge base", recovered["reason"])

        self.lifecycle.transition(run_id, RunState.BLOCKED, reason=blocker)
        self.assertEqual(self.lifecycle.reconcile_recoverable_blocked_runs(), ())
        self.assertEqual(
            self.lifecycle.get_run(run_id)["state"],
            RunState.BLOCKED.value,
        )
        with self.db.connect() as connection:
            recovery_count = connection.execute(
                """SELECT COUNT(*) FROM run_transitions
                   WHERE run_id=? AND from_state='blocked'
                     AND to_state='publishing'
                     AND reason LIKE '%candidate/current merge base%'""",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(recovery_count, 1)

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

    def test_ready_issue_discovery_becomes_stale_when_onboarding_is_not_ready(
        self,
    ) -> None:
        self.lifecycle.poll_repository("repo-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET onboarding_state='blocked' WHERE id='repo-1'"
            )

        self.assertEqual(self.lifecycle.poll_repository("repo-1"), ())

        with self.db.connect() as connection:
            discovery = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(discovery["status"], "stale")
        self.assertIn('"number": 3', discovery["issues_json"])
        self.assertIsNotNone(discovery["last_success_at"])
        self.assertEqual(discovery["error"], "Repository onboarding is blocked")

    def test_ready_issue_discovery_stays_stale_across_pause_and_resume_until_success(
        self,
    ) -> None:
        from repogents.github import GitHubError

        self.lifecycle.poll_repository("repo-1")
        paused_runs = self.lifecycle.set_repository_paused("repo-1", True)
        self.assertEqual(len(paused_runs), 1)
        with self.db.connect() as connection:
            paused = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(paused["status"], "stale")
        self.assertIn('\"number\": 3', paused["issues_json"])

        resumed_runs = self.lifecycle.set_repository_paused("repo-1", False)
        self.assertEqual(resumed_runs, paused_runs)
        with self.db.connect() as connection:
            resumed = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(resumed["status"], "stale")

        class FailingDiscoveryClient(FakeActivationClient):
            def list_ready_issues(self, owner: str, name: str) -> tuple[IssueInfo, ...]:
                raise GitHubError("server unavailable")

        failing = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=FailingDiscoveryClient([]),
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )
        self.assertEqual(failing.poll_repository("repo-1"), ())
        with self.db.connect() as connection:
            failed = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(failed["status"], "stale")
        self.assertIn('\"number\": 3', failed["issues_json"])

        class RefreshedDiscoveryClient(FakeActivationClient):
            def list_ready_issues(self, owner: str, name: str) -> tuple[IssueInfo, ...]:
                return (
                    IssueInfo(
                        number=4,
                        title="Newly ready after resume",
                        url="https://github.com/octo/example/issues/4",
                        updated_at="2025-01-02T00:00:00Z",
                        node_id="issue-node-4",
                        body="",
                        discussion=(),
                    ),
                )

        refreshed_lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=RefreshedDiscoveryClient([]),
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )
        refreshed_lifecycle.poll_repository("repo-1")
        with self.db.connect() as connection:
            refreshed = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(refreshed["status"], "available")
        self.assertNotIn('\"number\": 3', refreshed["issues_json"])
        self.assertIn('\"number\": 4', refreshed["issues_json"])
        self.assertIsNone(refreshed["error"])

    def test_inflight_ready_issue_success_stays_stale_across_pause_and_resume(
        self,
    ) -> None:
        import threading

        def issue(number: int, title: str) -> IssueInfo:
            return IssueInfo(
                number=number,
                title=title,
                url=f"https://github.com/octo/example/issues/{number}",
                updated_at=f"2025-01-{number:02d}T00:00:00Z",
                node_id=f"issue-node-{number}",
                body="",
                discussion=(),
            )

        class DiscoveryClient(FakeActivationClient):
            def __init__(self, discovered: IssueInfo) -> None:
                super().__init__([])
                self.discovered = discovered

            def list_ready_issues(
                self, owner: str, name: str
            ) -> tuple[IssueInfo, ...]:
                return (self.discovered,)

        initial = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=DiscoveryClient(issue(3, "Ready before pause")),
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )
        self.assertEqual(initial.poll_repository("repo-1"), ())

        lookup_started = threading.Event()
        release_lookup = threading.Event()

        class BlockingDiscoveryClient(FakeActivationClient):
            def list_ready_issues(
                self, owner: str, name: str
            ) -> tuple[IssueInfo, ...]:
                lookup_started.set()
                if not release_lookup.wait(timeout=5):
                    raise AssertionError("timed out waiting to release discovery")
                return (issue(4, "Pre-pause in-flight snapshot"),)

        racing = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=BlockingDiscoveryClient([]),
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )
        errors: list[BaseException] = []

        def poll() -> None:
            try:
                racing.poll_repository("repo-1")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        poll_thread = threading.Thread(target=poll)
        poll_thread.start()
        self.assertTrue(lookup_started.wait(timeout=5))
        self.lifecycle.set_repository_paused("repo-1", True)
        release_lookup.set()
        poll_thread.join(timeout=5)
        self.assertFalse(poll_thread.is_alive())
        self.assertEqual(errors, [])

        with self.db.connect() as connection:
            raced = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(raced["status"], "stale")
        self.assertIn('\"number\": 3', raced["issues_json"])
        self.assertNotIn('\"number\": 4', raced["issues_json"])

        self.lifecycle.set_repository_paused("repo-1", False)
        with self.db.connect() as connection:
            resumed = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(resumed["status"], "stale")
        self.assertIn('\"number\": 3', resumed["issues_json"])

        refreshed = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=DiscoveryClient(issue(5, "Ready after resume")),
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )
        self.assertEqual(refreshed.poll_repository("repo-1"), ())
        with self.db.connect() as connection:
            current = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id='repo-1'"
            ).fetchone()
        self.assertEqual(current["status"], "available")
        self.assertNotIn('\"number\": 3', current["issues_json"])
        self.assertIn('\"number\": 5', current["issues_json"])

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


    def test_automatic_merge_waits_for_fresh_verified_idle_state_and_confirmation(self) -> None:
        from repogents.github import PullRequestInfo

        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        head_sha = "c" * 40
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET automatic_merge_idle_seconds=60 WHERE id='repo-1'"
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('automatic-pull', ?, 'PR-auto', 8, 'pull-url',
                           'agent/issue-3-auto', 'main', ?, ?, ?, 'open', ?, ?)""",
                (
                    run_id,
                    "b" * 40,
                    head_sha,
                    head_sha,
                    "2026-01-01T00:00:00.000000Z",
                    "2026-01-01T00:00:00.000000Z",
                ),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at)
                   VALUES ('automatic-feedback', 'automatic-pull', 'comment',
                           'comment-1', '2026-01-01T00:00:30.000000Z',
                           'reviewer', 'please verify', 'pending',
                           '2026-01-01T00:00:30.000000Z')"""
            )

        pulls = [
            PullRequestInfo(
                node_id="PR-auto", number=8, url="pull-url", state="open",
                merged=False, head_branch="unexpected-branch", head_sha=head_sha,
                base_branch="main", updated_at="2026-01-01T00:01:30.000000Z",
                head_commit_at="2026-01-01T00:00:00.000000Z",
            ),
            PullRequestInfo(
                node_id="PR-auto", number=8, url="pull-url", state="open",
                merged=False, head_branch="agent/issue-3-auto", head_sha=head_sha,
                base_branch="main", updated_at="2026-01-01T00:01:30.000000Z",
                head_commit_at="2026-01-01T00:00:00.000000Z",
            ),
            PullRequestInfo(
                node_id="PR-auto", number=8, url="pull-url", state="open",
                merged=False, head_branch="agent/issue-3-auto", head_sha=head_sha,
                base_branch="main", updated_at="2026-01-01T00:01:31.000000Z",
                head_commit_at="2026-01-01T00:00:00.000000Z",
            ),
            PullRequestInfo(
                node_id="PR-auto", number=8, url="pull-url", state="closed",
                merged=True, head_branch="agent/issue-3-auto", head_sha=head_sha,
                base_branch="main", updated_at="2026-01-01T00:01:32.000000Z",
                head_commit_at="2026-01-01T00:00:00.000000Z",
            ),
        ]
        merge_calls: list[tuple[str, str, int, str]] = []
        self.github.get_pull_request = lambda owner, name, number: pulls.pop(0)
        self.github.merge_pull_request = lambda owner, name, number, expected, method: (
            merge_calls.append((owner, name, number, expected))
            or {"merged": True, "message": "accepted", "sha": expected}
        )

        with patch(
            "repogents.lifecycle._utc_now",
            side_effect=[
                "2026-01-01T00:01:30.000000Z",
                "2026-01-01T00:01:30.000000Z",
                "2026-01-01T00:01:31.000000Z",
                "2026-01-01T00:01:32.000000Z",
                "2026-01-01T00:01:32.000001Z",
            ],
        ):
            self.lifecycle.reconcile_automatic_merge(run_id)
            with self.db.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM automatic_merge_eligibility"
                    ).fetchone()[0],
                    0,
                )
            self.lifecycle.reconcile_automatic_merge(run_id)
            self.assertEqual(merge_calls, [])
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE feedback_versions SET state='resolved' WHERE id='automatic-feedback'"
                )
            self.lifecycle.reconcile_automatic_merge(run_id)
            self.assertEqual(merge_calls, [("owner", "repo", 8, head_sha)])
            self.assertEqual(
                self.lifecycle.get_run(run_id)["state"],
                RunState.WAITING_FOR_FEEDBACK.value,
            )
            self.lifecycle.reconcile_automatic_merge(run_id)

        with self.db.connect() as connection:
            operation = connection.execute(
                "SELECT state, expected_head_sha FROM automatic_merge_operations"
            ).fetchone()
            eligibility = connection.execute(
                "SELECT state, activity_anchor_at, deadline_at FROM automatic_merge_eligibility"
            ).fetchone()
        self.assertEqual(operation["state"], "confirmed")
        self.assertEqual(operation["expected_head_sha"], head_sha)
        self.assertEqual(eligibility["state"], "completed")
        self.assertEqual(eligibility["activity_anchor_at"], "2026-01-01T00:00:30.000000Z")
        self.assertEqual(eligibility["deadline_at"], "2026-01-01T00:01:30.000000Z")
        self.assertEqual(self.lifecycle.get_run(run_id)["state"], RunState.CLOSED.value)
        self.assertEqual(len(merge_calls), 1)

    def test_automatic_merge_fractional_boundary_and_new_comment_reset(self) -> None:
        from repogents.github import PullRequestInfo

        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        head_sha = "e" * 40
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET automatic_merge_idle_seconds=60 WHERE id='repo-1'"
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('fractional-pull', ?, 'PR-fractional', 10, 'pull-url',
                           'agent/issue-3-fractional', 'main', ?, ?, ?, 'open', ?, ?)""",
                (run_id, "b" * 40, head_sha, head_sha,
                 "2026-01-01T00:00:00.250000Z",
                 "2026-01-01T00:00:00.250000Z"),
            )

        pull = PullRequestInfo(
            node_id="PR-fractional", number=10, url="pull-url", state="open",
            merged=False, head_branch="agent/issue-3-fractional", head_sha=head_sha,
            base_branch="main", updated_at="2026-01-01T00:01:30.500000Z",
            head_commit_at="2026-01-01T00:00:00.250000Z",
        )
        merge_calls: list[str] = []
        self.github.get_pull_request = lambda owner, name, number: pull
        self.github.merge_pull_request = lambda owner, name, number, expected, method: (
            merge_calls.append(expected) or {"merged": True, "sha": expected}
        )

        with patch(
            "repogents.lifecycle._utc_now",
            side_effect=[
                "2026-01-01T00:01:00.249999Z",
                "2026-01-01T00:01:00.250000Z",
                "2026-01-01T00:01:30.499999Z",
                "2026-01-01T00:01:30.500000Z",
            ],
        ):
            self.lifecycle.reconcile_automatic_merge(run_id)
            self.assertEqual(merge_calls, [])
            with self.db.transaction() as connection:
                connection.execute(
                    """INSERT INTO feedback_versions
                       (id, pull_request_id, feedback_type, github_object_id,
                        github_version, author, body, state, observed_at)
                       VALUES ('fractional-comment', 'fractional-pull', 'comment',
                               'comment-fractional', '2026-01-01T00:00:30.500000Z',
                               'reviewer', 'later activity', 'resolved',
                               '2026-01-01T00:00:30.500000Z')"""
                )
            self.lifecycle.reconcile_automatic_merge(run_id)
            self.assertEqual(merge_calls, [])
            self.lifecycle.reconcile_automatic_merge(run_id)
            self.assertEqual(merge_calls, [])
            self.lifecycle.reconcile_automatic_merge(run_id)

        with self.db.connect() as connection:
            generations = connection.execute(
                """SELECT activity_anchor_at, deadline_at, state
                   FROM automatic_merge_eligibility
                   WHERE pull_request_id='fractional-pull'
                   ORDER BY created_at"""
            ).fetchall()
        self.assertEqual(len(generations), 2)
        self.assertEqual(generations[0]["activity_anchor_at"], "2026-01-01T00:00:00.250000Z")
        self.assertEqual(generations[0]["deadline_at"], "2026-01-01T00:01:00.250000Z")
        self.assertEqual(generations[0]["state"], "superseded")
        self.assertEqual(generations[1]["activity_anchor_at"], "2026-01-01T00:00:30.500000Z")
        self.assertEqual(generations[1]["deadline_at"], "2026-01-01T00:01:30.500000Z")
        self.assertEqual(merge_calls, [head_sha])

    def test_automatic_merge_uses_composite_review_submission_as_idle_anchor(self) -> None:
        from repogents.github import PullRequestInfo

        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        head_sha = "c" * 40
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET automatic_merge_idle_seconds=60 WHERE id='repo-1'"
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('review-anchor-pull', ?, 'PR-review-anchor', 11, 'pull-url',
                           'agent/issue-3-review-anchor', 'main', ?, ?, ?, 'open', ?, ?)""",
                (run_id, "b" * 40, head_sha, head_sha,
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at)
                   VALUES ('review-anchor-feedback', 'review-anchor-pull', 'review',
                           'review-11',
                           '2026-01-01T00:00:30.123456Z:deadbeef:COMMENTED',
                           'reviewer', 'review activity', 'resolved',
                           '2026-01-01T00:00:30.123456Z')"""
            )

        pull = PullRequestInfo(
            node_id="PR-review-anchor", number=11, url="pull-url", state="open",
            merged=False, head_branch="agent/issue-3-review-anchor", head_sha=head_sha,
            base_branch="main", updated_at="2026-01-01T00:01:30.123456Z",
            head_commit_at="2026-01-01T00:00:00.250000Z",
        )
        attempts: list[str] = []
        self.github.get_pull_request = lambda owner, name, number: pull
        self.github.merge_pull_request = lambda owner, name, number, expected, method: (
            attempts.append(expected) or {"merged": True, "sha": expected}
        )

        with patch(
            "repogents.lifecycle._utc_now",
            return_value="2026-01-01T00:01:30.123455Z",
        ):
            self.lifecycle.reconcile_automatic_merge(run_id)
        self.assertEqual(attempts, [])
        with self.db.connect() as connection:
            eligibility = connection.execute(
                """SELECT activity_anchor_at, deadline_at
                   FROM automatic_merge_eligibility
                   WHERE pull_request_id='review-anchor-pull' AND state='active'"""
            ).fetchone()
        self.assertEqual(eligibility["activity_anchor_at"], "2026-01-01T00:00:30.123456Z")
        self.assertEqual(eligibility["deadline_at"], "2026-01-01T00:01:30.123456Z")

        with patch(
            "repogents.lifecycle._utc_now",
            return_value="2026-01-01T00:01:30.123456Z",
        ):
            self.lifecycle.reconcile_automatic_merge(run_id)
        self.assertEqual(attempts, [head_sha])

    def test_automatic_merge_duration_changes_replace_deadline_generation(self) -> None:
        from repogents.github import PullRequestInfo

        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        head_sha = "f" * 40
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET automatic_merge_idle_seconds=60 WHERE id='repo-1'"
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('duration-pull', ?, 'PR-duration', 12, 'pull-url',
                           'agent/issue-3-duration', 'main', ?, ?, ?, 'open', ?, ?)""",
                (run_id, "b" * 40, head_sha, head_sha,
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )

        pull = PullRequestInfo(
            node_id="PR-duration", number=12, url="pull-url", state="open",
            merged=False, head_branch="agent/issue-3-duration", head_sha=head_sha,
            base_branch="main", updated_at="2026-01-01T00:00:30Z",
            head_commit_at="2026-01-01T00:00:00.250000Z",
        )
        attempts: list[str] = []
        self.github.get_pull_request = lambda owner, name, number: pull
        self.github.merge_pull_request = lambda owner, name, number, expected, method: (
            attempts.append(expected) or {"merged": True, "sha": expected}
        )

        with patch("repogents.lifecycle._utc_now", return_value="2026-01-01T00:00:30Z"):
            self.lifecycle.reconcile_automatic_merge(run_id)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET automatic_merge_idle_seconds=120 WHERE id='repo-1'"
            )
        with patch("repogents.lifecycle._utc_now", return_value="2026-01-01T00:00:31Z"):
            self.lifecycle.reconcile_automatic_merge(run_id)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET automatic_merge_idle_seconds=10 WHERE id='repo-1'"
            )
        with patch(
            "repogents.lifecycle._utc_now",
            return_value="2026-01-01T00:00:10.250000Z",
        ):
            self.lifecycle.reconcile_automatic_merge(run_id)

        with self.db.connect() as connection:
            generations = connection.execute(
                """SELECT generation, activity_anchor_at, deadline_at, state
                   FROM automatic_merge_eligibility
                   WHERE pull_request_id='duration-pull' ORDER BY generation"""
            ).fetchall()
        self.assertEqual(
            [(row["generation"], row["deadline_at"], row["state"]) for row in generations],
            [
                (1, "2026-01-01T00:01:00.250000Z", "superseded"),
                (2, "2026-01-01T00:02:00.250000Z", "superseded"),
                (3, "2026-01-01T00:00:10.250000Z", "active"),
            ],
        )
        self.assertTrue(all(
            row["activity_anchor_at"] == "2026-01-01T00:00:00.250000Z"
            for row in generations
        ))
        self.assertEqual(attempts, [head_sha])

    def test_automatic_merge_ambiguous_delivery_reconciles_without_retry(self) -> None:
        from repogents.github import PullRequestInfo

        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        head_sha = "d" * 40
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET automatic_merge_idle_seconds=1 WHERE id='repo-1'"
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('ambiguous-pull', ?, 'PR-ambiguous', 9, 'pull-url',
                           'agent/issue-3-ambiguous', 'main', ?, ?, ?, 'open', ?, ?)""",
                (run_id, "b" * 40, head_sha, head_sha,
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )

        pull = PullRequestInfo(
            node_id="PR-ambiguous", number=9, url="pull-url", state="open",
            merged=False, head_branch="agent/issue-3-ambiguous", head_sha=head_sha,
            base_branch="main", updated_at="2026-01-01T00:00:02Z",
            head_commit_at="2026-01-01T00:00:00Z",
        )
        attempts: list[str] = []
        self.github.get_pull_request = lambda owner, name, number: pull

        def ambiguous_merge(
            owner: str, name: str, number: int, expected: str, method: str
        ) -> object:
            self.assertEqual(method, "merge")
            attempts.append(expected)
            raise RuntimeError("connection reset after request upload")

        self.github.merge_pull_request = ambiguous_merge
        with patch("repogents.lifecycle._utc_now", return_value="2026-01-01T00:00:02.000000Z"):
            self.lifecycle.reconcile_automatic_merge(run_id)
        restarted = RunLifecycle(
            database=Database(self.root / "db.sqlite3"),
            data_root=self.data_root,
            github=self.github,
            checkouts=self.checkouts,
            sandbox=self.sandbox,
        )
        restarted.database.initialize()
        with patch("repogents.lifecycle._utc_now", return_value="2026-01-01T00:00:03.000000Z"):
            restarted.reconcile_automatic_merge(run_id)

        with self.db.connect() as connection:
            operation = connection.execute(
                "SELECT state, requested_at, reconciled_at FROM automatic_merge_operations"
            ).fetchone()
        self.assertEqual(operation["state"], "reconciling")
        self.assertIsNotNone(operation["requested_at"] )
        self.assertIsNotNone(operation["reconciled_at"] )
        self.assertEqual(attempts, [head_sha])
        self.assertEqual(restarted.get_run(run_id)["state"], RunState.WAITING_FOR_FEEDBACK.value)

    def test_automatic_merge_definite_rejection_retries_after_fresh_verification(self) -> None:
        from repogents.github import PullRequestInfo, PullRequestMergeResult

        run_id = self.lifecycle.poll_repository("repo-1")[0]
        self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        self.lifecycle.transition(run_id, RunState.VALIDATING)
        self.lifecycle.transition(run_id, RunState.PUBLISHING)
        self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
        head_sha = "e" * 40
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET automatic_merge_idle_seconds=1 WHERE id='repo-1'"
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('rejected-pull', ?, 'PR-rejected', 10, 'pull-url',
                           'agent/issue-3-rejected', 'main', ?, ?, ?, 'open', ?, ?)""",
                (run_id, "b" * 40, head_sha, head_sha,
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )

        pull = PullRequestInfo(
            node_id="PR-rejected", number=10, url="pull-url", state="open",
            merged=False, head_branch="agent/issue-3-rejected", head_sha=head_sha,
            base_branch="main", updated_at="2026-01-01T00:00:02Z",
            head_commit_at="2026-01-01T00:00:00Z",
        )
        attempts: list[str] = []
        results = [
            PullRequestMergeResult(merged=False, message="head changed", sha=None),
            PullRequestMergeResult(merged=True, message="merged", sha=head_sha),
        ]
        self.github.get_pull_request = lambda owner, name, number: pull

        def merge(owner: str, name: str, number: int, expected: str, method: str) -> object:
            attempts.append(expected)
            return results.pop(0)

        self.github.merge_pull_request = merge
        with patch("repogents.lifecycle._utc_now", return_value="2026-01-01T00:00:02.000000Z"):
            self.lifecycle.reconcile_automatic_merge(run_id)
        with self.db.connect() as connection:
            first = connection.execute(
                "SELECT state, result_json FROM automatic_merge_operations"
            ).fetchone()
        self.assertEqual(first["state"], "rejected")
        self.assertEqual(
            json.loads(first["result_json"]),
            {"merged": False, "message": "head changed", "sha": None},
        )

        with patch("repogents.lifecycle._utc_now", return_value="2026-01-01T00:00:03.000000Z"):
            self.lifecycle.reconcile_automatic_merge(run_id)
        with self.db.connect() as connection:
            operations = connection.execute(
                "SELECT state, result_json FROM automatic_merge_operations ORDER BY created_at"
            ).fetchall()
            eligibilities = connection.execute(
                "SELECT generation, state FROM automatic_merge_eligibility ORDER BY generation"
            ).fetchall()
        self.assertEqual([row["state"] for row in operations], ["rejected", "reconciling"])
        self.assertEqual(
            json.loads(operations[1]["result_json"]),
            {"merged": True, "message": "merged", "sha": head_sha},
        )
        self.assertEqual(
            [(row["generation"], row["state"]) for row in eligibilities],
            [(1, "superseded"), (2, "active")],
        )
        self.assertEqual(attempts, [head_sha, head_sha])
        self.assertEqual(self.lifecycle.get_run(run_id)["state"], RunState.WAITING_FOR_FEEDBACK.value)


if __name__ == "__main__":
    unittest.main()
