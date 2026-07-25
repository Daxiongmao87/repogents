from __future__ import annotations

import base64
import contextlib
import importlib
import json
import os
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DefaultAgent: Any = None
LitellmModel: Any = None
Submitted: Any = None
FormatError: Any = None

_COMPLETION_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MULTIMODAL_REGEX = (
    r"(?s)<MSWEA_MULTIMODAL_CONTENT><CONTENT_TYPE>(.+?)</CONTENT_TYPE>"
    r"(.+?)</MSWEA_MULTIMODAL_CONTENT>"
)


class ControllerDecisionEnvironment:
    """A mini-SWE environment that accepts one decision and executes nothing."""

    def __init__(self, response_schema: Mapping[str, object]) -> None:
        self.response_schema = dict(response_schema)
        self.decision: dict[str, object] | None = None

    def execute(self, action: dict[str, object]) -> dict[str, object]:
        try:
            command = action.get("command")
            if not isinstance(command, str):
                raise ValueError("mini-SWE decision action requires a command string")
            lines = command.lstrip().splitlines(keepends=True)
            if not lines or lines[0].strip() != _COMPLETION_MARKER:
                raise ValueError(
                    "repository commands are disabled in the mini-SWE worker"
                )
            submission = "".join(lines[1:]).strip()
            decision = _single_json_object(submission)
            if not isinstance(decision, dict):
                raise TypeError("mini-SWE submission must contain one JSON object")
            _validate_schema(decision, self.response_schema)
        except (TypeError, ValueError) as error:
            if FormatError is None:
                raise RuntimeError(
                    "mini-SWE format exception is unavailable"
                ) from error
            message: dict[str, object] = {
                "role": "user",
                "content": (
                    "The controller decision failed schema or format "
                    f"validation: {error}. Submit exactly one corrected "
                    "controller decision matching the requested schema."
                ),
                "extra": {
                    "interrupt_type": "FormatError",
                },
            }
            tool_call_id = action.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                message["role"] = "tool"
                message["tool_call_id"] = tool_call_id
            raise FormatError(message) from error
        self.decision = decision
        if Submitted is None:
            raise RuntimeError("mini-SWE completion exception is unavailable")
        raise Submitted(
            {
                "role": "exit",
                "content": json.dumps(decision, ensure_ascii=False, sort_keys=True),
                "extra": {
                    "exit_status": "Submitted",
                    "submission": json.dumps(
                        decision,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            }
        )

    def get_template_vars(self, **kwargs: object) -> dict[str, object]:
        return dict(kwargs)

    def serialize(self) -> dict[str, object]:
        return {
            "info": {
                "config": {
                    "environment_type": (
                        "repogents.mini_swe_worker.ControllerDecisionEnvironment"
                    )
                }
            }
        }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) not in {1, 2}:
        raise ValueError(
            "mini-SWE worker expects a request path and optional response path"
        )
    request_path = Path(arguments[0].removeprefix("@")).expanduser().resolve()
    response_path = (
        Path(arguments[1]).expanduser().resolve() if len(arguments) == 2 else None
    )
    if response_path is not None:
        response_path.unlink(missing_ok=True)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise TypeError("mini-SWE request must be a JSON object")
    with open(os.devnull, "w", encoding="utf-8") as model_stdout:
        with contextlib.redirect_stdout(model_stdout):
            decision, trajectory = _run_request(request)
    serialized = json.dumps(
        decision,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    _write_trajectory(request, decision, trajectory)
    if response_path is None:
        print(serialized)
    else:
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(serialized + "\n", encoding="utf-8")
        response_path.chmod(0o600)
    return 0


def _model_configuration(selector: str) -> tuple[str, str | None]:
    model, separator, effort = selector.rpartition(":")
    if separator and model and effort in _REASONING_EFFORTS:
        return model, effort
    return selector, None


def _multimodal_task(prompt: str, image_paths: object) -> tuple[str, bool]:
    if not isinstance(image_paths, list) or any(
        not isinstance(value, str) or not value.strip() for value in image_paths
    ):
        raise TypeError("mini-SWE worker image paths must be a string list")
    if not image_paths:
        return prompt, False
    parts = [prompt.rstrip()]
    for value in image_paths:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("mini-SWE worker image path must be a file")
        body = path.read_bytes()
        if not body or len(body) > _MAX_IMAGE_BYTES:
            raise ValueError("mini-SWE worker image size is invalid")
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif body.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        elif len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            media_type = "image/webp"
        else:
            raise ValueError("mini-SWE worker image type is unsupported")
        encoded = base64.b64encode(body).decode("ascii")
        parts.append(
            "<MSWEA_MULTIMODAL_CONTENT><CONTENT_TYPE>image_url</CONTENT_TYPE>"
            f"data:{media_type};base64,{encoded}"
            "</MSWEA_MULTIMODAL_CONTENT>"
        )
    return "\n".join(parts), True


def _run_request(
    request: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    model, reasoning_effort = _model_configuration(_required_string(request, "model"))
    system_prompt = _required_string(request, "system_prompt")
    prompt = _required_string(request, "prompt", allow_empty=True)
    task, has_images = _multimodal_task(
        prompt,
        request.get("image_paths", []),
    )
    state_directory = Path(_required_string(request, "state_directory")).resolve()
    state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_directory.chmod(0o700)
    config_directory = state_directory / "config"
    config_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_directory.chmod(0o700)
    os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(config_directory)
    os.environ["MSWEA_SILENT_STARTUP"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

    response_schema = request.get("response_schema")
    if not isinstance(response_schema, dict):
        raise TypeError("mini-SWE response schema must be an object")
    base_url = request.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        raise TypeError("mini-SWE base URL must be a string or null")
    timeout_value = request.get("timeout", 600)
    if (
        isinstance(timeout_value, bool)
        or not isinstance(timeout_value, (int, float))
        or timeout_value <= 0
    ):
        raise ValueError("mini-SWE worker timeout must be positive")

    _load_harness()
    model_kwargs: dict[str, object] = {}
    if base_url:
        model_kwargs["api_base"] = base_url
    if reasoning_effort is not None:
        model_kwargs["reasoning_effort"] = reasoning_effort
    model_adapter = LitellmModel(
        model_name=model,
        model_kwargs=model_kwargs,
        cost_tracking="ignore_errors",
        multimodal_regex=_MULTIMODAL_REGEX if has_images else "",
    )
    _abort_permanent_model_errors(model_adapter)
    environment = ControllerDecisionEnvironment(response_schema)
    protocol = (
        system_prompt.rstrip()
        + "\n\nThe Bash tool is a structured-return channel only; it never executes "
        "repository commands. Invoke it exactly once. The command's first line "
        f"must be {_COMPLETION_MARKER} and all remaining text must be exactly one "
        "JSON object matching this schema:\n"
        + json.dumps(response_schema, ensure_ascii=False, sort_keys=True)
        + "\nDo not invoke any other command and do not return the decision as prose."
    )
    agent = DefaultAgent(
        model_adapter,
        environment,
        system_template=protocol,
        instance_template="{{ task }}",
        step_limit=3,
        cost_limit=0,
        wall_time_limit_seconds=max(1, int(float(timeout_value))),
        max_consecutive_format_errors=2,
        output_path=None,
    )
    outcome = agent.run(task=task)
    submission = outcome.get("submission") if isinstance(outcome, dict) else None
    if not isinstance(submission, str) or not submission.strip():
        raise ValueError("mini-SWE agent did not submit one decision")
    decision = _single_json_object(submission)
    if not isinstance(decision, dict):
        raise TypeError("mini-SWE submission must contain one JSON object")
    _validate_schema(decision, response_schema)
    normalized = {
        "exit_status": str(outcome.get("exit_status", "Submitted")),
        "model_calls": getattr(agent, "n_calls", None),
    }
    return decision, normalized


def _load_harness() -> None:
    global DefaultAgent, FormatError, LitellmModel, Submitted
    if DefaultAgent is None:
        module = importlib.import_module("minisweagent.agents.default")
        DefaultAgent = module.DefaultAgent
    if LitellmModel is None:
        module = importlib.import_module("minisweagent.models.litellm_model")
        LitellmModel = module.LitellmModel
    if Submitted is None or FormatError is None:
        module = importlib.import_module("minisweagent.exceptions")
        if Submitted is None:
            Submitted = module.Submitted
        if FormatError is None:
            FormatError = module.FormatError


def _abort_permanent_model_errors(model_adapter: object) -> None:
    module = importlib.import_module("litellm")
    bad_request_error = module.exceptions.BadRequestError
    existing = list(getattr(model_adapter, "abort_exceptions", ()))
    if bad_request_error not in existing:
        existing.append(bad_request_error)
    model_adapter.abort_exceptions = existing


def _write_trajectory(
    request: Mapping[str, object],
    decision: Mapping[str, object],
    runtime: Mapping[str, object],
) -> None:
    state_directory = Path(str(request["state_directory"])).resolve()
    trajectory_directory = state_directory / "trajectories"
    trajectory_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    trajectory_directory.chmod(0o700)
    trajectory = {
        "trajectory_format": "repogents-mini-swe-1",
        "version": 1,
        "runtime": "mini-swe-agent",
        "model": request["model"],
        "messages": [
            {"role": "system", "content": request["system_prompt"]},
            {"role": "user", "content": request["prompt"]},
            {
                "role": "assistant",
                "content": json.dumps(
                    decision,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        ],
        "decision": dict(decision),
        "result": dict(runtime),
    }
    destination = trajectory_directory / (
        f"trajectory-{time.time_ns()}-{uuid.uuid4().hex}.json"
    )
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(trajectory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(destination)


def _required_string(
    value: Mapping[str, object],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or (not allow_empty and not item.strip()):
        raise ValueError(f"mini-SWE request requires {key}")
    return item


def _single_json_object(value: str) -> Any:
    content = value.strip()
    if not content:
        raise ValueError("mini-SWE submission is empty")
    try:
        result, end = json.JSONDecoder().raw_decode(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"mini-SWE submission is invalid JSON: {error}") from error
    if content[end:].strip():
        raise ValueError("mini-SWE submission must contain a single JSON object")
    return result


def _validate_schema(
    value: object,
    schema: Mapping[str, object],
    path: str = "$",
) -> None:
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        raise ValueError(f"{path} is not one of the allowed values")

    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _has_json_type(value, expected_type):
        raise TypeError(f"{path} must have JSON type {expected_type}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise TypeError(f"{path} schema properties must be an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise TypeError(f"{path} schema required must be a string list")
        missing = [item for item in required if item not in value]
        if missing:
            raise ValueError(f"{path} is missing required fields: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValueError(
                    f"{path} contains unexpected fields: {', '.join(extras)}"
                )
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, dict):
                _validate_schema(item, child, f"{path}.{key}")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} requires at least {minimum} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, f"{path}[{index}]")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise ValueError(f"{path} requires at least {minimum_length} characters")


def _has_json_type(value: object, expected: str) -> bool:
    checks = {
        "array": lambda item: isinstance(item, list),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: (isinstance(item, int) and not isinstance(item, bool)),
        "null": lambda item: item is None,
        "number": lambda item: isinstance(item, (int, float))
        and not isinstance(item, bool),
        "object": lambda item: isinstance(item, dict),
        "string": lambda item: isinstance(item, str),
    }
    check = checks.get(expected)
    if check is None:
        raise ValueError(f"unsupported JSON schema type: {expected}")
    return bool(check(value))


if __name__ == "__main__":
    raise SystemExit(main())
