from __future__ import annotations

import json
import subprocess
import threading
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from unittest import mock
from pathlib import Path

from repogents.database import Database
from repogents.github import PullRequestInfo
from repogents.lifecycle import RunLifecycle
from repogents.publication import (
    GitPublicationGateway,
    PublicationBaseChanged,
    MiniSweScopeReviewer,
    PublicationService,
    ScopeDecision,
)
from repogents.sandbox import SandboxManager


class NoActivationClient:
    def list_ready_events(self, owner: str, name: str) -> list[object]:
        return []

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        return "a" * 40


class NoCheckoutManager:
    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        return None


@dataclass
class FakeScopeReviewer:
    decision: ScopeDecision = ScopeDecision(
        True, "diff implements only the fixture issue"
    )
    reviews: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    issues: list[dict[str, object]] = field(default_factory=list)

    def review(
        self, issue: dict[str, object], diff: str, changed_files: tuple[str, ...]
    ) -> ScopeDecision:
        self.issues.append(issue)
        self.reviews.append((diff, changed_files))
        return self.decision


@dataclass
class FakeAcceptanceGate:
    state: str = "passed"
    summary: str = "The issue-required behavior was independently observed."
    observed: str | None = None
    issue_version_id: str = ""
    calls: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)

    def verify(
        self,
        run_id: str,
        commit_sha: str,
        changed_files: tuple[str, ...],
    ) -> dict[str, object]:
        self.calls.append((run_id, commit_sha, changed_files))
        return {
            "id": f"acceptance-{commit_sha}",
            "run_id": run_id,
            "commit_sha": commit_sha,
            "issue_version_id": self.issue_version_id,
            "state": self.state,
            "summary": self.summary,
            "claims": [
                {
                    "key": "value-output",
                    "claim": "The requested value is observable.",
                    "expected": "VALUE=2",
                    "method": "fixture command",
                    "result": "pass" if self.state == "passed" else "fail",
                    "observed": self.observed or self.summary,
                    "evidence": [1],
                }
            ],
            "scope": [
                {
                    "path": path,
                    "claim_keys": ["value-output"],
                    "necessity": "Implements or protects the issue behavior.",
                    "result": "pass",
                }
                for path in changed_files
            ],
            "screenshot_decision": {
                "required": False,
                "reason": "The fixture contract is nonvisual.",
            },
            "artifacts": [],
            "limitations": [],
        }


def passing_acceptance(issue_version_id: str) -> FakeAcceptanceGate:
    return FakeAcceptanceGate(issue_version_id=issue_version_id)


