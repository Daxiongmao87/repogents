from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from repogents.interface import LocalInterfaceServer


class FakeActions:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def state(self) -> dict[str, object]:
        return {
            "repositories": [
                {
                    "id": "repo-1",
                    "identity": "owner/repo",
                    "url": "https://github.com/owner/repo",
                    "default_branch": "main",
                    "onboarding_state": "ready",
                    "blocking_reason": None,
                    "sandbox_version": 1,
                    "team_version": 1,
                    "display_inputs": {
                        "host_paths": ["/srv/fixtures"],
                        "secret_references": {"API_TOKEN": "configured"},
                    },
                }
            ],
            "runs": [
                {
                    "id": "run-1",
                    "repository": "owner/repo",
                    "issue_number": 3,
                    "issue_title": "Fix scrolling",
                    "issue_url": "https://github.com/owner/repo/issues/3",
                    "state": "waiting_for_feedback",
                    "reason": None,
                    "pull_number": 7,
                    "pull_url": "https://github.com/owner/repo/pull/7",
                    "acceptance_verification": {
                        "id": "acceptance-1",
                        "commit_sha": "b" * 40,
                        "state": "passed",
                        "summary": "Scrolling was independently observed.",
                        "claims": [
                            {
                                "key": "scroll-history",
                                "claim": "Wheel input navigates retained history.",
                                "result": "pass",
                                "observed": "Visible rows changed after wheel input.",
                                "evidence": [1],
                            }
                        ],
                        "scope": [
                            {
                                "path": "src/web/app.ts",
                                "claim_keys": ["scroll-history"],
                                "necessity": "Implements wheel navigation.",
                                "result": "pass",
                            }
                        ],
                        "screenshot_decision": {
                            "required": True,
                            "reason": "The claim is visual.",
                        },
                        "artifacts": [
                            {
                                "id": "artifact-1",
                                "kind": "screenshot",
                                "description": "Rows after wheel input.",
                                "sha256": "c" * 64,
                                "media_type": "image/png",
                            }
                        ],
                        "limitations": [],
                    },
                }
            ],
            "notifications": [
                {
                    "id": "notice-1",
                    "owner": "owner",
                    "name": "repo",
                    "issue_number": 3,
                    "issue_url": "https://github.com/owner/repo/issues/3",
                    "issue_title": "Fix scrolling",
                    "pull_number": 7,
                    "pull_url": "https://github.com/owner/repo/pull/7",
                    "created_at": "2026-01-01T00:30:00Z",
                    "read_at": None,
                }
            ],
        }

    def add_repository(self, identity: str, inputs: dict[str, object]) -> str:
        self.calls.append(("add", identity, inputs))
        return "repo-2"

    def reonboard(self, repository_id: str, inputs: dict[str, object]) -> str:
        self.calls.append(("reonboard", repository_id, inputs))
        return "sandbox-2"

    def cancel(self, run_id: str) -> None:
        self.calls.append(("cancel", run_id))

    def acknowledge(self, notification_id: str) -> None:
        self.calls.append(("acknowledge", notification_id))

    def poll(self) -> None:
        self.calls.append(("poll",))

    def acceptance_artifact(self, artifact_id: str) -> tuple[bytes, str]:
        self.calls.append(("artifact", artifact_id))
        if artifact_id != "artifact-1":
            raise KeyError(artifact_id)
        return b"\x89PNG\r\n\x1a\nfixture", "image/png"


class InterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actions = FakeActions()
        self.server = LocalInterfaceServer(
            actions=self.actions,
            host="127.0.0.1",
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        host, port = self.server.address
        self.base = f"http://{host}:{port}"

    def request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        csrf: str | None = None,
        origin: str | None = None,
        host: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        if csrf is not None:
            request.add_header("X-Repogents-CSRF", csrf)
        if origin is not None:
            request.add_header("Origin", origin)
        if host is not None:
            request.add_header("Host", host)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def token(self) -> str:
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Repogents</title>", body)
        self.assertIn(b"<h1>Repogents</h1>", body)
        return headers["X-Repogents-CSRF"]

    def test_dashboard_state_exposes_inventory_runs_links_and_notifications(
        self,
    ) -> None:
        token = self.token()
        self.assertTrue(token)
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["repositories"][0]["identity"], "owner/repo")
        self.assertEqual(
            state["repositories"][0]["display_inputs"],
            {
                "host_paths": ["/srv/fixtures"],
                "secret_references": {"API_TOKEN": "configured"},
            },
        )
        self.assertEqual(
            state["runs"][0]["issue_url"], "https://github.com/owner/repo/issues/3"
        )
        self.assertEqual(
            state["runs"][0]["pull_url"], "https://github.com/owner/repo/pull/7"
        )
        self.assertIsNone(state["notifications"][0]["read_at"])
        acceptance = state["runs"][0]["acceptance_verification"]
        self.assertEqual(acceptance["state"], "passed")
        self.assertEqual(acceptance["claims"][0]["key"], "scroll-history")
        self.assertEqual(acceptance["artifacts"][0]["id"], "artifact-1")

    def test_dashboard_renders_display_inputs_and_prefills_reonboarding_from_them(
        self,
    ) -> None:
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        dashboard = body.decode("utf-8")
        self.assertIn("Retained inputs", dashboard)
        self.assertIn(
            "esc(JSON.stringify(r.display_inputs, null, 2))",
            dashboard,
        )
        self.assertIn(
            "displayInputs.get(b.dataset.reonboard)",
            dashboard,
        )
        self.assertNotIn(
            "prompt('Repository inputs JSON object:', '{}')",
            dashboard,
        )
        self.assertNotIn("r.inputs", dashboard)
        self.assertNotIn("data-retry", dashboard)
        self.assertNotIn("/retry", dashboard)
        self.assertIn("Issue acceptance", dashboard)
        self.assertIn("/api/acceptance-artifacts/", dashboard)
        self.assertIn("scope", dashboard)

    def test_acceptance_artifact_is_served_from_controller_storage(self) -> None:
        status, headers, body = self.request(
            "GET",
            "/api/acceptance-artifacts/artifact-1",
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(body, b"\x89PNG\r\n\x1a\nfixture")
        self.assertEqual(self.actions.calls, [("artifact", "artifact-1")])

    def test_mutations_require_exact_local_origin_and_csrf_token(self) -> None:
        token = self.token()
        status, _, _ = self.request(
            "POST", "/api/repositories", {"repository": "owner/second", "inputs": {}}
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST",
            "/api/repositories",
            {"repository": "owner/second", "inputs": {}},
            csrf=token,
            origin="https://attacker.example",
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.actions.calls, [])

    def test_mutations_pin_host_and_origin_to_bound_ephemeral_address(self) -> None:
        token = self.token()
        bound_host, bound_port = self.server.address
        self.assertNotEqual(bound_port, 0)
        canonical_authority = f"{bound_host}:{bound_port}"

        status, _, _ = self.request(
            "POST",
            "/api/poll",
            {},
            csrf=token,
            origin=f"http://{canonical_authority}",
            host=canonical_authority,
        )
        self.assertEqual(status, 200)

        for host, origin in (
            ("attacker.example", "http://attacker.example"),
            ("attacker.example", f"http://{canonical_authority}"),
            (canonical_authority, "http://attacker.example"),
        ):
            status, _, _ = self.request(
                "POST",
                "/api/poll",
                {},
                csrf=token,
                origin=origin,
                host=host,
            )
            self.assertEqual(status, 403, (host, origin))
        self.assertEqual(self.actions.calls, [("poll",)])

    def test_supported_actions_are_usable_through_http_client(self) -> None:
        token = self.token()
        headers = {"csrf": token, "origin": self.base}
        status, _, body = self.request(
            "POST",
            "/api/repositories",
            {"repository": "owner/second", "inputs": {"fixtures": ["sample"]}},
            **headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["repository_id"], "repo-2")
        routes = [
            ("/api/repositories/repo-1/reonboard", {"inputs": {"mode": "fresh"}}),
            ("/api/runs/run-1/cancel", {}),
            ("/api/notifications/notice-1/acknowledge", {}),
            ("/api/poll", {}),
        ]
        for path, payload in routes:
            status, _, _ = self.request("POST", path, payload, **headers)
            self.assertEqual(status, 200, path)
        self.assertEqual(
            self.actions.calls,
            [
                ("add", "owner/second", {"fixtures": ["sample"]}),
                ("reonboard", "repo-1", {"mode": "fresh"}),
                ("cancel", "run-1"),
                ("acknowledge", "notice-1"),
                ("poll",),
            ],
        )

    def test_invalid_payload_and_unknown_route_are_bounded_json_errors(self) -> None:
        token = self.token()
        status, _, body = self.request(
            "POST",
            "/api/repositories",
            {"repository": "", "inputs": []},
            csrf=token,
            origin=self.base,
        )
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        status, _, body = self.request("GET", "/missing")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))
        calls_before = list(self.actions.calls)
        status, _, body = self.request(
            "POST",
            "/api/runs/run-1/retry",
            {},
            csrf=token,
            origin=self.base,
        )
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.actions.calls, calls_before)


if __name__ == "__main__":
    unittest.main()
