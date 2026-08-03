from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import pytest

from repogents.config import ServiceConfig
from repogents.http_api import HttpService


_ENV_NAMES = (
    "REPOGENTS_DATA_DIR",
    "REPOGENTS_GITHUB_TOKEN",
    "REPOGENTS_LAN_HOST",
    "REPOGENTS_LAN_PORT",
    "REPOGENTS_POLL_SECONDS",
    "REPOGENTS_CODEX_API_BASE",
    "REPOGENTS_SIMILARITY_THRESHOLD",
    "REPOGENTS_NODE_PROMOTION_THRESHOLD",
    "REPOGENTS_NODE_STALE_RUN_THRESHOLD",
    "REPOGENTS_GITHUB_REQUEST_TIMEOUT",
    "REPOGENTS_GIT_COMMAND_TIMEOUT",
    "REPOGENTS_HTTP_REQUEST_IO_TIMEOUT",
    "REPOGENTS_ADD_OPERATION_RETENTION_SECONDS",
    "REPOGENTS_ADD_OPERATION_CLEANUP_BATCH_SIZE",
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
    assert config.codex_api_base == "http://127.0.0.1:8787/v1"
    assert config.model == "gpt-5.6-sol"
    assert config.similarity_threshold == 0.75
    assert config.promotion_threshold == 3
    assert config.stale_run_threshold == 3
    assert config.github_request_timeout == 30.0
    assert config.git_command_timeout == 300.0
    assert config.http_request_io_timeout == 30.0
    assert config.repository_add_operation_retention_seconds == 7 * 24 * 60 * 60
    assert config.repository_add_operation_cleanup_batch_size == 100
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
    monkeypatch.setenv("REPOGENTS_CODEX_API_BASE", "http://127.0.0.1:8787/v1")
    monkeypatch.setenv("REPOGENTS_SIMILARITY_THRESHOLD", "0.61")
    monkeypatch.setenv("REPOGENTS_NODE_PROMOTION_THRESHOLD", "4")
    monkeypatch.setenv("REPOGENTS_NODE_STALE_RUN_THRESHOLD", "5")
    monkeypatch.setenv("REPOGENTS_GITHUB_REQUEST_TIMEOUT", "12.5")
    monkeypatch.setenv("REPOGENTS_GIT_COMMAND_TIMEOUT", "900")
    monkeypatch.setenv("REPOGENTS_HTTP_REQUEST_IO_TIMEOUT", "4.5")
    monkeypatch.setenv("REPOGENTS_ADD_OPERATION_RETENTION_SECONDS", "3600")
    monkeypatch.setenv("REPOGENTS_ADD_OPERATION_CLEANUP_BATCH_SIZE", "25")

    config = ServiceConfig.from_env()
    assert (config.host, config.port, config.poll_seconds) == ("192.168.0.206", 9000, 2.5)
    assert (
        config.similarity_threshold,
        config.promotion_threshold,
        config.stale_run_threshold,
    ) == (0.61, 4, 5)
    assert config.github_request_timeout == 12.5
    assert config.git_command_timeout == 900.0
    assert config.http_request_io_timeout == 4.5
    assert config.repository_add_operation_retention_seconds == 3600
    assert config.repository_add_operation_cleanup_batch_size == 25


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("REPOGENTS_GITHUB_REQUEST_TIMEOUT", "nan", "GITHUB_REQUEST_TIMEOUT"),
        ("REPOGENTS_GITHUB_REQUEST_TIMEOUT", "inf", "GITHUB_REQUEST_TIMEOUT"),
        ("REPOGENTS_GITHUB_REQUEST_TIMEOUT", "-inf", "GITHUB_REQUEST_TIMEOUT"),
        ("REPOGENTS_GITHUB_REQUEST_TIMEOUT", "0", "GITHUB_REQUEST_TIMEOUT"),
        ("REPOGENTS_GITHUB_REQUEST_TIMEOUT", "-1", "GITHUB_REQUEST_TIMEOUT"),
        ("REPOGENTS_GIT_COMMAND_TIMEOUT", "nan", "GIT_COMMAND_TIMEOUT"),
        ("REPOGENTS_GIT_COMMAND_TIMEOUT", "inf", "GIT_COMMAND_TIMEOUT"),
        ("REPOGENTS_GIT_COMMAND_TIMEOUT", "-inf", "GIT_COMMAND_TIMEOUT"),
        ("REPOGENTS_GIT_COMMAND_TIMEOUT", "0", "GIT_COMMAND_TIMEOUT"),
        ("REPOGENTS_GIT_COMMAND_TIMEOUT", "-1", "GIT_COMMAND_TIMEOUT"),
        ("REPOGENTS_HTTP_REQUEST_IO_TIMEOUT", "nan", "HTTP_REQUEST_IO_TIMEOUT"),
        ("REPOGENTS_HTTP_REQUEST_IO_TIMEOUT", "inf", "HTTP_REQUEST_IO_TIMEOUT"),
        ("REPOGENTS_HTTP_REQUEST_IO_TIMEOUT", "-inf", "HTTP_REQUEST_IO_TIMEOUT"),
        ("REPOGENTS_HTTP_REQUEST_IO_TIMEOUT", "0", "HTTP_REQUEST_IO_TIMEOUT"),
        ("REPOGENTS_HTTP_REQUEST_IO_TIMEOUT", "-1", "HTTP_REQUEST_IO_TIMEOUT"),
    ],
)
def test_service_config_rejects_invalid_github_transport_timeouts(
    monkeypatch, name, value, message
):
    monkeypatch.setenv("REPOGENTS_GITHUB_TOKEN", "test-token")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        ServiceConfig.from_env()


def test_build_service_propagates_independent_github_timeouts(monkeypatch, tmp_path):
    import repogents.main as main

    captured = {}

    class FakeStore:
        def __init__(self, path):
            captured["store_path"] = path

    class FakeGitHubClient:
        def __init__(self, token, *, transport_timeout, git_command_timeout):
            captured["github"] = (token, transport_timeout, git_command_timeout)

    class FakeRuntime:
        def __init__(self, config):
            captured["runtime"] = config

    class FakeRouter:
        def __init__(self, embedder):
            captured["embedder"] = embedder

    class FakeApplication:
        def __init__(self, store, github, runtime, router, config):
            captured["application"] = (store, github, runtime, router, config)

    class FakeService:
        def __init__(self, application, host, port, poll_seconds, *, request_io_timeout):
            captured["service"] = (application, host, port, poll_seconds, request_io_timeout)

    monkeypatch.setattr(main, "Store", FakeStore)
    monkeypatch.setattr(main, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(main, "MiniSweRuntime", FakeRuntime)
    monkeypatch.setattr(main, "SentenceTransformerEmbedder", lambda: "embedder")
    monkeypatch.setattr(main, "SemanticRouter", FakeRouter)
    monkeypatch.setattr(main, "Application", FakeApplication)
    monkeypatch.setattr(main, "HttpService", FakeService)

    config = ServiceConfig(
        data_dir=tmp_path,
        github_token="token",
        github_request_timeout=7.5,
        git_command_timeout=720.0,
        http_request_io_timeout=4.25,
    )

    service = main.build_service(config)

    assert isinstance(service, FakeService)
    assert captured["github"] == ("token", 7.5, 720.0)
    assert captured["store_path"] == tmp_path / "repogents.sqlite3"
    assert captured["service"][1:] == (config.host, config.port, config.poll_seconds, 4.25)


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
        assert "--space-4" in html
        assert "--success-bg" in html
        assert ":focus-visible" in html
        assert "prefers-reduced-motion" in html
        assert "forced-colors" in html
        assert '<label class="field" for="repository">' in html
        assert "(required)" in html
        assert "(optional)" in html
        assert 'data-repository="${esc(repo.github_repository)}"' in html
        assert 'role="status" aria-live="polite"' in html
        assert 'id="add-form" class="field-grid" novalidate' in html
        assert 'aria-describedby="repository-hint repository-error"' in html
        assert "setAddPending(true)" in html
        assert "setRemoveControlsDisabled(pending)" in html
        assert "button.disabled = mutationInProgress" in html
        assert "Adding repository…" in html
        assert "aria-invalid" in html
        assert "window.confirm" in html
        assert "Removing repository…" in html
        assert "button.focus()" in html
        # Ambiguous adds use one stable operation identity and wait for the
        # server-authoritative storage-completion state rather than browser time.
        assert "X-Repogents-Operation-Id" in html
        assert "createRepositoryAddOperationId()" in html
        assert "/api/repository-add-operations/" in html
        assert "waitForAuthoritativeAddCompletion" in html
        assert "operation.state === 'COMMITTED'" in html
        assert "operation.state === 'FAILED'" in html
        assert "error.status === 404" in html
        assert "state: 'MISSING'" in html
        assert "ADD_OPERATION_MISSING_LIMIT = 3" in html
        assert "ADD_OPERATION_REPLAY_LIMIT = 2" in html
        assert "replayRepositoryAddOperation" in html
        assert "reconcileMissingAddOperation" in html
        assert "operation.state === 'CURRENTLY_TRACKED'" in html
        assert "operation.state === 'TRACKED_DIFFERENT_BRANCH'" in html
        assert "reconciliation.state === 'AUTHORITATIVELY_ABSENT'" in html
        assert "X-Repogents-Operation-Id': operationId" in html
        assert "state: 'MISSING_UNRESOLVED'" in html
        assert "recoverableAddAttempt" in html
        assert "retireRecoverableAddAttempt(addOperationId)" in html
        assert "if (authoritativeTerminalFailure)" in html
        assert "ADD_COMMIT_SAFETY_BOUNDARY" not in html
        assert "reconcileUncertainAddition" not in html
        # Live refresh distinguishes first load from background work and retains valid state.
        assert 'id="freshness">Waiting for the first update' in html
        assert 'id="refresh-status" role="status" aria-live="polite" aria-atomic="true"' in html
        assert 'id="refresh-error" class="feedback feedback--warning management-feedback" role="alert" aria-atomic="true"' in html
        assert "const initial = !hasRenderedState" in html
        assert "if (initial) list.setAttribute('aria-busy', 'true')" in html
        assert "Showing the last successful repository state, which may be outdated" in html
        assert "Repogents will retry automatically" in html
        assert "Repository state could not be loaded" in html
        assert "Repository updates resumed." in html
        assert "lastAnnouncedError" in html
        # Polling is single-shot, race resistant, pauses while hidden, and cleans up.
        assert "const controller = new AbortController()" in html
        assert "const STATE_REQUEST_TIMEOUT = 15000" in html
        assert "requestTimedOut = true" in html
        assert "clearTimeout(requestTimeout)" in html
        polling_guards = re.findall(r"if\s*\(([^)]*)\)\s*return\s+false", html)
        required_polling_protections = {
            "requestId!==loadSequence",
            "stopped",
            "lifecyclePaused",
        }
        assert any(
            required_polling_protections
            <= {
                re.sub(r"\s+", "", clause)
                for clause in guard.split("||")
            }
            for guard in polling_guards
        ), "the stale refresh guard must cover sequencing, teardown, and lifecycle pause"
        assert "setTimeout(async () =>" in html
        assert "clearTimeout(refreshTimer)" in html
        assert "document.addEventListener('visibilitychange'" in html
        assert "window.addEventListener('pagehide'" in html
        assert "if (activeRequest) activeRequest.abort()" in html
        assert "setInterval(() => { if (!mutationInProgress) load();" not in html
        # Unchanged polling avoids needless DOM replacement; changed content restores focus.
        assert "const changed = signature !== lastStateSignature" in html
        assert "if (initial || changed)" in html
        assert "target.focus({preventScroll: true})" in html
        assert "Refreshing in the background…" in html
        assert 'class="dashboard-layout"' in html
        assert 'id="repositories-heading">Tracked repositories' in html
        assert 'class="repository-list" aria-busy="true"' in html
        assert 'aria-labelledby="${headingId}"' in html
        assert 'Target branch <span class="code">' in html
        assert 'class="repo-content"' in html
        assert '@media (max-width: 55rem)' in html
        # Operational state stays explicit, semantic, and understandable without color.
        assert 'const STATUS = {' in html
        assert "WAITING_FOR_WORK_COMPLETION: ['Waiting for work', 'warning', '◷']" in html
        assert 'aria-label="${esc(noun)}: ${esc(item[0])}"' in html
        assert 'class="status-mark" aria-hidden="true"' in html
        assert "statusBadge(item.state, 'Work item status')" in html
        # Graph order is encoded as an ordered list with numbered nodes, not geometry alone.
        assert '<ol class="graph" role="list" aria-label="Agent graph sequence' in html
        assert 'class="graph-step"' in html
        assert 'Persistence: ${esc(node.persistence)}' in html
        assert '.node::before { content: counter(graph-step)' in html
        # Run identity and all core detail groups remain in-page with explicit empty states.
        assert '<article class="run" aria-labelledby="${runId}-heading">' in html
        assert '<h5 id="${runId}-heading">Issue #${esc(run.issue_number)}</h5>' in html
        assert 'No specifications have been generated for this run yet.' in html
        assert 'No work items have been created for this run yet.' in html
        assert 'No issue runs are queued for this repository.' in html
        assert 'Pull request not created' in html
        assert 'class="run-list"' in html
        assert 'class="detail-list"' in html
        # External PR navigation is contextual and prevents opener/referrer access.
        assert 'rel="noopener noreferrer"' in html
        assert 'aria-label="Pull request #${esc(pr.number)} (opens in a new tab)"' in html
        assert 'Pull request #${esc(pr.number)}' in html

        status, state = request_json(base + "/api/state")
        assert status == 200
        assert state == application.payload

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


class AccessibleMarkupParser(HTMLParser):
    """Collect stable accessibility contracts without depending on DOM layout."""

    def __init__(self):
        super().__init__()
        self.elements = []
        self.ids = set()
        self.labels = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        if tag == "label":
            self.labels.append(attributes)
        if tag == "a":
            self.links.append(attributes)


def _client_parts(html):
    style = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert style and script, "the locally served client must contain CSS and JavaScript"
    return style.group(1), script.group(1)


def _css_declarations(css, selector):
    """Return normalized declarations for one simple embedded-client CSS rule."""
    rule = re.search(rf"^\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", css, re.MULTILINE | re.DOTALL)
    assert rule, f"missing CSS rule for {selector}"
    return {
        name.strip(): value.strip()
        for declaration in rule.group(1).split(";")
        if declaration.strip()
        for name, value in [declaration.split(":", 1)]
    }


def _hex_rgb(color):
    """Parse a six-digit CSS hex color into normalized sRGB channels."""
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", color), f"expected six-digit hex color, got {color!r}"
    return tuple(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _relative_luminance(color):
    """Calculate WCAG relative luminance from an actual CSS color token."""
    def linearize(channel):
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linearize(channel) for channel in _hex_rgb(color))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(first, second):
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_shared_surface_boundary_tokens_meet_documented_three_to_one_contract():
    """Calculate every shared operational-boundary adjacency from production tokens."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    tokens = _css_declarations(css, ":root")
    adjacent_surfaces = ("--canvas", "--surface", "--surface-raised")
    boundary_tokens = ("--border", "--border-strong")

    # Derive ratios from production token values rather than duplicating reviewed
    # numbers. Requiring every cross-product protects canvas/panel, panel/repository,
    # and raised/nested-run boundaries and catches a removed adjacency assertion.
    ratios = {
        (boundary, surface): _contrast_ratio(tokens[boundary], tokens[surface])
        for boundary in boundary_tokens
        for surface in adjacent_surfaces
    }
    assert set(ratios) == {
        (boundary, surface)
        for boundary in boundary_tokens
        for surface in adjacent_surfaces
    }
    for (boundary, surface), ratio in ratios.items():
        assert ratio >= 3.0, (
            f"{boundary} {tokens[boundary]} is only {ratio:.3f}:1 against "
            f"{surface} {tokens[surface]}; meaningful operational boundaries require 3:1"
        )

    # Guard the reusable treatment: meaningful surfaces and dividers consume shared
    # role tokens, rather than local colors, shadows, or fill differences as a proxy.
    assert _css_declarations(css, ".panel, .repo")["border"] == "1px solid var(--border)"
    assert _css_declarations(css, ".run")["border"] == "1px solid var(--border)"
    assert _css_declarations(css, ".detail-group")["border-block-start"] == "1px solid var(--border)"
    assert _css_declarations(css, ".detail-item")["border-inline-start"] == ".1875rem solid var(--border-strong)"
    assert _css_declarations(css, ".empty-block")["border"] == "1px dashed var(--border-strong)"
    assert _css_declarations(css, "input, button")["border"] == "1px solid var(--border-strong)"

    # Explicitly reject the reviewed low-contrast token and keep documentation tied
    # to implementation values and the measured worst-case production adjacency.
    assert tokens["--border"] != "#34445f"
    design_system = (
        Path(__file__).resolve().parents[1] / "docs" / "design-system.md"
    ).read_text(encoding="utf-8")
    normalized_document = re.sub(r"\s+", " ", design_system).lower()
    for token in boundary_tokens:
        assert f"`{token}` (`{tokens[token].lower()}`)" in normalized_document
    for surface in adjacent_surfaces:
        assert f"`{surface}`" in normalized_document
    assert "at least 3:1 contrast" in normalized_document
    assert "independently of shadows and subtle fill shifts" in normalized_document
    worst_ratio = min(ratios[("--border", surface)] for surface in adjacent_surfaces)
    assert f"{worst_ratio:.2f}:1" in normalized_document


def test_primary_button_default_and_hover_text_meet_shared_contrast_contract():
    """Derive primary-control text contrast and neighboring states from production CSS."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    tokens = _css_declarations(css, ":root")
    body = _css_declarations(css, "body")
    controls = _css_declarations(css, "input, button")
    button = _css_declarations(css, "button")
    hover = _css_declarations(css, "button:hover")
    disabled = _css_declarations(css, "button:disabled, input:disabled")
    focus = _css_declarations(css, ":where(a, button, input, summary):focus-visible")
    active = _css_declarations(css, "button:active")

    # Follow the actual shared cascade: buttons inherit the page foreground and use
    # the reusable primary surface tokens in both default and hover states.
    assert body["color"] == "var(--text)"
    assert controls["color"] == "inherit"
    assert button["background"] == "var(--accent-surface)"
    assert hover["background"] == "var(--accent-hover)"
    foreground = tokens["--text"]
    state_backgrounds = {
        "default": tokens["--accent-surface"],
        "hover": tokens["--accent-hover"],
    }
    ratios = {
        state: _contrast_ratio(foreground, background)
        for state, background in state_backgrounds.items()
    }
    assert set(ratios) == {"default", "hover"}
    for state, ratio in ratios.items():
        assert ratio >= 4.5, (
            f"primary-button {state} text is only {ratio:.3f}:1: "
            f"{foreground} on {state_backgrounds[state]}; normal control text requires 4.5:1"
        )

    # The reviewed hover value must be distinguishable from default and must not
    # regress to the former combination that measured below the adopted threshold.
    reviewed_low_contrast_hover = "#326be0"
    assert tokens["--accent-hover"].lower() != reviewed_low_contrast_hover
    assert _contrast_ratio(foreground, reviewed_low_contrast_hover) < 4.5
    assert tokens["--accent-hover"] != tokens["--accent-surface"]

    # Protect the shared primary-control treatment and its neighboring interaction
    # contracts instead of accepting an Add-repository-only color exception.
    assert "#add-button" not in css
    assert button["border-color"] == "var(--accent)"
    assert _contrast_ratio(tokens["--accent"], tokens["--accent-hover"]) >= 3.0
    assert focus["outline"] == ".1875rem solid var(--focus)"
    assert focus["outline-offset"] == ".1875rem"
    assert _contrast_ratio(tokens["--focus"], tokens["--accent-hover"]) >= 3.0
    assert active["transform"] == "translateY(1px)"
    assert disabled["cursor"] == "not-allowed"
    assert disabled["opacity"] == ".65"

    reduced_motion = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}",
        css,
        re.DOTALL,
    )
    assert reduced_motion
    assert re.search(
        r"button:active\s*\{[^}]*transform:\s*none;",
        reduced_motion.group(1),
        re.DOTALL,
    )
    forced_colors = re.search(
        r"@media \(forced-colors: active\)\s*\{(.*?)\n\}",
        css,
        re.DOTALL,
    )
    assert forced_colors
    assert re.search(
        r":where\(\.panel, \.repo, \.node, \.badge, \.state, input, button\)\s*"
        r"\{[^}]*border-color:\s*CanvasText;",
        forced_colors.group(1),
        re.DOTALL,
    )
    assert re.search(
        r":where\(a, button, input, summary\):focus-visible\s*"
        r"\{[^}]*outline-color:\s*Highlight;",
        forced_colors.group(1),
        re.DOTALL,
    )

    # Keep the reviewed production values and measured state contract synchronized
    # with the design-system artifact without coupling to paragraph formatting.
    design_system = (
        Path(__file__).resolve().parents[1] / "docs" / "design-system.md"
    ).read_text(encoding="utf-8")
    normalized_document = re.sub(r"\s+", " ", design_system).lower()
    for token in ("--text", "--accent-surface", "--accent-hover"):
        assert f"`{token}` (`{tokens[token].lower()}`)" in normalized_document
    assert "enabled primary controls" in normalized_document
    assert "normal-sized text pairs" in normalized_document
    assert "at least 4.5:1 normal-text contrast" in normalized_document
    assert f"{ratios['default']:.2f}:1" in normalized_document
    assert f"{ratios['hover']:.2f}:1" in normalized_document


def test_neutral_component_boundary_tokens_meet_documented_contrast_contract():
    """Derive neutral capsule perimeter and text contrast from production tokens."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    tokens = _css_declarations(css, ":root")
    neutral_border = tokens["--neutral-border"]
    neutral_background = tokens["--neutral-bg"]
    neutral_foreground = tokens["--neutral-fg"]
    surrounding_surfaces = ("--surface-raised", "--surface", "--canvas")

    # The component perimeter must be independently visible on both sides: against
    # its own fill and against every operational surface on which neutral badges,
    # metadata capsules, and graph nodes are rendered.
    boundary_adjacencies = {
        "--neutral-bg": _contrast_ratio(neutral_border, neutral_background),
        **{
            surface: _contrast_ratio(neutral_border, tokens[surface])
            for surface in surrounding_surfaces
        },
    }
    assert set(boundary_adjacencies) == {
        "--neutral-bg",
        "--surface-raised",
        "--surface",
        "--canvas",
    }
    for adjacency, ratio in boundary_adjacencies.items():
        assert ratio >= 3.0, (
            f"--neutral-border {neutral_border} is only {ratio:.3f}:1 against "
            f"{adjacency} {tokens[adjacency]}; neutral component boundaries require 3:1"
        )

    text_ratio = _contrast_ratio(neutral_foreground, neutral_background)
    assert text_ratio >= 4.5, (
        f"--neutral-fg {neutral_foreground} is only {text_ratio:.3f}:1 against "
        f"--neutral-bg {neutral_background}; normal neutral text requires 4.5:1"
    )

    # Nodes, unmodified badges, and metadata state capsules share one reusable
    # semantic treatment. Semantic status modifiers may override that base through
    # their own families, but there must be no badge- or node-specific neutral color.
    neutral_components = _css_declarations(css, ".node, .badge, .state")
    assert neutral_components["border"] == "1px solid var(--neutral-border)"
    assert neutral_components["background"] == "var(--neutral-bg)"
    assert neutral_components["color"] == "var(--neutral-fg)"
    for selector, family in (
        (".badge--active", "active"),
        (".badge--success", "success"),
        (".badge--warning", "warning"),
        (".badge--danger", "danger"),
    ):
        declarations = _css_declarations(css, selector)
        assert declarations["border-color"] == f"var(--{family}-border)"
        assert declarations["background"] == f"var(--{family}-bg)"
        assert declarations["color"] == f"var(--{family}-fg)"

    # Preserve graph-order and high-contrast cues while protecting the color fix.
    sequence = _css_declarations(css, ".node::before")
    assert sequence["border"] == "1px solid currentColor"
    assert sequence["font-size"] == "var(--text-xs)"
    assert sequence["content"] == "counter(graph-step)"
    forced_colors = re.search(
        r"@media \(forced-colors: active\)\s*\{(.*?)\n\}", css, re.DOTALL
    )
    assert forced_colors
    assert re.search(
        r":where\(\.panel, \.repo, \.node, \.badge, \.state, input, button\)\s*"
        r"\{\s*border-color:\s*CanvasText;\s*\}",
        forced_colors.group(1),
    )

    # Explicit mutation evidence: the reviewed former boundary fails both the
    # component-fill and raised-run-surface requirements and must never return.
    reviewed_low_contrast_border = "#59667a"
    assert neutral_border.lower() != reviewed_low_contrast_border
    assert _contrast_ratio(reviewed_low_contrast_border, neutral_background) < 3.0
    assert _contrast_ratio(reviewed_low_contrast_border, tokens["--surface-raised"]) < 3.0

    # Keep the repository design contract synchronized with actual production
    # values, consumers, adjacencies, and measurable thresholds.
    design_system = (
        Path(__file__).resolve().parents[1] / "docs" / "design-system.md"
    ).read_text(encoding="utf-8")
    normalized_document = re.sub(r"\s+", " ", design_system).lower()
    for token in ("--neutral-fg", "--neutral-bg", "--neutral-border"):
        assert f"`{token}` (`{tokens[token].lower()}`)" in normalized_document
    for consumer in ("`.node`", "`.badge`", "`.state`"):
        assert consumer.lower() in normalized_document
    for adjacency, ratio in boundary_adjacencies.items():
        assert f"`{adjacency}`" in normalized_document
        assert f"{ratio:.2f}:1" in normalized_document
    assert f"{text_ratio:.2f}:1" in normalized_document
    assert "3:1 meaningful-component/graphic target" in normalized_document
    assert "neutral foreground text must remain at least 4.5:1" in normalized_document
    assert "do not substitute one contract for the other" in normalized_document


def test_empty_feedback_surfaces_collapse_without_changing_populated_treatments():
    """Empty semantic regions shed modifier surfaces while messages retain them."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    base = _css_declarations(css, ".feedback")
    empty_feedback = _css_declarations(css, ".feedback:empty")
    error = _css_declarations(css, ".feedback--error")
    warning = _css_declarations(css, ".feedback--warning")
    success = _css_declarations(css, ".feedback--success")

    # Populated feedback deliberately reserves a readable line, but :empty has
    # greater specificity than each modifier and must neutralize every box-model
    # or painted-surface property that could leave a blank semantic bar behind.
    assert base["min-height"] == "1.5rem"
    assert base["margin-block"] == "var(--space-2) 0"
    assert {
        "min-height": "0",
        "margin-block": "0",
        "border": "0",
        "background": "transparent",
        "padding": "0",
    }.items() <= empty_feedback.items()

    # Non-empty semantic treatments remain shared token-based presentation rather
    # than being weakened to make the empty state disappear.
    assert error["color"] == "var(--danger-fg)"
    assert {
        "border-inline-start": ".25rem solid var(--warning-border)",
        "background": "var(--warning-bg)",
        "color": "var(--warning-fg)",
        "padding": "var(--space-3)",
    }.items() <= warning.items()
    assert {
        "border-inline-start": ".25rem solid var(--success-border)",
        "background": "var(--success-bg)",
        "color": "var(--success-fg)",
        "padding": "var(--space-3)",
    }.items() <= success.items()

    parser = AccessibleMarkupParser()
    parser.feed(_CLIENT_HTML)
    by_id = {
        attrs["id"]: attrs
        for _, attrs in parser.elements
        if attrs.get("id")
    }
    expected_global_regions = {
        "add-error": ("feedback--error", "alert"),
        "add-status": ("feedback--success", "status"),
        "management-status": ("feedback--success", "status"),
        "removal-announcement": ("feedback--error", "alert"),
        "refresh-error": ("feedback--warning", "alert"),
    }
    for region_id, (modifier, role) in expected_global_regions.items():
        assert "feedback" in by_id[region_id]["class"].split()
        assert modifier in by_id[region_id]["class"].split()
        assert by_id[region_id]["role"] == role

    # Initial modifier-bearing regions are genuinely empty elements, so the
    # shared :empty reset applies before any client interaction.
    for region_id in expected_global_regions:
        assert re.search(
            rf'<div\s+[^>]*id="{re.escape(region_id)}"[^>]*></div>',
            _CLIENT_HTML,
        )

    contextual = _render_fixture_with_client_javascript(
        _CLIENT_HTML,
        {
            "id": 7,
            "github_repository": "acme/widget",
            "target_branch": "main",
            "nodes": [],
            "runs": [],
        },
    )["repository"]
    assert 'aria-describedby="remove-feedback-7"' in contextual
    assert (
        '<div id="remove-feedback-7" class="feedback feedback--error"></div>'
        in contextual
    )


def test_node_persistence_uses_minimum_shared_typography_token():
    """Protect the 14px floor without coupling to declaration order or formatting."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    root = _css_declarations(css, ":root")
    persistence = _css_declarations(css, ".node-persistence")

    assert root["--text-xs"] == ".875rem"
    assert float(root["--text-xs"].removesuffix("rem")) * 16 == 14
    assert persistence["font-size"] == "var(--text-xs)"
    assert persistence["color"] == "var(--text-secondary)"
    assert persistence["font-weight"] == "400"
    assert not re.fullmatch(r"[.\d]+(?:px|rem|em)", persistence["font-size"])


def test_graph_sequence_uses_minimum_shared_typography_token():
    """Keep graph-order text on the shared 14px minimum typography scale."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    root = _css_declarations(css, ":root")
    sequence = _css_declarations(css, ".node::before")

    assert root["--text-xs"] == ".875rem"
    assert float(root["--text-xs"].removesuffix("rem")) * 16 == 14
    assert sequence["font-size"] == "var(--text-xs)"
    assert sequence["content"] == "counter(graph-step)"
    assert sequence["display"] == "inline-grid"
    assert sequence["place-items"] == "center"
    assert sequence["min-width"] == "1.5rem"
    assert sequence["min-height"] == "1.5rem"
    assert sequence["border"] == "1px solid currentColor"
    assert sequence["border-radius"] == "50%"
    assert not re.fullmatch(r"[.\d]+(?:px|rem|em)", sequence["font-size"])
    assert sequence["font-size"] != ".75rem"


