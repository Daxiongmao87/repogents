from base64 import b64decode
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import os
import subprocess

import pytest

from repogents.github import (
    FeedbackAddress,
    GitHubClient,
    GitHubFeedback,
    GitHubIssue,
    PullRequest,
    PublicationCandidate,
)


def test_adapter_values_are_exact_and_immutable():
    issue = GitHubIssue(number=7, title="Fix it", body="Details", url="https://example.test/issues/7")
    feedback = GitHubFeedback(
        external_id="inline:31",
        kind="inline",
        body="Change this",
        path="src/app.py",
        line=14,
        review_thread_id="PRRT_thread_31",
        top_level_comment_id=31,
    )
    pull = PullRequest(
        number=9,
        url="https://example.test/pulls/9",
        branch="agent/issue-7",
        state="open",
        merged=False,
        diff="diff --git a/src/app.py b/src/app.py",
        head_sha="0123456789abcdef0123456789abcdef01234567",
    )
    address = FeedbackAddress(
        status="RESOLVED",
        response_url="https://example.test/pulls/9#discussion_r91",
    )

    assert issue == GitHubIssue(7, "Fix it", "Details", "https://example.test/issues/7")
    assert feedback == GitHubFeedback(
        "inline:31",
        "inline",
        "Change this",
        "src/app.py",
        14,
        "PRRT_thread_31",
        31,
    )
    assert GitHubFeedback(
        "inline:31",
        "inline",
        "Change this",
        "src/app.py",
        14,
    ).review_thread_id is None
    assert pull == PullRequest(
        9,
        "https://example.test/pulls/9",
        "agent/issue-7",
        "open",
        False,
        "diff --git a/src/app.py b/src/app.py",
        "0123456789abcdef0123456789abcdef01234567",
    )
    assert PullRequest(
        9,
        "https://example.test/pulls/9",
        "agent/issue-7",
        "open",
        False,
        "diff --git a/src/app.py b/src/app.py",
    ).head_sha == ""
    assert address == FeedbackAddress(
        "RESOLVED",
        "https://example.test/pulls/9#discussion_r91",
    )
    with pytest.raises(FrozenInstanceError):
        issue.title = "Changed"
    with pytest.raises(FrozenInstanceError):
        feedback.body = "Changed"
    with pytest.raises(FrozenInstanceError):
        pull.state = "closed"
    with pytest.raises(FrozenInstanceError):
        address.response_url = "https://example.test/changed"


def test_repository_returns_metadata_through_the_request_boundary():
    calls = []
    metadata = {
        "full_name": "acme/widgets",
        "default_branch": "main",
        "clone_url": "https://github.com/acme/widgets.git",
        "private": True,
    }

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        return metadata

    client = GitHubClient("placeholder-token", request=request)

    assert client.repository("acme/widgets") == metadata
    assert calls == [("GET", "/repos/acme/widgets", None, None)]


def test_default_request_authenticates_and_decodes_repository_json(monkeypatch):
    captured = {}

    class Response:
        headers = {"Content-Type": "application/json; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return (
                b'{"full_name":"acme/widgets","default_branch":"main",'
                b'"clone_url":"https://github.com/acme/widgets.git"}'
            )

    def urlopen(request):
        captured["request"] = request
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GitHubClient("placeholder-token", api_base="https://api.example.test/")

    assert client.repository("acme/widgets")["default_branch"] == "main"
    request = captured["request"]
    assert request.method == "GET"
    assert request.full_url == "https://api.example.test/repos/acme/widgets"
    assert request.get_header("Authorization") == "Bearer placeholder-token"


def test_list_open_issues_requests_every_open_issue_without_label_filter():
    calls = []
    response = [
        {
            "number": 7,
            "title": "First",
            "body": "First body",
            "html_url": "https://example.test/issues/7",
            "labels": [{"name": "urgent"}, {"name": "documentation"}],
        },
        {
            "number": 8,
            "title": "No body",
            "body": None,
            "html_url": "https://example.test/issues/8",
        },
        {
            "number": 9,
            "title": "A pull request, not an issue",
            "body": "Ignored",
            "html_url": "https://example.test/pulls/9",
            "pull_request": {"url": "https://api.example.test/pulls/9"},
        },
    ]

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        return response

    client = GitHubClient("placeholder-token", request=request)

    assert client.list_open_issues("acme/widgets") == [
        GitHubIssue(
            7,
            "First",
            "First body",
            "https://example.test/issues/7",
            ("urgent", "documentation"),
        ),
        GitHubIssue(8, "No body", "", "https://example.test/issues/8"),
    ]
    assert calls == [
        (
            "GET",
            "/repos/acme/widgets/issues",
            {"state": "open", "per_page": 100, "page": 1},
            None,
        )
    ]


def test_list_open_issues_paginates_until_a_short_page():
    calls = []
    pages = {
        1: [
            {
                "number": number,
                "title": f"Issue {number}",
                "body": f"Body {number}",
                "html_url": f"https://example.test/issues/{number}",
            }
            for number in range(1, 101)
        ],
        2: [
            {
                "number": 101,
                "title": "Issue 101",
                "body": "Body 101",
                "html_url": "https://example.test/issues/101",
            }
        ],
    }

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        return pages[query["page"]]

    client = GitHubClient("placeholder-token", request=request)

    issues = client.list_open_issues("acme/widgets")

    assert [issue.number for issue in issues] == list(range(1, 102))
    assert issues[0] == GitHubIssue(
        1,
        "Issue 1",
        "Body 1",
        "https://example.test/issues/1",
    )
    assert issues[-1] == GitHubIssue(
        101,
        "Issue 101",
        "Body 101",
        "https://example.test/issues/101",
    )
    assert calls == [
        (
            "GET",
            "/repos/acme/widgets/issues",
            {
                "state": "open",
                "per_page": 100,
                "page": 1,
            },
            None,
        ),
        (
            "GET",
            "/repos/acme/widgets/issues",
            {
                "state": "open",
                "per_page": 100,
                "page": 2,
            },
            None,
        ),
    ]


def test_ensure_follow_up_issue_creates_once_and_reuses_its_marker():
    calls = []
    external_id = "inline:101"
    title = "Fix the unrelated cache invalidation defect"
    body = "Observed defect and bounded acceptance criteria."
    marker = "<!-- repogents-follow-up:inline:101 -->"
    marked_body = f"{body}\n\n{marker}"
    existing_issue = None
    unrelated_issues = [
        {
            "number": number,
            "title": f"Unrelated issue {number}",
            "body": "Unrelated",
            "html_url": f"https://example.test/issues/{number}",
        }
        for number in range(1, 101)
    ]

    def request(method, path, *, query=None, json_body=None):
        nonlocal existing_issue
        calls.append((method, path, query, json_body))
        assert path == "/repos/acme/widgets/issues"
        if method == "GET":
            assert query["state"] == "all"
            assert query["per_page"] == 100
            if query["page"] == 1:
                return unrelated_issues
            assert query["page"] == 2
            return [] if existing_issue is None else [existing_issue]
        assert method == "POST"
        assert existing_issue is None
        assert json_body == {
            "title": title,
            "body": marked_body,
        }
        existing_issue = {
            "number": 301,
            "title": title,
            "body": marked_body,
            "html_url": "https://example.test/issues/301",
            "state": "closed",
        }
        return existing_issue

    client = GitHubClient("placeholder-token", request=request)

    first = client.ensure_follow_up_issue(
        "acme/widgets",
        external_id,
        title,
        body,
    )
    second = client.ensure_follow_up_issue(
        "acme/widgets",
        external_id,
        title,
        body,
    )

    expected = GitHubIssue(
        301,
        title,
        marked_body,
        "https://example.test/issues/301",
    )
    assert first == expected
    assert second == expected
    assert marked_body.count(marker) == 1
    assert [call[0] for call in calls].count("POST") == 1
    assert [call[2]["page"] for call in calls if call[0] == "GET"] == [
        1,
        2,
        1,
        2,
    ]


def test_checkout_clones_the_target_branch_into_a_new_workspace(tmp_path):
    request_calls = []
    command_calls = []
    workspace = tmp_path / "widgets"

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        return {
            "full_name": "acme/widgets",
            "default_branch": "main",
            "clone_url": "https://github.com/acme/widgets.git",
        }

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    assert client.checkout("acme/widgets", "main", workspace) == workspace
    assert request_calls == [("GET", "/repos/acme/widgets", None, None)]
    assert command_calls[0][0] == [
        "git",
        "clone",
        "--branch",
        "main",
        "--single-branch",
        "https://github.com/acme/widgets.git",
        str(workspace),
    ]
    assert command_calls[0][1] is None
    assert command_calls[0][2]["GIT_TERMINAL_PROMPT"] == "0"
    assert "placeholder-token" not in repr(command_calls)


def test_checkout_updates_an_existing_target_branch_workspace(tmp_path):
    workspace = tmp_path / "widgets"
    (workspace / ".git").mkdir(parents=True)
    command_calls = []

    def unexpected_request(method, path, *, query=None, json_body=None):
        raise AssertionError("an existing checkout does not need repository metadata")

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=unexpected_request,
        command_runner=command_runner,
    )

    assert client.checkout("acme/widgets", "main", workspace) == workspace
    assert [call[0] for call in command_calls] == [
        ["git", "fetch", "origin", "main"],
        ["git", "checkout", "main"],
        ["git", "pull", "--ff-only", "origin", "main"],
    ]
    assert all(call[1] == workspace for call in command_calls)
    assert all(call[2]["GIT_TERMINAL_PROMPT"] == "0" for call in command_calls)


def test_pull_request_returns_current_state_and_diff():
    calls = []
    pull_json = {
        "number": 12,
        "html_url": "https://example.test/pulls/12",
        "head": {
            "ref": "agent/issue-7",
            "sha": "1111111111111111111111111111111111111111",
        },
        "state": "closed",
        "merged": True,
    }
    diff = "diff --git a/old.py b/new.py\n"

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        if path.endswith(".diff"):
            return diff
        return pull_json

    client = GitHubClient("placeholder-token", request=request)

    assert client.pull_request("acme/widgets", 12) == PullRequest(
        number=12,
        url="https://example.test/pulls/12",
        branch="agent/issue-7",
        state="closed",
        merged=True,
        diff=diff,
        head_sha="1111111111111111111111111111111111111111",
    )
    assert calls == [
        ("GET", "/repos/acme/widgets/pulls/12", None, None),
        ("GET", "/repos/acme/widgets/pulls/12.diff", None, None),
    ]


def test_default_request_uses_the_github_diff_media_type(monkeypatch):
    requests = []
    pull_json = (
        b'{"number":12,"html_url":"https://example.test/pulls/12",'
        b'"head":{"ref":"agent/issue-7","sha":"2222222222222222222222222222222222222222"},'
        b'"state":"open","merged":false}'
    )
    diff = b"diff --git a/old.py b/new.py\n"

    class Response:
        def __init__(self, body, content_type):
            self._body = body
            self.headers = {"Content-Type": content_type}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return self._body

    def urlopen(request):
        requests.append(request)
        if request.get_header("Accept") == "application/vnd.github.diff":
            return Response(diff, "text/plain; charset=utf-8")
        return Response(pull_json, "application/json; charset=utf-8")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GitHubClient("placeholder-token", api_base="https://api.example.test")

    assert client.pull_request("acme/widgets", 12).diff == diff.decode()
    assert [request.full_url for request in requests] == [
        "https://api.example.test/repos/acme/widgets/pulls/12",
        "https://api.example.test/repos/acme/widgets/pulls/12",
    ]
    assert requests[1].get_header("Accept") == "application/vnd.github.diff"


