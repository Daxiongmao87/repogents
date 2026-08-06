from __future__ import annotations

from pathlib import Path

import json
import os
import re
import shutil
import sys
import subprocess
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
                                {
                                    "key": "implement",
                                    "title": "Implement endpoint",
                                    "state": "COMPLETED",
                                    "dependencies": ["verify"],
                                },
                                {
                                    "key": "verify",
                                    "title": "Verify endpoint",
                                    "state": "COMPLETED",
                                    "dependencies": [],
                                },
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


def rendered_page(url):
    browser = shutil.which("google-chrome") or shutil.which("chromium")
    if browser is None:
        pytest.skip("a headless browser is required to inspect rendered client output")
    return subprocess.run(
        [browser, "--headless", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=1000", "--dump-dom", url],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout


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

    monkeypatch.setattr("repogents.store.Store", lambda path: object())
    monkeypatch.setattr(
        "repogents.github.GitHubClient",
        lambda token, **kwargs: object(),
    )
    monkeypatch.setattr("repogents.agent_runtime.MiniSweRuntime", lambda config: object())
    monkeypatch.setattr("repogents.semantic.SentenceTransformerEmbedder", lambda: object())
    monkeypatch.setattr("repogents.semantic.SemanticRouter", lambda embedder: object())
    monkeypatch.setattr("repogents.application.Application", ComposedApplication)
    monkeypatch.setattr(
        "repogents.http_api.HttpService",
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
        assert "Saved agent nodes" in html
        assert "Nodes are not ordered by execution" in html
        rendered_html = rendered_page(base + "/")
        assert "Declared execution dependencies: verify → implement." in rendered_html
        assert "Nodes are not ordered by execution" in rendered_html
        assert 'class="arrow"' not in rendered_html
        assert "Specifications" in html
        assert "Work items" in html
        assert "Pull request" in html

        status, state = request_json(base + "/api/state")
        assert status == 200
        assert state == {**application.payload, "poll_failure": None, "last_poll_failure": None}

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
    ("message", "secret"),
    [
        ("session_credential=delimiter-secret", "delimiter-secret"),
        ("session_credential whitespace-secret", "whitespace-secret"),
        ('{"session_credential":"json-secret"}', "json-secret"),
    ],
)
def test_poll_failure_sanitizer_fails_closed_for_unrecognized_credential_labels(message, secret):
    application = FakeApplication()
    service = HttpService(application, "127.0.0.1", 0, 60)
    service._record_poll_failure(RuntimeError(message))

    projected_state = service.state()
    assert projected_state["poll_failure"]["message"] == "poll failure details withheld"
    assert secret not in json.dumps(projected_state)


def test_http_service_reports_sanitized_poll_failures_and_recovers():
    class FailingThenHealthyApplication(FakeApplication):
        def __init__(self):
            super().__init__()
            self.failed = threading.Event()
            self.allow_recovery = threading.Event()

        def poll_once(self):
            if not self.failed.is_set():
                self.failed.set()
                raise RuntimeError(
                    'upstream session_credential=browser-must-not-see-session-secret\\ntraceback details'
                )
            assert self.allow_recovery.wait(timeout=3)

    application = FailingThenHealthyApplication()
    service = HttpService(application, "127.0.0.1", 0, 0.05)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    host, port = service.address

    def wait_for_state_field(field, predicate, description):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _, state = request_json(f"http://{host}:{port}/api/state")
            if predicate(state[field]):
                return state
            time.sleep(0.01)
        pytest.fail(f"poll failure did not {description}")

    try:
        failed_state = wait_for_state_field(
            "poll_failure", lambda failure: failure is not None, "appear in service state"
        )
        failure = failed_state["poll_failure"]
        assert failure["type"] == "RuntimeError"
        assert failure["message"] == "poll failure details withheld"
        assert "traceback" not in failure["message"]
        assert "browser-must-not-see-session-secret" not in json.dumps(failed_state)
        assert failure["occurred_at"].endswith("+00:00")

        application.allow_recovery.set()
        recovered_state = wait_for_state_field(
            "poll_failure", lambda failure: failure is None, "clear from service state"
        )
        assert recovered_state["poll_failure"] is None
        assert recovered_state["last_poll_failure"] == failure
        assert "browser-must-not-see-session-secret" not in json.dumps(recovered_state)

        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=3) as response:
            html = response.read().decode("utf-8")
        assert "Background polling failed" in html
        assert "renderPollFailure(state.last_poll_failure)" in html
        assert 'id="poll-failure" role="alert" aria-live="polite"' in html
        assert '${esc(failure.type)}: ${esc(failure.message)}' in html
        assert 'Last failure: ${esc(failure.occurred_at)}' in html
    finally:
        application.allow_recovery.set()
        service.shutdown()
        thread.join(timeout=3)



def test_transient_poll_failure_remains_observable_after_client_refresh_interval():
    class FailingThenHealthyApplication(FakeApplication):
        def __init__(self):
            super().__init__()
            self.failed = threading.Event()
            self.allow_recovery = threading.Event()

        def poll_once(self):
            self.poll_calls += 1
            if not self.failed.is_set():
                self.failed.set()
                raise RuntimeError(
                    "session_credential=browser-must-not-see-session-secret\ntraceback details"
                )
            assert self.allow_recovery.wait(timeout=3)

    application = FailingThenHealthyApplication()
    service = HttpService(application, "127.0.0.1", 0, 0.05)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    host, port = service.address

    def wait_for_poll_failure(predicate, description):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _, state = request_json(f"http://{host}:{port}/api/state")
            if predicate(state["poll_failure"]):
                return state
            time.sleep(0.01)
        pytest.fail(f"poll failure did not {description}")

    try:
        failed_state = wait_for_poll_failure(lambda failure: failure is not None, "appear")
        application.allow_recovery.set()
        recovered_state = wait_for_poll_failure(lambda failure: failure is None, "recover")
        assert recovered_state["last_poll_failure"] == failed_state["poll_failure"]

        time.sleep(3.1)
        _, delayed_state = request_json(f"http://{host}:{port}/api/state")
        assert delayed_state["poll_failure"] is None
        assert delayed_state["last_poll_failure"] == failed_state["poll_failure"]
        serialized_state = json.dumps(delayed_state)
        assert "browser-must-not-see-session-secret" not in serialized_state
        assert "traceback" not in serialized_state
        assert application.poll_calls > 2
    finally:
        application.allow_recovery.set()
        service.shutdown()
        thread.join(timeout=3)

def test_poll_failure_logs_distinct_private_diagnostics(caplog):
    application = FakeApplication()
    service = HttpService(application, "127.0.0.1", 0, 60)

    with caplog.at_level("ERROR", logger="repogents.http_api"):
        service._record_poll_failure(RuntimeError("GitHub response missing node ID"))
        service._record_poll_failure(RuntimeError("GitHub response has invalid node ID"))

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Background poll failed: GitHub response missing node ID",
        "Background poll failed: GitHub response has invalid node ID",
    ]
    assert service.state()["poll_failure"]["message"] == "poll failure details withheld"


def test_main_version_prints_project_version_without_configuration():
    project_root = Path(__file__).resolve().parents[1]
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    expected_version = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE).group(1)
    environment = os.environ.copy()
    for name in _ENV_NAMES:
        environment.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-m", "repogents.main", "--version"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"{expected_version}\n"
    assert result.stderr == ""