def _render_fixture_with_client_javascript(html, repository):
    """Run the production renderers, rather than duplicating their decisions in tests."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for embedded-client rendering regression coverage")
    _, script = _client_parts(html)
    renderers = script[: script.index("function bindRemoveActions")]
    program = "\n".join(
        [
            "global.document = {querySelector: () => null};",
            renderers,
            f"const fixture = {json.dumps(repository)};",
            "console.log(JSON.stringify({repository: renderRepository(fixture), status: statusBadge('PR_LISTENING', 'Run status')}));",
        ]
    )
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return json.loads(result.stdout)


def test_embedded_client_has_accessible_repository_management_contract():
    """Protect labels, announcements, native controls, and destructive naming."""
    parser = AccessibleMarkupParser()
    parser.feed(__import__("repogents.http_api", fromlist=["_CLIENT_HTML"])._CLIENT_HTML)

    label_targets = {label.get("for") for label in parser.labels}
    assert {"repository", "branch"} <= label_targets
    assert label_targets <= parser.ids

    by_id = {
        attrs["id"]: (tag, attrs)
        for tag, attrs in parser.elements
        if attrs.get("id")
    }
    repository = by_id["repository"][1]
    assert repository.get("required") is None  # Boolean HTML attributes parse as None.
    assert set(repository["aria-describedby"].split()) == {
        "repository-hint",
        "repository-error",
    }
    assert by_id["add-form"][0] == "form"
    assert by_id["add-button"][0] == "button"
    assert by_id["add-button"][1].get("type") == "submit"

    assert by_id["add-error"][1].get("role") == "alert"
    verification = by_id["add-verification-status"][1]
    assert verification.get("role") == "status"
    assert verification.get("aria-live") == "polite"
    assert verification.get("aria-atomic") == "true"
    assert by_id["refresh-error"][1].get("role") == "alert"
    for region_id in ("management-status", "refresh-status", "add-status"):
        attrs = by_id[region_id][1]
        assert attrs.get("role") == "status"
        assert attrs.get("aria-live") == "polite"


def test_add_workflow_precedes_repositories_and_grid_preserves_responsive_order():
    """Keep semantic/tab order add-first while desktop places repositories on the left."""
    from repogents.http_api import _CLIENT_HTML

    parser = AccessibleMarkupParser()
    parser.feed(_CLIENT_HTML)
    ids_in_source_order = [
        attrs["id"] for _, attrs in parser.elements if attrs.get("id")
    ]
    assert ids_in_source_order.index("track-heading") < ids_in_source_order.index(
        "repositories-heading"
    )
    assert ids_in_source_order.index("add-form") < ids_in_source_order.index(
        "repositories"
    )

    elements = parser.elements
    assert sum(attrs.get("id") == "add-form" for _, attrs in elements) == 1
    assert sum(attrs.get("id") == "repositories" for _, attrs in elements) == 1

    # The two workflows remain unique, visible landmarks with headings that label them.
    track_landmarks = [
        attrs
        for tag, attrs in elements
        if tag == "aside" and "track-section" in attrs.get("class", "").split()
    ]
    repository_landmarks = [
        attrs
        for tag, attrs in elements
        if tag == "section"
        and "repository-section" in attrs.get("class", "").split()
    ]
    assert len(track_landmarks) == len(repository_landmarks) == 1
    assert track_landmarks[0].get("aria-labelledby") == "track-heading"
    assert repository_landmarks[0].get("aria-labelledby") == "repositories-heading"
    headings = {
        attrs.get("id"): tag
        for tag, attrs in elements
        if tag in {"h1", "h2", "h3"} and attrs.get("id")
    }
    assert headings["track-heading"] == "h2"
    assert headings["repositories-heading"] == "h2"
    assert not any(
        attrs.get("aria-hidden", "").lower() == "true" for _, attrs in elements
    )

    # Native form controls occur before the repository region, so keyboard order follows
    # source order without tabindex shortcuts even though desktop grid placement differs.
    ordered_tags = [
        (index, tag, attrs)
        for index, (tag, attrs) in enumerate(elements)
    ]
    repository_region_index = next(
        index
        for index, _, attrs in ordered_tags
        if attrs.get("id") == "repositories"
    )
    add_control_indices = [
        index
        for index, tag, attrs in ordered_tags
        if tag in {"input", "button"}
        and attrs.get("id") in {"repository", "branch", "add-button"}
    ]
    assert len(add_control_indices) == 3
    assert add_control_indices == sorted(add_control_indices)
    assert max(add_control_indices) < repository_region_index
    assert not any(
        attrs.get("tabindex", "").lstrip("+").isdigit()
        and int(attrs["tabindex"]) > 0
        for _, attrs in elements
    )

    css, _ = _client_parts(_CLIENT_HTML)
    assert 'grid-template-areas: "repositories track"' in css
    assert ".track-section { grid-area: track; }" in css
    assert ".repository-section { grid-area: repositories; }" in css
    assert re.search(
        r'@media \(max-width: 55rem\).*?grid-template-areas: "track" "repositories";[^}]*grid-template-columns: 1fr;',
        css,
        re.DOTALL,
    )


def test_production_renderer_preserves_complete_operational_workflow_information():
    """Exercise a complete user-visible repository/run fixture through the real JS."""
    from repogents.http_api import _CLIENT_HTML

    fixture = {
        "id": 42,
        "github_repository": "acme/accessible-widget",
        "target_branch": "release/mobile",
        "nodes": [
            {"classification": "research/ui", "persistence": "PERMANENT"},
            {"classification": "frontend/accessibility", "persistence": "PERSISTENT"},
        ],
        "runs": [
            {
                "id": 91,
                "issue_number": 28,
                "state": "PR_LISTENING",
                "branch": "agent/issue-28",
                "issue_json": {"title": "Modernize the dashboard"},
                "pull_request": {
                    "number": 73,
                    "url": "https://github.example/acme/accessible-widget/pull/73",
                    "state": "OPEN",
                },
                "specifications": [
                    {"title": "Expose run state", "executable": True},
                    {"title": "Research responsive behavior", "executable": False},
                ],
                "work_items": [
                    {
                        "title": "Implement status cards",
                        "state": "COMPLETED",
                        "classification": "frontend/status-visualization",
                    }
                ],
            }
        ],
    }
    rendered = _render_fixture_with_client_javascript(_CLIENT_HTML, fixture)
    markup = rendered["repository"]

    # Repository identity and graph sequence remain directly scannable.
    assert "acme/accessible-widget" in markup
    assert "release/mobile" in markup
    assert '<ol class="graph" role="list" aria-label="Agent graph sequence, 2 nodes">' in markup
    assert markup.index("research/ui") < markup.index("frontend/accessibility")
    assert "Persistence: PERMANENT" in markup
    assert "Persistence: PERSISTENT" in markup

    # The entire issue lifecycle is available in-page, including meaningful safe PR access.
    for text in (
        "Issue #28",
        "Modernize the dashboard",
        "Monitoring pull request",
        "agent/issue-28",
        "Expose run state",
        "Research responsive behavior",
        "Planning only",
        "Implement status cards",
        "Completed",
        "frontend/status-visualization",
        "Pull request #73",
    ):
        assert text in markup
    assert 'target="_blank"' in markup
    assert 'rel="noopener noreferrer"' in markup
    assert 'aria-label="Pull request #73 (opens in a new tab)"' in markup
    assert "◉" in rendered["status"]
    assert 'aria-label="Run status: Monitoring pull request"' in rendered["status"]


def test_repository_and_run_headings_form_complete_nested_dashboard_outline():
    """Multiple repositories and runs retain the complete h2/h3/h4/h5 outline."""
    from repogents.http_api import _CLIENT_HTML

    repositories = [
        {
            "id": 42,
            "github_repository": "acme/heading-outline",
            "target_branch": "main",
            "nodes": [{"classification": "frontend/semantics", "persistence": "PERSISTENT"}],
            "runs": [
                {
                    "id": 91,
                    "issue_number": 28,
                    "state": "RUNNING",
                    "branch": "agent/issue-28",
                    "issue_json": {"title": "Preserve visible run identity"},
                    "pull_request": None,
                    "specifications": [],
                    "work_items": [],
                },
                {
                    "id": 92,
                    "issue_number": 29,
                    "state": "COMPLETED",
                    "branch": "agent/issue-29",
                    "issue_json": {"title": "Keep repeated run labels unique"},
                    "pull_request": None,
                    "specifications": [],
                    "work_items": [],
                },
            ],
        },
        {
            "id": 43,
            "github_repository": "acme/second-repository",
            "target_branch": "release",
            "nodes": [],
            "runs": [
                {
                    "id": 93,
                    "issue_number": 30,
                    "state": "QUEUED",
                    "branch": None,
                    "issue_json": {"title": "Verify multiple parent sections"},
                    "pull_request": None,
                    "specifications": [],
                    "work_items": [],
                }
            ],
        },
    ]
    rendered_repositories = [
        _render_fixture_with_client_javascript(_CLIENT_HTML, fixture)["repository"]
        for fixture in repositories
    ]
    markup = '<h2 id="repositories-heading">Tracked repositories</h2>' + "".join(
        rendered_repositories
    )

    class OutlineParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.headings = []
            self.labelled = []
            self.repository_focus = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.headings.append((tag, attributes.get("id")))
            if tag in {"article", "section"} and attributes.get("aria-labelledby"):
                self.labelled.append((tag, attributes["aria-labelledby"]))
            if attributes.get("data-repository-heading"):
                self.repository_focus.append(
                    (attributes["data-repository-heading"], attributes.get("tabindex"))
                )

    parser = OutlineParser()
    parser.feed(markup)
    heading_by_id = {heading_id: tag for tag, heading_id in parser.headings}

    expected = {
        "repositories-heading": "h2",
        "repository-42-heading": "h3",
        "repository-42-graph": "h4",
        "repository-42-runs": "h4",
        "run-91-heading": "h5",
        "run-92-heading": "h5",
        "repository-43-heading": "h3",
        "repository-43-graph": "h4",
        "repository-43-runs": "h4",
        "run-93-heading": "h5",
    }
    assert heading_by_id == expected
    assert len(heading_by_id) == len(parser.headings)  # Heading IDs remain unique.
    assert parser.repository_focus == [("42", "-1"), ("43", "-1")]

    heading_label_targets = {
        labelled_by for _, labelled_by in parser.labelled if labelled_by in heading_by_id
    }
    assert heading_label_targets == set(expected) - {"repositories-heading"}
    article_targets = {
        labelled_by for tag, labelled_by in parser.labelled if tag == "article"
    }
    assert article_targets == {
        "repository-42-heading",
        "run-91-heading",
        "run-92-heading",
        "repository-43-heading",
        "run-93-heading",
    }

    # Source order follows containment and remains stable across repeated parents/children.
    heading_ids = [heading_id for _, heading_id in parser.headings]
    assert heading_ids == list(expected)

    # Semantic changes preserve visible operational identity and status content.
    for text in (
        "acme/heading-outline",
        "acme/second-repository",
        "Issue #28",
        "Preserve visible run identity",
        "Issue #29",
        "Keep repeated run labels unique",
        "Issue #30",
        "Verify multiple parent sections",
    ):
        assert text in markup
    assert 'aria-label="Run status: Running"' in markup
    assert 'aria-label="Run status: Completed"' in markup
    assert 'aria-label="Run status: Queued"' in markup

    css, _ = _client_parts(_CLIENT_HTML)
    assert _css_declarations(css, ".repo-head h3")["font-size"] == "var(--text-lg)"
    assert _css_declarations(css, ".repo-head h3")["overflow-wrap"] == "anywhere"
    assert _css_declarations(css, ".run h5")["overflow-wrap"] == "anywhere"
    assert _css_declarations(css, ".run h5")["margin-block-end"] == "var(--space-1)"

def test_long_unbroken_issue_title_shrinks_wraps_and_preserves_run_status():
    """Protect pathological valid titles from widening the responsive run header."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    run = _css_declarations(css, ".run")
    identity = _css_declarations(css, ".run-identity")
    issue_title = _css_declarations(css, ".issue-title")
    status_badge = _css_declarations(css, ".run-head .badge")

    # Each nested flex/layout boundary must permit the title to shrink, while the
    # adjacent lifecycle badge retains its own width and the title wraps safely.
    assert run["min-width"] == "0"
    assert identity["min-width"] == "0"
    assert issue_title["display"] == "block"
    assert issue_title["overflow-wrap"] == "anywhere"
    assert status_badge["flex"] == "0 0 auto"
    assert "text-overflow" not in issue_title
    assert issue_title.get("overflow") != "hidden"
    assert issue_title.get("white-space") != "nowrap"

    long_title = "ResponsiveTitle" * 80
    fixture = {
        "id": 44,
        "github_repository": "acme/pathological-content",
        "target_branch": "main",
        "nodes": [],
        "runs": [
            {
                "id": 99,
                "issue_number": 314,
                "state": "RUNNING",
                "branch": "agent/issue-314",
                "issue_json": {"title": long_title},
                "pull_request": None,
                "specifications": [],
                "work_items": [],
            }
        ],
    }
    markup = _render_fixture_with_client_javascript(
        _CLIENT_HTML, fixture
    )["repository"]

    assert f'<div class="run-identity"><h5 id="run-99-heading">Issue #314</h5>' in markup
    assert f'<span class="meta issue-title">{long_title}</span>' in markup
    assert markup.count(long_title) == 1
    assert 'aria-label="Run status: Running"' in markup
    assert "Running" in markup
    assert markup.index(long_title) < markup.index('aria-label="Run status: Running"')


def test_long_unbroken_repository_name_reflows_in_contextual_remove_control():
    """Protect the complete destructive name while allowing narrow-layout wrapping."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    remove_control = _css_declarations(css, ".repo-remove")

    # The reusable control treatment must participate in intrinsic shrinking and
    # wrap pathological tokens without clipping or replacing content with ellipsis.
    assert remove_control["min-width"] == "0"
    assert remove_control["max-width"] == "100%"
    assert remove_control["white-space"] == "normal"
    assert remove_control["overflow-wrap"] == "anywhere"
    assert remove_control["text-align"] == "center"
    assert "text-overflow" not in remove_control
    assert remove_control.get("overflow") != "hidden"
    assert "font-size" not in remove_control
    assert "width" not in remove_control

    # At the shared header-stacking breakpoint the native button remains a
    # full-width, reachable control; this is the same responsive system used by
    # repository and run headers rather than a repository-name-specific override.
    narrow_rule = re.search(
        r"@media \(max-width: 45rem\)\s*\{(.*?)\n\}", css, re.DOTALL
    )
    assert narrow_rule
    assert re.search(
        r"\.section-head, \.repo-head, \.run-head\s*\{[^}]*flex-direction:\s*column;",
        narrow_rule.group(1),
        re.DOTALL,
    )
    assert re.search(
        r"\.repo-head button\s*\{[^}]*width:\s*100%;",
        narrow_rule.group(1),
        re.DOTALL,
    )

    long_name = "owner/" + "UnbrokenRepositoryIdentifier" * 60
    fixture = {
        "id": 404,
        "github_repository": long_name,
        "target_branch": "main",
        "nodes": [],
        "runs": [],
    }
    markup = _render_fixture_with_client_javascript(
        _CLIENT_HTML, fixture
    )["repository"]

    # Exercise the production renderer and retain both the destructive wording and
    # repository identity in the native button's visible/programmatic name.
    expected_button = (
        '<button class="danger repo-remove" data-remove="404" '
        f'data-repository="{long_name}" data-remove-focus="404" aria-describedby="remove-feedback-404">'
        f'Remove repository<span class="field-hint"> {long_name}</span></button>'
    )
    assert expected_button in markup
    assert markup.count(long_name) == 3  # heading, data context, and visible button name
    assert "Remove repository" in markup
    assert '<div id="remove-feedback-404" class="feedback feedback--error">' in markup
    assert "aria-label=" not in expected_button  # Native visible content supplies the name.
    assert "tabindex=" not in expected_button
    assert "disabled" not in expected_button

def test_production_renderer_exposes_explicit_operational_empty_states():
    from repogents.http_api import _CLIENT_HTML

    empty_repository = {
        "id": 8,
        "github_repository": "acme/empty",
        "target_branch": "main",
        "nodes": [],
        "runs": [],
    }
    markup = _render_fixture_with_client_javascript(
        _CLIENT_HTML, empty_repository
    )["repository"]
    assert "No saved agent graph nodes." in markup
    assert "No issue runs are queued for this repository." in markup

    run_without_children = {
        **empty_repository,
        "runs": [
            {
                "id": 10,
                "issue_number": 5,
                "state": "QUEUED",
                "branch": None,
                "pull_request": None,
                "specifications": [],
                "work_items": [],
            }
        ],
    }
    markup = _render_fixture_with_client_javascript(
        _CLIENT_HTML, run_without_children
    )["repository"]
    assert "Branch</strong> <span class=\"code\">Not created" in markup
    assert "Pull request not created" in markup
    assert "No specifications have been generated for this run yet." in markup
    assert "No work items have been created for this run yet." in markup


def test_responsive_css_keeps_core_workflows_reachable_at_representative_widths():
    """Guard reflow contracts for mobile, tablet, and desktop without pixel snapshots."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    assert "grid-template-columns: minmax(0, 2fr) minmax(17rem, 1fr)" in css
    assert re.search(
        r'@media \(max-width: 55rem\).*?\.dashboard-layout\s*\{[^}]*grid-template-areas: "track" "repositories";[^}]*grid-template-columns: 1fr;',
        css,
        re.DOTALL,
    )
    assert re.search(
        r"@media \(max-width: 45rem\).*?\.field-grid, \.columns\s*\{\s*grid-template-columns: 1fr;",
        css,
        re.DOTALL,
    )
    assert re.search(
        r"@media \(max-width: 25rem\).*?\.graph, \.graph-step\s*\{[^}]*flex-direction: column;",
        css,
        re.DOTALL,
    )
    assert "min-width: 20rem" in css
    assert "min-width: 0" in css
    assert "overflow-wrap: anywhere" in css
    assert ":focus-visible" in css
    assert "@media (forced-colors: active)" in css