def test_candidate_diff_stages_the_complete_copy_against_latest_target(tmp_path):
    remote = tmp_path / "remote.git"
    workspace = tmp_path / "workspace"
    upstream = tmp_path / "upstream"

    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(workspace))
    (workspace / ".gitignore").write_text("*.ignored\n")
    (workspace / "base.txt").write_text("base\n")
    (workspace / "deleted.txt").write_text("delete me\n")
    (workspace / "staged.txt").write_text("before\n")
    git("add", "--all", cwd=workspace)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Base",
        cwd=workspace,
    )
    git("remote", "add", "origin", str(remote), cwd=workspace)
    git("push", "--set-upstream", "origin", "main", cwd=workspace)
    git("checkout", "-b", "agent/issue-7", cwd=workspace)
    (workspace / "prior.py").write_text("prior commit\n")
    git("add", "prior.py", cwd=workspace)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Prior issue work",
        cwd=workspace,
    )

    (workspace / "staged.txt").write_text("after\n")
    git("add", "staged.txt", cwd=workspace)
    (workspace / "deleted.txt").unlink()
    (workspace / "untracked.txt").write_text("untracked\n")
    (workspace / "scratch.ignored").write_text("ignored\n")

    git("clone", "--branch", "main", str(remote), str(upstream))
    (upstream / "base.txt").write_text("base\nlatest target\n")
    git("add", "base.txt", cwd=upstream)
    git(
        "-c",
        "user.name=Upstream",
        "-c",
        "user.email=upstream@example.invalid",
        "commit",
        "-m",
        "Advance target",
        cwd=upstream,
    )
    git("push", "origin", "main", cwd=upstream)
    git("add", "--all", cwd=workspace)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Freeze complete candidate",
        cwd=workspace,
    )

    frozen_candidate = PublicationCandidate(
        branch="agent/issue-7",
        head_sha=git("rev-parse", "HEAD", cwd=workspace),
        target_head_sha=git("rev-parse", "HEAD", cwd=upstream),
        remote_head_sha="",
    )
    (workspace / "staged.txt").write_text("post-freeze\n")
    candidate = GitHubClient("placeholder-token").candidate_diff(
        "main",
        workspace,
        candidate=frozen_candidate,
    )

    assert "-latest target" in candidate
    assert "+prior commit" in candidate
    assert "-before" in candidate
    assert "+after" in candidate
    assert "post-freeze" not in candidate
    assert "deleted file mode" in candidate
    assert "-delete me" in candidate
    assert "new file mode" in candidate
    assert "+untracked" in candidate
    assert "scratch.ignored" not in candidate
    assert git("show", "origin/main:base.txt", cwd=workspace) == (
        "base\nlatest target"
    )



def test_candidate_diff_refuses_prepared_candidate_head_movement(tmp_path):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    command_calls = []
    prepared_head = "1111111111111111111111111111111111111111"
    moved_head = "2222222222222222222222222222222222222222"
    target_head = "3333333333333333333333333333333333333333"
    candidate = PublicationCandidate(
        branch="agent/issue-7",
        head_sha=prepared_head,
        target_head_sha=target_head,
        remote_head_sha="",
    )

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{moved_head}\n")
        if args[:2] == ["git", "rev-parse"] and args[2] in {
            "origin/main",
            "refs/remotes/origin/main",
        }:
            return SimpleNamespace(stdout=f"{target_head}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        command_runner=command_runner,
    )

    with pytest.raises(RuntimeError):
        client.candidate_diff("main", workspace, candidate=candidate)

    commands = [args for args, _, _ in command_calls]
    assert ["git", "rev-parse", "HEAD"] in commands
    assert not any(args[1] in {"add", "diff"} for args in commands)


def test_candidate_diff_refuses_fetched_target_movement(tmp_path):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    command_calls = []
    prepared_head = "1111111111111111111111111111111111111111"
    prepared_target = "2222222222222222222222222222222222222222"
    moved_target = "3333333333333333333333333333333333333333"
    candidate = PublicationCandidate(
        branch="agent/issue-7",
        head_sha=prepared_head,
        target_head_sha=prepared_target,
        remote_head_sha="",
    )

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{prepared_head}\n")
        if args[:2] == ["git", "rev-parse"] and args[2] in {
            "origin/main",
            "refs/remotes/origin/main",
        }:
            return SimpleNamespace(stdout=f"{moved_target}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        command_runner=command_runner,
    )

    with pytest.raises(RuntimeError):
        client.candidate_diff("main", workspace, candidate=candidate)

    commands = [args for args, _, _ in command_calls]
    assert any(
        args[:3] == ["git", "fetch", "origin"]
        and any("main" in argument for argument in args[3:])
        for args in commands
    )
    assert any(
        args[:2] == ["git", "rev-parse"]
        and args[2] in {"origin/main", "refs/remotes/origin/main"}
        for args in commands
    )
    assert not any(args[1] in {"add", "diff"} for args in commands)

def test_prepare_publication_returns_exact_frozen_candidate_and_diff_without_remote_effect(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    branch = "agent/issue-7"
    target_head_sha = "1111111111111111111111111111111111111111"
    rebased_head_sha = "2222222222222222222222222222222222222222"
    head_sha = "3333333333333333333333333333333333333333"
    remote_head_sha = "4444444444444444444444444444444444444444"
    expected_diff = "diff --git a/app.py b/app.py\n+prepared\n"
    commit_count = 0

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        raise AssertionError("preparation must not call the GitHub API")

    def command_runner(args, *, cwd=None, env=None):
        nonlocal commit_count
        command_calls.append((args, cwd, env))
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        ]:
            return SimpleNamespace(
                stdout=f"{remote_head_sha}\trefs/heads/{branch}\n"
            )
        if args == ["git", "diff", "--cached", "--name-only"]:
            return SimpleNamespace(stdout="app.py\n")
        if args == ["git", "commit", "-m", "Resolve issue #7"]:
            commit_count += 1
            return SimpleNamespace(stdout="")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{target_head_sha}\n")
        if args == ["git", "rev-parse", "HEAD"]:
            current_head = head_sha if commit_count == 2 else rebased_head_sha
            return SimpleNamespace(stdout=f"{current_head}\n")
        if args == [
            "git",
            "diff",
            "--no-color",
            target_head_sha,
            head_sha,
            "--",
        ]:
            return SimpleNamespace(stdout=expected_diff)
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    candidate, candidate_diff = client.prepare_publication(7, "main", workspace)

    assert candidate == PublicationCandidate(
        branch=branch,
        head_sha=head_sha,
        target_head_sha=target_head_sha,
        remote_head_sha=remote_head_sha,
    )
    assert candidate_diff == expected_diff
    with pytest.raises(FrozenInstanceError):
        candidate.head_sha = "5555555555555555555555555555555555555555"
    commands = [call[0] for call in command_calls]
    assert ["git", "checkout", "-B", branch] in commands
    assert ["git", "fetch", "origin", "main"] in commands
    assert ["git", "rebase", "origin/main"] in commands
    assert ["git", "reset", "--soft", "origin/main"] in commands
    assert commands.count(
        ["git", "commit", "-m", "Resolve issue #7"]
    ) == 2
    assert [
        "git",
        "diff",
        "--no-color",
        target_head_sha,
        head_sha,
        "--",
    ] in commands
    assert request_calls == []
    assert not any(args[:2] == ["git", "push"] for args in commands)
    assert all(call[1] == workspace for call in command_calls)


def test_amend_publication_uses_agent_message_and_returns_new_frozen_head(tmp_path):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    original_head = "1" * 40
    amended_head = "2" * 40
    candidate = PublicationCandidate(
        branch="agent/issue-7",
        head_sha=original_head,
        target_head_sha="3" * 40,
        remote_head_sha="4" * 40,
    )
    calls = []
    rev_parse_count = 0

    def command_runner(args, *, cwd=None, env=None):
        nonlocal rev_parse_count
        calls.append(args)
        if args == ["git", "rev-parse", "HEAD"]:
            rev_parse_count += 1
            return SimpleNamespace(
                stdout=f"{original_head if rev_parse_count == 1 else amended_head}\n"
            )
        return SimpleNamespace(stdout="")

    amended = GitHubClient(
        "placeholder-token", command_runner=command_runner
    ).amend_publication(
        7,
        workspace,
        candidate,
        "Describe the validated change",
    )

    assert amended == PublicationCandidate(
        branch=candidate.branch,
        head_sha=amended_head,
        target_head_sha=candidate.target_head_sha,
        remote_head_sha=candidate.remote_head_sha,
    )
    assert [
        "git",
        "commit",
        "--amend",
        "-m",
        "Describe the validated change",
    ] in calls



