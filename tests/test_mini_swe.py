from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from repogents import mini_swe_worker
from repogents.mini_swe import (
    MINI_SWE_RUNTIME,
    MiniSweInference,
    mini_swe_environment,
)

_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"enum": ["finish", "block"]},
        "reason": {"type": "string"},
    },
    "required": ["action", "reason"],
    "additionalProperties": False,
}


class MiniSweConfigurationTests(unittest.TestCase):
    def test_runtime_identifier_is_frozen(self) -> None:
        self.assertEqual(MINI_SWE_RUNTIME, "mini-swe-agent")

    def test_environment_is_isolated_and_selects_only_provider_credential(
        self,
    ) -> None:
        source = {
            "PATH": "/controller/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SSL_CERT_FILE": "/controller/ca.pem",
            "TMPDIR": "/controller/tmp",
            "HOME": "/home/poisoned",
            "OMP_CONFIG": "/home/poisoned/.omp/config.json",
            "PI_CODING_AGENT_DIR": "/home/poisoned/.pi/agent",
            "PI_PACKAGE_DIR": "/home/poisoned/.pi/packages",
            "GITHUB_TOKEN": "ambient-github",
            "GH_TOKEN": "ambient-gh",
            "REPOGENTS_GITHUB_TOKEN": "controller-github",
            "REPOGENTS_SECRET_PACKAGE_TOKEN": "sandbox-binding",  # pragma: allowlist secret
            "SANDBOX_TOKEN": "sandbox-secret",
            "OPENAI_API_KEY": "openai-secret",  # pragma: allowlist secret
            "OPENAI_BASE_URL": "https://ambient.invalid/v1",
            "ANTHROPIC_API_KEY": "anthropic-secret",  # pragma: allowlist secret
            "UNRELATED_SECRET": "unrelated",  # pragma: allowlist secret
        }
        cases = (
            ("openai/gpt-test", "OPENAI_API_KEY", "openai-secret"),
            (
                "anthropic/claude-test",
                "ANTHROPIC_API_KEY",
                "anthropic-secret",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            application_state = Path(directory)
            for index, (model, credential, value) in enumerate(cases):
                with self.subTest(model=model):
                    config_directory = (
                        application_state / "mini-swe" / str(index) / "config"
                    )
                    environment = mini_swe_environment(
                        model,
                        config_directory,
                        source,
                    )

                    self.assertEqual(
                        environment,
                        {
                            "PATH": "/controller/bin",
                            "LANG": "C.UTF-8",
                            "LC_ALL": "C.UTF-8",
                            "SSL_CERT_FILE": "/controller/ca.pem",
                            "TMPDIR": "/controller/tmp",
                            credential: value,
                            "MSWEA_GLOBAL_CONFIG_DIR": str(config_directory),
                        },
                    )
                    self.assertTrue(config_directory.is_dir())
                    self.assertTrue(config_directory.is_relative_to(application_state))

    def test_environment_preserves_multi_value_and_alternative_credentials(
        self,
    ) -> None:
        source = {
            "PATH": "/controller/bin",
            "AWS_ACCESS_KEY_ID": "access-id",
            "AWS_SECRET_ACCESS_KEY": "secret-access-key",  # pragma: allowlist secret
            "AWS_SESSION_TOKEN": "session-token",  # pragma: allowlist secret
            "ANTHROPIC_OAUTH_TOKEN": "oauth-token",  # pragma: allowlist secret
            "OPENAI_API_KEY": "unrelated-openai-key",  # pragma: allowlist secret
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aws = mini_swe_environment("aws/bedrock-model", root / "aws", source)
            anthropic = mini_swe_environment(
                "anthropic/claude-test",
                root / "anthropic",
                source,
            )

        self.assertEqual(aws["AWS_ACCESS_KEY_ID"], "access-id")
        self.assertEqual(aws["AWS_SECRET_ACCESS_KEY"], "secret-access-key")
        self.assertEqual(aws["AWS_SESSION_TOKEN"], "session-token")
        self.assertNotIn("ANTHROPIC_OAUTH_TOKEN", aws)
        self.assertNotIn("OPENAI_API_KEY", aws)
        self.assertEqual(anthropic["ANTHROPIC_OAUTH_TOKEN"], "oauth-token")
        self.assertNotIn("ANTHROPIC_API_KEY", anthropic)
        self.assertNotIn("AWS_ACCESS_KEY_ID", anthropic)


class _RecordingFileBackedRunner:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        argv,
        request,
        cwd,
        timeout,
        *,
        environment,
        supervisor=None,
        run_id=None,
    ):
        self.calls.append(
            {
                "argv": list(argv),
                "request": request,
                "cwd": Path(cwd),
                "timeout": timeout,
                "environment": dict(environment),
                "supervisor": supervisor,
                "run_id": run_id,
            }
        )
        return subprocess.CompletedProcess(list(argv), 0, self.stdout, "")


class MiniSweInferenceBoundaryTests(unittest.TestCase):
    def test_inference_wires_endpoint_file_request_and_supervisor(
        self,
    ) -> None:
        decision = {"action": "finish", "reason": "complete"}
        runner = _RecordingFileBackedRunner(json.dumps(decision))
        supervisor = object()
        large_prompt = "repository evidence\n" + ("x" * 200_000)

        with tempfile.TemporaryDirectory() as directory:
            state_directory = Path(directory) / "application-state" / "run-7"
            with patch.dict(
                os.environ,
                {
                    "PATH": "/controller/bin",
                    "LANG": "C.UTF-8",
                    "OPENAI_API_KEY": "dedicated-model-key",  # pragma: allowlist secret
                    "HOME": "/home/poisoned",
                    "GITHUB_TOKEN": "ambient-github",
                },
                clear=True,
            ):
                inference = MiniSweInference(
                    "openai/gpt-explicit",
                    base_url="https://models.example.test/v1",
                    timeout=73,
                    runner=runner,
                    supervisor=supervisor,
                    run_id="run-7",
                )
                result = inference.infer(
                    system_prompt="Return one controller decision.",
                    prompt=large_prompt,
                    response_schema=_ACTION_SCHEMA,
                    state_directory=state_directory,
                )

        self.assertEqual(result, decision)
        self.assertEqual(len(runner.calls), 1)
        call = runner.calls[0]
        request = json.loads(str(call["request"]))
        self.assertEqual(request["model"], "openai/gpt-explicit")
        self.assertEqual(
            request["base_url"],
            "https://models.example.test/v1",
        )
        self.assertEqual(
            request["system_prompt"],
            "Return one controller decision.",
        )
        self.assertEqual(request["prompt"], large_prompt)
        self.assertEqual(request["response_schema"], _ACTION_SCHEMA)
        self.assertNotIn(large_prompt, call["argv"])
        self.assertIn("repogents.mini_swe_worker", call["argv"])
        self.assertEqual(call["cwd"], state_directory)
        self.assertEqual(call["timeout"], 73)
        self.assertIs(call["supervisor"], supervisor)
        self.assertEqual(call["run_id"], "run-7")
        environment = call["environment"]
        self.assertEqual(
            environment["OPENAI_API_KEY"],
            "dedicated-model-key",
        )
        self.assertNotIn("HOME", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        config_directory = Path(environment["MSWEA_GLOBAL_CONFIG_DIR"])
        self.assertTrue(config_directory.is_relative_to(state_directory))

    def test_explicit_dashboard_key_replaces_ambient_key_without_entering_request(
        self,
    ) -> None:
        decision = {"action": "finish", "reason": "complete"}
        runner = _RecordingFileBackedRunner(json.dumps(decision))

        with tempfile.TemporaryDirectory() as directory:
            state_directory = Path(directory) / "state"
            with patch.dict(
                os.environ,
                {
                    "PATH": "/controller/bin",
                    "OPENAI_API_KEY": "ambient-key",  # pragma: allowlist secret
                },
                clear=True,
            ):
                inference = MiniSweInference(
                    "openai/gpt-explicit",
                    api_key="dashboard-key",  # pragma: allowlist secret
                    runner=runner,
                )
                inference.infer(
                    system_prompt="Return one controller decision.",
                    prompt="Choose an action.",
                    response_schema=_ACTION_SCHEMA,
                    state_directory=state_directory,
                )

        call = runner.calls[0]
        self.assertEqual(
            call["environment"]["OPENAI_API_KEY"],
            "dashboard-key",
        )
        self.assertNotIn("dashboard-key", str(call["request"]))
        self.assertNotIn("dashboard-key", str(call["argv"]))

    def test_inference_passes_resolved_images_only_in_file_request(
        self,
    ) -> None:
        decision = {"action": "finish", "reason": "image reviewed"}
        runner = _RecordingFileBackedRunner(json.dumps(decision))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "capture.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage-pixels")
            state_directory = root / "state"
            inference = MiniSweInference(
                "openai/gpt-explicit",
                runner=runner,
            )
            result = inference.infer(
                system_prompt="Review one image.",
                prompt="Does the image show the expected state?",
                response_schema=_ACTION_SCHEMA,
                state_directory=state_directory,
                image_paths=(image,),
            )

        self.assertEqual(result, decision)
        call = runner.calls[0]
        request = json.loads(str(call["request"]))
        self.assertEqual(request["image_paths"], [str(image.resolve())])
        self.assertNotIn(str(image), call["argv"])

    def test_timeout_from_inference_is_propagated_by_file_backed_runner(
        self,
    ) -> None:
        class TimingOutRunner(_RecordingFileBackedRunner):
            def __call__(self, argv, request, cwd, timeout, **kwargs):
                super().__call__(argv, request, cwd, timeout, **kwargs)
                raise subprocess.TimeoutExpired(argv, timeout)

        runner = TimingOutRunner("")
        with tempfile.TemporaryDirectory() as directory:
            inference = MiniSweInference(
                "openai/gpt-explicit",
                timeout=0.25,
                runner=runner,
                supervisor=object(),
                run_id="run-timeout",
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                inference.infer(
                    system_prompt="system",
                    prompt="prompt",
                    response_schema=_ACTION_SCHEMA,
                    state_directory=Path(directory),
                )

        self.assertEqual(runner.calls[0]["timeout"], 0.25)
        self.assertEqual(runner.calls[0]["run_id"], "run-timeout")


class MiniSweWorkerTests(unittest.TestCase):
    def test_worker_emits_one_schema_valid_object_and_normalized_trajectory(
        self,
    ) -> None:
        decision = {"action": "finish", "reason": "complete"}
        provider_secret = "raw-provider-secret-payload"  # pragma: allowlist secret
        observed: dict[str, Any] = {}

        class FakeLiteLLM:
            abort_exceptions = [TimeoutError]

            def __init__(self, **kwargs) -> None:
                observed["model_arguments"] = kwargs

        class FakeDefaultAgent:
            def __init__(self, model, environment, **kwargs) -> None:
                observed["model"] = model
                observed["environment"] = environment
                observed["agent_arguments"] = kwargs
                self.messages = [
                    {
                        "role": "system",
                        "content": "Return one decision",
                    },
                    {
                        "role": "assistant",
                        "content": json.dumps(decision),
                        "raw_response": {
                            "id": "provider-response-id",
                            "authorization": provider_secret,
                        },
                    },
                ]

            def run(self, task: str = "", **kwargs):
                observed["task"] = task
                try:
                    observed["environment"].execute({"command": "git status"})
                except Exception:
                    observed["repository_action_rejected"] = True
                else:
                    observed["repository_action_rejected"] = False
                return {"submission": json.dumps(decision)}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_directory = root / "application-state" / "run-1"
            request_path = root / "request.json"
            response_path = root / "response.json"
            request_path.write_text(
                json.dumps(
                    {
                        "model": "openai/codex/gpt-5.6-terra:medium",
                        "base_url": "https://models.example.test/v1",
                        "system_prompt": "Return one decision",
                        "prompt": "Inspect the supplied controller context",
                        "response_schema": _ACTION_SCHEMA,
                        "state_directory": str(state_directory),
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(mini_swe_worker, "LitellmModel", FakeLiteLLM),
                patch.object(
                    mini_swe_worker,
                    "DefaultAgent",
                    FakeDefaultAgent,
                ),
            ):
                exit_code = mini_swe_worker.main(
                    [str(request_path), str(response_path)]
                )

            self.assertIn(exit_code, (None, 0))
            self.assertEqual(
                json.loads(response_path.read_text(encoding="utf-8")),
                decision,
            )
            trajectories = []
            for path in state_directory.rglob("*.json"):
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and (
                    "trajectory_format" in value or "version" in value
                ):
                    trajectories.append(value)
            self.assertEqual(len(trajectories), 1)
            trajectory = trajectories[0]

        model_arguments = observed["model_arguments"]
        self.assertIn(TimeoutError, observed["model"].abort_exceptions)
        self.assertIn(
            "BadRequestError",
            {error.__name__ for error in observed["model"].abort_exceptions},
        )
        self.assertEqual(
            model_arguments["model_name"],
            "openai/codex/gpt-5.6-terra",
        )
        self.assertEqual(
            model_arguments["model_kwargs"]["api_base"],
            "https://models.example.test/v1",
        )
        self.assertEqual(
            model_arguments["model_kwargs"]["reasoning_effort"],
            "medium",
        )
        self.assertEqual(
            observed["task"],
            "Inspect the supplied controller context",
        )
        self.assertTrue(observed["repository_action_rejected"])
        serialized = json.dumps(trajectory, sort_keys=True)
        self.assertIn("finish", serialized)
        self.assertIn("complete", serialized)
        self.assertNotIn("raw_response", serialized)
        self.assertNotIn("provider-response-id", serialized)
        self.assertNotIn(provider_secret, serialized)

    def test_worker_expands_images_without_persisting_pixel_bytes(
        self,
    ) -> None:
        decision = {"action": "finish", "reason": "pixels reviewed"}
        observed: dict[str, Any] = {}
        image_body = b"\x89PNG\r\n\x1a\nprivate-image-pixels"

        class FakeLiteLLM:
            def __init__(self, **kwargs) -> None:
                observed["model_arguments"] = kwargs

        class FakeDefaultAgent:
            def __init__(self, model, environment, **kwargs) -> None:
                del model, environment, kwargs
                self.n_calls = 1

            def run(self, task: str = "", **kwargs):
                del kwargs
                observed["task"] = task
                return {"submission": json.dumps(decision)}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_directory = root / "state"
            image = root / "capture.png"
            image.write_bytes(image_body)
            request_path = root / "request.json"
            response_path = root / "response.json"
            request_path.write_text(
                json.dumps(
                    {
                        "model": "openai/codex/gpt-5.6-terra",
                        "base_url": "https://models.example.test/v1",
                        "system_prompt": "Review one screenshot.",
                        "prompt": "Judge only visible pixels.",
                        "response_schema": _ACTION_SCHEMA,
                        "state_directory": str(state_directory),
                        "image_paths": [str(image)],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(mini_swe_worker, "LitellmModel", FakeLiteLLM),
                patch.object(
                    mini_swe_worker,
                    "DefaultAgent",
                    FakeDefaultAgent,
                ),
            ):
                mini_swe_worker.main([str(request_path), str(response_path)])
            response_value = json.loads(response_path.read_text(encoding="utf-8"))

            trajectories = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in state_directory.rglob("*.json")
                if "trajectory-" in path.name
            ]

        encoded = base64.b64encode(image_body).decode("ascii")
        self.assertIn(f"data:image/png;base64,{encoded}", observed["task"])
        self.assertTrue(observed["model_arguments"]["multimodal_regex"])
        self.assertEqual(response_value, decision)
        self.assertEqual(len(trajectories), 1)
        self.assertNotIn(encoded, json.dumps(trajectories[0], sort_keys=True))

    def test_worker_stdout_contains_only_the_final_decision(self) -> None:
        decision = {"action": "finish", "reason": "clean channel"}

        class FakeLiteLLM:
            def __init__(self, **kwargs) -> None:
                pass

        class NoisyFakeAgent:
            def __init__(self, model, environment, **kwargs) -> None:
                self.messages = []
                self.n_calls = 1

            def run(self, task: str = "", **kwargs):
                print("Provider List: diagnostic noise")
                return {"submission": json.dumps(decision)}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "model": "openai/codex/gpt-5.4-mini",
                        "base_url": "https://models.example.test/v1",
                        "system_prompt": "system",
                        "prompt": "prompt",
                        "response_schema": _ACTION_SCHEMA,
                        "state_directory": str(root / "state"),
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with (
                patch.object(
                    mini_swe_worker,
                    "LitellmModel",
                    FakeLiteLLM,
                ),
                patch.object(
                    mini_swe_worker,
                    "DefaultAgent",
                    NoisyFakeAgent,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                mini_swe_worker.main([str(request_path)])

        self.assertEqual(
            stdout.getvalue(),
            json.dumps(
                decision,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
        )

    def test_worker_retries_schema_invalid_action_as_format_error(self) -> None:
        decision = {"action": "finish", "reason": "corrected"}
        observed: dict[str, Any] = {}

        class FakeFormatError(Exception):
            def __init__(self, *messages: dict[str, object]) -> None:
                self.messages = messages
                super().__init__()

        class FakeSubmitted(Exception):
            def __init__(self, *messages: dict[str, object]) -> None:
                self.messages = messages
                super().__init__()

        class FakeLiteLLM:
            def __init__(self, **kwargs) -> None:
                pass

        class RecoveringFakeAgent:
            def __init__(self, model, environment, **kwargs) -> None:
                self.environment = environment
                self.messages = []
                self.n_calls = 2

            def run(self, task: str = "", **kwargs):
                try:
                    self.environment.execute(
                        {
                            "command": (
                                "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
                                '{"action":"unknown","reason":"bad"}'
                            ),
                            "tool_call_id": "call-invalid",
                        }
                    )
                except FakeFormatError as error:
                    observed["format_messages"] = error.messages
                else:
                    raise AssertionError("invalid decision was not recoverable")
                try:
                    self.environment.execute(
                        {
                            "command": (
                                "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
                                + json.dumps(decision)
                            ),
                            "tool_call_id": "call-corrected",
                        }
                    )
                except FakeSubmitted as submitted:
                    return submitted.messages[0]["extra"]
                raise AssertionError("corrected decision was not submitted")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.json"
            response_path = root / "response.json"
            request_path.write_text(
                json.dumps(
                    {
                        "model": "openai/gpt-explicit",
                        "base_url": None,
                        "system_prompt": "system",
                        "prompt": "prompt",
                        "response_schema": _ACTION_SCHEMA,
                        "state_directory": str(root / "state"),
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(
                    mini_swe_worker,
                    "LitellmModel",
                    FakeLiteLLM,
                ),
                patch.object(
                    mini_swe_worker,
                    "DefaultAgent",
                    RecoveringFakeAgent,
                ),
                patch.object(
                    mini_swe_worker,
                    "FormatError",
                    FakeFormatError,
                    create=True,
                ),
                patch.object(
                    mini_swe_worker,
                    "Submitted",
                    FakeSubmitted,
                ),
            ):
                mini_swe_worker.main([str(request_path), str(response_path)])
            result = json.loads(response_path.read_text(encoding="utf-8"))

        self.assertEqual(result, decision)
        format_messages = observed["format_messages"]
        self.assertIn("schema", str(format_messages).lower())
        self.assertEqual(format_messages[0]["role"], "tool")
        self.assertEqual(
            format_messages[0]["tool_call_id"],
            "call-invalid",
        )

    def test_worker_rejects_multiple_or_schema_invalid_decisions(self) -> None:
        invalid_submissions = {
            "multiple": (
                '{"action":"finish","reason":"first"}\n'
                '{"action":"block","reason":"second"}'
            ),
            "wrong schema": '{"action":"unknown","reason":"bad"}',
        }

        for label, submission in invalid_submissions.items():
            with self.subTest(label=label):

                class FakeLiteLLM:
                    def __init__(self, **kwargs) -> None:
                        pass

                class FakeDefaultAgent:
                    def __init__(self, model, environment, **kwargs) -> None:
                        self.messages = []

                    def run(self, task: str = "", **kwargs):
                        return {"submission": submission}

                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    request_path = root / "request.json"
                    response_path = root / "response.json"
                    request_path.write_text(
                        json.dumps(
                            {
                                "model": "openai/gpt-explicit",
                                "base_url": None,
                                "system_prompt": "system",
                                "prompt": "prompt",
                                "response_schema": _ACTION_SCHEMA,
                                "state_directory": str(root / "state"),
                            }
                        ),
                        encoding="utf-8",
                    )
                    with (
                        patch.object(
                            mini_swe_worker,
                            "LitellmModel",
                            FakeLiteLLM,
                        ),
                        patch.object(
                            mini_swe_worker,
                            "DefaultAgent",
                            FakeDefaultAgent,
                        ),
                    ):
                        with self.assertRaisesRegex(
                            (TypeError, ValueError),
                            "single|one|schema|valid|object|JSON",
                        ):
                            mini_swe_worker.main(
                                [str(request_path), str(response_path)]
                            )
                    self.assertFalse(response_path.exists())

    def test_permanent_model_errors_abort_while_transient_errors_retry(self) -> None:
        import litellm
        from minisweagent.models.litellm_model import (
            LitellmModel as HarnessLitellmModel,
        )
        from tenacity import (
            Retrying,
            retry_if_not_exception_type,
            stop_after_attempt,
            wait_none,
        )

        adapter = HarnessLitellmModel(
            model_name="openai/test-model",
            model_kwargs={},
            cost_tracking="ignore_errors",
        )
        mini_swe_worker._abort_permanent_model_errors(adapter)

        def immediate_retry(*, logger, abort_exceptions):
            del logger
            return Retrying(
                reraise=True,
                stop=stop_after_attempt(2),
                wait=wait_none(),
                retry=retry_if_not_exception_type(tuple(abort_exceptions)),
            )

        permanent_calls = 0

        def raise_permanent(*args, **kwargs):
            nonlocal permanent_calls
            permanent_calls += 1
            raise litellm.exceptions.BadRequestError(
                "model not found",
                "test-model",
                "openai",
            )

        transient_calls = 0

        def raise_transient(*args, **kwargs):
            nonlocal transient_calls
            transient_calls += 1
            raise ConnectionError("temporary provider outage")

        messages = [{"role": "user", "content": "test"}]
        with patch(
            "minisweagent.models.litellm_model.retry",
            side_effect=immediate_retry,
        ):
            with patch.object(adapter, "_query", side_effect=raise_permanent):
                with self.assertRaises(litellm.exceptions.BadRequestError):
                    adapter.query(messages)
            with patch.object(adapter, "_query", side_effect=raise_transient):
                with self.assertRaises(ConnectionError):
                    adapter.query(messages)

        self.assertEqual(permanent_calls, 1)
        self.assertEqual(transient_calls, 2)


if __name__ == "__main__":
    unittest.main()
