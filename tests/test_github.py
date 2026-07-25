from __future__ import annotations

import unittest
from typing import Any

from repogents.github import FeedbackItem, GitHubClient, GitHubError


class StubGitHubClient(GitHubClient):
    def __init__(
        self,
        responses: dict[str, Any],
        *,
        application_author: str | None = "configured-user",
    ) -> None:
        super().__init__(
            token="test-token",
            api_url="https://api.invalid",
            application_author=application_author,
        )
        self.responses = responses
        self.requests: list[tuple[str, str]] = []
        self.request_bodies: list[dict[str, Any] | None] = []

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        self.requests.append((method, path))
        self.request_bodies.append(body)
        key = path.split("?", 1)[0]
        if key not in self.responses:
            raise AssertionError(f"unexpected request {method} {path}")
        response = self.responses[key]
        if callable(response):
            return response(body), {}
        return response, {}


class GitHubAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.responses = {
            "repos/owner/repo": {
                "node_id": "R1",
                "id": 101,
                "owner": {"login": "owner"},
                "name": "repo",
                "html_url": "https://github.com/owner/repo",
                "default_branch": "main",
                "private": False,
            },
            "repos/owner/repo/issues/events": [
                {
                    "id": 501,
                    "event": "labeled",
                    "created_at": "2026-01-01T00:00:00Z",
                    "label": {"name": "agent:ready"},
                    "issue": {"number": 3},
                },
                {
                    "id": 502,
                    "event": "unlabeled",
                    "created_at": "2026-01-01T00:01:00Z",
                    "label": {"name": "agent:ready"},
                    "issue": {"number": 3},
                },
                {
                    "id": 503,
                    "event": "labeled",
                    "created_at": "2026-01-01T00:02:00Z",
                    "label": {"name": "other"},
                    "issue": {"number": 3},
                },
            ],
            "repos/owner/repo/issues/3": {
                "node_id": "I3",
                "number": 3,
                "html_url": "https://github.com/owner/repo/issues/3",
                "title": "Terse issue",
                "body": "Fix it",
                "state": "open",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            "repos/owner/repo/issues/3/comments": [
                {
                    "id": 601,
                    "node_id": "IC601",
                    "user": {"login": "reviewer"},
                    "body": "More context",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "html_url": "comment-url",
                }
            ],
            "repos/owner/repo/branches/main": {"commit": {"sha": "a" * 40}},
            "user": {"login": "configured-user"},
            "graphql": {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "THREAD-703",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [{"databaseId": 703}],
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
            },
        }
        self.client = StubGitHubClient(self.responses)