def test_prepare_publication_rebase_state_reports_and_stages_only_explicit_paths(
    tmp_path,
):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    workspace = tmp_path / "workspace"
    issue_branch = "agent/issue-7"

    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(cwd, message):
        git("add", "--all", cwd=cwd)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            message,
            cwd=cwd,
        )

    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(seed))
    (seed / "src").mkdir()
    (seed / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (seed / "shared.txt").write_text("base shared\n", encoding="utf-8")
    (seed / "later.txt").write_text("base later\n", encoding="utf-8")
    (seed / "later source.txt").write_text(
        "base later source\n", encoding="utf-8"
    )
    (seed / "src" / "ready z.py").write_text("base ready z\n", encoding="utf-8")
    (seed / "src" / "ready a.py").write_text("base ready a\n", encoding="utf-8")
    (seed / "src" / "related z.py").write_text(
        "base related z\n", encoding="utf-8"
    )
    (seed / "src" / "related a.py").write_text(
        "base related a\n", encoding="utf-8"
    )
    commit(seed, "Base")
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "--set-upstream", "origin", "main", cwd=seed)

    git("clone", "--branch", "main", str(remote), str(workspace))
    git("config", "core.editor", "true", cwd=workspace)
    git("config", "user.name", "Test", cwd=workspace)
    git("config", "user.email", "test@example.invalid", cwd=workspace)
    git("checkout", "-b", issue_branch, cwd=workspace)
    (workspace / "shared.txt").write_text("issue shared\n", encoding="utf-8")
    commit(workspace, "Change shared source")
    (workspace / "later.txt").write_text("issue later\n", encoding="utf-8")
    (workspace / "later source.txt").write_text(
        "issue later source\n", encoding="utf-8"
    )
    (workspace / "src" / "ready z.py").write_text(
        "issue ready z\n", encoding="utf-8"
    )
    (workspace / "src" / "ready a.py").write_text(
        "issue ready a\n", encoding="utf-8"
    )
    commit(workspace, "Change later source")
    issue_head = git("rev-parse", "HEAD", cwd=workspace)

    (seed / "shared.txt").write_text("target shared\n", encoding="utf-8")
    (seed / "later.txt").write_text("target later\n", encoding="utf-8")
    (seed / "later source.txt").write_text(
        "target later source\n", encoding="utf-8"
    )
    commit(seed, "Advance target")
    target_head = git("rev-parse", "HEAD", cwd=seed)
    git("push", "origin", "main", cwd=seed)

    completed_errors = []

    def command_runner(args, *, cwd=None, env=None):
        command_env = os.environ.copy()
        command_env.update(env or {})
        try:
            return subprocess.run(
                args,
                cwd=cwd,
                env=command_env,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as error:
            completed_errors.append(error)
            raise

    def binary_command_runner(args, *, cwd=None, env=None):
        command_env = os.environ.copy()
        command_env.update(env or {})
        return subprocess.run(
            args,
            cwd=cwd,
            env=command_env,
            check=True,
            capture_output=True,
            text=False,
        )

    client = GitHubClient(
        "placeholder-token",
        command_runner=command_runner,
        binary_command_runner=binary_command_runner,
    )

    with pytest.raises(subprocess.CalledProcessError) as captured:
        client.prepare_publication(7, "main", workspace)

    assert completed_errors == [captured.value]
    assert captured.value is completed_errors[0]
    assert captured.value.cmd == ["git", "rebase", "origin/main"]
    assert captured.value.returncode == 1
    assert isinstance(captured.value.stdout, str)
    assert isinstance(captured.value.stderr, str)

    rebase_states = [
        workspace / ".git" / name
        for name in ("rebase-merge", "rebase-apply")
        if (workspace / ".git" / name).exists()
    ]
    assert len(rebase_states) == 1
    assert client.repository_operation_state(workspace) == {
        "rebase_in_progress": True,
        "unmerged_paths": ["shared.txt"],
        "staged_paths": [],
        "unstaged_paths": [],
        "untracked_paths": [],
    }
    unmerged_entries = git("ls-files", "--unmerged", cwd=workspace).splitlines()
    assert {entry.split(maxsplit=3)[2] for entry in unmerged_entries} == {
        "1",
        "2",
        "3",
    }
    assert {
        entry.split("\t", maxsplit=1)[1] for entry in unmerged_entries
    } == {"shared.txt"}
    conflicted_shared = (workspace / "shared.txt").read_text(encoding="utf-8")
    assert conflicted_shared.startswith("<<<<<<< ")
    assert "\n=======\n" in conflicted_shared
    assert "\n>>>>>>> " in conflicted_shared
    assert "target shared\n" in conflicted_shared
    assert "issue shared\n" in conflicted_shared
    assert git("rev-parse", f"refs/heads/{issue_branch}", cwd=workspace) == issue_head
    assert not (workspace / ".git" / "repogents-workspace-unusable").exists()

    first_artifacts = tmp_path / "first-operation-artifacts"
    first_manifest = client.export_repository_operation_artifacts(
        workspace,
        first_artifacts,
    )
    assert first_manifest == {
        "shared.txt": {
            "base": "base/shared.txt",
            "ours": "ours/shared.txt",
            "theirs": "theirs/shared.txt",
        }
    }
    assert (first_artifacts / "base/shared.txt").read_text(encoding="utf-8") == (
        "base shared\n"
    )
    assert (first_artifacts / "ours/shared.txt").read_text(encoding="utf-8") == (
        "target shared\n"
    )
    assert (first_artifacts / "theirs/shared.txt").read_text(encoding="utf-8") == (
        "issue shared\n"
    )
    assert (workspace / "shared.txt").read_text(encoding="utf-8") == conflicted_shared
    assert client.repository_operation_state(workspace) == {
        "rebase_in_progress": True,
        "unmerged_paths": ["shared.txt"],
        "staged_paths": [],
        "unstaged_paths": [],
        "untracked_paths": [],
    }

    (workspace / "shared.txt").write_text("resolved shared\n", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError) as later_conflict:
        client.continue_repository_operation(workspace, ["shared.txt"])

    assert later_conflict.value is completed_errors[-1]
    assert later_conflict.value.cmd == ["git", "rebase", "--continue"]
    assert later_conflict.value.returncode == 1
    assert isinstance(later_conflict.value.stdout, str)
    assert isinstance(later_conflict.value.stderr, str)
    assert client.repository_operation_state(workspace) == {
        "rebase_in_progress": True,
        "unmerged_paths": ["later source.txt", "later.txt"],
        "staged_paths": ["src/ready a.py", "src/ready z.py"],
        "unstaged_paths": [],
        "untracked_paths": [],
    }
    assert git("show", "HEAD:shared.txt", cwd=workspace) == "resolved shared"
    conflicted_later = (workspace / "later.txt").read_text(encoding="utf-8")
    assert conflicted_later.startswith("<<<<<<< ")
    assert "\n=======\n" in conflicted_later
    assert "\n>>>>>>> " in conflicted_later
    assert "target later\n" in conflicted_later
    assert "issue later\n" in conflicted_later
    conflicted_later_source = (workspace / "later source.txt").read_text(
        encoding="utf-8"
    )
    assert conflicted_later_source.startswith("<<<<<<< ")
    assert "\n=======\n" in conflicted_later_source
    assert "\n>>>>>>> " in conflicted_later_source
    assert "target later source\n" in conflicted_later_source
    assert "issue later source\n" in conflicted_later_source

    later_artifacts = tmp_path / "later-operation-artifacts"
    later_manifest = client.export_repository_operation_artifacts(
        workspace,
        later_artifacts,
    )
    assert later_manifest == {
        "later source.txt": {
            "base": "base/later source.txt",
            "ours": "ours/later source.txt",
            "theirs": "theirs/later source.txt",
        },
        "later.txt": {
            "base": "base/later.txt",
            "ours": "ours/later.txt",
            "theirs": "theirs/later.txt",
        },
    }
    assert (later_artifacts / "base/later.txt").read_text(encoding="utf-8") == (
        "base later\n"
    )
    assert (later_artifacts / "ours/later.txt").read_text(encoding="utf-8") == (
        "target later\n"
    )
    assert (later_artifacts / "theirs/later.txt").read_text(encoding="utf-8") == (
        "issue later\n"
    )
    assert (
        later_artifacts / "base/later source.txt"
    ).read_text(encoding="utf-8") == "base later source\n"
    assert (
        later_artifacts / "ours/later source.txt"
    ).read_text(encoding="utf-8") == "target later source\n"
    assert (
        later_artifacts / "theirs/later source.txt"
    ).read_text(encoding="utf-8") == "issue later source\n"

    (workspace / "src" / "related z.py").write_text(
        "resolved related z\n", encoding="utf-8"
    )
    (workspace / "src" / "related a.py").write_text(
        "resolved related a\n", encoding="utf-8"
    )
    (workspace / "src" / "new z.py").write_text(
        "resolved new z\n", encoding="utf-8"
    )
    (workspace / "src" / "new a.py").write_text(
        "resolved new a\n", encoding="utf-8"
    )
    generated = workspace / "generated"
    generated.mkdir()
    ignored_generated = generated / "build output.log"
    ignored_generated.write_text("ignored generated output\n", encoding="utf-8")

    assert git(
        "check-ignore",
        "--",
        "generated/build output.log",
        cwd=workspace,
    ) == "generated/build output.log"
    assert client.repository_operation_state(workspace) == {
        "rebase_in_progress": True,
        "unmerged_paths": ["later source.txt", "later.txt"],
        "staged_paths": ["src/ready a.py", "src/ready z.py"],
        "unstaged_paths": ["src/related a.py", "src/related z.py"],
        "untracked_paths": ["src/new a.py", "src/new z.py"],
    }

    (workspace / "later.txt").write_text("resolved later\n", encoding="utf-8")
    (workspace / "later source.txt").write_text(
        "resolved later source\n", encoding="utf-8"
    )
    with pytest.raises(subprocess.CalledProcessError) as dirty_failure:
        client.continue_repository_operation(
            workspace,
            ["later.txt", "later source.txt"],
        )

    assert dirty_failure.value is completed_errors[-1]
    assert dirty_failure.value.cmd == ["git", "rebase", "--continue"]
    assert dirty_failure.value.returncode == 1
    assert isinstance(dirty_failure.value.stdout, str)
    assert isinstance(dirty_failure.value.stderr, str)
    assert git("ls-files", "--unmerged", cwd=workspace) == ""
    assert client.repository_operation_state(workspace) == {
        "rebase_in_progress": True,
        "unmerged_paths": [],
        "staged_paths": [
            "later source.txt",
            "later.txt",
            "src/ready a.py",
            "src/ready z.py",
        ],
        "unstaged_paths": ["src/related a.py", "src/related z.py"],
        "untracked_paths": ["src/new a.py", "src/new z.py"],
    }
    assert git(
        "ls-files",
        "--cached",
        "--",
        "src/new a.py",
        "src/new z.py",
        cwd=workspace,
    ) == ""

    assert (
        client.continue_repository_operation(
            workspace,
            [
                "src/related z.py",
                "src/new z.py",
                "src/related a.py",
                "src/new a.py",
            ],
        )
        is True
    )
    assert client.repository_operation_state(workspace) == {
        "rebase_in_progress": False,
        "unmerged_paths": [],
        "staged_paths": [],
        "unstaged_paths": [],
        "untracked_paths": [],
    }
    assert not any(
        (workspace / ".git" / metadata).exists()
        for metadata in ("rebase-merge", "rebase-apply")
    )
    assert git("ls-files", "--unmerged", cwd=workspace) == ""
    assert ignored_generated.read_text(encoding="utf-8") == (
        "ignored generated output\n"
    )
    assert git(
        "ls-files",
        "--cached",
        "--",
        "generated/build output.log",
        cwd=workspace,
    ) == ""

    candidate, candidate_diff = client.prepare_publication(7, "main", workspace)

    assert candidate == PublicationCandidate(
        branch=issue_branch,
        head_sha=git("rev-parse", "HEAD", cwd=workspace),
        target_head_sha=target_head,
        remote_head_sha="",
    )
    assert "-target shared" in candidate_diff
    assert "+resolved shared" in candidate_diff
    assert "-target later" in candidate_diff
    assert "+resolved later" in candidate_diff
    assert "-target later source" in candidate_diff
    assert "+resolved later source" in candidate_diff
    assert "+resolved related a" in candidate_diff
    assert "+resolved related z" in candidate_diff
    assert "+resolved new a" in candidate_diff
    assert "+resolved new z" in candidate_diff
    assert git("show", "HEAD:shared.txt", cwd=workspace) == "resolved shared"
    assert git("show", "HEAD:later.txt", cwd=workspace) == "resolved later"
    assert git("show", "HEAD:later source.txt", cwd=workspace) == (
        "resolved later source"
    )
    assert git("show", "HEAD:src/related a.py", cwd=workspace) == (
        "resolved related a"
    )
    assert git("show", "HEAD:src/related z.py", cwd=workspace) == (
        "resolved related z"
    )
    assert git("show", "HEAD:src/new a.py", cwd=workspace) == "resolved new a"
    assert git("show", "HEAD:src/new z.py", cwd=workspace) == "resolved new z"
    assert git("branch", "--show-current", cwd=workspace) == issue_branch
    assert git("status", "--porcelain", cwd=workspace) == ""
    assert ignored_generated.exists()


def test_prepare_publication_replaces_remote_history_with_one_target_based_commit(
    tmp_path,
):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    workspace = tmp_path / "workspace"
    issue_branch = "agent/issue-7"

    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(seed))
    (seed / "base.txt").write_text("base\n")
    git("add", "base.txt", cwd=seed)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Target",
        cwd=seed,
    )
    target_head = git("rev-parse", "HEAD", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "--set-upstream", "origin", "main", cwd=seed)

    git("checkout", "-b", issue_branch, cwd=seed)
    (seed / "prior.txt").write_text("prior issue work\n")
    git("add", "prior.txt", cwd=seed)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Prior issue work",
        cwd=seed,
    )
    git("push", "--set-upstream", "origin", issue_branch, cwd=seed)

    git("clone", "--branch", issue_branch, str(remote), str(workspace))
    git(
        "-c",
        "user.name=Reviewer",
        "-c",
        "user.email=reviewer@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Reviewed issue head",
        cwd=seed,
    )
    remote_issue_head = git("rev-parse", "HEAD", cwd=seed)
    git("push", "origin", issue_branch, cwd=seed)

    absent_remote_head = subprocess.run(
        ["git", "cat-file", "-e", f"{remote_issue_head}^{{commit}}"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    assert absent_remote_head.returncode != 0

    (workspace / "feedback.txt").write_text("feedback work\n")
    git("add", "--all", cwd=workspace)
    expected_diff = subprocess.run(
        ["git", "diff", "--cached", "--no-color", target_head, "--"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    published_head = ""

    def request(method, path, *, query=None, json_body=None):
        if method == "GET" and path == "/repos/acme/widgets/pulls/12":
            return {
                "number": 12,
                "html_url": "https://example.test/pulls/12",
                "head": {"ref": issue_branch, "sha": published_head},
                "state": "open",
                "merged": False,
            }
        if method == "GET" and path.endswith(".diff"):
            return expected_diff
        raise AssertionError(f"unexpected request: {method} {path}")

    client = GitHubClient("placeholder-token", request=request)
    candidate, candidate_diff = client.prepare_publication(7, "main", workspace)

    assert candidate.target_head_sha == target_head
    assert candidate.remote_head_sha == remote_issue_head
    assert candidate_diff == expected_diff
    assert set(
        git(
            "diff",
            "--name-status",
            target_head,
            candidate.head_sha,
            "--",
            cwd=workspace,
        ).splitlines()
    ) == {"A\tprior.txt", "A\tfeedback.txt"}
    assert (
        git("show", f"{candidate.head_sha}:prior.txt", cwd=workspace)
        == "prior issue work"
    )
    assert (
        git("show", f"{candidate.head_sha}:feedback.txt", cwd=workspace)
        == "feedback work"
    )
    ancestry = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            remote_issue_head,
            candidate.head_sha,
        ],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ancestry.returncode == 1
    assert git("rev-parse", f"{candidate.head_sha}^", cwd=workspace) == target_head
    assert git("rev-list", "--count", f"{target_head}..{candidate.head_sha}", cwd=workspace) == "1"
    assert (
        git("rev-parse", f"refs/heads/{issue_branch}", cwd=remote)
        == remote_issue_head
    )
    unpublished_candidate = subprocess.run(
        ["git", "cat-file", "-e", f"{candidate.head_sha}^{{commit}}"],
        cwd=remote,
        check=False,
        capture_output=True,
        text=True,
    )
    assert unpublished_candidate.returncode != 0

    amended = client.amend_publication(
        7,
        workspace,
        candidate,
        "Apply reviewed feedback",
    )
    published_head = amended.head_sha
    client.publish_prepared(
        "acme/widgets",
        7,
        "main",
        workspace,
        amended,
        existing_pr=12,
    )

    assert git("rev-parse", f"refs/heads/{issue_branch}", cwd=remote) == amended.head_sha
    assert git("rev-list", "--count", f"{target_head}..{amended.head_sha}", cwd=workspace) == "1"
    assert git("show", "-s", "--format=%s", amended.head_sha, cwd=workspace) == "Apply reviewed feedback"

@pytest.mark.parametrize(
    "remote_head_sha",
    ["4444444444444444444444444444444444444444", ""],
)
def test_publish_prepared_force_pushes_exact_candidate_with_lease_and_creates_pull_request(
    tmp_path,
    remote_head_sha,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    branch = "agent/issue-7"
    issue_ref = f"refs/heads/{branch}"
    target_head_sha = "1111111111111111111111111111111111111111"
    head_sha = "3333333333333333333333333333333333333333"
    candidate = PublicationCandidate(
        branch=branch,
        head_sha=head_sha,
        target_head_sha=target_head_sha,
        remote_head_sha=remote_head_sha,
    )
    pull_json = {
        "number": 12,
        "html_url": "https://example.test/pulls/12",
        "head": {
            "ref": branch,
            "sha": "6666666666666666666666666666666666666666",
        },
        "state": "open",
        "merged": False,
    }
    pull_diff = "diff --git a/app.py b/app.py\n+published\n"
    issue_push = [
        "git",
        "push",
        f"--force-with-lease={issue_ref}:{remote_head_sha}",
        "--set-upstream",
        "origin",
        f"{head_sha}:{issue_ref}",
    ]
    pushed = False

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "GET" and path == "/repos/acme/widgets/pulls":
            assert pushed
            return []
        if method == "POST" and path == "/repos/acme/widgets/pulls":
            assert pushed
            return {"number": 12}
        if method == "GET" and path.endswith(".diff"):
            return pull_diff
        if method == "GET" and path == "/repos/acme/widgets/pulls/12":
            return pull_json
        raise AssertionError(f"unexpected request: {method} {path}")

    def command_runner(args, *, cwd=None, env=None):
        nonlocal pushed
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{head_sha}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{target_head_sha}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ]:
            stdout = (
                f"{remote_head_sha}\t{issue_ref}\n"
                if remote_head_sha
                else ""
            )
            return SimpleNamespace(stdout=stdout)
        if args == issue_push:
            pushed = True
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    pull = client.publish_prepared(
        "acme/widgets",
        7,
        "main",
        workspace,
        candidate,
    )

    assert pull == PullRequest(
        number=12,
        url="https://example.test/pulls/12",
        branch=branch,
        state="open",
        merged=False,
        diff=pull_diff,
        head_sha=head_sha,
    )
    commands = [call[0] for call in command_calls]
    assert ["git", "rev-parse", "HEAD"] in commands
    assert ["git", "fetch", "origin", "main"] in commands
    assert ["git", "rev-parse", "origin/main"] in commands
    assert [
        "git",
        "ls-remote",
        "--heads",
        "origin",
        issue_ref,
    ] in commands
    assert [
        args
        for args in commands
        if args[:2] == ["git", "push"]
    ] == [issue_push]
    assert [call[:3] for call in request_calls] == [
        (
            "GET",
            "/repos/acme/widgets/pulls",
            {"state": "open", "per_page": 100, "page": 1},
        ),
        ("POST", "/repos/acme/widgets/pulls", None),
        ("GET", "/repos/acme/widgets/pulls/12", None),
        ("GET", "/repos/acme/widgets/pulls/12.diff", None),
    ]
    assert request_calls[1][3] == {
        "title": "Resolve issue #7",
        "head": branch,
        "base": "main",
        "body": "Closes #7",
    }
    assert all(call[1] == workspace for call in command_calls)


def test_publish_prepared_retry_resumes_after_exact_candidate_was_pushed(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    command_calls = []
    branch = "agent/issue-7"
    target_head_sha = "1111111111111111111111111111111111111111"
    head_sha = "3333333333333333333333333333333333333333"
    candidate = PublicationCandidate(
        branch=branch,
        head_sha=head_sha,
        target_head_sha=target_head_sha,
        remote_head_sha="2222222222222222222222222222222222222222",
    )
    pull_json = {
        "number": 12,
        "html_url": "https://example.test/pulls/12",
        "head": {"ref": branch, "sha": head_sha},
        "state": "open",
        "merged": False,
    }

    def request(method, path, *, query=None, json_body=None):
        if path.endswith(".diff"):
            return "already-pushed diff"
        return pull_json

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{head_sha}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{target_head_sha}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        ]:
            return SimpleNamespace(
                stdout=f"{head_sha}\trefs/heads/{branch}\n"
            )
        if args[:2] == ["git", "push"]:
            raise AssertionError("the exact candidate is already on the branch")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    pull = client.publish_prepared(
        "acme/widgets",
        7,
        "main",
        workspace,
        candidate,
        existing_pr=12,
    )

    assert pull is not None
    assert pull.head_sha == head_sha
    assert not any(
        args[:2] == ["git", "push"] for args, _, _ in command_calls
    )


def test_publish_prepared_declines_local_head_movement_without_push(tmp_path):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    candidate = PublicationCandidate(
        branch="agent/issue-7",
        head_sha="1111111111111111111111111111111111111111",
        target_head_sha="2222222222222222222222222222222222222222",
        remote_head_sha="3333333333333333333333333333333333333333",
    )

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        return {}

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(
                stdout="4444444444444444444444444444444444444444\n"
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    assert client.publish_prepared(
        "acme/widgets",
        7,
        "main",
        workspace,
        candidate,
        existing_pr=12,
    ) is None
    commands = [call[0] for call in command_calls]
    assert ["git", "rev-parse", "HEAD"] in commands
    assert not any(args[:2] == ["git", "push"] for args in commands)
    assert not any(
        method in {"POST", "PATCH", "PUT", "DELETE"}
        for method, _, _, _ in request_calls
    )


def test_publish_prepared_declines_fetched_target_movement_without_push(tmp_path):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    head_sha = "1111111111111111111111111111111111111111"
    candidate = PublicationCandidate(
        branch="agent/issue-7",
        head_sha=head_sha,
        target_head_sha="2222222222222222222222222222222222222222",
        remote_head_sha="3333333333333333333333333333333333333333",
    )

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        return {}

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{head_sha}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(
                stdout="4444444444444444444444444444444444444444\n"
            )
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ]:
            return SimpleNamespace(
                stdout=(
                    f"{candidate.remote_head_sha}"
                    "\trefs/heads/agent/issue-7\n"
                )
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    assert client.publish_prepared(
        "acme/widgets",
        7,
        "main",
        workspace,
        candidate,
        existing_pr=12,
    ) is None
    commands = [call[0] for call in command_calls]
    assert ["git", "fetch", "origin", "main"] in commands
    assert ["git", "rev-parse", "origin/main"] in commands
    assert not any(args[:2] == ["git", "push"] for args in commands)
    assert not any(
        method in {"POST", "PATCH", "PUT", "DELETE"}
        for method, _, _, _ in request_calls
    )


def test_publish_prepared_declines_remote_issue_branch_movement_without_push(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    branch = "agent/issue-7"
    head_sha = "1111111111111111111111111111111111111111"
    target_head_sha = "2222222222222222222222222222222222222222"
    candidate = PublicationCandidate(
        branch=branch,
        head_sha=head_sha,
        target_head_sha=target_head_sha,
        remote_head_sha="3333333333333333333333333333333333333333",
    )

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        return {}

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{head_sha}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{target_head_sha}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        ]:
            return SimpleNamespace(
                stdout=(
                    "4444444444444444444444444444444444444444"
                    f"\trefs/heads/{branch}\n"
                )
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    assert client.publish_prepared(
        "acme/widgets",
        7,
        "main",
        workspace,
        candidate,
        existing_pr=12,
    ) is None
    commands = [call[0] for call in command_calls]
    assert [
        "git",
        "ls-remote",
        "--heads",
        "origin",
        f"refs/heads/{branch}",
    ] in commands
    assert not any(args[:2] == ["git", "push"] for args in commands)
    assert not any(
        method in {"POST", "PATCH", "PUT", "DELETE"}
        for method, _, _, _ in request_calls
    )



def test_publish_prepared_propagates_force_with_lease_failure(tmp_path):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    branch = "agent/issue-7"
    issue_ref = f"refs/heads/{branch}"
    target_head = "1111111111111111111111111111111111111111"
    remote_head = "2222222222222222222222222222222222222222"
    candidate_head = "3333333333333333333333333333333333333333"
    candidate = PublicationCandidate(
        branch=branch,
        head_sha=candidate_head,
        target_head_sha=target_head,
        remote_head_sha=remote_head,
    )
    issue_push = [
        "git",
        "push",
        f"--force-with-lease={issue_ref}:{remote_head}",
        "--set-upstream",
        "origin",
        f"{candidate_head}:{issue_ref}",
    ]

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        raise AssertionError("failed push must not continue to GitHub publication")

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{candidate_head}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{target_head}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ]:
            return SimpleNamespace(stdout=f"{remote_head}\t{issue_ref}\n")
        if args == issue_push:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    with pytest.raises(subprocess.CalledProcessError):
        client.publish_prepared(
            "acme/widgets",
            7,
            "main",
            workspace,
            candidate,
            existing_pr=12,
        )

    assert request_calls == []
    assert [
        args
        for args, _, _ in command_calls
        if args[:2] == ["git", "push"]
    ] == [issue_push]


def test_publish_prepared_existing_pull_uses_force_with_lease(tmp_path):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    branch = "agent/issue-7"
    issue_ref = f"refs/heads/{branch}"
    target_head = "1111111111111111111111111111111111111111"
    remote_head = "2222222222222222222222222222222222222222"
    candidate_head = "3333333333333333333333333333333333333333"
    candidate = PublicationCandidate(
        branch=branch,
        head_sha=candidate_head,
        target_head_sha=target_head,
        remote_head_sha=remote_head,
    )
    issue_push = [
        "git",
        "push",
        f"--force-with-lease={issue_ref}:{remote_head}",
        "--set-upstream",
        "origin",
        f"{candidate_head}:{issue_ref}",
    ]

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "GET" and path == "/repos/acme/widgets/pulls/12":
            return {
                "number": 12,
                "html_url": "https://example.test/pulls/12",
                "head": {"ref": branch, "sha": candidate_head},
                "state": "open",
                "merged": False,
            }
        if method == "GET" and path.endswith(".diff"):
            return "updated diff"
        raise AssertionError(f"unexpected request: {method} {path}")

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{candidate_head}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{target_head}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ]:
            return SimpleNamespace(stdout=f"{remote_head}\t{issue_ref}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    pull = client.publish_prepared(
        "acme/widgets",
        7,
        "main",
        workspace,
        candidate,
        existing_pr=12,
    )
    assert pull.head_sha == candidate_head
    assert [call[:3] for call in request_calls] == [
        ("GET", "/repos/acme/widgets/pulls/12", None),
        ("GET", "/repos/acme/widgets/pulls/12.diff", None),
    ]
    assert [
        args
        for args, _, _ in command_calls
        if args[:2] == ["git", "push"]
    ] == [issue_push]



def test_publish_prepared_already_pushed_candidate_finishes_pr_without_target_revalidation_after_target_moves(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    command_calls = []
    request_calls = []
    branch = "agent/issue-7"
    issue_ref = f"refs/heads/{branch}"
    candidate_head = "1111111111111111111111111111111111111111"
    prepared_target = "2222222222222222222222222222222222222222"
    moved_target = "3333333333333333333333333333333333333333"
    candidate = PublicationCandidate(
        branch=branch,
        head_sha=candidate_head,
        target_head_sha=prepared_target,
        remote_head_sha="4444444444444444444444444444444444444444",
    )
    pull_json = {
        "number": 12,
        "html_url": "https://example.test/pulls/12",
        "head": {"ref": branch, "sha": candidate_head},
        "state": "open",
        "merged": False,
    }

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if path.endswith(".diff"):
            return "already-pushed diff"
        return pull_json

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{candidate_head}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{moved_target}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ]:
            return SimpleNamespace(stdout=f"{candidate_head}\t{issue_ref}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    pull = client.publish_prepared(
        "acme/widgets",
        7,
        "main",
        workspace,
        candidate,
        existing_pr=12,
    )

    assert pull is not None
    assert pull.head_sha == candidate_head
    commands = [args for args, _, _ in command_calls]
    assert [
        "git",
        "ls-remote",
        "--heads",
        "origin",
        issue_ref,
    ] in commands
    assert not any(args[:2] == ["git", "push"] for args in commands)
    assert not any(args[1] in {"add", "diff"} for args in commands)
    assert not any(
        args[:2] == ["git", "rev-parse"]
        and args[2] in {"origin/main", "refs/remotes/origin/main"}
        for args in commands
    )
    assert not any(
        args[:3] == ["git", "fetch", "origin"]
        and any("main" in argument for argument in args[3:])
        for args in commands
    )
    assert request_calls == [
        ("GET", "/repos/acme/widgets/pulls/12", None, None),
        ("GET", "/repos/acme/widgets/pulls/12.diff", None, None),
    ]

def test_publish_validated_to_target_sends_one_atomic_guarded_non_force_update(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    repository_id = "R_kgDOExample"
    expected_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    target_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    target_branch = "release/next"
    issue_branch = "agent/issue-7"
    issue_ref = f"refs/heads/{issue_branch}"
    target_ref = f"refs/heads/{target_branch}"

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "GET" and path == "/repos/acme/widgets":
            return {"node_id": repository_id}
        if method == "POST" and path == "/graphql":
            return {"data": {"updateRefs": {}}}
        raise AssertionError(f"unexpected request: {method} {path}")

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{expected_head}\n")
        if args == ["git", "rev-parse", f"origin/{target_branch}"]:
            return SimpleNamespace(stdout=f"{target_head}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ]:
            return SimpleNamespace(stdout=f"{expected_head}\t{issue_ref}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    assert client.publish_validated_to_target(
        "acme/widgets",
        target_branch,
        workspace,
        expected_head,
        issue_branch=issue_branch,
    )
    assert [call[0] for call in command_calls] == [
        ["git", "rev-parse", "HEAD"],
        [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ],
        ["git", "fetch", "origin", target_branch],
        ["git", "rev-parse", f"origin/{target_branch}"],
        [
            "git",
            "merge-base",
            "--is-ancestor",
            target_head,
            expected_head,
        ],
    ]
    assert request_calls[0] == (
        "GET",
        "/repos/acme/widgets",
        None,
        None,
    )
    assert request_calls[1][:3] == ("POST", "/graphql", None)
    assert len(request_calls) == 2
    graphql_body = request_calls[1][3]
    document = "".join(graphql_body["query"].split()).replace(",", "")
    assert document.count("mutation") == 1
    assert document.count("updateRefs(input:$input)") == 1
    expected_updates = [
        {
            "name": issue_ref,
            "beforeOid": expected_head,
            "afterOid": expected_head,
            "force": False,
        },
        {
            "name": target_ref,
            "beforeOid": target_head,
            "afterOid": expected_head,
            "force": False,
        },
    ]
    assert graphql_body["variables"] == {
        "input": {
            "repositoryId": repository_id,
            "refUpdates": expected_updates,
        }
    }
    assert all(update["force"] is False for update in expected_updates)
    assert all(call[1] == workspace for call in command_calls)
    assert all(call[2]["GIT_TERMINAL_PROMPT"] == "0" for call in command_calls)
    assert not any(
        args[:2] == ["git", "push"]
        for args, _, _ in command_calls
    )


def test_publish_validated_to_target_succeeds_without_mutation_when_target_contains_head(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    validated_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    integrated_target_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    target_branch = "main"
    issue_branch = "agent/issue-7"
    issue_ref = f"refs/heads/{issue_branch}"
    target_ref = f"refs/heads/{target_branch}"
    refs = {
        "HEAD": validated_head,
        f"origin/{target_branch}": integrated_target_head,
    }
    ancestors = {
        validated_head: {validated_head},
        integrated_target_head: {validated_head, integrated_target_head},
    }

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "GET" and path == "/repos/acme/widgets":
            return {"node_id": "R_kgDOUnused"}
        if method == "POST" and path == "/graphql":
            return {"data": {"updateRefs": {}}}
        raise AssertionError(f"unexpected request: {method} {path}")

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(stdout=f"{refs[args[2]]}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ]:
            return SimpleNamespace(stdout=f"{validated_head}\t{issue_ref}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            target_ref,
        ]:
            return SimpleNamespace(
                stdout=f"{integrated_target_head}\t{target_ref}\n"
            )
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            ancestor = refs.get(args[3], args[3])
            descendant = refs.get(args[4], args[4])
            if ancestor not in ancestors[descendant]:
                raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    assert client.publish_validated_to_target(
        "acme/widgets",
        target_branch,
        workspace,
        validated_head,
        issue_branch=issue_branch,
    )
    assert request_calls == []
    assert not any(
        args[:2] == ["git", "push"]
        or args[:2] == ["git", "update-ref"]
        for args, _, _ in command_calls
    )




def test_publish_validated_to_target_declines_divergent_target_without_push(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    expected_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    target_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    issue_branch = "agent/issue-7"
    issue_ref = f"refs/heads/{issue_branch}"

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        raise AssertionError("divergent publication must not call GitHub")

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{expected_head}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{target_head}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ]:
            return SimpleNamespace(stdout=f"{expected_head}\t{issue_ref}\n")
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    assert not client.publish_validated_to_target(
        "acme/widgets",
        "main",
        workspace,
        expected_head,
        issue_branch=issue_branch,
    )
    assert request_calls == []
    assert not any(args[1] == "push" for args, _, _ in command_calls)
    assert not any(
        argument.startswith("--force")
        for args, _, _ in command_calls
        for argument in args
    )


def test_publish_validated_to_target_rejects_expected_local_head_mismatch(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    command_calls = []
    expected_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    local_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        assert args == ["git", "rev-parse", "HEAD"]
        return SimpleNamespace(stdout=f"{local_head}\n")

    client = GitHubClient(
        "placeholder-token",
        command_runner=command_runner,
    )

    with pytest.raises(RuntimeError, match="validated head"):
        client.publish_validated_to_target(
            "acme/widgets",
            "release/next",
            workspace,
            expected_head,
            issue_branch="agent/issue-7",
        )

    assert [call[0] for call in command_calls] == [
        ["git", "rev-parse", "HEAD"],
    ]
    assert not any(args[1] == "push" for args, _, _ in command_calls)
    assert not any(
        argument.startswith("--force")
        for args, _, _ in command_calls
        for argument in args
    )



def test_publish_validated_to_target_raises_for_malformed_update_refs_response(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    expected_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    target_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    issue_branch = "agent/issue-7"
    issue_ref = f"refs/heads/{issue_branch}"

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "GET" and path == "/repos/acme/widgets":
            return {"node_id": "R_kgDOExample"}
        if method == "POST" and path == "/graphql":
            return {"data": {"updateRefs": []}}
        raise AssertionError(f"unexpected request: {method} {path}")

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{expected_head}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{target_head}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ]:
            return SimpleNamespace(stdout=f"{expected_head}\t{issue_ref}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    with pytest.raises(RuntimeError):
        client.publish_validated_to_target(
            "acme/widgets",
            "main",
            workspace,
            expected_head,
            issue_branch=issue_branch,
        )

    assert [call[:3] for call in request_calls] == [
        ("GET", "/repos/acme/widgets", None),
        ("POST", "/graphql", None),
    ]
    assert not any(
        args[:2] == ["git", "push"]
        for args, _, _ in command_calls
    )


def test_publish_validated_to_target_returns_false_when_update_refs_is_rejected(
    tmp_path,
):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    command_calls = []
    expected_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    target_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    issue_branch = "agent/issue-7"
    issue_ref = f"refs/heads/{issue_branch}"

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "GET" and path == "/repos/acme/widgets":
            return {"node_id": "R_kgDOExample"}
        if method == "POST" and path == "/graphql":
            return {
                "errors": [
                    {
                        "type": "UNPROCESSABLE",
                        "message": "one or more refs did not match beforeOid",
                    }
                ]
            }
        raise AssertionError("rejected publication must not continue")

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{expected_head}\n")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(stdout=f"{target_head}\n")
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            issue_ref,
        ]:
            return SimpleNamespace(stdout=f"{expected_head}\t{issue_ref}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    assert not client.publish_validated_to_target(
        "acme/widgets",
        "main",
        workspace,
        expected_head,
        issue_branch=issue_branch,
    )
    assert [call[:3] for call in request_calls] == [
        ("GET", "/repos/acme/widgets", None),
        ("POST", "/graphql", None),
    ]
    assert not any(
        args[:2] == ["git", "push"]
        for args, _, _ in command_calls
    )



def test_repeated_publication_keeps_one_issue_commit_on_the_remote_branch(tmp_path):
    remote = tmp_path / "remote.git"
    workspace = tmp_path / "workspace"
    upstream = tmp_path / "upstream"

    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(workspace))
    (workspace / "base.txt").write_text("base\n")
    git("add", "--all", cwd=workspace)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Base",
        cwd=workspace,
    )
    git("remote", "add", "origin", str(remote), cwd=workspace)
    git("push", "--set-upstream", "origin", "main", cwd=workspace)
    issue_ref = "refs/heads/agent/issue-7"

    def request(method, path, *, query=None, json_body=None):
        if path.endswith(".diff"):
            return "updated diff"
        if method == "GET" and path == "/repos/acme/widgets/pulls":
            return []
        if method == "POST":
            return {"number": 12}
        return {
            "number": 12,
            "html_url": "https://example.test/pulls/12",
            "head": {
                "ref": "agent/issue-7",
                "sha": git("--git-dir", str(remote), "rev-parse", issue_ref),
            },
            "state": "open",
            "merged": False,
        }

    client = GitHubClient("placeholder-token", request=request)
    (workspace / "feature.txt").write_text("first\n")
    first = client.publish(
        "acme/widgets",
        7,
        "main",
        workspace,
    )
    git("update-ref", "-d", f"refs/remotes/origin/agent/issue-7", cwd=workspace)
    git("clone", "--branch", "main", str(remote), str(upstream))
    (upstream / "base.txt").write_text("base\nupstream\n")
    git("add", "--all", cwd=upstream)
    git(
        "-c",
        "user.name=Upstream",
        "-c",
        "user.email=upstream@example.invalid",
        "commit",
        "-m",
        "Advance main",
        cwd=upstream,
    )
    git("push", "origin", "main", cwd=upstream)

    (workspace / "feature.txt").write_text("first\nsecond\n")
    second = client.publish(
        "acme/widgets",
        7,
        "main",
        workspace,
        existing_pr=12,
    )

    assert first.head_sha != second.head_sha
    assert git(
        "--git-dir",
        str(remote),
        "rev-list",
        "--count",
        f"refs/heads/main..{issue_ref}",
    ) == "1"
    assert git("--git-dir", str(remote), "rev-parse", f"{issue_ref}^") == git(
        "--git-dir",
        str(remote),
        "rev-parse",
        "refs/heads/main",
    )
    assert git(
        "--git-dir",
        str(remote),
        "show",
        f"{issue_ref}:feature.txt",
    ) == "first\nsecond"
    assert git(
        "--git-dir",
        str(remote),
        "show",
        f"{issue_ref}:base.txt",
    ) == "base\nupstream"
    assert git(
        "--git-dir",
        str(remote),
        "log",
        "-1",
        "--pretty=%s",
        issue_ref,
    ) == "Resolve issue #7"


def test_publish_uses_local_pushed_head_when_pull_api_is_stale(tmp_path):
    request_calls = []
    command_calls = []
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    local_head_sha = "8888888888888888888888888888888888888888"
    remote_head_sha = "2222222222222222222222222222222222222222"
    pull_json = {
        "number": 12,
        "html_url": "https://example.test/pulls/12",
        "head": {
            "ref": "agent/issue-7",
            "sha": "3333333333333333333333333333333333333333",
        },
        "state": "open",
        "merged": False,
    }
    diff = "diff --git a/app.py b/app.py\n"

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "GET" and path == "/repos/acme/widgets/pulls":
            return []
        if method == "POST":
            return {"number": 12}
        if path.endswith(".diff"):
            return diff
        return pull_json

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ]:
            return SimpleNamespace(
                stdout=f"{remote_head_sha}\trefs/heads/agent/issue-7\n"
            )
        if args == ["git", "diff", "--cached", "--name-only"]:
            return SimpleNamespace(stdout="app.py\n")
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{local_head_sha}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    assert client.publish(
        "acme/widgets",
        7,
        "main",
        workspace,
        existing_pr=12,
    ) == PullRequest(
        12,
        "https://example.test/pulls/12",
        "agent/issue-7",
        "open",
        False,
        diff,
        local_head_sha,
    )
    assert [call[0] for call in command_calls] == [
        ["git", "checkout", "-B", "agent/issue-7"],
        ["git", "fetch", "origin", "main"],
        [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ],
        ["git", "fetch", "origin", "refs/heads/agent/issue-7"],
        ["git", "add", "--all"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "commit", "-m", "Resolve issue #7"],
        ["git", "rebase", "origin/main"],
        ["git", "rev-parse", "HEAD"],
        ["git", "reset", "--soft", "origin/main"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "commit", "-m", "Resolve issue #7"],
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/agent/issue-7:{remote_head_sha}",
            "--set-upstream",
            "origin",
            "agent/issue-7",
        ],
        ["git", "rev-parse", "HEAD"],
    ]
    assert all(call[1] == workspace for call in command_calls)
    assert all(call[2]["GIT_TERMINAL_PROMPT"] == "0" for call in command_calls)
    assert request_calls == [
        ("GET", "/repos/acme/widgets/pulls/12", None, None),
        ("GET", "/repos/acme/widgets/pulls/12.diff", None, None),
    ]
    assert "placeholder-token" not in repr(command_calls)


def test_publish_pushes_the_issue_branch_without_duplicating_an_existing_pr(tmp_path):
    request_calls = []
    command_calls = []
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    local_head_sha = "6666666666666666666666666666666666666666"
    pull_json = {
        "number": 12,
        "html_url": "https://example.test/pulls/12",
        "head": {
            "ref": "agent/issue-7",
            "sha": "4444444444444444444444444444444444444444",
        },
        "state": "open",
        "merged": False,
    }

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "POST":
            raise AssertionError("an existing pull request must not be recreated")
        if path.endswith(".diff"):
            return "updated diff"
        return pull_json

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ]:
            return SimpleNamespace(
                stdout="4444444444444444444444444444444444444444"
                "\trefs/heads/agent/issue-7\n"
            )
        if args == ["git", "diff", "--cached", "--name-only"]:
            return SimpleNamespace(stdout="app.py\n")
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{local_head_sha}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    pull = client.publish(
        "acme/widgets",
        7,
        "main",
        workspace,
        existing_pr=12,
    )

    assert pull.number == 12
    assert pull.branch == "agent/issue-7"
    assert [call[0] for call in command_calls] == [
        ["git", "checkout", "-B", "agent/issue-7"],
        ["git", "fetch", "origin", "main"],
        [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ],
        ["git", "fetch", "origin", "refs/heads/agent/issue-7"],
        ["git", "add", "--all"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "commit", "-m", "Resolve issue #7"],
        ["git", "rebase", "origin/main"],
        ["git", "rev-parse", "HEAD"],
        ["git", "reset", "--soft", "origin/main"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "commit", "-m", "Resolve issue #7"],
        [
            "git",
            "push",
            "--force-with-lease=refs/heads/agent/issue-7:"
            "4444444444444444444444444444444444444444",
            "--set-upstream",
            "origin",
            "agent/issue-7",
        ],
        ["git", "rev-parse", "HEAD"],
    ]
    assert request_calls == [
        ("GET", "/repos/acme/widgets/pulls/12", None, None),
        ("GET", "/repos/acme/widgets/pulls/12.diff", None, None),
    ]


def test_initial_publish_reuses_an_open_issue_branch_pull_request(tmp_path):
    request_calls = []
    command_calls = []
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    local_head_sha = "6666666666666666666666666666666666666666"
    pull_json = {
        "number": 12,
        "html_url": "https://example.test/pulls/12",
        "head": {
            "ref": "agent/issue-7",
            "sha": "5555555555555555555555555555555555555555",
        },
        "state": "open",
        "merged": False,
    }

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "POST":
            raise AssertionError("the open issue-branch pull request must be reused")
        if path == "/repos/acme/widgets/pulls":
            return [
                {
                    "number": 11,
                    "head": {
                        "ref": "agent/issue-6",
                        "repo": {"full_name": "acme/widgets"},
                    },
                    "base": {"ref": "main"},
                },
                {
                    "number": 12,
                    "head": {
                        "ref": "agent/issue-7",
                        "repo": {"full_name": "acme/widgets"},
                    },
                    "base": {"ref": "main"},
                },
            ]
        if path.endswith(".diff"):
            return "existing diff"
        return pull_json

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ]:
            return SimpleNamespace(
                stdout="5555555555555555555555555555555555555555"
                "\trefs/heads/agent/issue-7\n"
            )
        if args == ["git", "diff", "--cached", "--name-only"]:
            return SimpleNamespace(stdout="app.py\n")
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{local_head_sha}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    pull = client.publish("acme/widgets", 7, "main", workspace)

    assert pull == PullRequest(
        12,
        "https://example.test/pulls/12",
        "agent/issue-7",
        "open",
        False,
        "existing diff",
        local_head_sha,
    )
    assert [call[0] for call in command_calls] == [
        ["git", "checkout", "-B", "agent/issue-7"],
        ["git", "fetch", "origin", "main"],
        [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ],
        ["git", "fetch", "origin", "refs/heads/agent/issue-7"],
        ["git", "add", "--all"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "commit", "-m", "Resolve issue #7"],
        ["git", "rebase", "origin/main"],
        ["git", "rev-parse", "HEAD"],
        ["git", "reset", "--soft", "origin/main"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "commit", "-m", "Resolve issue #7"],
        [
            "git",
            "push",
            "--force-with-lease=refs/heads/agent/issue-7:"
            "5555555555555555555555555555555555555555",
            "--set-upstream",
            "origin",
            "agent/issue-7",
        ],
        ["git", "rev-parse", "HEAD"],
    ]
    assert request_calls == [
        (
            "GET",
            "/repos/acme/widgets/pulls",
            {"state": "open", "per_page": 100, "page": 1},
            None,
        ),
        ("GET", "/repos/acme/widgets/pulls/12", None, None),
        ("GET", "/repos/acme/widgets/pulls/12.diff", None, None),
    ]


def test_initial_publish_requires_matching_base_and_supplied_head_repository(tmp_path):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    request_calls = []
    pull_json = {
        "number": 15,
        "html_url": "https://example.test/pulls/15",
        "head": {
            "ref": "agent/issue-7",
            "sha": "6666666666666666666666666666666666666666",
        },
        "state": "open",
        "merged": False,
    }

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if method == "POST":
            raise AssertionError("the matching open pull request must be reused")
        if path == "/repos/acme/widgets/pulls":
            return [
                {
                    "number": 13,
                    "head": {
                        "ref": "agent/issue-7",
                        "repo": {"full_name": "acme/widgets"},
                    },
                    "base": {"ref": "release"},
                },
                {
                    "number": 14,
                    "head": {
                        "ref": "agent/issue-7",
                        "repo": {"full_name": "other/widgets"},
                    },
                    "base": {"ref": "main"},
                },
                {
                    "number": 15,
                    "head": {"ref": "agent/issue-7"},
                    "base": {"ref": "main"},
                },
            ]
        if path.endswith(".diff"):
            return "existing diff"
        return pull_json

    def command_runner(args, *, cwd=None, env=None):
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(
                stdout="6666666666666666666666666666666666666666\n"
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    pull = client.publish("acme/widgets", 7, "main", workspace)

    assert pull.number == 15
    assert request_calls == [
        (
            "GET",
            "/repos/acme/widgets/pulls",
            {"state": "open", "per_page": 100, "page": 1},
            None,
        ),
        ("GET", "/repos/acme/widgets/pulls/15", None, None),
        ("GET", "/repos/acme/widgets/pulls/15.diff", None, None),
    ]


def test_publish_with_no_staged_changes_skips_commit_but_still_pushes(tmp_path):
    request_calls = []
    command_calls = []
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    pull_json = {
        "number": 12,
        "html_url": "https://example.test/pulls/12",
        "head": {
            "ref": "agent/issue-7",
            "sha": "7777777777777777777777777777777777777777",
        },
        "state": "open",
        "merged": False,
    }

    def request(method, path, *, query=None, json_body=None):
        request_calls.append((method, path, query, json_body))
        if path.endswith(".diff"):
            return "existing diff"
        return pull_json

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ]:
            return SimpleNamespace(
                stdout="7777777777777777777777777777777777777777"
                "\trefs/heads/agent/issue-7\n"
            )
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(
                stdout="7777777777777777777777777777777777777777\n"
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    pull = client.publish(
        "acme/widgets",
        7,
        "main",
        workspace,
        existing_pr=12,
    )

    assert pull.number == 12
    assert [call[0] for call in command_calls] == [
        ["git", "checkout", "-B", "agent/issue-7"],
        ["git", "fetch", "origin", "main"],
        [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ],
        ["git", "fetch", "origin", "refs/heads/agent/issue-7"],
        ["git", "add", "--all"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "rebase", "origin/main"],
        ["git", "rev-parse", "HEAD"],
        [
            "git",
            "push",
            "--force-with-lease=refs/heads/agent/issue-7:"
            "7777777777777777777777777777777777777777",
            "--set-upstream",
            "origin",
            "agent/issue-7",
        ],
        ["git", "rev-parse", "HEAD"],
    ]
    assert request_calls == [
        ("GET", "/repos/acme/widgets/pulls/12", None, None),
        ("GET", "/repos/acme/widgets/pulls/12.diff", None, None),
    ]



def test_publish_retry_preserves_remote_head_when_local_head_is_unchanged(
    tmp_path,
):
    command_calls = []
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    prior_remote_head = "6666666666666666666666666666666666666666"
    pushed_head = "7777777777777777777777777777777777777777"
    publish_attempt = 0

    def request(method, path, *, query=None, json_body=None):
        if path.endswith(".diff"):
            return "existing diff"
        return {
            "number": 12,
            "html_url": "https://example.test/pulls/12",
            "head": {
                "ref": "agent/issue-7",
                "sha": pushed_head,
            },
            "state": "open",
            "merged": False,
        }

    def command_runner(args, *, cwd=None, env=None):
        nonlocal publish_attempt
        command_calls.append((args, cwd, env))
        if args == [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent/issue-7",
        ]:
            publish_attempt += 1
            remote_head = (
                prior_remote_head if publish_attempt == 1 else pushed_head
            )
            return SimpleNamespace(
                stdout=f"{remote_head}\trefs/heads/agent/issue-7\n"
            )
        if args == ["git", "diff", "--cached", "--name-only"]:
            return SimpleNamespace(
                stdout="app.py\n" if publish_attempt == 1 else ""
            )
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{pushed_head}\n")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    first = client.publish(
        "acme/widgets",
        7,
        "main",
        workspace,
        existing_pr=12,
    )
    command_calls.clear()

    second = client.publish(
        "acme/widgets",
        7,
        "main",
        workspace,
        existing_pr=12,
    )

    assert first.head_sha == pushed_head
    assert second.head_sha == first.head_sha
    retry_commands = [call[0] for call in command_calls]
    assert ["git", "reset", "--soft", "origin/main"] not in retry_commands
    assert not any(command[1] == "commit" for command in retry_commands)


def test_list_feedback_uses_inline_comments_and_request_changes_state_only():
    calls = []
    service_marker = "<!-- repogents-feedback:inline:101 -->"
    responses = {
        "/repos/acme/widgets/pulls/12/comments": [
            {
                "id": 101,
                "node_id": "PRRC_inline_101",
                "body": "Use the shared helper",
                "path": "src/app.py",
                "line": 14,
            },
            {
                "id": 102,
                "node_id": "PRRC_inline_102",
                "in_reply_to_id": 101,
                "body": "This old line also needs correction",
                "path": "src/old.py",
                "line": None,
            },
            {
                "id": 103,
                "node_id": "PRRC_service_reply",
                "in_reply_to_id": 101,
                "body": (
                    "Addressed in validated commit "
                    "`aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`.\n\n"
                    f"{service_marker}"
                ),
                "path": "src/app.py",
                "line": 14,
            },
        ],
        "/repos/acme/widgets/pulls/12/reviews": [
            {
                "id": 201,
                "state": "CHANGES_REQUESTED",
                "body": "Please address the review",
            },
            {
                "id": 202,
                "state": "APPROVED",
                "body": "Approved",
            },
            {
                "id": 203,
                "state": "COMMENTED",
                "body": "This still needs work",
            },
        ],
        "/repos/acme/widgets/issues/12/comments": [
            {"id": 301, "body": "This NEEDS WORK before release."},
            {"id": 302, "body": "Looks good to me."},
            {
                "id": 303,
                "body": (
                    "Addressed in validated commit "
                    "`aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`.\n\n"
                    "<!-- repogents-feedback:comment:301 -->"
                ),
            },
        ],
    }

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        if path == "/graphql":
            operation = json_body["query"].lstrip().split()[1].split("(")[0]
            assert operation == "ReviewThreads"
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "PRRT_shared_thread",
                                        "isResolved": False,
                                        "viewerCanResolve": True,
                                        "comments": {
                                            "nodes": [
                                                {"id": "PRRC_inline_101"},
                                                {"id": "PRRC_inline_102"},
                                                {"id": "PRRC_service_reply"},
                                            ],
                                            "pageInfo": {
                                                "hasNextPage": False,
                                                "endCursor": None,
                                            },
                                        },
                                    }
                                ],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            }
                        }
                    }
                }
            }
        return responses[path]

    client = GitHubClient("placeholder-token", request=request)

    assert client.list_feedback("acme/widgets", 12) == [
        GitHubFeedback(
            "inline:101",
            "inline",
            "Use the shared helper",
            "src/app.py",
            14,
            "PRRT_shared_thread",
            101,
        ),
        GitHubFeedback(
            "inline:102",
            "inline",
            "This old line also needs correction",
            "src/old.py",
            None,
            "PRRT_shared_thread",
            101,
        ),
        GitHubFeedback(
            "review:201",
            "review",
            "Please address the review",
        ),
        GitHubFeedback(
            "comment:301",
            "comment",
            "This NEEDS WORK before release.",
        ),
        GitHubFeedback(
            "comment:302",
            "comment",
            "Looks good to me.",
        ),
    ]
    assert calls[:3] == [
        (
            "GET",
            "/repos/acme/widgets/pulls/12/comments",
            {"per_page": 100, "page": 1},
            None,
        ),
        (
            "GET",
            "/repos/acme/widgets/pulls/12/reviews",
            {"per_page": 100, "page": 1},
            None,
        ),
        (
            "GET",
            "/repos/acme/widgets/issues/12/comments",
            {"per_page": 100, "page": 1},
            None,
        ),
    ]
    assert len(calls) == 4
    graphql_call = calls[3]
    assert graphql_call[:3] == ("POST", "/graphql", None)
    assert graphql_call[3]["variables"] == {
        "owner": "acme",
        "name": "widgets",
        "number": 12,
        "after": None,
    }
    document = "".join(graphql_call[3]["query"].split()).replace(",", "")
    assert "repository(owner:$ownername:$name)" in document
    assert "pullRequest(number:$number)" in document
    assert "reviewThreads(first:100after:$after)" in document
    assert "comments(first:100)" in document
    for field in (
        "id",
        "isResolved",
        "viewerCanResolve",
        "pageInfo",
        "hasNextPage",
        "endCursor",
    ):
        assert field in document


