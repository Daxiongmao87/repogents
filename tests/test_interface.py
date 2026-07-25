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
        self._activity_condition = threading.Condition()
        self._activity_revision = 0
        self.model_configuration: dict[str, object] = {
            "configured": True,
            "api_endpoint": "https://models.example.test/v1",
            "default_model": "openai/default",
            "lead_model": "openai/lead",
            "implementer_model": None,
            "verifier_model": "openai/verifier",
            "api_key_configured": True,
            "api_key_required": True,
            "api_key_source": "saved",
        }
        self.run_reason: str | None = None
        self.run_reason_truncated = False
        self.activity_entries: list[dict[str, object]] = [
            {
                "kind": "transition",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": "queued → implementing",
            },
            {
                "kind": "agent",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": "Lead inspected src/app.py",
            },
        ]
        self.run_activity_entries: dict[str, list[dict[str, object]]] = {
            "run-1": [
                {
                    "kind": "transition",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": "run one queued → implementing",
                },
                {
                    "kind": "agent",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "message": "Run one lead inspected src/app.py",
                },
            ],
            "run-2": [
                {
                    "kind": "transition",
                    "timestamp": "2025-12-31T23:00:00Z",
                    "message": "run two waiting_for_feedback → closed",
                }
            ],
        }

    def state(self) -> dict[str, object]:
        return {
            "repositories": [
                {
                    "id": "repo-1",
                    "identity": "owner/repo",
                    "url": "https://github.com/owner/repo",
                    "default_branch": "main",
                    "enabled": True,
                    "active": True,
                    "active_run_count": 1,
                    "latest_run_state": "waiting_for_feedback",
                    "latest_activity_at": "2026-01-01T00:30:00Z",
                    "onboarding_state": "ready",
                    "blocking_reason": None,
                    "sandbox_version": 1,
                    "team_version": 1,
                    "team": {
                        "id": "team-1",
                        "version": 1,
                        "members": [
                            {
                                "stable_key": "lead",
                                "role": "lead",
                                "responsibilities": "Own the final result.",
                                "runtime": "mini-swe-agent",
                                "model": "test-model",
                                "instructions": "Lead the repository team.",
                            },
                            {
                                "stable_key": "verification",
                                "role": "verifier",
                                "responsibilities": "Verify observable behavior.",
                                "runtime": "mini-swe-agent",
                                "model": "test-model",
                                "instructions": "Verify independently.",
                            },
                        ],
                    },
                    "display_inputs": {
                        "host_paths": ["/srv/fixtures"],
                        "secret_references": {"API_TOKEN": "configured"},
                    },
                }
            ],
            "runs": [
                {
                    "id": "run-1",
                    "repository_id": "repo-1",
                    "repository": "owner/repo",
                    "issue_number": 3,
                    "issue_title": "Fix scrolling",
                    "issue_url": "https://github.com/owner/repo/issues/3",
                    "state": "waiting_for_feedback",
                    "priority": 0,
                    "queue_position": 1,
                    "forced": False,
                    "reason": self.run_reason,
                    "reason_truncated": self.run_reason_truncated,
                    "reason_severity": "neutral",
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
            "model_configuration": dict(self.model_configuration),
        }

    def configure_model(self, values: dict[str, object]) -> dict[str, object]:
        self.calls.append(("configure-model", dict(values)))
        default_model = values.get("default_model")
        if not isinstance(default_model, str) or not default_model.strip():
            raise ValueError("default model must be a nonempty string")
        self.model_configuration.update(
            {
                "configured": True,
                "api_endpoint": values.get("api_endpoint") or None,
                "default_model": default_model.strip(),
                "lead_model": values.get("lead_model") or None,
                "implementer_model": values.get("implementer_model") or None,
                "verifier_model": values.get("verifier_model") or None,
            }
        )
        if isinstance(values.get("api_key"), str) and values["api_key"]:
            self.model_configuration["api_key_configured"] = True
            self.model_configuration["api_key_source"] = "saved"
        if values.get("clear_api_key") is True:
            self.model_configuration["api_key_configured"] = False
            self.model_configuration["api_key_source"] = None
        return dict(self.model_configuration)

    def model_catalog(self) -> dict[str, object]:
        self.calls.append(("model-catalog",))
        return {
            "available": True,
            "reason": None,
            "models": [
                {
                    "id": "codex/gpt-5.6-sol",
                    "value": "openai/codex/gpt-5.6-sol",
                }
            ],
        }

    def add_repository(self, identity: str, inputs: dict[str, object]) -> str:
        self.calls.append(("add", identity, inputs))
        return "repo-2"

    def reonboard(self, repository_id: str, inputs: dict[str, object]) -> str:
        self.calls.append(("reonboard", repository_id, inputs))
        return "sandbox-2"

    def set_repository_enabled(self, repository_id: str, enabled: bool) -> None:
        if repository_id != "repo-1":
            raise KeyError(repository_id)
        self.calls.append(("enabled", repository_id, enabled))

    def remove_repository(self, repository_id: str) -> None:
        if repository_id != "repo-1":
            raise KeyError(repository_id)
        self.calls.append(("remove", repository_id))

    def repository_log(self, repository_id: str) -> dict[str, object]:
        self.calls.append(("log", repository_id))
        if repository_id != "repo-1":
            raise KeyError(repository_id)
        return {
            "repository_id": repository_id,
            "run_id": "run-1",
            "active": True,
            "entries": list(self.activity_entries),
        }

    def run_log(self, run_id: str) -> dict[str, object]:
        self.calls.append(("run-log", run_id))
        if run_id not in self.run_activity_entries:
            raise KeyError(run_id)
        number = 3 if run_id == "run-1" else 2
        state = "waiting_for_feedback" if run_id == "run-1" else "closed"
        return {
            "run_id": run_id,
            "repository_id": "repo-1",
            "repository": "owner/repo",
            "issue": {
                "id": f"issue-{number}",
                "number": number,
                "title": f"Issue {number}",
                "url": f"https://github.com/owner/repo/issues/{number}",
            },
            "state": state,
            "active": state != "closed",
            "entries": list(self.run_activity_entries[run_id]),
        }

    def activity_revision(self) -> int:
        with self._activity_condition:
            return self._activity_revision

    def wait_for_activity_change(self, revision: int, timeout: float) -> int:
        with self._activity_condition:
            self._activity_condition.wait_for(
                lambda: self._activity_revision != revision,
                timeout=timeout,
            )
            return self._activity_revision

    def publish_activity(self, message: str) -> None:
        with self._activity_condition:
            self.activity_entries.append(
                {
                    "kind": "agent",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": message,
                }
            )
            self._activity_revision += 1
            self._activity_condition.notify_all()

    def publish_run_activity(self, run_id: str, message: str) -> None:
        with self._activity_condition:
            self.run_activity_entries[run_id].append(
                {
                    "kind": "agent",
                    "timestamp": "2026-01-01T00:00:03Z",
                    "message": message,
                }
            )
            self._activity_revision += 1
            self._activity_condition.notify_all()

    def cancel(self, run_id: str) -> None:
        self.calls.append(("cancel", run_id))

    def reorder_runs(self, run_ids: list[str]) -> None:
        self.calls.append(("reorder", list(run_ids)))

    def set_run_forced(self, run_id: str, forced: bool) -> None:
        self.calls.append(("force", run_id, forced))

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
    ) -> tuple[int, dict[str, str], bytes]:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    @staticmethod
    def read_sse_event(response: object) -> tuple[str, dict[str, object]]:
        event = "message"
        data: list[str] = []
        while True:
            raw = response.readline()
            if not raw:
                raise AssertionError("activity stream closed before an event")
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                break
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data.append(line.split(":", 1)[1].lstrip())
        return event, json.loads("\n".join(data))

    def test_dashboard_state_exposes_inventory_runs_and_links(
        self,
    ) -> None:
        status, headers, dashboard = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Repogents-CSRF", headers)
        self.assertNotIn(b"X-Repogents-CSRF", dashboard)
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["repositories"][0]["identity"], "owner/repo")
        repository = state["repositories"][0]
        self.assertIs(repository["enabled"], True)
        self.assertIs(repository["active"], True)
        self.assertEqual(repository["active_run_count"], 1)
        self.assertEqual(repository["latest_run_state"], "waiting_for_feedback")
        self.assertEqual(
            repository["team"]["members"][0]["instructions"],
            "Lead the repository team.",
        )
        self.assertEqual(state["runs"][0]["repository_id"], "repo-1")
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
        self.assertNotIn("notifications", state)
        acceptance = state["runs"][0]["acceptance_verification"]
        self.assertEqual(acceptance["state"], "passed")
        self.assertEqual(acceptance["claims"][0]["key"], "scroll-history")
        self.assertEqual(acceptance["artifacts"][0]["id"], "artifact-1")
        model_configuration = state["model_configuration"]
        self.assertEqual(
            model_configuration["api_endpoint"],
            "https://models.example.test/v1",
        )
        self.assertIs(model_configuration["api_key_configured"], True)
        self.assertIs(model_configuration["api_key_required"], True)
        self.assertNotIn("api_key", model_configuration)
        self.assertNotIn("dashboard-secret", body.decode("utf-8"))

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

    def test_dashboard_contains_issue_queue_and_inline_log(
        self,
    ) -> None:
        status, _, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        dashboard = body.decode("utf-8")
        for expected in (
            'id="repository-list"',
            'id="repository-detail"',
            'id="issue-live-log"',
            "Add repository",
            "Remove repository",
            "Issues and Runs",
            "Issue Log",
            "Role prompt",
            'draggable="true"',
            "data-run-select",
            "data-force-run",
            "/api/runs/priority",
            "/force",
        ):
            self.assertIn(expected, dashboard)
        self.assertNotIn("Notifications", dashboard)
        for removed in (
            'id="live-log"',
            "Live activity",
            'id="run-log-dialog"',
            'id="run-live-log"',
            "View log",
        ):
            self.assertNotIn(removed, dashboard)
        self.assertIn("new EventSource(", dashboard)
        self.assertIn("/events", dashboard)
        self.assertNotIn("setInterval(refreshLog", dashboard)
        self.assertIn("/api/runs/", dashboard)

    def test_dashboard_contains_modal_write_only_model_configuration(self) -> None:
        status, _, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        dashboard = body.decode("utf-8")
        self.assertIn('id="open-model-configuration"', dashboard)
        self.assertIn('<dialog id="model-configuration-dialog"', dashboard)
        dialog_start = dashboard.index('<dialog id="model-configuration-dialog"')
        dialog_end = dashboard.index("</dialog>", dialog_start)
        dialog = dashboard[dialog_start:dialog_end]
        for expected in (
            'id="model-configuration-form"',
            'id="model-catalog"',
            "Model provider",
            "API endpoint",
            'type="password"',
            "Default model",
            "Lead model",
            "Implementer model",
            "Verifier model",
            "Re-onboard",
        ):
            self.assertIn(expected, dialog)
        self.assertIn("Model API credential is missing", dashboard)
        self.assertIn('class="blocking-error"', dashboard)
        self.assertNotIn("https://models.example.test/v1", dashboard)
        self.assertNotIn("dashboard-secret", dashboard)
        self.assertNotIn(
            '<section class="panel" aria-labelledby="model-configuration-title">',
            dashboard,
        )

    def test_model_catalog_is_available_through_secret_free_http(self) -> None:
        status, _, body = self.request("GET", "/api/model-configuration/models")

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "available": True,
                "reason": None,
                "models": [
                    {
                        "id": "codex/gpt-5.6-sol",
                        "value": "openai/codex/gpt-5.6-sol",
                    }
                ],
            },
        )
        self.assertNotIn("dashboard-secret", body.decode("utf-8"))
        self.assertEqual(self.actions.calls, [("model-catalog",)])

    def test_model_configuration_mutation_never_returns_key(self) -> None:
        payload = {
            "api_endpoint": "https://updated.example.test/v1",
            "api_key": "posted-dashboard-secret",  # pragma: allowlist secret
            "default_model": "openai/default-next",
            "lead_model": "openai/lead-next",
            "implementer_model": "",
            "verifier_model": "openai/verifier-next",
        }
        status, _, body = self.request(
            "POST",
            "/api/model-configuration",
            payload,
        )

        self.assertEqual(status, 200)
        response = json.loads(body)
        self.assertEqual(response["default_model"], "openai/default-next")
        self.assertIs(response["api_key_configured"], True)
        self.assertNotIn("api_key", response)
        self.assertNotIn("posted-dashboard-secret", body.decode("utf-8"))
        self.assertEqual(
            self.actions.calls,
            [("configure-model", payload)],
        )

        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn("posted-dashboard-secret", body.decode("utf-8"))

        status, _, body = self.request(
            "POST",
            "/api/model-configuration",
            {"default_model": ""},
        )
        self.assertEqual(status, 400)
        self.assertIn("default model", json.loads(body)["error"])

    def test_repository_log_snapshot_is_available_through_http(self) -> None:
        status, _, body = self.request(
            "GET",
            "/api/repositories/repo-1/logs",
        )

        self.assertEqual(status, 200)
        log = json.loads(body)
        self.assertEqual(log["repository_id"], "repo-1")
        self.assertIs(log["active"], True)
        self.assertEqual(log["entries"][-1]["kind"], "agent")
        self.assertEqual(self.actions.calls, [("log", "repo-1")])

    def test_repository_log_stream_pushes_initial_and_signaled_snapshots(
        self,
    ) -> None:
        request = urllib.request.Request(
            self.base + "/api/repositories/repo-1/events",
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers["Content-Type"],
                "text/event-stream; charset=utf-8",
            )
            event, initial = self.read_sse_event(response)
            self.assertEqual(event, "activity")
            self.assertEqual(
                initial["entries"][-1]["message"], "Lead inspected src/app.py"
            )

            self.actions.publish_activity("Lead committed the fix")
            event, changed = self.read_sse_event(response)
            self.assertEqual(event, "activity")
            self.assertEqual(
                changed["entries"][-1]["message"], "Lead committed the fix"
            )

    def test_run_log_snapshot_returns_the_selected_historical_run(self) -> None:
        status, _, body = self.request(
            "GET",
            "/api/runs/run-2/logs",
        )

        self.assertEqual(status, 200)
        log = json.loads(body)
        self.assertEqual(log["run_id"], "run-2")
        self.assertEqual(log["issue"]["number"], 2)
        self.assertEqual(log["state"], "closed")
        messages = "\n".join(entry["message"] for entry in log["entries"])
        self.assertIn("run two", messages)
        self.assertNotIn("run one", messages)
        self.assertEqual(self.actions.calls, [("run-log", "run-2")])

    def test_state_bounds_issue_reason_while_run_log_retains_detail(self) -> None:
        full_reason = ("acceptance failure " * 50) + "\nfull command output"
        summary = full_reason.splitlines()[0][:400] + "…"
        self.actions.run_reason = summary
        self.actions.run_reason_truncated = True
        self.actions.run_activity_entries["run-1"].append(
            {
                "kind": "error",
                "timestamp": "2026-01-01T00:00:04Z",
                "message": full_reason,
            }
        )

        state_status, _, state_body = self.request("GET", "/api/state")
        log_status, _, log_body = self.request("GET", "/api/runs/run-1/logs")

        self.assertEqual(state_status, 200)
        self.assertEqual(log_status, 200)
        run = json.loads(state_body)["runs"][0]
        self.assertEqual(run["reason"], summary)
        self.assertIs(run["reason_truncated"], True)
        self.assertNotIn(full_reason, state_body.decode("utf-8"))
        messages = "\n".join(
            entry["message"] for entry in json.loads(log_body)["entries"]
        )
        self.assertIn(full_reason, messages)

    def test_run_log_stream_pushes_only_the_selected_run(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/runs/run-1/events",
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers["Content-Type"],
                "text/event-stream; charset=utf-8",
            )
            event, initial = self.read_sse_event(response)
            self.assertEqual(event, "activity")
            self.assertEqual(initial["run_id"], "run-1")
            self.assertEqual(initial["issue"]["number"], 3)
            self.assertNotIn(
                "run two",
                "\n".join(entry["message"] for entry in initial["entries"]),
            )

            self.actions.publish_run_activity(
                "run-1",
                "Run one lead committed the fix",
            )
            event, changed = self.read_sse_event(response)
            self.assertEqual(event, "activity")
            self.assertEqual(changed["run_id"], "run-1")
            self.assertEqual(
                changed["entries"][-1]["message"],
                "Run one lead committed the fix",
            )

    def test_unknown_run_snapshot_and_stream_return_bounded_not_found(self) -> None:
        for path in (
            "/api/runs/missing/logs",
            "/api/runs/missing/events",
        ):
            status, _, body = self.request("GET", path)
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body), {"error": "run not found"})
            self.assertLess(len(body), 200)

    def test_unknown_repository_log_stream_returns_not_found(self) -> None:
        status, _, body = self.request(
            "GET",
            "/api/repositories/missing/events",
        )

        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def test_unknown_repository_controls_and_log_return_not_found(self) -> None:
        status, _, body = self.request(
            "GET",
            "/api/repositories/missing/logs",
        )
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

        for path, payload in (
            ("/api/repositories/missing/enabled", {"enabled": False}),
            ("/api/repositories/missing/remove", {}),
        ):
            status, _, body = self.request("POST", path, payload)
            self.assertEqual(status, 404)
            self.assertIn("error", json.loads(body))

    def test_acceptance_artifact_is_served_from_controller_storage(self) -> None:
        status, headers, body = self.request(
            "GET",
            "/api/acceptance-artifacts/artifact-1",
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(body, b"\x89PNG\r\n\x1a\nfixture")
        self.assertEqual(self.actions.calls, [("artifact", "artifact-1")])

    def test_force_and_release_work_without_authorization_headers(self) -> None:
        for forced in (True, False):
            status, _, body = self.request(
                "POST",
                "/api/runs/run-1/force",
                {"forced": forced},
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True, "forced": forced})
        self.assertEqual(
            self.actions.calls,
            [
                ("force", "run-1", True),
                ("force", "run-1", False),
            ],
        )

    def test_wildcard_bind_accepts_force_without_authorization_headers(
        self,
    ) -> None:
        server = LocalInterfaceServer(
            actions=self.actions,
            host="0.0.0.0",
            port=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.address[1]}"

        for forced in (True, False):
            request = urllib.request.Request(
                base + "/api/runs/run-1/force",
                data=json.dumps({"forced": forced}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read()),
                    {"ok": True, "forced": forced},
                )
        self.assertEqual(
            self.actions.calls,
            [
                ("force", "run-1", True),
                ("force", "run-1", False),
            ],
        )

    def test_supported_actions_are_usable_through_http_client(self) -> None:
        status, _, body = self.request(
            "POST",
            "/api/repositories",
            {"repository": "owner/second", "inputs": {"fixtures": ["sample"]}},
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["repository_id"], "repo-2")
        status, _, body = self.request(
            "POST",
            "/api/repositories/repo-1/enabled",
            {"enabled": False},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"ok": True, "enabled": False, "paused": True},
        )
        status, _, body = self.request(
            "POST",
            "/api/repositories/repo-1/enabled",
            {"enabled": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"ok": True, "enabled": True, "paused": False},
        )
        routes = [
            ("/api/repositories/repo-1/reonboard", {"inputs": {"mode": "fresh"}}),
            ("/api/repositories/repo-1/remove", {}),
            ("/api/runs/priority", {"run_ids": ["run-1"]}),
            ("/api/runs/run-1/force", {"forced": True}),
            ("/api/runs/run-1/cancel", {}),
            ("/api/poll", {}),
        ]
        for path, payload in routes:
            status, _, _ = self.request("POST", path, payload)
            self.assertEqual(status, 200, path)
        self.assertEqual(
            self.actions.calls,
            [
                ("add", "owner/second", {"fixtures": ["sample"]}),
                ("enabled", "repo-1", False),
                ("enabled", "repo-1", True),
                ("reonboard", "repo-1", {"mode": "fresh"}),
                ("remove", "repo-1"),
                ("reorder", ["run-1"]),
                ("force", "run-1", True),
                ("cancel", "run-1"),
                ("poll",),
            ],
        )
        status, _, _ = self.request(
            "POST",
            "/api/notifications/notice-1/acknowledge",
            {},
        )
        self.assertEqual(status, 404)

    def test_invalid_payload_and_unknown_route_are_bounded_json_errors(self) -> None:
        status, _, body = self.request(
            "POST",
            "/api/repositories",
            {"repository": "", "inputs": []},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        status, _, body = self.request(
            "POST",
            "/api/repositories/repo-1/enabled",
            {"enabled": "yes"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        status, _, body = self.request(
            "POST",
            "/api/runs/priority",
            {"run_ids": "run-1"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        status, _, body = self.request(
            "POST",
            "/api/runs/run-1/force",
            {"forced": "yes"},
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
        )
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.actions.calls, calls_before)


if __name__ == "__main__":
    unittest.main()
