from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.extra.bubblewrap import (
    BubblewrapEnvironment,
    BubblewrapEnvironmentConfig,
)
from minisweagent.models.litellm_model import LitellmModel


_SYSTEM_TEMPLATE = """\
You are an adaptive repository agent operating inside an isolated workspace.
Canonical Git and controller state and credentials are unavailable in this disposable workspace.
Git repository operations are controller-owned; source editing and tests remain available.
Use the available shell tools flexibly and follow the task and role instructions.
"""

_INSTANCE_TEMPLATE = """\
Task:
{{ task }}

{% if role_prompt %}Role instructions:
{{ role_prompt }}
{% endif %}
Choose the actions relevant to this task. You may inspect or modify files, run tests,
create artifacts, request additional work, or hand off work when relevant. No fixed
implementation procedure is required.

Before submitting, write exactly one valid JSON object to:
{{ result_path }}
{% if result_schema %}
The object must match this requested result schema:
{{ result_schema }}
{% endif %}
Then finish by issuing `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` on its own.
"""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    api_base: str = "http://127.0.0.1:8787/v1"
    model: str = "gpt-5.6-sol"
    step_limit: int = 50
    command_timeout: int = 120


class MiniSweRuntime:
    def __init__(self, config: RuntimeConfig):
        self.config = config

    def run(
        self,
        task: str,
        workspace: str | Path,
        *,
        role_prompt: str = "",
        result_schema: dict | None = None,
        trajectory_path: str | Path | None = None,
    ) -> dict:
        workspace_path = Path(workspace).resolve()
        results_directory = workspace_path / ".repogents" / "results"
        results_directory.mkdir(parents=True, exist_ok=True)
        result_path = results_directory / f"{uuid.uuid4().hex}.json"

        model_kwargs = {
            "api_base": self._api_v1_base(),
            "api_key": "local-bridge",
        }
        model = LitellmModel(
            model_name=f"openai/{self.config.model}",
            cost_tracking="ignore_errors",
            model_kwargs=model_kwargs,
        )
        environment = BubblewrapEnvironment(
            wrapper_args=["--clearenv", *BubblewrapEnvironmentConfig().wrapper_args],
            cwd=str(workspace_path),
            timeout=self.config.command_timeout,
        )
        agent = DefaultAgent(
            model,
            environment,
            system_template=_SYSTEM_TEMPLATE,
            instance_template=_INSTANCE_TEMPLATE,
            step_limit=self.config.step_limit,
            cost_limit=0,
        )

        schema_text = "" if result_schema is None else json.dumps(result_schema, sort_keys=True)
        try:
            agent.run(
                task,
                role_prompt=role_prompt,
                result_schema=schema_text,
                result_path=str(result_path),
            )
            if trajectory_path is not None:
                self._save_trajectory(agent, Path(trajectory_path))
            return self._load_result(result_path)
        finally:
            result_path.unlink(missing_ok=True)

    def _api_v1_base(self) -> str:
        base = self.config.api_base.rstrip("/")
        return base if base.endswith("/v1") else f"{base}/v1"

    @staticmethod
    def _load_result(result_path: Path) -> dict:
        if not result_path.is_file():
            raise RuntimeError("agent result JSON was not written")
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("agent result must be a valid JSON object") from exc
        if not isinstance(result, dict):
            raise RuntimeError("agent result must be a valid JSON object")
        return result

    @classmethod
    def _save_trajectory(cls, agent: DefaultAgent, trajectory_path: Path) -> None:
        trajectory: dict[str, Any] = agent.save(None)
        cls._remove_api_keys(trajectory)
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        trajectory_path.write_text(json.dumps(trajectory, indent=2))

    @classmethod
    def _remove_api_keys(cls, value: Any) -> None:
        if isinstance(value, dict):
            value.pop("api_key", None)
            for child in value.values():
                cls._remove_api_keys(child)
        elif isinstance(value, list):
            for child in value:
                cls._remove_api_keys(child)