def test_list_feedback_paginates_all_pull_request_feedback():
    calls = []
    inline_path = "/repos/acme/widgets/pulls/12/comments"
    review_path = "/repos/acme/widgets/pulls/12/reviews"
    conversation_path = "/repos/acme/widgets/issues/12/comments"
    pages = {
        (inline_path, 1): [
            {
                "id": number,
                "node_id": f"PRRC_inline_{number}",
                "body": f"Inline {number}",
                "path": f"src/{number}.py",
                "line": number,
            }
            for number in range(1, 101)
        ],
        (inline_path, 2): [
            {
                "id": 101,
                "node_id": "PRRC_inline_101",
                "body": "Inline 101",
                "path": "src/101.py",
                "line": 101,
            }
        ],
        (review_path, 1): [
            {"id": number, "state": "APPROVED", "body": "Approved"}
            for number in range(1, 100)
        ]
        + [
            {
                "id": 200,
                "state": "CHANGES_REQUESTED",
                "body": "First requested change",
            }
        ],
        (review_path, 2): [
            {
                "id": 201,
                "state": "CHANGES_REQUESTED",
                "body": "Second requested change",
            },
            {"id": 202, "state": "APPROVED", "body": "Approved"},
        ],
        (conversation_path, 1): [
            {"id": number, "body": "Discussion only"} for number in range(1, 100)
        ]
        + [{"id": 300, "body": "This NEEDS WORK."}],
        (conversation_path, 2): [
            {"id": 301, "body": "This NeEdS WoRk too."},
            {"id": 302, "body": "Looks good."},
        ],
    }
    no_more_pages = {"hasNextPage": False, "endCursor": None}
    thread_a = {
        "id": "PRRT_thread_a",
        "isResolved": False,
        "viewerCanResolve": True,
        "comments": {
            "nodes": [
                *({"id": f"PRRC_inline_{number}"} for number in range(1, 100)),
                {"id": "PRRC_unrelated_comment"},
            ],
            "pageInfo": {
                "hasNextPage": True,
                "endCursor": "thread-a-comments-page-1",
            },
        },
    }
    irrelevant_threads = [
        {
            "id": f"PRRT_irrelevant_{number}",
            "isResolved": False,
            "viewerCanResolve": True,
            "comments": {
                "nodes": [{"id": f"PRRC_irrelevant_{number}"}],
                "pageInfo": no_more_pages,
            },
        }
        for number in range(2, 101)
    ]
    thread_b = {
        "id": "PRRT_thread_b",
        "isResolved": False,
        "viewerCanResolve": True,
        "comments": {
            "nodes": [{"id": "PRRC_inline_101"}],
            "pageInfo": no_more_pages,
        },
    }

    def review_threads_response(nodes, *, has_next_page, end_cursor):
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": has_next_page,
                                "endCursor": end_cursor,
                            },
                        }
                    }
                }
            }
        }

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        if path != "/graphql":
            return pages[(path, query["page"])]
        operation = json_body["query"].lstrip().split()[1].split("(")[0]
        variables = json_body["variables"]
        if operation == "ReviewThreads":
            if variables["after"] is None:
                return review_threads_response(
                    [thread_a, *irrelevant_threads],
                    has_next_page=True,
                    end_cursor="review-threads-page-1",
                )
            assert variables["after"] == "review-threads-page-1"
            return review_threads_response(
                [thread_b],
                has_next_page=False,
                end_cursor=None,
            )
        if operation == "ReviewThreadComments":
            assert variables == {
                "threadId": "PRRT_thread_a",
                "after": "thread-a-comments-page-1",
            }
            return {
                "data": {
                    "node": {
                        "id": "PRRT_thread_a",
                        "comments": {
                            "nodes": [{"id": "PRRC_inline_100"}],
                            "pageInfo": no_more_pages,
                        },
                    }
                }
            }
        raise AssertionError(f"unexpected GraphQL operation: {operation}")

    client = GitHubClient("placeholder-token", request=request)

    feedback = client.list_feedback("acme/widgets", 12)

    assert [item.external_id for item in feedback] == [
        *(f"inline:{number}" for number in range(1, 102)),
        "review:200",
        "review:201",
        *(f"comment:{number}" for number in range(1, 100)),
        "comment:300",
        "comment:301",
        "comment:302",
    ]
    assert feedback[99] == GitHubFeedback(
        "inline:100",
        "inline",
        "Inline 100",
        "src/100.py",
        100,
        "PRRT_thread_a",
        100,
    )
    assert feedback[100] == GitHubFeedback(
        "inline:101",
        "inline",
        "Inline 101",
        "src/101.py",
        101,
        "PRRT_thread_b",
        101,
    )
    rest_calls = calls[:6]
    assert [(call[1], call[2]["page"]) for call in rest_calls] == [
        (inline_path, 1),
        (inline_path, 2),
        (review_path, 1),
        (review_path, 2),
        (conversation_path, 1),
        (conversation_path, 2),
    ]
    assert all(call[2]["per_page"] == 100 for call in rest_calls)
    graphql_calls = calls[6:]
    assert [
        call[3]["query"].lstrip().split()[1].split("(")[0]
        for call in graphql_calls
    ] == [
        "ReviewThreads",
        "ReviewThreadComments",
        "ReviewThreads",
    ]
    assert [call[:3] for call in graphql_calls] == [
        ("POST", "/graphql", None),
        ("POST", "/graphql", None),
        ("POST", "/graphql", None),
    ]
    assert [call[3]["variables"] for call in graphql_calls] == [
        {
            "owner": "acme",
            "name": "widgets",
            "number": 12,
            "after": None,
        },
        {
            "threadId": "PRRT_thread_a",
            "after": "thread-a-comments-page-1",
        },
        {
            "owner": "acme",
            "name": "widgets",
            "number": 12,
            "after": "review-threads-page-1",
        },
    ]
    outer_document = "".join(graphql_calls[0][3]["query"].split()).replace(",", "")
    assert "reviewThreads(first:100after:$after)" in outer_document
    assert "comments(first:100)" in outer_document
    inner_document = "".join(graphql_calls[1][3]["query"].split()).replace(",", "")
    assert "node(id:$threadId)" in inner_document
    assert "...onPullRequestReviewThread" in inner_document
    assert "comments(first:100after:$after)" in inner_document
    for field in ("id", "pageInfo", "hasNextPage", "endCursor"):
        assert field in inner_document


