from __future__ import annotations
from base64 import b64encode

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from urllib import parse as urlparse
from urllib import request as urlrequest


_REVIEW_THREADS_QUERY = """
query ReviewThreads(
  $owner: String!
  $name: String!
  $number: Int!
  $after: String
) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes {
          id
          isResolved
          viewerCanResolve
          comments(first: 100) {
            nodes {
              id
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""

_REVIEW_THREAD_COMMENTS_QUERY = """
query ReviewThreadComments($threadId: ID!, $after: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      comments(first: 100, after: $after) {
        nodes {
          id
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""

_REVIEW_THREAD_QUERY = """
query ReviewThread($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      viewerCanResolve
    }
  }
}
"""

_RESOLVE_THREAD_MUTATION = """
mutation ResolveThread($input: ResolveReviewThreadInput!) {
  resolveReviewThread(input: $input) {
    thread {
      id
      isResolved
    }
  }
}
"""

_UPDATE_REFS_MUTATION = """
mutation UpdateRefs($input: UpdateRefsInput!) {
  updateRefs(input: $input) {
    clientMutationId
  }
}
"""
_ZERO_OID = "0" * 40

_FEEDBACK_MARKER_PREFIX = "<!-- repogents-feedback:"
_FOLLOW_UP_MARKER_PREFIX = "<!-- repogents-follow-up:"


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str
    url: str


@dataclass(frozen=True)
class GitHubFeedback:
    external_id: str
    kind: str
    body: str
    path: str | None = None
    line: int | None = None
    review_thread_id: str | None = None
    top_level_comment_id: int | None = None


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    branch: str
    state: str
    merged: bool
    diff: str
    head_sha: str = ""


@dataclass(frozen=True)
class PublicationCandidate:
    branch: str
    head_sha: str
    target_head_sha: str
    remote_head_sha: str


@dataclass(frozen=True)
class FeedbackAddress:
    status: str
    response_url: str


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_base: str = "https://api.github.com",
        request=None,
        command_runner=None,
        binary_command_runner=None,
    ):
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._request = request or self._default_request
        self._command_runner = command_runner or self._default_command_runner
        self._repository_operation_binary_runner = (
            binary_command_runner or self._default_binary_command_runner
        )
        credential = b64encode(f"x-access-token:{token}".encode()).decode()
        self._git_command_env = {"GIT_TERMINAL_PROMPT": "0"}
        self._git_auth_env = {
            **self._git_command_env,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
        }
        self._git_identity_env = {
            **self._git_command_env,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "Repogents",
            "GIT_CONFIG_KEY_1": "user.email",
            "GIT_CONFIG_VALUE_1": "repogents@localhost",
        }
        self._git_literal_path_env = {
            **self._git_command_env,
            "GIT_LITERAL_PATHSPECS": "1",
        }
        self._git_rebase_continue_env = {
            **self._git_identity_env,
            "GIT_EDITOR": "true",
        }

    def _default_request(self, method, path, *, query=None, json_body=None):
        accept = "application/vnd.github+json"
        if path.endswith(".diff"):
            path = path.removesuffix(".diff")
            accept = "application/vnd.github.diff"
        url = f"{self._api_base}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urlparse.urlencode(query, doseq=True)}"
        body = None if json_body is None else json.dumps(json_body).encode("utf-8")
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "repogents",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        http_request = urlrequest.Request(url, data=body, headers=headers, method=method)
        with urlrequest.urlopen(http_request) as response:
            payload = response.read()
            if not payload:
                return None
            content_type = response.headers.get("Content-Type", "")
            text = payload.decode("utf-8")
            if "json" in content_type:
                return json.loads(text)
            return text

    @staticmethod
    def _default_command_runner(args, *, cwd=None, env=None):
        command_env = os.environ.copy()
        if env:
            command_env.update(env)
        return subprocess.run(
            args,
            cwd=cwd,
            env=command_env,
            check=True,
            text=True,
            capture_output=True,
        )

    @staticmethod
    def _default_binary_command_runner(args, *, cwd=None, env=None):
        command_env = os.environ.copy()
        if env:
            command_env.update(env)
        return subprocess.run(
            args,
            cwd=cwd,
            env=command_env,
            check=True,
            text=False,
            capture_output=True,
        )


    def repository(self, github_repository: str) -> dict:
        repository = self._request("GET", f"/repos/{github_repository}")
        if not isinstance(repository, dict):
            raise RuntimeError("GitHub returned an invalid repository")
        return repository


    def list_ready_issues(self, github_repository: str) -> list[GitHubIssue]:
        issues = []
        page = 1
        while True:
            page_issues = self._request(
                "GET",
                f"/repos/{github_repository}/issues",
                query={
                    "state": "open",
                    "labels": "agent:ready",
                    "per_page": 100,
                    "page": page,
                },
            )
            if not isinstance(page_issues, list):
                raise RuntimeError("GitHub returned an invalid issues page")
            issues.extend(page_issues)
            if len(page_issues) < 100:
                break
            page += 1
        return [
            GitHubIssue(
                number=issue["number"],
                title=issue["title"],
                body=issue.get("body") or "",
                url=issue["html_url"],
            )
            for issue in issues
            if "pull_request" not in issue
        ]

    def ensure_follow_up_issue(
        self,
        github_repository: str,
        external_id: str,
        title: str,
        body: str,
    ) -> GitHubIssue:
        self._validate_github_repository(github_repository)
        self._validate_feedback_external_id(external_id)
        if not isinstance(title, str):
            raise TypeError("title must be a string")
        if not title.strip():
            raise ValueError("title must not be empty")
        if not isinstance(body, str):
            raise TypeError("body must be a string")
        if not body.strip():
            raise ValueError("body must not be empty")
        if _FOLLOW_UP_MARKER_PREFIX in body:
            raise ValueError("body cannot contain a follow-up marker")

        marker = f"{_FOLLOW_UP_MARKER_PREFIX}{external_id} -->"
        marked_body = f"{body}\n\n{marker}"
        issues_path = f"/repos/{github_repository}/issues"
        matching_issue = None
        page = 1
        while True:
            page_issues = self._request(
                "GET",
                issues_path,
                query={"state": "all", "per_page": 100, "page": page},
            )
            if not isinstance(page_issues, list):
                raise RuntimeError("GitHub returned an invalid issues page")
            for issue in page_issues:
                if not isinstance(issue, dict):
                    raise RuntimeError("GitHub returned an invalid issue")
                if "pull_request" in issue:
                    continue
                issue_body = issue.get("body") or ""
                if not isinstance(issue_body, str):
                    raise RuntimeError("GitHub returned an invalid issue body")
                if marker not in issue_body:
                    continue
                if issue_body.count(_FOLLOW_UP_MARKER_PREFIX) != 1:
                    raise RuntimeError(
                        "GitHub follow-up issue has invalid marker content"
                    )
                candidate = self._parse_github_issue(issue)
                if (
                    matching_issue is not None
                    and matching_issue.number != candidate.number
                ):
                    raise RuntimeError(
                        "GitHub returned duplicate follow-up issues"
                    )
                matching_issue = candidate
            if len(page_issues) < 100:
                break
            page += 1

        if matching_issue is not None:
            return matching_issue

        created = self._request(
            "POST",
            issues_path,
            json_body={
                "title": title,
                "body": marked_body,
                "labels": ["agent:ready"],
            },
        )
        created_issue = self._parse_github_issue(created)
        if created_issue.title != title or created_issue.body != marked_body:
            raise RuntimeError("GitHub returned a mismatched follow-up issue")
        return created_issue

    def checkout(
        self,
        github_repository: str,
        target_branch: str,
        workspace: str | Path,
    ) -> Path:
        workspace_path = Path(workspace)
        if (workspace_path / ".git").is_dir():
            for args, environment in (
                (["git", "fetch", "origin", target_branch], self._git_auth_env),
                (["git", "checkout", target_branch], self._git_command_env),
                (
                    ["git", "pull", "--ff-only", "origin", target_branch],
                    self._git_auth_env,
                ),
            ):
                self._command_runner(args, cwd=workspace_path, env=environment)
            return workspace_path
        metadata = self.repository(github_repository)
        self._command_runner(
            [
                "git",
                "clone",
                "--branch",
                target_branch,
                "--single-branch",
                metadata["clone_url"],
                str(workspace_path),
            ],
            cwd=None,
            env=self._git_auth_env,
        )
        return workspace_path

    def candidate_diff(
        self,
        target_branch: str,
        workspace: str | Path,
        *,
        candidate: PublicationCandidate,
    ) -> str:
        self._validate_target_branch(target_branch)
        if not isinstance(workspace, (str, Path)):
            raise TypeError("workspace must be a string or Path")
        if isinstance(workspace, str) and not workspace:
            raise ValueError("workspace must not be empty")
        if not isinstance(candidate, PublicationCandidate):
            raise TypeError("candidate must be a PublicationCandidate")

        workspace_path = Path(workspace)
        if self._rev_parse_commit("HEAD", workspace_path) != candidate.head_sha:
            raise RuntimeError("workspace HEAD moved after candidate preparation")
        target_ref = f"refs/remotes/origin/{target_branch}"
        self._command_runner(
            [
                "git",
                "fetch",
                "origin",
                f"+refs/heads/{target_branch}:{target_ref}",
            ],
            cwd=workspace_path,
            env=self._git_auth_env,
        )
        if (
            self._rev_parse_commit(target_ref, workspace_path)
            != candidate.target_head_sha
        ):
            raise RuntimeError("target branch moved after candidate preparation")
        candidate_diff = self._command_runner(
            [
                "git",
                "diff",
                "--no-color",
                candidate.target_head_sha,
                candidate.head_sha,
                "--",
            ],
            cwd=workspace_path,
            env=self._git_command_env,
        )
        if not isinstance(candidate_diff.stdout, str):
            raise RuntimeError("git diff returned invalid output")
        return candidate_diff.stdout

    def publish_validated_to_target(
        self,
        github_repository: str,
        target_branch: str,
        workspace: str | Path,
        expected_head: str,
        *,
        issue_branch: str,
    ) -> bool:
        self._validate_github_repository(github_repository)
        self._validate_target_branch(target_branch)
        self._validate_target_branch(issue_branch)
        if not isinstance(workspace, (str, Path)):
            raise TypeError("workspace must be a string or Path")
        if isinstance(workspace, str) and not workspace:
            raise ValueError("workspace must not be empty")
        if not self._is_commit_sha(expected_head):
            raise ValueError(
                "expected_head must be a full hexadecimal commit SHA"
            )

        workspace_path = Path(workspace)
        local_head = self._rev_parse_commit("HEAD", workspace_path)
        if local_head != expected_head:
            raise RuntimeError(
                "workspace HEAD does not match the validated head"
            )
        if (
            self._remote_issue_branch_head(issue_branch, workspace_path)
            != expected_head
        ):
            return False
        self._command_runner(
            ["git", "fetch", "origin", target_branch],
            cwd=workspace_path,
            env=self._git_auth_env,
        )
        target_head = self._rev_parse_commit(
            f"origin/{target_branch}",
            workspace_path,
        )
        if target_head == expected_head:
            return True
        if not self._commit_is_ancestor(
            target_head,
            expected_head,
            workspace_path,
        ):
            return self._commit_is_ancestor(
                expected_head,
                target_head,
                workspace_path,
            )

        issue_ref = f"refs/heads/{issue_branch}"
        target_ref = f"refs/heads/{target_branch}"
        repository_id = self._repository_node_id(github_repository)
        return self._update_refs(
            repository_id,
            [
                (issue_ref, expected_head, expected_head),
                (target_ref, target_head, expected_head),
            ],
        )

    def pull_request(self, github_repository: str, number: int) -> PullRequest:
        path = f"/repos/{github_repository}/pulls/{number}"
        pull = self._request("GET", path)
        diff = self._request("GET", f"{path}.diff")
        if not isinstance(pull, dict):
            raise RuntimeError("GitHub returned an invalid pull request")
        if not isinstance(diff, str):
            raise RuntimeError("GitHub returned an invalid pull request diff")
        return PullRequest(
            number=pull["number"],
            url=pull["html_url"],
            branch=pull["head"]["ref"],
            state=pull["state"],
            merged=bool(pull["merged"]),
            diff=diff,
            head_sha=pull["head"]["sha"],
        )

    @staticmethod
    def _is_commit_sha(value) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 40
            and all(
                character in "0123456789abcdefABCDEF"
                for character in value
            )
        )

    @staticmethod
    def _validate_issue_number(issue_number: int) -> None:
        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")

    @staticmethod
    def _publication_workspace(workspace: str | Path) -> Path:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("workspace must be a string or Path")
        if isinstance(workspace, str) and not workspace:
            raise ValueError("workspace must not be empty")
        return Path(workspace)

    @classmethod
    def _validate_publication_candidate(
        cls,
        candidate: PublicationCandidate,
        issue_number: int,
    ) -> None:
        if not isinstance(candidate, PublicationCandidate):
            raise TypeError("candidate must be a PublicationCandidate")
        expected_branch = f"agent/issue-{issue_number}"
        if candidate.branch != expected_branch:
            raise ValueError("candidate branch does not match issue_number")
        for name, value in (
            ("head_sha", candidate.head_sha),
            ("target_head_sha", candidate.target_head_sha),
        ):
            if not cls._is_commit_sha(value):
                raise ValueError(
                    f"candidate {name} must be a full hexadecimal commit SHA"
                )
        if (
            candidate.remote_head_sha
            and not cls._is_commit_sha(candidate.remote_head_sha)
        ):
            raise ValueError(
                "candidate remote_head_sha must be empty or a full "
                "hexadecimal commit SHA"
            )
        if not isinstance(candidate.remote_head_sha, str):
            raise ValueError(
                "candidate remote_head_sha must be empty or a full "
                "hexadecimal commit SHA"
            )

    def _rev_parse_commit(
        self,
        revision: str,
        workspace_path: Path,
    ) -> str:
        result = self._command_runner(
            ["git", "rev-parse", revision],
            cwd=workspace_path,
            env=self._git_command_env,
        )
        stdout = getattr(result, "stdout", None)
        commit_sha = stdout.strip() if isinstance(stdout, str) else ""
        if not self._is_commit_sha(commit_sha):
            raise RuntimeError(
                f"git rev-parse {revision} returned an invalid commit SHA"
            )
        return commit_sha

    def _commit_is_ancestor(
        self,
        ancestor: str,
        descendant: str,
        workspace_path: Path,
    ) -> bool:
        try:
            self._command_runner(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    ancestor,
                    descendant,
                ],
                cwd=workspace_path,
                env=self._git_command_env,
            )
        except subprocess.CalledProcessError as error:
            if error.returncode == 1:
                return False
            raise
        return True

    def _remote_issue_branch_head(
        self,
        branch: str,
        workspace_path: Path,
    ) -> str:
        remote_ref = f"refs/heads/{branch}"
        result = self._command_runner(
            ["git", "ls-remote", "--heads", "origin", remote_ref],
            cwd=workspace_path,
            env=self._git_auth_env,
        )
        stdout = getattr(result, "stdout", None)
        if not isinstance(stdout, str):
            raise RuntimeError("git ls-remote returned an invalid branch ref")
        remote_branch = stdout.strip()
        if not remote_branch:
            return ""
        fields = remote_branch.split()
        if (
            len(fields) != 2
            or fields[1] != remote_ref
            or not self._is_commit_sha(fields[0])
        ):
            raise RuntimeError("git ls-remote returned an invalid branch ref")
        return fields[0]

    def _repository_node_id(self, github_repository: str) -> str:
        repository = self.repository(github_repository)
        node_id = repository.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise RuntimeError("GitHub repository has no valid node ID")
        return node_id

    def _update_refs(
        self,
        repository_id: str,
        updates: list[tuple[str, str, str]],
    ) -> bool:
        if not updates:
            raise ValueError("reference transaction requires at least one update")
        refs = [ref for ref, _old_oid, _new_oid in updates]
        if len(refs) != len(set(refs)):
            raise ValueError("reference transaction contains a duplicate ref")
        ref_updates = []
        for ref, old_oid, new_oid in updates:
            if (
                not ref.startswith("refs/heads/")
                or not self._is_commit_sha(old_oid)
                and old_oid != _ZERO_OID
                or not self._is_commit_sha(new_oid)
                and new_oid != _ZERO_OID
            ):
                raise ValueError("reference transaction contains an invalid update")
            ref_updates.append(
                {
                    "name": ref,
                    "beforeOid": old_oid,
                    "afterOid": new_oid,
                    "force": False,
                }
            )
        payload = self._request(
            "POST",
            "/graphql",
            json_body={
                "query": _UPDATE_REFS_MUTATION,
                "variables": {
                    "input": {
                        "repositoryId": repository_id,
                        "refUpdates": ref_updates,
                    }
                },
            },
        )
        if isinstance(payload, dict) and payload.get("errors"):
            return False
        data = self._graphql_data(payload, "reference transaction")
        if not isinstance(data.get("updateRefs"), dict):
            raise RuntimeError(
                "GitHub reference transaction returned an invalid result"
            )
        return True

    @staticmethod
    def _repository_relative_path(semantic_path: str) -> Path:
        if not isinstance(semantic_path, str):
            raise TypeError("repository path must be a string")
        parts = semantic_path.split("/")
        relative_path = Path(*parts)
        if (
            not semantic_path
            or "\0" in semantic_path
            or relative_path.is_absolute()
            or bool(relative_path.drive)
            or any(part in {"", ".", ".."} for part in parts)
            or any(part == ".." for part in relative_path.parts)
            or (
                relative_path.parts
                and relative_path.parts[0].casefold() in {".git", ".repogents"}
            )
        ):
            raise ValueError(
                "repository path must be a secure repository-relative "
                "source path"
            )
        return relative_path

    @staticmethod
    def _rebase_in_progress(workspace_path: Path) -> bool:
        git_dir = workspace_path / ".git"
        return any(
            (git_dir / metadata).exists()
            for metadata in ("rebase-merge", "rebase-apply")
        )

    def _unmerged_repository_entries(
        self,
        workspace_path: Path,
    ) -> dict[str, dict[str, str]]:
        result = self._repository_operation_binary_runner(
            ["git", "ls-files", "--unmerged", "-z"],
            cwd=workspace_path,
            env=self._git_command_env,
        )
        stdout = getattr(result, "stdout", None)
        if not isinstance(stdout, bytes):
            raise RuntimeError("git ls-files returned invalid binary output")

        stage_names = {b"1": "base", b"2": "ours", b"3": "theirs"}
        entries: dict[str, dict[str, str]] = {}
        for encoded_entry in stdout.split(b"\0"):
            if not encoded_entry:
                continue
            header, separator, encoded_path = encoded_entry.partition(b"\t")
            fields = header.split()
            if not separator or not encoded_path or len(fields) != 3:
                raise RuntimeError(
                    "git ls-files returned an invalid unmerged entry"
                )
            _mode, encoded_object_id, encoded_stage = fields
            stage = stage_names.get(encoded_stage)
            try:
                object_id = encoded_object_id.decode("ascii")
            except UnicodeDecodeError as error:
                raise RuntimeError(
                    "git ls-files returned an invalid unmerged object ID"
                ) from error
            if (
                stage is None
                or len(object_id) not in {40, 64}
                or any(
                    character not in "0123456789abcdefABCDEF"
                    for character in object_id
                )
            ):
                raise RuntimeError(
                    "git ls-files returned an invalid unmerged entry"
                )

            semantic_path = os.fsdecode(encoded_path)
            self._repository_relative_path(semantic_path)
            stages = entries.setdefault(semantic_path, {})
            if stage in stages:
                raise RuntimeError(
                    "git ls-files returned a duplicate unmerged stage"
                )
            stages[stage] = object_id
        return entries

    def _nul_delimited_repository_paths(
        self,
        workspace_path: Path,
        args: list[str],
    ) -> list[str]:
        result = self._repository_operation_binary_runner(
            args,
            cwd=workspace_path,
            env=self._git_command_env,
        )
        stdout = getattr(result, "stdout", None)
        if not isinstance(stdout, bytes):
            raise RuntimeError(
                f"{' '.join(args[:2])} returned invalid binary output"
            )

        paths: set[str] = set()
        for encoded_path in stdout.split(b"\0"):
            if not encoded_path:
                continue
            semantic_path = os.fsdecode(encoded_path)
            self._repository_relative_path(semantic_path)
            paths.add(semantic_path)
        return sorted(paths)

    def repository_operation_state(
        self,
        workspace: str | Path,
    ) -> dict[str, bool | list[str]]:
        workspace_path = self._publication_workspace(workspace)
        unmerged_paths = sorted(
            self._unmerged_repository_entries(workspace_path)
        )
        unmerged_path_set = set(unmerged_paths)
        staged_paths = self._nul_delimited_repository_paths(
            workspace_path,
            ["git", "diff", "--cached", "--name-only", "-z"],
        )
        unstaged_paths = self._nul_delimited_repository_paths(
            workspace_path,
            ["git", "diff", "--name-only", "-z"],
        )
        untracked_paths = self._nul_delimited_repository_paths(
            workspace_path,
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        )
        return {
            "rebase_in_progress": self._rebase_in_progress(workspace_path),
            "unmerged_paths": unmerged_paths,
            "staged_paths": [
                path for path in staged_paths if path not in unmerged_path_set
            ],
            "unstaged_paths": [
                path
                for path in unstaged_paths
                if path not in unmerged_path_set
            ],
            "untracked_paths": untracked_paths,
        }

    def export_repository_operation_artifacts(
        self,
        workspace: str | Path,
        destination: str | Path,
    ) -> dict[str, dict[str, str]]:
        workspace_path = self._publication_workspace(workspace)
        destination_path = self._publication_workspace(destination)
        destination_path.mkdir(parents=True, exist_ok=True)
        destination_root = destination_path.resolve()
        entries = self._unmerged_repository_entries(workspace_path)
        manifest: dict[str, dict[str, str]] = {}

        for semantic_path in sorted(entries):
            relative_path = self._repository_relative_path(semantic_path)
            artifacts: dict[str, str] = {}
            for stage in ("base", "ours", "theirs"):
                object_id = entries[semantic_path].get(stage)
                if object_id is None:
                    continue
                result = self._repository_operation_binary_runner(
                    ["git", "cat-file", "blob", object_id],
                    cwd=workspace_path,
                    env=self._git_command_env,
                )
                contents = getattr(result, "stdout", None)
                if not isinstance(contents, bytes):
                    raise RuntimeError(
                        "git cat-file returned invalid binary output"
                    )

                artifact_relative = Path(stage) / relative_path
                artifact_path = destination_root / artifact_relative
                artifact_parent = destination_root
                for part in artifact_relative.parent.parts:
                    artifact_parent /= part
                    if artifact_parent.is_symlink():
                        raise ValueError(
                            "artifact destination contains a symbolic-link "
                            "directory"
                        )
                    artifact_parent.mkdir(exist_ok=True)
                    if not artifact_parent.is_dir():
                        raise ValueError(
                            "artifact destination contains a non-directory "
                            "parent"
                        )
                if artifact_path.is_symlink():
                    raise ValueError(
                        "artifact destination contains a symbolic-link "
                        "artifact"
                    )
                artifact_path.write_bytes(contents)
                artifacts[stage] = artifact_relative.as_posix()
            manifest[semantic_path] = artifacts
        return manifest

    def continue_repository_operation(
        self,
        workspace: str | Path,
        paths: list[str],
    ) -> bool:
        workspace_path = self._publication_workspace(workspace)
        if not self._rebase_in_progress(workspace_path):
            return False
        if not isinstance(paths, list):
            raise TypeError("paths must be a list")

        repository_paths = []
        seen_paths = set()
        for semantic_path in paths:
            self._repository_relative_path(semantic_path)
            if semantic_path not in seen_paths:
                repository_paths.append(semantic_path)
                seen_paths.add(semantic_path)
        if repository_paths:
            self._command_runner(
                ["git", "add", "--", *repository_paths],
                cwd=workspace_path,
                env=self._git_literal_path_env,
            )
        self._command_runner(
            ["git", "rebase", "--continue"],
            cwd=workspace_path,
            env=self._git_rebase_continue_env,
        )
        return True

    def _prepare_issue_branch(
        self,
        issue_number: int,
        target_branch: str,
        workspace_path: Path,
    ) -> tuple[str, str]:
        branch = f"agent/issue-{issue_number}"
        self._command_runner(
            ["git", "checkout", "-B", branch],
            cwd=workspace_path,
            env=self._git_command_env,
        )
        self._command_runner(
            ["git", "fetch", "origin", target_branch],
            cwd=workspace_path,
            env=self._git_auth_env,
        )
        remote_head = self._remote_issue_branch_head(branch, workspace_path)
        if remote_head:
            self._command_runner(
                ["git", "fetch", "origin", f"refs/heads/{branch}"],
                cwd=workspace_path,
                env=self._git_auth_env,
            )
        self._command_runner(
            ["git", "add", "--all"],
            cwd=workspace_path,
            env=self._git_command_env,
        )
        pending = self._command_runner(
            ["git", "diff", "--cached", "--name-only"],
            cwd=workspace_path,
            env=self._git_command_env,
        )
        if pending.stdout.strip():
            self._command_runner(
                ["git", "commit", "-m", f"Resolve issue #{issue_number}"],
                cwd=workspace_path,
                env=self._git_identity_env,
            )
        self._command_runner(
            ["git", "rebase", f"origin/{target_branch}"],
            cwd=workspace_path,
            env=self._git_identity_env,
        )
        local_head = self._rev_parse_commit("HEAD", workspace_path)
        if not remote_head or local_head != remote_head:
            self._command_runner(
                ["git", "reset", "--soft", f"origin/{target_branch}"],
                cwd=workspace_path,
                env=self._git_command_env,
            )
            squashed = self._command_runner(
                ["git", "diff", "--cached", "--name-only"],
                cwd=workspace_path,
                env=self._git_command_env,
            )
            if squashed.stdout.strip():
                self._command_runner(
                    ["git", "commit", "-m", f"Resolve issue #{issue_number}"],
                    cwd=workspace_path,
                    env=self._git_identity_env,
                )
        return branch, remote_head

    def _find_open_pull_number(
        self,
        github_repository: str,
        branch: str,
        target_branch: str,
    ) -> int | None:
        open_pulls = []
        page = 1
        while True:
            page_pulls = self._request(
                "GET",
                f"/repos/{github_repository}/pulls",
                query={"state": "open", "per_page": 100, "page": page},
            )
            if not isinstance(page_pulls, list):
                raise RuntimeError("GitHub returned an invalid pulls page")
            open_pulls.extend(page_pulls)
            if len(page_pulls) < 100:
                break
            page += 1
        for pull in open_pulls:
            head = pull.get("head", {})
            head_repository = (head.get("repo") or {}).get("full_name")
            if (
                head.get("ref") == branch
                and pull.get("base", {}).get("ref") == target_branch
                and (
                    head_repository is None
                    or head_repository == github_repository
                )
            ):
                return pull["number"]
        return None

    def _finish_publication(
        self,
        github_repository: str,
        issue_number: int,
        target_branch: str,
        branch: str,
        head_sha: str,
        pull_number: int | None,
    ) -> PullRequest:
        if pull_number is None:
            created = self._request(
                "POST",
                f"/repos/{github_repository}/pulls",
                json_body={
                    "title": f"Resolve issue #{issue_number}",
                    "head": branch,
                    "base": target_branch,
                    "body": f"Closes #{issue_number}",
                },
            )
            if not isinstance(created, dict):
                raise RuntimeError(
                    "GitHub returned an invalid created pull request"
                )
            created_number = created.get("number")
            if not isinstance(created_number, int) or created_number <= 0:
                raise RuntimeError(
                    "GitHub returned an invalid created pull request"
                )
            pull_number = created_number
        pull = self.pull_request(github_repository, pull_number)
        return PullRequest(
            number=pull.number,
            url=pull.url,
            branch=pull.branch,
            state=pull.state,
            merged=pull.merged,
            diff=pull.diff,
            head_sha=head_sha,
        )

    def prepare_publication(
        self,
        issue_number: int,
        target_branch: str,
        workspace: str | Path,
    ) -> tuple[PublicationCandidate, str]:
        self._validate_issue_number(issue_number)
        self._validate_target_branch(target_branch)
        workspace_path = self._publication_workspace(workspace)

        branch, remote_head = self._prepare_issue_branch(
            issue_number,
            target_branch,
            workspace_path,
        )
        target_head = self._rev_parse_commit(
            f"origin/{target_branch}",
            workspace_path,
        )
        head = self._rev_parse_commit("HEAD", workspace_path)
        if remote_head and head != remote_head:
            tree = self._command_runner(
                ["git", "write-tree"],
                cwd=workspace_path,
                env=self._git_command_env,
            )
            tree_oid = (
                tree.stdout.strip()
                if isinstance(tree.stdout, str)
                else ""
            )
            if not self._is_commit_sha(tree_oid):
                raise RuntimeError("git write-tree returned an invalid tree")
            commit_args = [
                "git",
                "commit-tree",
                tree_oid,
                "-p",
                remote_head,
            ]
            if not self._commit_is_ancestor(
                target_head,
                remote_head,
                workspace_path,
            ):
                commit_args.extend(["-p", target_head])
            commit_args.extend(
                ["-m", f"Resolve issue #{issue_number}"]
            )
            commit = self._command_runner(
                commit_args,
                cwd=workspace_path,
                env=self._git_identity_env,
            )
            head = (
                commit.stdout.strip()
                if isinstance(commit.stdout, str)
                else ""
            )
            if not self._is_commit_sha(head):
                raise RuntimeError(
                    "git commit-tree returned an invalid commit"
                )
            self._command_runner(
                ["git", "reset", "--soft", head],
                cwd=workspace_path,
                env=self._git_command_env,
            )
        candidate_diff = self._command_runner(
            ["git", "diff", "--no-color", target_head, head, "--"],
            cwd=workspace_path,
            env=self._git_command_env,
        )
        if not isinstance(candidate_diff.stdout, str):
            raise RuntimeError("git diff returned invalid output")
        return (
            PublicationCandidate(
                branch=branch,
                head_sha=head,
                target_head_sha=target_head,
                remote_head_sha=remote_head,
            ),
            candidate_diff.stdout,
        )

    def publish(
        self,
        github_repository: str,
        issue_number: int,
        target_branch: str,
        workspace: str | Path,
        existing_pr: int | None = None,
    ) -> PullRequest:
        branch = f"agent/issue-{issue_number}"
        pull_number = existing_pr
        if pull_number is None:
            pull_number = self._find_open_pull_number(
                github_repository,
                branch,
                target_branch,
            )
        workspace_path = Path(workspace)
        branch, remote_head = self._prepare_issue_branch(
            issue_number,
            target_branch,
            workspace_path,
        )
        remote_ref = f"refs/heads/{branch}"
        self._command_runner(
            [
                "git",
                "push",
                f"--force-with-lease={remote_ref}:{remote_head}",
                "--set-upstream",
                "origin",
                branch,
            ],
            cwd=workspace_path,
            env=self._git_auth_env,
        )
        pushed_head = self._rev_parse_commit("HEAD", workspace_path)
        return self._finish_publication(
            github_repository,
            issue_number,
            target_branch,
            branch,
            pushed_head,
            pull_number,
        )

    def publish_prepared(
        self,
        github_repository: str,
        issue_number: int,
        target_branch: str,
        workspace: str | Path,
        candidate: PublicationCandidate,
        existing_pr: int | None = None,
    ) -> PullRequest | None:
        self._validate_github_repository(github_repository)
        self._validate_issue_number(issue_number)
        self._validate_target_branch(target_branch)
        workspace_path = self._publication_workspace(workspace)
        self._validate_publication_candidate(candidate, issue_number)
        if existing_pr is not None and (
            type(existing_pr) is not int or existing_pr <= 0
        ):
            raise ValueError("existing_pr must be a positive integer or None")

        if (
            self._rev_parse_commit("HEAD", workspace_path)
            != candidate.head_sha
        ):
            return None
        remote_head = self._remote_issue_branch_head(
            candidate.branch,
            workspace_path,
        )
        if remote_head == candidate.head_sha:
            pull_number = existing_pr
            if pull_number is None:
                pull_number = self._find_open_pull_number(
                    github_repository,
                    candidate.branch,
                    target_branch,
                )
            return self._finish_publication(
                github_repository,
                issue_number,
                target_branch,
                candidate.branch,
                candidate.head_sha,
                pull_number,
            )
        if remote_head != candidate.remote_head_sha:
            return None

        self._command_runner(
            ["git", "fetch", "origin", target_branch],
            cwd=workspace_path,
            env=self._git_auth_env,
        )
        if (
            self._rev_parse_commit(
                f"origin/{target_branch}",
                workspace_path,
            )
            != candidate.target_head_sha
        ):
            return None

        remote_ref = f"refs/heads/{candidate.branch}"
        target_ref = f"refs/heads/{target_branch}"
        staging_branch = f"repogents/staging/issue-{issue_number}"
        staging_ref = f"refs/heads/{staging_branch}"
        repository_id = self._repository_node_id(github_repository)
        staging_head = self._remote_issue_branch_head(
            staging_branch,
            workspace_path,
        )
        if staging_head != candidate.head_sha:
            self._command_runner(
                [
                    "git",
                    "push",
                    f"--force-with-lease={staging_ref}:{staging_head}",
                    "origin",
                    f"{candidate.head_sha}:{staging_ref}",
                ],
                cwd=workspace_path,
                env=self._git_auth_env,
            )
        if not self._update_refs(
            repository_id,
            [
                (
                    target_ref,
                    candidate.target_head_sha,
                    candidate.target_head_sha,
                ),
                (
                    remote_ref,
                    remote_head or _ZERO_OID,
                    candidate.head_sha,
                ),
                (staging_ref, candidate.head_sha, _ZERO_OID),
            ],
        ):
            return None

        pull_number = existing_pr
        if pull_number is None:
            pull_number = self._find_open_pull_number(
                github_repository,
                candidate.branch,
                target_branch,
            )
        return self._finish_publication(
            github_repository,
            issue_number,
            target_branch,
            candidate.branch,
            candidate.head_sha,
            pull_number,
        )

    def list_feedback(
        self,
        github_repository: str,
        pull_number: int,
    ) -> list[GitHubFeedback]:
        repository_path = f"/repos/{github_repository}"
        inline_comments = []
        page = 1
        while True:
            page_comments = self._request(
                "GET",
                f"{repository_path}/pulls/{pull_number}/comments",
                query={"per_page": 100, "page": page},
            )
            if not isinstance(page_comments, list):
                raise RuntimeError(
                    "GitHub returned an invalid review comments page"
                )
            inline_comments.extend(page_comments)
            if len(page_comments) < 100:
                break
            page += 1

        reviews = []
        page = 1
        while True:
            page_reviews = self._request(
                "GET",
                f"{repository_path}/pulls/{pull_number}/reviews",
                query={"per_page": 100, "page": page},
            )
            if not isinstance(page_reviews, list):
                raise RuntimeError("GitHub returned an invalid reviews page")
            reviews.extend(page_reviews)
            if len(page_reviews) < 100:
                break
            page += 1


        inline_items = []
        comment_ids_by_node = {}
        for comment in inline_comments:
            if not isinstance(comment, dict):
                raise RuntimeError("GitHub returned an invalid review comment")
            body = comment.get("body") or ""
            if not isinstance(body, str):
                raise RuntimeError("GitHub returned an invalid review comment body")
            if _FEEDBACK_MARKER_PREFIX in body:
                continue

            comment_id = comment.get("id")
            if type(comment_id) is not int or comment_id <= 0:
                raise RuntimeError("GitHub review comment is missing a valid id")
            node_id = comment.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                raise RuntimeError("GitHub review comment is missing its node_id")
            prior_comment_id = comment_ids_by_node.get(node_id)
            if prior_comment_id is not None and prior_comment_id != comment_id:
                raise RuntimeError(
                    "GitHub review comment node_id maps to multiple REST comments"
                )
            comment_ids_by_node[node_id] = comment_id

            in_reply_to_id = comment.get("in_reply_to_id")
            top_level_comment_id = (
                comment_id if in_reply_to_id is None else in_reply_to_id
            )
            if (
                type(top_level_comment_id) is not int
                or top_level_comment_id <= 0
            ):
                raise RuntimeError(
                    "GitHub review comment is missing a valid reply root"
                )
            inline_items.append(
                (comment, body, node_id, top_level_comment_id)
            )

        thread_ids = (
            self._review_thread_ids(
                github_repository,
                pull_number,
                set(comment_ids_by_node),
            )
            if inline_items
            else {}
        )

        feedback = [
            GitHubFeedback(
                external_id=f"inline:{comment['id']}",
                kind="inline",
                body=body,
                path=comment.get("path"),
                line=comment.get("line"),
                review_thread_id=thread_ids[node_id],
                top_level_comment_id=top_level_comment_id,
            )
            for comment, body, node_id, top_level_comment_id in inline_items
        ]
        for review in reviews:
            if not isinstance(review, dict):
                raise RuntimeError("GitHub returned an invalid pull request review")
            body = review.get("body") or ""
            if not isinstance(body, str):
                raise RuntimeError("GitHub returned an invalid review body")
            if (
                review["state"] == "CHANGES_REQUESTED"
                and _FEEDBACK_MARKER_PREFIX not in body
            ):
                feedback.append(
                    GitHubFeedback(
                        external_id=f"review:{review['id']}",
                        kind="review",
                        body=body,
                    )
                )
        return feedback

    @staticmethod
    def _graphql_data(payload, operation: str) -> dict:
        if not isinstance(payload, dict):
            raise RuntimeError(f"GitHub {operation} returned a non-object response")
        if payload.get("errors"):
            raise RuntimeError(f"GitHub {operation} returned GraphQL errors")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"GitHub {operation} returned no GraphQL data")
        return data

    @staticmethod
    def _map_review_comments_page(
        connection,
        thread_id: str,
        target_node_ids: set[str],
        thread_ids: dict[str, str],
    ) -> tuple[bool, str | None]:
        if not isinstance(connection, dict):
            raise RuntimeError("GitHub review thread has no comments connection")
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise RuntimeError("GitHub review thread comments are missing")
        for comment in nodes:
            if not isinstance(comment, dict):
                raise RuntimeError("GitHub review thread contains an invalid comment")
            comment_id = comment.get("id")
            if not isinstance(comment_id, str) or not comment_id:
                raise RuntimeError("GitHub review thread comment is missing its id")
            if comment_id not in target_node_ids:
                continue
            mapped_thread_id = thread_ids.get(comment_id)
            if (
                mapped_thread_id is not None
                and mapped_thread_id != thread_id
            ):
                raise RuntimeError(
                    "GitHub review comment maps to multiple review threads"
                )
            thread_ids[comment_id] = thread_id

        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict):
            raise RuntimeError(
                "GitHub review thread comments are missing pageInfo"
            )
        has_next_page = page_info.get("hasNextPage")
        if type(has_next_page) is not bool:
            raise RuntimeError(
                "GitHub review thread comments have invalid pagination"
            )
        end_cursor = page_info.get("endCursor")
        if has_next_page and (
            not isinstance(end_cursor, str) or not end_cursor
        ):
            raise RuntimeError(
                "GitHub review thread comments have no next-page cursor"
            )
        return has_next_page, end_cursor

    def _review_thread_ids(
        self,
        github_repository: str,
        pull_number: int,
        target_node_ids: set[str],
    ) -> dict[str, str]:
        repository_parts = github_repository.split("/")
        if (
            len(repository_parts) != 2
            or not repository_parts[0]
            or not repository_parts[1]
        ):
            raise ValueError("github_repository must be in owner/name form")
        owner, name = repository_parts

        thread_ids = {}
        seen_thread_ids = set()
        seen_thread_cursors = set()
        after = None
        while True:
            payload = self._request(
                "POST",
                "/graphql",
                json_body={
                    "query": _REVIEW_THREADS_QUERY,
                    "variables": {
                        "owner": owner,
                        "name": name,
                        "number": pull_number,
                        "after": after,
                    },
                },
            )
            data = self._graphql_data(payload, "ReviewThreads")
            repository = data.get("repository")
            if not isinstance(repository, dict):
                raise RuntimeError("GitHub repository was missing from ReviewThreads")
            pull_request = repository.get("pullRequest")
            if not isinstance(pull_request, dict):
                raise RuntimeError(
                    "GitHub pull request was missing from ReviewThreads"
                )
            connection = pull_request.get("reviewThreads")
            if not isinstance(connection, dict):
                raise RuntimeError("GitHub reviewThreads connection is missing")
            threads = connection.get("nodes")
            if not isinstance(threads, list):
                raise RuntimeError("GitHub reviewThreads nodes are missing")

            for thread in threads:
                if not isinstance(thread, dict):
                    raise RuntimeError("GitHub returned an invalid review thread")
                thread_id = thread.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise RuntimeError("GitHub review thread is missing its id")
                if thread_id in seen_thread_ids:
                    raise RuntimeError(
                        "GitHub returned the same review thread more than once"
                    )
                seen_thread_ids.add(thread_id)
                if type(thread.get("isResolved")) is not bool:
                    raise RuntimeError(
                        "GitHub review thread has invalid resolution state"
                    )
                if type(thread.get("viewerCanResolve")) is not bool:
                    raise RuntimeError(
                        "GitHub review thread has invalid resolution capability"
                    )

                has_more_comments, comments_after = (
                    self._map_review_comments_page(
                        thread.get("comments"),
                        thread_id,
                        target_node_ids,
                        thread_ids,
                    )
                )
                seen_comment_cursors = set()
                while has_more_comments:
                    if comments_after in seen_comment_cursors:
                        raise RuntimeError(
                            "GitHub repeated a review-comment page cursor"
                        )
                    seen_comment_cursors.add(comments_after)
                    comments_payload = self._request(
                        "POST",
                        "/graphql",
                        json_body={
                            "query": _REVIEW_THREAD_COMMENTS_QUERY,
                            "variables": {
                                "threadId": thread_id,
                                "after": comments_after,
                            },
                        },
                    )
                    comments_data = self._graphql_data(
                        comments_payload,
                        "ReviewThreadComments",
                    )
                    comments_thread = comments_data.get("node")
                    if not isinstance(comments_thread, dict):
                        raise RuntimeError(
                            "GitHub review thread was missing while paginating comments"
                        )
                    if comments_thread.get("id") != thread_id:
                        raise RuntimeError(
                            "GitHub returned comments for a different review thread"
                        )
                    has_more_comments, comments_after = (
                        self._map_review_comments_page(
                            comments_thread.get("comments"),
                            thread_id,
                            target_node_ids,
                            thread_ids,
                        )
                    )

            page_info = connection.get("pageInfo")
            if not isinstance(page_info, dict):
                raise RuntimeError("GitHub reviewThreads pageInfo is missing")
            has_next_page = page_info.get("hasNextPage")
            if type(has_next_page) is not bool:
                raise RuntimeError("GitHub reviewThreads pagination is invalid")
            if not has_next_page:
                break
            after = page_info.get("endCursor")
            if not isinstance(after, str) or not after:
                raise RuntimeError(
                    "GitHub reviewThreads has no next-page cursor"
                )
            if after in seen_thread_cursors:
                raise RuntimeError("GitHub repeated a review-thread page cursor")
            seen_thread_cursors.add(after)

        missing_node_ids = target_node_ids.difference(thread_ids)
        if missing_node_ids:
            raise RuntimeError(
                "GitHub review comments could not be mapped to review threads"
            )
        return thread_ids

    @staticmethod
    def _validate_github_repository(github_repository: str) -> None:
        repository_parts = (
            github_repository.split("/")
            if isinstance(github_repository, str)
            else []
        )
        if (
            len(repository_parts) != 2
            or not repository_parts[0]
            or not repository_parts[1]
            or github_repository.strip() != github_repository
        ):
            raise ValueError("github_repository must be in owner/name form")

    @staticmethod
    def _validate_target_branch(target_branch: str) -> None:
        if not isinstance(target_branch, str):
            raise TypeError("target_branch must be a string")
        forbidden_characters = {" ", "~", "^", ":", "?", "*", "[", "\\"}
        components = target_branch.split("/")
        if (
            not target_branch
            or target_branch.strip() != target_branch
            or target_branch.startswith("-")
            or target_branch.endswith(".")
            or ".." in target_branch
            or "@{" in target_branch
            or target_branch == "@"
            or any(
                not component
                or component.startswith(".")
                or component.endswith(".lock")
                for component in components
            )
            or any(
                character in forbidden_characters
                or ord(character) < 32
                or ord(character) == 127
                for character in target_branch
            )
        ):
            raise ValueError("target_branch must be a valid branch name")

    @staticmethod
    def _validate_feedback_external_id(
        external_id: str,
        expected_kind: str | None = None,
    ) -> None:
        if not isinstance(external_id, str):
            raise ValueError("feedback external_id is invalid")
        kind, separator, numeric_id = external_id.partition(":")
        if expected_kind is not None and kind != expected_kind:
            raise ValueError("feedback external_id does not match its kind")
        if (
            separator != ":"
            or kind not in {"inline", "review"}
            or not numeric_id
            or not numeric_id.isascii()
            or not numeric_id.isdigit()
            or int(numeric_id) <= 0
        ):
            raise ValueError("feedback external_id is invalid")

    @staticmethod
    def _parse_github_issue(issue) -> GitHubIssue:
        if not isinstance(issue, dict):
            raise RuntimeError("GitHub returned an invalid issue")
        number = issue.get("number")
        title = issue.get("title")
        body = issue.get("body")
        url = issue.get("html_url")
        if type(number) is not int or number <= 0:
            raise RuntimeError("GitHub issue is missing a valid number")
        if not isinstance(title, str) or not title:
            raise RuntimeError("GitHub issue is missing a valid title")
        if not isinstance(body, str):
            raise RuntimeError("GitHub issue is missing a valid body")
        if not isinstance(url, str) or not url:
            raise RuntimeError("GitHub issue is missing a valid URL")
        return GitHubIssue(number=number, title=title, body=body, url=url)

    @classmethod
    def _validate_feedback_target_inputs(
        cls,
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
    ) -> None:
        cls._validate_github_repository(github_repository)
        if type(pull_number) is not int or pull_number <= 0:
            raise ValueError("pull_number must be a positive integer")
        if not isinstance(feedback, GitHubFeedback):
            raise TypeError("feedback must be a GitHubFeedback")
        if feedback.kind not in {"inline", "review"}:
            raise ValueError("feedback kind is not addressable")
        cls._validate_feedback_external_id(
            feedback.external_id,
            expected_kind=feedback.kind,
        )

    @staticmethod
    def _validate_feedback_thread_identity(
        feedback: GitHubFeedback,
    ) -> None:
        if feedback.kind == "inline":
            if (
                not isinstance(feedback.review_thread_id, str)
                or not feedback.review_thread_id
            ):
                raise ValueError("inline feedback requires a review thread id")
            if (
                type(feedback.top_level_comment_id) is not int
                or feedback.top_level_comment_id <= 0
            ):
                raise ValueError(
                    "inline feedback requires a top-level review comment id"
                )
        elif (
            feedback.review_thread_id is not None
            or feedback.top_level_comment_id is not None
        ):
            raise ValueError("non-thread feedback cannot carry thread identity")

    @classmethod
    def _validate_address_feedback_inputs(
        cls,
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
        head_sha: str,
    ) -> None:
        cls._validate_feedback_target_inputs(
            github_repository,
            pull_number,
            feedback,
        )
        if (
            not isinstance(head_sha, str)
            or len(head_sha) != 40
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in head_sha
            )
        ):
            raise ValueError("head_sha must be a full hexadecimal commit SHA")
        cls._validate_feedback_thread_identity(feedback)

    def _ensure_feedback_response(
        self,
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
        response_body: str,
        mismatch_error: str,
    ) -> str:
        marker = f"{_FEEDBACK_MARKER_PREFIX}{feedback.external_id} -->"
        if feedback.kind == "inline":
            comments_path = (
                f"/repos/{github_repository}/pulls/{pull_number}/comments"
            )
        else:
            comments_path = (
                f"/repos/{github_repository}/issues/{pull_number}/comments"
            )

        response_url = None
        page = 1
        while True:
            page_comments = self._request(
                "GET",
                comments_path,
                query={"per_page": 100, "page": page},
            )
            if not isinstance(page_comments, list):
                raise RuntimeError("GitHub returned an invalid comment collection")
            for comment in page_comments:
                if not isinstance(comment, dict):
                    raise RuntimeError("GitHub returned an invalid comment")
                body = comment.get("body") or ""
                if not isinstance(body, str):
                    raise RuntimeError("GitHub returned an invalid comment body")
                if marker not in body:
                    continue
                if body != response_body:
                    raise RuntimeError(mismatch_error)
                if response_url is not None:
                    continue
                candidate_url = comment.get("html_url")
                if not isinstance(candidate_url, str) or not candidate_url:
                    raise RuntimeError(
                        "GitHub feedback response is missing its URL"
                    )
                response_url = candidate_url
            if len(page_comments) < 100:
                break
            page += 1

        if response_url is not None:
            return response_url

        post_path = comments_path
        if feedback.kind == "inline":
            post_path = (
                f"{comments_path}/{feedback.top_level_comment_id}/replies"
            )
        created = self._request(
            "POST",
            post_path,
            json_body={"body": response_body},
        )
        if not isinstance(created, dict):
            raise RuntimeError("GitHub returned no feedback response")
        if created.get("body") != response_body:
            raise RuntimeError("GitHub returned a mismatched feedback response")
        response_url = created.get("html_url")
        if not isinstance(response_url, str) or not response_url:
            raise RuntimeError("GitHub feedback response is missing its URL")
        return response_url

    def _finish_feedback_address(
        self,
        feedback: GitHubFeedback,
        response_url: str,
    ) -> FeedbackAddress:
        if feedback.kind != "inline":
            return FeedbackAddress(
                status="ACKNOWLEDGED",
                response_url=response_url,
            )

        thread_id = feedback.review_thread_id
        state_payload = self._request(
            "POST",
            "/graphql",
            json_body={
                "query": _REVIEW_THREAD_QUERY,
                "variables": {"threadId": thread_id},
            },
        )
        state_data = self._graphql_data(state_payload, "ReviewThread")
        thread = state_data.get("node")
        if not isinstance(thread, dict):
            raise RuntimeError("GitHub review thread state is missing")
        if thread.get("id") != thread_id:
            raise RuntimeError("GitHub returned a different review thread")
        is_resolved = thread.get("isResolved")
        viewer_can_resolve = thread.get("viewerCanResolve")
        if type(is_resolved) is not bool:
            raise RuntimeError("GitHub review thread has invalid resolution state")
        if type(viewer_can_resolve) is not bool:
            raise RuntimeError(
                "GitHub review thread has invalid resolution capability"
            )

        if not is_resolved:
            if not viewer_can_resolve:
                raise RuntimeError(
                    "GitHub viewer cannot resolve the review thread"
                )
            resolution_payload = self._request(
                "POST",
                "/graphql",
                json_body={
                    "query": _RESOLVE_THREAD_MUTATION,
                    "variables": {"input": {"threadId": thread_id}},
                },
            )
            resolution_data = self._graphql_data(
                resolution_payload,
                "ResolveThread",
            )
            result = resolution_data.get("resolveReviewThread")
            if not isinstance(result, dict):
                raise RuntimeError(
                    "GitHub returned no review-thread resolution result"
                )
            resolved_thread = result.get("thread")
            if not isinstance(resolved_thread, dict):
                raise RuntimeError(
                    "GitHub returned no resolved review thread"
                )
            if resolved_thread.get("id") != thread_id:
                raise RuntimeError(
                    "GitHub resolved a different review thread"
                )
            if resolved_thread.get("isResolved") is not True:
                raise RuntimeError(
                    "GitHub did not confirm review-thread resolution"
                )

        return FeedbackAddress(
            status="RESOLVED",
            response_url=response_url,
        )

    def address_feedback(
        self,
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
        head_sha: str,
    ) -> FeedbackAddress:
        self._validate_address_feedback_inputs(
            github_repository,
            pull_number,
            feedback,
            head_sha,
        )
        marker = f"{_FEEDBACK_MARKER_PREFIX}{feedback.external_id} -->"
        acknowledgement = (
            f"Addressed in validated commit `{head_sha}`.\n\n{marker}"
        )
        response_url = self._ensure_feedback_response(
            github_repository,
            pull_number,
            feedback,
            acknowledgement,
            "GitHub acknowledgement does not match the current commit",
        )
        return self._finish_feedback_address(feedback, response_url)

    def resolve_feedback_without_code(
        self,
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
        response: str,
    ) -> FeedbackAddress:
        self._validate_feedback_target_inputs(
            github_repository,
            pull_number,
            feedback,
        )
        self._validate_feedback_thread_identity(feedback)
        if not isinstance(response, str):
            raise TypeError("response must be a string")
        if not response.strip():
            raise ValueError("response must not be empty")
        if _FEEDBACK_MARKER_PREFIX in response:
            raise ValueError("response cannot contain a feedback marker")

        marker = f"{_FEEDBACK_MARKER_PREFIX}{feedback.external_id} -->"
        response_body = f"{response}\n\n{marker}"
        response_url = self._ensure_feedback_response(
            github_repository,
            pull_number,
            feedback,
            response_body,
            "GitHub feedback response does not match the current disposition",
        )
        return self._finish_feedback_address(feedback, response_url)
