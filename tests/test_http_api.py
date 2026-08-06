from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from repogents.config import ServiceConfig
import repogents.main as main_module
from repogents.http_api import HttpService


_ENV_NAMES = (
    "REPOGENTS_DATA_DIR",
    "REPOGENTS_GITHUB_TOKEN",
    "REPOGENTS_LAN_HOST",
    "REPOGENTS_LAN_PORT",
    "REPOGENTS_POLL_SECONDS",
    "REPOGENTS_PR_SILENCE_SECONDS",
    "REPOGENTS_CODEX_API_BASE",
    "REPOGENTS_SIMILARITY_THRESHOLD",
    "REPOGENTS_NODE_PROMOTION_THRESHOLD",
    "REPOGENTS_NODE_STALE_RUN_THRESHOLD",
    "OPENAI_API_KEY",
)


class FakeApplication:
    def __init__(self):
        self.poll_calls = 0
        self.added = []
        self.removed = []
        self.closed = False
        self.payload = {
            "repositories": [
                {
                    "id": 1,
                    "github_repository": "acme/widget",
                    "target_branch": "main",
                    "nodes": [
                        {"classification": "Specify", "persistence": "PERMANENT"},
                        {"classification": "backend/api", "persistence": "PERSISTENT"},
                        {"classification": "Validate", "persistence": "PERMANENT"},
                    ],
                    "runs": [
                        {
                            "id": 9,
                            "issue_number": 7,
                            "state": "PR_LISTENING",
                            "branch": "agent/issue-7",
                            "pull_request": {
                                "number": 17,
                                "url": "https://github.test/acme/widget/pull/17",
                            },
                            "specifications": [{"title": "Add endpoint"}],
                            "work_items": [
                                {"title": "Implement endpoint", "state": "COMPLETED"}
                            ],
                        }
                    ],
                }
            ]
        }

    def poll_once(self):
        self.poll_calls += 1

    def state(self):
        return self.payload

    def add_repository(self, github_repository, target_branch=None):
        self.added.append((github_repository, target_branch))
        return {
            "id": 2,
            "github_repository": github_repository,
            "target_branch": target_branch or "main",
        }

    def remove_repository(self, repository_id):
        self.removed.append(repository_id)

    def close(self):
        self.closed = True


def request_json(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = response.read()
        return response.status, None if not payload else json.loads(payload)


def test_service_config_uses_subscription_bridge_and_required_mvp_defaults(monkeypatch, tmp_path):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPOGENTS_GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("REPOGENTS_DATA_DIR", str(tmp_path))

    config = ServiceConfig.from_env()

    assert config.data_dir == tmp_path
    assert config.github_token == "github-token"
    assert config.host == "0.0.0.0"
    assert config.port == 8766
    assert config.poll_seconds == 60.0
    assert config.pr_silence_seconds == 3600.0
    assert config.codex_api_base == "http://127.0.0.1:8787/v1"
    assert config.model == "gpt-5.6-sol"
    assert config.similarity_threshold == 0.75
    assert config.promotion_threshold == 3
    assert config.stale_run_threshold == 3
    assert not hasattr(config, "openai_api_key")
    assert not hasattr(config, "proxy_access_token")


def test_service_config_requires_github_token_and_parses_configurable_thresholds(monkeypatch, tmp_path):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="REPOGENTS_GITHUB_TOKEN"):
        ServiceConfig.from_env()

    monkeypatch.setenv("REPOGENTS_GITHUB_TOKEN", "token")
    monkeypatch.setenv("REPOGENTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REPOGENTS_LAN_HOST", "192.168.0.206")
    monkeypatch.setenv("REPOGENTS_LAN_PORT", "9000")
    monkeypatch.setenv("REPOGENTS_POLL_SECONDS", "2.5")
    monkeypatch.setenv("REPOGENTS_PR_SILENCE_SECONDS", "1800.5")
    monkeypatch.setenv("REPOGENTS_CODEX_API_BASE", "http://127.0.0.1:8787/v1")
    monkeypatch.setenv("REPOGENTS_SIMILARITY_THRESHOLD", "0.61")
    monkeypatch.setenv("REPOGENTS_NODE_PROMOTION_THRESHOLD", "4")
    monkeypatch.setenv("REPOGENTS_NODE_STALE_RUN_THRESHOLD", "5")

    config = ServiceConfig.from_env()
    assert (config.host, config.port, config.poll_seconds, config.pr_silence_seconds) == (
        "192.168.0.206",
        9000,
        2.5,
        1800.5,
    )
    assert (
        config.similarity_threshold,
        config.promotion_threshold,
        config.stale_run_threshold,
    ) == (0.61, 4, 5)


@pytest.mark.parametrize("threshold", ["-0.01", "1", "1.01"])
def test_service_config_rejects_similarity_threshold_outside_routing_domain(
    monkeypatch, threshold
):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPOGENTS_GITHUB_TOKEN", "token")
    monkeypatch.setenv("REPOGENTS_SIMILARITY_THRESHOLD", threshold)

    with pytest.raises(ValueError, match="REPOGENTS_SIMILARITY_THRESHOLD"):
        ServiceConfig.from_env()


@pytest.mark.parametrize("silence_seconds", ["0", "-1"])
def test_service_config_rejects_nonpositive_pr_silence_seconds(
    monkeypatch, silence_seconds
):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPOGENTS_GITHUB_TOKEN", "token")
    monkeypatch.setenv("REPOGENTS_PR_SILENCE_SECONDS", silence_seconds)

    with pytest.raises(ValueError, match="REPOGENTS_PR_SILENCE_SECONDS"):
        ServiceConfig.from_env()