def test_address_feedback_replies_to_inline_top_level_before_resolving_thread():
    calls = []
    head_sha = "8888888888888888888888888888888888888888"
    marker = "<!-- repogents-feedback:inline:101 -->"
    acknowledgement = f"Addressed in validated commit `{head_sha}`.\n\n{marker}"
    comments_path = "/repos/acme/widgets/pulls/12/comments"
    reply_path = f"{comments_path}/77/replies"
    response_url = "https://example.test/pulls/12#discussion_r901"

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        if method == "GET" and path == comments_path:
            return []
        if method == "POST" and path == reply_path:
            return {
                "id": 901,
                "body": acknowledgement,
                "html_url": response_url,
            }
        if method == "POST" and path == "/graphql":
            operation = json_body["query"].lstrip().split()[1].split("(")[0]
            if operation == "ReviewThread":
                return {
                    "data": {
                        "node": {
                            "id": "PRRT_inline_101",
                            "isResolved": False,
                            "viewerCanResolve": True,
                        }
                    }
                }
            if operation == "ResolveThread":
                return {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {
                                "id": "PRRT_inline_101",
                                "isResolved": True,
                            }
                        }
                    }
                }
        raise AssertionError(f"unexpected request: {method} {path}")

    client = GitHubClient("placeholder-token", request=request)
    feedback = GitHubFeedback(
        external_id="inline:101",
        kind="inline",
        body="Use the shared helper",
        path="src/app.py",
        line=14,
        review_thread_id="PRRT_inline_101",
        top_level_comment_id=77,
    )

    assert client.address_feedback(
        "acme/widgets",
        12,
        feedback,
        head_sha,
    ) == FeedbackAddress("RESOLVED", response_url)
    assert [call[:3] for call in calls] == [
        (
            "GET",
            comments_path,
            {"per_page": 100, "page": 1},
        ),
        ("POST", reply_path, None),
        ("POST", "/graphql", None),
        ("POST", "/graphql", None),
    ]
    assert calls[0][3] is None
    assert calls[1][3] == {"body": acknowledgement}
    state_body = calls[2][3]
    assert state_body["variables"] == {"threadId": "PRRT_inline_101"}
    assert state_body["query"].lstrip().split()[1].split("(")[0] == "ReviewThread"
    state_document = "".join(state_body["query"].split()).replace(",", "")
    assert "node(id:$threadId)" in state_document
    assert "...onPullRequestReviewThread" in state_document
    for field in ("id", "isResolved", "viewerCanResolve"):
        assert field in state_document
    resolution_body = calls[3][3]
    assert resolution_body["variables"] == {
        "input": {"threadId": "PRRT_inline_101"}
    }
    assert (
        resolution_body["query"].lstrip().split()[1].split("(")[0]
        == "ResolveThread"
    )
    resolution_document = "".join(resolution_body["query"].split()).replace(",", "")
    assert "resolveReviewThread(input:$input)" in resolution_document
    assert "thread{idisResolved}" in resolution_document