    def test_repository_issue_event_discussion_and_branch_are_parsed(self) -> None:
        repository = self.client.get_repository("owner/repo")
        events = self.client.list_ready_events("owner", "repo")
        sha = self.client.get_branch_head("owner", "repo", "main")
        self.assertEqual(repository.default_branch, "main")
        self.assertFalse(repository.is_private)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "501")
        self.assertEqual(events[0].issue.node_id, "I3")
        self.assertEqual(events[0].issue.state, "open")
        self.assertEqual(events[0].issue.discussion[0]["author"], "reviewer")
        self.assertEqual(sha, "a" * 40)
        self.assertEqual(
            self.client.list_ready_events("owner", "repo")[0].event_id,
            events[0].event_id,
        )

    def test_ready_label_event_on_later_page_is_returned(self) -> None:
        class PagedClient(StubGitHubClient):
            def _request(
                self,
                method: str,
                path: str,
                *,
                body: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None,
            ) -> tuple[Any, dict[str, str]]:
                if path.startswith("repos/owner/repo/issues/events?"):
                    page = int(path.rsplit("page=", 1)[1])
                    if page == 1:
                        return (
                            [
                                {
                                    "id": index,
                                    "event": "unlabeled",
                                    "created_at": "2026-01-01T00:00:00Z",
                                    "label": {"name": "agent:ready"},
                                    "issue": {"number": 3},
                                }
                                for index in range(100)
                            ],
                            {},
                        )
                    return (
                        [
                            {
                                "id": 9001,
                                "event": "labeled",
                                "created_at": "2026-01-02T00:00:00Z",
                                "label": {"name": "agent:ready"},
                                "issue": {"number": 3},
                            }
                        ],
                        {},
                    )
                return super()._request(
                    method,
                    path,
                    body=body,
                    headers=headers,
                )

        events = PagedClient(self.responses).list_ready_events("owner", "repo")

        self.assertEqual([event.event_id for event in events], ["9001"])

    def test_empty_review_container_is_ignored_but_inline_comment_remains(self) -> None:
        self.responses.update(
            {
                "repos/owner/repo/pulls/7/reviews": [
                    {
                        "id": 701,
                        "user": {"login": "configured-user"},
                        "body": "",
                        "state": "COMMENTED",
                        "submitted_at": "2026-01-01T00:03:00Z",
                        "commit_id": "b" * 40,
                    },
                    {
                        "id": 702,
                        "user": {"login": "reviewer"},
                        "body": "Top-level review feedback",
                        "state": "COMMENTED",
                        "submitted_at": "2026-01-01T00:04:00Z",
                        "commit_id": "b" * 40,
                    },
                ],
                "repos/owner/repo/pulls/7/comments": [
                    {
                        "id": 703,
                        "user": {"login": "configured-user"},
                        "body": "Inline response body",
                        "path": "app.py",
                        "line": 10,
                        "created_at": "2026-01-01T00:03:00Z",
                        "updated_at": "2026-01-01T00:03:00Z",
                    }
                ],
                "repos/owner/repo/issues/7/comments": [],
            }
        )

        feedback = self.client.list_feedback("owner", "repo", 7)

        self.assertEqual(
            [(item.feedback_type, item.object_id) for item in feedback],
            [("inline_comment", "703"), ("review", "702")],
        )

    def test_inline_comments_include_paginated_review_thread_state(self) -> None:
        def graphql(body: dict[str, Any] | None) -> dict[str, Any]:
            self.assertIsNotNone(body)
            variables = body["variables"]
            after = variables["after"]
            if after is None:
                nodes = [
                    {
                        "id": "THREAD-1",
                        "isResolved": False,
                        "comments": {
                            "nodes": [
                                {"databaseId": 703},
                                {"databaseId": 705},
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        },
                    }
                ]
                page_info = {"hasNextPage": True, "endCursor": "cursor-1"}
            else:
                self.assertEqual(after, "cursor-1")
                nodes = [
                    {
                        "id": "THREAD-2",
                        "isResolved": True,
                        "comments": {
                            "nodes": [{"databaseId": 704}],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        },
                    }
                ]
                page_info = {"hasNextPage": False, "endCursor": "cursor-2"}
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": nodes,
                                "pageInfo": page_info,
                            }
                        }
                    }
                }
            }

        self.responses.update(
            {
                "graphql": graphql,
                "repos/owner/repo/pulls/7/reviews": [],
                "repos/owner/repo/pulls/7/comments": [
                    {
                        "id": 703,
                        "user": {"login": "reviewer"},
                        "body": "First",
                        "path": "app.py",
                        "line": 10,
                        "created_at": "2026-01-01T00:03:00Z",
                        "updated_at": "2026-01-01T00:03:00Z",
                    },
                    {
                        "id": 704,
                        "user": {"login": "reviewer"},
                        "body": "Second",
                        "path": "app.py",
                        "line": 20,
                        "created_at": "2026-01-01T00:04:00Z",
                        "updated_at": "2026-01-01T00:04:00Z",
                    },
                    {
                        "id": 705,
                        "user": {"login": "reviewer"},
                        "body": "Third",
                        "path": "app.py",
                        "line": 30,
                        "created_at": "2026-01-01T00:05:00Z",
                        "updated_at": "2026-01-01T00:05:00Z",
                    },
                ],
                "repos/owner/repo/issues/7/comments": [],
            }
        )

        feedback = self.client.list_feedback("owner", "repo", 7)

        self.assertEqual(
            [
                (
                    item.object_id,
                    item.review_thread_id,
                    item.review_thread_resolved,
                )
                for item in feedback
            ],
            [
                ("703", "THREAD-1", False),
                ("704", "THREAD-2", True),
                ("705", "THREAD-1", False),
            ],
        )

    def test_resolve_review_thread_requires_resolved_mutation_result(self) -> None:
        self.responses["graphql"] = {
            "data": {
                "resolveReviewThread": {
                    "thread": {"id": "THREAD-1", "isResolved": True}
                }
            }
        }

        self.client.resolve_review_thread("THREAD-1")

        self.assertEqual(self.client.requests[-1], ("POST", "graphql"))
        self.assertEqual(
            self.client.request_bodies[-1]["variables"],
            {"threadId": "THREAD-1"},
        )

    def test_closed_pull_request_status_is_parsed(self) -> None:
        self.responses["repos/owner/repo/pulls/7"] = {
            "node_id": "PR7",
            "number": 7,
            "html_url": "pull-url",
            "state": "closed",
            "merged": False,
            "head": {"ref": "agent/run-7", "sha": "c" * 40},
            "base": {"ref": "main", "sha": "d" * 40},
            "mergeable": False,
            "updated_at": "2026-01-01T00:07:00Z",
        }

        pull = self.client.get_pull_request("owner", "repo", 7)

        self.assertEqual(pull.state, "closed")
        self.assertFalse(pull.merged)
        self.assertEqual(pull.head_sha, "c" * 40)
        self.assertEqual(pull.base_sha, "d" * 40)
        self.assertIs(pull.mergeable, False)

    def test_find_response_requires_application_author_and_exact_inline_thread(
        self,
    ) -> None:
        self.responses["repos/owner/repo/pulls/7/comments"] = [
            {
                "id": 801,
                "user": {"login": "third-party"},
                "body": "Implemented and validated.",
                "in_reply_to_id": 703,
                "created_at": "2026-01-01T00:05:00Z",
                "html_url": "third-party-url",
            },
            {
                "id": 802,
                "user": {"login": "configured-user"},
                "body": "Implemented and validated.",
                "in_reply_to_id": 999,
                "created_at": "2026-01-01T00:05:00Z",
                "html_url": "wrong-thread-url",
            },
            {
                "id": 803,
                "user": {"login": "configured-user"},
                "body": "Implemented and validated.",
                "in_reply_to_id": 703,
                "created_at": "2026-01-01T00:05:00Z",
                "html_url": "application-url",
            },
        ]
        target = FeedbackItem(
            feedback_type="inline_comment",
            object_id="703",
            version="v1",
            author="reviewer",
            body="Please change this.",
            path="app.py",
            line=10,
            url="feedback-url",
            created_at="2026-01-01T00:03:00Z",
            updated_at="2026-01-01T00:03:00Z",
        )

        output = self.client.find_response(
            "owner",
            "repo",
            7,
            target,
            "Implemented and validated.",
            "2026-01-01T00:04:00Z",
        )

        self.assertIsNotNone(output)
        self.assertEqual(output.object_id, "803")
        self.assertEqual(output.target_object_id, "703")

    def test_find_response_resolves_and_caches_authenticated_application_author(
        self,
    ) -> None:
        self.responses["repos/owner/repo/issues/7/comments"] = [
            {
                "id": 804,
                "user": {"login": "configured-user"},
                "body": "Application response",
                "created_at": "2026-01-01T00:05:00Z",
                "html_url": "application-url",
            }
        ]
        client = StubGitHubClient(self.responses, application_author=None)
        target = FeedbackItem(
            feedback_type="comment",
            object_id="704",
            version="v1",
            author="reviewer",
            body="Question?",
            path=None,
            line=None,
            url="feedback-url",
            created_at="2026-01-01T00:03:00Z",
            updated_at="2026-01-01T00:03:00Z",
        )

        first = client.find_response(
            "owner", "repo", 7, target, "Application response", "2026-01-01T00:04:00Z"
        )
        second = client.find_response(
            "owner", "repo", 7, target, "Application response", "2026-01-01T00:04:00Z"
        )

        self.assertEqual(first, second)
        self.assertEqual(client.requests.count(("GET", "user")), 1)

    def test_update_pull_request_body_uses_controller_owned_patch(self) -> None:
        self.responses["repos/owner/repo/pulls/7"] = {}

        self.client.update_pull_request_body(
            "owner",
            "repo",
            7,
            "current SHA-bound proof",
        )

        self.assertEqual(
            self.client.requests[-1],
            ("PATCH", "repos/owner/repo/pulls/7"),
        )
        self.assertEqual(
            self.client.request_bodies[-1],
            {"body": "current SHA-bound proof"},
        )

    def test_invalid_branch_sha_is_rejected(self) -> None:
        self.responses["repos/owner/repo/branches/main"] = {
            "commit": {"sha": "not-a-sha"}
        }
        with self.assertRaises(GitHubError):
            self.client.get_branch_head("owner", "repo", "main")


if __name__ == "__main__":
    unittest.main()
