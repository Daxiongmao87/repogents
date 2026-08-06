import json
import os
import shlex
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import repogents.agent_runtime as agent_runtime
from repogents.agent_runtime import BridgeAgent, BridgeTextModel, MiniSweRuntime, RuntimeConfig


def _install_factories(monkeypatch, result_content):
    records = {
        "models": [],
        "environments": [],
        "environment_checks": [],
        "agents": [],
        "runs": [],
        "saves": [],
    }

    class FakeModel:
        def __init__(self, kwargs):
            self.kwargs = kwargs

    class FakeEnvironment:
        def __init__(self, kwargs):
            self.kwargs = kwargs

        def execute(self, action):
            records["environment_checks"].append(action)
            return {"output": "", "returncode": 0, "exception_info": ""}

        def cleanup(self):
            pass

    class FakeAgent:
        def __init__(self, model, environment, kwargs):
            self.model = model
            self.environment = environment
            self.kwargs = kwargs

        def run(self, task, **kwargs):
            records["runs"].append({"agent": self, "task": task, "kwargs": kwargs})
            if result_content is not None:
                content = result_content(len(records["runs"])) if callable(result_content) else result_content
                Path(kwargs["result_path"]).write_text(content)
            return {"exit_status": "Submitted"}

        def save(self, path):
            records["saves"].append({"agent": self, "path": path})
            return {
                "info": {
                    "config": {
                        "model": {
                            "model_kwargs": dict(self.model.kwargs["model_kwargs"]),
                        }
                    }
                },
                "messages": [{"role": "exit", "content": "completed"}],
            }

    def model_factory(**kwargs):
        model = FakeModel(kwargs)
        records["models"].append(model)
        return model

    def environment_factory(**kwargs):
        environment = FakeEnvironment(kwargs)
        records["environments"].append(environment)
        return environment

    def agent_factory(model, environment, **kwargs):
        agent = FakeAgent(model, environment, kwargs)
        records["agents"].append(agent)
        return agent

    monkeypatch.setattr(agent_runtime, "BridgeTextModel", model_factory)
    monkeypatch.setattr(agent_runtime, "LandlockEnvironment", environment_factory)
    monkeypatch.setattr(agent_runtime, "BridgeAgent", agent_factory)
    return records


@pytest.mark.parametrize("credential_field", ["api_key", "proxy_access_token"])
def test_runtime_config_rejects_caller_credentials(credential_field):
    with pytest.raises(TypeError):
        RuntimeConfig(model="gpt-5.6-terra", **{credential_field: "not-a-real-key"})


def test_real_landlock_environment_clears_inherited_credentials_and_returns_result(
    monkeypatch,
    tmp_path,
):
    credential_sentinel = "REPOGENTS_TEST_CREDENTIAL_SENTINEL"
    monkeypatch.setenv(credential_sentinel, "must-not-reach-agent-shell")

    class FakeAgent:
        def __init__(self, model, environment, **kwargs):
            self.environment = environment

        def run(self, task, **kwargs):
            result_path = shlex.quote(kwargs["result_path"])
            execution = self.environment.execute(
                {
                    "command": (
                        f'if test -n "${{{credential_sentinel}+x}}"; '
                        "then exposed=true; else exposed=false; fi; "
                        'printf \'{"credential_exposed":%s,"status":"returned"}\\n\' '
                        f'"$exposed" > {result_path}'
                    )
                }
            )
            assert execution["returncode"] == 0, execution
            return {"exit_status": "Submitted"}

    monkeypatch.setattr(agent_runtime, "BridgeTextModel", lambda **kwargs: object())
    monkeypatch.setattr(agent_runtime, "BridgeAgent", FakeAgent)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = MiniSweRuntime(RuntimeConfig(model="gpt-5.6-terra")).run(
        "inspect the shell environment", workspace
    )

    assert result == {"credential_exposed": False, "status": "returned"}


