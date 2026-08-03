from base64 import b64decode
from dataclasses import FrozenInstanceError
import json
from types import SimpleNamespace
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from repogents.github import (
    FeedbackAddress,
    GitHubClient,
    GitHubFeedback,
    GitHubIssue,
    PullRequest,
    _GitWorkspaceSnapshot,
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

        def __init__(self):
            self._consumed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            if self._consumed:
                return b""
            self._consumed = True
            return (
                b'{"full_name":"acme/widgets","default_branch":"main",'
                b'"clone_url":"https://github.com/acme/widgets.git"}'
            )

    def urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GitHubClient("placeholder-token", api_base="https://api.example.test/")

    assert client.repository("acme/widgets")["default_branch"] == "main"
    request = captured["request"]
    assert request.method == "GET"
    assert request.full_url == "https://api.example.test/repos/acme/widgets"
    assert request.get_header("Authorization") == "Bearer placeholder-token"
    assert 0 < captured["timeout"] <= 30.0


def test_list_ready_issues_requests_the_ready_open_issue_set():
    calls = []
    response = [
        {
            "number": 7,
            "title": "First",
            "body": "First body",
            "html_url": "https://example.test/issues/7",
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

    assert client.list_ready_issues("acme/widgets") == [
        GitHubIssue(7, "First", "First body", "https://example.test/issues/7"),
        GitHubIssue(8, "No body", "", "https://example.test/issues/8"),
    ]
    assert calls == [
        (
            "GET",
            "/repos/acme/widgets/issues",
            {"state": "open", "labels": "agent:ready", "per_page": 100, "page": 1},
            None,
        )
    ]


def test_list_ready_issues_paginates_until_a_short_page():
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

    issues = client.list_ready_issues("acme/widgets")

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
                "labels": "agent:ready",
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
                "labels": "agent:ready",
                "per_page": 100,
                "page": 2,
            },
            None,
        ),
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


def test_checkout_cleans_partial_workspace_after_clone_timeout_and_retries(tmp_path):
    workspace = tmp_path / "widgets"
    command_calls = []
    clone_attempts = 0

    def request(method, path, *, query=None, json_body=None):
        return {
            "full_name": "acme/widgets",
            "default_branch": "main",
            "clone_url": "https://github.com/acme/widgets.git",
        }

    def command_runner(args, *, cwd=None, env=None):
        nonlocal clone_attempts
        command_calls.append((args, cwd, env))
        if args[1] == "clone":
            clone_attempts += 1
            (workspace / ".git").mkdir(parents=True)
            (workspace / ".git" / "config").write_text("partial", encoding="utf-8")
            if clone_attempts == 1:
                raise subprocess.TimeoutExpired(args, timeout=300.0)
            (workspace / "README.md").write_text("complete", encoding="utf-8")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token", request=request, command_runner=command_runner
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client.checkout("acme/widgets", "main", workspace)

    assert captured.value.timeout == 300.0
    assert not workspace.exists()
    assert client.checkout("acme/widgets", "main", workspace) == workspace
    assert clone_attempts == 2
    assert (workspace / ".git" / "config").read_text(encoding="utf-8") == "partial"
    assert (workspace / "README.md").read_text(encoding="utf-8") == "complete"
    assert [call[0][1] for call in command_calls] == ["clone", "clone"]


def test_checkout_clone_timeout_never_removes_preexisting_workspace(tmp_path):
    workspace = tmp_path / "caller-owned"
    workspace.mkdir()
    sentinel = workspace / "keep.txt"
    sentinel.write_text("caller data", encoding="utf-8")

    def request(method, path, *, query=None, json_body=None):
        return {
            "full_name": "acme/widgets",
            "default_branch": "main",
            "clone_url": "https://github.com/acme/widgets.git",
        }

    def command_runner(args, *, cwd=None, env=None):
        (workspace / ".git").mkdir()
        raise subprocess.TimeoutExpired(args, timeout=300.0)

    client = GitHubClient(
        "placeholder-token", request=request, command_runner=command_runner
    )

    with pytest.raises(subprocess.TimeoutExpired):
        client.checkout("acme/widgets", "main", workspace)

    assert workspace.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "caller data"
    assert not (workspace / ".git").exists()


def test_checkout_cleans_partial_clone_from_preexisting_empty_workspace_and_retries(tmp_path):
    workspace = tmp_path / "existing-empty"
    workspace.mkdir()
    command_calls = []
    clone_attempts = 0

    def request(method, path, *, query=None, json_body=None):
        return {
            "full_name": "acme/widgets",
            "default_branch": "main",
            "clone_url": "https://github.com/acme/widgets.git",
        }

    def command_runner(args, *, cwd=None, env=None):
        nonlocal clone_attempts
        command_calls.append((args, cwd, env))
        assert args[1] == "clone"
        clone_attempts += 1
        (workspace / ".git" / "objects").mkdir(parents=True)
        (workspace / ".git" / "config").write_text("partial", encoding="utf-8")
        (workspace / "clone-created.tmp").write_text("partial", encoding="utf-8")
        if clone_attempts == 1:
            raise subprocess.TimeoutExpired(args, timeout=300.0)
        (workspace / "README.md").write_text("complete", encoding="utf-8")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token", request=request, command_runner=command_runner
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client.checkout("acme/widgets", "release", workspace)

    assert captured.value.timeout == 300.0
    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []

    assert client.checkout("acme/widgets", "release", workspace) == workspace
    assert clone_attempts == 2
    assert [call[0][1] for call in command_calls] == ["clone", "clone"]
    assert all("release" in call[0] for call in command_calls)
    assert (workspace / ".git" / "config").read_text(encoding="utf-8") == "partial"
    assert (workspace / "README.md").read_text(encoding="utf-8") == "complete"


def test_checkout_timeout_preserves_preexisting_directory_tree_and_valid_checkout(tmp_path):
    workspace = tmp_path / "caller-owned"
    nested = workspace / "notes" / "archive"
    nested.mkdir(parents=True)
    sentinel = nested / "keep.txt"
    sentinel.write_text("caller data", encoding="utf-8")

    def request(method, path, *, query=None, json_body=None):
        return {
            "full_name": "acme/widgets",
            "default_branch": "main",
            "clone_url": "https://github.com/acme/widgets.git",
        }

    def timed_out_clone(args, *, cwd=None, env=None):
        (workspace / ".git" / "objects").mkdir(parents=True)
        (workspace / "clone-created.tmp").write_text("partial", encoding="utf-8")
        raise subprocess.TimeoutExpired(args, timeout=300.0)

    client = GitHubClient(
        "placeholder-token", request=request, command_runner=timed_out_clone
    )
    with pytest.raises(subprocess.TimeoutExpired):
        client.checkout("acme/widgets", "main", workspace)

    assert workspace.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "caller data"
    assert (workspace / "notes").is_dir()
    assert not (workspace / ".git").exists()
    assert not (workspace / "clone-created.tmp").exists()

    valid_workspace = tmp_path / "valid"
    (valid_workspace / ".git").mkdir(parents=True)
    valid_sentinel = valid_workspace / "keep.txt"
    valid_sentinel.write_text("valid checkout", encoding="utf-8")
    calls = []

    def existing_runner(args, *, cwd=None, env=None):
        calls.append((args, cwd, env))
        return SimpleNamespace(stdout="")

    existing_client = GitHubClient(
        "placeholder-token", request=request, command_runner=existing_runner
    )
    assert existing_client.checkout("acme/widgets", "main", valid_workspace) == valid_workspace
    assert valid_sentinel.read_text(encoding="utf-8") == "valid checkout"
    assert [call[0][1] for call in calls] == ["fetch", "checkout", "pull"]


def test_checkout_non_timeout_clone_failure_preserves_error_and_partial_destination(tmp_path):
    workspace = tmp_path / "failed-clone"
    command_error = subprocess.CalledProcessError(
        128,
        ["git", "clone"],
        stderr="fatal: repository unavailable",
    )

    def request(method, path, *, query=None, json_body=None):
        return {
            "full_name": "acme/widgets",
            "default_branch": "main",
            "clone_url": "https://github.com/acme/widgets.git",
        }

    def command_runner(args, *, cwd=None, env=None):
        (workspace / ".git").mkdir(parents=True)
        (workspace / "clone.log").write_text("diagnostic", encoding="utf-8")
        raise command_error

    client = GitHubClient(
        "placeholder-token", request=request, command_runner=command_runner
    )

    with pytest.raises(subprocess.CalledProcessError) as captured:
        client.checkout("acme/widgets", "main", workspace)

    assert captured.value is command_error
    assert (workspace / ".git").is_dir()
    assert (workspace / "clone.log").read_text(encoding="utf-8") == "diagnostic"


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
            self._consumed = False
            self.headers = {"Content-Type": content_type}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            if self._consumed:
                return b""
            self._consumed = True
            return self._body

    def urlopen(request, *, timeout):
        assert 0 < timeout <= 30.0
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
    ]
    assert calls[:2] == [
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
    ]
    assert len(calls) == 3
    graphql_call = calls[2]
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


