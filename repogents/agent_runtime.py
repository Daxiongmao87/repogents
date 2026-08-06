from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import litellm
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import FormatError, Submitted
from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel

from repogents.landlock_environment import LandlockEnvironment


_SYSTEM_TEMPLATE = """\
You are an adaptive repository agent operating inside an isolated workspace.
Canonical Git and controller state and credentials are unavailable in this disposable workspace.
Git repository operations are controller-owned; source editing and tests remain available.
Use the available shell tools flexibly and follow the task and role instructions.
The shell is the only tool interface. The apply_patch command is unavailable; use
available shell programs to inspect and modify repository artifacts.
Every response must contain exactly one shell action in this form:
```mswea_bash_command
command
```
Only the first action block is executed. Wait for its observation before deciding
the next action. Submit only in a later, standalone action after observing that all
required artifact changes and checks succeeded.
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
    model: str
    api_base: str = "http://127.0.0.1:8787/v1"
    step_limit: int = 50
    command_timeout: int = 120
    model_request_timeout: float = 120.0


class BridgeTextModel(LitellmTextbasedModel):
    """Text-action model for bridges that do not preserve native tool calls."""

    abort_exceptions = [
        *LitellmTextbasedModel.abort_exceptions,
        litellm.exceptions.Timeout,
    ]

    def _parse_actions(self, response) -> list[dict]:
        content = response.choices[0].message.content or ""
        commands = [
            command.strip()
            for command in re.findall(
                self.config.action_regex,
                content,
                re.DOTALL,
            )
            if command.strip()
        ]
        if not commands:
            raise FormatError(
                {
                    "role": "user",
                    "content": (
                        "No shell actions found in the response. Every response "
                        "MUST include at least one mswea_bash_command block."
                    ),
                    "extra": {
                        "interrupt_type": "FormatError",
                        "n_actions": 0,
                        "model_response": content,
                    },
                }
            )
        return [{"command": commands[0]}]


class BridgeAgent(DefaultAgent):
    """Agent control flow that rejects submission after a failed batched action."""

    def execute_actions(self, message: dict) -> list[dict]:
        outputs = []
        for action in message.get("extra", {}).get("actions", []):
            try:
                outputs.append(self.env.execute(action))
            except Submitted:
                if any(output.get("returncode") != 0 for output in outputs):
                    outputs.append(
                        {
                            "output": "",
                            "returncode": -1,
                            "exception_info": (
                                "submission rejected because an earlier action "
                                "in this response failed"
                            ),
                        }
                    )
                    break
                raise
        return self.add_messages(
            *self.model.format_observation_messages(
                message,
                outputs,
                self.get_template_vars(),
            )
        )


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
            "timeout": self.config.model_request_timeout,
        }
        model = BridgeTextModel(
            model_name=f"openai/{self.config.model}",
            cost_tracking="ignore_errors",
            model_kwargs=model_kwargs,
        )
        environment = self._verified_environment(workspace_path)
        agent = BridgeAgent(
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
            environment.cleanup()

    def preflight(self, workspace: str | Path) -> None:
        workspace_path = Path(workspace).resolve()
        workspace_path.mkdir(parents=True, exist_ok=True)
        environment = self._verified_environment(workspace_path)
        environment.cleanup()

    def _verified_environment(self, workspace: Path):
        environment = LandlockEnvironment(
            cwd=str(workspace),
            timeout=self.config.command_timeout,
        )
        execution = environment.execute({"command": "true"})
        if execution.get("returncode") == 0:
            return environment
        output = str(execution.get("output", "")).strip()
        detail = output or str(execution.get("exception_info", "")).strip()
        environment.cleanup()
        raise RuntimeError(
            f"Landlock sandbox preflight failed: {detail or 'unknown error'}"
        )

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