@pytest.mark.parametrize(
    ("kind", "external_id"),
    [
        ("review", "review:201"),
        ("comment", "comment:301"),
    ],
)
def test_address_feedback_acknowledges_non_thread_feedback_without_resolution(
    kind,
    external_id,
):
    calls = []
    head_sha = "9999999999999999999999999999999999999999"
    marker = f"<!-- repogents-feedback:{external_id} -->"
    acknowledgement = f"Addressed in validated commit `{head_sha}`.\n\n{marker}"
    comments_path = "/repos/acme/widgets/issues/12/comments"
    response_url = "https://example.test/pulls/12#issuecomment-902"

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        if method == "GET" and path == comments_path:
            return []
        if method == "POST" and path == comments_path:
            return {
                "id": 902,
                "body": acknowledgement,
                "html_url": response_url,
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    client = GitHubClient("placeholder-token", request=request)
    feedback = GitHubFeedback(
        external_id=external_id,
        kind=kind,
        body="Please address this feedback",
    )

    assert client.address_feedback(
        "acme/widgets",
        12,
        feedback,
        head_sha,
    ) == FeedbackAddress("ACKNOWLEDGED", response_url)
    assert calls == [
        (
            "GET",
            comments_path,
            {"per_page": 100, "page": 1},
            None,
        ),
        (
            "POST",
            comments_path,
            None,
            {"body": acknowledgement},
        ),
    ]


def test_address_feedback_rejects_mismatched_comment_external_id():
    calls = []

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        return []

    client = GitHubClient("placeholder-token", request=request)
    feedback = GitHubFeedback(
        external_id="review:301",
        kind="comment",
        body="This NEEDS WORK before release.",
    )

    with pytest.raises(ValueError):
        client.address_feedback(
            "acme/widgets",
            12,
            feedback,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

    assert calls == []



@pytest.mark.parametrize("already_resolved", [False, True])
def test_address_feedback_retry_reuses_marker_and_reconciles_inline_resolution(
    already_resolved,
):
    calls = []
    head_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    marker = "<!-- repogents-feedback:inline:101 -->"
    acknowledgement = f"Addressed in validated commit `{head_sha}`.\n\n{marker}"
    comments_path = "/repos/acme/widgets/pulls/12/comments"
    reply_path = f"{comments_path}/77/replies"
    response_url = "https://example.test/pulls/12#discussion_r901"
    first_page = [
        {
            "id": number,
            "body": f"Unrelated review comment {number}",
            "html_url": f"https://example.test/comments/{number}",
        }
        for number in range(1, 101)
    ]

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        if method == "GET" and path == comments_path:
            if query["page"] == 1:
                return first_page
            assert query["page"] == 2
            return [
                {
                    "id": 901,
                    "body": acknowledgement,
                    "html_url": response_url,
                }
            ]
        if path == reply_path:
            raise AssertionError("retry must not post a duplicate acknowledgement")
        if method == "POST" and path == "/graphql":
            operation = json_body["query"].lstrip().split()[1].split("(")[0]
            if operation == "ReviewThread":
                return {
                    "data": {
                        "node": {
                            "id": "PRRT_inline_101",
                            "isResolved": already_resolved,
                            "viewerCanResolve": True,
                        }
                    }
                }
            if operation == "ResolveThread" and not already_resolved:
                return {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {
                                "id": "PRRT_inline_101",
                                "isResolved": True,
                            }
                        }
                    }
                }
        raise AssertionError(f"unexpected request: {method} {path}")

    client = GitHubClient("placeholder-token", request=request)
    feedback = GitHubFeedback(
        external_id="inline:101",
        kind="inline",
        body="Use the shared helper",
        path="src/app.py",
        line=14,
        review_thread_id="PRRT_inline_101",
        top_level_comment_id=77,
    )

    assert client.address_feedback(
        "acme/widgets",
        12,
        feedback,
        head_sha,
    ) == FeedbackAddress("RESOLVED", response_url)
    assert calls[:2] == [
        (
            "GET",
            comments_path,
            {"per_page": 100, "page": 1},
            None,
        ),
        (
            "GET",
            comments_path,
            {"per_page": 100, "page": 2},
            None,
        ),
    ]
    assert all(call[1] != reply_path for call in calls)
    graphql_calls = calls[2:]
    assert [
        call[3]["query"].lstrip().split()[1].split("(")[0]
        for call in graphql_calls
    ] == (
        ["ReviewThread"]
        if already_resolved
        else ["ReviewThread", "ResolveThread"]
    )
    assert all(call[:3] == ("POST", "/graphql", None) for call in graphql_calls)
    assert graphql_calls[0][3]["variables"] == {
        "threadId": "PRRT_inline_101"
    }
    if not already_resolved:
        assert graphql_calls[1][3]["variables"] == {
            "input": {"threadId": "PRRT_inline_101"}
        }