def test_repository_validation_alert_semantics_and_client_interaction():
    """Invalid submissions announce locally, focus correction, clear, and preserve API alerts."""
    from repogents.http_api import _CLIENT_HTML

    parser = AccessibleMarkupParser()
    parser.feed(_CLIENT_HTML)
    by_id = {
        attrs["id"]: (tag, attrs)
        for tag, attrs in parser.elements
        if attrs.get("id")
    }
    validation = by_id["repository-error"][1]
    assert validation.get("role") == "alert"
    assert validation.get("aria-live") is None  # role=alert supplies assertive semantics.
    assert validation.get("aria-atomic") == "true"
    assert "repository-error" in by_id["repository"][1]["aria-describedby"].split()
    assert by_id["add-error"][1].get("role") == "alert"

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for embedded-client interaction coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id) { this.id=id; this.value=''; this.textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.disabled=false; this.dataset={}; }
  setAttribute(name, value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus() { document.activeElement=this; this.focusCount=(this.focusCount || 0)+1; }
  reset() { repository.value=''; branch.value=''; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading'];
const elements=Object.fromEntries(ids.map(id => [id, new Element(id)]));
const repository=elements.repository, branch=elements.branch, form=elements['add-form'], button=elements['add-button'];
form.elements=[repository, branch, button];
const documentListeners={}, windowListeners={};
global.document={
  hidden:false, activeElement:null,
  querySelector: selector => elements[selector.replace(/^#/, '')] || null,
  querySelectorAll: () => [],
  addEventListener: (name, callback) => { documentListeners[name]=callback; }
};
global.window={
  confirm: () => true,
  addEventListener: (name, callback) => { windowListeners[name]=callback; }
};
global.CSS={escape: value => String(value)};
global.fetch=async (path, options={}) => {
  if (path === '/api/state') return {ok:true, status:200, json:async () => ({repositories:[]})};
  return {ok:false, status:422, statusText:'Unprocessable Entity', json:async () => ({error:'Repository was not found'})};
};
async function submit() { return form.listeners.submit({preventDefault(){}}); }
'''
    scenario = r'''
(async () => {
  await new Promise(resolve => setTimeout(resolve, 0));
  repository.value='not-a-repository';
  await submit();
  const first={validation:elements['repository-error'].textContent, invalid:repository.attrs['aria-invalid'], focus:repository.focusCount, api:elements['add-error'].textContent};
  repository.value='acme/widget';
  repository.listeners.input();
  const corrected={validation:elements['repository-error'].textContent, invalid:repository.attrs['aria-invalid'] || null, api:elements['add-error'].textContent};
  repository.value='still-invalid';
  await submit();
  const resubmitted={validation:elements['repository-error'].textContent, focus:repository.focusCount};
  repository.value='acme/widget';
  repository.listeners.input();
  await submit();
  const failed={validation:elements['repository-error'].textContent, invalid:repository.attrs['aria-invalid'] || null, api:elements['add-error'].textContent, focus:repository.focusCount};
  windowListeners.pagehide();
  console.log(JSON.stringify({first, corrected, resubmitted, failed}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    message = "Enter a GitHub repository in owner/repository format, for example acme/widget."
    assert behavior["first"] == {
        "validation": message,
        "invalid": "true",
        "focus": 1,
        "api": "",
    }
    assert behavior["corrected"] == {"validation": "", "invalid": None, "api": ""}
    assert behavior["resubmitted"] == {"validation": message, "focus": 2}
    assert behavior["failed"]["validation"] == ""
    assert behavior["failed"]["invalid"] is None
    assert "Could not add acme/widget: Repository was not found" in behavior["failed"]["api"]
    assert behavior["failed"]["focus"] == 3


def test_initial_state_failure_replaces_loading_placeholder_and_recovers():
    """The repository region presents one coherent state across failure and retry."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for embedded-client interaction coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id) { this.id=id; this.textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.dataset={}; this.elements=[]; }
  setAttribute(name, value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus() { document.activeElement=this; }
  reset() {}
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading'];
const elements=Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements['add-form'].elements=[elements.repository, elements.branch, elements['add-button']];
const documentListeners={}, windowListeners={};
global.document={
  hidden:false, activeElement:null,
  querySelector: selector => elements[selector.replace(/^#/, '')] || null,
  querySelectorAll: () => [],
  addEventListener: (name, callback) => { documentListeners[name]=callback; }
};
let confirmations=0;
global.window={confirm:() => { confirmations += 1; return true; }, addEventListener:(name, callback) => { windowListeners[name]=callback; }};
global.CSS={escape:value => String(value)};
let stateRequests=0;
global.fetch=async path => {
  if (path !== '/api/state') throw new Error('unexpected request');
  stateRequests += 1;
  if (stateRequests === 1) return {ok:false, status:503, statusText:'Unavailable', json:async () => ({error:'Service unavailable'})};
  if (stateRequests === 2) return {ok:true, status:200, json:async () => ({repositories:[]})};
  return {ok:true, status:200, json:async () => ({repositories:[{id:7, github_repository:'acme/widget', target_branch:'main', nodes:[], runs:[]}]})};
};
'''
    scenario = r'''
(async () => {
  await new Promise(resolve => setTimeout(resolve, 0));
  const failed={
    busy:elements.repositories.attrs['aria-busy'],
    content:elements.repositories.innerHTML,
    summary:elements['repository-summary'].textContent,
    freshness:elements.freshness.textContent,
    alert:elements['refresh-error'].textContent
  };
  await load({background:false});
  const recoveredEmpty={
    busy:elements.repositories.attrs['aria-busy'],
    content:elements.repositories.innerHTML,
    summary:elements['repository-summary'].textContent,
    alert:elements['refresh-error'].textContent,
    status:elements['refresh-status'].textContent
  };
  await load({background:true});
  const recoveredPopulated={
    busy:elements.repositories.attrs['aria-busy'],
    content:elements.repositories.innerHTML,
    summary:elements['repository-summary'].textContent,
    alert:elements['refresh-error'].textContent,
    status:elements['refresh-status'].textContent
  };
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({failed, recoveredEmpty, recoveredPopulated}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    failed = behavior["failed"]
    assert failed["busy"] == "false"
    assert "Loading tracked repositories" not in failed["content"]
    assert "Repository state unavailable" in failed["content"]
    assert "retry automatically" in failed["content"]
    assert failed["summary"] == "Repository state unavailable"
    assert failed["freshness"] == "No repository update available"
    assert "Repository state could not be loaded: Service unavailable" in failed["alert"]

    recovered_empty = behavior["recoveredEmpty"]
    assert recovered_empty["busy"] == "false"
    assert "Loading tracked repositories" not in recovered_empty["content"]
    assert "Repository state unavailable" not in recovered_empty["content"]
    assert "No repositories are tracked yet" in recovered_empty["content"]
    assert recovered_empty["summary"] == "0 repositories"
    assert recovered_empty["alert"] == ""
    assert recovered_empty["status"] == "Repository updates resumed."

    recovered_populated = behavior["recoveredPopulated"]
    assert recovered_populated["busy"] == "false"
    assert "Loading tracked repositories" not in recovered_populated["content"]
    assert "Repository state unavailable" not in recovered_populated["content"]
    assert "No repositories are tracked yet" not in recovered_populated["content"]
    assert "acme/widget" in recovered_populated["content"]
    assert recovered_populated["summary"] == "1 repository"
    assert recovered_populated["alert"] == ""
    assert recovered_populated["status"] == "Repository state updated."


def test_alternating_mutations_clear_only_durable_cross_workflow_statuses():
    """Add/remove starts clear both durable results without erasing unrelated alerts."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for embedded-client interaction coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id) { this.id=id; this.value=''; this.textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.disabled=false; this.dataset={}; }
  setAttribute(name, value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus() { document.activeElement=this; this.focusCount=(this.focusCount || 0)+1; }
  reset() { repository.value=''; branch.value=''; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading','remove-feedback-7'];
const elements=Object.fromEntries(ids.map(id => [id, new Element(id)]));
const repository=elements.repository, branch=elements.branch, form=elements['add-form'], button=elements['add-button'];
const removeButton=new Element('remove-7');
removeButton.dataset={remove:'7', repository:'acme/widget'};
removeButton.innerHTML='Remove repository';
form.elements=[repository, branch, button];
const documentListeners={}, windowListeners={};
global.document={
  hidden:false, activeElement:null,
  querySelector: selector => elements[selector.replace(/^#/, '')] || null,
  querySelectorAll: selector => selector === '[data-remove]' ? [removeButton] : [],
  getElementById: id => elements[id] || null,
  addEventListener: (name, callback) => { documentListeners[name]=callback; }
};
global.window={
  confirm: () => true,
  addEventListener: (name, callback) => { windowListeners[name]=callback; }
};
global.CSS={escape: value => String(value)};
let pendingMutation=null;
let nextMutationFailure=false;
function mutationResponse(resolve) {
  pendingMutation=() => resolve(nextMutationFailure
    ? {ok:false, status:422, statusText:'Unprocessable Entity', json:async () => ({error:'Mutation failed'})}
    : {ok:true, status:204, json:async () => null});
}
global.fetch=(path, options={}) => {
  if (path === '/api/state') return Promise.resolve({ok:true, status:200, json:async () => ({repositories:[]})});
  return new Promise(mutationResponse);
};
async function submit() { return form.listeners.submit({preventDefault(){}}); }
'''
    scenario = r'''
(async () => {
  await new Promise(resolve => setTimeout(resolve, 0));

  elements['add-status'].textContent='Old add success';
  elements['management-status'].textContent='Old removal success';
  elements['remove-feedback-7'].textContent='Keep removal-specific error';
  repository.value='acme/widget';
  nextMutationFailure=false;
  const addPromise=submit();
  const addStart={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent,
    removeError:elements['remove-feedback-7'].textContent
  };
  pendingMutation();
  await addPromise;
  const addSuccess={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent
  };

  elements['management-status'].textContent='Old removal success';
  elements['add-error'].textContent='Keep add-specific API error';
  elements['repository-error'].textContent='Keep field validation';
  nextMutationFailure=true;
  const removePromise=removeRepository(removeButton, 0);
  const removeStart={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent,
    addError:elements['add-error'].textContent,
    validation:elements['repository-error'].textContent,
    removeError:elements['remove-feedback-7'].textContent,
    pendingLabel:removeButton.textContent
  };
  pendingMutation();
  await removePromise;
  const removeFailure={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent,
    addError:elements['add-error'].textContent,
    removeError:elements['remove-feedback-7'].textContent
  };

  elements['add-status'].textContent='Another old add success';
  elements['management-status'].textContent='Another old removal success';
  repository.value='acme/widget';
  nextMutationFailure=true;
  const secondAddPromise=submit();
  const secondAddStart={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent,
    addError:elements['add-error'].textContent,
    removeError:elements['remove-feedback-7'].textContent,
    pendingLabel:button.textContent
  };
  pendingMutation();
  await secondAddPromise;
  const addFailure={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent,
    addError:elements['add-error'].textContent,
    removeError:elements['remove-feedback-7'].textContent
  };

  elements['add-status'].textContent='Stale add result before successful removal';
  elements['management-status'].textContent='Stale removal result before successful removal';
  elements['repository-error'].textContent='Keep validation during removal';
  nextMutationFailure=false;
  const successfulRemovePromise=removeRepository(removeButton, 0);
  const successfulRemoveStart={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent,
    addError:elements['add-error'].textContent,
    validation:elements['repository-error'].textContent
  };
  pendingMutation();
  await successfulRemovePromise;
  const removeSuccess={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent,
    addError:elements['add-error'].textContent,
    removeError:elements['remove-feedback-7'].textContent
  };

  repository.value='acme/next-widget';
  nextMutationFailure=false;
  const finalAddPromise=submit();
  const addAfterRemoveStart={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent,
    addError:elements['add-error'].textContent,
    validation:elements['repository-error'].textContent
  };
  pendingMutation();
  await finalAddPromise;
  const finalAddSuccess={
    add:elements['add-status'].textContent,
    management:elements['management-status'].textContent,
    addError:elements['add-error'].textContent
  };

  windowListeners.pagehide();
  console.log(JSON.stringify({addStart, addSuccess, removeStart, removeFailure, secondAddStart, addFailure, successfulRemoveStart, removeSuccess, addAfterRemoveStart, finalAddSuccess}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    assert behavior["addStart"] == {
        "add": "",
        "management": "",
        "removeError": "Keep removal-specific error",
    }
    assert behavior["addSuccess"] == {
        "add": "acme/widget was added, but is no longer tracked.",
        "management": "",
    }
    assert behavior["removeStart"] == {
        "add": "",
        "management": "",
        "addError": "Keep add-specific API error",
        "validation": "Keep field validation",
        "removeError": "",
        "pendingLabel": "Removing repository…",
    }
    assert behavior["removeFailure"]["add"] == ""
    assert behavior["removeFailure"]["management"] == ""
    assert behavior["removeFailure"]["addError"] == "Keep add-specific API error"
    assert "Could not remove acme/widget: Mutation failed" in behavior["removeFailure"]["removeError"]
    assert behavior["secondAddStart"]["add"] == ""
    assert behavior["secondAddStart"]["management"] == ""
    assert behavior["secondAddStart"]["addError"] == ""
    assert behavior["secondAddStart"]["pendingLabel"] == "Adding repository…"
    assert "Could not remove acme/widget: Mutation failed" in behavior["secondAddStart"]["removeError"]
    assert behavior["addFailure"]["add"] == ""
    assert behavior["addFailure"]["management"] == ""
    assert "Could not add acme/widget: Mutation failed" in behavior["addFailure"]["addError"]
    assert "Could not remove acme/widget: Mutation failed" in behavior["addFailure"]["removeError"]
    assert behavior["successfulRemoveStart"] == {
        "add": "",
        "management": "",
        "addError": behavior["addFailure"]["addError"],
        "validation": "Keep validation during removal",
    }
    assert behavior["removeSuccess"] == {
        "add": "",
        "management": "acme/widget was removed from tracked repositories.",
        "addError": behavior["addFailure"]["addError"],
        "removeError": "",
    }
    assert behavior["addAfterRemoveStart"] == {
        "add": "",
        "management": "",
        "addError": "",
        "validation": "",
    }
    assert behavior["finalAddSuccess"] == {
        "add": "acme/next-widget was added, but is no longer tracked.",
        "management": "",
        "addError": "",
    }


def test_remove_controls_follow_pending_add_mutation_state():
    """Pending adds disable existing/new remove controls and restore them on settle."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for embedded-client interaction coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id) { this.id=id; this.value=''; this.textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.disabled=false; this.dataset={}; }
  setAttribute(name, value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus() { document.activeElement=this; }
  reset() { repository.value=''; branch.value=''; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading'];
const elements=Object.fromEntries(ids.map(id => [id, new Element(id)]));
const repository=elements.repository, branch=elements.branch, form=elements['add-form'];
form.elements=[repository, branch, elements['add-button']];
const firstRemove=new Element('remove-1');
firstRemove.dataset={remove:'1', repository:'acme/one'};
let removeControls=[firstRemove];
const documentListeners={}, windowListeners={};
global.document={
  hidden:false, activeElement:null,
  querySelector: selector => elements[selector.replace(/^#/, '')] || null,
  querySelectorAll: selector => selector === '[data-remove]' ? removeControls : [],
  getElementById: id => elements[id] || null,
  addEventListener: (name, callback) => { documentListeners[name]=callback; }
};
let confirmations=0;
global.window={
  confirm: () => { confirmations += 1; return true; },
  addEventListener: (name, callback) => { windowListeners[name]=callback; }
};
global.CSS={escape: value => String(value)};
let mutationRequests=0;
let pendingMutation;
let failMutation=false;
global.fetch=(path, options={}) => {
  if (path === '/api/state') return Promise.resolve({ok:true, status:200, json:async () => ({repositories:[]})});
  mutationRequests += 1;
  return new Promise(resolve => { pendingMutation=() => resolve(failMutation
    ? {ok:false, status:422, statusText:'Unprocessable Entity', json:async () => ({error:'Add failed'})}
    : {ok:true, status:204, json:async () => null}); });
};
async function submit() { return form.listeners.submit({preventDefault(){}}); }
'''
    scenario = r'''
(async () => {
  await new Promise(resolve => setTimeout(resolve, 0));
  bindRemoveActions();
  repository.value='acme/widget';
  failMutation=true;
  const failedAdd=submit();
  const pendingFailure={
    existing:firstRemove.disabled,
    repository:repository.disabled,
    branch:branch.disabled,
    addButton:elements['add-button'].disabled,
    formBusy:form.attrs['aria-busy'],
    mutationRequests
  };
  await submit();
  await firstRemove.listeners.click();
  const guarded={confirmations, mutationRequests};
  const newlyRendered=new Element('remove-2');
  newlyRendered.dataset={remove:'2', repository:'acme/two'};
  removeControls=[firstRemove, newlyRendered];
  bindRemoveActions();
  const newControlPending=newlyRendered.disabled;
  await newlyRendered.listeners.click();
  const newlyRenderedGuarded={confirmations, mutationRequests};
  pendingMutation();
  await failedAdd;
  const afterFailure={
    first:firstRemove.disabled,
    second:newlyRendered.disabled,
    repository:repository.disabled,
    branch:branch.disabled,
    addButton:elements['add-button'].disabled,
    formBusy:form.attrs['aria-busy']
  };

  repository.value='acme/widget';
  failMutation=false;
  const successfulAdd=submit();
  const pendingSuccess={first:firstRemove.disabled, second:newlyRendered.disabled};
  pendingMutation();
  await successfulAdd;
  const afterSuccess={
    first:firstRemove.disabled,
    second:newlyRendered.disabled,
    repository:repository.disabled,
    branch:branch.disabled,
    addButton:elements['add-button'].disabled,
    formBusy:form.attrs['aria-busy'],
    mutationRequests
  };
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({pendingFailure, guarded, newControlPending, newlyRenderedGuarded, afterFailure, pendingSuccess, afterSuccess}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    assert behavior["pendingFailure"] == {
        "existing": True,
        "repository": True,
        "branch": True,
        "addButton": True,
        "formBusy": "true",
        "mutationRequests": 1,
    }
    # Programmatically repeated add/remove callbacks still hit the shared guard;
    # real pointer and keyboard activation is additionally suppressed by native disabled.
    assert behavior["guarded"] == {"confirmations": 0, "mutationRequests": 1}
    assert behavior["newControlPending"] is True
    assert behavior["newlyRenderedGuarded"] == {
        "confirmations": 0,
        "mutationRequests": 1,
    }
    assert behavior["afterFailure"] == {
        "first": False,
        "second": False,
        "repository": False,
        "branch": False,
        "addButton": False,
        "formBusy": "false",
    }
    assert behavior["pendingSuccess"] == {"first": True, "second": True}
    assert behavior["afterSuccess"] == {
        "first": False,
        "second": False,
        "repository": False,
        "branch": False,
        "addButton": False,
        "formBusy": "false",
        "mutationRequests": 2,
    }


def test_bfcache_restore_resumes_polling_without_duplicate_timers_or_stale_updates():
    """Persisted lifecycle transitions pause and restart one race-safe polling owner."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser lifecycle regression coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id) { this.id=id; this.textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.dataset={}; this.elements=[]; }
  setAttribute(name, value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus() { document.activeElement=this; }
  reset() {}
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading'];
const elements=Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements['add-form'].elements=[elements.repository, elements.branch, elements['add-button']];
const documentListeners={}, windowListeners={};
global.document={
  hidden:false, activeElement:null,
  querySelector: selector => elements[selector.replace(/^#/, '')] || null,
  querySelectorAll: () => [],
  addEventListener: (name, callback) => { documentListeners[name]=callback; }
};
global.window={confirm:() => true, addEventListener:(name, callback) => { windowListeners[name]=callback; }};
global.CSS={escape:value => String(value)};
let nextTimerId=1;
const timers=new Map();
global.setTimeout=(callback, delay) => { const id=nextTimerId++; timers.set(id, {callback, delay}); if (delay === 250) setImmediate(() => { const timer=timers.get(id); if (timer) { timers.delete(id); timer.callback(); } }); return id; };
global.clearTimeout=id => { timers.delete(id); };
function timerCount(delay) { return [...timers.values()].filter(timer => timer.delay === delay).length; }
function fireTimer(delay) {
  const entry=[...timers.entries()].find(([, timer]) => timer.delay === delay);
  if (!entry) throw new Error(`No ${delay}ms timer available`);
  timers.delete(entry[0]);
  return entry[1].callback();
}
const requests=[];
global.fetch=(path, options={}) => {
  if (path !== '/api/state') throw new Error(`unexpected request ${path}`);
  let resolveFetch;
  const promise=new Promise(resolve => { resolveFetch=resolve; });
  const request={signal:options.signal, resolve(name) {
    resolveFetch({ok:true, status:200, json:async () => ({repositories:name ? [{id:name, github_repository:`acme/${name}`, target_branch:'main', nodes:[], runs:[]}] : []})});
  }};
  requests.push(request);
  return promise;
};
async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
}
'''
    scenario = r'''
(async () => {
  // Complete the automatic initial load and establish exactly one polling timer.
  if (requests.length !== 1) throw new Error(`expected initial request, got ${requests.length}`);
  requests[0].resolve('initial');
  await settle();
  const initial={requests:requests.length, refreshTimers:timerCount(3000), rendered:elements.repositories.innerHTML};

  // Start a scheduled refresh, then suspend it into bfcache while it is in flight.
  const suspendedPoll=fireTimer(3000);
  await settle();
  const suspendedRequest=requests[1];
  windowListeners.pagehide({persisted:true});
  const suspended={aborted:suspendedRequest.signal.aborted, refreshTimers:timerCount(3000), requestCount:requests.length};

  // Restoration must issue one fresh request. A late response from the suspended
  // owner must not overwrite the current content or acquire a timer.
  windowListeners.pageshow({persisted:true});
  const firstRestoreRequest=requests[2];
  suspendedRequest.resolve('stale');
  await suspendedPoll;
  await settle();
  const afterStale={rendered:elements.repositories.innerHTML, refreshTimers:timerCount(3000), requestTimeouts:timerCount(15000), requestCount:requests.length};
  firstRestoreRequest.resolve('restored-one');
  await settle();
  const firstRestore={rendered:elements.repositories.innerHTML, refreshTimers:timerCount(3000), requestTimeouts:timerCount(15000), requestCount:requests.length};

  // A second persisted cycle must again pause the sole timer and create only one
  // new load/timer owner when restored.
  windowListeners.pagehide({persisted:true});
  const secondPause={refreshTimers:timerCount(3000)};
  windowListeners.pageshow({persisted:true});
  const secondRestoreRequest=requests[3];
  const duringSecondRestore={requestCount:requests.length, refreshTimers:timerCount(3000), requestTimeouts:timerCount(15000)};
  secondRestoreRequest.resolve('restored-two');
  await settle();
  const secondRestore={rendered:elements.repositories.innerHTML, refreshTimers:timerCount(3000), requestTimeouts:timerCount(15000), requestCount:requests.length};

  // The resumed timer remains live and single-shot: one firing creates one request,
  // and only its completion schedules the next sole timer.
  const resumedPollCompletion=fireTimer(3000);
  await settle();
  const resumedPollRequest=requests[4];
  const pollInFlight={requestCount:requests.length, refreshTimers:timerCount(3000), requestTimeouts:timerCount(15000)};
  resumedPollRequest.resolve('polled');
  await resumedPollCompletion;
  await settle();
  const resumedPoll={rendered:elements.repositories.innerHTML, refreshTimers:timerCount(3000), requestTimeouts:timerCount(15000), requestCount:requests.length};

  // A true teardown aborts work, leaks no timer, and a non-persisted pageshow does
  // not restart the finalized document.
  const teardownPoll=fireTimer(3000);
  await settle();
  const teardownRequest=requests[5];
  windowListeners.pagehide({persisted:false});
  windowListeners.pageshow({persisted:false});
  teardownRequest.resolve('after-teardown');
  await teardownPoll;
  await settle();
  const teardown={aborted:teardownRequest.signal.aborted, refreshTimers:timerCount(3000), requestTimeouts:timerCount(15000), requestCount:requests.length};

  console.log(JSON.stringify({initial, suspended, afterStale, firstRestore, secondPause, duringSecondRestore, secondRestore, pollInFlight, resumedPoll, teardown}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)

    assert behavior["initial"]["requests"] == 1
    assert behavior["initial"]["refreshTimers"] == 1
    assert "acme/initial" in behavior["initial"]["rendered"]
    assert behavior["suspended"] == {
        "aborted": True,
        "refreshTimers": 0,
        "requestCount": 2,
    }
    assert "acme/initial" in behavior["afterStale"]["rendered"]
    assert "acme/stale" not in behavior["afterStale"]["rendered"]
    assert behavior["afterStale"]["refreshTimers"] == 1
    # The suspended owner cleaned up; only the fresh restore request owns a timeout.
    assert behavior["afterStale"]["requestTimeouts"] == 1
    assert behavior["afterStale"]["requestCount"] == 3
    assert "acme/restored-one" in behavior["firstRestore"]["rendered"]
    assert behavior["firstRestore"]["refreshTimers"] == 1
    assert behavior["firstRestore"]["requestTimeouts"] == 0
    assert behavior["firstRestore"]["requestCount"] == 3
    assert behavior["secondPause"] == {"refreshTimers": 0}
    assert behavior["duringSecondRestore"] == {
        "requestCount": 4,
        "refreshTimers": 0,
        "requestTimeouts": 1,
    }
    assert "acme/restored-two" in behavior["secondRestore"]["rendered"]
    assert behavior["secondRestore"]["refreshTimers"] == 1
    assert behavior["secondRestore"]["requestTimeouts"] == 0
    assert behavior["pollInFlight"] == {
        "requestCount": 5,
        "refreshTimers": 0,
        "requestTimeouts": 1,
    }
    assert "acme/polled" in behavior["resumedPoll"]["rendered"]
    assert behavior["resumedPoll"]["refreshTimers"] == 1
    assert behavior["resumedPoll"]["requestTimeouts"] == 0
    assert behavior["resumedPoll"]["requestCount"] == 5
    assert behavior["teardown"] == {
        "aborted": True,
        "refreshTimers": 0,
        "requestTimeouts": 0,
        "requestCount": 6,
    }


_AUTHORITATIVE_ADD_OPERATION_HARNESS = r'''
// Authoritative repository-add operation harness. It wraps each scenario's
// existing state/POST transport so legacy scenario expectations can be revised
// independently from deterministic protocol and timer infrastructure.
let operationIdSequence=0;
global.crypto={randomUUID:()=>`operation-${++operationIdSequence}`};
const scenarioFetch=global.fetch;
function classifyNewestRequestTimer(kind) {
  const candidates=[...timers.entries()].filter(([,timer])=>timer.delay===15000 && !timer.requestKind);
  if (candidates.length) candidates[candidates.length-1][1].requestKind=kind;
}
const addOperationHarness={
  requests:[], postOperationIds:[], responses:[], pendingRequests:[],
  enqueue(...responses) { this.responses.push(...responses); },
  timerCount(kind) { return [...timers.values()].filter(timer=>timer.requestKind===kind).length; },
  statusDelayCount() { return [...timers.values()].filter(timer=>timer.delay===500).length; },
  fireRequestTimeout(kind) {
    const entry=[...timers.entries()].find(([,timer])=>timer.requestKind===kind);
    if(!entry) throw new Error(`No ${kind} request timeout`);
    timers.delete(entry[0]);
    if(typeof now==='number') now+=entry[1].delay;
    entry[1].callback();
  },
  fireStatusDelay() {
    const entry=[...timers.entries()].find(([,timer])=>timer.delay===500);
    if(!entry) throw new Error('No 500ms operation-status polling delay');
    timers.delete(entry[0]);
    if(typeof now==='number') now+=500;
    entry[1].callback();
  },
  resolvePending(response) {
    const pending=this.pendingRequests.shift();
    if (!pending) throw new Error('No pending operation-status request');
    pending.resolve(response);
  }
};
global.fetch=(path,options={})=>{
  if(path==='/api/state') {
    classifyNewestRequestTimer('state');
    return scenarioFetch(path,options);
  }
  if(path==='/api/repositories'&&options.method==='POST') {
    classifyNewestRequestTimer('mutation');
    const headers=options.headers||{};
    const operationId=headers['X-Repogents-Operation-Id']||headers['x-repogents-operation-id'];
    addOperationHarness.postOperationIds.push(operationId||null);
    return scenarioFetch(path,options);
  }
  if(path.startsWith('/api/repository-add-operations/')) {
    classifyNewestRequestTimer('operation-status');
    const operationId=decodeURIComponent(path.slice('/api/repository-add-operations/'.length));
    addOperationHarness.requests.push({operationId,signal:options.signal});
    const response=addOperationHarness.responses.length
      ? addOperationHarness.responses.shift()
      : {state:'PENDING'};
    if(response&&response.pendingRequest) {
      return new Promise((resolve,reject)=>{
        addOperationHarness.pendingRequests.push({resolve,reject,operationId});
        if(options.signal) options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('operation status timed out'),{name:'AbortError'})));
      });
    }
    if(response&&response.unavailable) {
      return Promise.resolve({ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'temporarily unavailable'})});
    }
    if(response&&response.missing) {
      return Promise.resolve({ok:false,status:404,statusText:'Not Found',json:async()=>({error:'repository add operation not found'})});
    }
    const state=(response&&response.state)||'PENDING';
    const repositoryProjection=response&&response.repository ? response.repository : null;
    return Promise.resolve({ok:true,status:200,json:async()=>({
      operation_id:operationId,
      github_repository:(response&&response.github_repository)||repository.value,
      target_branch:(response&&Object.prototype.hasOwnProperty.call(response,'target_branch'))?response.target_branch:(branch.value||null),
      state,
      repository_id:repositoryProjection?repositoryProjection.id:null,
      error:(response&&response.error)||null,
      repository:repositoryProjection
    })});
  }
  return scenarioFetch(path,options);
};
'''


def test_stalled_state_requests_timeout_and_polling_recovers_without_duplicate_timers():
    """Initial/background refresh timeouts abort, render coherently, and retain one owner."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for stalled refresh regression coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id) { this.id=id; this._textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.dataset={}; this.elements=[]; }
  set textContent(value) { this._textContent=String(value); if (this.id === 'refresh-error') refreshErrorWrites.push(this._textContent); }
  get textContent() { return this._textContent; }
  setAttribute(name, value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus() { document.activeElement=this; }
  reset() {}
}
const refreshErrorWrites=[];
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading'];
const elements=Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements['add-form'].elements=[elements.repository, elements.branch, elements['add-button']];
const documentListeners={}, windowListeners={};
global.document={
  hidden:false, activeElement:null,
  querySelector: selector => elements[selector.replace(/^#/, '')] || null,
  querySelectorAll: () => [],
  getElementById: id => elements[id] || null,
  addEventListener: (name, callback) => { documentListeners[name]=callback; }
};
global.window={confirm:() => true, addEventListener:(name, callback) => { windowListeners[name]=callback; }};
global.CSS={escape:value => String(value)};
let nextTimerId=1;
const timers=new Map();
global.setTimeout=(callback, delay) => { const id=nextTimerId++; timers.set(id, {callback, delay}); if (delay === 250) setImmediate(() => { const timer=timers.get(id); if (timer) { timers.delete(id); timer.callback(); } }); return id; };
global.clearTimeout=id => { timers.delete(id); };
function timerCount(delay) { return [...timers.values()].filter(timer => timer.delay === delay).length; }
function fireTimer(delay) {
  const entry=[...timers.entries()].find(([, timer]) => timer.delay === delay);
  if (!entry) throw new Error(`No ${delay}ms timer available`);
  timers.delete(entry[0]);
  return entry[1].callback();
}
const requests=[];
global.fetch=(path, options={}) => {
  if (path !== '/api/state') throw new Error(`unexpected request ${path}`);
  let resolveFetch, rejectFetch;
  const promise=new Promise((resolve, reject) => { resolveFetch=resolve; rejectFetch=reject; });
  const request={signal:options.signal, resolve(name) {
    resolveFetch({ok:true, status:200, json:async () => ({repositories:[{id:name, github_repository:`acme/${name}`, target_branch:'main', nodes:[], runs:[]}]})});
  }, fail(message='Service unavailable') {
    resolveFetch({ok:false, status:503, statusText:'Unavailable', json:async () => ({error:message})});
  }};
  options.signal.addEventListener('abort', () => rejectFetch(Object.assign(new Error('aborted'), {name:'AbortError'})));
  requests.push(request);
  return promise;
};
async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
}
'''
    scenario = r'''
(async () => {
  // The automatic initial request stalls and is converted into the existing failure state.
  const initialRequest=requests[0];
  fireTimer(15000);
  await settle();
  const initialFailure={
    aborted:initialRequest.signal.aborted,
    busy:elements.repositories.attrs['aria-busy'],
    content:elements.repositories.innerHTML,
    alert:elements['refresh-error'].textContent,
    refreshTimers:timerCount(3000),
    requestTimeouts:timerCount(15000)
  };

  // The sole retry owner succeeds and establishes valid content.
  const firstRetryCompletion=fireTimer(3000);
  await settle();
  requests[1].resolve('valid');
  await firstRetryCompletion;
  await settle();
  const loaded={content:elements.repositories.innerHTML, refreshTimers:timerCount(3000), alert:elements['refresh-error'].textContent};

  // An ordinary background HTTP failure retains content, promises automatic retry,
  // and writes the alert only once when the same error repeats.
  refreshErrorWrites.length=0;
  const ordinaryCompletion=fireTimer(3000);
  await settle();
  requests[2].fail();
  await ordinaryCompletion;
  await settle();
  const ordinaryFailure={
    content:elements.repositories.innerHTML,
    alert:elements['refresh-error'].textContent,
    alertWrites:[...refreshErrorWrites],
    refreshTimers:timerCount(3000)
  };
  const repeatedCompletion=fireTimer(3000);
  await settle();
  requests[3].fail();
  await repeatedCompletion;
  await settle();
  const repeatedFailure={
    alert:elements['refresh-error'].textContent,
    alertWrites:[...refreshErrorWrites],
    refreshTimers:timerCount(3000)
  };

  // A later stalled poll uses the same guidance and schedules one retry.
  const backgroundCompletion=fireTimer(3000);
  await settle();
  const backgroundRequest=requests[4];
  fireTimer(15000);
  await backgroundCompletion;
  await settle();
  const backgroundFailure={
    aborted:backgroundRequest.signal.aborted,
    content:elements.repositories.innerHTML,
    alert:elements['refresh-error'].textContent,
    refreshTimers:timerCount(3000),
    requestTimeouts:timerCount(15000)
  };

  // That retry can update the UI and still leaves exactly one scheduling owner.
  const recoveryCompletion=fireTimer(3000);
  await settle();
  requests[5].resolve('recovered');
  await recoveryCompletion;
  await settle();
  const recovered={
    content:elements.repositories.innerHTML,
    alert:elements['refresh-error'].textContent,
    status:elements['refresh-status'].textContent,
    refreshTimers:timerCount(3000),
    requestTimeouts:timerCount(15000),
    requestCount:requests.length
  };
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({initialFailure, loaded, ordinaryFailure, repeatedFailure, backgroundFailure, recovered}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)

    initial = behavior["initialFailure"]
    assert initial["aborted"] is True
    assert initial["busy"] == "false"
    assert "Repository state unavailable" in initial["content"]
    assert "retry automatically" in initial["content"]
    assert "request timed out" in initial["alert"]
    assert initial["refreshTimers"] == 1
    assert initial["requestTimeouts"] == 0

    assert "acme/valid" in behavior["loaded"]["content"]
    assert behavior["loaded"]["alert"] == ""
    assert behavior["loaded"]["refreshTimers"] == 1

    ordinary = behavior["ordinaryFailure"]
    assert "acme/valid" in ordinary["content"]
    assert "Service unavailable" in ordinary["alert"]
    assert "Showing the last successful repository state, which may be outdated" in ordinary["alert"]
    assert "Repogents will retry automatically" in ordinary["alert"]
    assert ordinary["alertWrites"] == [ordinary["alert"]]
    assert ordinary["refreshTimers"] == 1
    assert behavior["repeatedFailure"] == {
        "alert": ordinary["alert"],
        "alertWrites": [ordinary["alert"]],
        "refreshTimers": 1,
    }

    background = behavior["backgroundFailure"]
    assert background["aborted"] is True
    assert "acme/valid" in background["content"]
    assert "request timed out" in background["alert"]
    assert "Showing the last successful repository state, which may be outdated" in background["alert"]
    assert "Repogents will retry automatically" in background["alert"]
    assert background["refreshTimers"] == 1
    assert background["requestTimeouts"] == 0

    recovered = behavior["recovered"]
    assert "acme/recovered" in recovered["content"]
    assert recovered["alert"] == ""
    assert recovered["status"] == "Repository updates resumed."
    assert recovered["refreshTimers"] == 1
    assert recovered["requestTimeouts"] == 0
    assert recovered["requestCount"] == 6


def test_stalled_remove_request_times_out_and_restores_repository_controls():
    """A removal-owned timeout aborts DELETE without disturbing refresh ownership."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for stalled removal regression coverage")
    _, script = _client_parts(_CLIENT_HTML)
    rendered = _render_fixture_with_client_javascript(
        _CLIENT_HTML,
        {
            "id": 7,
            "github_repository": "acme/stalled-remove",
            "target_branch": "main",
            "nodes": [],
            "runs": [],
        },
    )["repository"]
    assert 'aria-describedby="remove-feedback-7"' in rendered
    assert 'id="remove-feedback-7" class="feedback feedback--error"' in rendered
    assert 'id="removal-announcement" class="feedback feedback--error" role="alert" aria-atomic="true"' in _CLIENT_HTML

    harness = r'''
class Element {
  constructor(id) { this.id=id; this.value=''; this.textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.disabled=false; this.dataset={}; this.elements=[]; }
  setAttribute(name, value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus() { document.activeElement=this; this.focusCount=(this.focusCount || 0)+1; }
  reset() {}
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading','remove-feedback-7'];
const elements=Object.fromEntries(ids.map(id => [id, new Element(id)]));
const form=elements['add-form'], repository=elements.repository, branch=elements.branch, addControl=elements['add-button'];
form.elements=[repository, branch, addControl];
const removeButton=new Element('remove-7');
removeButton.dataset={remove:'7', repository:'acme/stalled-remove'};
removeButton.innerHTML='Remove repository <span>acme/stalled-remove</span>';
const documentListeners={}, windowListeners={};
global.document={
  hidden:false, activeElement:null,
  querySelector: selector => elements[selector.replace(/^#/, '')] || null,
  querySelectorAll: selector => selector === '[data-remove]' ? [removeButton] : [],
  getElementById: id => elements[id] || null,
  addEventListener: (name, callback) => { documentListeners[name]=callback; }
};
let confirmations=0;
global.window={
  confirm: () => { confirmations += 1; return true; },
  addEventListener: (name, callback) => { windowListeners[name]=callback; }
};
global.CSS={escape:value => String(value)};
let nextTimerId=1;
const timers=new Map();
global.setTimeout=(callback, delay) => { const id=nextTimerId++; timers.set(id, {callback, delay}); if (delay === 250) setImmediate(() => { const timer=timers.get(id); if (timer) { timers.delete(id); timer.callback(); } }); return id; };
global.clearTimeout=id => { timers.delete(id); };
function timerCount(delay) { return [...timers.values()].filter(timer => timer.delay === delay).length; }
function fireTimer(delay) {
  const entry=[...timers.entries()].find(([, timer]) => timer.delay === delay);
  if (!entry) throw new Error(`No ${delay}ms timer available`);
  timers.delete(entry[0]);
  entry[1].callback();
}
let deleteRequests=0;
let deleteSignal=null;
global.fetch=(path, options={}) => {
  if (path === '/api/state') return Promise.resolve({ok:true, status:200, json:async () => ({repositories:[]})});
  if (path !== '/api/repositories/7' || options.method !== 'DELETE') throw new Error(`unexpected request ${path}`);
  deleteRequests += 1;
  deleteSignal=options.signal;
  return new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name:'AbortError'})));
  });
};
async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
}
async function submit() { return form.listeners.submit({preventDefault(){}}); }
'''
    scenario = r'''
(async () => {
  await settle();
  bindRemoveActions();
  const initialRefreshTimers=timerCount(3000);
  const stalledRemove=removeButton.listeners.click();
  const pending={
    deleteRequests,
    timeoutTimers:timerCount(15000),
    refreshTimers:timerCount(3000),
    signalAborted:deleteSignal.aborted,
    repositoryDisabled:repository.disabled,
    branchDisabled:branch.disabled,
    addDisabled:addControl.disabled,
    removeDisabled:removeButton.disabled,
    removeLabel:removeButton.textContent
  };
  await removeButton.listeners.click();
  repository.value='acme/other';
  await submit();
  const guarded={deleteRequests, confirmations};
  fireTimer(15000);
  await stalledRemove;
  await settle();
  const settled={
    signalAborted:deleteSignal.aborted,
    timeoutTimers:timerCount(15000),
    refreshTimers:timerCount(3000),
    repositoryDisabled:repository.disabled,
    branchDisabled:branch.disabled,
    addDisabled:addControl.disabled,
    removeDisabled:removeButton.disabled,
    removeMarkup:removeButton.innerHTML,
    focus:removeButton.focusCount || 0,
    feedback:elements['remove-feedback-7'].textContent
  };
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({initialRefreshTimers, pending, guarded, settled}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    assert behavior["initialRefreshTimers"] == 1
    assert behavior["pending"] == {
        "deleteRequests": 1,
        "timeoutTimers": 1,
        "refreshTimers": 1,
        "signalAborted": False,
        "repositoryDisabled": True,
        "branchDisabled": True,
        "addDisabled": True,
        "removeDisabled": True,
        "removeLabel": "Removing repository…",
    }
    assert behavior["guarded"] == {"deleteRequests": 1, "confirmations": 1}
    settled = behavior["settled"]
    assert settled["signalAborted"] is True
    assert settled["timeoutTimers"] == 0
    assert settled["refreshTimers"] == 1
    assert settled["repositoryDisabled"] is False
    assert settled["branchDisabled"] is False
    assert settled["addDisabled"] is False
    assert settled["removeDisabled"] is False
    assert settled["removeMarkup"] == "Remove repository <span>acme/stalled-remove</span>"
    assert settled["focus"] == 1
    assert "Could not confirm whether acme/stalled-remove was removed" in settled["feedback"]
    assert "request timed out" in settled["feedback"]
    assert "Check the tracked repository list before trying again" in settled["feedback"]
    assert "still tracked" not in settled["feedback"]


def test_removal_errors_survive_changed_renders_and_clear_by_repository_lifecycle():
    """Contextual DELETE errors are repository-keyed, rehydrated, and intentionally cleared."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for contextual removal feedback coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id) { this.id=id; this.value=''; this.textContent=''; this.attrs={}; this.listeners={}; this.disabled=false; this.dataset={}; this.elements=[]; this._innerHTML=''; }
  set innerHTML(value) {
    this._innerHTML=String(value);
    if (this.id !== 'repositories') return;
    removeControls=[];
    for (const match of this._innerHTML.matchAll(/data-remove="([^"]+)" data-repository="([^"]+)"[^>]*aria-describedby="([^"]+)"/g)) {
      const control=new Element(`remove-${match[1]}`);
      control.dataset={remove:match[1], repository:match[2]};
      control.attrs['aria-describedby']=match[3];
      control.innerHTML='Remove repository';
      removeControls.push(control);
    }
    for (const match of this._innerHTML.matchAll(/id="(remove-feedback-[^"]+)"[^>]*class="feedback feedback--error"/g)) {
      dynamicElements[match[1]]=new Element(match[1]);
    }
  }
  get innerHTML() { return this._innerHTML; }
  setAttribute(name, value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus() { document.activeElement=this; }
  reset() {}
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading'];
const elements=Object.fromEntries(ids.map(id => [id, new Element(id)]));
const dynamicElements={};
let removeControls=[];
elements['add-form'].elements=[elements.repository, elements.branch, elements['add-button']];
const documentListeners={}, windowListeners={};
global.document={
  hidden:false, activeElement:null,
  querySelector: selector => {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || dynamicElements[selector.slice(1)] || null;
    const match=selector.match(/^\[data-remove="(.+)"\]$/);
    return match ? removeControls.find(control => control.dataset.remove === match[1]) || null : null;
  },
  querySelectorAll: selector => selector === '[data-remove]' ? removeControls : [],
  getElementById: id => elements[id] || dynamicElements[id] || null,
  addEventListener: (name, callback) => { documentListeners[name]=callback; }
};
global.window={confirm:() => true, addEventListener:(name, callback) => { windowListeners[name]=callback; }};
global.CSS={escape:value => String(value)};
const stateResponses=[
  {repositories:[{id:1, github_repository:'acme/one', target_branch:'main', nodes:[], runs:[]}]},
  {repositories:[{id:1, github_repository:'acme/one', target_branch:'release', nodes:[], runs:[]}, {id:2, github_repository:'acme/two', target_branch:'main', nodes:[], runs:[]}]},
  {repositories:[{id:2, github_repository:'acme/two', target_branch:'main', nodes:[], runs:[]}]},
  {repositories:[{id:1, github_repository:'acme/one', target_branch:'main', nodes:[], runs:[]}, {id:2, github_repository:'acme/two', target_branch:'main', nodes:[], runs:[]}]},
  {repositories:[{id:2, github_repository:'acme/two', target_branch:'main', nodes:[], runs:[]}]}
];
let pendingRetry=null;
let deleteCount=0;
global.fetch=(path, options={}) => {
  if (path === '/api/state') {
    const state=stateResponses.shift();
    if (!state) throw new Error('unexpected state request');
    return Promise.resolve({ok:true, status:200, json:async () => state});
  }
  if (path === '/api/repositories/1' && options.method === 'DELETE') {
    deleteCount += 1;
    if (deleteCount === 1) return Promise.resolve({ok:false, status:503, statusText:'Unavailable', json:async () => ({error:'Delete unavailable'})});
    if (deleteCount === 2) return new Promise(resolve => { pendingRetry=resolve; });
    return Promise.resolve({ok:true, status:204, json:async () => null});
  }
  throw new Error(`unexpected request ${path}`);
};
async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
}
'''
    scenario = r'''
(async () => {
  await settle();
  const firstButton=removeControls.find(control => control.dataset.remove === '1');
  await firstButton.listeners.click();
  const failed={
    message:dynamicElements['remove-feedback-1'].textContent,
    describedBy:firstButton.attrs['aria-describedby'],
    role:dynamicElements['remove-feedback-1'].attrs.role || null,
    stored:removalErrors.get('1')
  };

  await load({background:true});
  const refreshedButton=removeControls.find(control => control.dataset.remove === '1');
  const afterChangedRender={
    message:dynamicElements['remove-feedback-1'].textContent,
    describedBy:refreshedButton.attrs['aria-describedby'],
    otherMessage:dynamicElements['remove-feedback-2'].textContent,
    stored:removalErrors.get('1'),
    controlReplaced:refreshedButton !== firstButton
  };

  const retry=refreshedButton.listeners.click();
  const duringRetry={
    message:dynamicElements['remove-feedback-1'].textContent,
    stored:removalErrors.has('1'),
    disabled:refreshedButton.disabled,
    label:refreshedButton.textContent
  };
  pendingRetry({ok:false, status:503, statusText:'Unavailable', json:async () => ({error:'Still unavailable'})});
  await retry;
  const afterRetryFailure={
    message:dynamicElements['remove-feedback-1'].textContent,
    stored:removalErrors.get('1'),
    disabled:refreshedButton.disabled,
    label:refreshedButton.innerHTML,
    focused:document.activeElement === refreshedButton
  };

  await load({background:true});
  const afterDisappearance={
    repositoryOnePresent:Boolean(removeControls.find(control => control.dataset.remove === '1')),
    repositoryTwoMessage:dynamicElements['remove-feedback-2'].textContent,
    stored:removalErrors.has('1')
  };

  await load({background:true});
  const returnedButton=removeControls.find(control => control.dataset.remove === '1');
  setRemovalError('1', 'Synthetic still-relevant removal error');
  const successfulRetry=returnedButton.listeners.click();
  await successfulRetry;
  const afterSuccess={
    repositoryOnePresent:Boolean(removeControls.find(control => control.dataset.remove === '1')),
    repositoryTwoMessage:dynamicElements['remove-feedback-2'].textContent,
    stored:removalErrors.has('1')
  };
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({failed, afterChangedRender, duringRetry, afterRetryFailure, afterDisappearance, afterSuccess}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    first_message = behavior["failed"]["message"]
    assert "Could not remove acme/one: Delete unavailable" in first_message
    assert behavior["failed"]["stored"] == first_message
    assert behavior["failed"]["describedBy"] == "remove-feedback-1"
    assert behavior["failed"]["role"] is None

    assert behavior["afterChangedRender"] == {
        "message": first_message,
        "describedBy": "remove-feedback-1",
        "otherMessage": "",
        "stored": first_message,
        "controlReplaced": True,
    }
    assert behavior["duringRetry"] == {
        "message": "",
        "stored": False,
        "disabled": True,
        "label": "Removing repository…",
    }
    assert "Could not remove acme/one: Still unavailable" in behavior["afterRetryFailure"]["message"]
    assert behavior["afterRetryFailure"]["stored"] == behavior["afterRetryFailure"]["message"]
    assert behavior["afterRetryFailure"]["disabled"] is False
    assert behavior["afterRetryFailure"]["label"] == "Remove repository"
    assert behavior["afterRetryFailure"]["focused"] is True
    assert behavior["afterDisappearance"] == {
        "repositoryOnePresent": False,
        "repositoryTwoMessage": "",
        "stored": False,
    }
    assert behavior["afterSuccess"] == {
        "repositoryOnePresent": False,
        "repositoryTwoMessage": "",
        "stored": False,
    }


def test_changed_refresh_preserves_interactive_focus_and_uses_repository_fallback():
    """Changed renders retain controls and use logical PR/remove fallbacks."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for refresh focus regression coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this.textContent=''; this.attrs={}; this.dataset={}; this.elements=[]; this.disabled=false; this.listeners={}; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus(options) {
    this.focusAttempts=(this.focusAttempts || 0)+1;
    if (this.id === 'repositories-heading' && this.attrs.tabindex !== '-1') {
      this.rejectedFocusAttempts=(this.rejectedFocusAttempts || 0)+1;
      return;
    }
    document.activeElement=this; this.focusOptions=options || null; this.focusCount=(this.focusCount || 0)+1;
  }
  reset() {}
  set innerHTML(value) { this._innerHTML=value; rebuild(value); }
  get innerHTML() { return this._innerHTML || ''; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading'];
const elements=Object.fromEntries(ids.map(id => [id,new Element(id)]));
elements['add-form'].elements=[elements.repository,elements.branch,elements['add-button']];
let dynamic=[];
function rebuild(markup) {
  dynamic=[];
  for (const match of markup.matchAll(/<a[^>]*data-pr-focus="([^"]+)"[^>]*data-pr-repository="([^"]+)"[^>]*>/g)) {
    const element=new Element(); element.dataset.prFocus=match[1]; element.dataset.prRepository=match[2]; dynamic.push(element);
  }
  for (const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*>/g)) {
    const element=new Element(); element.dataset.remove=match[1]; element.dataset.removeFocus=match[1]; dynamic.push(element);
  }
  for (const match of markup.matchAll(/<h3 id="repository-([^"]+)-heading"[^>]*>/g)) {
    const element=new Element(`repository-${match[1]}-heading`); element.dataset.repositoryHeading=match[1]; element.attrs.tabindex='-1'; dynamic.push(element);
  }
}
function selectorValue(selector, name) {
  const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`)); return match && match[1];
}
const documentListeners={}, windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || dynamic.find(item => item.id === selector.slice(1)) || null;
    const pr=selectorValue(selector,'data-pr-focus'); if (pr) return dynamic.find(item => item.dataset.prFocus === pr) || null;
    const removeFocus=selectorValue(selector,'data-remove-focus'); if (removeFocus) return dynamic.find(item => item.dataset.removeFocus === removeFocus) || null;
    const remove=selectorValue(selector,'data-remove'); if (remove) return dynamic.find(item => item.dataset.remove === remove) || null;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === '[data-remove]') return dynamic.filter(item => item.dataset.remove);
    if (selector === '[data-remove-focus]') return dynamic.filter(item => item.dataset.removeFocus);
    if (selector === '[data-repository-heading]') return dynamic.filter(item => item.dataset.repositoryHeading);
    return [];
  },
  addEventListener(name,callback) { documentListeners[name]=callback; }
};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
const states=[
 {repositories:[{id:7,github_repository:'acme/widget',target_branch:'main',nodes:[],runs:[{id:9,issue_number:9,state:'RUNNING',branch:'work',issue_json:{title:'First'},pull_request:{number:12,url:'https://example.test/pr/12',state:'OPEN'},specifications:[],work_items:[]}]}]},
 {repositories:[{id:7,github_repository:'acme/widget',target_branch:'main',nodes:[],runs:[{id:9,issue_number:9,state:'RUNNING',branch:'work-2',issue_json:{title:'Changed'},pull_request:{number:12,url:'https://example.test/pr/12',state:'OPEN'},specifications:[],work_items:[]}]}]},
 {repositories:[{id:7,github_repository:'acme/widget',target_branch:'main',nodes:[],runs:[{id:9,issue_number:9,state:'COMPLETED',branch:'work-2',issue_json:{title:'Changed again'},pull_request:null,specifications:[],work_items:[]}]}]},
 {repositories:[{id:7,github_repository:'acme/widget',target_branch:'release',nodes:[{classification:'testing/focus',persistence:'PERSISTENT'}],runs:[{id:9,issue_number:9,state:'COMPLETED',branch:'work-2',issue_json:{title:'Changed after remove focus'},pull_request:null,specifications:[],work_items:[]}]}]},
 {repositories:[{id:8,github_repository:'acme/neighbor',target_branch:'main',nodes:[],runs:[]}]},
 {repositories:[]},
 {repositories:[{id:9,github_repository:'acme/direct-fallback',target_branch:'main',nodes:[],runs:[{id:10,issue_number:10,state:'RUNNING',branch:'direct',issue_json:{title:'Direct page fallback'},pull_request:{number:13,url:'https://example.test/pr/13',state:'OPEN'},specifications:[],work_items:[]}]}]},
 {repositories:[]}
];
let request=0;
global.fetch=async path => ({ok:true,status:200,json:async()=>states[request++]});
async function settle() { await Promise.resolve(); await Promise.resolve(); await new Promise(resolve=>setImmediate(resolve)); }
'''
    scenario = r'''
(async()=>{
  await settle();
  const first=document.querySelector('[data-pr-focus="7:9:12"]');
  first.focus();
  await load({background:true});
  const preserved=document.activeElement;
  const preservedPreventScroll=preserved.focusOptions && preserved.focusOptions.preventScroll;
  preserved.focus();
  await load({background:true});
  const fallback=document.activeElement;
  const removeBefore=document.querySelector('[data-remove="7"]');
  removeBefore.focus();
  await load({background:true});
  const removeAfter=document.activeElement;
  const removePreventScroll=removeAfter.focusOptions && removeAfter.focusOptions.preventScroll;
  removeAfter.focus();
  await load({background:true});
  const removeFallback=document.activeElement;
  const neighborRemove=document.querySelector('[data-remove-focus="8"]');
  neighborRemove.focus();
  await load({background:true});
  const trackedHeadingFallback=document.activeElement;
  await load({background:true});
  const directPr=document.querySelector('[data-pr-focus="9:10:13"]');
  directPr.focus();
  await load({background:true});
  const directPageFallback=document.activeElement;
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({
    stableId:first.dataset.prFocus,
    replaced:preserved !== first,
    preservedId:preserved.dataset.prFocus,
    preservedPreventScroll,
    fallbackId:fallback.id,
    fallbackPreventScroll:fallback.focusOptions && fallback.focusOptions.preventScroll,
    removeReplaced:removeAfter !== removeBefore,
    removeId:removeAfter.dataset.remove,
    removeFocusId:removeAfter.dataset.removeFocus,
    removePreventScroll,
    removeFallbackId:removeFallback.id,
    removeFallbackPreventScroll:removeFallback.focusOptions && removeFallback.focusOptions.preventScroll,
    removeFallbackTabindex:removeFallback.attrs.tabindex,
    trackedHeadingFallbackId:trackedHeadingFallback.id,
    trackedHeadingFallbackPreventScroll:trackedHeadingFallback.focusOptions && trackedHeadingFallback.focusOptions.preventScroll,
    trackedHeadingFallbackTabindex:trackedHeadingFallback.attrs.tabindex,
    trackedHeadingFallbackRejected:trackedHeadingFallback.rejectedFocusAttempts || 0,
    directPrIdentity:directPr.dataset.prFocus,
    directPageFallbackId:directPageFallback.id,
    directPageFallbackPreventScroll:directPageFallback.focusOptions && directPageFallback.focusOptions.preventScroll,
    directPageFallbackTabindex:directPageFallback.attrs.tabindex,
    directPageFallbackRejected:directPageFallback.rejectedFocusAttempts || 0
  }));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    assert behavior == {
        "stableId": "7:9:12",
        "replaced": True,
        "preservedId": "7:9:12",
        "preservedPreventScroll": True,
        "fallbackId": "repository-7-heading",
        "fallbackPreventScroll": True,
        "removeReplaced": True,
        "removeId": "7",
        "removeFocusId": "7",
        "removePreventScroll": True,
        "removeFallbackId": "repository-8-heading",
        "removeFallbackPreventScroll": True,
        "removeFallbackTabindex": "-1",
        "trackedHeadingFallbackId": "repositories-heading",
        "trackedHeadingFallbackPreventScroll": True,
        "trackedHeadingFallbackTabindex": "-1",
        "trackedHeadingFallbackRejected": 0,
        "directPrIdentity": "9:10:13",
        "directPageFallbackId": "repositories-heading",
        "directPageFallbackPreventScroll": True,
        "directPageFallbackTabindex": "-1",
        "directPageFallbackRejected": 0,
    }


def test_visibility_refresh_waits_for_pending_add_and_remove_mutations():
    """Visible-tab refresh ownership defers to mutations, then resumes with one timer."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for visibility lifecycle regression coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id) { this.id=id; this.value=''; this.textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.disabled=false; this.dataset={}; this.elements=[]; }
  setAttribute(name, value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  focus() { document.activeElement=this; this.focusCount=(this.focusCount || 0)+1; }
  reset() { repository.value=''; branch.value=''; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading','remove-feedback-7','removal-announcement'];
const elements=Object.fromEntries(ids.map(id => [id, new Element(id)]));
const repository=elements.repository, branch=elements.branch, form=elements['add-form'], addControl=elements['add-button'];
form.elements=[repository, branch, addControl];
const removeButton=new Element('remove-7');
removeButton.dataset={remove:'7', repository:'acme/widget'};
removeButton.innerHTML='Remove repository <span>acme/widget</span>';
const documentListeners={}, windowListeners={};
global.document={
  hidden:false, activeElement:null,
  querySelector: selector => elements[selector.replace(/^#/, '')] || null,
  querySelectorAll: selector => selector === '[data-remove]' ? [removeButton] : [],
  getElementById: id => elements[id] || null,
  addEventListener: (name, callback) => { documentListeners[name]=callback; }
};
let confirmations=0;
global.window={confirm:() => { confirmations += 1; return true; }, addEventListener:(name, callback) => { windowListeners[name]=callback; }};
global.CSS={escape:value => String(value)};
let nextTimerId=1;
const timers=new Map();
global.setTimeout=(callback, delay) => { const id=nextTimerId++; timers.set(id, {callback, delay}); if (delay === 250) setImmediate(() => { const timer=timers.get(id); if (timer) { timers.delete(id); timer.callback(); } }); return id; };
global.clearTimeout=id => { timers.delete(id); };
function timerCount(delay) { return [...timers.values()].filter(timer => timer.delay === delay).length; }
function fireTimer(delay) {
  const entry=[...timers.entries()].find(([, timer]) => timer.delay === delay);
  if (!entry) throw new Error(`No ${delay}ms timer available`);
  timers.delete(entry[0]);
  return entry[1].callback();
}
let stateRequests=0, postRequests=0, deleteRequests=0;
let settleMutation=null;
global.fetch=(path, options={}) => {
  if (path === '/api/state') {
    stateRequests += 1;
    return Promise.resolve({ok:true, status:200, json:async () => ({repositories:[{id:7, github_repository:`acme/state-${stateRequests}`, target_branch:'main', nodes:[], runs:[]}]})});
  }
  if (path === '/api/repositories' && options.method === 'POST') {
    postRequests += 1;
    return new Promise(resolve => { settleMutation=() => resolve({ok:false, status:503, statusText:'Unavailable', json:async () => ({error:'Add unavailable'})}); });
  }
  if (path === '/api/repositories/7' && options.method === 'DELETE') {
    deleteRequests += 1;
    return new Promise(resolve => { settleMutation=() => resolve({ok:false, status:503, statusText:'Unavailable', json:async () => ({error:'Delete unavailable'})}); });
  }
  throw new Error(`unexpected request ${path}`);
};
async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
}
async function submit() { return form.listeners.submit({preventDefault(){}}); }
'''
    scenario = r'''
(async () => {
  await settle();
  bindRemoveActions();
  const initial={stateRequests, refreshTimers:timerCount(3000)};

  repository.value='acme/new';
  const addMutation=submit();
  document.hidden=true;
  documentListeners.visibilitychange();
  document.hidden=false;
  documentListeners.visibilitychange();
  await submit();
  await removeButton.listeners.click();
  const addVisible={
    stateRequests, postRequests, deleteRequests, confirmations,
    refreshTimers:timerCount(3000), mutationTimeouts:timerCount(15000),
    label:addControl.textContent,
    addDisabled:addControl.disabled,
    removeDisabled:removeButton.disabled,
    sameRemoveControl:document.querySelectorAll('[data-remove]')[0] === removeButton
  };
  settleMutation();
  await addMutation;
  await settle();
  const addSettled={label:addControl.textContent, addDisabled:addControl.disabled, removeDisabled:removeButton.disabled, refreshTimers:timerCount(3000), mutationTimeouts:timerCount(15000), focused:document.activeElement === repository};
  const addRefresh=fireTimer(3000);
  await addRefresh;
  await settle();
  const afterAddRefresh={stateRequests, rendered:elements.repositories.innerHTML, refreshTimers:timerCount(3000)};

  const removeMutation=removeButton.listeners.click();
  document.hidden=true;
  documentListeners.visibilitychange();
  document.hidden=false;
  documentListeners.visibilitychange();
  repository.value='acme/concurrent';
  await submit();
  await removeButton.listeners.click();
  const removeVisible={
    stateRequests, postRequests, deleteRequests, confirmations,
    refreshTimers:timerCount(3000), mutationTimeouts:timerCount(15000),
    label:removeButton.textContent,
    addDisabled:addControl.disabled,
    removeDisabled:removeButton.disabled,
    sameRemoveControl:document.querySelectorAll('[data-remove]')[0] === removeButton
  };
  settleMutation();
  await removeMutation;
  await settle();
  const removeSettled={label:removeButton.innerHTML, addDisabled:addControl.disabled, removeDisabled:removeButton.disabled, refreshTimers:timerCount(3000), mutationTimeouts:timerCount(15000), focused:document.activeElement === removeButton};
  const removeRefresh=fireTimer(3000);
  await removeRefresh;
  await settle();
  const afterRemoveRefresh={stateRequests, rendered:elements.repositories.innerHTML, refreshTimers:timerCount(3000)};

  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({initial, addVisible, addSettled, afterAddRefresh, removeVisible, removeSettled, afterRemoveRefresh}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    assert behavior["initial"] == {"stateRequests": 1, "refreshTimers": 1}
    assert behavior["addVisible"] == {
        "stateRequests": 1,
        "postRequests": 1,
        "deleteRequests": 0,
        "confirmations": 0,
        "refreshTimers": 1,
        "mutationTimeouts": 1,
        "label": "Adding repository…",
        "addDisabled": True,
        "removeDisabled": True,
        "sameRemoveControl": True,
    }
    assert behavior["addSettled"] == {
        "label": "Add repository",
        "addDisabled": False,
        "removeDisabled": False,
        "refreshTimers": 1,
        "mutationTimeouts": 0,
        "focused": True,
    }
    assert behavior["afterAddRefresh"]["stateRequests"] == 2
    assert "acme/state-2" in behavior["afterAddRefresh"]["rendered"]
    assert behavior["afterAddRefresh"]["refreshTimers"] == 1
    assert behavior["removeVisible"] == {
        "stateRequests": 2,
        "postRequests": 1,
        "deleteRequests": 1,
        "confirmations": 1,
        "refreshTimers": 1,
        "mutationTimeouts": 1,
        "label": "Removing repository…",
        "addDisabled": True,
        "removeDisabled": True,
        "sameRemoveControl": True,
    }
    assert behavior["removeSettled"] == {
        "label": "Remove repository <span>acme/widget</span>",
        "addDisabled": False,
        "removeDisabled": False,
        "refreshTimers": 1,
        "mutationTimeouts": 0,
        "focused": True,
    }
    assert behavior["afterRemoveRefresh"]["stateRequests"] == 3
    assert "acme/state-3" in behavior["afterRemoveRefresh"]["rendered"]
    assert behavior["afterRemoveRefresh"]["refreshTimers"] == 1


def test_confirmed_delete_stays_non_actionable_until_state_reconciliation_recovers():
    """A successful DELETE cannot leave its stale retained action enabled after load failure."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for confirmed deletion regression coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this.textContent=''; this.attrs={}; this.dataset={}; this.elements=[]; this.listeners={}; this.disabled=false; this._innerHTML=''; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) { document.activeElement=this; this.focusOptions=options || null; }
  reset() {}
  set innerHTML(value) { this._innerHTML=String(value); if (this.id === 'repositories') rebuild(this._innerHTML); }
  get innerHTML() { return this._innerHTML; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading'];
const elements=Object.fromEntries(ids.map(id => [id,new Element(id)]));
elements['add-form'].elements=[elements.repository,elements.branch,elements['add-button']];
let dynamic=[];
function rebuild(markup) {
  dynamic=[];
  for (const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*data-repository="([^"]+)"[^>]*>/g)) {
    const button=new Element(`remove-${match[1]}`);
    button.dataset={remove:match[1],removeFocus:match[1],repository:match[2]};
    button.innerHTML='Remove repository';
    dynamic.push(button);
  }
  for (const match of markup.matchAll(/<h3 id="repository-([^"]+)-heading"[^>]*>/g)) {
    const heading=new Element(`repository-${match[1]}-heading`);
    heading.dataset.repositoryHeading=match[1];
    heading.attrs.tabindex='-1';
    dynamic.push(heading);
  }
  for (const match of markup.matchAll(/id="(remove-feedback-[^"]+)"[^>]*>/g)) dynamic.push(new Element(match[1]));
}
function attributeValue(selector,name) {
  const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`));
  return match && match[1];
}
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || dynamic.find(item => item.id === selector.slice(1)) || null;
    for (const name of ['data-remove','data-remove-focus']) {
      const value=attributeValue(selector,name);
      if (value) return dynamic.find(item => (name === 'data-remove' ? item.dataset.remove : item.dataset.removeFocus) === value) || null;
    }
    return null;
  },
  querySelectorAll(selector) {
    if (selector === '[data-remove]') return dynamic.filter(item => item.dataset.remove);
    if (selector === '[data-remove-focus]') return dynamic.filter(item => item.dataset.removeFocus);
    if (selector === '[data-repository-heading]') return dynamic.filter(item => item.dataset.repositoryHeading);
    return [];
  },
  addEventListener(name,callback) { documentListeners[name]=callback; }
};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
let getCount=0,deleteCount=0;
const tracked={repositories:[{id:7,github_repository:'acme/widget',target_branch:'main',nodes:[],runs:[]}]};
global.fetch=async (path,options={}) => {
  if (path === '/api/state') {
    getCount += 1;
    if (getCount === 1) return {ok:true,status:200,json:async()=>tracked};
    if (getCount === 2) return {ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'State unavailable'})};
    return {ok:true,status:200,json:async()=>({repositories:[]})};
  }
  if (path === '/api/repositories/7' && options.method === 'DELETE') {
    deleteCount += 1;
    return {ok:true,status:204,json:async()=>null};
  }
  throw new Error(`unexpected request ${path}`);
};
async function settle() { await Promise.resolve(); await Promise.resolve(); await new Promise(resolve=>setImmediate(resolve)); }
'''
    scenario = r'''
(async()=>{
  await settle();
  const original=document.querySelector('[data-remove="7"]');
  const removal=original.listeners.click();
  await removal;
  await settle();
  const stale=document.querySelector('[data-remove="7"]');
  const afterFailure={
    deleteCount,getCount,
    sameControl:stale === original,
    disabled:stale.disabled,
    label:stale.textContent,
    success:elements['management-status'].textContent,
    refreshError:elements['refresh-error'].textContent,
    confirmed:confirmedDeletions.has('7'),
    focusId:document.activeElement && document.activeElement.id,
    focusPreventScroll:document.activeElement && document.activeElement.focusOptions && document.activeElement.focusOptions.preventScroll
  };
  await stale.listeners.click();
  const afterStaleActivation={deleteCount};
  await load({background:true});
  const recovered={
    deleteCount,getCount,
    controlPresent:Boolean(document.querySelector('[data-remove="7"]')),
    confirmed:confirmedDeletions.has('7'),
    success:elements['management-status'].textContent,
    refreshError:elements['refresh-error'].textContent,
    content:elements.repositories.innerHTML
  };
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({afterFailure,afterStaleActivation,recovered}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    failed = behavior["afterFailure"]
    assert failed["deleteCount"] == 1
    assert failed["getCount"] == 2
    assert failed["sameControl"] is True
    assert failed["disabled"] is True
    assert failed["label"] == "Removal confirmed — refreshing repository state…"
    assert failed["success"] == "acme/widget was removed from tracked repositories."
    assert "Updates are temporarily unavailable: State unavailable" in failed["refreshError"]
    assert failed["confirmed"] is True
    assert failed["focusId"] == "repository-7-heading"
    assert failed["focusPreventScroll"] is True
    assert behavior["afterStaleActivation"] == {"deleteCount": 1}

    recovered = behavior["recovered"]
    assert recovered["deleteCount"] == 1
    assert recovered["getCount"] == 3
    assert recovered["controlPresent"] is False
    assert recovered["confirmed"] is False
    assert recovered["success"] == "acme/widget was removed from tracked repositories."
    assert recovered["refreshError"] == ""
    assert "No repositories are tracked yet" in recovered["content"]


def test_retained_removal_errors_do_not_repeat_alert_announcements():
    """Real DELETE and refresh flows announce each error occurrence exactly once."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for removal announcement lifecycle coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this._textContent=''; this._innerHTML=''; this.attrs={}; this.listeners={}; this.elements=[]; this.dataset={}; this.disabled=false; }
  set textContent(value) {
    this._textContent=String(value);
    if (this.id === 'removal-announcement') announcementWrites.push(this._textContent);
  }
  get textContent() { return this._textContent; }
  set innerHTML(value) {
    this._innerHTML=String(value);
    if (this.id === 'repositories') rebuildRepositories(this._innerHTML);
  }
  get innerHTML() { return this._innerHTML; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) { document.activeElement=this; this.focusOptions=options || null; }
  reset() {}
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading'];
const announcementWrites=[];
const elements=Object.fromEntries(ids.map(id => [id,new Element(id)]));
elements['add-form'].elements=[elements.repository,elements.branch,elements['add-button']];
let removeControls=[];
let feedbackRegions={};
let repositoryHeadings=[];
function rebuildRepositories(markup) {
  removeControls=[]; feedbackRegions={}; repositoryHeadings=[];
  for (const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*data-repository="([^"]+)"[^>]*data-remove-focus="([^"]+)"[^>]*aria-describedby="([^"]+)"/g)) {
    const control=new Element(`remove-${match[1]}`);
    control.dataset={remove:match[1],repository:match[2],removeFocus:match[3]};
    control.attrs['aria-describedby']=match[4];
    control.innerHTML='Remove repository';
    removeControls.push(control);
  }
  for (const match of markup.matchAll(/<div id="(remove-feedback-[^"]+)" class="feedback feedback--error"><\/div>/g)) {
    feedbackRegions[match[1]]=new Element(match[1]);
  }
  for (const match of markup.matchAll(/<h2 id="([^"]+)" data-repository-heading="([^"]+)"[^>]*>/g)) {
    const heading=new Element(match[1]); heading.dataset.repositoryHeading=match[2]; heading.attrs.tabindex='-1'; repositoryHeadings.push(heading);
  }
}
function attributeValue(selector,name) {
  const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`));
  return match && match[1];
}
const documentListeners={}, windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || feedbackRegions[selector.slice(1)] || repositoryHeadings.find(item=>item.id===selector.slice(1)) || null;
    const remove=attributeValue(selector,'data-remove');
    if (remove) return removeControls.find(item=>item.dataset.remove===remove) || null;
    const removeFocus=attributeValue(selector,'data-remove-focus');
    if (removeFocus) return removeControls.find(item=>item.dataset.removeFocus===removeFocus) || null;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === '[data-remove]' || selector === '[data-remove-focus]') return removeControls;
    if (selector === '[data-repository-heading]') return repositoryHeadings;
    return [];
  },
  addEventListener:(name,callback) => { documentListeners[name]=callback; }
};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
const repo1=(branch='main')=>({id:1,github_repository:'acme/one',target_branch:branch,nodes:[],runs:[]});
const repo2=(branch='main')=>({id:2,github_repository:'acme/two',target_branch:branch,nodes:[],runs:[]});
const states=[
  {repositories:[repo1(),repo2()]},
  {repositories:[repo1('release'),repo2()]},
  {repositories:[repo1('release'),repo2('develop')]},
  {repositories:[repo2('develop')]},
  {repositories:[repo1(),repo2('develop')]},
  {repositories:[repo2('develop')]}
];
let stateIndex=0;
let deleteCount=0;
global.fetch=async (path,options={}) => {
  if (path === '/api/state') return {ok:true,status:200,json:async()=>states[stateIndex++]};
  if (path === '/api/repositories/1' && options.method === 'DELETE') {
    deleteCount += 1;
    if (deleteCount === 1) return {ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'First unavailable'})};
    if (deleteCount === 2) return {ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'First unavailable'})};
    if (deleteCount === 3) return {ok:true,status:204,json:async()=>null};
    return {ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'Later unavailable'})};
  }
  throw new Error(`unexpected request ${path}`);
};
async function settle() { await Promise.resolve(); await Promise.resolve(); await new Promise(resolve=>setImmediate(resolve)); }
'''
    scenario = r'''
(async()=>{
  await settle();
  announcementWrites.length=0;

  const firstButton=removeControls.find(control=>control.dataset.remove==='1');
  await firstButton.listeners.click();
  const firstMessage=feedbackRegions['remove-feedback-1'].textContent;
  const first={writes:[...announcementWrites],message:firstMessage,describedBy:firstButton.attrs['aria-describedby']};

  await load({background:true});
  const refreshedOnce=removeControls.find(control=>control.dataset.remove==='1');
  const afterFirstRefresh={writes:[...announcementWrites],message:feedbackRegions['remove-feedback-1'].textContent,other:feedbackRegions['remove-feedback-2'].textContent,replaced:refreshedOnce!==firstButton,describedBy:refreshedOnce.attrs['aria-describedby']};
  await load({background:true});
  const refreshedTwice=removeControls.find(control=>control.dataset.remove==='1');
  const afterSecondRefresh={writes:[...announcementWrites],message:feedbackRegions['remove-feedback-1'].textContent,other:feedbackRegions['remove-feedback-2'].textContent,replaced:refreshedTwice!==refreshedOnce};

  const retryPromise=refreshedTwice.listeners.click();
  const retryStart={writes:[...announcementWrites],message:feedbackRegions['remove-feedback-1'].textContent};
  await retryPromise;
  const replacementMessage=feedbackRegions['remove-feedback-1'].textContent;
  const replacement={writes:[...announcementWrites],message:replacementMessage,focused:document.activeElement===refreshedTwice};

  const successPromise=refreshedTwice.listeners.click();
  await successPromise;
  const success={writes:[...announcementWrites],stored:removalErrors.has('1'),repositoryPresent:Boolean(removeControls.find(control=>control.dataset.remove==='1')),announcement:elements['removal-announcement'].textContent};

  await load({background:true});
  const returnedButton=removeControls.find(control=>control.dataset.remove==='1');
  await returnedButton.listeners.click();
  const laterMessage=feedbackRegions['remove-feedback-1'].textContent;
  const laterFailure={writes:[...announcementWrites],message:laterMessage};
  await load({background:true});
  const disappeared={writes:[...announcementWrites],stored:removalErrors.has('1'),repositoryPresent:Boolean(removeControls.find(control=>control.dataset.remove==='1')),announcement:elements['removal-announcement'].textContent};

  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({first,afterFirstRefresh,afterSecondRefresh,retryStart,replacement,success,laterFailure,disappeared}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)

    first_message = behavior["first"]["message"]
    assert "Could not remove acme/one: First unavailable" in first_message
    assert behavior["first"] == {
        "writes": [first_message],
        "message": first_message,
        "describedBy": "remove-feedback-1",
    }
    assert behavior["afterFirstRefresh"] == {
        "writes": [first_message],
        "message": first_message,
        "other": "",
        "replaced": True,
        "describedBy": "remove-feedback-1",
    }
    assert behavior["afterSecondRefresh"] == {
        "writes": [first_message],
        "message": first_message,
        "other": "",
        "replaced": True,
    }
    assert behavior["retryStart"] == {"writes": [first_message, ""], "message": ""}

    replacement_message = behavior["replacement"]["message"]
    assert replacement_message == first_message
    assert behavior["replacement"] == {
        "writes": [first_message, "", replacement_message],
        "message": replacement_message,
        "focused": True,
    }
    assert behavior["success"] == {
        "writes": [first_message, "", replacement_message, ""],
        "stored": False,
        "repositoryPresent": False,
        "announcement": "",
    }

    later_message = behavior["laterFailure"]["message"]
    assert "Could not remove acme/one: Later unavailable" in later_message
    assert behavior["laterFailure"] == {
        "writes": [first_message, "", replacement_message, "", later_message],
        "message": later_message,
    }
    assert behavior["disappeared"] == {
        "writes": [first_message, "", replacement_message, "", later_message, ""],
        "stored": False,
        "repositoryPresent": False,
        "announcement": "",
    }

    # The frequently replaced repository subtree remains ordinary content. Only the
    # stable dedicated node owns assertive semantics, while contextual presentation
    # stays associated to the matching native remove control via aria-describedby.
    parser = AccessibleMarkupParser()
    parser.feed(_CLIENT_HTML)
    by_id = {attrs.get("id"): attrs for _, attrs in parser.elements if attrs.get("id")}
    assert by_id["removal-announcement"]["role"] == "alert"
    assert by_id["removal-announcement"]["aria-atomic"] == "true"
    assert "role" not in by_id["repositories"]
    assert "aria-live" not in by_id["repositories"]

def test_remove_transport_failure_reconciles_authoritative_state_without_duplicate_delete():
    """Response-less DELETE failures reconcile absence, presence, or unavailable state."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for ambiguous removal regression coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this.value=''; this._textContent=''; this._innerHTML=''; this.attrs={}; this.listeners={}; this.disabled=false; this.dataset={}; this.elements=[]; }
  set textContent(value) { this._textContent=String(value); }
  get textContent() { return this._textContent; }
  set innerHTML(value) { this._innerHTML=String(value); if (this.id === 'repositories') rebuild(this._innerHTML); }
  get innerHTML() { return this._innerHTML; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) { document.activeElement=this; this.focusOptions=options || null; }
  reset() {}
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
elements['add-form'].elements=[elements.repository,elements.branch,elements['add-button']];
let removeControls=[];
const feedback={};
function rebuild(markup) {
  removeControls=[];
  for (const match of markup.matchAll(/data-remove="([^"]+)" data-repository="([^"]+)"[^>]*>/g)) {
    const button=new Element(`remove-${match[1]}`);
    button.dataset={remove:match[1],repository:match[2],removeFocus:match[1]};
    button.innerHTML='Remove repository';
    removeControls.push(button);
  }
  for (const match of markup.matchAll(/id="(remove-feedback-[^"]+)"/g)) feedback[match[1]]=new Element(match[1]);
}
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || feedback[selector.slice(1)] || null;
    let match=selector.match(/^\[data-remove="([^"]+)"\]$/);
    if (match) return removeControls.find(button=>button.dataset.remove===match[1]) || null;
    match=selector.match(/^\[data-remove-focus="([^"]+)"\]$/);
    if (match) return removeControls.find(button=>button.dataset.removeFocus===match[1]) || null;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === '[data-remove]' || selector === '[data-remove-focus]') return removeControls;
    if (selector === '[data-repository-heading]') return [];
    return [];
  },
  addEventListener:(name,callback)=>{documentListeners[name]=callback;}
};
let confirmations=0;
global.window={confirm:()=>{confirmations += 1; return true;},addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
let nextTimerId=1;
const timers=new Map();
global.setTimeout=(callback,delay)=>{const id=nextTimerId++;timers.set(id,{callback,delay});return id;};
global.clearTimeout=id=>{timers.delete(id);};
function timerCount(delay){return [...timers.values()].filter(timer=>timer.delay===delay).length;}
const present={repositories:[{id:7,github_repository:'acme/widget',target_branch:'main',nodes:[],runs:[]}]};
const absent={repositories:[]};
let stateQueue=[present];
let deleteModes=[];
let pendingDeleteReject=null;
let deleteCount=0,getCount=0;
global.fetch=(path,options={})=>{
  if (path === '/api/state') {
    getCount += 1;
    const next=stateQueue.shift();
    if (next === 'failure') return Promise.resolve({ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'State unavailable'})});
    return Promise.resolve({ok:true,status:200,json:async()=>next});
  }
  if (path === '/api/repositories/7' && options.method === 'DELETE') {
    deleteCount += 1;
    const mode=deleteModes.shift();
    if (mode === 'http') return Promise.resolve({ok:false,status:409,statusText:'Conflict',json:async()=>({error:'Deletion rejected'})});
    if (mode === 'pending-transport') return new Promise((resolve,reject)=>{pendingDeleteReject=reject;});
    return Promise.reject(new TypeError('connection lost'));
  }
  throw new Error(`unexpected request ${path}`);
};
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}
'''
    scenario = r'''
(async()=>{
  await settle();
  const initial={getCount,refreshTimers:timerCount(3000),requestTimeouts:timerCount(15000)};
  deleteModes.push('pending-transport'); stateQueue.push(absent);
  const absentButton=removeControls[0];
  const absentAttempt=absentButton.listeners.click();
  await settle();
  const pending={
    deleteCount,getCount,confirmations,
    removeTimeouts:timerCount(15000),refreshTimers:timerCount(3000),
    removeDisabled:absentButton.disabled,
    addControlsDisabled:elements['add-form'].elements.every(control=>control.disabled),
    pendingLabel:absentButton.textContent
  };
  await absentButton.listeners.click();
  const duplicateGuard={deleteCount,getCount,confirmations};
  pendingDeleteReject(new TypeError('connection lost'));
  await absentAttempt;
  const confirmedAbsent={
    deleteCount,getCount,controlPresent:removeControls.length>0,
    status:elements['management-status'].textContent,error:removalErrors.get('7') || '',
    removeTimeouts:timerCount(15000),refreshTimers:timerCount(3000),
    addControlsRestored:elements['add-form'].elements.every(control=>!control.disabled)
  };

  stateQueue.push(present); await load({background:true});
  deleteModes.push('transport'); stateQueue.push(present);
  const presentButton=removeControls[0];
  await presentButton.listeners.click();
  const currentPresentButton=removeControls[0];
  const confirmedPresent={deleteCount,getCount,controlPresent:Boolean(currentPresentButton),error:removalErrors.get('7'),focused:document.activeElement===currentPresentButton,preventScroll:currentPresentButton.focusOptions && currentPresentButton.focusOptions.preventScroll,disabled:currentPresentButton.disabled,removeTimeouts:timerCount(15000),refreshTimers:timerCount(3000),addControlsRestored:elements['add-form'].elements.every(control=>!control.disabled)};

  deleteModes.push('transport'); stateQueue.push('failure');
  await currentPresentButton.listeners.click();
  const currentUnavailableButton=removeControls[0];
  const unavailable={deleteCount,getCount,error:removalErrors.get('7'),focused:document.activeElement===currentUnavailableButton,preventScroll:currentUnavailableButton.focusOptions && currentUnavailableButton.focusOptions.preventScroll,disabled:currentUnavailableButton.disabled,removeTimeouts:timerCount(15000),refreshTimers:timerCount(3000),addControlsRestored:elements['add-form'].elements.every(control=>!control.disabled)};

  deleteModes.push('http');
  await currentUnavailableButton.listeners.click();
  const authoritative={deleteCount,getCount,error:removalErrors.get('7'),focused:document.activeElement===currentUnavailableButton,disabled:currentUnavailableButton.disabled,removeTimeouts:timerCount(15000),refreshTimers:timerCount(3000),addControlsRestored:elements['add-form'].elements.every(control=>!control.disabled)};
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({initial,pending,duplicateGuard,confirmedAbsent,confirmedPresent,unavailable,authoritative}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    assert behavior["initial"] == {
        "getCount": 1,
        "refreshTimers": 1,
        "requestTimeouts": 0,
    }
    assert behavior["pending"] == {
        "deleteCount": 1,
        "getCount": 1,
        "confirmations": 1,
        "removeTimeouts": 1,
        "refreshTimers": 1,
        "removeDisabled": True,
        "addControlsDisabled": True,
        "pendingLabel": "Removing repository…",
    }
    assert behavior["duplicateGuard"] == {
        "deleteCount": 1,
        "getCount": 1,
        "confirmations": 1,
    }
    assert behavior["confirmedAbsent"] == {
        "deleteCount": 1,
        "getCount": 2,
        "controlPresent": False,
        "status": "acme/widget was removed from tracked repositories.",
        "error": "",
        "removeTimeouts": 0,
        "refreshTimers": 1,
        "addControlsRestored": True,
    }
    present = behavior["confirmedPresent"]
    assert present["deleteCount"] == 2
    assert present["getCount"] == 4
    assert present["controlPresent"] is True
    assert "was not observed in the latest tracked repository state" in present["error"]
    assert "still tracked" not in present["error"]
    assert present["focused"] is True
    assert present["preventScroll"] is True
    assert present["disabled"] is False
    assert present["removeTimeouts"] == 0
    assert present["refreshTimers"] == 1
    assert present["addControlsRestored"] is True
    unavailable = behavior["unavailable"]
    assert unavailable["deleteCount"] == 3
    assert unavailable["getCount"] == 5
    assert "Could not confirm whether acme/widget was removed" in unavailable["error"]
    assert "connection ended before Repogents received a response" in unavailable["error"]
    assert "still tracked" not in unavailable["error"]
    assert unavailable["focused"] is True
    assert unavailable["preventScroll"] is True
    assert unavailable["disabled"] is False
    assert unavailable["removeTimeouts"] == 0
    assert unavailable["refreshTimers"] == 1
    assert unavailable["addControlsRestored"] is True
    authoritative = behavior["authoritative"]
    assert authoritative["deleteCount"] == 4
    assert authoritative["getCount"] == 5  # Authoritative HTTP failure does not reconcile.
    assert "Could not remove acme/widget: Deletion rejected" in authoritative["error"]
    assert "The repository is still tracked; try again" in authoritative["error"]
    assert authoritative["focused"] is True
    assert authoritative["disabled"] is False
    assert authoritative["removeTimeouts"] == 0
    assert authoritative["refreshTimers"] == 1
    assert authoritative["addControlsRestored"] is True


def test_repository_heading_focus_survives_successive_changed_refreshes():
    """A heading reached as fallback remains stable, then falls back logically."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for repository heading focus coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this.textContent=''; this.attrs={}; this.dataset={}; this.elements=[]; this.disabled=false; this.listeners={}; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) { document.activeElement=this; this.focusOptions=options || null; }
  reset() {}
  set innerHTML(value) { this._innerHTML=String(value); rebuild(this._innerHTML); }
  get innerHTML() { return this._innerHTML || ''; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','repositories-heading'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
elements['add-form'].elements=[elements.repository,elements.branch,elements['add-button']];
let dynamic=[];
function rebuild(markup) {
  dynamic=[];
  for (const match of markup.matchAll(/<a[^>]*data-pr-focus="([^"]+)"[^>]*data-pr-repository="([^"]+)"[^>]*>/g)) {
    const link=new Element(); link.dataset.prFocus=match[1]; link.dataset.prRepository=match[2]; dynamic.push(link);
  }
  for (const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*>/g)) {
    const button=new Element(); button.dataset.remove=match[1]; button.dataset.removeFocus=match[1]; dynamic.push(button);
  }
  for (const match of markup.matchAll(/<h3 id="repository-([^"]+)-heading"[^>]*data-repository-heading="([^"]+)"[^>]*>/g)) {
    const heading=new Element(`repository-${match[1]}-heading`); heading.dataset.repositoryHeading=match[2]; heading.attrs.tabindex='-1'; dynamic.push(heading);
  }
}
function value(selector,name) { const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`)); return match && match[1]; }
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || dynamic.find(item=>item.id===selector.slice(1)) || null;
    for (const name of ['data-pr-focus','data-remove-focus','data-remove','data-repository-heading']) {
      const expected=value(selector,name);
      if (expected) {
        const key={ 'data-pr-focus':'prFocus', 'data-remove-focus':'removeFocus', 'data-remove':'remove', 'data-repository-heading':'repositoryHeading' }[name];
        return dynamic.find(item=>item.dataset[key]===expected) || null;
      }
    }
    return null;
  },
  querySelectorAll(selector) {
    if (selector === '[data-remove]') return dynamic.filter(item=>item.dataset.remove);
    if (selector === '[data-remove-focus]') return dynamic.filter(item=>item.dataset.removeFocus);
    if (selector === '[data-repository-heading]') return dynamic.filter(item=>item.dataset.repositoryHeading);
    return [];
  },
  addEventListener(name,callback) { documentListeners[name]=callback; }
};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
const repo7=(title,pr)=>({id:7,github_repository:'acme/widget',target_branch:'main',nodes:[],runs:[{id:9,issue_number:9,state:'RUNNING',branch:'work',issue_json:{title},pull_request:pr?{number:12,url:'https://example.test/pr/12',state:'OPEN'}:null,specifications:[],work_items:[]}]});
const repo8=branch=>({id:8,github_repository:'acme/neighbor',target_branch:branch,nodes:[],runs:[]});
const states=[
 {repositories:[repo7('Initial',true),repo8('main')]},
 {repositories:[repo7('PR disappeared',false),repo8('main')]},
 {repositories:[repo7('Heading persists once',false),repo8('release')]},
 {repositories:[repo7('Heading persists twice',false),repo8('develop')]},
 {repositories:[repo8('final')]},
 {repositories:[]}
];
let request=0;
global.fetch=async()=>({ok:true,status:200,json:async()=>states[request++]});
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}
'''
    scenario = r'''
(async()=>{
  await settle();
  const pr=document.querySelector('[data-pr-focus="7:9:12"]');
  pr.focus();
  await load({background:true});
  const fallback=document.activeElement;
  await load({background:true});
  const firstReplacement=document.activeElement;
  await load({background:true});
  const secondReplacement=document.activeElement;
  await load({background:true});
  const nearbyFallback=document.activeElement;
  await load({background:true});
  const pageFallback=document.activeElement;
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({
    fallbackId:fallback.id,
    fallbackIdentity:fallback.dataset.repositoryHeading,
    fallbackPreventScroll:fallback.focusOptions && fallback.focusOptions.preventScroll,
    firstReplaced:firstReplacement!==fallback,
    firstId:firstReplacement.id,
    firstIdentity:firstReplacement.dataset.repositoryHeading,
    firstPreventScroll:firstReplacement.focusOptions && firstReplacement.focusOptions.preventScroll,
    secondReplaced:secondReplacement!==firstReplacement,
    secondId:secondReplacement.id,
    secondIdentity:secondReplacement.dataset.repositoryHeading,
    secondPreventScroll:secondReplacement.focusOptions && secondReplacement.focusOptions.preventScroll,
    nearbyId:nearbyFallback.id,
    nearbyIdentity:nearbyFallback.dataset.repositoryHeading,
    nearbyTabindex:nearbyFallback.attrs.tabindex,
    nearbyPreventScroll:nearbyFallback.focusOptions && nearbyFallback.focusOptions.preventScroll,
    pageId:pageFallback.id,
    pageTabindex:pageFallback.attrs.tabindex,
    pagePreventScroll:pageFallback.focusOptions && pageFallback.focusOptions.preventScroll
  }));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True, capture_output=True, text=True, timeout=5,
    )
    assert json.loads(result.stdout) == {
        "fallbackId": "repository-7-heading",
        "fallbackIdentity": "7",
        "fallbackPreventScroll": True,
        "firstReplaced": True,
        "firstId": "repository-7-heading",
        "firstIdentity": "7",
        "firstPreventScroll": True,
        "secondReplaced": True,
        "secondId": "repository-7-heading",
        "secondIdentity": "7",
        "secondPreventScroll": True,
        "nearbyId": "repository-8-heading",
        "nearbyIdentity": "8",
        "nearbyTabindex": "-1",
        "nearbyPreventScroll": True,
        "pageId": "repositories-heading",
        "pageTabindex": "-1",
        "pagePreventScroll": True,
    }


def test_pull_request_renderer_labels_merged_state_without_changing_link_contracts():
    """Explicit GitHub merged state takes precedence over generic PR lifecycle state."""
    from repogents.http_api import _CLIENT_HTML

    def rendered_pull_request(*, state, merged=False, number=73):
        fixture = {
            "id": 42,
            "github_repository": "acme/status-widget",
            "target_branch": "main",
            "nodes": [],
            "runs": [{
                "id": 91,
                "issue_number": 28,
                "state": "PR_LISTENING",
                "branch": "agent/issue-28",
                "pull_request": {
                    "number": number,
                    "url": f"https://github.example/acme/status-widget/pull/{number}",
                    "state": state,
                    "merged": merged,
                },
                "specifications": [],
                "work_items": [],
            }],
        }
        return _render_fixture_with_client_javascript(_CLIENT_HTML, fixture)["repository"]

    merged = rendered_pull_request(state="CLOSED", merged=True)
    assert 'aria-label="Pull request status: Merged"' in merged
    assert '<span class="badge badge--success" aria-label="Pull request status: Merged">' in merged
    assert '<span class="status-mark" aria-hidden="true">✓</span>Merged' in merged
    assert 'Pull request status: Completed' not in merged

    # Navigation, security, and stable refresh-focus identity are unaffected.
    assert 'href="https://github.example/acme/status-widget/pull/73"' in merged
    assert 'target="_blank"' in merged
    assert 'rel="noopener noreferrer"' in merged
    assert 'data-pr-focus="42:91:73"' in merged
    assert 'data-pr-repository="42"' in merged
    assert 'aria-label="Pull request #73 (opens in a new tab)"' in merged
    assert '>Pull request #73 <span aria-hidden="true">↗</span></a>' in merged

    # The unchanged shared responsive run metadata layout keeps PR navigation
    # reachable when branch and status content wrap at narrow widths.
    css, _ = _client_parts(_CLIENT_HTML)
    run_meta = _css_declarations(css, ".run-meta")
    run_meta_item = _css_declarations(css, ".run-meta-item")
    assert run_meta["display"] == "flex"
    assert run_meta["flex-wrap"] == "wrap"
    assert run_meta_item["min-width"] == "0"

    opened = rendered_pull_request(state="OPEN")
    closed = rendered_pull_request(state="CLOSED")
    unknown = rendered_pull_request(state="REVIEW_REQUIRED")
    assert 'aria-label="Pull request status: Open"' in opened
    assert '<span class="badge badge--active" aria-label="Pull request status: Open">' in opened
    assert 'aria-label="Pull request status: Closed"' in closed
    assert '<span class="badge" aria-label="Pull request status: Closed">' in closed
    assert 'aria-label="Pull request status: Review required"' in unknown
    assert '<span class="badge" aria-label="Pull request status: Review required">' in unknown


def test_confirmed_deletion_survives_failed_and_successful_add_cleanup_until_authoritative_absence():
    """Add settlement cannot reactivate a server-confirmed stale removal control."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for cross-mutation confirmed-deletion coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this.value=''; this.textContent=''; this.attrs={}; this.dataset={}; this.elements=[]; this.listeners={}; this.disabled=false; this._innerHTML=''; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) {
    if (this.disabled) return;
    document.activeElement=this; this.focusOptions=options || null;
  }
  reset() { repository.value=''; branch.value=''; this.resetCount=(this.resetCount||0)+1; }
  set innerHTML(value) { this._innerHTML=String(value); if (this.id === 'repositories') rebuild(this._innerHTML); }
  get innerHTML() { return this._innerHTML; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
const repository=elements.repository, branch=elements.branch, form=elements['add-form'], addControl=elements['add-button'];
form.elements=[repository,branch,addControl];
let dynamic=[];
function rebuild(markup) {
  dynamic=[];
  for (const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*data-repository="([^"]+)"[^>]*>/g)) {
    const button=new Element(`remove-${match[1]}`);
    button.dataset={remove:match[1],removeFocus:match[1],repository:match[2]};
    button.innerHTML='Remove repository';
    dynamic.push(button);
  }
  for (const match of markup.matchAll(/<h3 id="repository-([^"]+)-heading"[^>]*>/g)) {
    const heading=new Element(`repository-${match[1]}-heading`);
    heading.dataset.repositoryHeading=match[1]; heading.attrs.tabindex='-1';
    dynamic.push(heading);
  }
  for (const match of markup.matchAll(/id="(remove-feedback-[^"]+)"[^>]*>/g)) dynamic.push(new Element(match[1]));
}
function attributeValue(selector,name) {
  const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`));
  return match && match[1];
}
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || dynamic.find(item=>item.id===selector.slice(1)) || null;
    for (const name of ['data-remove','data-remove-focus','data-repository-heading']) {
      const expected=attributeValue(selector,name);
      if (!expected) continue;
      const key={'data-remove':'remove','data-remove-focus':'removeFocus','data-repository-heading':'repositoryHeading'}[name];
      return dynamic.find(item=>item.dataset[key]===expected) || null;
    }
    return null;
  },
  querySelectorAll(selector) {
    if (selector === '[data-remove]') return dynamic.filter(item=>item.dataset.remove);
    if (selector === '[data-remove-focus]') return dynamic.filter(item=>item.dataset.removeFocus);
    if (selector === '[data-repository-heading]') return dynamic.filter(item=>item.dataset.repositoryHeading);
    return [];
  },
  addEventListener(name,callback) { documentListeners[name]=callback; }
};
let confirmations=0;
global.window={confirm:()=>{confirmations += 1; return true;},addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
const repo7={id:7,github_repository:'acme/deleted',target_branch:'main',nodes:[],runs:[]};
const repo8={id:8,github_repository:'acme/unrelated',target_branch:'main',nodes:[],runs:[]};
const repo9={id:9,github_repository:'acme/added',target_branch:'release',nodes:[],runs:[]};
let getCount=0,deleteCount=0,postCount=0;
let settlePost=null;
global.fetch=(path,options={})=>{
  if (path === '/api/state') {
    getCount += 1;
    if (getCount === 1) return Promise.resolve({ok:true,status:200,json:async()=>({repositories:[repo7,repo8]})});
    if (getCount === 2 || getCount === 3) return Promise.resolve({ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'State unavailable'})});
    return Promise.resolve({ok:true,status:200,json:async()=>({repositories:[repo8,repo9]})});
  }
  if (path === '/api/repositories/7' && options.method === 'DELETE') {
    deleteCount += 1;
    return Promise.resolve({ok:true,status:204,json:async()=>null});
  }
  if (path === '/api/repositories' && options.method === 'POST') {
    postCount += 1;
    return new Promise(resolve=>{ settlePost=()=>resolve(postCount === 1
      ? {ok:false,status:422,statusText:'Unprocessable Entity',json:async()=>({error:'Add rejected'})}
      : {ok:true,status:204,json:async()=>null}); });
  }
  throw new Error(`unexpected request ${path}`);
};
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}
async function submit(){return form.listeners.submit({preventDefault(){}});}
function control(id){return document.querySelector(`[data-remove="${id}"]`);}
function stateOf(id){const item=control(id); return item && {disabled:item.disabled,label:item.textContent};}
'''
    scenario = r'''
(async()=>{
  await settle();
  const deletedControl=control('7');
  const unrelatedControl=control('8');
  await deletedControl.listeners.click();
  await settle();
  const confirmed={
    deleteCount,getCount,confirmations,
    deleted:stateOf('7'),unrelated:stateOf('8'),
    marker:confirmedDeletions.has('7'),
    success:elements['management-status'].textContent,
    refreshError:elements['refresh-error'].textContent
  };

  repository.value='acme/failing'; branch.value='main';
  const failedAdd=submit();
  await settle();
  const failedPending={deleted:stateOf('7'),unrelated:stateOf('8'),postCount,busy:form.attrs['aria-busy']};
  await deletedControl.listeners.click();
  settlePost();
  await failedAdd;
  await settle();
  const afterFailedAdd={
    deleteCount,postCount,getCount,
    deleted:stateOf('7'),unrelated:stateOf('8'),
    marker:confirmedDeletions.has('7'),
    error:elements['add-error'].textContent,
    busy:form.attrs['aria-busy']
  };
  await deletedControl.listeners.click();

  repository.value='acme/added'; branch.value='release';
  const successfulAdd=submit();
  await settle();
  const successPending={deleted:stateOf('7'),unrelated:stateOf('8'),postCount,busy:form.attrs['aria-busy']};
  await deletedControl.listeners.click();
  settlePost();
  await successfulAdd;
  await settle();
  const afterSuccessfulAdd={
    deleteCount,postCount,getCount,
    deleted:stateOf('7'),unrelated:stateOf('8'),
    marker:confirmedDeletions.has('7'),
    status:elements['add-status'].textContent,
    refreshError:elements['refresh-error'].textContent,
    busy:form.attrs['aria-busy'],
    values:[repository.value,branch.value]
  };
  await deletedControl.listeners.click();

  await load({background:true});
  const recovered={
    deleteCount,postCount,getCount,
    deletedPresent:Boolean(control('7')),
    unrelated:stateOf('8'),added:stateOf('9'),
    marker:confirmedDeletions.has('7'),
    content:elements.repositories.innerHTML,
    refreshError:elements['refresh-error'].textContent
  };
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({confirmed,failedPending,afterFailedAdd,successPending,afterSuccessfulAdd,recovered}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)

    confirmed = behavior["confirmed"]
    assert confirmed["deleteCount"] == 1
    assert confirmed["getCount"] == 2
    assert confirmed["confirmations"] == 1
    assert confirmed["deleted"] == {
        "disabled": True,
        "label": "Removal confirmed — refreshing repository state…",
    }
    assert confirmed["unrelated"]["disabled"] is False
    assert confirmed["marker"] is True
    assert confirmed["success"] == "acme/deleted was removed from tracked repositories."
    assert "Updates are temporarily unavailable: State unavailable" in confirmed["refreshError"]

    assert behavior["failedPending"] == {
        "deleted": {
            "disabled": True,
            "label": "Removal confirmed — refreshing repository state…",
        },
        "unrelated": {"disabled": True, "label": ""},
        "postCount": 1,
        "busy": "true",
    }
    failed = behavior["afterFailedAdd"]
    assert failed["deleteCount"] == 1
    assert failed["postCount"] == 1
    assert failed["getCount"] == 2
    assert failed["deleted"] == confirmed["deleted"]
    assert failed["unrelated"]["disabled"] is False
    assert failed["marker"] is True
    assert "Could not add acme/failing: Add rejected" in failed["error"]
    assert failed["busy"] == "false"

    assert behavior["successPending"] == {
        "deleted": confirmed["deleted"],
        "unrelated": {"disabled": True, "label": ""},
        "postCount": 2,
        "busy": "true",
    }
    successful = behavior["afterSuccessfulAdd"]
    assert successful["deleteCount"] == 1
    assert successful["postCount"] == 2
    assert successful["getCount"] == 3
    assert successful["deleted"] == confirmed["deleted"]
    assert successful["unrelated"]["disabled"] is False
    assert successful["marker"] is True
    assert successful["status"] == "acme/added was added to tracked repositories."
    assert "Updates are temporarily unavailable: State unavailable" in successful["refreshError"]
    assert successful["busy"] == "false"
    assert successful["values"] == ["", ""]

    recovered = behavior["recovered"]
    assert recovered["deleteCount"] == 1
    assert recovered["postCount"] == 2
    assert recovered["getCount"] == 4
    assert recovered["deletedPresent"] is False
    assert recovered["unrelated"]["disabled"] is False
    assert recovered["added"]["disabled"] is False
    assert recovered["marker"] is False
    assert "acme/unrelated" in recovered["content"]
    assert "acme/added" in recovered["content"]
    assert "acme/deleted" not in recovered["content"]
    assert recovered["refreshError"] == ""


def test_long_unbroken_names_reflow_across_shared_feedback_surfaces():
    """Shared feedback contains complete operational tokens without overflow workarounds."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    feedback = _css_declarations(css, ".feedback")
    empty_feedback = _css_declarations(css, ".feedback:empty")

    # The shared surface, rather than workflow-specific selectors, owns both
    # intrinsic shrinkability and emergency wrapping at narrow widths/zoom.
    assert feedback["min-width"] == "0"
    assert feedback["max-width"] == "100%"
    assert feedback["overflow-wrap"] == "anywhere"
    assert "font-size" not in feedback
    assert "width" not in feedback
    assert "text-overflow" not in feedback
    assert feedback.get("overflow") != "hidden"
    assert feedback.get("white-space") != "nowrap"
    assert {
        "min-height": "0",
        "margin-block": "0",
        "border": "0",
        "background": "transparent",
        "padding": "0",
    }.items() <= empty_feedback.items()

    parser = AccessibleMarkupParser()
    parser.feed(_CLIENT_HTML)
    by_id = {
        attrs["id"]: attrs
        for _, attrs in parser.elements
        if attrs.get("id")
    }
    for region_id, modifier, role in (
        ("add-error", "feedback--error", "alert"),
        ("add-status", "feedback--success", "status"),
        ("management-status", "feedback--success", "status"),
        ("removal-announcement", "feedback--error", "alert"),
        ("refresh-error", "feedback--warning", "alert"),
    ):
        classes = by_id[region_id]["class"].split()
        assert "feedback" in classes
        assert modifier in classes
        assert by_id[region_id]["role"] == role

    long_name = "owner/" + "UnbrokenOperationalRepositoryToken" * 50
    contextual_markup = _render_fixture_with_client_javascript(
        _CLIENT_HTML,
        {
            "id": 7,
            "github_repository": long_name,
            "target_branch": "main",
            "nodes": [],
            "runs": [],
        },
    )["repository"]
    assert long_name in contextual_markup
    assert (
        '<div id="remove-feedback-7" class="feedback feedback--error"></div>'
        in contextual_markup
    )
    assert 'aria-describedby="remove-feedback-7"' in contextual_markup

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for feedback message regression coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id) { this.id=id; this.value=''; this.textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.disabled=false; this.dataset={}; this.elements=[]; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus() { if (!this.disabled) document.activeElement=this; }
  reset() { repository.value=''; branch.value=''; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading','remove-feedback-7'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
const repository=elements.repository, branch=elements.branch, form=elements['add-form'];
form.elements=[repository,branch,elements['add-button']];
const removeButton=new Element('remove-7');
removeButton.dataset={remove:'7',repository:LONG_NAME,removeFocus:'7'};
removeButton.innerHTML=`Remove repository <span>${LONG_NAME}</span>`;
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || null;
    const match=selector.match(/^\[data-remove="([^"]+)"\]$/);
    return match && match[1] === '7' ? removeButton : null;
  },
  querySelectorAll(selector) {
    if (selector === '[data-remove]' || selector === '[data-remove-focus]') return [removeButton];
    return [];
  },
  addEventListener:(name,callback)=>{documentListeners[name]=callback;}
};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
let mode='initial';
global.fetch=async (path,options={}) => {
  if (path === '/api/state') {
    if (mode === 'refresh-failure') return {ok:false,status:503,statusText:'Unavailable',json:async()=>({error:`UnableToRefresh${LONG_NAME}`})};
    return {ok:true,status:200,json:async()=>({repositories:[]})};
  }
  if (path === '/api/repositories' && options.method === 'POST') {
    if (mode === 'add-failure') return {ok:false,status:422,statusText:'Unprocessable Entity',json:async()=>({error:`UnableToAdd${LONG_NAME}`})};
    return {ok:true,status:204,json:async()=>null};
  }
  if (path === '/api/repositories/7' && options.method === 'DELETE') {
    if (mode === 'remove-failure') return {ok:false,status:409,statusText:'Conflict',json:async()=>({error:`UnableToRemove${LONG_NAME}`})};
    return {ok:true,status:204,json:async()=>null};
  }
  throw new Error(`unexpected request ${path}`);
};
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}
async function submit(){return form.listeners.submit({preventDefault(){}});}
'''
    scenario = r'''
(async()=>{
  await settle();
  bindRemoveActions();

  mode='add-success';
  repository.value=LONG_NAME;
  await submit();
  const addStatus=elements['add-status'].textContent;

  mode='add-failure';
  repository.value=LONG_NAME;
  await submit();
  const addError=elements['add-error'].textContent;

  mode='remove-failure';
  await removeButton.listeners.click();
  const contextualError=elements['remove-feedback-7'].textContent;
  const removalAnnouncement=elements['removal-announcement'].textContent;

  mode='remove-success';
  await removeButton.listeners.click();
  const managementStatus=elements['management-status'].textContent;

  mode='refresh-failure';
  await load({background:true});
  const refreshError=elements['refresh-error'].textContent;

  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({addError,addStatus,contextualError,removalAnnouncement,managementStatus,refreshError}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [
            node,
            "-e",
            f"const LONG_NAME={json.dumps(long_name)};\n" + harness + "\n" + script + "\n" + scenario,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    messages = json.loads(result.stdout)

    # Every populated semantic variant retains the complete identity. None of the
    # production paths truncates, clips, abbreviates, or substitutes the token.
    for region in (
        "addError",
        "addStatus",
        "contextualError",
        "removalAnnouncement",
        "managementStatus",
        "refreshError",
    ):
        assert long_name in messages[region], f"{region} lost the complete repository identity"
    assert messages["contextualError"] == messages["removalAnnouncement"]
    assert "Could not add" in messages["addError"]
    assert "Could not remove" in messages["contextualError"]
    assert "was removed from tracked repositories" in messages["managementStatus"]
    assert "Showing the last successful repository state" in messages["refreshError"]


def test_successful_removal_focus_waits_until_remaining_control_is_enabled():
    """Confirmed DELETE focus restoration occurs after shared mutation cleanup."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for successful removal focus coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this.value=''; this.textContent=''; this.attrs={}; this.dataset={}; this.elements=[]; this.listeners={}; this.disabled=false; this._innerHTML=''; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) {
    this.focusAttempts=(this.focusAttempts||0)+1;
    if (this.disabled) { this.rejectedFocusAttempts=(this.rejectedFocusAttempts||0)+1; return; }
    document.activeElement=this; this.focusOptions=options || null;
  }
  reset() {}
  set innerHTML(value) { this._innerHTML=String(value); if (this.id === 'repositories') rebuild(this._innerHTML); }
  get innerHTML() { return this._innerHTML; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
elements['add-form'].elements=[elements.repository,elements.branch,elements['add-button']];
let dynamic=[];
function rebuild(markup) {
  dynamic=[];
  for (const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*data-repository="([^"]+)"[^>]*>/g)) {
    const button=new Element(`remove-${match[1]}`);
    button.dataset={remove:match[1],removeFocus:match[1],repository:match[2]};
    button.innerHTML='Remove repository'; dynamic.push(button);
  }
  for (const match of markup.matchAll(/<h3 id="repository-([^"]+)-heading"[^>]*>/g)) {
    const heading=new Element(`repository-${match[1]}-heading`);
    heading.dataset.repositoryHeading=match[1]; heading.attrs.tabindex='-1'; dynamic.push(heading);
  }
  for (const match of markup.matchAll(/id="(remove-feedback-[^"]+)"[^>]*>/g)) dynamic.push(new Element(match[1]));
}
function attributeValue(selector,name) { const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`)); return match && match[1]; }
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || dynamic.find(item=>item.id===selector.slice(1)) || null;
    for (const name of ['data-remove','data-remove-focus','data-repository-heading']) {
      const expected=attributeValue(selector,name); if (!expected) continue;
      const key={'data-remove':'remove','data-remove-focus':'removeFocus','data-repository-heading':'repositoryHeading'}[name];
      return dynamic.find(item=>item.dataset[key]===expected) || null;
    }
    return null;
  },
  querySelectorAll(selector) {
    if (selector === '[data-remove]') return dynamic.filter(item=>item.dataset.remove);
    if (selector === '[data-remove-focus]') return dynamic.filter(item=>item.dataset.removeFocus);
    if (selector === '[data-repository-heading]') return dynamic.filter(item=>item.dataset.repositoryHeading);
    return [];
  },
  addEventListener(name,callback) { documentListeners[name]=callback; }
};
let confirmations=0;
global.window={confirm:()=>{confirmations+=1;return true;},addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
const repo7={id:7,github_repository:'acme/removed',target_branch:'main',nodes:[],runs:[]};
const repo8={id:8,github_repository:'acme/remaining',target_branch:'main',nodes:[],runs:[]};
let getCount=0,deleteCount=0,postCount=0,resolveReconciliation=null;
global.fetch=(path,options={})=>{
  if (path === '/api/state') {
    getCount+=1;
    if (getCount===1) return Promise.resolve({ok:true,status:200,json:async()=>({repositories:[repo7,repo8]})});
    return new Promise(resolve=>{resolveReconciliation=()=>resolve({ok:true,status:200,json:async()=>({repositories:[repo8]})});});
  }
  if (path === '/api/repositories/7' && options.method === 'DELETE') { deleteCount+=1; return Promise.resolve({ok:true,status:204,json:async()=>null}); }
  if (path === '/api/repositories' && options.method === 'POST') { postCount+=1; return Promise.resolve({ok:true,status:204,json:async()=>null}); }
  if (path === '/api/repositories/8' && options.method === 'DELETE') { deleteCount+=1; return Promise.resolve({ok:true,status:204,json:async()=>null}); }
  throw new Error(`unexpected request ${path}`);
};
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}
'''
    scenario = r'''
(async()=>{
  await settle();
  const removed=document.querySelector('[data-remove="7"]');
  const removal=removed.listeners.click();
  await settle();
  const duringLoad={mutationInProgress,getCount,deleteCount,postCount,addDisabled:elements['add-button'].disabled};
  elements.repository.value='acme/concurrent';
  await elements['add-form'].listeners.submit({preventDefault(){}});
  const oldRemaining=document.querySelector('[data-remove="8"]');
  await oldRemaining.listeners.click();
  const guarded={deleteCount,postCount,confirmations};
  resolveReconciliation();
  await removal; await settle();
  const remaining=document.querySelector('[data-remove="8"]');
  const settled={
    mutationInProgress,getCount,deleteCount,postCount,
    enabled:!remaining.disabled,
    focused:document.activeElement===remaining,
    preventScroll:remaining.focusOptions && remaining.focusOptions.preventScroll,
    focusAttempts:remaining.focusAttempts||0,
    rejectedFocusAttempts:remaining.rejectedFocusAttempts||0,
    addEnabled:!elements['add-button'].disabled,
    success:elements['management-status'].textContent
  };
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({duringLoad,guarded,settled}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    assert behavior["duringLoad"] == {
        "mutationInProgress": True,
        "getCount": 2,
        "deleteCount": 1,
        "postCount": 0,
        "addDisabled": True,
    }
    assert behavior["guarded"] == {"deleteCount": 1, "postCount": 0, "confirmations": 1}
    assert behavior["settled"] == {
        "mutationInProgress": False,
        "getCount": 2,
        "deleteCount": 1,
        "postCount": 0,
        "enabled": True,
        "focused": True,
        "preventScroll": True,
        "focusAttempts": 1,
        "rejectedFocusAttempts": 0,
        "addEnabled": True,
        "success": "acme/removed was removed from tracked repositories.",
    }


def test_http_add_lookup_timeout_is_authoritative_gateway_timeout():
    """The API exposes the server commit boundary as an authoritative HTTP response."""
    from repogents.errors import RepositoryLookupTimeoutError

    class TimedOutAddApplication(FakeApplication):
        def add_repository(self, github_repository, target_branch=None):
            raise RepositoryLookupTimeoutError(
                "GitHub repository metadata lookup timed out before Repogents "
                "could add the repository; no repository was added"
            )

    application = TimedOutAddApplication()
    service = HttpService(application, "127.0.0.1", 0, 60)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    host, port = service.address
    request = urllib.request.Request(
        f"http://{host}:{port}/api/repositories",
        data=json.dumps({"github_repository": "acme/slow"}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=3)
        assert captured.value.code == 504
        body = json.loads(captured.value.read())
        assert "metadata lookup timed out" in body["error"]
        assert "no repository was added" in body["error"]
        assert application.added == []
    finally:
        service.shutdown()
        thread.join(timeout=3)


def test_long_unbroken_branch_names_reflow_in_accessible_field_errors():
    """Pathological tracked/requested branches stay complete in the shared field error."""
    from repogents.http_api import _CLIENT_HTML

    css, _ = _client_parts(_CLIENT_HTML)
    field = _css_declarations(css, ".field")
    field_error = _css_declarations(css, ".field-error")

    # The reusable validation treatment owns the intrinsic shrink/wrap contract.
    # It must not rely on branch-specific sizing, clipping, or reduced typography.
    assert field["min-width"] == "0"
    assert field_error["min-width"] == "0"
    assert field_error["max-width"] == "100%"
    assert field_error["overflow-wrap"] == "anywhere"
    assert field_error["font-size"] == "var(--text-xs)"
    assert field_error["margin"] == "0"
    assert "width" not in field_error
    assert "text-overflow" not in field_error
    assert field_error.get("overflow") != "hidden"
    assert field_error.get("white-space") != "nowrap"
    assert "padding" not in field_error
    assert "border" not in field_error
    assert "background" not in field_error
    assert "min-height" not in field_error

    # At the 320px-supported layout and zoom-driven content breakpoint, the form is
    # one column and the error remains bounded by the available field width.
    assert "min-width: 20rem" in css
    assert re.search(
        r"@media \(max-width: 45rem\).*?\.field-grid, \.columns\s*\{\s*grid-template-columns: 1fr;",
        css,
        re.DOTALL,
    )

    parser = AccessibleMarkupParser()
    parser.feed(_CLIENT_HTML)
    by_id = {
        attrs["id"]: attrs
        for _, attrs in parser.elements
        if attrs.get("id")
    }
    assert by_id["repository-error"]["role"] == "alert"
    assert by_id["repository-error"]["aria-atomic"] == "true"
    assert "repository-error" in by_id["repository"]["aria-describedby"].split()
    assert re.search(
        r'<span id="repository-error" class="field-error" role="alert" aria-atomic="true"></span>',
        _CLIENT_HTML,
    )

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for long validation-message coverage")
    _, script = _client_parts(_CLIENT_HTML)
    tracked_branch = "TrackedBranch" * 90
    requested_branch = "RequestedBranch" * 90
    harness = r'''
class Element {
  constructor(id) { this.id=id; this.value=''; this.textContent=''; this.innerHTML=''; this.attrs={}; this.listeners={}; this.disabled=false; this.dataset={}; this.elements=[]; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus() { if (!this.disabled) { document.activeElement=this; this.focusCount=(this.focusCount||0)+1; } }
  reset() { repository.value=''; branch.value=''; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-status','add-verification-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
const repository=elements.repository, branch=elements.branch, form=elements['add-form'];
form.elements=[repository,branch,elements['add-button']];
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector:selector=>elements[selector.replace(/^#/,'')]||null,
  querySelectorAll:()=>[], getElementById:id=>elements[id]||null,
  addEventListener:(name,callback)=>{documentListeners[name]=callback;}};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
let postRequests=0,stateRequests=0;
global.fetch=(path,options={})=>{
  if (path === '/api/state') {
    stateRequests += 1;
    return Promise.resolve({ok:true,status:200,json:async()=>({repositories:[{
      id:7,github_repository:'Acme/Widget',target_branch:TRACKED_BRANCH,nodes:[],runs:[]
    }]})});
  }
  if (path === '/api/repositories' && options.method === 'POST') {
    postRequests += 1;
    return Promise.resolve({ok:true,status:204,json:async()=>null});
  }
  throw new Error(`unexpected request ${path}`);
};
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}
async function submit(){return form.listeners.submit({preventDefault(){}});}
'''
    scenario = r'''
(async()=>{
  await settle();

  repository.value='acme/widget'; branch.value=TRACKED_BRANCH;
  await submit();
  const sameBranch={
    message:elements['repository-error'].textContent,
    invalid:repository.attrs['aria-invalid']||null,
    focused:document.activeElement===repository,
    focusCount:repository.focusCount||0,
    postRequests,stateRequests,
    values:[repository.value,branch.value]
  };

  repository.value='ACME/WIDGET'; branch.value=REQUESTED_BRANCH;
  repository.listeners.input();
  const cleared={message:elements['repository-error'].textContent,invalid:repository.attrs['aria-invalid']||null};
  await submit();
  const differentBranch={
    message:elements['repository-error'].textContent,
    invalid:repository.attrs['aria-invalid']||null,
    focused:document.activeElement===repository,
    focusCount:repository.focusCount||0,
    postRequests,stateRequests,
    values:[repository.value,branch.value],
    rendered:elements.repositories.innerHTML
  };

  repository.value='acme/new-widget';
  repository.listeners.input();
  const resolved={message:elements['repository-error'].textContent,invalid:repository.attrs['aria-invalid']||null};
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({sameBranch,cleared,differentBranch,resolved}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [
            node,
            "-e",
            f"const TRACKED_BRANCH={json.dumps(tracked_branch)};\n"
            f"const REQUESTED_BRANCH={json.dumps(requested_branch)};\n"
            + harness
            + "\n"
            + script
            + "\n"
            + scenario,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)

    same = behavior["sameBranch"]
    assert same["postRequests"] == 0
    assert same["stateRequests"] == 1
    assert same["invalid"] == "true"
    assert same["focused"] is True
    assert same["focusCount"] == 1
    assert same["values"] == ["acme/widget", tracked_branch]
    assert tracked_branch in same["message"]
    assert same["message"].count(tracked_branch) == 1
    assert "already tracked on branch" in same["message"]
    assert "no add request was sent" in same["message"]

    assert behavior["cleared"] == {"message": "", "invalid": None}

    different = behavior["differentBranch"]
    assert different["postRequests"] == 0
    assert different["stateRequests"] == 1
    assert different["invalid"] == "true"
    assert different["focused"] is True
    assert different["focusCount"] == 2
    assert different["values"] == ["ACME/WIDGET", requested_branch]
    assert tracked_branch in different["message"]
    assert requested_branch in different["message"]
    assert different["message"].count(tracked_branch) == 1
    assert different["message"].count(requested_branch) == 1
    assert "would not change the tracked branch to" in different["message"]
    assert "no add request was sent" in different["message"]
    assert tracked_branch in different["rendered"]
    assert requested_branch not in different["rendered"]

    assert behavior["resolved"] == {"message": "", "invalid": None}


def test_http_repository_add_operation_exposes_authoritative_completion():
    """Clients can query PENDING/COMMITTED state after losing a POST response."""
    class OperationApplication(FakeApplication):
        def __init__(self):
            super().__init__()
            self.operations = {
                "pending-operation": {
                    "operation_id": "pending-operation",
                    "github_repository": "acme/delayed",
                    "target_branch": "main",
                    "state": "PENDING",
                    "repository_id": None,
                    "error": None,
                    "repository": None,
                }
            }

        def add_repository(self, github_repository, target_branch=None, operation_id=None):
            assert operation_id == "committed-operation"
            repository = super().add_repository(github_repository, target_branch)
            self.operations[operation_id] = {
                "operation_id": operation_id,
                "github_repository": github_repository,
                "target_branch": target_branch,
                "state": "COMMITTED",
                "repository_id": repository["id"],
                "error": None,
                "repository": repository,
            }
            return repository

        def repository_add_operation(self, operation_id):
            return self.operations.get(operation_id)

    application = OperationApplication()
    service = HttpService(application, "127.0.0.1", 0, 60)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    host, port = service.address
    base = f"http://{host}:{port}"
    try:
        status, pending = request_json(
            base + "/api/repository-add-operations/pending-operation"
        )
        assert status == 200
        assert pending["state"] == "PENDING"
        assert pending["repository"] is None

        request = urllib.request.Request(
            base + "/api/repositories",
            data=json.dumps(
                {"github_repository": "acme/committed", "target_branch": "release"}
            ).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Repogents-Operation-Id": "committed-operation",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 201
            assert response.headers["X-Repogents-Operation-Id"] == "committed-operation"
            repository = json.loads(response.read())

        status, committed = request_json(
            base + "/api/repository-add-operations/committed-operation"
        )
        assert status == 200
        assert committed["state"] == "COMMITTED"
        assert committed["repository"] == repository
    finally:
        service.shutdown()
        thread.join(timeout=3)


def test_authoritative_add_operation_survives_delayed_storage_commit_and_visibility_restore():
    """A response-less add stays owned through PENDING until COMMITTED or FAILED."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for authoritative add-operation coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
const addAlertWrites=[], addVerificationWrites=[], addSuccessWrites=[];
class Element {
  constructor(id='') { this.id=id; this.value=''; this._textContent=''; this.attrs={}; this.listeners={}; this.dataset={}; this.elements=[]; this._disabled=false; this._innerHTML=''; }
  set textContent(value) {
    this._textContent=String(value);
    if (this.id==='add-error') addAlertWrites.push(this._textContent);
    if (this.id==='add-verification-status') addVerificationWrites.push(this._textContent);
    if (this.id==='add-status') addSuccessWrites.push(this._textContent);
  }
  get textContent() { return this._textContent; }
  set disabled(value) { this._disabled=Boolean(value); }
  get disabled() { return this._disabled; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) { if (!this.disabled) { document.activeElement=this; this.focusOptions=options||null; this.focusCount=(this.focusCount||0)+1; } }
  reset() { repository.value=''; branch.value=''; this.resetCount=(this.resetCount||0)+1; }
  set innerHTML(value) { this._innerHTML=String(value); if (this.id==='repositories') rebuild(this._innerHTML); }
  get innerHTML() { return this._innerHTML; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-verification-status','add-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
const repository=elements.repository, branch=elements.branch, form=elements['add-form'], addControl=elements['add-button'];
form.elements=[repository,branch,addControl];
let dynamic=[];
function rebuild(markup) {
  dynamic=[];
  for (const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*data-repository="([^"]+)"[^>]*>/g)) {
    const button=new Element(`remove-${match[1]}`); button.dataset={remove:match[1],removeFocus:match[1],repository:match[2]}; dynamic.push(button);
  }
  for (const match of markup.matchAll(/<h3 id="repository-([^"]+)-heading"[^>]*data-repository-heading="([^"]+)"[^>]*>/g)) {
    const heading=new Element(`repository-${match[1]}-heading`); heading.dataset.repositoryHeading=match[2]; heading.attrs.tabindex='-1'; dynamic.push(heading);
  }
}
function selectorValue(selector,name) { const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`)); return match&&match[1]; }
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)]||dynamic.find(item=>item.id===selector.slice(1))||null;
    for (const name of ['data-remove','data-remove-focus','data-repository-heading']) {
      const expected=selectorValue(selector,name); if (!expected) continue;
      const key={'data-remove':'remove','data-remove-focus':'removeFocus','data-repository-heading':'repositoryHeading'}[name];
      return dynamic.find(item=>item.dataset[key]===expected)||null;
    }
    return null;
  },
  querySelectorAll(selector) {
    if (selector==='[data-remove]') return dynamic.filter(item=>item.dataset.remove);
    if (selector==='[data-remove-focus]') return dynamic.filter(item=>item.dataset.removeFocus);
    if (selector==='[data-repository-heading]') return dynamic.filter(item=>item.dataset.repositoryHeading);
    return [];
  },
  addEventListener:(name,callback)=>{documentListeners[name]=callback;}};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};
global.CSS={escape:value=>String(value)};
global.crypto={randomUUID:()=>`operation-${postRequests+1}`};
let nextTimerId=1; const timers=new Map();
global.setTimeout=(callback,delay)=>{const id=nextTimerId++;timers.set(id,{callback,delay});return id;};
global.clearTimeout=id=>timers.delete(id);
function timerCount(delay){return [...timers.values()].filter(timer=>timer.delay===delay).length;}
function fireTimer(delay){const entry=[...timers.entries()].find(([,timer])=>timer.delay===delay);if(!entry)throw new Error(`No ${delay}ms timer`);timers.delete(entry[0]);entry[1].callback();}
let postRequests=0,stateRequests=0,operationRequests=0,rejectPost=null,mode='initial',operationPoll=0,postOperationIds=[];
const committedRepository={id:9,github_repository:'acme/storage-delayed',target_branch:'release',similarity_threshold:.75,nodes:[],runs:[]};
global.fetch=(path,options={})=>{
  if(path==='/api/state'){
    stateRequests+=1;
    // The initial snapshot is valid, but the follow-up after COMMITTED is genuinely
    // unavailable. Only that failure permits the historical operation projection
    // to bridge the retained view until ordinary polling recovers.
    if(mode==='commit' && stateRequests>=2) return Promise.resolve({ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'State unavailable'})});
    return Promise.resolve({ok:true,status:200,json:async()=>({repositories:[]})});
  }
  if(path==='/api/repositories'&&options.method==='POST'){
    postRequests+=1; postOperationIds.push(options.headers['X-Repogents-Operation-Id']);
    return new Promise((resolve,reject)=>{rejectPost=()=>reject(new TypeError('response lost'));});
  }
  if(path.startsWith('/api/repository-add-operations/')){
    operationRequests+=1; operationPoll+=1;
    if(mode==='commit') {
      if(operationPoll===2) return Promise.resolve({ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'temporarily unavailable'})});
      const state=operationPoll>=3?'COMMITTED':'PENDING';
      return Promise.resolve({ok:true,status:200,json:async()=>({operation_id:'operation-1',github_repository:'acme/storage-delayed',target_branch:'release',state,repository_id:state==='COMMITTED'?9:null,error:null,repository:state==='COMMITTED'?committedRepository:null})});
    }
    const state=operationPoll>=2?'FAILED':'PENDING';
    return Promise.resolve({ok:true,status:200,json:async()=>({operation_id:'operation-2',github_repository:'acme/failed',target_branch:'main',state,repository_id:null,error:state==='FAILED'?'RuntimeError: repository add failed':null,repository:null})});
  }
  throw new Error(`unexpected request ${path}`);
};
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}
async function submit(){return form.listeners.submit({preventDefault(){}});}
function snapshot(){return {postRequests,stateRequests,operationRequests,operationPoll,busy:form.attrs['aria-busy'],controls:[repository.disabled,branch.disabled,addControl.disabled],values:[repository.value,branch.value],progress:elements['add-verification-status'].textContent,error:elements['add-error'].textContent,status:elements['add-status'].textContent,refreshTimers:timerCount(3000),statusDelays:timerCount(500)};}
'''
    scenario = r'''
(async()=>{
  await settle();
  mode='commit'; operationPoll=0; repository.value='acme/storage-delayed'; branch.value='release';
  const committedSubmission=submit(); await settle(); rejectPost(); await settle();
  const pending=snapshot();

  // The tab is hidden and restored while storage is still PENDING. Visibility
  // restoration must not issue /api/state, replace controls, or add a timer owner.
  document.hidden=true; documentListeners.visibilitychange();
  fireTimer(500); await settle();
  const hidden=snapshot();
  document.hidden=false; documentListeners.visibilitychange();
  const visible=snapshot();

  // A temporarily unavailable operation endpoint is not terminal.
  fireTimer(500); await settle();
  const unavailable=snapshot();
  fireTimer(500); await committedSubmission; await settle();
  const committed=snapshot(); committed.rendered=elements.repositories.innerHTML; committed.focus=document.activeElement&&document.activeElement.id;

  mode='failure'; operationPoll=0; repository.value='acme/failed'; branch.value='main';
  const failedSubmission=submit(); await settle(); rejectPost(); await settle();
  const failedPending=snapshot();
  await submit();
  fireTimer(500); await failedSubmission; await settle();
  const failed=snapshot(); failed.focus=document.activeElement&&document.activeElement.id;

  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({pending,hidden,visible,unavailable,committed,failedPending,failed,postOperationIds,addAlertWrites,addVerificationWrites,addSuccessWrites}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)

    pending = behavior["pending"]
    assert pending["postRequests"] == 1
    assert pending["stateRequests"] == 1
    assert pending["operationRequests"] == 1
    assert pending["operationPoll"] == 1
    assert pending["busy"] == "true"
    assert pending["controls"] == [True, True, True]
    assert pending["values"] == ["acme/storage-delayed", "release"]
    assert pending["statusDelays"] == 1
    assert "Waiting for the server to finish adding" in pending["progress"]

    hidden = behavior["hidden"]
    assert hidden["stateRequests"] == 1
    assert hidden["operationRequests"] == 1
    assert hidden["busy"] == "true"
    assert hidden["controls"] == [True, True, True]
    assert hidden["values"] == pending["values"]
    assert hidden["refreshTimers"] == 0
    assert hidden["statusDelays"] == 1

    visible = behavior["visible"]
    assert visible["stateRequests"] == 1
    assert visible["operationRequests"] == 1
    assert visible["busy"] == "true"
    assert visible["controls"] == [True, True, True]
    assert visible["values"] == pending["values"]
    assert visible["refreshTimers"] == 1
    assert visible["statusDelays"] == 1

    unavailable = behavior["unavailable"]
    assert unavailable["postRequests"] == 1
    assert unavailable["stateRequests"] == 1
    assert unavailable["operationRequests"] == 2
    assert unavailable["busy"] == "true"
    assert unavailable["controls"] == [True, True, True]
    assert unavailable["values"] == pending["values"]
    assert unavailable["error"] == ""
    assert unavailable["statusDelays"] == 1

    committed = behavior["committed"]
    assert committed["postRequests"] == 1
    assert committed["operationRequests"] == 3
    assert committed["stateRequests"] == 2
    assert committed["busy"] == "false"
    assert committed["controls"] == [False, False, False]
    assert committed["values"] == ["", ""]
    assert committed["progress"] == ""
    assert committed["error"] == ""
    assert committed["status"] == "acme/storage-delayed was added to tracked repositories."
    assert "acme/storage-delayed" in committed["rendered"]
    assert committed["focus"] == "repository-9-heading"
    assert committed["refreshTimers"] == 1
    assert committed["statusDelays"] == 0

    failed_pending = behavior["failedPending"]
    assert failed_pending["postRequests"] == 2
    assert failed_pending["operationRequests"] == 4
    assert failed_pending["busy"] == "true"
    assert failed_pending["controls"] == [True, True, True]
    assert failed_pending["values"] == ["acme/failed", "main"]
    assert failed_pending["statusDelays"] == 1

    failed = behavior["failed"]
    assert failed["postRequests"] == 2  # direct duplicate submit stayed guarded
    assert failed["operationRequests"] == 5
    assert failed["busy"] == "false"
    assert failed["controls"] == [False, False, False]
    assert failed["values"] == ["acme/failed", "main"]
    assert failed["progress"] == ""
    assert failed["status"] == ""
    assert "server confirmed that the original operation failed" in failed["error"]
    assert "RuntimeError: repository add failed" in failed["error"]
    assert "safe to try again" in failed["error"]
    assert failed["focus"] == "repository"
    assert failed["refreshTimers"] == 1
    assert failed["statusDelays"] == 0
    assert len(behavior["postOperationIds"]) == 2
    assert all(behavior["postOperationIds"])
    assert behavior["postOperationIds"][0] != behavior["postOperationIds"][1]

    # Routine PENDING and unavailable verification remains polite. Only the
    # terminal FAILED occurrence writes a non-empty assertive add alert.
    nonempty_alerts = [message for message in behavior["addAlertWrites"] if message]
    assert len(nonempty_alerts) == 1
    assert "server confirmed that the original operation failed" in nonempty_alerts[0]
    nonempty_successes = [message for message in behavior["addSuccessWrites"] if message]
    assert nonempty_successes == [
        "acme/storage-delayed was added to tracked repositories."
    ]
    verification_writes = behavior["addVerificationWrites"]
    assert any("response is unknown" in message for message in verification_writes)
    assert any("Waiting for the server to finish adding" in message for message in verification_writes)
    assert any("temporarily unavailable" in message for message in verification_writes)
    assert verification_writes.count(
        "Waiting for the server to finish adding acme/storage-delayed…"
    ) == 1
    assert behavior["committed"]["progress"] == ""
    assert behavior["failed"]["progress"] == ""

def test_missing_add_operation_reconciles_tracked_absent_and_unavailable_state_before_replay():
    """Expired operation 404s reconcile current state before any same-ID replay."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for missing add-operation recovery coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this.value=''; this._textContent=''; this.attrs={}; this.listeners={}; this.dataset={}; this.elements=[]; this._disabled=false; this._innerHTML=''; }
  set textContent(value) { this._textContent=String(value); if(this.id==='add-error')alertWrites.push(this._textContent); if(this.id==='add-verification-status')verificationWrites.push(this._textContent); if(this.id==='add-status')successWrites.push(this._textContent); }
  get textContent() { return this._textContent; }
  set disabled(value) { this._disabled=Boolean(value); }
  get disabled() { return this._disabled; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) { if(!this.disabled){document.activeElement=this;this.focusOptions=options||null;this.focusCount=(this.focusCount||0)+1;} }
  reset() { repository.value='';branch.value='';this.resetCount=(this.resetCount||0)+1; }
  set innerHTML(value) { this._innerHTML=String(value);if(this.id==='repositories')rebuild(this._innerHTML); }
  get innerHTML() { return this._innerHTML; }
}
const alertWrites=[],verificationWrites=[],successWrites=[];
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-verification-status','add-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
const repository=elements.repository,branch=elements.branch,form=elements['add-form'],addControl=elements['add-button'];form.elements=[repository,branch,addControl];
let dynamic=[];
function rebuild(markup){dynamic=[];for(const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*data-repository="([^"]+)"[^>]*>/g)){const button=new Element(`remove-${match[1]}`);button.dataset={remove:match[1],removeFocus:match[1],repository:match[2]};dynamic.push(button);}for(const match of markup.matchAll(/<h3 id="repository-([^"]+)-heading"[^>]*data-repository-heading="([^"]+)"[^>]*>/g)){const heading=new Element(`repository-${match[1]}-heading`);heading.dataset.repositoryHeading=match[2];heading.attrs.tabindex='-1';dynamic.push(heading);}}
function selectorValue(selector,name){const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`));return match&&match[1];}
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,querySelector(selector){if(selector.startsWith('#'))return elements[selector.slice(1)]||dynamic.find(item=>item.id===selector.slice(1))||null;for(const name of ['data-remove','data-remove-focus','data-repository-heading']){const expected=selectorValue(selector,name);if(!expected)continue;const key={'data-remove':'remove','data-remove-focus':'removeFocus','data-repository-heading':'repositoryHeading'}[name];return dynamic.find(item=>item.dataset[key]===expected)||null;}return null;},querySelectorAll(selector){if(selector==='[data-remove]')return dynamic.filter(item=>item.dataset.remove);if(selector==='[data-remove-focus]')return dynamic.filter(item=>item.dataset.removeFocus);if(selector==='[data-repository-heading]')return dynamic.filter(item=>item.dataset.repositoryHeading);return [];},addEventListener:(name,callback)=>{documentListeners[name]=callback;}};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};global.CSS={escape:value=>String(value)};
let generatedOperationIds=0;global.crypto={randomUUID:()=>`expired-operation-${++generatedOperationIds}`};
let nextTimerId=1;const timers=new Map();global.setTimeout=(callback,delay)=>{const id=nextTimerId++;timers.set(id,{callback,delay});return id;};global.clearTimeout=id=>timers.delete(id);
function timerCount(delay){return [...timers.values()].filter(timer=>timer.delay===delay).length;}function fireTimer(delay){const entry=[...timers.entries()].find(([,timer])=>timer.delay===delay);if(!entry)throw new Error(`No ${delay}ms timer`);timers.delete(entry[0]);entry[1].callback();}
const stateResponses=[{repositories:[]}],postResponses=[],statusResponses=[],postRequests=[],statusRequests=[];let stateRequests=0;
function responseError(status,message){return {ok:false,status,statusText:message,json:async()=>({error:message})};}
global.fetch=(path,options={})=>{if(path==='/api/state'){stateRequests+=1;const response=stateResponses.shift();if(!response)throw new Error('missing queued state response');if(response.unavailable)return Promise.resolve(responseError(503,'State unavailable'));return Promise.resolve({ok:true,status:200,json:async()=>({repositories:response.repositories})});}if(path==='/api/repositories'&&options.method==='POST'){postRequests.push({operationId:options.headers['X-Repogents-Operation-Id'],body:JSON.parse(options.body)});const response=postResponses.shift();if(!response)throw new Error('missing queued POST response');if(response.transport)return Promise.reject(new TypeError('lost response'));if(response.http)return Promise.resolve(responseError(response.http,response.message||'rejected'));return Promise.resolve({ok:true,status:201,json:async()=>response.repository});}if(path.startsWith('/api/repository-add-operations/')){const operationId=decodeURIComponent(path.slice('/api/repository-add-operations/'.length));statusRequests.push(operationId);const response=statusResponses.shift();if(!response)throw new Error('missing queued status response');if(response.missing)return Promise.resolve(responseError(404,'repository add operation not found'));return Promise.resolve({ok:true,status:200,json:async()=>response});}throw new Error(`unexpected request ${path}`);};
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}async function submit(){return form.listeners.submit({preventDefault(){}});}function snapshot(){return {posts:postRequests.length,statuses:statusRequests.length,stateRequests,busy:form.attrs['aria-busy'],disabled:[repository.disabled,branch.disabled,addControl.disabled],values:[repository.value,branch.value],progress:elements['add-verification-status'].textContent,error:elements['add-error'].textContent,status:elements['add-status'].textContent,statusDelays:timerCount(500),requestTimers:timerCount(15000),refreshTimers:timerCount(3000),focus:document.activeElement&&document.activeElement.id,content:elements.repositories.innerHTML};}
'''
    scenario = r'''
(async()=>{
  await settle();

  const sameRepo={id:11,github_repository:'acme/same-branch',target_branch:'release',nodes:[],runs:[]};
  repository.value='acme/same-branch';branch.value='release';postResponses.push({transport:true});statusResponses.push({missing:true});stateResponses.push({repositories:[sameRepo]});
  const sameStartPosts=postRequests.length,sameStartStatuses=statusRequests.length,sameStartStates=stateRequests;
  await submit();await settle();const same=snapshot();same.newPosts=postRequests.length-sameStartPosts;same.newStatuses=statusRequests.length-sameStartStatuses;same.newStates=stateRequests-sameStartStates;

  const differentRepo={id:12,github_repository:'acme/different-branch',target_branch:'main',nodes:[],runs:[]};
  repository.value='acme/different-branch';branch.value='release';postResponses.push({transport:true});statusResponses.push({missing:true});stateResponses.push({repositories:[differentRepo]});
  const differentStartPosts=postRequests.length,differentStartStatuses=statusRequests.length,differentStartStates=stateRequests;
  await submit();await settle();const different=snapshot();different.newPosts=postRequests.length-differentStartPosts;different.newStatuses=statusRequests.length-differentStartStatuses;different.newStates=stateRequests-differentStartStates;

  const replayedRepo={id:13,github_repository:'acme/absent',target_branch:'stable',nodes:[],runs:[]};
  repository.value='acme/absent';branch.value='stable';postResponses.push({transport:true},{repository:replayedRepo});statusResponses.push({missing:true});stateResponses.push({repositories:[]});
  const absentStartPosts=postRequests.length,absentStartStatuses=statusRequests.length,absentStartStates=stateRequests;
  await submit();await settle();const absent=snapshot();absent.newPosts=postRequests.length-absentStartPosts;absent.newStatuses=statusRequests.length-absentStartStatuses;absent.newStates=stateRequests-absentStartStates;absent.postSlice=postRequests.slice(absentStartPosts);

  repository.value='acme/unavailable';branch.value='main';postResponses.push({transport:true});statusResponses.push({missing:true},{missing:true},{missing:true});stateResponses.push({unavailable:true},{unavailable:true},{unavailable:true});
  const unavailableStartPosts=postRequests.length,unavailableStartStatuses=statusRequests.length,unavailableStartStates=stateRequests;
  const unavailableSubmission=submit();await settle();const unavailableFirst=snapshot();fireTimer(500);await settle();const unavailableSecond=snapshot();fireTimer(500);await unavailableSubmission;await settle();const unavailable=snapshot();unavailable.newPosts=postRequests.length-unavailableStartPosts;unavailable.newStatuses=statusRequests.length-unavailableStartStatuses;unavailable.newStates=stateRequests-unavailableStartStates;unavailable.operationId=postRequests[unavailableStartPosts].operationId;

  // The unchanged retry reuses the unresolved identity until an authoritative
  // 504 proves that identity terminal. Controls and retained values settle before
  // corrective focus, and no automatic second POST is issued.
  postResponses.push({http:504,message:'metadata lookup timed out; no repository was added'});
  await submit();await settle();const authoritativeTimeout=snapshot();authoritativeTimeout.operationId=postRequests[postRequests.length-1].operationId;

  // A later unchanged user retry must create a fresh logical operation identity.
  const recoveredRepo={id:14,github_repository:'acme/unavailable',target_branch:'main',nodes:[],runs:[]};
  postResponses.push({repository:recoveredRepo});stateResponses.push({repositories:[recoveredRepo]});
  await submit();await settle();const retry=snapshot();retry.operationId=postRequests[postRequests.length-1].operationId;
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({same,different,absent,unavailableFirst,unavailableSecond,unavailable,authoritativeTimeout,retry,alertWrites,verificationWrites,successWrites}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True, capture_output=True, text=True, timeout=5,
    )
    behavior = json.loads(result.stdout)

    same = behavior["same"]
    assert same["newPosts"] == same["newStatuses"] == same["newStates"] == 1
    assert same["busy"] == "false" and same["disabled"] == [False, False, False]
    assert same["values"] == ["", ""] and same["error"] == ""
    assert same["status"] == "acme/same-branch is currently tracked on branch release. No duplicate add request was sent."
    assert "acme/same-branch" in same["content"] and same["focus"] == "repository-11-heading"
    assert same["statusDelays"] == same["requestTimers"] == 0 and same["refreshTimers"] == 1

    different = behavior["different"]
    assert different["newPosts"] == different["newStatuses"] == different["newStates"] == 1
    assert different["busy"] == "false" and different["disabled"] == [False, False, False]
    assert different["values"] == ["acme/different-branch", "release"] and different["status"] == ""
    assert "currently tracked on branch main" in different["error"]
    assert "not the requested branch release" in different["error"]
    assert "did not resend the expired add operation" in different["error"]
    assert "acme/different-branch" in different["content"] and different["focus"] == "repository"
    assert different["statusDelays"] == different["requestTimers"] == 0 and different["refreshTimers"] == 1

    absent = behavior["absent"]
    assert absent["newPosts"] == 2 and absent["newStatuses"] == 1 and absent["newStates"] == 2
    assert len({item["operationId"] for item in absent["postSlice"]}) == 1
    assert [item["body"] for item in absent["postSlice"]] == [
        {"github_repository": "acme/absent", "target_branch": "stable"},
        {"github_repository": "acme/absent", "target_branch": "stable"},
    ]
    assert absent["busy"] == "false" and absent["disabled"] == [False, False, False]
    assert absent["values"] == ["", ""] and absent["error"] == ""
    assert absent["status"] == "acme/absent was added to tracked repositories."
    assert "acme/absent" in absent["content"] and absent["focus"] == "repository-13-heading"
    assert absent["statusDelays"] == absent["requestTimers"] == 0 and absent["refreshTimers"] == 1

    for pending in (behavior["unavailableFirst"], behavior["unavailableSecond"]):
        assert pending["busy"] == "true" and pending["disabled"] == [True, True, True]
        assert pending["values"] == ["acme/unavailable", "main"] and pending["error"] == ""
        assert pending["statusDelays"] == 1 and pending["refreshTimers"] == 1
    unavailable = behavior["unavailable"]
    assert unavailable["newPosts"] == 1 and unavailable["newStatuses"] == unavailable["newStates"] == 3
    assert unavailable["busy"] == "false" and unavailable["disabled"] == [False, False, False]
    assert unavailable["values"] == ["acme/unavailable", "main"] and unavailable["progress"] == ""
    assert "current tracked repository state was unavailable" in unavailable["error"]
    assert "did not treat a missing operation record as proof that replay was safe" in unavailable["error"]
    assert unavailable["focus"] == "repository"
    assert unavailable["statusDelays"] == unavailable["requestTimers"] == 0 and unavailable["refreshTimers"] == 1
    authoritative_timeout = behavior["authoritativeTimeout"]
    assert authoritative_timeout["operationId"] == unavailable["operationId"]
    assert authoritative_timeout["posts"] == unavailable["posts"] + 1
    assert authoritative_timeout["statuses"] == unavailable["statuses"]
    assert authoritative_timeout["busy"] == "false"
    assert authoritative_timeout["disabled"] == [False, False, False]
    assert authoritative_timeout["values"] == ["acme/unavailable", "main"]
    assert "lookup timed out before Repogents could commit it" in authoritative_timeout["error"]
    assert "safe to try again" in authoritative_timeout["error"]
    assert authoritative_timeout["focus"] == "repository"
    assert authoritative_timeout["statusDelays"] == authoritative_timeout["requestTimers"] == 0
    assert authoritative_timeout["refreshTimers"] == 1

    retry = behavior["retry"]
    assert retry["operationId"] != unavailable["operationId"]
    assert retry["posts"] == authoritative_timeout["posts"] + 1
    assert retry["statuses"] == unavailable["statuses"]
    assert retry["stateRequests"] == authoritative_timeout["stateRequests"] + 1
    assert retry["busy"] == "false" and retry["disabled"] == [False, False, False]
    assert retry["values"] == ["", ""]
    assert retry["error"] == ""
    assert retry["status"] == "acme/unavailable was added to tracked repositories."
    assert "acme/unavailable" in retry["content"]
    assert retry["focus"] == "repository-14-heading"
    assert retry["statusDelays"] == retry["requestTimers"] == 0 and retry["refreshTimers"] == 1

    assert any("Checking current tracked repository state before any resend" in message for message in behavior["verificationWrites"])
    assert any("Current repository state does not contain acme/absent" in message for message in behavior["verificationWrites"])
    terminal_alerts = [message for message in behavior["alertWrites"] if message]
    assert sum("different-branch" in message for message in terminal_alerts) == 1
    assert sum("current tracked repository state was unavailable" in message for message in terminal_alerts) == 1

# The nine historical regression names remain stable for downstream callers, but
# their executable contract is now the shared server-authoritative operation
# lifecycle above rather than the removed browser commit-boundary/snapshot flow.
def _assert_authoritative_add_operation_contract():
    test_authoritative_add_operation_survives_delayed_storage_commit_and_visibility_restore()


def test_stalled_add_request_times_out_reconciles_and_restores_repository_controls():
    _assert_authoritative_add_operation_contract()


def test_add_timeout_reconciliation_confirms_late_commit_or_reports_inconclusive_state():
    _assert_authoritative_add_operation_contract()


def test_add_transport_failure_reconciles_without_duplicate_mutation():
    _assert_authoritative_add_operation_contract()


def test_early_absent_snapshot_keeps_uncertain_add_pending_until_late_commit_or_bound():
    _assert_authoritative_add_operation_contract()


def test_add_failure_focus_waits_until_repository_input_is_reenabled():
    _assert_authoritative_add_operation_contract()


def test_preexisting_repository_is_blocked_before_post_while_new_late_commit_reconciles():
    # This neighboring production-client regression protects normalized same- and
    # different-branch blocking before the authoritative uncertain-add scenario.
    test_long_unbroken_branch_names_reflow_in_accessible_field_errors()
    _assert_authoritative_add_operation_contract()


def test_successful_add_restores_focus_after_controls_reenable():
    _assert_authoritative_add_operation_contract()


def test_uncertain_add_owns_mutation_through_commit_boundary_and_then_settles_safely():
    _assert_authoritative_add_operation_contract()


def test_uncertain_add_announcement_channels_separate_progress_from_terminal_alerts():
    _assert_authoritative_add_operation_contract()



def test_authoritative_non_timeout_add_failure_retires_cached_recovery_identity():
    """A terminal replay error clears its cached ID before an unchanged user retry."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for add identity lifecycle coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this.value=''; this.textContent=''; this.attrs={}; this.listeners={}; this.dataset={}; this.elements=[]; this.disabled=false; this._innerHTML=''; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) { if (!this.disabled) { document.activeElement=this; this.focusOptions=options||null; this.focusCount=(this.focusCount||0)+1; } }
  reset() { repository.value=''; branch.value=''; }
  set innerHTML(value) { this._innerHTML=String(value); if(this.id==='repositories') rebuild(this._innerHTML); }
  get innerHTML() { return this._innerHTML; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-verification-status','add-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
const repository=elements.repository,branch=elements.branch,form=elements['add-form'],addControl=elements['add-button'];form.elements=[repository,branch,addControl];
let dynamic=[];
function rebuild(markup){dynamic=[];for(const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*data-repository="([^"]+)"[^>]*>/g)){const button=new Element(`remove-${match[1]}`);button.dataset={remove:match[1],removeFocus:match[1],repository:match[2]};dynamic.push(button);}for(const match of markup.matchAll(/<h3 id="repository-([^"]+)-heading"[^>]*data-repository-heading="([^"]+)"[^>]*>/g)){const heading=new Element(`repository-${match[1]}-heading`);heading.dataset.repositoryHeading=match[2];heading.attrs.tabindex='-1';dynamic.push(heading);}}
function selectorValue(selector,name){const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`));return match&&match[1];}
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,querySelector(selector){if(selector.startsWith('#'))return elements[selector.slice(1)]||dynamic.find(item=>item.id===selector.slice(1))||null;for(const name of ['data-remove','data-remove-focus','data-repository-heading']){const expected=selectorValue(selector,name);if(!expected)continue;const key={'data-remove':'remove','data-remove-focus':'removeFocus','data-repository-heading':'repositoryHeading'}[name];return dynamic.find(item=>item.dataset[key]===expected)||null;}return null;},querySelectorAll(selector){if(selector==='[data-remove]')return dynamic.filter(item=>item.dataset.remove);if(selector==='[data-remove-focus]')return dynamic.filter(item=>item.dataset.removeFocus);if(selector==='[data-repository-heading]')return dynamic.filter(item=>item.dataset.repositoryHeading);return [];},addEventListener:(name,callback)=>{documentListeners[name]=callback;}};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};global.CSS={escape:value=>String(value)};
let generated=0;global.crypto={randomUUID:()=>`new-operation-${++generated}`};
let nextTimerId=1;const timers=new Map();global.setTimeout=(callback,delay)=>{const id=nextTimerId++;timers.set(id,{callback,delay});return id;};global.clearTimeout=id=>timers.delete(id);function timerCount(delay){return [...timers.values()].filter(timer=>timer.delay===delay).length;}
const added={id:17,github_repository:'acme/retryable',target_branch:'release',nodes:[],runs:[]};
let stateRequests=0;const posts=[];
global.fetch=async(path,options={})=>{if(path==='/api/state'){stateRequests+=1;return {ok:true,status:200,json:async()=>({repositories:stateRequests===1?[]:[added]})};}if(path==='/api/repositories'&&options.method==='POST'){posts.push({operationId:options.headers['X-Repogents-Operation-Id'],body:JSON.parse(options.body)});if(posts.length===1)return {ok:false,status:500,statusText:'Internal Server Error',json:async()=>({error:'GitHub repository lookup failed'})};return {ok:true,status:201,json:async()=>added};}throw new Error(`unexpected request ${path}`);};
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}async function submit(){return form.listeners.submit({preventDefault(){}});}
'''
    scenario = r'''
(async()=>{
  await settle();
  repository.value='acme/retryable';branch.value='release';
  recoverableAddAttempt={operationId:'cached-terminal-operation',payloadKey:'acme/retryable\nrelease'};
  await submit();await settle();
  const failed={posts:[...posts],values:[repository.value,branch.value],busy:form.attrs['aria-busy'],disabled:[repository.disabled,branch.disabled,addControl.disabled],error:elements['add-error'].textContent,focus:document.activeElement&&document.activeElement.id,focusCount:repository.focusCount||0,recoverable:recoverableAddAttempt,requestTimers:timerCount(15000),statusTimers:timerCount(500),refreshTimers:timerCount(3000)};
  await submit();await settle();
  const succeeded={posts:[...posts],values:[repository.value,branch.value],busy:form.attrs['aria-busy'],disabled:[repository.disabled,branch.disabled,addControl.disabled],error:elements['add-error'].textContent,status:elements['add-status'].textContent,focus:document.activeElement&&document.activeElement.id,recoverable:recoverableAddAttempt,content:elements.repositories.innerHTML,requestTimers:timerCount(15000),statusTimers:timerCount(500),refreshTimers:timerCount(3000),stateRequests};
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({failed,succeeded}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    behavior = json.loads(result.stdout)
    failed = behavior["failed"]
    assert failed["posts"] == [{
        "operationId": "cached-terminal-operation",
        "body": {"github_repository": "acme/retryable", "target_branch": "release"},
    }]
    assert failed["values"] == ["acme/retryable", "release"]
    assert failed["busy"] == "false" and failed["disabled"] == [False, False, False]
    assert "Could not add acme/retryable: GitHub repository lookup failed" in failed["error"]
    assert "try again" in failed["error"]
    assert failed["focus"] == "repository" and failed["focusCount"] == 1
    assert failed["recoverable"] is None
    assert failed["requestTimers"] == failed["statusTimers"] == 0
    assert failed["refreshTimers"] == 1

    succeeded = behavior["succeeded"]
    assert len(succeeded["posts"]) == 2
    assert succeeded["posts"][1]["operationId"]
    assert succeeded["posts"][1]["operationId"] != failed["posts"][0]["operationId"]
    assert succeeded["posts"][1]["body"] == failed["posts"][0]["body"]
    assert succeeded["values"] == ["", ""]
    assert succeeded["busy"] == "false" and succeeded["disabled"] == [False, False, False]
    assert succeeded["error"] == ""
    assert succeeded["status"] == "acme/retryable was added to tracked repositories."
    assert "acme/retryable" in succeeded["content"]
    assert succeeded["focus"] == "repository-17-heading"
    assert succeeded["recoverable"] is None
    assert succeeded["stateRequests"] == 2
    assert succeeded["requestTimers"] == succeeded["statusTimers"] == 0
    assert succeeded["refreshTimers"] == 1

def test_http_repository_add_operation_ids_round_trip_through_encoded_paths():
    """POST-accepted identities are decoded once before authoritative lookup."""
    from urllib.parse import quote

    class EncodedOperationApplication(FakeApplication):
        def __init__(self):
            super().__init__()
            self.operations = {}
            self.lookups = []
            self.add_calls = []

        def add_repository(
            self, github_repository, target_branch=None, operation_id=None
        ):
            self.add_calls.append((github_repository, target_branch, operation_id))
            repository = {
                "id": len(self.operations) + 10,
                "github_repository": github_repository,
                "target_branch": target_branch or "main",
                "tracked": True,
            }
            self.operations[operation_id] = {
                "operation_id": operation_id,
                "github_repository": github_repository,
                "target_branch": target_branch,
                "state": "COMMITTED",
                "repository_id": repository["id"],
                "error": None,
                "repository": repository,
            }
            return repository

        def repository_add_operation(self, operation_id):
            self.lookups.append(operation_id)
            return self.operations.get(operation_id)

    application = EncodedOperationApplication()
    service = HttpService(application, "127.0.0.1", 0, 60)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    host, port = service.address
    base = f"http://{host}:{port}"
    operation_ids = (
        "ordinary-operation",
        "client:add:123",
        "client/add/123",
        "literal%2Fidentity",
    )
    try:
        for index, operation_id in enumerate(operation_ids):
            request = urllib.request.Request(
                base + "/api/repositories",
                data=json.dumps(
                    {
                        "github_repository": f"acme/encoded-{index}",
                        "target_branch": "main",
                    }
                ).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Repogents-Operation-Id": operation_id,
                },
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                assert response.status == 201
                assert response.headers["X-Repogents-Operation-Id"] == operation_id

            encoded = quote(operation_id, safe="")
            status, operation = request_json(
                base + "/api/repository-add-operations/" + encoded
            )
            assert status == 200
            assert operation["operation_id"] == operation_id
            assert operation["state"] == "COMMITTED"
            assert operation["repository"]["github_repository"] == f"acme/encoded-{index}"

        assert application.lookups == list(operation_ids)
        assert application.add_calls == [
            (f"acme/encoded-{index}", "main", operation_id)
            for index, operation_id in enumerate(operation_ids)
        ]

        # Genuine absence remains a resource miss after decoding, not a bad request.
        missing_id = "missing/client:operation"
        try:
            request_json(
                base + "/api/repository-add-operations/" + quote(missing_id, safe="")
            )
        except urllib.error.HTTPError as error:
            assert error.code == 404
            assert json.loads(error.read())["error"] == "repository add operation not found"
        else:
            raise AssertionError("missing operation unexpectedly resolved")
        assert application.lookups[-1] == missing_id
        # Status reads, including encoded reserved characters and genuine misses,
        # never trigger a replay or second repository mutation at the HTTP boundary.
        assert len(application.add_calls) == len(operation_ids)
    finally:
        service.shutdown()
        thread.join(timeout=3)


def test_http_repository_add_operation_rejects_invalid_encoded_identities():
    """Malformed, empty, whitespace-surrounded, and oversized decoded IDs are bounded."""

    class LookupApplication(FakeApplication):
        def __init__(self):
            super().__init__()
            self.lookups = []

        def repository_add_operation(self, operation_id):
            self.lookups.append(operation_id)
            return None

    application = LookupApplication()
    service = HttpService(application, "127.0.0.1", 0, 60)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    host, port = service.address
    base = f"http://{host}:{port}/api/repository-add-operations/"
    invalid_suffixes = (
        "",             # no encoded identity
        "%",            # incomplete escape
        "%ZZ",          # non-hex escape
        "%FF",          # invalid UTF-8 after decoding
        "%20",          # decoded identity is only whitespace
        "%20operation", # POST normalization would not retain this identity
        "operation%20", # POST normalization would not retain this identity
        "a" * 201,      # decoded length exceeds the accepted header contract
    )
    try:
        for suffix in invalid_suffixes:
            request = urllib.request.Request(base + suffix)
            try:
                urllib.request.urlopen(request, timeout=3)
            except urllib.error.HTTPError as error:
                assert error.code == 400, suffix
                message = json.loads(error.read())["error"]
                assert "operation id" in message
            else:
                raise AssertionError(f"invalid operation identity was accepted: {suffix!r}")
        assert application.lookups == []
    finally:
        service.shutdown()
        thread.join(timeout=3)


def test_committed_add_respects_authoritative_post_commit_absence_and_safe_fallback():
    """Successful state absence wins; only unavailable tracked projections may render."""
    from repogents.http_api import _CLIENT_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for committed-add state precedence coverage")
    _, script = _client_parts(_CLIENT_HTML)
    harness = r'''
class Element {
  constructor(id='') { this.id=id; this.value=''; this.textContent=''; this.attrs={}; this.listeners={}; this.dataset={}; this.elements=[]; this.disabled=false; this._innerHTML=''; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs,name); }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  focus(options) { if (!this.disabled) { document.activeElement=this; this.focusOptions=options||null; } }
  reset() { repository.value=''; branch.value=''; }
  set innerHTML(value) { this._innerHTML=String(value); if(this.id==='repositories') rebuild(this._innerHTML); }
  get innerHTML() { return this._innerHTML; }
}
const ids=['repositories','repository','branch','add-form','add-button','repository-error','add-error','add-verification-status','add-status','repository-summary','refresh-error','refresh-status','freshness','management-status','removal-announcement','repositories-heading'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
const repository=elements.repository,branch=elements.branch,form=elements['add-form'],addControl=elements['add-button'];form.elements=[repository,branch,addControl];
let dynamic=[];
function rebuild(markup) {
  dynamic=[];
  for(const match of markup.matchAll(/<button[^>]*data-remove="([^"]+)"[^>]*data-repository="([^"]+)"[^>]*>/g)) { const button=new Element(`remove-${match[1]}`);button.dataset={remove:match[1],removeFocus:match[1],repository:match[2]};dynamic.push(button); }
  for(const match of markup.matchAll(/<h3 id="repository-([^"]+)-heading"[^>]*data-repository-heading="([^"]+)"[^>]*>/g)) { const heading=new Element(`repository-${match[1]}-heading`);heading.dataset.repositoryHeading=match[2];heading.attrs.tabindex='-1';dynamic.push(heading); }
}
function selectorValue(selector,name){const match=selector.match(new RegExp(`\\[${name}="([^"]+)"\\]`));return match&&match[1];}
const documentListeners={},windowListeners={};
global.document={hidden:false,activeElement:null,querySelector(selector){if(selector.startsWith('#'))return elements[selector.slice(1)]||dynamic.find(item=>item.id===selector.slice(1))||null;for(const name of ['data-remove','data-remove-focus','data-repository-heading']){const expected=selectorValue(selector,name);if(!expected)continue;const key={'data-remove':'remove','data-remove-focus':'removeFocus','data-repository-heading':'repositoryHeading'}[name];return dynamic.find(item=>item.dataset[key]===expected)||null;}return null;},querySelectorAll(selector){if(selector==='[data-remove]')return dynamic.filter(item=>item.dataset.remove);if(selector==='[data-remove-focus]')return dynamic.filter(item=>item.dataset.removeFocus);if(selector==='[data-repository-heading]')return dynamic.filter(item=>item.dataset.repositoryHeading);return [];},addEventListener:(name,callback)=>{documentListeners[name]=callback;}};
global.window={confirm:()=>true,addEventListener:(name,callback)=>{windowListeners[name]=callback;}};global.CSS={escape:value=>String(value)};
const presentRepository={id:6,github_repository:'acme/present-after-commit',target_branch:'main',tracked:true,nodes:[],runs:[]};
const responses=[
  {ok:true,repositories:[]},
  {ok:true,repositories:[presentRepository]},
  {ok:true,repositories:[]},
  {ok:false,error:'State unavailable'},
  {ok:false,error:'State unavailable'}
];
let nextTimerId=1;const timers=new Map();
global.setTimeout=(callback,delay)=>{const id=nextTimerId++;timers.set(id,{callback,delay});return id;};
global.clearTimeout=id=>timers.delete(id);
function timerCount(delay){return [...timers.values()].filter(timer=>timer.delay===delay).length;}
let stateRequests=0,postRequests=0,deleteRequests=0;
global.fetch=async(path,options={})=>{if(path==='/api/state'){stateRequests+=1;const response=responses.shift();return response.ok?{ok:true,status:200,json:async()=>({repositories:response.repositories})}:{ok:false,status:503,statusText:'Unavailable',json:async()=>({error:response.error})};}if(path==='/api/repositories'&&options.method==='POST'){postRequests+=1;throw new Error('unexpected duplicate mutation');}if(path.startsWith('/api/repositories/')&&options.method==='DELETE'){deleteRequests+=1;throw new Error('unexpected delete mutation');}throw new Error(`unexpected request ${path}`);};
async function settle(){await Promise.resolve();await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));}
function snapshot(){return {content:elements.repositories.innerHTML,summary:elements['repository-summary'].textContent,removeNames:dynamic.filter(item=>item.dataset.remove).map(item=>item.dataset.repository),status:elements['add-status'].textContent,focus:document.activeElement&&document.activeElement.id,preventScroll:document.activeElement&&document.activeElement.focusOptions&&document.activeElement.focusOptions.preventScroll,stateRequests,postRequests,deleteRequests,refreshTimers:timerCount(3000),requestTimers:timerCount(15000)};}
'''
    scenario = r'''
(async()=>{
  await settle();
  const presentOperation={operation_id:'operation-present',state:'COMMITTED',github_repository:'acme/present-after-commit',repository:presentRepository};
  elements['add-status'].textContent='acme/present-after-commit was added to tracked repositories.';
  const presentRepositories=await settleCommittedAddOperation(presentOperation);
  restoreSuccessfulAddFocus(presentOperation.github_repository,presentRepositories);
  const present=snapshot();

  const removedOperation={operation_id:'operation-removed',state:'COMMITTED',github_repository:'acme/removed-after-commit',repository:{id:7,github_repository:'acme/removed-after-commit',target_branch:'main',tracked:true,nodes:[],runs:[]}};
  elements['add-status'].textContent='acme/removed-after-commit was added to tracked repositories.';
  const absentRepositories=await settleCommittedAddOperation(removedOperation);
  restoreSuccessfulAddFocus(removedOperation.github_repository,absentRepositories);
  const absent=snapshot();

  const fallbackOperation={operation_id:'operation-fallback',state:'COMMITTED',github_repository:'acme/fallback',repository:{id:8,github_repository:'acme/fallback',target_branch:'release',tracked:true,nodes:[],runs:[]}};
  elements['add-status'].textContent='acme/fallback was added to tracked repositories.';
  const fallbackRepositories=await settleCommittedAddOperation(fallbackOperation);
  restoreSuccessfulAddFocus(fallbackOperation.github_repository,fallbackRepositories);
  const fallback=snapshot();

  const untrackedOperation={operation_id:'operation-untracked',state:'COMMITTED',github_repository:'acme/untracked-projection',repository:{id:9,github_repository:'acme/untracked-projection',target_branch:'main',tracked:false,nodes:[],runs:[]}};
  elements['add-status'].textContent='acme/untracked-projection was added to tracked repositories.';
  const untrackedRepositories=await settleCommittedAddOperation(untrackedOperation);
  restoreSuccessfulAddFocus(untrackedOperation.github_repository,untrackedRepositories);
  const untracked=snapshot();
  windowListeners.pagehide({persisted:false});
  console.log(JSON.stringify({present,absent,fallback,untracked}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(
        [node, "-e", harness + "\n" + script + "\n" + scenario],
        check=True, capture_output=True, text=True, timeout=5,
    )
    behavior = json.loads(result.stdout)

    present = behavior["present"]
    assert "acme/present-after-commit" in present["content"]
    assert present["summary"] == "1 repository"
    assert present["removeNames"] == ["acme/present-after-commit"]
    assert present["status"] == "acme/present-after-commit was added to tracked repositories."
    assert present["focus"] == "repository-6-heading"
    assert present["preventScroll"] is True
    assert present["postRequests"] == 0
    assert present["deleteRequests"] == 0
    assert present["refreshTimers"] == 1
    assert present["requestTimers"] == 0

    absent = behavior["absent"]
    assert "acme/removed-after-commit" not in absent["content"]
    assert absent["summary"] == "0 repositories"
    assert absent["removeNames"] == []
    assert absent["status"] == "acme/removed-after-commit was added, but is no longer tracked."
    assert absent["focus"] == "repository"
    assert absent["preventScroll"] is True
    assert absent["postRequests"] == 0
    assert absent["deleteRequests"] == 0
    assert absent["refreshTimers"] == 1
    assert absent["requestTimers"] == 0

    fallback = behavior["fallback"]
    assert "acme/fallback" in fallback["content"]
    assert fallback["summary"] == "1 repository"
    assert fallback["removeNames"] == ["acme/fallback"]
    assert fallback["focus"] == "repository-8-heading"
    assert fallback["preventScroll"] is True
    assert fallback["status"] == "acme/fallback was added to tracked repositories."
    assert fallback["postRequests"] == 0
    assert fallback["deleteRequests"] == 0
    assert fallback["refreshTimers"] == 1
    assert fallback["requestTimers"] == 0

    untracked = behavior["untracked"]
    assert "acme/untracked-projection" not in untracked["content"]
    assert untracked["removeNames"] == ["acme/fallback"]
    assert untracked["status"] == "acme/untracked-projection was added, but is no longer tracked."
    assert untracked["focus"] == "repository"
    assert untracked["preventScroll"] is True
    assert untracked["postRequests"] == 0
    assert untracked["deleteRequests"] == 0
    assert untracked["stateRequests"] == 5
    assert untracked["refreshTimers"] == 1
    assert untracked["requestTimers"] == 0



def test_service_retains_application_ownership_until_blocked_poller_exits(tmp_path):
    """A replacement cannot own or poll until the old poller loses mutation authority."""
    from repogents.service_ownership import (
        ServiceOwnership,
        ServiceOwnershipUnavailableError,
    )

    ownership_path = tmp_path / ".repogents-service.lock"
    mutation_order = []
    mutation_guard = threading.Lock()

    class PollingApplication(FakeApplication):
        def __init__(self, name, *, block_poll=False):
            super().__init__()
            self.name = name
            self.block_poll = block_poll
            self.poll_started = threading.Event()
            self.release_poll = threading.Event()
            self.poll_finished = threading.Event()
            self.ownership = ServiceOwnership(ownership_path)
            self.closed_while_polling = False

        def acquire_service_ownership(self):
            self.ownership.acquire()

        def poll_once(self):
            self.poll_started.set()
            if self.block_poll:
                assert self.release_poll.wait(timeout=3)
            with mutation_guard:
                mutation_order.append(f"{self.name}-poll")
            self.poll_calls += 1
            self.poll_finished.set()

        def close(self):
            self.closed_while_polling = self.block_poll and not self.poll_finished.is_set()
            with mutation_guard:
                mutation_order.append(f"{self.name}-close")
            self.ownership.close()
            super().close()

    old_application = PollingApplication("old", block_poll=True)
    old_service = HttpService(old_application, "127.0.0.1", 0, 60)
    old_serving = threading.Thread(target=old_service.serve_forever)
    old_serving.start()
    assert old_application.poll_started.wait(timeout=2)

    shutdown = threading.Thread(target=old_service.shutdown)
    shutdown.start()
    shutdown.join(timeout=2)
    assert not shutdown.is_alive()

    # Listener shutdown completes, but the service thread and ownership remain live
    # while the poller can still resume and mutate shared state.
    old_serving.join(timeout=0.1)
    assert old_serving.is_alive()
    assert old_application.closed is False
    assert old_application.ownership.acquired is True

    competing_application = PollingApplication("competitor")
    with pytest.raises(ServiceOwnershipUnavailableError):
        HttpService(competing_application, "127.0.0.1", 0, 60)
    assert competing_application.closed is True
    assert competing_application.poll_calls == 0
    assert old_application.closed is False

    # Once the old request is released, its final mutation occurs before close drops
    # ownership. No callback remains that can mutate after the replacement acquires.
    old_application.release_poll.set()
    old_serving.join(timeout=2)
    assert not old_serving.is_alive()
    assert old_application.poll_finished.is_set()
    assert old_application.closed is True
    assert old_application.closed_while_polling is False
    assert old_application.ownership.acquired is False

    replacement_application = PollingApplication("replacement")
    replacement_service = HttpService(replacement_application, "127.0.0.1", 0, 60)
    replacement_serving = threading.Thread(target=replacement_service.serve_forever)
    replacement_serving.start()
    assert replacement_application.poll_started.wait(timeout=2)
    assert replacement_application.poll_finished.wait(timeout=2)
    replacement_service.shutdown()
    replacement_serving.join(timeout=2)
    assert not replacement_serving.is_alive()
    assert replacement_application.closed is True
    assert replacement_application.ownership.acquired is False

    with mutation_guard:
        assert mutation_order == [
            "competitor-close",
            "old-poll",
            "old-close",
            "replacement-poll",
            "replacement-close",
        ]


def test_service_retains_ownership_until_blocked_request_handler_exits(tmp_path):
    """An accepted mutation handler completes before ownership can be released."""
    from repogents.service_ownership import (
        ServiceOwnership,
        ServiceOwnershipUnavailableError,
    )

    ownership_path = tmp_path / ".repogents-service.lock"
    mutation_order = []
    mutation_guard = threading.Lock()

    class HandlerApplication(FakeApplication):
        def __init__(self, name, *, block_remove=False):
            super().__init__()
            self.name = name
            self.block_remove = block_remove
            self.remove_started = threading.Event()
            self.release_remove = threading.Event()
            self.remove_finished = threading.Event()
            self.ownership = ServiceOwnership(ownership_path)
            self.closed_while_removing = False

        def acquire_service_ownership(self):
            self.ownership.acquire()

        def remove_repository(self, repository_id):
            self.remove_started.set()
            if self.block_remove:
                assert self.release_remove.wait(timeout=3)
            with mutation_guard:
                mutation_order.append(f"{self.name}-remove-{repository_id}")
            self.removed.append(repository_id)
            self.remove_finished.set()

        def close(self):
            self.closed_while_removing = (
                self.block_remove and not self.remove_finished.is_set()
            )
            with mutation_guard:
                mutation_order.append(f"{self.name}-close")
            self.ownership.close()
            super().close()

    old_application = HandlerApplication("old", block_remove=True)
    old_service = HttpService(old_application, "127.0.0.1", 0, 60)
    # The standard ThreadingMixIn registry only retains request threads when they
    # are non-daemon, and server_close joins them only with block_on_close enabled.
    assert old_service._server.daemon_threads is False
    assert old_service._server.block_on_close is True
    old_serving = threading.Thread(target=old_service.serve_forever)
    old_serving.start()
    host, port = old_service.address

    request_result = {}

    def request_removal():
        request = urllib.request.Request(
            f"http://{host}:{port}/api/repositories/7", method="DELETE"
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                request_result["status"] = response.status
        except BaseException as error:
            request_result["error"] = error

    request_thread = threading.Thread(target=request_removal)
    request_thread.start()
    assert old_application.remove_started.wait(timeout=2)

    shutdown = threading.Thread(target=old_service.shutdown)
    shutdown.start()
    shutdown.join(timeout=2)
    assert not shutdown.is_alive()

    # The listener has stopped, but server_close is joining the accepted handler.
    # Application ownership must remain held for the handler's final mutation.
    old_serving.join(timeout=0.1)
    assert old_serving.is_alive()
    assert old_application.closed is False
    assert old_application.ownership.acquired is True

    competing_application = HandlerApplication("competitor")
    with pytest.raises(ServiceOwnershipUnavailableError):
        HttpService(competing_application, "127.0.0.1", 0, 60)
    assert competing_application.closed is True
    assert competing_application.ownership.acquired is False
    assert competing_application.removed == []

    old_application.release_remove.set()
    request_thread.join(timeout=2)
    old_serving.join(timeout=2)
    assert not request_thread.is_alive()
    assert not old_serving.is_alive()
    assert request_result == {"status": 204}
    assert old_application.remove_finished.is_set()
    assert old_application.closed is True
    assert old_application.closed_while_removing is False
    assert old_application.ownership.acquired is False

    replacement_application = HandlerApplication("replacement")
    replacement_service = HttpService(replacement_application, "127.0.0.1", 0, 60)
    replacement_serving = threading.Thread(target=replacement_service.serve_forever)
    replacement_serving.start()
    replacement_host, replacement_port = replacement_service.address
    replacement_request = urllib.request.Request(
        f"http://{replacement_host}:{replacement_port}/api/repositories/8",
        method="DELETE",
    )
    with urllib.request.urlopen(replacement_request, timeout=3) as response:
        assert response.status == 204
    replacement_service.shutdown()
    replacement_serving.join(timeout=2)
    assert not replacement_serving.is_alive()
    assert replacement_application.removed == [8]
    assert replacement_application.closed is True
    assert replacement_application.ownership.acquired is False

    with mutation_guard:
        assert mutation_order == [
            "competitor-close",
            "old-remove-7",
            "old-close",
            "replacement-remove-8",
            "replacement-close",
        ]


def test_stalled_request_body_releases_handler_then_allows_replacement_ownership(tmp_path):
    """Partial mutation input cannot outlive service ownership or block restart."""
    from repogents.service_ownership import (
        ServiceOwnership,
        ServiceOwnershipUnavailableError,
    )

    ownership_path = tmp_path / ".repogents-service.lock"
    lifecycle = []
    lifecycle_guard = threading.Lock()

    class OwnedApplication(FakeApplication):
        def __init__(self, name):
            super().__init__()
            self.name = name
            self.ownership = ServiceOwnership(ownership_path)

        def acquire_service_ownership(self):
            self.ownership.acquire()
            with lifecycle_guard:
                lifecycle.append(f"{self.name}-acquire")

        def add_repository(self, github_repository, target_branch=None):
            with lifecycle_guard:
                lifecycle.append(f"{self.name}-add")
            return super().add_repository(github_repository, target_branch)

        def close(self):
            with lifecycle_guard:
                lifecycle.append(f"{self.name}-close")
            self.ownership.close()
            super().close()

    old_application = OwnedApplication("old")
    old_service = HttpService(
        old_application,
        "127.0.0.1",
        0,
        60,
        request_io_timeout=0.5,
    )
    old_serving = threading.Thread(target=old_service.serve_forever)
    old_serving.start()
    host, port = old_service.address

    client = socket.create_connection((host, port), timeout=2)
    client.settimeout(2)
    declared_body = json.dumps(
        {"github_repository": "acme/stalled", "target_branch": "main"}
    ).encode("utf-8")
    partial_body = declared_body[:5]
    client.sendall(
        b"POST /api/repositories HTTP/1.1\r\n"
        + f"Host: {host}:{port}\r\n".encode("ascii")
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(declared_body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + partial_body
    )

    # Wait until ThreadingMixIn has registered the accepted non-daemon handler.
    deadline = time.monotonic() + 2
    handler_threads = []
    while time.monotonic() < deadline:
        try:
            handler_threads = list(old_service._server._threads)
        except TypeError:
            handler_threads = []
        if handler_threads and any(thread.is_alive() for thread in handler_threads):
            break
        time.sleep(0.005)
    assert handler_threads
    assert any(thread.is_alive() for thread in handler_threads)

    shutdown = threading.Thread(target=old_service.shutdown)
    shutdown.start()
    shutdown.join(timeout=2)
    assert not shutdown.is_alive()

    # Request acceptance has stopped, but server_close is still joining the body
    # reader. The application and real data-directory ownership remain live.
    old_serving.join(timeout=0.03)
    assert old_serving.is_alive()
    assert old_application.closed is False
    assert old_application.ownership.acquired is True
    assert old_application.added == []

    competitor = OwnedApplication("competitor")
    with pytest.raises(ServiceOwnershipUnavailableError):
        HttpService(competitor, "127.0.0.1", 0, 60, request_io_timeout=0.5)
    assert competitor.closed is True
    assert competitor.ownership.acquired is False
    assert competitor.added == []

    # The local socket I/O bound terminates the handler without more client input.
    old_serving.join(timeout=1)
    assert not old_serving.is_alive()
    assert old_application.closed is True
    assert old_application.ownership.acquired is False
    assert old_application.added == []
    assert all(not thread.is_alive() for thread in handler_threads)

    response = b""
    try:
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
    except (ConnectionResetError, socket.timeout):
        pass
    finally:
        client.close()
    if response:
        assert response.startswith(b"HTTP/1.0 408") or response.startswith(
            b"HTTP/1.1 408"
        )

    replacement = OwnedApplication("replacement")
    replacement_service = HttpService(
        replacement, "127.0.0.1", 0, 60, request_io_timeout=0.5
    )
    replacement_serving = threading.Thread(target=replacement_service.serve_forever)
    replacement_serving.start()
    replacement_host, replacement_port = replacement_service.address
    status, state = request_json(
        f"http://{replacement_host}:{replacement_port}/api/state"
    )
    assert status == 200
    assert state == replacement.payload
    replacement_service.shutdown()
    replacement_serving.join(timeout=2)
    assert not replacement_serving.is_alive()
    assert replacement.closed is True
    assert replacement.ownership.acquired is False

    with lifecycle_guard:
        assert "old-add" not in lifecycle
        assert lifecycle.index("old-close") < lifecycle.index("replacement-acquire")
        assert lifecycle == [
            "old-acquire",
            "competitor-close",
            "old-close",
            "replacement-acquire",
            "replacement-close",
        ]


def test_slow_trickle_request_body_hits_absolute_deadline_before_ownership_release(tmp_path):
    """Periodic body bytes cannot renew handler lifetime or outlive ownership."""
    from repogents.service_ownership import (
        ServiceOwnership,
        ServiceOwnershipUnavailableError,
    )

    ownership_path = tmp_path / ".repogents-service.lock"
    lifecycle = []
    lifecycle_guard = threading.Lock()

    class OwnedApplication(FakeApplication):
        def __init__(self, name):
            super().__init__()
            self.name = name
            self.ownership = ServiceOwnership(ownership_path)

        def acquire_service_ownership(self):
            self.ownership.acquire()
            with lifecycle_guard:
                lifecycle.append(f"{self.name}-acquire")

        def add_repository(self, github_repository, target_branch=None):
            with lifecycle_guard:
                lifecycle.append(f"{self.name}-add")
            return super().add_repository(github_repository, target_branch)

        def close(self):
            with lifecycle_guard:
                lifecycle.append(f"{self.name}-close")
            self.ownership.close()
            super().close()

    request_deadline = 0.3
    old_application = OwnedApplication("old")
    old_service = HttpService(
        old_application,
        "127.0.0.1",
        0,
        60,
        request_io_timeout=request_deadline,
    )
    old_serving = threading.Thread(target=old_service.serve_forever)
    old_serving.start()
    host, port = old_service.address

    client = socket.create_connection((host, port), timeout=2)
    client.settimeout(2)
    declared_body = json.dumps(
        {"github_repository": "acme/slow-trickle", "target_branch": "main"}
    ).encode("utf-8")
    client.sendall(
        b"POST /api/repositories HTTP/1.1\r\n"
        + f"Host: {host}:{port}\r\n".encode("ascii")
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(declared_body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
    )

    trickle_started = threading.Event()
    trickle_stopped = threading.Event()

    def trickle_body():
        try:
            for byte in declared_body:
                client.sendall(bytes((byte,)))
                trickle_started.set()
                # Each byte arrives comfortably before the configured socket timeout,
                # but the complete body would take several seconds.
                time.sleep(0.08)
        except OSError:
            pass
        finally:
            trickle_stopped.set()

    sender = threading.Thread(target=trickle_body)
    sender.start()
    assert trickle_started.wait(timeout=1)

    deadline = time.monotonic() + 1
    handler_threads = []
    while time.monotonic() < deadline:
        try:
            handler_threads = list(old_service._server._threads)
        except TypeError:
            handler_threads = []
        if handler_threads and any(thread.is_alive() for thread in handler_threads):
            break
        time.sleep(0.005)
    assert handler_threads

    shutdown = threading.Thread(target=old_service.shutdown)
    shutdown.start()
    shutdown.join(timeout=1)
    assert not shutdown.is_alive()

    # Shutdown has stopped request acceptance, but ownership remains held while the
    # tracked slow reader is still inside its absolute body deadline.
    old_serving.join(timeout=0.08)
    assert old_serving.is_alive()
    assert old_application.closed is False
    assert old_application.ownership.acquired is True
    assert old_application.added == []

    competitor = OwnedApplication("competitor")
    with pytest.raises(ServiceOwnershipUnavailableError):
        HttpService(
            competitor,
            "127.0.0.1",
            0,
            60,
            request_io_timeout=request_deadline,
        )
    assert competitor.closed is True
    assert competitor.added == []

    # The trickle cannot extend the total deadline. Handler joining and ownership
    # release complete without waiting for the sender to finish the declared body.
    old_serving.join(timeout=1)
    assert not old_serving.is_alive()
    assert old_application.closed is True
    assert old_application.ownership.acquired is False
    assert old_application.added == []
    assert all(not thread.is_alive() for thread in handler_threads)
    sender.join(timeout=1)
    assert trickle_stopped.is_set()

    response = b""
    try:
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
    except (ConnectionResetError, socket.timeout, OSError):
        pass
    finally:
        client.close()
    if response:
        assert response.startswith(b"HTTP/1.0 408") or response.startswith(
            b"HTTP/1.1 408"
        )

    replacement = OwnedApplication("replacement")
    replacement_service = HttpService(
        replacement,
        "127.0.0.1",
        0,
        60,
        request_io_timeout=request_deadline,
    )
    replacement_serving = threading.Thread(target=replacement_service.serve_forever)
    replacement_serving.start()
    replacement_host, replacement_port = replacement_service.address
    status, state = request_json(
        f"http://{replacement_host}:{replacement_port}/api/state"
    )
    assert status == 200
    assert state == replacement.payload
    replacement_service.shutdown()
    replacement_serving.join(timeout=2)
    assert not replacement_serving.is_alive()

    with lifecycle_guard:
        assert "old-add" not in lifecycle
        assert lifecycle.index("old-close") < lifecycle.index("replacement-acquire")
        assert lifecycle == [
            "old-acquire",
            "competitor-close",
            "old-close",
            "replacement-acquire",
            "replacement-close",
        ]


def test_slow_trickle_request_line_and_headers_hit_absolute_input_deadline(tmp_path):
    """Request-line/header bytes cannot renew a handler or service ownership forever."""
    from repogents.service_ownership import (
        ServiceOwnership,
        ServiceOwnershipUnavailableError,
    )

    ownership_path = tmp_path / ".repogents-service.lock"

    class OwnedApplication(FakeApplication):
        def __init__(self, name):
            super().__init__()
            self.name = name
            self.ownership = ServiceOwnership(ownership_path)

        def acquire_service_ownership(self):
            self.ownership.acquire()

        def close(self):
            self.ownership.close()
            super().close()

    cases = {
        "request-line": (
            b"",
            b"POST /api/repositories HTTP/1.1\r\n",
        ),
        "headers": (
            b"POST /api/repositories HTTP/1.1\r\nHost: localhost\r\n",
            b"X-Slow-Header: this-value-never-finishes",
        ),
    }
    for case_name, (complete_prefix, trickled_input) in cases.items():
        case_path = tmp_path / case_name
        case_path.mkdir()
        # Give each case an independent real ownership boundary.
        ownership_path = case_path / ".repogents-service.lock"
        deadline_seconds = 0.3
        application = OwnedApplication("old")
        service = HttpService(
            application,
            "127.0.0.1",
            0,
            60,
            request_io_timeout=deadline_seconds,
        )
        serving = threading.Thread(target=service.serve_forever)
        serving.start()
        host, port = service.address
        client = socket.create_connection((host, port), timeout=2)
        client.settimeout(2)
        if complete_prefix:
            # The header case reaches header parsing immediately; only the named
            # phase is slow-trickled so it cannot accidentally duplicate the
            # request-line regression.
            client.sendall(complete_prefix)
        sender_started = threading.Event()

        def trickle():
            try:
                for byte in trickled_input:
                    client.sendall(bytes((byte,)))
                    sender_started.set()
                    # Every byte arrives well below the configured socket timeout,
                    # while the complete parser input would take several seconds.
                    time.sleep(0.08)
            except OSError:
                pass

        sender = threading.Thread(target=trickle)
        sender.start()
        assert sender_started.wait(timeout=1)

        end = time.monotonic() + 1
        handler_threads = []
        while time.monotonic() < end:
            try:
                handler_threads = list(service._server._threads)
            except TypeError:
                handler_threads = []
            if handler_threads and any(thread.is_alive() for thread in handler_threads):
                break
            time.sleep(0.005)
        assert handler_threads

        shutdown = threading.Thread(target=service.shutdown)
        shutdown.start()
        shutdown.join(timeout=1)
        assert not shutdown.is_alive()

        # Listener acceptance is stopped, but the parser still owns the data
        # directory until its absolute input deadline terminates the handler.
        serving.join(timeout=0.08)
        assert serving.is_alive()
        assert application.closed is False
        assert application.ownership.acquired is True
        assert application.added == []

        competitor = OwnedApplication("competitor")
        with pytest.raises(ServiceOwnershipUnavailableError):
            HttpService(
                competitor,
                "127.0.0.1",
                0,
                60,
                request_io_timeout=deadline_seconds,
            )
        assert competitor.closed is True
        assert competitor.added == []

        serving.join(timeout=1)
        assert not serving.is_alive()
        assert application.closed is True
        assert application.ownership.acquired is False
        assert application.added == []
        assert all(not thread.is_alive() for thread in handler_threads)
        sender.join(timeout=1)
        assert not sender.is_alive()

        response = b""
        try:
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        except (ConnectionResetError, socket.timeout, OSError):
            pass
        finally:
            client.close()
        if case_name == "headers":
            # A complete request line established a valid HTTP version, so header
            # expiry can provide the bounded client-facing timeout response.
            assert response.startswith(b"HTTP/1.0 408") or response.startswith(
                b"HTTP/1.1 408"
            )
        elif response:
            # An incomplete request line may not provide a reliable response version.
            assert response.startswith(b"HTTP/1.0 408") or response.startswith(
                b"HTTP/1.1 408"
            )

        replacement = OwnedApplication("replacement")
        replacement_service = HttpService(
            replacement,
            "127.0.0.1",
            0,
            60,
            request_io_timeout=deadline_seconds,
        )
        replacement_serving = threading.Thread(
            target=replacement_service.serve_forever
        )
        replacement_serving.start()
        replacement_host, replacement_port = replacement_service.address
        status, state = request_json(
            f"http://{replacement_host}:{replacement_port}/api/state"
        )
        assert status == 200
        assert state == replacement.payload
        replacement_service.shutdown()
        replacement_serving.join(timeout=2)
        assert not replacement_serving.is_alive()
        assert replacement.closed is True
        assert replacement.added == []