def test_real_landlock_executes_installed_software_reached_through_opt(tmp_path):
    executable = Path("/usr/bin/google-chrome")
    if not executable.exists() or not executable.resolve().is_relative_to(
        Path("/opt").resolve()
    ):
        pytest.skip("no installed executable resolves through /opt")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = agent_runtime.LandlockEnvironment(cwd=str(workspace), timeout=10)
    try:
        execution = environment.execute(
            {"command": f"{shlex.quote(str(executable))} --version"}
        )
        assert execution["returncode"] == 0, execution
        assert execution["output"].strip()
        assert "Permission denied" not in execution["output"]
        rendering = environment.execute(
            {
                "command": (
                    "google-chrome --headless --no-sandbox --disable-gpu "
                    "--disable-dev-shm-usage --user-data-dir=\"$TMPDIR/chrome\" "
                    "--screenshot=browser.png --window-size=800,600 "
                    "'data:text/html,<h1>Sandbox render</h1>' "
                    ">\"$TMPDIR/chrome.log\" 2>&1 && test -s browser.png"
                )
            }
        )
        assert rendering["returncode"] == 0, rendering
        assert (workspace / "browser.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        environment.cleanup()


def test_each_run_uses_a_fresh_direct_local_landlock_agent(monkeypatch, tmp_path):
    records = _install_factories(monkeypatch, lambda call: json.dumps({"call": call}))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = MiniSweRuntime(
        RuntimeConfig(
            model="gpt-5.6-terra",
            step_limit=7,
            command_timeout=19,
        )
    )

    first = runtime.run("first task", workspace)
    second = runtime.run("second task", workspace)

    assert first == {"call": 1}
    assert second == {"call": 2}
    assert len(records["models"]) == len(records["environments"]) == len(records["agents"]) == 2
    assert records["agents"][0] is not records["agents"][1]
    assert records["agents"][0].model is records["models"][0]
    assert records["agents"][1].model is records["models"][1]
    assert records["agents"][0].environment is records["environments"][0]
    assert records["agents"][1].environment is records["environments"][1]
    assert [model.kwargs for model in records["models"]] == [
        {
            "model_name": "openai/gpt-5.6-terra",
            "cost_tracking": "ignore_errors",
            "model_kwargs": {
                "api_base": "http://127.0.0.1:8787/v1",
                "api_key": "local-bridge",
                "timeout": 120.0,
            },
        },
        {
            "model_name": "openai/gpt-5.6-terra",
            "cost_tracking": "ignore_errors",
            "model_kwargs": {
                "api_base": "http://127.0.0.1:8787/v1",
                "api_key": "local-bridge",
                "timeout": 120.0,
            },
        },
    ]
    for environment in records["environments"]:
        assert environment.kwargs["cwd"] == str(workspace.resolve())
        assert environment.kwargs["timeout"] == 19
        assert environment.kwargs["internet_access"] is False
    assert records["environment_checks"] == [
        {"command": "true"},
        {"command": "true"},
    ]
    assert [agent.kwargs["step_limit"] for agent in records["agents"]] == [7, 7]
    assert [agent.kwargs["cost_limit"] for agent in records["agents"]] == [0, 0]
    result_paths = [Path(run["kwargs"]["result_path"]) for run in records["runs"]]
    assert result_paths[0] != result_paths[1]
    assert all(path.parent == workspace.resolve() / ".repogents" / "results" for path in result_paths)
    assert all(not path.exists() for path in result_paths)
    assert records["saves"] == []


def test_runtime_preflight_fails_closed_with_landlock_error(
    monkeypatch,
    tmp_path,
):
    class FailingEnvironment:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def execute(self, action):
            return {
                "output": "Landlock unavailable\n",
                "returncode": 1,
                "exception_info": "",
            }

        def cleanup(self):
            pass

    monkeypatch.setattr(agent_runtime, "LandlockEnvironment", FailingEnvironment)
    runtime = MiniSweRuntime(RuntimeConfig(model="gpt-5.6-terra"))

    with pytest.raises(
        RuntimeError,
        match="Landlock sandbox preflight failed: Landlock unavailable",
    ):
        runtime.preflight(tmp_path / "workspace")


def test_real_landlock_denies_files_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "controller-secret"
    secret.write_text("must-not-be-readable")
    environment = agent_runtime.LandlockEnvironment(cwd=str(workspace), timeout=5)
    try:
        execution = environment.execute(
            {"command": f"cat {shlex.quote(str(secret))}"}
        )
        assert execution["returncode"] != 0
        assert "Permission denied" in execution["output"]

        execution = environment.execute(
            {"command": "printf writable > artifact && cat artifact"}
        )
        assert execution == {
            "output": "writable",
            "returncode": 0,
            "exception_info": "",
        }
        assert (workspace / "artifact").read_text() == "writable"
    finally:
        environment.cleanup()


def test_real_landlock_disabled_mode_denies_internet_sockets(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = agent_runtime.LandlockEnvironment(
        cwd=str(workspace), timeout=5, internet_access=False
    )
    try:
        execution = environment.execute(
            {
                "command": (
                    "python3 -c 'import socket; "
                    "socket.socket(socket.AF_INET, socket.SOCK_STREAM)'"
                )
            }
        )
        assert execution["returncode"] != 0
        assert "PermissionError" in execution["output"]
    finally:
        environment.cleanup()


def test_real_landlock_enabled_mode_permits_outbound_sockets(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = agent_runtime.LandlockEnvironment(
        cwd=str(workspace), timeout=5, internet_access=True
    )
    try:
        execution = environment.execute(
            {
                "command": (
                    "python3 -c 'import socket; "
                    "s=socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.close()'"
                )
            }
        )
        assert execution == {"output": "", "returncode": 0, "exception_info": ""}
    finally:
        environment.cleanup()


def test_real_landlock_timeout_kills_command_process_group(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = agent_runtime.LandlockEnvironment(cwd=str(workspace), timeout=0.1)
    try:
        execution = environment.execute({"command": "sleep 30 & echo $!; wait"})
        assert execution["returncode"] == -1
        child_pid = int(execution["output"].strip())
        for _ in range(100):
            if not Path(f"/proc/{child_pid}").exists():
                break
            time.sleep(0.01)
        assert not Path(f"/proc/{child_pid}").exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        environment.cleanup()


def test_landlock_requires_standalone_submission_action(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = agent_runtime.LandlockEnvironment(cwd=str(workspace), timeout=5)
    try:
        combined = environment.execute(
            {"command": "false; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}
        )
        assert combined == {
            "output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n",
            "returncode": 0,
            "exception_info": "",
        }
        with pytest.raises(agent_runtime.Submitted):
            environment.execute(
                {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}
            )
    finally:
        environment.cleanup()


def test_run_preserves_flexible_task_role_schema_and_trajectory(monkeypatch, tmp_path):
    records = _install_factories(monkeypatch, '{"status": "handed-off"}')
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trajectory = tmp_path / "trajectories" / "turn.json"
    task = "Inspect the relevant code, then decide whether changes or a handoff are appropriate."
    role_prompt = "Use your repository judgment; preserve valid work and explain any handoff."
    result_schema = {"status": "string", "handoff": {"classification": "string"}}

    config = RuntimeConfig(
        model="gpt-5.6-terra",
        api_base="http://bridge.invalid",
    )
    result = MiniSweRuntime(config).run(
        task,
        workspace,
        role_prompt=role_prompt,
        result_schema=result_schema,
        trajectory_path=trajectory,
    )

    assert result == {"status": "handed-off"}
    assert records["models"][0].kwargs == {
        "model_name": "openai/gpt-5.6-terra",
        "cost_tracking": "ignore_errors",
        "model_kwargs": {
            "api_base": "http://bridge.invalid/v1",
            "api_key": "local-bridge",
            "timeout": 120.0,
        },
    }
    assert records["runs"][0]["task"] == task
    assert records["runs"][0]["kwargs"]["role_prompt"] == role_prompt
    assert records["runs"][0]["kwargs"]["result_schema"] == json.dumps(result_schema, sort_keys=True)
    result_path = Path(records["runs"][0]["kwargs"]["result_path"])
    assert result_path.parent == workspace.resolve() / ".repogents" / "results"
    assert not result_path.exists()
    instance_template = records["agents"][0].kwargs["instance_template"]
    assert "{{ task }}" in instance_template
    assert "{{ role_prompt }}" in instance_template
    assert "inspect or modify files" in instance_template
    assert "run tests" in instance_template
    assert "create artifacts" in instance_template
    assert "hand off" in instance_template
    assert "Choose the actions relevant to this task" in instance_template
    system_template = records["agents"][0].kwargs["system_template"]
    assert "exactly one shell action" in system_template
    assert "apply_patch command is unavailable" in system_template
    assert "more than one action block are rejected" in system_template
    assert "standalone action" in system_template
    assert "```mswea_bash_command" in system_template
    assert records["saves"] == [{"agent": records["agents"][0], "path": None}]
    trajectory_data = json.loads(trajectory.read_text())
    assert trajectory_data["info"]["config"]["model"]["model_kwargs"] == {
        "api_base": "http://bridge.invalid/v1",
        "timeout": 120.0,
    }
    assert "local-bridge" not in trajectory.read_text()


def test_run_fails_when_agent_does_not_write_result(monkeypatch, tmp_path):
    _install_factories(monkeypatch, None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="agent result JSON was not written"):
        MiniSweRuntime(RuntimeConfig(model="gpt-5.6-terra")).run(
            "task", workspace
        )


def test_bridge_text_model_rejects_multiple_shell_actions_before_execution():
    model = BridgeTextModel(
        model_name="openai/test",
        cost_tracking="ignore_errors",
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=(
                        "Inspect first.\n```mswea_bash_command\npwd\n```\n"
                        "Then test.\n```mswea_bash_command\npytest -q\n```"
                    )
                )
            )
        ]
    )

    with pytest.raises(agent_runtime.FormatError) as caught:
        model._parse_actions(response)

    assert caught.value.messages[0]["extra"]["n_actions"] == 2


def test_bridge_text_model_rejects_response_without_shell_actions():
    model = BridgeTextModel(
        model_name="openai/test",
        cost_tracking="ignore_errors",
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="No action."))]
    )

    with pytest.raises(agent_runtime.FormatError):
        model._parse_actions(response)