def test_resolve_feedback_without_code_reuses_reply_and_inline_resolution():
    calls = []
    comments_path = "/repos/acme/widgets/pulls/12/comments"
    reply_path = f"{comments_path}/77/replies"
    response_url = "https://example.test/pulls/12#discussion_r901"
    response = (
        "No branch change is needed. Current-head evidence shows the reported "
        "case is already handled."
    )
    marker = "<!-- repogents-feedback:inline:101 -->"
    response_body = f"{response}\n\n{marker}"
    posted_response = None
    resolved = False

    def request(method, path, *, query=None, json_body=None):
        nonlocal posted_response, resolved
        calls.append((method, path, query, json_body))
        if method == "GET" and path == comments_path:
            return [] if posted_response is None else [posted_response]
        if method == "POST" and path == reply_path:
            assert posted_response is None
            assert json_body == {"body": response_body}
            posted_response = {
                "id": 901,
                "body": response_body,
                "html_url": response_url,
            }
            return posted_response
        if method == "POST" and path == "/graphql":
            operation = json_body["query"].lstrip().split()[1].split("(")[0]
            if operation == "ReviewThread":
                return {
                    "data": {
                        "node": {
                            "id": "PRRT_inline_101",
                            "isResolved": resolved,
                            "viewerCanResolve": True,
                        }
                    }
                }
            if operation == "ResolveThread":
                resolved = True
                return {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {
                                "id": "PRRT_inline_101",
                                "isResolved": True,
                            }
                        }
                    }
                }
        raise AssertionError(f"unexpected request: {method} {path}")

    client = GitHubClient("placeholder-token", request=request)
    feedback = GitHubFeedback(
        external_id="inline:101",
        kind="inline",
        body="Use the shared helper",
        path="src/app.py",
        line=14,
        review_thread_id="PRRT_inline_101",
        top_level_comment_id=77,
    )

    first = client.resolve_feedback_without_code(
        "acme/widgets",
        12,
        feedback,
        response,
    )
    second = client.resolve_feedback_without_code(
        "acme/widgets",
        12,
        feedback,
        response,
    )

    assert first == FeedbackAddress("RESOLVED", response_url)
    assert second == first
    assert response_body.count(marker) == 1
    assert [call[:2] for call in calls].count(("POST", reply_path)) == 1
    graphql_operations = [
        call[3]["query"].lstrip().split()[1].split("(")[0]
        for call in calls
        if call[1] == "/graphql"
    ]
    assert graphql_operations == [
        "ReviewThread",
        "ResolveThread",
        "ReviewThread",
    ]


