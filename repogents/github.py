from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

_GITHUB_HOST = "github.com"
_IDENTITY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

_REVIEW_THREADS_QUERY = """
query RepogentsReviewThreads(
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
          comments(first: 100) {
            nodes { databaseId }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
_REVIEW_THREAD_COMMENTS_QUERY = """
query RepogentsReviewThreadComments($threadId: ID!, $after: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $after) {
        nodes { databaseId }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
_RESOLVE_REVIEW_THREAD_MUTATION = """
mutation RepogentsResolveReviewThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


@dataclass(frozen=True)
class RepositoryInfo:
    node_id: str
    database_id: int
    owner: str
    name: str
    url: str
    default_branch: str
    is_private: bool

    @property
    def identity(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class IssueInfo:
    node_id: str
    number: int
    url: str
    title: str
    body: str
    discussion: tuple[dict[str, object], ...]
    updated_at: str
    state: str = "open"


@dataclass(frozen=True)
class ActivationEvent:
    event_id: str
    applied_at: str
    issue: IssueInfo


@dataclass(frozen=True)
class PullRequestInfo:
    node_id: str
    number: int
    url: str
    state: str
    merged: bool
    head_branch: str
    head_sha: str
    base_branch: str
    updated_at: str
    base_sha: str = ""
    mergeable: bool | None = None


@dataclass(frozen=True)
class FeedbackItem:
    feedback_type: str
    object_id: str
    version: str
    author: str
    body: str
    path: str | None
    line: int | None
    url: str
    created_at: str
    updated_at: str
    review_thread_id: str | None = None
    review_thread_resolved: bool | None = None


@dataclass(frozen=True)
class FeedbackOutput:
    feedback_type: str
    object_id: str
    target_object_id: str
    body: str
    url: str
    created_at: str


class GitHubError(RuntimeError):
    pass


class GitHubNotFound(GitHubError):
    pass


def parse_repository_identity(value: str) -> tuple[str, str]:
    candidate = value.strip().rstrip("/")
    if candidate.startswith("git@github.com:"):
        candidate = candidate.removeprefix("git@github.com:")
    elif "://" in candidate:
        parsed = urllib.parse.urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != _GITHUB_HOST:
            raise ValueError("repository URL must use https://github.com")
        candidate = parsed.path.strip("/")
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    if not _IDENTITY.fullmatch(candidate):
        raise ValueError(
            "repository identity must be owner/name or a github.com repository URL"
        )
    owner, name = candidate.split("/", 1)
    return owner, name


class GitHubClient:
    """Controller-owned GitHub REST client.

    Credentials remain in this process and are never returned in responses or
    passed into repository command environments.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        api_url: str = "https://api.github.com",
        application_author: str | None = None,
    ) -> None:
        self._token = (
            token
            if token is not None
            else os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        )
        self._api_url = api_url.rstrip("/")
        self._application_author = (
            application_author.strip() if application_author is not None else None
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        request_headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "repogents/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            request_headers["Authorization"] = f"Bearer {self._token}"
        if headers:
            request_headers.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._api_url}/{path.lstrip('/')}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                try:
                    payload = json.loads(raw) if raw else None
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise GitHubError(
                        f"GitHub {method} {path} returned an invalid JSON response"
                    ) from error
                return payload, {
                    key.lower(): value for key, value in response.headers.items()
                }
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise GitHubNotFound(
                    f"GitHub {method} {path} returned HTTP 404"
                ) from error
            detail = error.read().decode("utf-8", "replace")
            raise GitHubError(
                f"GitHub {method} {path} failed with HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise GitHubError(
                f"GitHub {method} {path} failed: {error.reason}"
            ) from error

    def _graphql(
        self,
        query: str,
        variables: dict[str, object],
    ) -> dict[str, Any]:
        payload, _ = self._request(
            "POST",
            "graphql",
            body={"query": query, "variables": variables},
        )
        if not isinstance(payload, dict):
            raise GitHubError("GitHub GraphQL response was not an object")
        errors = payload.get("errors")
        if errors:
            raise GitHubError(f"GitHub GraphQL request failed: {errors}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubError("GitHub GraphQL response omitted data")
        return data

    def _review_thread_states(
        self,
        owner: str,
        name: str,
        pull_number: int,
    ) -> dict[str, tuple[str, bool]]:
        states: dict[str, tuple[str, bool]] = {}
        after: str | None = None
        while True:
            data = self._graphql(
                _REVIEW_THREADS_QUERY,
                {
                    "owner": owner,
                    "name": name,
                    "number": pull_number,
                    "after": after,
                },
            )
            try:
                threads = data["repository"]["pullRequest"]["reviewThreads"]
            except (KeyError, TypeError) as error:
                raise GitHubError(
                    "GitHub review-thread response omitted the pull request"
                ) from error
            if not isinstance(threads, dict):
                raise GitHubError(
                    "GitHub review-thread response was not a connection"
                )
            nodes = threads.get("nodes")
            page_info = threads.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise GitHubError(
                    "GitHub review-thread connection omitted pagination fields"
                )
            for node in nodes:
                if not isinstance(node, dict):
                    raise GitHubError("GitHub review-thread node was invalid")
                thread_id = str(node.get("id") or "")
                resolved = node.get("isResolved")
                comments = node.get("comments")
                if (
                    not thread_id
                    or not isinstance(resolved, bool)
                    or not isinstance(comments, dict)
                ):
                    raise GitHubError(
                        "GitHub review-thread node omitted required fields"
                    )
                self._record_thread_comments(
                    states,
                    thread_id,
                    resolved,
                    comments,
                )
                comment_page = comments.get("pageInfo")
                while isinstance(comment_page, dict) and bool(
                    comment_page.get("hasNextPage")
                ):
                    comment_after = comment_page.get("endCursor")
                    if not isinstance(comment_after, str) or not comment_after:
                        raise GitHubError(
                            "GitHub review-thread comment cursor was missing"
                        )
                    comment_data = self._graphql(
                        _REVIEW_THREAD_COMMENTS_QUERY,
                        {"threadId": thread_id, "after": comment_after},
                    )
                    comment_node = comment_data.get("node")
                    if not isinstance(comment_node, dict):
                        raise GitHubError(
                            "GitHub review-thread comment page omitted its thread"
                        )
                    comments = comment_node.get("comments")
                    if not isinstance(comments, dict):
                        raise GitHubError(
                            "GitHub review-thread comment page was invalid"
                        )
                    self._record_thread_comments(
                        states,
                        thread_id,
                        resolved,
                        comments,
                    )
                    comment_page = comments.get("pageInfo")
            if not bool(page_info.get("hasNextPage")):
                return states
            next_after = page_info.get("endCursor")
            if not isinstance(next_after, str) or not next_after:
                raise GitHubError("GitHub review-thread cursor was missing")
            after = next_after

    @staticmethod
    def _record_thread_comments(
        states: dict[str, tuple[str, bool]],
        thread_id: str,
        resolved: bool,
        comments: dict[str, Any],
    ) -> None:
        nodes = comments.get("nodes")
        page_info = comments.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise GitHubError(
                "GitHub review-thread comments omitted pagination fields"
            )
        for node in nodes:
            if not isinstance(node, dict):
                raise GitHubError("GitHub review-thread comment was invalid")
            database_id = node.get("databaseId")
            if not isinstance(database_id, int) or isinstance(database_id, bool):
                raise GitHubError(
                    "GitHub review-thread comment omitted its database ID"
                )
            object_id = str(database_id)
            prior = states.get(object_id)
            current = (thread_id, resolved)
            if prior is not None and prior != current:
                raise GitHubError(
                    "GitHub inline comment appeared in multiple review threads"
                )
            states[object_id] = current

    def get_repository(self, identity: str) -> RepositoryInfo:
        owner, name = parse_repository_identity(identity)
        payload, _ = self._request("GET", f"repos/{owner}/{name}")
        if not isinstance(payload, dict):
            raise GitHubError("GitHub repository response was not an object")
        try:
            resolved_owner = str(payload["owner"]["login"])
            return RepositoryInfo(
                node_id=str(payload["node_id"]),
                database_id=int(payload["id"]),
                owner=resolved_owner,
                name=str(payload["name"]),
                url=str(payload["html_url"]),
                default_branch=str(payload["default_branch"]),
                is_private=bool(payload["private"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubError(
                "GitHub repository response omitted required identity fields"
            ) from error

    def _paginate(self, path: str) -> list[dict[str, Any]]:
        separator = "&" if "?" in path else "?"
        page = 1
        values: list[dict[str, Any]] = []
        while True:
            payload, _ = self._request(
                "GET", f"{path}{separator}per_page=100&page={page}"
            )
            if not isinstance(payload, list):
                raise GitHubError("GitHub paginated response was not an array")
            page_values = [value for value in payload if isinstance(value, dict)]
            values.extend(page_values)
            if len(payload) < 100:
                return values
            page += 1

    def get_issue(self, owner: str, name: str, number: int) -> IssueInfo:
        payload, _ = self._request("GET", f"repos/{owner}/{name}/issues/{number}")
        if not isinstance(payload, dict) or "pull_request" in payload:
            raise GitHubError(
                "GitHub issue response was missing or represented a pull request"
            )
        comments = self._paginate(f"repos/{owner}/{name}/issues/{number}/comments")
        discussion = tuple(
            {
                "id": int(comment["id"]),
                "node_id": str(comment.get("node_id", "")),
                "author": str(comment.get("user", {}).get("login", "")),
                "body": str(comment.get("body") or ""),
                "created_at": str(comment.get("created_at") or ""),
                "updated_at": str(comment.get("updated_at") or ""),
                "url": str(comment.get("html_url") or ""),
            }
            for comment in comments
        )
        try:
            return IssueInfo(
                node_id=str(payload["node_id"]),
                number=int(payload["number"]),
                url=str(payload["html_url"]),
                title=str(payload["title"]),
                body=str(payload.get("body") or ""),
                discussion=discussion,
                updated_at=str(payload["updated_at"]),
                state=str(payload["state"]).lower(),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubError(
                "GitHub issue response omitted required fields"
            ) from error

    def list_ready_events(self, owner: str, name: str) -> list[ActivationEvent]:
        events = self._paginate(f"repos/{owner}/{name}/issues/events")
        issue_cache: dict[int, IssueInfo] = {}
        activations: list[ActivationEvent] = []
        for event in events:
            label = event.get("label")
            if event.get("event") != "labeled" or not isinstance(label, dict):
                continue
            if str(label.get("name")) != "agent:ready":
                continue
            issue_payload = event.get("issue")
            if not isinstance(issue_payload, dict):
                continue
            try:
                number = int(issue_payload["number"])
                event_id = str(event["id"])
                applied_at = str(event["created_at"])
            except (KeyError, TypeError, ValueError):
                continue
            issue = issue_cache.get(number)
            if issue is None:
                issue = self.get_issue(owner, name, number)
                issue_cache[number] = issue
            activations.append(
                ActivationEvent(event_id=event_id, applied_at=applied_at, issue=issue)
            )
        activations.sort(key=lambda item: (item.applied_at, item.event_id))
        return activations

    def list_ready_issues(self, owner: str, name: str) -> list[IssueInfo]:
        payloads = self._paginate(
            f"repos/{owner}/{name}/issues?state=open&labels=agent%3Aready"
        )
        issues: list[IssueInfo] = []
        for payload in payloads:
            if "pull_request" in payload:
                continue
            try:
                number = int(payload["number"])
                title = payload["title"]
                url = payload["html_url"]
                updated_at = payload["updated_at"]
            except (KeyError, TypeError, ValueError):
                continue
            if not all(isinstance(value, str) and value for value in (title, url, updated_at)):
                continue
            node_id = payload.get("node_id", "")
            if not isinstance(node_id, str):
                node_id = ""
            issues.append(
                IssueInfo(
                    node_id=node_id,
                    number=number,
                    url=url,
                    title=title,
                    body="",
                    discussion=(),
                    updated_at=updated_at,
                )
            )
        issues.sort(key=lambda issue: issue.number)
        return issues

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        encoded_branch = urllib.parse.quote(branch, safe="")
        payload, _ = self._request(
            "GET", f"repos/{owner}/{name}/branches/{encoded_branch}"
        )
        try:
            sha = str(payload["commit"]["sha"])
        except (KeyError, TypeError) as error:
            raise GitHubError("GitHub branch response omitted the head SHA") from error
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubError("GitHub branch response returned an invalid head SHA")
        return sha

    def get_remote_branch_head(self, owner: str, name: str, branch: str) -> str | None:
        encoded = urllib.parse.quote(branch, safe="")
        try:
            payload, _ = self._request(
                "GET", f"repos/{owner}/{name}/git/ref/heads/{encoded}"
            )
        except GitHubNotFound:
            return None
        try:
            sha = str(payload["object"]["sha"])
        except (KeyError, TypeError) as error:
            raise GitHubError("GitHub ref response omitted the head SHA") from error
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubError("GitHub ref response returned an invalid head SHA")
        return sha

    def find_pull_request(
        self, owner: str, name: str, branch: str
    ) -> PullRequestInfo | None:
        head = urllib.parse.quote(f"{owner}:{branch}", safe="")
        pulls = self._paginate(f"repos/{owner}/{name}/pulls?state=all&head={head}")
        if not pulls:
            return None
        matching = [
            self._pull_request(payload)
            for payload in pulls
            if payload.get("head", {}).get("ref") == branch
        ]
        if len(matching) > 1:
            raise GitHubError(
                "multiple pull requests exist for the deterministic run branch"
            )
        return matching[0] if matching else None

    def create_pull_request(
        self,
        owner: str,
        name: str,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestInfo:
        payload, _ = self._request(
            "POST",
            f"repos/{owner}/{name}/pulls",
            body={
                "title": title,
                "head": branch,
                "base": base,
                "body": body,
                "draft": False,
            },
        )
        return self._pull_request(payload)

    def update_pull_request_body(
        self,
        owner: str,
        name: str,
        number: int,
        body: str,
    ) -> None:
        self._request(
            "PATCH",
            f"repos/{owner}/{name}/pulls/{number}",
            body={"body": body},
        )

    def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestInfo:
        payload, _ = self._request("GET", f"repos/{owner}/{name}/pulls/{number}")
        return self._pull_request(payload)

    @staticmethod
    def _pull_request(payload: object) -> PullRequestInfo:
        if not isinstance(payload, dict):
            raise GitHubError("GitHub pull-request response was not an object")
        mergeable = payload.get("mergeable")
        if mergeable is not None and not isinstance(mergeable, bool):
            raise GitHubError(
                "GitHub pull-request mergeable result was not boolean or null"
            )
        try:
            return PullRequestInfo(
                node_id=str(payload["node_id"]),
                number=int(payload["number"]),
                url=str(payload["html_url"]),
                state=str(payload["state"]),
                merged=bool(payload.get("merged", False)),
                head_branch=str(payload["head"]["ref"]),
                head_sha=str(payload["head"]["sha"]),
                base_branch=str(payload["base"]["ref"]),
                updated_at=str(payload["updated_at"]),
                base_sha=str(payload["base"].get("sha", "")),
                mergeable=mergeable,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubError(
                "GitHub pull-request response omitted required fields"
            ) from error

    def list_feedback(
        self, owner: str, name: str, pull_number: int
    ) -> list[FeedbackItem]:
        thread_states = self._review_thread_states(owner, name, pull_number)
        reviews = self._paginate(f"repos/{owner}/{name}/pulls/{pull_number}/reviews")
        inline_comments = self._paginate(
            f"repos/{owner}/{name}/pulls/{pull_number}/comments"
        )
        comments = self._paginate(f"repos/{owner}/{name}/issues/{pull_number}/comments")
        values: list[FeedbackItem] = []
        for review in reviews:
            body = str(review.get("body") or "")
            if not body.strip():
                continue
            submitted = str(review.get("submitted_at") or "")
            version = str(
                review.get("updated_at")
                or f"{submitted}:{review.get('commit_id', '')}:{review.get('state', '')}"
            )
            values.append(
                FeedbackItem(
                    feedback_type="review",
                    object_id=str(review["id"]),
                    version=version,
                    author=str(review.get("user", {}).get("login", "")),
                    body=body,
                    path=None,
                    line=None,
                    url=str(review.get("html_url") or ""),
                    created_at=submitted,
                    updated_at=version,
                )
            )
        missing_thread_ids = [
            str(comment.get("id") or "")
            for comment in inline_comments
            if str(comment.get("id") or "") not in thread_states
        ]
        if missing_thread_ids:
            raise GitHubError(
                "GitHub review-thread response omitted inline comments: "
                + ", ".join(missing_thread_ids)
            )
        for comment in inline_comments:
            values.append(
                FeedbackItem(
                    feedback_type="inline_comment",
                    object_id=str(comment["id"]),
                    version=str(
                        comment.get("updated_at") or comment.get("created_at") or ""
                    ),
                    author=str(comment.get("user", {}).get("login", "")),
                    body=str(comment.get("body") or ""),
                    path=str(comment.get("path") or "") or None,
                    line=_optional_int(
                        comment.get("line") or comment.get("original_line")
                    ),
                    url=str(comment.get("html_url") or ""),
                    created_at=str(comment.get("created_at") or ""),
                    updated_at=str(
                        comment.get("updated_at") or comment.get("created_at") or ""
                    ),
                    review_thread_id=thread_states[str(comment["id"])][0],
                    review_thread_resolved=thread_states[str(comment["id"])][1],
                )
            )
        for comment in comments:
            values.append(
                FeedbackItem(
                    feedback_type="comment",
                    object_id=str(comment["id"]),
                    version=str(
                        comment.get("updated_at") or comment.get("created_at") or ""
                    ),
                    author=str(comment.get("user", {}).get("login", "")),
                    body=str(comment.get("body") or ""),
                    path=None,
                    line=None,
                    url=str(comment.get("html_url") or ""),
                    created_at=str(comment.get("created_at") or ""),
                    updated_at=str(
                        comment.get("updated_at") or comment.get("created_at") or ""
                    ),
                )
            )
        values.sort(
            key=lambda item: (
                item.created_at,
                item.feedback_type,
                item.object_id,
                item.version,
            )
        )
        return values

    def resolve_review_thread(self, thread_id: str) -> None:
        if not thread_id.strip():
            raise ValueError("review thread ID must be nonempty")
        data = self._graphql(
            _RESOLVE_REVIEW_THREAD_MUTATION,
            {"threadId": thread_id},
        )
        mutation = data.get("resolveReviewThread")
        thread = (
            mutation.get("thread")
            if isinstance(mutation, dict)
            else None
        )
        if (
            not isinstance(thread, dict)
            or thread.get("id") != thread_id
            or thread.get("isResolved") is not True
        ):
            raise GitHubError(
                "GitHub did not confirm review-thread resolution"
            )

    def post_response(
        self,
        owner: str,
        name: str,
        pull_number: int,
        feedback: FeedbackItem,
        body: str,
    ) -> FeedbackOutput:
        if feedback.feedback_type == "inline_comment":
            payload, _ = self._request(
                "POST",
                f"repos/{owner}/{name}/pulls/{pull_number}/comments/{feedback.object_id}/replies",
                body={"body": body},
            )
            output_type = "inline_comment"
        else:
            payload, _ = self._request(
                "POST",
                f"repos/{owner}/{name}/issues/{pull_number}/comments",
                body={"body": body},
            )
            output_type = "comment"
        return FeedbackOutput(
            feedback_type=output_type,
            object_id=str(payload["id"]),
            target_object_id=feedback.object_id,
            body=str(payload.get("body") or body),
            url=str(payload.get("html_url") or ""),
            created_at=str(payload.get("created_at") or ""),
        )

    def _application_login(self) -> str:
        if self._application_author:
            return self._application_author
        payload, _ = self._request("GET", "user")
        login = payload.get("login") if isinstance(payload, dict) else None
        if not isinstance(login, str) or not login.strip():
            raise GitHubError(
                "GitHub authenticated-user response omitted the application login"
            )
        self._application_author = login.strip()
        return self._application_author

    def application_login(self) -> str:
        return self._application_login()

    def find_response(
        self,
        owner: str,
        name: str,
        pull_number: int,
        feedback: FeedbackItem,
        body: str,
        attempted_at: str,
    ) -> FeedbackOutput | None:
        application_author = self._application_login()

        if feedback.feedback_type == "inline_comment":
            candidates = self._paginate(
                f"repos/{owner}/{name}/pulls/{pull_number}/comments"
            )
            output_type = "inline_comment"
            candidates = [
                item
                for item in candidates
                if str(item.get("in_reply_to_id") or "") == feedback.object_id
            ]
        else:
            candidates = self._paginate(
                f"repos/{owner}/{name}/issues/{pull_number}/comments"
            )
            output_type = "comment"
        matching = [
            item
            for item in candidates
            if str(item.get("user", {}).get("login", "")).casefold()
            == application_author.casefold()
            and str(item.get("body") or "") == body
            and str(item.get("created_at") or "") >= attempted_at
        ]
        if len(matching) > 1:
            raise GitHubError(
                "multiple candidate feedback responses match a pending operation"
            )
        if not matching:
            return None
        item = matching[0]
        return FeedbackOutput(
            feedback_type=output_type,
            object_id=str(item["id"]),
            target_object_id=feedback.object_id,
            body=str(item.get("body") or ""),
            url=str(item.get("html_url") or ""),
            created_at=str(item.get("created_at") or ""),
        )


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise GitHubError("expected an integer GitHub field")
    return int(value)