@pytest.mark.parametrize("silence_seconds", ["nan", "inf", "-inf"])
def test_service_config_rejects_nonfinite_pr_silence_seconds(
    monkeypatch, silence_seconds
):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPOGENTS_GITHUB_TOKEN", "token")
    monkeypatch.setenv("REPOGENTS_PR_SILENCE_SECONDS", silence_seconds)

    with pytest.raises(ValueError, match="REPOGENTS_PR_SILENCE_SECONDS"):
        ServiceConfig.from_env()


def test_build_service_passes_configured_pr_silence_seconds(monkeypatch, tmp_path):
    class ComposedApplication:
        def __init__(self, *args):
            self.config = args[-1]

    monkeypatch.setattr(main_module, "Store", lambda path: object())
    monkeypatch.setattr(
        main_module,
        "GitHubClient",
        lambda token, **kwargs: object(),
    )
    monkeypatch.setattr(main_module, "MiniSweRuntime", lambda config: object())
    monkeypatch.setattr(main_module, "SentenceTransformerEmbedder", lambda: object())
    monkeypatch.setattr(main_module, "SemanticRouter", lambda embedder: object())
    monkeypatch.setattr(main_module, "Application", ComposedApplication)
    monkeypatch.setattr(
        main_module,
        "HttpService",
        lambda application, *args, **kwargs: application,
    )
    config = ServiceConfig(
        data_dir=tmp_path,
        github_token="token",
        pr_silence_seconds=45.0,
    )

    service = main_module.build_service(config)

    assert service.config.pr_silence_seconds == 45.0


def test_http_service_exposes_client_state_repository_mutations_and_background_polling():
    application = FakeApplication()
    service = HttpService(application, "127.0.0.1", 0, 0.02)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    host, port = service.address
    base = f"http://{host}:{port}"
    try:
        with urllib.request.urlopen(base + "/", timeout=3) as response:
            html = response.read().decode("utf-8")
        assert response.status == 200
        assert "Track repository" in html
        assert "Saved agent graph" in html
        assert "Specifications" in html
        assert "Work items" in html
        assert "Pull request" in html

        status, state = request_json(base + "/api/state")
        assert status == 200
        assert state == {**application.payload, "poll_failure": None}

        status, added = request_json(
            base + "/api/repositories",
            method="POST",
            body={"github_repository": "acme/new", "target_branch": "develop"},
        )
        assert status == 201
        assert added["github_repository"] == "acme/new"
        assert application.added == [("acme/new", "develop")]

        status, body = request_json(base + "/api/repositories/2", method="DELETE")
        assert status == 204
        assert body is None
        assert application.removed == [2]

        for _ in range(100):
            if application.poll_calls:
                break
            time.sleep(0.01)
        assert application.poll_calls > 0
    finally:
        service.shutdown()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert application.closed is True


def test_http_service_rejects_malformed_repository_requests():
    application = FakeApplication()
    service = HttpService(application, "127.0.0.1", 0, 60)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    host, port = service.address
    try:
        request = urllib.request.Request(
            f"http://{host}:{port}/api/repositories",
            data=json.dumps({"target_branch": "main"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=3)
        assert captured.value.code == 400
        error = json.loads(captured.value.read())
        assert "github_repository" in error["error"]
    finally:
        service.shutdown()
        thread.join(timeout=3)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("token=delimiter-secret", "token=[redacted]"),
        ("api_key whitespace-secret", "api_key=[redacted]"),
        ('{"token":"json-secret"}', '{"token":"[redacted]"}'),
        ('{"access_token":"access-token-secret","client_secret":"client-secret-value"}', '{"access_token":"[redacted]","client_secret":"[redacted]"}'),
    ],
)
def test_poll_failure_sanitizer_redacts_delimited_and_whitespace_credentials(message, expected):
    sanitized = HttpService._sanitized_message(RuntimeError(message))
    assert sanitized == expected


def test_http_service_reports_sanitized_poll_failures_and_recovers():
    class FailingThenHealthyApplication(FakeApplication):
        def __init__(self):
            super().__init__()
            self.failed = threading.Event()

        def poll_once(self):
            self.poll_calls += 1
            if self.poll_calls == 1:
                self.failed.set()
                raise RuntimeError(
                    'upstream response {"access_token":"browser-must-not-see-this-access-token","client_secret":"browser-must-not-see-this-client-secret"}\ntraceback details'
                )

    application = FailingThenHealthyApplication()
    service = HttpService(application, "127.0.0.1", 0, 0.05)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    host, port = service.address
    try:
        assert application.failed.wait(timeout=3)
        _, failed_state = request_json(f"http://{host}:{port}/api/state")
        failure = failed_state["poll_failure"]
        assert failure["type"] == "RuntimeError"
        assert failure["message"] == 'upstream response {"access_token":"[redacted]","client_secret":"[redacted]"}'
        assert "traceback" not in failure["message"]
        assert "browser-must-not-see-this-access-token" not in json.dumps(failed_state)
        assert "browser-must-not-see-this-client-secret" not in json.dumps(failed_state)
        assert failure["occurred_at"].endswith("+00:00")

        for _ in range(100):
            if application.poll_calls >= 2:
                break
            time.sleep(0.01)
        assert application.poll_calls >= 2
        _, recovered_state = request_json(f"http://{host}:{port}/api/state")
        assert recovered_state["poll_failure"] is None

        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=3) as response:
            html = response.read().decode("utf-8")
        assert "Background polling failed" in html
        assert "renderPollFailure(state.poll_failure)" in html
        assert 'id="poll-failure" role="alert" aria-live="polite"' in html
        assert '${esc(failure.type)}: ${esc(failure.message)}' in html
        assert 'Last failure: ${esc(failure.occurred_at)}' in html
    finally:
        service.shutdown()
        thread.join(timeout=3)