def test_list_feedback_paginates_inline_comments_and_reviews():
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
    rest_calls = calls[:4]
    assert [(call[1], call[2]["page"]) for call in rest_calls] == [
        (inline_path, 1),
        (inline_path, 2),
        (review_path, 1),
        (review_path, 2),
    ]
    assert all(call[2]["per_page"] == 100 for call in rest_calls)
    graphql_calls = calls[4:]
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


def test_address_feedback_rejects_general_pull_request_comments():
    calls = []

    def request(method, path, *, query=None, json_body=None):
        calls.append((method, path, query, json_body))
        return []

    client = GitHubClient("placeholder-token", request=request)
    feedback = GitHubFeedback(
        external_id="comment:301",
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


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    [
        ("transport_timeout", float("nan"), "transport_timeout"),
        ("transport_timeout", float("inf"), "transport_timeout"),
        ("transport_timeout", float("-inf"), "transport_timeout"),
        ("transport_timeout", 0.0, "transport_timeout"),
        ("transport_timeout", -1.0, "transport_timeout"),
        ("git_command_timeout", float("nan"), "git_command_timeout"),
        ("git_command_timeout", float("inf"), "git_command_timeout"),
        ("git_command_timeout", float("-inf"), "git_command_timeout"),
        ("git_command_timeout", 0.0, "git_command_timeout"),
        ("git_command_timeout", -1.0, "git_command_timeout"),
    ],
)
def test_github_client_rejects_invalid_independent_timeout_budgets(
    argument, value, message
):
    with pytest.raises(ValueError, match=message):
        GitHubClient("placeholder-token", **{argument: value})


