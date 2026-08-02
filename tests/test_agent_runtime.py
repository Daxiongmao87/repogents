import json
from pathlib import Path

import pytest

import repogents.agent_runtime as agent_runtime
from repogents.agent_runtime import MiniSweRuntime, RuntimeConfig


def _install_factories(monkeypatch, result_content):
    records = {"models": [], "environments": [], "agents": [], "runs": [], "saves": []}

    class FakeModel:
        def __init__(self, kwargs):
            self.kwargs = kwargs

    class FakeEnvironment:
        def __init__(self, kwargs):
            self.kwargs = kwargs

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

    monkeypatch.setattr(agent_runtime, "LitellmModel", model_factory)
    monkeypatch.setattr(agent_runtime, "BubblewrapEnvironment", environment_factory)
    monkeypatch.setattr(agent_runtime, "DefaultAgent", agent_factory)
    return records


@pytest.mark.parametrize("credential_field", ["api_key", "proxy_access_token"])
def test_runtime_config_rejects_caller_credentials(credential_field):
    with pytest.raises(TypeError):
        RuntimeConfig(**{credential_field: "not-a-real-key"})


def test_each_run_uses_a_fresh_direct_local_bubblewrap_agent(monkeypatch, tmp_path):
    records = _install_factories(monkeypatch, lambda call: json.dumps({"call": call}))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = MiniSweRuntime(RuntimeConfig(step_limit=7, command_timeout=19))

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
            "model_name": "openai/gpt-5.6-sol",
            "cost_tracking": "ignore_errors",
            "model_kwargs": {
                "api_base": "http://127.0.0.1:8787/v1",
                "api_key": "local-bridge",
            },
        },
        {
            "model_name": "openai/gpt-5.6-sol",
            "cost_tracking": "ignore_errors",
            "model_kwargs": {
                "api_base": "http://127.0.0.1:8787/v1",
                "api_key": "local-bridge",
            },
        },
    ]
    assert [environment.kwargs for environment in records["environments"]] == [
        {"cwd": str(workspace.resolve()), "timeout": 19},
        {"cwd": str(workspace.resolve()), "timeout": 19},
    ]
    assert [agent.kwargs["step_limit"] for agent in records["agents"]] == [7, 7]
    assert [agent.kwargs["cost_limit"] for agent in records["agents"]] == [0, 0]
    result_paths = [Path(run["kwargs"]["result_path"]) for run in records["runs"]]
    assert result_paths[0] != result_paths[1]
    assert all(path.parent == workspace.resolve() / ".repogents" / "results" for path in result_paths)
    assert all(not path.exists() for path in result_paths)
    assert records["saves"] == []


def test_run_preserves_flexible_task_role_schema_and_trajectory(monkeypatch, tmp_path):
    records = _install_factories(monkeypatch, '{"status": "handed-off"}')
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trajectory = tmp_path / "trajectories" / "turn.json"
    task = "Inspect the relevant code, then decide whether changes or a handoff are appropriate."
    role_prompt = "Use your repository judgment; preserve valid work and explain any handoff."
    result_schema = {"status": "string", "handoff": {"classification": "string"}}

    config = RuntimeConfig(api_base="http://bridge.invalid")
    result = MiniSweRuntime(config).run(
        task,
        workspace,
        role_prompt=role_prompt,
        result_schema=result_schema,
        trajectory_path=trajectory,
    )

    assert result == {"status": "handed-off"}
    assert records["models"][0].kwargs == {
        "model_name": "openai/gpt-5.6-sol",
        "cost_tracking": "ignore_errors",
        "model_kwargs": {
            "api_base": "http://bridge.invalid/v1",
            "api_key": "local-bridge",
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
    assert records["saves"] == [{"agent": records["agents"][0], "path": None}]
    trajectory_data = json.loads(trajectory.read_text())
    assert trajectory_data["info"]["config"]["model"]["model_kwargs"] == {
        "api_base": "http://bridge.invalid/v1"
    }
    assert "local-bridge" not in trajectory.read_text()


def test_run_fails_when_agent_does_not_write_result(monkeypatch, tmp_path):
    _install_factories(monkeypatch, None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="agent result JSON was not written"):
        MiniSweRuntime(RuntimeConfig()).run("task", workspace)


@pytest.mark.parametrize("result_content", ["not JSON", "[]"])
def test_run_fails_when_agent_output_is_not_a_json_object(monkeypatch, tmp_path, result_content):
    _install_factories(monkeypatch, result_content)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="agent result must be a valid JSON object"):
        MiniSweRuntime(RuntimeConfig()).run("task", workspace)