def test_resolve_feedback_without_code_acknowledges_review_without_resolution():
    calls = []
    comments_path = "/repos/acme/widgets/issues/12/comments"
    response_url = "https://example.test/pulls/12#issuecomment-902"
    response = (
        "The report is valid but outside this issue. Follow-up: "
        "https://example.test/issues/301"
    )
    marker = "<!-- repogents-feedback:review:201 -->"
    response_body = f"{response}\n\n{marker}"
    posted_response = None

    def request(method, path, *, query=None, json_body=None):
        nonlocal posted_response
        calls.append((method, path, query, json_body))
        assert path == comments_path
        if method == "GET":
            return [] if posted_response is None else [posted_response]
        assert method == "POST"
        assert posted_response is None
        assert json_body == {"body": response_body}
        posted_response = {
            "id": 902,
            "body": response_body,
            "html_url": response_url,
        }
        return posted_response

    client = GitHubClient("placeholder-token", request=request)
    feedback = GitHubFeedback(
        external_id="review:201",
        kind="review",
        body="Please address the review",
    )

    first = client.resolve_feedback_without_code(
        "acme/widgets",
        12,
        feedback,
        response,
    )
    second = client.resolve_feedback_without_code(
        "acme/widgets",
        12,
        feedback,
        response,
    )

    assert first == FeedbackAddress("ACKNOWLEDGED", response_url)
    assert second == first
    assert [call[0] for call in calls].count("POST") == 1
    assert all(call[1] != "/graphql" for call in calls)



def test_address_feedback_rejects_marker_for_a_different_head_sha():
    calls = []
    current_head_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    prior_head_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    marker = "<!-- repogents-feedback:inline:101 -->"
    comments_path = "/repos/acme/widgets/pulls/12/comments"
    response_url = "https://example.test/pulls/12#discussion_r901"

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        if method == "GET" and path == comments_path:
            return [
                {
                    "id": 901,
                    "body": (
                        "Addressed in validated commit "
                        f"`{prior_head_sha}`.\n\n{marker}"
                    ),
                    "html_url": response_url,
                }
            ]
        if method == "POST" and path == "/graphql":
            return {
                "data": {
                    "node": {
                        "id": "PRRT_inline_101",
                        "isResolved": True,
                        "viewerCanResolve": True,
                    }
                }
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    client = GitHubClient("placeholder-token", request=request)
    feedback = GitHubFeedback(
        external_id="inline:101",
        kind="inline",
        body="Use the shared helper",
        path="src/app.py",
        line=14,
        review_thread_id="PRRT_inline_101",
        top_level_comment_id=77,
    )

    with pytest.raises(RuntimeError):
        client.address_feedback(
            "acme/widgets",
            12,
            feedback,
            current_head_sha,
        )

    assert calls == [
        (
            "GET",
            comments_path,
            {"per_page": 100, "page": 1},
            None,
        )
    ]


def test_client_exposes_no_merge_operation():
    assert not hasattr(GitHubClient, "merge")


def _git_config(environment):
    return {
        environment[f"GIT_CONFIG_KEY_{index}"]: environment[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(environment["GIT_CONFIG_COUNT"]))
    }


def test_git_network_commands_use_transient_token_without_persisting_it(tmp_path):
    token = "placeholder-token"
    command_calls = []
    new_workspace = tmp_path / "new"
    existing_workspace = tmp_path / "existing"
    (existing_workspace / ".git").mkdir(parents=True)

    def request(method, path, *, query=None, json_body=None):
        if path == "/repos/acme/widgets":
            return {
                "full_name": "acme/widgets",
                "default_branch": "main",
                "clone_url": "https://github.com/acme/widgets.git",
            }
        if path.endswith(".diff"):
            return "updated diff"
        return {
            "number": 12,
            "html_url": "https://example.test/pulls/12",
            "head": {
                "ref": "agent/issue-7",
                "sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
            "state": "open",
            "merged": False,
        }

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(
                stdout="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(token, request=request, command_runner=command_runner)

    client.checkout("acme/widgets", "main", new_workspace)
    client.checkout("acme/widgets", "main", existing_workspace)
    client.publish(
        "acme/widgets",
        7,
        "main",
        existing_workspace,
        existing_pr=12,
    )

    network_calls = [
        call
        for call in command_calls
        if call[0][1] in {"clone", "fetch", "pull", "push"}
    ]
    assert [call[0][1] for call in network_calls] == [
        "clone",
        "fetch",
        "pull",
        "fetch",
        "push",
    ]
    for args, _, environment in network_calls:
        authorization = _git_config(environment)["http.extraHeader"]
        prefix = "Authorization: Basic "
        assert authorization.startswith(prefix)
        assert b64decode(authorization.removeprefix(prefix)).decode() == (
            f"x-access-token:{token}"
        )
        assert token not in repr(args)
        assert all("@" not in arg for arg in args if "://" in arg)
    assert not any(call[0][1:3] == ["remote", "set-url"] for call in command_calls)


def test_publish_commit_supplies_service_local_git_identity(tmp_path):
    workspace = tmp_path / "widgets"
    workspace.mkdir()
    command_calls = []

    def request(method, path, *, query=None, json_body=None):
        if path.endswith(".diff"):
            return "updated diff"
        return {
            "number": 12,
            "html_url": "https://example.test/pulls/12",
            "head": {
                "ref": "agent/issue-7",
                "sha": "cccccccccccccccccccccccccccccccccccccccc",
            },
            "state": "open",
            "merged": False,
        }

    def command_runner(args, *, cwd=None, env=None):
        command_calls.append((args, cwd, env))
        if args == ["git", "diff", "--cached", "--name-only"]:
            return SimpleNamespace(stdout="app.py\n")
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(
                stdout="cccccccccccccccccccccccccccccccccccccccc\n"
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=request,
        command_runner=command_runner,
    )

    client.publish(
        "acme/widgets",
        7,
        "main",
        workspace,
        existing_pr=12,
    )

    commit_call = next(call for call in command_calls if call[0][1] == "commit")
    commit_config = _git_config(commit_call[2])
    assert commit_config["user.name"]
    assert commit_config["user.email"]