def test_default_git_runner_uses_independent_transfer_timeout(monkeypatch, tmp_path):
    calls = []

    class Process:
        returncode = 0

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

        def communicate(self, *, timeout):
            assert timeout == 600.0
            return "", ""

    monkeypatch.setattr("subprocess.Popen", Process)
    client = GitHubClient(
        "placeholder-token",
        transport_timeout=11.0,
        git_command_timeout=600.0,
    )

    for command in (
        ["git", "clone", "https://example.test/acme/widget.git", "widget"],
        ["git", "fetch", "origin", "main"],
        ["git", "pull", "--ff-only", "origin", "main"],
        ["git", "push", "origin", "agent/issue-7"],
    ):
        result = client._default_command_runner(
            command, cwd=tmp_path, env={"GIT_TERMINAL_PROMPT": "0"}
        )
        assert result.returncode == 0

    assert [call[0] for call in calls] == [
        ["git", "clone", "https://example.test/acme/widget.git", "widget"],
        ["git", "fetch", "origin", "main"],
        ["git", "pull", "--ff-only", "origin", "main"],
        ["git", "push", "origin", "agent/issue-7"],
    ]
    assert all(call[1]["cwd"] == tmp_path for call in calls)
    assert all(call[1]["stdout"] is subprocess.PIPE for call in calls)
    assert all(call[1]["stderr"] is subprocess.PIPE for call in calls)
    assert all(call[1]["text"] is True for call in calls)
    assert all(call[1]["env"]["GIT_TERMINAL_PROMPT"] == "0" for call in calls)
    if os.name == "nt":
        assert all(
            call[1]["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
            for call in calls
        )
    else:
        assert all(call[1]["start_new_session"] is True for call in calls)


def test_http_and_git_timeout_defaults_are_distinct():
    client = GitHubClient("placeholder-token")

    assert client._transport_timeout == 30.0
    assert client._git_command_timeout == 300.0
    assert client._git_command_timeout > client._transport_timeout


def test_default_http_request_uses_http_budget_not_git_budget(monkeypatch):
    captured = {}

    class Response:
        headers = {"Content-Type": "application/json"}

        def __init__(self):
            self._consumed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            if self._consumed:
                return b""
            self._consumed = True
            return b'{"full_name":"acme/widget","default_branch":"main"}'

    def urlopen(request, *, timeout):
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GitHubClient(
        "placeholder-token",
        transport_timeout=12.5,
        git_command_timeout=900.0,
    )

    assert client.repository("acme/widget")["default_branch"] == "main"
    assert 0 < captured["timeout"] <= 12.5


@pytest.mark.parametrize("command", ["checkout", "pull", "commit", "rebase"])
def test_mutating_git_timeout_recovers_workspace_and_allows_retry(tmp_path, command):
    """Each supported timed-out mutation leaves a reusable, lock-free workspace."""
    workspace = tmp_path / command
    git_dir = workspace / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    original_head = "1" * 40
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(
        original_head + "\n", encoding="ascii"
    )
    caller_content = workspace / "generated.txt"
    caller_content.write_text("keep generated work", encoding="utf-8")
    calls = []
    timed_out = [True]

    target_args = {
        "checkout": ["git", "checkout", "feature"],
        "pull": ["git", "pull", "--ff-only", "origin", "main"],
        "commit": ["git", "commit", "-m", "Resolve issue #7"],
        "rebase": ["git", "rebase", "origin/main"],
    }[command]

    def command_runner(args, *, cwd=None, env=None):
        calls.append((list(args), cwd, env))
        if args == target_args and timed_out[0]:
            timed_out[0] = False
            (git_dir / "index.lock").write_text("locked", encoding="utf-8")
            # Model an unintended branch/worktree transition plus operation state.
            (git_dir / "HEAD").write_text(
                "ref: refs/heads/interrupted\n", encoding="utf-8"
            )
            (git_dir / "refs" / "heads" / "interrupted").write_text(
                "2" * 40 + "\n", encoding="ascii"
            )
            if command == "rebase":
                (git_dir / "rebase-merge").mkdir()
                (git_dir / "rebase-merge" / "head-name").write_text(
                    "refs/heads/main", encoding="utf-8"
                )
            raise subprocess.TimeoutExpired(args, timeout=300.0)
        if args in (
            ["git", "checkout", "main"],
            ["git", "checkout", "--force", "main"],
        ):
            (git_dir / "HEAD").write_text(
                "ref: refs/heads/main\n", encoding="utf-8"
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token", request=lambda *_args, **_kwargs: None,
        command_runner=command_runner,
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._run_mutating_git(
            target_args, workspace=workspace, env=client._git_command_env
        )

    assert captured.value.cmd == target_args
    assert captured.value.timeout == 300.0
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "rebase-merge").exists()
    assert (git_dir / "HEAD").read_text(encoding="utf-8") == (
        "ref: refs/heads/main\n"
    )
    assert caller_content.read_text(encoding="utf-8") == "keep generated work"
    expected_checkout = (
        ["git", "checkout", "--force", "main"]
        if command in {"checkout", "pull"}
        else ["git", "checkout", "main"]
    )
    expected_reset = [
        "git",
        "reset",
        "--hard" if command in {"checkout", "pull"} else "--mixed",
        original_head,
    ]
    assert expected_checkout in [call[0] for call in calls]
    assert expected_reset in [call[0] for call in calls]
    if command == "rebase":
        assert ["git", "rebase", "--abort"] in [call[0] for call in calls]

    # A later poll/publish attempt can use the same workspace without manual cleanup.
    result = client._run_mutating_git(
        target_args, workspace=workspace, env=client._git_command_env
    )
    assert result.stdout == ""
    assert calls[-1][0] == target_args


def test_incomplete_mutation_timeout_recovery_marks_workspace_unusable_and_preserves_timeout(tmp_path):
    """A failed in-place restore cannot be hidden by a different exception or reused."""
    workspace = tmp_path / "damaged"
    git_dir = workspace / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    original_head = "1" * 40
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(
        original_head + "\n", encoding="ascii"
    )
    caller_content = workspace / "generated.txt"
    caller_content.write_text("preserve me", encoding="utf-8")
    target_args = ["git", "checkout", "feature"]
    timeout = subprocess.TimeoutExpired(target_args, timeout=300.0)

    def command_runner(args, *, cwd=None, env=None):
        if args == target_args:
            (git_dir / "index.lock").write_text("locked", encoding="utf-8")
            (git_dir / "HEAD").write_text(
                "ref: refs/heads/interrupted\n", encoding="utf-8"
            )
            raise timeout
        if args == ["git", "reset", "--hard", original_head]:
            raise subprocess.CalledProcessError(128, args, stderr="reset failed")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token",
        request=lambda *_args, **_kwargs: None,
        command_runner=command_runner,
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._run_mutating_git(
            target_args, workspace=workspace, env=client._git_command_env
        )

    assert captured.value is timeout
    assert not (git_dir / "index.lock").exists()
    assert caller_content.read_text(encoding="utf-8") == "preserve me"
    marker = git_dir / "repogents-workspace-unusable"
    assert marker.is_file()
    assert "could not restore pre-timeout Git HEAD" in marker.read_text(
        encoding="utf-8"
    )
    diagnostics = getattr(timeout, "__notes__", None) or getattr(
        timeout, "repogents_recovery_errors", []
    )
    assert any("could not fully recover Git workspace" in detail for detail in diagnostics)

    with pytest.raises(RuntimeError, match="requires recreation"):
        client.checkout("acme/widgets", "main", workspace)
    assert caller_content.read_text(encoding="utf-8") == "preserve me"


def test_checkout_and_publish_route_timeout_sensitive_mutations_through_recovery():
    source = Path(__file__).resolve().parents[1] / "repogents" / "github.py"
    implementation = source.read_text(encoding="utf-8")

    assert 'if args[1] in {"checkout", "pull"}' in implementation
    assert implementation.count("self._run_mutating_git(") >= 5
    assert '["git", "commit", "-m", f"Resolve issue #{issue_number}"]' in implementation
    assert '["git", "rebase", f"origin/{target_branch}"]' in implementation


@pytest.mark.parametrize(
    ("path", "content_type", "chunks"),
    [
        (
            "/repos/acme/widget",
            "application/json; charset=utf-8",
            [b'{"full_name":"acme/widget",', b'"default_branch":"main"}'],
        ),
        (
            "/repos/acme/widget/pulls/7.diff",
            "text/plain; charset=utf-8",
            [b"diff --git a/old.py ", b"b/new.py\n"],
        ),
    ],
)
def test_default_request_enforces_total_deadline_while_reading_response_body(
    monkeypatch, path, content_type, chunks
):
    """Trickle chunks below inactivity timeout cannot renew the total HTTP budget."""
    clock = [100.0]
    read_calls = []

    class Response:
        headers = {"Content-Type": content_type}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            read_calls.append(size)
            # Each individual read completes before the configured 1-second socket
            # inactivity timeout, but the complete body exceeds that same total budget.
            clock[0] += 0.6
            return chunks.pop(0) if chunks else b""

    def urlopen(request, *, timeout):
        assert 0 < timeout <= 1.0
        return Response()

    monkeypatch.setattr("repogents.github.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GitHubClient("placeholder-token", transport_timeout=1.0)

    with pytest.raises(TimeoutError, match="total transport deadline"):
        client._default_request("GET", path)

    assert read_calls == [64 * 1024, 64 * 1024]


def test_default_request_preserves_successful_chunked_json_and_diff_decoding(monkeypatch):
    """Bounded body reads preserve complete successful JSON and text responses."""
    clock = [200.0]
    responses = [
        (
            "application/json; charset=utf-8",
            [b'{"full_name":"acme/widget",', b'"default_branch":"main"}', b""],
        ),
        ("text/plain; charset=utf-8", [b"diff --git ", b"a/old b/new\n", b""]),
    ]
    socket_timeouts = []

    class Socket:
        def settimeout(self, timeout):
            socket_timeouts.append(timeout)

    class Response:
        def __init__(self, content_type, chunks):
            self.headers = {"Content-Type": content_type}
            self.chunks = chunks
            self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=Socket()))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            assert size == 64 * 1024
            clock[0] += 0.1
            return self.chunks.pop(0)

    def urlopen(request, *, timeout):
        content_type, chunks = responses.pop(0)
        return Response(content_type, chunks)

    monkeypatch.setattr("repogents.github.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GitHubClient("placeholder-token", transport_timeout=1.0)

    assert client._default_request("GET", "/repos/acme/widget") == {
        "full_name": "acme/widget",
        "default_branch": "main",
    }
    assert client._default_request("GET", "/repos/acme/widget/pulls/7.diff") == (
        "diff --git a/old b/new\n"
    )
    assert len(socket_timeouts) == 6
    assert all(0 < timeout <= 1.0 for timeout in socket_timeouts)


def test_slow_github_response_deadline_releases_lookup_capacity_without_late_commit(
    monkeypatch, tmp_path
):
    """A timed-out metadata body settles FAILED and the fixed slot is reusable."""
    import time

    from repogents.application import Application, ApplicationConfig
    from repogents.store import Store

    read_started = threading.Event()
    request_count = 0

    class SlowResponse:
        headers = {"Content-Type": "application/json"}

        def __init__(self):
            self.chunks = [
                b'{"full_name":"acme/slow",',
                b'"default_branch":"main",',
                b'"clone_url":"https://example.test/acme/slow.git"}',
                b"",
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            assert size == 64 * 1024
            read_started.set()
            time.sleep(0.025)  # below the 60ms inactivity budget per read
            return self.chunks.pop(0)

    class FastResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            assert size == 64 * 1024
            if hasattr(self, "done"):
                return b""
            self.done = True
            return (
                b'{"full_name":"acme/recovered","default_branch":"main",'
                b'"clone_url":"https://example.test/acme/recovered.git"}'
            )

    def urlopen(_request, *, timeout):
        nonlocal request_count
        request_count += 1
        assert timeout > 0
        return SlowResponse() if request_count == 1 else FastResponse()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    github = GitHubClient("placeholder-token", transport_timeout=0.06)
    store = Store(tmp_path / "state.sqlite3")
    app = Application(
        store,
        github,
        object(),
        object(),
        ApplicationConfig(
            data_dir=tmp_path / "runtime",
            add_repository_lookup_timeout=0.5,
            add_repository_lookup_max_workers=1,
        ),
    )
    try:
        with pytest.raises(TimeoutError, match="total transport deadline"):
            app.add_repository("acme/slow", operation_id="slow-response")

        assert read_started.is_set()
        operation = app.repository_add_operation("slow-response")
        assert operation is not None
        assert operation["state"] == "FAILED", operation
        assert store.list_repositories() == []

        recovered = app.add_repository(
            "acme/recovered", operation_id="recovered-response"
        )
        assert recovered["github_repository"] == "acme/recovered"
        assert app.repository_add_operation("recovered-response")["state"] == "COMMITTED"
        assert request_count == 2
    finally:
        app.close()


def test_slow_github_response_cannot_outlive_poller_service_ownership(
    monkeypatch, tmp_path
):
    """Shutdown retains ownership until a slow response expires and poller exits."""
    import time

    from repogents.http_api import HttpService
    from repogents.service_ownership import (
        ServiceOwnership,
        ServiceOwnershipUnavailableError,
    )

    ownership_path = tmp_path / ".repogents-service.lock"
    read_started = threading.Event()
    mutations = []

    class SlowResponse:
        headers = {"Content-Type": "application/json"}

        def __init__(self):
            self.chunks = [b"[", b"]", b" ", b" ", b""]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            assert size == 64 * 1024
            read_started.set()
            time.sleep(0.12)  # each chunk is below the 400ms inactivity timeout
            return self.chunks.pop(0)

    def urlopen(_request, *, timeout):
        assert timeout > 0
        return SlowResponse()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    github = GitHubClient("placeholder-token", transport_timeout=0.4)

    class PollingApplication:
        def __init__(self):
            self.ownership = ServiceOwnership(ownership_path)
            self.closed = False

        def acquire_service_ownership(self):
            self.ownership.acquire()

        def poll_once(self):
            github._default_request("GET", "/repos/acme/widget/issues")
            mutations.append("late-poller-mutation")

        def state(self):
            return {"repositories": []}

        def close(self):
            self.closed = True
            self.ownership.close()

    application = PollingApplication()
    service = HttpService(application, "127.0.0.1", 0, 60)
    serving = threading.Thread(target=service.serve_forever)
    serving.start()
    assert read_started.wait(timeout=1)

    service.shutdown()
    serving.join(timeout=0.05)
    assert serving.is_alive()
    assert application.ownership.acquired is True

    competitor = ServiceOwnership(ownership_path)
    with pytest.raises(ServiceOwnershipUnavailableError):
        competitor.acquire()

    serving.join(timeout=1)
    assert not serving.is_alive()
    assert application.closed is True
    assert application.ownership.acquired is False
    assert mutations == []

    replacement = ServiceOwnership(ownership_path)
    replacement.acquire()
    replacement.close()


def test_clone_timeout_cleanup_failure_preserves_timeout_without_add_note(
    tmp_path, monkeypatch
):
    """Python 3.10-compatible diagnostics cannot mask the clone timeout."""
    workspace = tmp_path / "partial-cleanup-failure"

    class TimeoutWithoutAddNote(subprocess.TimeoutExpired):
        add_note = None

    clone_args = [
        "git",
        "clone",
        "--branch",
        "main",
        "--single-branch",
        "https://github.com/acme/widgets.git",
        str(workspace),
    ]
    timeout = TimeoutWithoutAddNote(clone_args, timeout=300.0)

    def request(method, path, *, query=None, json_body=None):
        return {
            "full_name": "acme/widgets",
            "default_branch": "main",
            "clone_url": "https://github.com/acme/widgets.git",
        }

    def command_runner(args, *, cwd=None, env=None):
        assert args == clone_args
        (workspace / ".git").mkdir(parents=True)
        raise timeout

    cleanup_error = OSError("cleanup denied")

    def failed_cleanup(path):
        assert path == workspace
        raise cleanup_error

    monkeypatch.setattr("repogents.github.shutil.rmtree", failed_cleanup)
    client = GitHubClient(
        "placeholder-token", request=request, command_runner=command_runner
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client.checkout("acme/widgets", "main", workspace)

    assert captured.value is timeout
    assert timeout.add_note is None
    assert timeout.repogents_recovery_errors == [
        f"could not remove partial clone workspace {workspace}: cleanup denied"
    ]
    assert workspace.is_dir()
    assert (workspace / ".git").is_dir()


def test_clone_timeout_cleanup_diagnostic_uses_exception_note_when_available():
    """Newer interpreters may retain the same diagnostic through exception notes."""
    timeout = subprocess.TimeoutExpired(["git", "clone"], timeout=300.0)

    GitHubClient._record_recovery_error(timeout, "clone cleanup failed")

    notes = getattr(timeout, "__notes__", None)
    if callable(getattr(timeout, "add_note", None)):
        assert notes == ["clone cleanup failed"]
        assert not hasattr(timeout, "repogents_recovery_errors")
    else:
        assert timeout.repogents_recovery_errors == ["clone cleanup failed"]


def test_fast_forward_pull_timeout_restores_tracked_worktree_and_allows_retry(tmp_path):
    """A post-merge timeout cannot leave upstream tracked changes as local edits."""
    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            capture_output=True, timeout=5,
        )

    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    workspace = tmp_path / "workspace"
    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(seed))
    git("config", "user.name", "Repogents Test", cwd=seed)
    git("config", "user.email", "repogents@example.test", cwd=seed)
    tracked = seed / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    git("add", "tracked.txt", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)
    git("clone", "--branch", "main", str(remote), str(workspace))
    pre_pull_head = git("rev-parse", "HEAD", cwd=workspace).stdout.strip()

    tracked.write_text("after\n", encoding="utf-8")
    git("commit", "-am", "upstream update", cwd=seed)
    git("push", "origin", "main", cwd=seed)
    upstream_head = git("rev-parse", "HEAD", cwd=seed).stdout.strip()

    hook = workspace / ".git" / "hooks" / "post-merge"
    hook.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    hook.chmod(0o755)
    client = GitHubClient(
        "placeholder-token",
        request=lambda *_args, **_kwargs: None,
        git_command_timeout=0.2,
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client.checkout("acme/widget", "main", workspace)

    assert captured.value.cmd[:3] == ["git", "pull", "--ff-only"]
    assert git("rev-parse", "HEAD", cwd=workspace).stdout.strip() == pre_pull_head
    assert git("symbolic-ref", "--short", "HEAD", cwd=workspace).stdout.strip() == "main"
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "before\n"
    assert git("status", "--porcelain", cwd=workspace).stdout == ""
    assert not (workspace / ".git" / "repogents-workspace-unusable").exists()

    # Removing the deliberately slow hook lets the same workspace fast-forward
    # cleanly; no local tracked changes remain to block the retry.
    hook.unlink()
    assert client.checkout("acme/widget", "main", workspace) == workspace
    assert git("rev-parse", "HEAD", cwd=workspace).stdout.strip() == upstream_head
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "after\n"
    assert git("status", "--porcelain", cwd=workspace).stdout == ""



def test_branch_checkout_timeout_restores_tracked_worktree_and_allows_retry(tmp_path):
    """A post-checkout timeout restores the original branch and tracked files."""
    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
            timeout=5,
        )

    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    outside.write_text("unrelated caller content\n", encoding="utf-8")
    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(seed))
    git("config", "user.name", "Repogents Test", cwd=seed)
    git("config", "user.email", "repogents@example.test", cwd=seed)
    tracked = seed / "tracked.txt"
    tracked.write_text("main content\n", encoding="utf-8")
    git("add", "tracked.txt", cwd=seed)
    git("commit", "-m", "main content", cwd=seed)
    git("checkout", "-b", "feature", cwd=seed)
    tracked.write_text("feature content\n", encoding="utf-8")
    git("commit", "-am", "feature content", cwd=seed)
    feature_head = git("rev-parse", "HEAD", cwd=seed).stdout.strip()
    git("checkout", "main", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "origin", "main", "feature", cwd=seed)
    git("clone", "--branch", "main", str(remote), str(workspace))
    pre_checkout_head = git("rev-parse", "HEAD", cwd=workspace).stdout.strip()
    generated = workspace / "generated.txt"
    generated.write_text("untracked generated work\n", encoding="utf-8")

    hook = workspace / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        "#!/bin/sh\n"
        "branch=$(git symbolic-ref --quiet --short HEAD)\n"
        "if [ \"$branch\" = feature ]; then sleep 2; fi\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    client = GitHubClient(
        "placeholder-token",
        request=lambda *_args, **_kwargs: None,
        git_command_timeout=0.2,
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client.checkout("acme/widget", "feature", workspace)

    assert captured.value.cmd[:2] == ["git", "checkout"]
    assert git("rev-parse", "HEAD", cwd=workspace).stdout.strip() == pre_checkout_head
    assert git("symbolic-ref", "--short", "HEAD", cwd=workspace).stdout.strip() == "main"
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "main content\n"
    assert generated.read_text(encoding="utf-8") == "untracked generated work\n"
    assert outside.read_text(encoding="utf-8") == "unrelated caller content\n"
    assert git("status", "--porcelain", cwd=workspace).stdout == "?? generated.txt\n"
    assert not (workspace / ".git" / "repogents-workspace-unusable").exists()

    hook.unlink()
    assert client.checkout("acme/widget", "feature", workspace) == workspace
    assert git("rev-parse", "HEAD", cwd=workspace).stdout.strip() == feature_head
    assert git("symbolic-ref", "--short", "HEAD", cwd=workspace).stdout.strip() == "feature"
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "feature content\n"
    assert generated.read_text(encoding="utf-8") == "untracked generated work\n"
    assert outside.read_text(encoding="utf-8") == "unrelated caller content\n"
    assert git("status", "--porcelain", cwd=workspace).stdout == "?? generated.txt\n"