def test_bridge_agent_returns_prior_action_failure_instead_of_submitting():
    class FakeEnvironment:
        def execute(self, action):
            if action["command"] == "submit":
                raise agent_runtime.Submitted(
                    {"role": "exit", "content": "submitted", "extra": {}}
                )
            return {"output": "not found", "returncode": 127, "exception_info": ""}

        def get_template_vars(self):
            return {}

    class FakeModel:
        def format_observation_messages(self, message, outputs, template_vars):
            return [{"role": "user", "content": "observed", "extra": {"outputs": outputs}}]

        def get_template_vars(self):
            return {}

    agent = BridgeAgent(
        FakeModel(),
        FakeEnvironment(),
        system_template="system",
        instance_template="instance",
        step_limit=0,
        cost_limit=0,
    )
    message = {
        "role": "assistant",
        "extra": {"actions": [{"command": "missing"}, {"command": "submit"}]},
    }

    observations = agent.execute_actions(message)

    assert observations[0]["extra"]["outputs"] == [
        {"output": "not found", "returncode": 127, "exception_info": ""},
        {
            "output": "",
            "returncode": -1,
            "exception_info": "submission rejected because an earlier action in this response failed",
        },
    ]


def test_bridge_agent_submits_after_successful_prior_actions(tmp_path):
    class FakeEnvironment:
        def execute(self, action):
            if action["command"] == "submit":
                raise agent_runtime.Submitted(
                    {"role": "exit", "content": "submitted", "extra": {}}
                )
            return {"output": "ok", "returncode": 0, "exception_info": ""}

        def get_template_vars(self):
            return {}

    class FakeModel:
        def get_template_vars(self):
            return {}

    agent = BridgeAgent(
        FakeModel(),
        FakeEnvironment(),
        system_template="system",
        instance_template="instance",
        step_limit=0,
        cost_limit=0,
    )
    message = {
        "role": "assistant",
        "extra": {"actions": [{"command": "success"}, {"command": "submit"}]},
    }
    result_path = tmp_path / "result.json"
    agent.extra_template_vars["result_path"] = str(result_path)
    result_path.write_text("{}")

    try:
        with pytest.raises(agent_runtime.Submitted):
            agent.execute_actions(message)
    finally:
        result_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("result_content", "expected_error"),
    [
        (None, "required result JSON was not written"),
        ("not JSON", "result file is not valid JSON"),
        ("[]", "result JSON is not an object"),
    ],
)
def test_bridge_agent_rejects_submission_without_valid_result_object(
    tmp_path, result_content, expected_error
):
    class FakeEnvironment:
        def execute(self, action):
            raise agent_runtime.Submitted(
                {"role": "exit", "content": "submitted", "extra": {}}
            )

        def get_template_vars(self):
            return {}

    class FakeModel:
        def format_observation_messages(self, message, outputs, template_vars):
            return [
                {"role": "user", "content": "observed", "extra": {"outputs": outputs}}
            ]

        def get_template_vars(self):
            return {}

    result_path = tmp_path / "result.json"
    if result_content is not None:
        result_path.write_text(result_content)
    agent = BridgeAgent(
        FakeModel(),
        FakeEnvironment(),
        system_template="system",
        instance_template="instance",
        step_limit=0,
        cost_limit=0,
    )
    agent.extra_template_vars["result_path"] = str(result_path)

    observations = agent.execute_actions(
        {"role": "assistant", "extra": {"actions": [{"command": "submit"}]}}
    )

    assert observations[0]["extra"]["outputs"] == [
        {"output": "", "returncode": -1, "exception_info": f"submission rejected because the {expected_error}"}
    ]


@pytest.mark.parametrize("result_content", ["not JSON", "[]"])
def test_run_fails_when_agent_output_is_not_a_json_object(monkeypatch, tmp_path, result_content):
    _install_factories(monkeypatch, result_content)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="agent result must be a valid JSON object"):
        MiniSweRuntime(RuntimeConfig(model="gpt-5.6-terra")).run(
            "task", workspace
        )
