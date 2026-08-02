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

_FEEDBACK_MARKER_PREFIX = "<!-- repogents-feedback:"


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
    ):
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._request = request or self._default_request
        self._command_runner = command_runner or self._default_command_runner
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
                    pull_number = pull["number"]
                    break
        workspace_path = Path(workspace)
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
        remote_ref = f"refs/heads/{branch}"
        remote_branch = self._command_runner(
            ["git", "ls-remote", "--heads", "origin", remote_ref],
            cwd=workspace_path,
            env=self._git_auth_env,
        ).stdout.strip()
        expected_head = ""
        if remote_branch:
            fields = remote_branch.split()
            if (
                len(fields) != 2
                or fields[1] != remote_ref
                or len(fields[0]) != 40
                or any(
                    character not in "0123456789abcdefABCDEF"
                    for character in fields[0]
                )
            ):
                raise RuntimeError("git ls-remote returned an invalid branch ref")
            expected_head = fields[0]
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
        local_head = self._command_runner(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_path,
            env=self._git_command_env,
        ).stdout.strip()
        if (
            len(local_head) != 40
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in local_head
            )
        ):
            raise RuntimeError(
                "git rev-parse HEAD returned an invalid commit SHA"
            )
        if not expected_head or local_head != expected_head:
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
        self._command_runner(
            [
                "git",
                "push",
                f"--force-with-lease={remote_ref}:{expected_head}",
                "--set-upstream",
                "origin",
                branch,
            ],
            cwd=workspace_path,
            env=self._git_auth_env,
        )
        pushed_head = self._command_runner(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_path,
            env=self._git_command_env,
        ).stdout.strip()
        if (
            len(pushed_head) != 40
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in pushed_head
            )
        ):
            raise RuntimeError("git rev-parse HEAD returned an invalid commit SHA")

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
                raise RuntimeError("GitHub returned an invalid created pull request")
            pull_number = created["number"]
        pull = self.pull_request(github_repository, pull_number)
        return PullRequest(
            number=pull.number,
            url=pull.url,
            branch=pull.branch,
            state=pull.state,
            merged=pull.merged,
            diff=pull.diff,
            head_sha=pushed_head,
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
    def _validate_address_feedback_inputs(
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
        head_sha: str,
    ) -> None:
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
        if type(pull_number) is not int or pull_number <= 0:
            raise ValueError("pull_number must be a positive integer")
        if not isinstance(feedback, GitHubFeedback):
            raise TypeError("feedback must be a GitHubFeedback")
        if feedback.kind not in {"inline", "review"}:
            raise ValueError("feedback kind is not addressable")
        external_id_prefix = f"{feedback.kind}:"
        if (
            not isinstance(feedback.external_id, str)
            or not feedback.external_id.startswith(external_id_prefix)
        ):
            raise ValueError("feedback external_id does not match its kind")
        numeric_id = feedback.external_id.removeprefix(external_id_prefix)
        if (
            not numeric_id
            or not numeric_id.isascii()
            or not numeric_id.isdigit()
            or int(numeric_id) <= 0
        ):
            raise ValueError("feedback external_id is invalid")
        if (
            not isinstance(head_sha, str)
            or len(head_sha) != 40
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in head_sha
            )
        ):
            raise ValueError("head_sha must be a full hexadecimal commit SHA")

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
                if body != acknowledgement:
                    raise RuntimeError(
                        "GitHub acknowledgement does not match "
                        "the current commit"
                    )
                if response_url is not None:
                    continue
                candidate_url = comment.get("html_url")
                if not isinstance(candidate_url, str) or not candidate_url:
                    raise RuntimeError(
                        "GitHub acknowledgement is missing its response URL"
                    )
                response_url = candidate_url
            if len(page_comments) < 100:
                break
            page += 1

        if response_url is None:
            post_path = comments_path
            if feedback.kind == "inline":
                post_path = (
                    f"{comments_path}/{feedback.top_level_comment_id}/replies"
                )
            created = self._request(
                "POST",
                post_path,
                json_body={"body": acknowledgement},
            )
            if not isinstance(created, dict):
                raise RuntimeError("GitHub returned no acknowledgement")
            if created.get("body") != acknowledgement:
                raise RuntimeError(
                    "GitHub returned a mismatched acknowledgement"
                )
            candidate_url = created.get("html_url")
            if not isinstance(candidate_url, str) or not candidate_url:
                raise RuntimeError(
                    "GitHub acknowledgement is missing its response URL"
                )
            response_url = candidate_url

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
