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
        raise ValueError("repository identity must be owner/name or a github.com repository URL")
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
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
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
                payload = json.loads(raw) if raw else None
                return payload, {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise GitHubNotFound(f"GitHub {method} {path} returned HTTP 404") from error
            detail = error.read().decode("utf-8", "replace")
            raise GitHubError(f"GitHub {method} {path} failed with HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise GitHubError(f"GitHub {method} {path} failed: {error.reason}") from error

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
            raise GitHubError("GitHub repository response omitted required identity fields") from error

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
            raise GitHubError("GitHub issue response was missing or represented a pull request")
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
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubError("GitHub issue response omitted required fields") from error

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

    def get_remote_branch_head(
        self, owner: str, name: str, branch: str
    ) -> str | None:
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
        pulls = self._paginate(
            f"repos/{owner}/{name}/pulls?state=all&head={head}"
        )
        if not pulls:
            return None
        matching = [
            self._pull_request(payload)
            for payload in pulls
            if payload.get("head", {}).get("ref") == branch
        ]
        if len(matching) > 1:
            raise GitHubError("multiple pull requests exist for the deterministic run branch")
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

    def get_pull_request(
        self, owner: str, name: str, number: int
    ) -> PullRequestInfo:
        payload, _ = self._request(
            "GET", f"repos/{owner}/{name}/pulls/{number}"
        )
        return self._pull_request(payload)

    @staticmethod
    def _pull_request(payload: object) -> PullRequestInfo:
        if not isinstance(payload, dict):
            raise GitHubError("GitHub pull-request response was not an object")
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
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubError(
                "GitHub pull-request response omitted required fields"
            ) from error

    def list_feedback(
        self, owner: str, name: str, pull_number: int
    ) -> list[FeedbackItem]:
        reviews = self._paginate(
            f"repos/{owner}/{name}/pulls/{pull_number}/reviews"
        )
        inline_comments = self._paginate(
            f"repos/{owner}/{name}/pulls/{pull_number}/comments"
        )
        comments = self._paginate(
            f"repos/{owner}/{name}/issues/{pull_number}/comments"
        )
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
        for comment in inline_comments:
            values.append(
                FeedbackItem(
                    feedback_type="inline_comment",
                    object_id=str(comment["id"]),
                    version=str(comment.get("updated_at") or comment.get("created_at") or ""),
                    author=str(comment.get("user", {}).get("login", "")),
                    body=str(comment.get("body") or ""),
                    path=str(comment.get("path") or "") or None,
                    line=_optional_int(comment.get("line") or comment.get("original_line")),
                    url=str(comment.get("html_url") or ""),
                    created_at=str(comment.get("created_at") or ""),
                    updated_at=str(comment.get("updated_at") or comment.get("created_at") or ""),
                )
            )
        for comment in comments:
            values.append(
                FeedbackItem(
                    feedback_type="comment",
                    object_id=str(comment["id"]),
                    version=str(comment.get("updated_at") or comment.get("created_at") or ""),
                    author=str(comment.get("user", {}).get("login", "")),
                    body=str(comment.get("body") or ""),
                    path=None,
                    line=None,
                    url=str(comment.get("html_url") or ""),
                    created_at=str(comment.get("created_at") or ""),
                    updated_at=str(comment.get("updated_at") or comment.get("created_at") or ""),
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
