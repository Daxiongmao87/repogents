from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .controller import RunProcessSupervisor, require_explicit_model, run_file_backed

MINI_SWE_RUNTIME = "mini-swe-agent"

_MINI_SWE_BASE_ENVIRONMENT_KEYS = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NODE_EXTRA_CA_CERTS",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
}
_PROVIDER_CREDENTIAL_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN"),
    "aws": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"),
    "azure": ("AZURE_OPENAI_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "gemini": ("GEMINI_API_KEY",),
    "google": ("GEMINI_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "ollama-cloud": ("OLLAMA_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openai-codex": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "vercel": ("AI_GATEWAY_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "zai": ("ZAI_API_KEY",),
}
_MULTI_VALUE_CREDENTIAL_PROVIDERS = {"aws"}


FileBackedRunner = Callable[..., subprocess.CompletedProcess[str]]


def model_api_key_variables(model: str) -> tuple[str, ...]:
    explicit_model = require_explicit_model(model)
    provider = explicit_model.split("/", 1)[0].lower()
    return _PROVIDER_CREDENTIAL_KEYS.get(provider, ())


def model_managed_api_key_variable(model: str) -> str | None:
    explicit_model = require_explicit_model(model)
    provider = explicit_model.split("/", 1)[0].lower()
    if provider in _MULTI_VALUE_CREDENTIAL_PROVIDERS:
        return None
    variables = _PROVIDER_CREDENTIAL_KEYS.get(provider, ())
    return variables[0] if variables else None


def mini_swe_environment(
    model: str,
    config_directory: Path,
    source: Mapping[str, str] | None = None,
    *,
    api_key: str | None = None,
) -> dict[str, str]:
    """Build the credential-minimal environment for one mini-SWE worker."""

    explicit_model = require_explicit_model(model)
    values = os.environ if source is None else source
    environment = {
        key: value
        for key, value in values.items()
        if key in _MINI_SWE_BASE_ENVIRONMENT_KEYS
    }
    credential_keys = model_api_key_variables(explicit_model)
    if api_key is not None:
        if not api_key:
            raise ValueError("configured model API key cannot be blank")
        credential_key = model_managed_api_key_variable(explicit_model)
        if credential_key is None:
            raise ValueError(
                f"model provider does not support a managed API key: {explicit_model}"
            )
        environment[credential_key] = api_key
    else:
        for key in credential_keys:
            value = values.get(key)
            if value:
                environment[key] = value

    directory = Path(config_directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    environment["MSWEA_GLOBAL_CONFIG_DIR"] = str(directory)
    return environment


class MiniSweInference:
    """Invoke a pinned mini-SWE worker through a file-backed JSON boundary."""

    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 600,
        runner: FileBackedRunner | None = None,
        supervisor: RunProcessSupervisor | None = None,
        run_id: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("mini-SWE inference timeout must be positive")
        if runner is None:
            self.model = require_explicit_model(model)
        else:
            self.model = (
                model.strip() if isinstance(model, str) and model.strip() else None
            )
        if base_url is not None and not base_url.strip():
            raise ValueError("mini-SWE model base URL cannot be blank")
        self.base_url = base_url
        if api_key is not None and not api_key:
            raise ValueError("mini-SWE model API key cannot be blank")
        self.api_key = api_key
        self.timeout = timeout
        self.runner = runner or run_file_backed
        self.supervisor = supervisor
        self.run_id = run_id

    def infer(
        self,
        *,
        system_prompt: str,
        prompt: str,
        response_schema: Mapping[str, object],
        state_directory: Path,
        image_paths: Sequence[Path] = (),
    ) -> dict[str, object]:
        model = require_explicit_model(self.model)
        state = Path(state_directory).expanduser().resolve()
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        images: list[str] = []
        for image_path in image_paths:
            image = Path(image_path).expanduser().resolve(strict=True)
            if not image.is_file():
                raise ValueError("mini-SWE inference image must be a file")
            images.append(str(image))
        request = json.dumps(
            {
                "model": model,
                "base_url": self.base_url,
                "system_prompt": system_prompt,
                "prompt": prompt,
                "response_schema": dict(response_schema),
                "state_directory": str(state),
                "timeout": self.timeout,
                "image_paths": images,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        argv = [sys.executable, "-m", "repogents.mini_swe_worker"]
        result = self.runner(
            argv,
            request,
            state,
            self.timeout,
            environment=mini_swe_environment(
                model,
                state / "config",
                api_key=self.api_key,
            ),
            supervisor=self.supervisor,
            run_id=self.run_id,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"mini-SWE worker exited {result.returncode}: {detail}")
        value = _single_json_object(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("mini-SWE worker decision must be a JSON object")
        return value


def _single_json_object(value: str) -> Any:
    decoder = json.JSONDecoder()
    content = value.strip()
    if not content:
        raise ValueError("mini-SWE worker returned no decision")
    try:
        parsed, end = decoder.raw_decode(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"mini-SWE worker returned invalid JSON: {error}") from error
    if content[end:].strip():
        raise ValueError("mini-SWE worker returned more than one decision")
    return parsed