def test_checkout_pull_use_hard_recovery_but_commit_rebase_preserve_generated_work():
    """Only checkout/pull receive destructive tracked-worktree restoration."""
    source = Path(__file__).resolve().parents[1] / "repogents" / "github.py"
    implementation = source.read_text(encoding="utf-8")

    assert 'restores_tracked_worktree = command in {"checkout", "pull"}' in implementation
    assert 'reset_mode = "--hard" if restores_tracked_worktree else "--mixed"' in implementation
    assert 'checkout_args.append("--force")' in implementation


def test_publication_checkout_timeout_preserves_preexisting_tracked_agent_edits(tmp_path):
    """A timed-out issue-branch checkout restores the agent's exact tracked state."""
    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def git_bytes(*args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            timeout=5,
        )

    remote = tmp_path / "remote.git"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    outside.write_text("unrelated caller content\n", encoding="utf-8")
    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(workspace))
    git("config", "user.name", "Repogents Test", cwd=workspace)
    git("config", "user.email", "repogents@example.test", cwd=workspace)
    (workspace / "staged.txt").write_text("base staged\n", encoding="utf-8")
    (workspace / "unstaged.txt").write_text("base unstaged\n", encoding="utf-8")
    (workspace / "mixed.txt").write_text("base mixed\n", encoding="utf-8")
    (workspace / "binary.dat").write_bytes(b"base\x00binary\xff\n")
    (workspace / ".gitattributes").write_text("nonutf.txt text\n", encoding="utf-8")
    (workspace / "nonutf.txt").write_bytes(b"base text\n")
    git("add", "--all", cwd=workspace)
    git("commit", "-m", "base", cwd=workspace)
    git("remote", "add", "origin", str(remote), cwd=workspace)
    git("push", "--set-upstream", "origin", "main", cwd=workspace)
    original_head = git("rev-parse", "HEAD", cwd=workspace).stdout.strip()

    # Model generated publication work across both index and worktree layers.
    (workspace / "staged.txt").write_text("agent staged edit\n", encoding="utf-8")
    (workspace / "mixed.txt").write_text("agent staged mixed\n", encoding="utf-8")
    git("add", "staged.txt", "mixed.txt", cwd=workspace)
    (workspace / "unstaged.txt").write_text("agent unstaged edit\n", encoding="utf-8")
    (workspace / "binary.dat").write_bytes(b"agent\x00binary\xfe\x01\n")
    # Git is explicitly told this file is text, so its textual hunk contains the
    # raw non-UTF-8 byte rather than an ASCII binary patch encoding.
    (workspace / "nonutf.txt").write_bytes(b"agent text \xff\n")
    (workspace / "mixed.txt").write_text(
        "agent staged mixed\nagent unstaged mixed\n", encoding="utf-8"
    )
    generated = workspace / "generated.txt"
    generated.write_text("untracked generated work\n", encoding="utf-8")
    expected_cached = git_bytes("diff", "--cached", "--binary", cwd=workspace).stdout
    expected_unstaged = git_bytes("diff", "--binary", cwd=workspace).stdout
    assert b"agent text \xff" in expected_unstaged
    expected_status = git("status", "--porcelain", cwd=workspace).stdout

    hook = workspace / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        "#!/bin/sh\n"
        "branch=$(git symbolic-ref --quiet --short HEAD)\n"
        "case \"$branch\" in agent/issue-*) sleep 2;; esac\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    def request(method, path, *, query=None, json_body=None):
        if path.endswith(".diff"):
            return "published diff"
        return {
            "number": 12,
            "html_url": "https://example.test/pulls/12",
            "head": {
                "ref": "agent/issue-80",
                "sha": git("rev-parse", "HEAD", cwd=workspace).stdout.strip(),
            },
            "state": "open",
            "merged": False,
        }

    client = GitHubClient(
        "placeholder-token",
        request=request,
        git_command_timeout=0.2,
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client.publish(
            "acme/widget", 80, "main", workspace, existing_pr=12
        )

    assert captured.value.cmd == ["git", "checkout", "-B", "agent/issue-80"]
    assert not getattr(captured.value, "__notes__", [])
    assert not getattr(captured.value, "repogents_recovery_errors", [])
    assert git("symbolic-ref", "--short", "HEAD", cwd=workspace).stdout.strip() == "main"
    assert git("rev-parse", "HEAD", cwd=workspace).stdout.strip() == original_head
    assert git_bytes("diff", "--cached", "--binary", cwd=workspace).stdout == expected_cached
    assert git_bytes("diff", "--binary", cwd=workspace).stdout == expected_unstaged
    assert (workspace / "nonutf.txt").read_bytes() == b"agent text \xff\n"
    assert git("status", "--porcelain", cwd=workspace).stdout == expected_status
    assert generated.read_text(encoding="utf-8") == "untracked generated work\n"
    assert outside.read_text(encoding="utf-8") == "unrelated caller content\n"
    assert not (workspace / ".git" / "repogents-workspace-unusable").exists()

    # The same workspace can publish after the deliberately slow hook is removed;
    # the preserved edits become the issue commit rather than being lost in recovery.
    hook.unlink()
    published = client.publish(
        "acme/widget", 80, "main", workspace, existing_pr=12
    )
    assert published.branch == "agent/issue-80"
    assert git("symbolic-ref", "--short", "HEAD", cwd=workspace).stdout.strip() == (
        "agent/issue-80"
    )
    assert git("show", "HEAD:staged.txt", cwd=workspace).stdout == "agent staged edit\n"
    assert git("show", "HEAD:unstaged.txt", cwd=workspace).stdout == "agent unstaged edit\n"
    assert git("show", "HEAD:mixed.txt", cwd=workspace).stdout == (
        "agent staged mixed\nagent unstaged mixed\n"
    )
    binary_result = subprocess.run(
        ["git", "show", "HEAD:binary.dat"],
        cwd=workspace,
        check=True,
        capture_output=True,
        timeout=5,
    )
    assert binary_result.stdout == b"agent\x00binary\xfe\x01\n"
    nonutf_result = git_bytes("show", "HEAD:nonutf.txt", cwd=workspace)
    assert nonutf_result.stdout == b"agent text \xff\n"
    assert git("show", "HEAD:generated.txt", cwd=workspace).stdout == (
        "untracked generated work\n"
    )
    assert outside.read_text(encoding="utf-8") == "unrelated caller content\n"
    assert git("status", "--porcelain", cwd=workspace).stdout == ""


def test_publication_checkout_uses_edit_preserving_snapshot_recovery_contract():
    source = Path(__file__).resolve().parents[1] / "repogents" / "github.py"
    implementation = source.read_text(encoding="utf-8")

    assert "publication_snapshot = self._publication_workspace_snapshot(workspace_path)" in implementation
    assert "snapshot=publication_snapshot" in implementation
    assert 'patch = self._binary_command_runner(' in implementation
    assert '["git", "diff", "--binary", snapshot.head]' in implementation
    assert 'mode="wb"' in implementation
    assert '["git", "apply", "--binary", str(patch_path)]' in implementation
    assert "os.replace(temporary_index, index_path)" in implementation

def test_git_process_group_completion_ignores_zombie_only_procfs_members(monkeypatch):
    """Zombie-only procfs remnants are not mutation-capable liveness failures."""
    groups = [
        {101: ("Z", 1), 102: ("Z", 1)},
        {101: ("Z", 1), 102: ("S", 1)},
    ]

    monkeypatch.setattr(
        GitHubClient,
        "_git_process_group_members",
        staticmethod(lambda _process_group: groups.pop(0)),
    )

    assert GitHubClient._git_process_group_exists(100) is False
    assert GitHubClient._git_process_group_exists(100) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group integration coverage")
def test_default_git_timeout_terminates_complete_helper_process_group(tmp_path):
    """A timed-out Git alias cannot leave its shell or long-lived child orphaned."""
    helper = tmp_path / "hang-helper.sh"
    child = tmp_path / "hang-child.py"
    helper_pid = tmp_path / "helper.pid"
    child_pid = tmp_path / "child.pid"
    late_marker = tmp_path / "late-marker"
    child.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
        "open(sys.argv[2], 'w').write('late')\n",
        encoding="utf-8",
    )
    helper.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {helper_pid}\n"
        f"python3 {child} {child_pid} {late_marker} &\n"
        "wait $!\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=5)
    client = GitHubClient("placeholder-token", git_command_timeout=0.15)
    command = ["git", "-c", f"alias.repogents-hang=!{helper}", "repogents-hang"]

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._default_command_runner(command, cwd=tmp_path)

    assert captured.value.cmd == command
    assert not getattr(captured.value, "__notes__", [])
    assert not getattr(captured.value, "repogents_recovery_errors", [])
    assert helper_pid.is_file()
    assert child_pid.is_file()
    pids = [int(helper_pid.read_text()), int(child_pid.read_text())]

    def process_exists(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    deadline = time.monotonic() + 2
    while any(process_exists(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not any(process_exists(pid) for pid in pids)
    assert not late_marker.exists()

    # Repeated timeouts reuse no old process group and leave no accumulating helper.
    helper_pid.unlink()
    child_pid.unlink()
    with pytest.raises(subprocess.TimeoutExpired) as repeated:
        client._default_command_runner(command, cwd=tmp_path)
    assert not getattr(repeated.value, "__notes__", [])
    assert not getattr(repeated.value, "repogents_recovery_errors", [])
    second_pids = [int(helper_pid.read_text()), int(child_pid.read_text())]
    deadline = time.monotonic() + 2
    while any(process_exists(pid) for pid in second_pids) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not any(process_exists(pid) for pid in second_pids)
    assert not late_marker.exists()

    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    result = client._default_command_runner(command, cwd=tmp_path)
    assert result.returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="real Git hook timeout coverage uses POSIX hooks")
def test_pre_rebase_hook_timeout_skips_abort_without_state_and_allows_retry(tmp_path):
    """A hook timeout before rebase state exists cannot quarantine the workspace."""
    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            capture_output=True, timeout=5,
        )

    workspace = tmp_path / "workspace"
    git("init", "-b", "main", str(workspace))
    git("config", "user.name", "Repogents Test", cwd=workspace)
    git("config", "user.email", "repogents@example.test", cwd=workspace)
    tracked = workspace / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    git("add", "tracked.txt", cwd=workspace)
    git("commit", "-m", "base", cwd=workspace)
    git("checkout", "-b", "feature", cwd=workspace)
    (workspace / "feature.txt").write_text("feature\n", encoding="utf-8")
    git("add", "feature.txt", cwd=workspace)
    git("commit", "-m", "feature", cwd=workspace)
    feature_head = git("rev-parse", "HEAD", cwd=workspace).stdout.strip()
    git("checkout", "main", cwd=workspace)
    tracked.write_text("main update\n", encoding="utf-8")
    git("commit", "-am", "main update", cwd=workspace)
    git("checkout", "feature", cwd=workspace)
    generated = workspace / "generated.txt"
    generated.write_text("preserve generated work\n", encoding="utf-8")
    status_before = git("status", "--porcelain", cwd=workspace).stdout

    hook = workspace / ".git" / "hooks" / "pre-rebase"
    hook.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    hook.chmod(0o755)
    client = GitHubClient("placeholder-token", git_command_timeout=0.2)
    command = ["git", "rebase", "main"]

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._run_mutating_git(
            command, workspace=workspace, env=client._git_identity_env
        )

    assert captured.value.cmd == command
    assert git("symbolic-ref", "--short", "HEAD", cwd=workspace).stdout.strip() == "feature"
    assert git("rev-parse", "HEAD", cwd=workspace).stdout.strip() == feature_head
    assert git("status", "--porcelain", cwd=workspace).stdout == status_before
    assert generated.read_text(encoding="utf-8") == "preserve generated work\n"
    assert not (workspace / ".git" / "rebase-merge").exists()
    assert not (workspace / ".git" / "rebase-apply").exists()
    assert not (workspace / ".git" / "repogents-workspace-unusable").exists()
    assert not getattr(captured.value, "__notes__", [])
    assert not getattr(captured.value, "repogents_recovery_errors", [])

    hook.unlink()
    result = client._run_mutating_git(
        command, workspace=workspace, env=client._git_identity_env
    )
    assert result.returncode == 0
    assert git("symbolic-ref", "--short", "HEAD", cwd=workspace).stdout.strip() == "feature"
    assert generated.read_text(encoding="utf-8") == "preserve generated work\n"
    assert not (workspace / ".git" / "repogents-workspace-unusable").exists()


def test_rebase_abort_failure_is_benign_when_state_disappears_before_result(tmp_path):
    """A raced no-rebase abort result is ignored after Git removes its state."""
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    head = "1" * 40
    (git_dir / "refs" / "heads" / "main").write_text(head + "\n", encoding="ascii")
    rebase_state = git_dir / "rebase-merge"
    rebase_state.mkdir()
    calls = []

    def command_runner(args, *, cwd=None, env=None):
        calls.append(list(args))
        if args == ["git", "rebase", "--abort"]:
            shutil.rmtree(rebase_state)
            raise subprocess.CalledProcessError(
                128, args, stderr="fatal: No rebase in progress?"
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token", request=lambda *_args, **_kwargs: None,
        command_runner=command_runner,
    )
    client._recover_timed_out_git_mutation(
        workspace, client._workspace_snapshot(workspace), "rebase"
    )

    assert ["git", "rebase", "--abort"] in calls
    assert ["git", "checkout", "main"] in calls
    assert ["git", "reset", "--mixed", head] in calls
    assert not (git_dir / "repogents-workspace-unusable").exists()


def test_persistent_active_rebase_abort_failure_quarantines_and_preserves_timeout(tmp_path):
    """A genuine abort failure with active metadata retains quarantine semantics."""
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    head = "1" * 40
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(head + "\n", encoding="ascii")
    rebase_state = git_dir / "rebase-merge"
    rebase_state.mkdir()
    (rebase_state / "head-name").write_text("refs/heads/main\n", encoding="utf-8")
    generated = workspace / "generated.txt"
    generated.write_text("preserve generated work\n", encoding="utf-8")
    command = ["git", "rebase", "origin/main"]
    timeout = subprocess.TimeoutExpired(command, timeout=300.0)
    calls = []

    def command_runner(args, *, cwd=None, env=None):
        calls.append(list(args))
        if args == command:
            raise timeout
        if args == ["git", "rebase", "--abort"]:
            assert rebase_state.is_dir()
            raise subprocess.CalledProcessError(
                128, args, stderr="fatal: could not abort active rebase"
            )
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token", request=lambda *_args, **_kwargs: None,
        command_runner=command_runner,
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._run_mutating_git(
            command, workspace=workspace, env=client._git_identity_env
        )

    assert captured.value is timeout
    assert ["git", "rebase", "--abort"] in calls
    assert ["git", "checkout", "main"] in calls
    assert ["git", "reset", "--mixed", head] in calls
    assert not rebase_state.exists()
    assert generated.read_text(encoding="utf-8") == "preserve generated work\n"
    marker = git_dir / "repogents-workspace-unusable"
    assert marker.is_file()
    assert "git rebase --abort failed" in marker.read_text(encoding="utf-8")
    diagnostics = getattr(timeout, "__notes__", None) or getattr(
        timeout, "repogents_recovery_errors", []
    )
    assert any("could not fully recover Git workspace" in detail for detail in diagnostics)

    with pytest.raises(RuntimeError, match="requires recreation"):
        client.checkout("acme/widget", "main", workspace)


def test_publication_binary_patch_restore_failure_quarantines_without_masking_timeout(tmp_path):
    """Unsafe byte-patch restoration preserves timeout identity and quarantines."""
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    head = "1" * 40
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(head + "\n", encoding="ascii")
    original_index = b"binary-index\x00\xff"
    (git_dir / "index").write_bytes(b"checkout-mutated-index")
    generated = workspace / "generated.txt"
    generated.write_text("preserve generated work\n", encoding="utf-8")

    checkout = ["git", "checkout", "-B", "agent/issue-84"]
    timeout = subprocess.TimeoutExpired(checkout, timeout=300.0)
    patch = b"diff --git a/nonutf.txt b/nonutf.txt\n+raw byte: \xff\n"
    captured_patch = []

    def command_runner(args, *, cwd=None, env=None):
        if args == checkout:
            raise timeout
        if args[:3] == ["git", "apply", "--binary"]:
            patch_path = Path(args[3])
            captured_patch.append(patch_path.read_bytes())
            raise subprocess.CalledProcessError(1, args, stderr="patch rejected")
        return SimpleNamespace(stdout="", stderr="", returncode=0, args=args)

    client = GitHubClient(
        "placeholder-token",
        request=lambda *_args, **_kwargs: None,
        command_runner=command_runner,
    )
    snapshot = _GitWorkspaceSnapshot(
        head,
        "main",
        index=original_index,
        tracked_worktree_patch=patch,
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._run_mutating_git(
            checkout,
            workspace=workspace,
            env=client._git_command_env,
            snapshot=snapshot,
        )

    assert captured.value is timeout
    assert captured_patch == [patch]
    assert (git_dir / "index").read_bytes() == original_index
    assert list(git_dir.glob("repogents-worktree-*.patch")) == []
    assert generated.read_text(encoding="utf-8") == "preserve generated work\n"
    marker = git_dir / "repogents-workspace-unusable"
    assert marker.is_file()
    assert "could not restore pre-timeout Git HEAD" in marker.read_text(encoding="utf-8")
    diagnostics = getattr(timeout, "__notes__", None) or getattr(
        timeout, "repogents_recovery_errors", []
    )
    assert any("could not fully recover Git workspace" in detail for detail in diagnostics)


def test_default_git_timeout_marks_unconfirmed_tree_termination_unsafe(monkeypatch):
    """Termination failure is propagated on the original timeout, not swallowed."""
    command = ["git", "checkout", "feature"]
    timeout_error = subprocess.TimeoutExpired(command, timeout=0.1)

    class Process:
        pid = 12345
        returncode = None

        def communicate(self, *, timeout):
            if timeout == 0.1:
                raise timeout_error
            raise subprocess.TimeoutExpired(command, timeout=timeout)

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: Process())
    client = GitHubClient("placeholder-token", git_command_timeout=0.1)
    monkeypatch.setattr(
        client,
        "_terminate_git_process_tree",
        lambda process: (_ for _ in ()).throw(OSError("helper still alive")),
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._default_command_runner(command)

    assert captured.value is timeout_error
    assert timeout_error.repogents_git_tree_termination_safe is False
    diagnostics = getattr(timeout_error, "__notes__", None) or getattr(
        timeout_error, "repogents_recovery_errors", []
    )
    assert any(
        "could not fully terminate timed-out Git process tree: helper still alive"
        in detail
        for detail in diagnostics
    )


def test_unsafe_git_tree_termination_skips_workspace_recovery_and_quarantines(
    tmp_path, monkeypatch
):
    """Possible live helpers forbid every in-place recovery action."""
    workspace = tmp_path / "unsafe-workspace"
    git_dir = workspace / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(
        "1" * 40 + "\n", encoding="ascii"
    )
    stale_lock = git_dir / "index.lock"
    stale_lock.write_text("possibly active", encoding="utf-8")
    generated = workspace / "generated.txt"
    generated.write_text("preserve me", encoding="utf-8")
    command = ["git", "checkout", "feature"]
    timeout = subprocess.TimeoutExpired(command, timeout=0.1)
    popen_calls = []

    class Process:
        pid = 12345
        returncode = None

        def communicate(self, *, timeout):
            if timeout == 0.1:
                raise timeout_error
            raise subprocess.TimeoutExpired(command, timeout=timeout)

    timeout_error = timeout

    def popen(args, **kwargs):
        popen_calls.append((list(args), kwargs))
        return Process()

    monkeypatch.setattr("subprocess.Popen", popen)
    client = GitHubClient(
        "placeholder-token",
        request=lambda *_args, **_kwargs: None,
        git_command_timeout=0.1,
    )
    monkeypatch.setattr(
        client,
        "_terminate_git_process_tree",
        lambda process: (_ for _ in ()).throw(OSError("helper still alive")),
    )
    recovery_calls = []
    monkeypatch.setattr(
        client,
        "_recover_timed_out_git_mutation",
        lambda *args, **kwargs: recovery_calls.append((args, kwargs)),
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._run_mutating_git(
            command, workspace=workspace, env=client._git_command_env
        )

    assert captured.value is timeout
    assert timeout.repogents_git_tree_termination_safe is False
    assert [call[0] for call in popen_calls] == [command]
    assert popen_calls[0][1]["cwd"] == workspace
    assert recovery_calls == []
    # No lock cleanup, checkout/reset, rebase abort, or patch restoration ran.
    assert stale_lock.read_text(encoding="utf-8") == "possibly active"
    assert generated.read_text(encoding="utf-8") == "preserve me"
    marker = git_dir / "repogents-workspace-unusable"
    assert marker.read_text(encoding="utf-8") == (
        "Git process-tree termination could not be confirmed; in-place timeout "
        "recovery was skipped"
    )
    diagnostics = getattr(timeout, "__notes__", None) or getattr(
        timeout, "repogents_recovery_errors", []
    )
    assert any("helper still alive" in detail for detail in diagnostics)

    with pytest.raises(RuntimeError, match="requires recreation"):
        client.checkout("acme/widget", "main", workspace)
    with pytest.raises(RuntimeError, match="requires recreation"):
        client.publish("acme/widget", 85, "main", workspace, existing_pr=12)
    assert [call[0] for call in popen_calls] == [command]


@pytest.mark.parametrize(
    "taskkill_outcome", ["timeout", "error", "failure-status", "wait-timeout"]
)
def test_windows_taskkill_failure_marks_timeout_unsafe_and_quarantines_without_recovery(
    tmp_path, monkeypatch, taskkill_outcome
):
    """Every unconfirmed Windows task-tree outcome forbids workspace recovery."""
    workspace = tmp_path / f"windows-{taskkill_outcome}"
    git_dir = workspace / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(
        "1" * 40 + "\n", encoding="ascii"
    )
    stale_lock = git_dir / "index.lock"
    stale_lock.write_text("possibly active", encoding="utf-8")
    generated = workspace / "generated.txt"
    generated.write_text("preserve me", encoding="utf-8")
    command = ["git", "checkout", "feature"]
    timeout_error = subprocess.TimeoutExpired(command, timeout=0.1)

    class Process:
        pid = 43210
        returncode = None

        def communicate(self, *, timeout):
            if timeout == 0.1:
                raise timeout_error
            raise subprocess.TimeoutExpired(command, timeout=timeout)

        def wait(self, *, timeout):
            if taskkill_outcome == "wait-timeout":
                raise subprocess.TimeoutExpired(command, timeout=timeout)
            raise AssertionError("top-level wait must not hide failed taskkill")

        def poll(self):
            return None

    def taskkill(args, **kwargs):
        assert args == ["taskkill", "/PID", "43210", "/T", "/F"]
        assert kwargs["timeout"] == 0.5
        if taskkill_outcome == "timeout":
            raise subprocess.TimeoutExpired(args, timeout=0.5)
        if taskkill_outcome == "error":
            raise OSError("taskkill unavailable")
        return SimpleNamespace(
            returncode=0 if taskkill_outcome == "wait-timeout" else 5
        )

    monkeypatch.setattr("repogents.github.os.name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr("subprocess.run", taskkill)
    client = GitHubClient(
        "placeholder-token",
        request=lambda *_args, **_kwargs: None,
        git_command_timeout=0.1,
    )
    recovery_calls = []
    monkeypatch.setattr(
        client,
        "_recover_timed_out_git_mutation",
        lambda *args, **kwargs: recovery_calls.append((args, kwargs)),
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._run_mutating_git(
            command, workspace=workspace, env=client._git_command_env
        )

    assert captured.value is timeout_error
    assert timeout_error.repogents_git_tree_termination_safe is False
    assert recovery_calls == []
    assert stale_lock.read_text(encoding="utf-8") == "possibly active"
    assert generated.read_text(encoding="utf-8") == "preserve me"
    marker = git_dir / "repogents-workspace-unusable"
    assert "termination could not be confirmed" in marker.read_text(encoding="utf-8")
    diagnostics = getattr(timeout_error, "__notes__", None) or getattr(
        timeout_error, "repogents_recovery_errors", []
    )
    assert any("could not fully terminate timed-out Git process tree" in detail for detail in diagnostics)
    if taskkill_outcome == "failure-status":
        assert any("exit status 5" in detail for detail in diagnostics)
    if taskkill_outcome == "wait-timeout":
        assert any("did not exit after taskkill" in detail for detail in diagnostics)


def test_windows_confirmed_taskkill_and_git_exit_permit_timeout_recovery(
    tmp_path, monkeypatch
):
    """Successful task-tree termination retains the existing recovery path."""
    workspace = tmp_path / "windows-confirmed"
    git_dir = workspace / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    head = "1" * 40
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(head + "\n", encoding="ascii")
    command = ["git", "checkout", "feature"]
    timeout_error = subprocess.TimeoutExpired(command, timeout=0.1)

    class Process:
        pid = 54321
        returncode = None
        exited = False

        def communicate(self, *, timeout):
            if timeout == 0.1:
                raise timeout_error
            return "", ""

        def wait(self, *, timeout):
            self.exited = True
            self.returncode = 1
            return self.returncode

        def poll(self):
            return self.returncode if self.exited else None

    process = Process()
    taskkill_calls = []

    def taskkill(args, **kwargs):
        taskkill_calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("repogents.github.os.name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("subprocess.run", taskkill)
    client = GitHubClient(
        "placeholder-token",
        request=lambda *_args, **_kwargs: None,
        git_command_timeout=0.1,
    )
    recovery_calls = []
    monkeypatch.setattr(
        client,
        "_recover_timed_out_git_mutation",
        lambda *args, **kwargs: recovery_calls.append((args, kwargs)),
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client._run_mutating_git(
            command, workspace=workspace, env=client._git_command_env
        )

    assert captured.value is timeout_error
    assert timeout_error.repogents_git_tree_termination_safe is True
    assert len(taskkill_calls) == 1
    assert len(recovery_calls) == 1
    assert recovery_calls[0][0][0] == workspace
    assert recovery_calls[0][0][2] == "checkout"
    assert not (git_dir / "repogents-workspace-unusable").exists()
    assert not getattr(timeout_error, "__notes__", [])
    assert not getattr(timeout_error, "repogents_recovery_errors", [])


@pytest.mark.parametrize("preexisting", [False, True])
def test_unsafe_clone_timeout_quarantines_destination_without_cleanup(
    tmp_path, preexisting, monkeypatch
):
    """An unconfirmed clone tree is quarantined without touching its destination."""
    workspace = tmp_path / ("existing-target" if preexisting else "new-target")
    caller_file = workspace / "caller.txt"
    if preexisting:
        workspace.mkdir()
        caller_file.write_text("caller-owned\n", encoding="utf-8")
    clone_calls = []
    timeout = subprocess.TimeoutExpired(["git", "clone"], timeout=300.0)
    timeout.repogents_git_tree_termination_safe = False
    GitHubClient._record_recovery_error(
        timeout, "could not fully terminate timed-out Git process tree: helper alive"
    )

    def request(method, path, *, query=None, json_body=None):
        return {
            "full_name": "acme/unsafe",
            "default_branch": "main",
            "clone_url": "https://github.test/acme/unsafe.git",
        }

    def command_runner(args, *, cwd=None, env=None):
        clone_calls.append(list(args))
        (workspace / ".git" / "objects").mkdir(parents=True, exist_ok=True)
        (workspace / ".git" / "config").write_text("partial\n", encoding="utf-8")
        (workspace / "clone-created.tmp").write_text("still active\n", encoding="utf-8")
        raise timeout

    client = GitHubClient(
        "placeholder-token", request=request, command_runner=command_runner
    )
    destructive_cleanup_calls = []

    def forbidden_rmtree(path, *args, **kwargs):
        destructive_cleanup_calls.append(("rmtree", Path(path)))
        raise AssertionError("unsafe clone timeout must not remove a destination tree")

    def forbidden_unlink(path, *args, **kwargs):
        destructive_cleanup_calls.append(("unlink", Path(path)))
        raise AssertionError("unsafe clone timeout must not unlink destination state")

    monkeypatch.setattr("repogents.github.shutil.rmtree", forbidden_rmtree)
    monkeypatch.setattr(Path, "unlink", forbidden_unlink)

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        client.checkout("acme/unsafe", "main", workspace)

    assert captured.value is timeout
    assert timeout.repogents_git_tree_termination_safe is False
    assert (workspace / ".git" / "config").read_text(encoding="utf-8") == "partial\n"
    assert (workspace / "clone-created.tmp").read_text(encoding="utf-8") == (
        "still active\n"
    )
    if preexisting:
        assert caller_file.read_text(encoding="utf-8") == "caller-owned\n"
    quarantine = workspace.parent / f".{workspace.name}.repogents-workspace-unusable"
    assert quarantine.read_text(encoding="utf-8") == (
        "Git process-tree termination could not be confirmed; partial clone cleanup "
        "was skipped"
    )
    diagnostics = getattr(timeout, "__notes__", None) or getattr(
        timeout, "repogents_recovery_errors", []
    )
    assert any("helper alive" in detail for detail in diagnostics)

    # Reuse is rejected before metadata lookup or any clone/fetch/checkout/pull work.
    with pytest.raises(RuntimeError, match="requires recreation"):
        client.checkout("acme/unsafe", "main", workspace)
    with pytest.raises(RuntimeError, match="requires recreation"):
        client.publish("acme/unsafe", 87, "main", workspace, existing_pr=12)
    assert len(clone_calls) == 1
    assert destructive_cleanup_calls == []


def test_safe_clone_timeout_cleanup_contract_remains_retryable(tmp_path):
    """Confirmed process-tree exit retains the established cleanup and retry path."""
    workspace = tmp_path / "safe-target"
    attempts = 0

    def request(method, path, *, query=None, json_body=None):
        return {
            "full_name": "acme/safe",
            "default_branch": "main",
            "clone_url": "https://github.test/acme/safe.git",
        }

    def command_runner(args, *, cwd=None, env=None):
        nonlocal attempts
        attempts += 1
        (workspace / ".git").mkdir(parents=True, exist_ok=True)
        if attempts == 1:
            timeout = subprocess.TimeoutExpired(args, timeout=300.0)
            timeout.repogents_git_tree_termination_safe = True
            raise timeout
        (workspace / "README.md").write_text("complete\n", encoding="utf-8")
        return SimpleNamespace(stdout="")

    client = GitHubClient(
        "placeholder-token", request=request, command_runner=command_runner
    )
    with pytest.raises(subprocess.TimeoutExpired):
        client.checkout("acme/safe", "main", workspace)

    assert not workspace.exists()
    assert not (workspace.parent / ".safe-target.repogents-workspace-unusable").exists()
    assert client.checkout("acme/safe", "main", workspace) == workspace
    assert attempts == 2
    assert (workspace / "README.md").read_text(encoding="utf-8") == "complete\n"


@pytest.mark.parametrize("workers", [0, -1, True, 1.5])
def test_github_client_rejects_invalid_http_transport_worker_capacity(workers):
    with pytest.raises(ValueError, match="http_transport_max_workers"):
        GitHubClient("placeholder-token", http_transport_max_workers=workers)


def test_default_request_bounds_stalled_dns_with_fixed_transport_capacity(
    monkeypatch,
):
    """Resolver stalls consume only fixed complete-request workers and time out callers."""
    import socket

    capacity = 2
    resolver_started = threading.Barrier(capacity + 1)
    release_resolver = threading.Event()
    resolver_calls = 0
    resolver_guard = threading.Lock()
    successful_resolution = [False]

    real_getaddrinfo = socket.getaddrinfo

    def stalled_getaddrinfo(*args, **kwargs):
        nonlocal resolver_calls
        with resolver_guard:
            resolver_calls += 1
            call = resolver_calls
        if not successful_resolution[0] and call <= capacity:
            resolver_started.wait(timeout=2)
            assert release_resolver.wait(timeout=3)
        return real_getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)

    class Response:
        headers = {"Content-Type": "application/json"}

        def __init__(self):
            self.chunks = [
                b'{"full_name":"acme/recovered","default_branch":"main"}',
                b"",
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            return self.chunks.pop(0)

    def resolving_urlopen(request, *, timeout):
        socket.getaddrinfo("github.invalid", 443, type=socket.SOCK_STREAM)
        return Response()

    monkeypatch.setattr("socket.getaddrinfo", stalled_getaddrinfo)
    monkeypatch.setattr("urllib.request.urlopen", resolving_urlopen)
    client = GitHubClient(
        "placeholder-token",
        transport_timeout=0.08,
        http_transport_max_workers=capacity,
    )
    errors = []

    def request():
        try:
            client.repository("acme/stalled")
        except BaseException as error:
            errors.append(error)

    callers = [threading.Thread(target=request) for _ in range(6)]
    started = time.monotonic()
    for caller in callers:
        caller.start()
    resolver_started.wait(timeout=2)
    for caller in callers:
        caller.join(timeout=1)
        assert not caller.is_alive()

    assert time.monotonic() - started < 0.6
    assert len(errors) == 6
    assert all(isinstance(error, TimeoutError) for error in errors)
    assert resolver_calls == capacity
    assert len(client._http_transport_pool._workers) == capacity
    assert sum(worker.is_alive() for worker in client._http_transport_pool._workers) == capacity
    assert client._http_transport_pool._tasks.qsize() == 0

    # Resolver completion after caller timeout has no callback or decoded result, but
    # it releases the fixed slots for a later ordinary request.
    successful_resolution[0] = True
    release_resolver.set()
    deadline = time.monotonic() + 2
    while client._http_transport_pool._capacity._value < capacity:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    recovered = client.repository("acme/recovered")
    assert recovered["full_name"] == "acme/recovered"
    assert resolver_calls == capacity + 1


def test_dns_stalled_metadata_lookup_fails_authoritatively_and_recovers_capacity(
    monkeypatch, tmp_path
):
    """A resolver-stalled add becomes FAILED and late DNS cannot commit."""
    import socket

    from repogents.application import Application, ApplicationConfig
    from repogents.store import Store

    resolver_started = threading.Event()
    release_resolver = threading.Event()
    resolver_finished = threading.Event()
    resolver_calls = 0
    request_calls = 0

    def stalled_getaddrinfo(*_args, **_kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        resolver_started.set()
        assert release_resolver.wait(timeout=3)
        resolver_finished.set()
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    class Response:
        headers = {"Content-Type": "application/json"}

        def __init__(self, repository):
            self._repository = repository
            self._consumed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            assert size == 64 * 1024
            if self._consumed:
                return b""
            self._consumed = True
            return json.dumps(
                {
                    "full_name": self._repository,
                    "default_branch": "main",
                    "clone_url": f"https://github.test/{self._repository}.git",
                }
            ).encode("utf-8")

    def urlopen(request, *, timeout):
        nonlocal request_calls
        request_calls += 1
        if request_calls == 1:
            socket.getaddrinfo("github.invalid", 443, type=socket.SOCK_STREAM)
            repository = "acme/dns-stalled"
        else:
            repository = "acme/dns-recovered"
        return Response(repository)

    monkeypatch.setattr("socket.getaddrinfo", stalled_getaddrinfo)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    github = GitHubClient(
        "placeholder-token",
        transport_timeout=0.06,
        http_transport_max_workers=1,
    )
    store = Store(tmp_path / "dns-add.sqlite3")
    app = Application(
        store,
        github,
        object(),
        object(),
        ApplicationConfig(
            data_dir=tmp_path / "runtime",
            add_repository_lookup_timeout=0.5,
            add_repository_lookup_max_workers=1,
        ),
    )
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="total transport deadline"):
            app.add_repository("acme/dns-stalled", operation_id="dns-stalled-add")
        assert time.monotonic() - started < 0.4
        assert resolver_started.is_set()
        operation = app.repository_add_operation("dns-stalled-add")
        assert operation is not None
        assert operation["state"] == "FAILED"
        assert operation["repository"] is None
        assert store.list_repositories() == []

        # The abandoned transport worker has no storage authority. Once DNS returns,
        # it only releases fixed transport capacity for a later ordinary lookup.
        release_resolver.set()
        assert resolver_finished.wait(timeout=2)
        deadline = time.monotonic() + 2
        while github._http_transport_pool._capacity._value < 1:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert store.list_repositories() == []
        assert app.repository_add_operation("dns-stalled-add")["state"] == "FAILED"

        recovered = app.add_repository(
            "acme/dns-recovered", operation_id="dns-recovered-add"
        )
        assert recovered["github_repository"] == "acme/dns-recovered"
        assert app.repository_add_operation("dns-recovered-add")["state"] == "COMMITTED"
        assert request_calls == 2
        assert resolver_calls == 1
    finally:
        release_resolver.set()
        app.close()


def test_dns_stalled_poller_releases_service_ownership_after_deadline(
    monkeypatch, tmp_path
):
    """Shutdown owns a DNS-stalled poller until its bounded caller exits."""
    import socket

    from repogents.http_api import HttpService
    from repogents.service_ownership import (
        ServiceOwnership,
        ServiceOwnershipUnavailableError,
    )

    ownership_path = tmp_path / ".repogents-service.lock"
    resolver_started = threading.Event()
    release_resolver = threading.Event()
    late_mutations = []

    def stalled_getaddrinfo(*_args, **_kwargs):
        resolver_started.set()
        assert release_resolver.wait(timeout=3)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            return b"[]" if not hasattr(self, "consumed") else b""

    def urlopen(_request, *, timeout):
        socket.getaddrinfo("github.invalid", 443, type=socket.SOCK_STREAM)
        response = Response()
        response.consumed = False
        original_read = response.read

        def read(size=-1):
            if response.consumed:
                return b""
            response.consumed = True
            return original_read(size)

        response.read = read
        return response

    monkeypatch.setattr("socket.getaddrinfo", stalled_getaddrinfo)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    github = GitHubClient(
        "placeholder-token",
        transport_timeout=0.5,
        http_transport_max_workers=1,
    )

    class PollingApplication:
        def __init__(self):
            self.ownership = ServiceOwnership(ownership_path)
            self.closed = False

        def acquire_service_ownership(self):
            self.ownership.acquire()

        def poll_once(self):
            github._default_request("GET", "/repos/acme/widget/issues")
            late_mutations.append("old-poller")

        def state(self):
            return {"repositories": []}

        def close(self):
            self.closed = True
            self.ownership.close()

    application = PollingApplication()
    service = HttpService(application, "127.0.0.1", 0, 60)
    serving = threading.Thread(target=service.serve_forever)
    serving.start()
    try:
        assert resolver_started.wait(timeout=1)
        service.shutdown()
        serving.join(timeout=0.03)
        assert serving.is_alive()
        assert application.ownership.acquired is True

        competitor = ServiceOwnership(ownership_path)
        with pytest.raises(ServiceOwnershipUnavailableError):
            competitor.acquire()

        # The request caller reaches its absolute deadline even though the resolver
        # worker remains blocked. Service teardown can then release ownership without
        # waiting for DNS and no application callback runs after the timed-out call.
        serving.join(timeout=1)
        assert not serving.is_alive()
        assert application.closed is True
        assert application.ownership.acquired is False
        assert late_mutations == []

        replacement = ServiceOwnership(ownership_path)
        replacement.acquire()
        replacement.close()

        # Eventual resolver completion cannot revive the old poll callback.
        release_resolver.set()
        deadline = time.monotonic() + 2
        while github._http_transport_pool._capacity._value < 1:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert late_mutations == []
    finally:
        release_resolver.set()
        if serving.is_alive():
            serving.join(timeout=2)