class FakePublicationGateway:
    def __init__(self) -> None:
        self.branches: dict[str, str] = {}
        self.pull_requests: list[PullRequestInfo] = []
        self.pushes: list[tuple[str, str]] = []
        self.push_leases: list[str | None] = []
        self.create_calls = 0
        self.pull_bodies: dict[int, str] = {}
        self.update_body_calls: list[tuple[int, str]] = []
        self.fail_before_push = False
        self.crash_after_push = False
        self.fail_before_create = False
        self.crash_after_create = False
        self.stale_pull_reads = 0
        self.intended_base_head: str | None = None
        self.after_remote_head_read: Callable[[], None] | None = None
        self.after_pull_read: Callable[[], None] | None = None
        self.before_push: Callable[[], None] | None = None

    def get_remote_branch_head(self, owner: str, name: str, branch: str) -> str | None:
        head = self.branches.get(branch)
        callback = self.after_remote_head_read
        self.after_remote_head_read = None
        if callback is not None:
            callback()
        return head

    def fetch_intended_base_head(
        self,
        checkout: Path,
        owner: str,
        name: str,
        branch: str,
    ) -> str:
        if self.intended_base_head is None:
            raise AssertionError("fixture intended-base head was not configured")
        return self.intended_base_head

    def push_branch(
        self,
        checkout: Path,
        owner: str,
        name: str,
        branch: str,
        sha: str,
        expected_remote_sha: str | None = None,
    ) -> None:
        self.push_leases.append(expected_remote_sha)
        if self.before_push is not None:
            self.before_push()
        if self.fail_before_push:
            self.fail_before_push = False
            raise RuntimeError("connection failed before push")
        if self.branches.get(branch) != expected_remote_sha:
            raise RuntimeError("remote branch changed before leased push")
        self.pushes.append((branch, sha))
        self.branches[branch] = sha
        if self.crash_after_push:
            self.crash_after_push = False
            raise RuntimeError("connection lost after GitHub accepted push")

    def find_pull_request(
        self, owner: str, name: str, branch: str
    ) -> PullRequestInfo | None:
        pull = next(
            (item for item in self.pull_requests if item.head_branch == branch),
            None,
        )
        if pull is None:
            result = None
        elif self.stale_pull_reads:
            self.stale_pull_reads -= 1
            result = pull
        else:
            remote_head = self.branches.get(branch)
            result = replace(pull, head_sha=remote_head) if remote_head else pull
        callback = self.after_pull_read
        self.after_pull_read = None
        if callback is not None:
            callback()
        return result

    def create_pull_request(
        self,
        owner: str,
        name: str,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestInfo:
        if self.fail_before_create:
            self.fail_before_create = False
            raise RuntimeError("connection failed before pull request creation")
        self.create_calls += 1
        pull = PullRequestInfo(
            node_id="PR1",
            number=11,
            url=f"https://github.com/{owner}/{name}/pull/11",
            state="open",
            merged=False,
            head_branch=branch,
            head_sha=self.branches[branch],
            base_branch=base,
            updated_at="2026-01-01T00:00:00Z",
        )
        self.pull_requests.append(pull)
        self.pull_bodies[pull.number] = body
        if self.crash_after_create:
            self.crash_after_create = False
            raise RuntimeError("connection lost after GitHub accepted pull request")
        return pull

    def update_pull_request_body(
        self,
        owner: str,
        name: str,
        number: int,
        body: str,
    ) -> None:
        del owner, name
        self.pull_bodies[number] = body
        self.update_body_calls.append((number, body))


class GitPublicationGatewayTests(unittest.TestCase):
    def test_push_uses_configured_token_without_ambient_gh_credential_helper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            observed: dict[str, object] = {}

            def run(
                argv: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                environment = dict(kwargs["env"])  # type: ignore[arg-type]
                observed["argv"] = argv
                observed["environment"] = environment
                observed["askpass_exists"] = Path(environment["GIT_ASKPASS"]).is_file()
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            gateway = GitPublicationGateway(object(), token="configured-token")  # type: ignore[arg-type]
            with mock.patch("repogents.publication.subprocess.run", side_effect=run):
                gateway.push_branch(
                    checkout,
                    "owner",
                    "repo",
                    "agent/issue-3-run-1",
                    "a" * 40,
                    None,
                )

        argv = observed["argv"]
        environment = observed["environment"]
        self.assertTrue(observed["askpass_exists"])
        self.assertEqual(environment["REPOGENTS_GITHUB_TOKEN"], "configured-token")  # type: ignore[index]
        self.assertFalse(any("credential.helper" in value for value in argv))  # type: ignore[union-attr]
        self.assertNotIn("gh auth git-credential", " ".join(argv))  # type: ignore[arg-type]
        self.assertIn(
            "--force-with-lease=refs/heads/agent/issue-3-run-1:",
            argv,
        )

    def test_rewritten_push_leases_against_expected_remote_sha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            observed: dict[str, object] = {}

            def run(
                argv: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                observed["argv"] = argv
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            gateway = GitPublicationGateway(object())  # type: ignore[arg-type]
            with mock.patch("repogents.publication.subprocess.run", side_effect=run):
                gateway.push_branch(
                    checkout,
                    "owner",
                    "repo",
                    "agent/issue-3-run-1",
                    "a" * 40,
                    "b" * 40,
                )

        self.assertIn(
            "--force-with-lease=refs/heads/agent/issue-3-run-1:" + "b" * 40,
            observed["argv"],
        )

    def test_fetch_intended_base_uses_the_same_configured_token_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            calls: list[tuple[list[str], dict[str, str]]] = []

            def run(
                argv: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                environment = dict(kwargs["env"])  # type: ignore[arg-type]
                calls.append((argv, environment))
                self.assertTrue(Path(environment["GIT_ASKPASS"]).is_file())
                stdout = "b" * 40 + "\n" if "rev-parse" in argv else ""
                return subprocess.CompletedProcess(argv, 0, stdout, "")

            gateway = GitPublicationGateway(object(), token="configured-token")  # type: ignore[arg-type]
            with mock.patch("repogents.publication.subprocess.run", side_effect=run):
                head = gateway.fetch_intended_base_head(
                    checkout,
                    "owner",
                    "repo",
                    "main",
                )

        self.assertEqual(head, "b" * 40)
        self.assertEqual(len(calls), 2)
        self.assertIn("fetch", calls[0][0])
        self.assertIn("refs/heads/main", calls[0][0])
        self.assertTrue(
            all(
                call[1]["REPOGENTS_GITHUB_TOKEN"] == "configured-token"
                for call in calls
            )
        )
        self.assertTrue(
            all("gh auth git-credential" not in " ".join(call[0]) for call in calls)
        )


class MiniSweScopeReviewerTests(unittest.TestCase):
    def test_passes_stored_model_base_url_state_directory_supervisor_and_run_id(
        self,
    ) -> None:
        observed: dict[str, object] = {}
        state_root = Path("/model-state/scope-review")

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
            return {"in_scope": True, "reason": "only the requested change"}

        with (
            mock.patch.object(
                MiniSweScopeReviewer,
                "_build_inference",
                side_effect=lambda self_: self_,
            ),
            mock.patch(
                "repogents.publication.MiniSweInference.infer",
                autospec=True,
                side_effect=fake_infer,
            ),
        ):
            reviewer = MiniSweScopeReviewer(
                base_url="https://custom.example.com/v1",
                state_root=state_root,
                processes=supervisor,
            )
            decision = reviewer.review(
                {
                    "run_id": "run-42",
                    "number": 3,
                    "title": "Large change",
                    "stored_verifier": {
                        "runtime": "mini-swe-agent",
                        "model": "openai/gpt-4.1",
                    },
                },
                "+" + ("changed line\n" * 10),
                ("large.py",),
            )

        self.assertEqual(decision.in_scope, True)
        self.assertEqual(decision.reason, "only the requested change")
        self.assertEqual(observed["model"], "openai/gpt-4.1")
        self.assertEqual(observed["base_url"], "https://custom.example.com/v1")
        self.assertEqual(observed["timeout"], 600)
        self.assertEqual(observed["supervisor"], supervisor)
        self.assertEqual(observed["run_id"], "run-42")
        self.assertTrue(observed["state_directory"].is_absolute())
        self.assertIn("scope", str(observed["state_directory"]))
        prompt_data = json.loads(observed["prompt"])
        self.assertIn("response_schema", prompt_data)
        self.assertEqual(prompt_data["response_schema"]["in_scope"], "boolean")
        self.assertTrue(
            "Return exactly one JSON object" in observed["system_prompt"]
            or "Return one JSON" in observed["system_prompt"]
        )

    def test_large_scope_prompt_is_file_backed(self) -> None:
        observed: dict[str, object] = {}
        large_diff = "+" + ("changed line\n" * 20_000)

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
            return {"in_scope": True, "reason": "large change is in scope"}

        with mock.patch(
            "repogents.publication.MiniSweInference.infer",
            autospec=True,
            side_effect=fake_infer,
        ):
            reviewer = MiniSweScopeReviewer()
            reviewer.review(
                {
                    "number": 3,
                    "title": "Large change",
                    "stored_verifier": {
                        "runtime": "mini-swe-agent",
                        "model": "openai/gpt-5",
                    },
                },
                large_diff,
                ("large.py",),
            )

        prompt_data = json.loads(observed["prompt"])
        self.assertEqual(prompt_data["diff"], large_diff)
        rules = prompt_data.get("decision_rules", [])
        self.assertTrue(any("ambient host" in rule for rule in rules))
        self.assertTrue(
            any("must not be required in the committed diff" in rule for rule in rules)
        )
        self.assertTrue(any("reject them when present" in rule for rule in rules))
        self.assertNotIn("changed line", observed["system_prompt"])
        self.assertLess(len(observed["system_prompt"]), 1_000)

    def test_rejects_obsolete_omp_runtime(self) -> None:
        reviewer = MiniSweScopeReviewer()
        with self.assertRaises(RuntimeError) as raised:
            reviewer.review(
                {
                    "number": 3,
                    "title": "Change",
                    "stored_verifier": {
                        "runtime": "omp",
                        "model": "openai/gpt-3.5-turbo",
                    },
                },
                "+change",
                ("app.py",),
            )
        self.assertIn("omp", str(raised.exception))

    def test_requires_explicit_model_when_no_constructor_or_stored_override(
        self,
    ) -> None:
        reviewer = MiniSweScopeReviewer()
        with self.assertRaises(RuntimeError) as raised:
            reviewer.review(
                {"number": 3, "title": "Change"},
                "+change",
                ("app.py",),
            )
        self.assertIn("model", str(raised.exception).lower())

    def test_persists_durable_state_directory_per_run(self) -> None:
        state_root = Path("/model-state/scope-review")
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
            return {"in_scope": True, "reason": "ok"}

        with mock.patch(
            "repogents.publication.MiniSweInference.infer",
            autospec=True,
            side_effect=fake_infer,
        ):
            reviewer = MiniSweScopeReviewer(state_root=state_root)
            reviewer.review(
                {
                    "run_id": "run-alpha",
                    "number": 3,
                    "title": "Change A",
                    "stored_verifier": {
                        "runtime": "mini-swe-agent",
                        "model": "openai/gpt-4.1",
                    },
                },
                "+change a",
                ("app.py",),
            )
            reviewer.review(
                {
                    "run_id": "run-beta",
                    "number": 4,
                    "title": "Change B",
                    "stored_verifier": {
                        "runtime": "mini-swe-agent",
                        "model": "openai/gpt-4.1",
                    },
                },
                "+change b",
                ("app.py",),
            )

        self.assertEqual(len(observed_dirs), 2)
        self.assertNotEqual(observed_dirs[0], observed_dirs[1])
        self.assertIn("alpha", str(observed_dirs[0]))
        self.assertIn("beta", str(observed_dirs[1]))


class PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.checkout = (
            self.data_root / "repositories" / "repo-1" / "runs" / "run-1" / "checkout"
        )
        self.checkout.mkdir(parents=True)
        (self.checkout / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "base",
        )
        self.base_sha = self._git("rev-parse", "HEAD").strip()
        (self.checkout / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "fix",
        )
        self.validated_sha = self._git("rev-parse", "HEAD").strip()
        self.sandbox_root = self.data_root / "repositories" / "repo-1" / "sandbox" / "1"
        self.sandbox_root.mkdir(parents=True)
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
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, ?, '{}',
                           '{"instructions":[],"summary":"fixture repository"}', ?)""",
                (str(self.sandbox_root), now),
            )
            connection.execute(
                """INSERT INTO validation_commands
                   (id, sandbox_version_id, position, command_json, source, required)
                   VALUES ('command-1', 'sandbox-1', 0, '[\"python3\",\"-m\",\"unittest\"]', 'fixture', 1)"""
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1,
                           '{"instructions":[],"summary":"fixture repository"}', ?)""",
                (now,),
            )
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions)
                   VALUES
                   ('lead-1', 'team-1', 'lead', 'lead', 'Own result', '[]',
                    'mini-swe-agent', 'openai/gpt-stored', ''),
                   ('verifier-1', 'team-1', 'verification', 'verifier',
                    'Independently review the candidate', '["read","run","git_diff"]',
                    'mini-swe-agent', 'openai/gpt-verifier', 'Review correctness')""")
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 3, 'issue-url', 'Set value',
                           'Set VALUE to 2', '[]', ?)""",
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
                           'sandbox-1', 'team-1', 'main', ?, 'publishing', 'validating', ?, ?, ?, ?, ?)""",
                (
                    self.base_sha,
                    self.validated_sha,
                    str(self.checkout),
                    str(self.checkout.parent),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO validation_baselines
                   (id, run_id, validation_command_id, command_json,
                    base_sha, mode, started_at, completed_at, exit_status,
                    log_path, findings_json)
                   VALUES ('baseline-1', 'run-1', 'command-1',
                           '["python3","-m","unittest"]', ?, 'strict',
                           ?, ?, 0, ?, '[]')""",
                (
                    self.base_sha,
                    now,
                    now,
                    str(self.root / "baseline.log"),
                ),
            )
            connection.execute(
                """INSERT INTO validation_results
                   (id, run_id, validation_command_id, commit_sha,
                    command_json, started_at, completed_at, exit_status,
                    log_path, verdict)
                   VALUES ('result-1', 'run-1', 'command-1', ?,
                           '["python3","-m","unittest"]', ?, ?, 0, ?, 'pass')""",
                (self.validated_sha, now, now, str(self.root / "validation.log")),
            )
            connection.execute(
                """UPDATE repositories SET current_sandbox_version_id='sandbox-1',
                                             current_team_version_id='team-1'
                   WHERE id='repo-1'"""
            )
        self.gateway = FakePublicationGateway()
        self.gateway.intended_base_head = self.base_sha
        self.sandbox = SandboxManager()
        self.lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )
        self.issue_version_id = self.lifecycle.current_issue_version("run-1")
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE activation_events SET issue_version_id=?
                   WHERE id='activation-1'""",
                (self.issue_version_id,),
            )
            connection.execute(
                """UPDATE runs SET validated_issue_version_id=?
                   WHERE id='run-1'""",
                (self.issue_version_id,),
            )
        self.acceptance = passing_acceptance(self.issue_version_id)
        self.service = PublicationService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            scope_reviewer=FakeScopeReviewer(),
            acceptance=self.acceptance,
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

    def _cancel_durably(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET state='canceled', reason='fixture cancellation'
                   WHERE id='run-1'""")

    def _revise_issue_durably(self) -> None:
        with self.db.transaction() as connection:
            current = connection.execute(
                """SELECT issue_versions.id, issue_versions.version
                   FROM issues
                   JOIN issue_versions
                     ON issue_versions.id=issues.current_version_id
                   WHERE issues.id='issue-1'"""
            ).fetchone()
            if current is None:
                raise AssertionError("publication did not establish issue version")
            connection.execute(
                """INSERT INTO issue_versions
                   (id, issue_id, version, previous_version_id,
                    github_updated_at, content_sha256, title, body,
                    discussion_json, observed_at)
                   VALUES ('issue-version-race', 'issue-1', ?, ?,
                           '2026-01-01T01:00:00Z', ?, 'Set value directly',
                           'Set VALUE to 2 without the obsolete dependency.',
                           '[]', '2026-01-01T01:00:01Z')""",
                (int(current["version"]) + 1, current["id"], "f" * 64),
            )
            connection.execute("""UPDATE issues
                   SET current_version_id='issue-version-race',
                       title='Set value directly',
                       body='Set VALUE to 2 without the obsolete dependency.',
                       updated_at='2026-01-01T01:00:00Z'
                   WHERE id='issue-1'""")

    @property
    def branch(self) -> str:
        return "agent/issue-3-run-1"

    def test_passing_result_without_baseline_cannot_publish(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM validation_baselines")

        self.assertIsNone(self.service.publish("run-1"))

        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("baseline", run["reason"])
        self.assertEqual(self.gateway.pushes, [])

    def test_policy_pass_with_nonzero_process_status_is_publishable(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("""UPDATE validation_results
                   SET exit_status=1, verdict='pass'
                   WHERE id='result-1'""")

        pull = self.service.publish("run-1")

        self.assertIsNotNone(pull)
        self.assertEqual(self.gateway.pushes, [(self.branch, self.validated_sha)])

    def test_policy_failure_blocks_even_when_process_status_is_zero(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("""UPDATE validation_results
                   SET exit_status=0, verdict='fail'
                   WHERE id='result-1'""")

        self.assertIsNone(self.service.publish("run-1"))

        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("validation command", run["reason"])
        self.assertEqual(self.gateway.pushes, [])

    def test_publishes_validated_sha_to_one_deterministic_unmerged_pull_request(
        self,
    ) -> None:
        pull = self.service.publish("run-1")
        self.assertIsNotNone(pull)
        self.assertEqual(self.gateway.pushes, [(self.branch, self.validated_sha)])
        self.assertEqual(self.gateway.branches[self.branch], self.validated_sha)
        self.assertEqual(self.gateway.create_calls, 1)
        self.assertFalse(pull.merged)
        self.assertEqual(pull.base_branch, "main")
        with self.db.connect() as connection:
            records = connection.execute("SELECT * FROM pull_requests").fetchall()
            operations = connection.execute(
                "SELECT state FROM outbound_operations ORDER BY kind"
            ).fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["remote_head_sha"], self.validated_sha)
        self.assertEqual(records[0]["state"], "open")
        self.assertTrue(
            all(row["state"] in {"completed", "reconciled"} for row in operations)
        )
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"], "waiting_for_feedback"
        )
        reviewer = self.service.scope_reviewer
        self.assertEqual(
            reviewer.issues[0]["stored_verifier"],  # type: ignore[attr-defined,index]
            {
                "runtime": "mini-swe-agent",
                "model": "openai/gpt-verifier",
                "instructions": "Review correctness",
            },
        )
        self.assertEqual(
            self.acceptance.calls,
            [("run-1", self.validated_sha, ("app.py",))],
        )
        body = self.gateway.pull_bodies[pull.number]
        self.assertIn("Issue acceptance verification", body)
        self.assertIn(self.validated_sha, body)
        self.assertIn("The requested value is observable", body)
        self.assertIn("Closes #3", body)

    def test_unresolved_feedback_prevents_push_at_publication_boundary(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, github_node_id, number, url, branch_name,
                    intended_base_branch, base_sha, validated_head_sha,
                    remote_head_sha, state, created_at, updated_at)
                   VALUES ('pr-existing', 'run-1', 'PR1', 11, 'pr-url',
                           ?, 'main', ?, ?, ?, 'open', ?, ?)""",
                (
                    self.branch,
                    self.base_sha,
                    self.validated_sha,
                    self.validated_sha,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO feedback_versions
                   (id, pull_request_id, feedback_type, github_object_id,
                    github_version, author, body, state, observed_at)
                   VALUES ('feedback-1', 'pr-existing', 'inline_comment',
                           'comment-1', 'v1', 'reviewer',
                           'Please change this before pushing', 'pending', ?)""",
                ("2026-01-01T00:01:00Z",),
            )

        self.assertIsNone(self.service.publish("run-1"))

        self.assertEqual(self.gateway.pushes, [])
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "implementing")
        self.assertIn("unresolved pull-request feedback", run["reason"])

    def test_failed_issue_acceptance_returns_to_implementation_before_push(
        self,
    ) -> None:
        acceptance = FakeAcceptanceGate(
            state="failed",
            summary="The issue-required value was not observed.",
            observed="Command exited 0 but stdout contained VALUE=1.",
            issue_version_id=self.issue_version_id,
        )
        service = PublicationService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            scope_reviewer=FakeScopeReviewer(),
            acceptance=acceptance,
        )

        self.assertIsNone(service.publish("run-1"))

        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "implementing")
        self.assertIn("VALUE=1", run["reason"])
        self.assertEqual(self.gateway.pushes, [])

    def test_revised_sha_refreshes_existing_pull_request_proof(self) -> None:
        pull = self.service.publish("run-1")
        self.assertIsNotNone(pull)
        old_body = self.gateway.pull_bodies[pull.number]
        (self.checkout / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "feedback revision",
        )
        revised_sha = self._git("rev-parse", "HEAD").strip()
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='publishing', last_completed_state='validating',
                       validated_sha=?, reason=NULL
                   WHERE id='run-1'""",
                (revised_sha,),
            )
            connection.execute(
                """UPDATE validation_results SET commit_sha=?
                   WHERE id='result-1'""",
                (revised_sha,),
            )

        revised = self.service.publish("run-1")

        self.assertIsNotNone(revised)
        self.assertEqual(self.gateway.create_calls, 1)
        self.assertEqual(len(self.gateway.pull_requests), 1)
        self.assertEqual(
            self.acceptance.calls[-1],
            ("run-1", revised_sha, ("app.py",)),
        )
        current_body = self.gateway.pull_bodies[pull.number]
        self.assertNotEqual(current_body, old_body)
        self.assertIn(revised_sha, current_body)
        self.assertNotIn(self.validated_sha, current_body)
        self.assertEqual(self.gateway.update_body_calls[-1][0], pull.number)

    def test_scope_review_receives_stored_repository_context_and_commit_ids(
        self,
    ) -> None:
        repository_evidence = {
            "summary": "fixture repository",
            "instructions": [["AGENTS.md", "Do not modify generated files."]],
        }
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE sandbox_versions SET evidence_json=? WHERE id='sandbox-1'",
                (json.dumps(repository_evidence),),
            )
            connection.execute(
                "UPDATE team_versions SET evidence_json=? WHERE id='team-1'",
                (json.dumps(repository_evidence),),
            )
        reviewer = FakeScopeReviewer(
            decision=ScopeDecision(
                False,
                "app.py is governed by the stored generated-file instruction",
            )
        )
        service = PublicationService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            scope_reviewer=reviewer,
            acceptance=passing_acceptance(self.issue_version_id),
        )

        self.assertIsNone(service.publish("run-1"))

        review_context = reviewer.issues[0]
        self.assertEqual(
            review_context["repository_evidence"],
            repository_evidence,
        )
        self.assertEqual(review_context["base_sha"], self.base_sha)
        self.assertEqual(review_context["validated_sha"], self.validated_sha)
        self.assertIn(
            "Do not modify generated files.",
            json.dumps(review_context),
        )
        self.assertEqual(self.gateway.pushes, [])
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "implementing")
        self.assertIn("generated-file instruction", run["reason"])

    def test_missing_stored_scope_context_preserves_publication_for_retry(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE sandbox_versions SET evidence_json='not-json' WHERE id='sandbox-1'"
            )

        self.assertIsNone(self.service.publish("run-1"))

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertEqual(self.gateway.pushes, [])
        self.assertEqual(self.gateway.create_calls, 0)

    def test_feedback_revision_waits_for_pull_head_convergence(self) -> None:
        self.assertIsNotNone(self.service.publish("run-1"))
        (self.checkout / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "feedback",
        )
        revised_sha = self._git("rev-parse", "HEAD").strip()
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='publishing', last_completed_state='validating',
                       validated_sha=?, reason=NULL
                   WHERE id='run-1'""",
                (revised_sha,),
            )
            connection.execute(
                "UPDATE validation_results SET commit_sha=? WHERE id='result-1'",
                (revised_sha,),
            )
        self.gateway.stale_pull_reads = 2

        pull = self.service.publish("run-1")

        self.assertIsNotNone(pull)
        self.assertEqual(pull.head_sha, revised_sha)
        self.assertEqual(self.gateway.create_calls, 1)
        self.assertEqual(len(self.gateway.pull_requests), 1)
        self.assertEqual(
            self.gateway.pushes,
            [(self.branch, self.validated_sha), (self.branch, revised_sha)],
        )
        self.assertEqual(
            self.gateway.push_leases,
            [None, self.validated_sha],
        )

    def test_next_attempt_retries_push_that_failed_before_external_mutation(
        self,
    ) -> None:
        self.gateway.fail_before_push = True
        self.assertIsNone(self.service.publish("run-1"))
        self.assertEqual(self.gateway.pushes, [])
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")

        self.assertIsNotNone(self.service.publish("run-1"))
        self.assertEqual(self.gateway.pushes, [(self.branch, self.validated_sha)])
        self.assertEqual(self.gateway.create_calls, 1)

    def test_next_attempt_reconciles_push_accepted_before_response_loss(self) -> None:
        self.gateway.crash_after_push = True
        self.assertIsNone(self.service.publish("run-1"))
        self.assertEqual(self.gateway.pushes, [(self.branch, self.validated_sha)])
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")

        self.assertIsNotNone(self.service.publish("run-1"))
        self.assertEqual(self.gateway.pushes, [(self.branch, self.validated_sha)])
        self.assertEqual(self.gateway.create_calls, 1)

    def test_next_attempt_retries_pull_request_failure_before_external_mutation(
        self,
    ) -> None:
        self.gateway.fail_before_create = True
        self.assertIsNone(self.service.publish("run-1"))
        self.assertEqual(self.gateway.pushes, [(self.branch, self.validated_sha)])
        self.assertEqual(self.gateway.create_calls, 0)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")

        self.assertIsNotNone(self.service.publish("run-1"))
        self.assertEqual(self.gateway.pushes, [(self.branch, self.validated_sha)])
        self.assertEqual(self.gateway.create_calls, 1)

    def test_next_attempt_reconciles_pull_request_created_before_response_loss(
        self,
    ) -> None:
        self.gateway.crash_after_create = True
        self.assertIsNone(self.service.publish("run-1"))
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        pull = self.service.publish("run-1")
        self.assertIsNotNone(pull)
        self.assertEqual(self.gateway.create_calls, 1)
        self.assertEqual(len(self.gateway.pull_requests), 1)
        self.assertEqual(len(self.gateway.pushes), 1)
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0],
                1,
            )

    def test_unexpected_remote_branch_sha_blocks_without_overwrite(self) -> None:
        self.gateway.branches[self.branch] = "c" * 40
        self.assertIsNone(self.service.publish("run-1"))
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("unexpected SHA", run["reason"])
        self.assertEqual(self.gateway.pushes, [])

    def test_remote_rewrite_race_cannot_overwrite_concurrent_head(self) -> None:
        self.assertIsNotNone(self.service.publish("run-1"))
        (self.checkout / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "feedback revision",
        )
        revised_sha = self._git("rev-parse", "HEAD").strip()
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='publishing', last_completed_state='validating',
                       validated_sha=?, reason=NULL
                   WHERE id='run-1'""",
                (revised_sha,),
            )
            connection.execute(
                "UPDATE validation_results SET commit_sha=? WHERE id='result-1'",
                (revised_sha,),
            )
        concurrent_sha = "c" * 40
        self.gateway.before_push = lambda: self.gateway.branches.__setitem__(
            self.branch,
            concurrent_sha,
        )

        self.assertIsNone(self.service.publish("run-1"))

        self.assertEqual(self.gateway.branches[self.branch], concurrent_sha)
        self.assertEqual(
            self.gateway.pushes,
            [(self.branch, self.validated_sha)],
        )
        self.assertEqual(
            self.gateway.push_leases,
            [None, self.validated_sha],
        )

    def test_durable_cancellation_after_remote_read_prevents_push(self) -> None:
        self.gateway.after_remote_head_read = self._cancel_durably

        self.assertIsNone(self.service.publish("run-1"))

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "canceled")
        self.assertEqual(self.gateway.pushes, [])
        self.assertEqual(self.gateway.create_calls, 0)

    def test_issue_revision_after_remote_read_prevents_stale_push(self) -> None:
        self.gateway.after_remote_head_read = self._revise_issue_durably

        self.assertIsNone(self.service.publish("run-1"))

        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("issue version", run["reason"])
        self.assertEqual(self.gateway.pushes, [])
        self.assertEqual(self.gateway.create_calls, 0)
        with self.db.connect() as connection:
            pull = connection.execute("""SELECT validated_head_sha, remote_head_sha,
                          validated_issue_version_id
                   FROM pull_requests WHERE run_id='run-1'""").fetchone()
        self.assertEqual(pull["validated_head_sha"], self.validated_sha)
        self.assertIsNone(pull["remote_head_sha"])
        self.assertNotEqual(
            pull["validated_issue_version_id"],
            "issue-version-race",
        )

    def test_durable_cancellation_after_pull_lookup_prevents_create(self) -> None:
        self.gateway.after_pull_read = self._cancel_durably

        self.assertIsNone(self.service.publish("run-1"))

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "canceled")
        self.assertEqual(
            self.gateway.pushes,
            [(self.branch, self.validated_sha)],
        )
        self.assertEqual(self.gateway.create_calls, 0)

    def test_cancellation_serializes_with_inflight_push_boundary(self) -> None:
        push_started = threading.Event()
        release_push = threading.Event()
        cancel_done = threading.Event()
        observed_states: list[str] = []

        def before_push() -> None:
            push_started.set()
            self.assertTrue(release_push.wait(5))
            observed_states.append(str(self.lifecycle.get_run("run-1")["state"]))

        self.gateway.before_push = before_push
        publish_thread = threading.Thread(target=lambda: self.service.publish("run-1"))
        publish_thread.start()
        self.assertTrue(push_started.wait(5))
        cancel_thread = threading.Thread(
            target=lambda: (
                self.lifecycle.cancel("run-1", "canceled during push"),
                cancel_done.set(),
            )
        )
        cancel_thread.start()

        self.assertFalse(cancel_done.wait(0.1))
        release_push.set()
        publish_thread.join(5)
        cancel_thread.join(5)

        self.assertFalse(publish_thread.is_alive())
        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual(observed_states, ["publishing"])
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "canceled")
        self.assertEqual(
            self.gateway.pushes,
            [(self.branch, self.validated_sha)],
        )
        self.assertIn(self.gateway.create_calls, {0, 1})

    def test_durable_cancellation_prevents_branch_reconciliation_mutations(
        self,
    ) -> None:
        self.gateway.branches[self.branch] = self.validated_sha
        self.gateway.after_remote_head_read = self._cancel_durably

        self.assertIsNone(self.service.publish("run-1"))

        with self.db.connect() as connection:
            pull = connection.execute(
                "SELECT remote_head_sha FROM pull_requests WHERE run_id='run-1'"
            ).fetchone()
            operation = connection.execute("""SELECT state FROM outbound_operations
                   WHERE run_id='run-1' AND kind='push_branch'""").fetchone()
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "canceled")
        self.assertIsNone(pull["remote_head_sha"])
        self.assertEqual(operation["state"], "pending")
        self.assertEqual(self.gateway.pushes, [])
        self.assertEqual(self.gateway.create_calls, 0)

    def test_durable_cancellation_prevents_pull_reconciliation_mutations(self) -> None:
        self.gateway.branches[self.branch] = self.validated_sha
        self.gateway.pull_requests.append(
            PullRequestInfo(
                node_id="PR-existing",
                number=11,
                url="https://github.com/owner/repo/pull/11",
                state="open",
                merged=False,
                head_branch=self.branch,
                head_sha=self.validated_sha,
                base_branch="main",
                updated_at="2026-01-01T00:00:00Z",
            )
        )
        self.gateway.after_pull_read = self._cancel_durably

        self.assertIsNone(self.service.publish("run-1"))

        with self.db.connect() as connection:
            pull = connection.execute("""SELECT github_node_id, url, state
                   FROM pull_requests WHERE run_id='run-1'""").fetchone()
            operation = connection.execute("""SELECT state FROM outbound_operations
                   WHERE run_id='run-1' AND kind='create_pull_request'""").fetchone()
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "canceled")
        self.assertIsNone(pull["github_node_id"])
        self.assertEqual(pull["url"], "")
        self.assertEqual(pull["state"], "pending")
        self.assertEqual(operation["state"], "pending")
        self.assertEqual(self.gateway.create_calls, 0)

    def test_prepare_base_revision_reports_changed_base_generation(self) -> None:
        self.assertIsNotNone(self.service.publish("run-1"))
        current_base_sha = "f" * 40
        self.gateway.intended_base_head = current_base_sha

        with self.assertRaises(PublicationBaseChanged) as caught:
            self.service.prepare_base_revision("run-1", self.base_sha)

        self.assertEqual(caught.exception.expected_base_sha, self.base_sha)
        self.assertEqual(caught.exception.current_base_sha, current_base_sha)
        self.assertIn(self.base_sha, str(caught.exception))
        self.assertIn(current_base_sha, str(caught.exception))

    def test_resolved_base_conflict_updates_the_original_pull_request(
        self,
    ) -> None:
        first_pull = self.service.publish("run-1")
        self.assertIsNotNone(first_pull)
        self.assertEqual(self.gateway.create_calls, 1)

        self._git("switch", "-qc", "advanced-base", self.base_sha)
        (self.checkout / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
        (self.checkout / "upstream.txt").write_text(
            "base branch work\n",
            encoding="utf-8",
        )
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "advance base",
        )
        current_base_sha = self._git("rev-parse", "HEAD").strip()
        self.gateway.intended_base_head = current_base_sha
        self._git("switch", "-q", "main")

        prepared_sha = self.service.prepare_base_revision(
            "run-1",
            current_base_sha,
        )
        self.assertEqual(prepared_sha, current_base_sha)
        self.assertEqual(
            self._git(
                "rev-parse",
                f"refs/repogents/pull-bases/{current_base_sha}",
            ).strip(),
            current_base_sha,
        )
        merge = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=f@example.com",
                "merge",
                "--no-commit",
                prepared_sha,
            ],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertNotEqual(merge.returncode, 0)
        (self.checkout / "app.py").write_text(
            "VALUE = 2\n",
            encoding="utf-8",
        )
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "resolve base conflict",
        )
        resolved_sha = self._git("rev-parse", "HEAD").strip()
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE runs
                   SET state='publishing', last_completed_state='validating',
                       validated_sha=?
                   WHERE id='run-1'""",
                (resolved_sha,),
            )
            connection.execute(
                """UPDATE validation_results
                   SET commit_sha=?
                   WHERE id='result-1'""",
                (resolved_sha,),
            )

        reviewer = FakeScopeReviewer()
        resumed = PublicationService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            scope_reviewer=reviewer,
            acceptance=passing_acceptance(self.issue_version_id),
        )
        second_pull = resumed.publish("run-1")

        self.assertIsNotNone(second_pull)
        self.assertEqual(second_pull.number, first_pull.number)
        self.assertEqual(self.gateway.create_calls, 1)
        self.assertEqual(len(self.gateway.pull_requests), 1)
        self.assertEqual(
            self.gateway.branches[self.branch],
            resolved_sha,
        )
        self.assertEqual(
            reviewer.reviews[-1][1],
            ("app.py",),
        )

    def test_current_intended_base_conflict_blocks_without_changing_activation_base(
        self,
    ) -> None:
        self._git("switch", "-qc", "advanced-base", self.base_sha)
        (self.checkout / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "conflicting base advance",
        )
        self.gateway.intended_base_head = self._git("rev-parse", "HEAD").strip()
        self._git("switch", "-q", "main")

        self.assertIsNone(self.service.publish("run-1"))

        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("conflict", run["reason"].lower())
        self.assertEqual(run["base_sha"], self.base_sha)
        self.assertEqual(self.gateway.pushes, [])

    def test_stale_or_missing_validation_blocks_publication(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM validation_results")
        self.assertIsNone(self.service.publish("run-1"))
        self.assertIn("validation", self.lifecycle.get_run("run-1")["reason"].lower())

    def test_deletion_only_diff_is_reviewed_and_published(self) -> None:
        (self.checkout / "app.py").unlink()
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "remove obsolete application module",
        )
        deletion_sha = self._git("rev-parse", "HEAD").strip()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id='run-1'",
                (deletion_sha,),
            )
            connection.execute(
                "UPDATE validation_results SET commit_sha=? WHERE id='result-1'",
                (deletion_sha,),
            )
        reviewer = FakeScopeReviewer()
        service = PublicationService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            scope_reviewer=reviewer,
            acceptance=passing_acceptance(self.issue_version_id),
        )

        pull = service.publish("run-1")

        self.assertIsNotNone(pull)
        self.assertEqual(self.gateway.pushes, [(self.branch, deletion_sha)])
        self.assertEqual(len(reviewer.reviews), 1)
        diff, changed_files = reviewer.reviews[0]
        self.assertEqual(changed_files, ("app.py",))
        self.assertIn("deleted file mode", diff)

    def test_binary_diff_is_preserved_for_scope_review_and_publication(self) -> None:
        (self.checkout / "asset.bin").write_bytes(bytes(range(256)) * 8)
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "add requested binary fixture",
        )
        binary_sha = self._git("rev-parse", "HEAD").strip()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id='run-1'",
                (binary_sha,),
            )
            connection.execute(
                "UPDATE validation_results SET commit_sha=? WHERE id='result-1'",
                (binary_sha,),
            )
        reviewer = FakeScopeReviewer()
        service = PublicationService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            scope_reviewer=reviewer,
            acceptance=passing_acceptance(self.issue_version_id),
        )

        pull = service.publish("run-1")

        self.assertIsNotNone(pull)
        self.assertEqual(self.gateway.pushes, [(self.branch, binary_sha)])
        diff, changed_files = reviewer.reviews[0]
        self.assertIn("asset.bin", changed_files)
        self.assertIn("GIT binary patch", diff)

    def test_scope_rejection_returns_to_implementation_before_push(self) -> None:
        service = PublicationService(
            database=self.db,
            lifecycle=self.lifecycle,
            gateway=self.gateway,
            scope_reviewer=FakeScopeReviewer(
                ScopeDecision(False, "unrelated file changed")
            ),
            acceptance=passing_acceptance(self.issue_version_id),
        )
        self.assertIsNone(service.publish("run-1"))
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "implementing")
        self.assertIn("unrelated", run["reason"])
        self.assertEqual(self.gateway.pushes, [])

    def test_secret_or_forbidden_artifact_blocks_before_push(self) -> None:
        secret = "ghp_" + "A" * 36  # pragma: allowlist secret
        (self.checkout / ".env").write_text(f"TOKEN={secret}\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.com",
            "commit",
            "-qm",
            "bad",
        )
        bad_sha = self._git("rev-parse", "HEAD").strip()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET validated_sha=? WHERE id='run-1'", (bad_sha,)
            )
            connection.execute(
                "UPDATE validation_results SET commit_sha=? WHERE id='result-1'",
                (bad_sha,),
            )
        self.assertIsNone(self.service.publish("run-1"))
        self.assertEqual(self.gateway.pushes, [])
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")
        self.assertIn("forbidden", self.lifecycle.get_run("run-1")["reason"].lower())


if __name__ == "__main__":
    unittest.main()
