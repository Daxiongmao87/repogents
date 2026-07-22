from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path


_BASE_ENVIRONMENT_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NODE_EXTRA_CA_CERTS",
    "PATH",
    "PI_CODING_AGENT_DIR",
    "PI_PACKAGE_DIR",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
}

_PROVIDER_ENVIRONMENT_KEYS = {
    "anthropic": {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_OAUTH_TOKEN",
        "CLAUDE_CODE_CLIENT_CERT",
        "CLAUDE_CODE_CLIENT_KEY",
        "CLAUDE_CODE_USE_FOUNDRY",
        "FOUNDRY_BASE_URL",
    },
    "aws": {"AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"},
    "azure": {"AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"},
    "cerebras": {"CEREBRAS_API_KEY"},
    "cursor": {"CURSOR_ACCESS_TOKEN"},
    "deepseek": {"DEEPSEEK_API_KEY"},
    "gemini": {"GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_PROJECT"},
    "github-copilot": {"COPILOT_GITHUB_TOKEN"},
    "google": {"GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_PROJECT"},
    "groq": {"GROQ_API_KEY"},
    "kilo": {"KILO_API_KEY"},
    "minimax": {"MINIMAX_API_KEY"},
    "mistral": {"MISTRAL_API_KEY"},
    "ollama-cloud": {"OLLAMA_API_KEY"},
    "openai": {"OPENAI_API_KEY", "OPENAI_BASE_URL"},
    "openai-codex": {"OPENAI_API_KEY", "OPENAI_BASE_URL"},
    "opencode": {"OPENCODE_API_KEY"},
    "openrouter": {"OPENROUTER_API_KEY"},
    "vercel": {"AI_GATEWAY_API_KEY"},
    "xai": {"XAI_API_KEY"},
    "zai": {"ZAI_API_KEY"},
}


def require_explicit_model(model: str | None) -> str:
    """Return one explicit model selector without consulting host configuration."""

    if not isinstance(model, str) or not model.strip():
        raise ValueError("an explicit model selector is required")
    return model.strip()


def model_environment(
    model: str | None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the minimum host environment needed by one configured model."""

    values = os.environ if source is None else source
    allowed = set(_BASE_ENVIRONMENT_KEYS)
    if model:
        provider = model.split("/", 1)[0].lower()
        allowed.update(_PROVIDER_ENVIRONMENT_KEYS.get(provider, ()))
    return {key: value for key, value in values.items() if key in allowed}


class EnvironmentSecretResolver:
    """Resolve opaque secret references from controller-only host variables."""

    _REFERENCE = re.compile(r"^secret://([A-Za-z0-9][A-Za-z0-9_.-]*)$")

    def __init__(self, source: Mapping[str, str] | None = None) -> None:
        self._source = dict(os.environ if source is None else source)

    def __call__(self, reference: str) -> str:
        match = self._REFERENCE.fullmatch(reference)
        if match is None:
            raise ValueError(
                "secret references must use secret:// followed by a stable name"
            )
        suffix = re.sub(r"[^A-Za-z0-9]", "_", match.group(1)).upper()
        variable = f"REPOGENTS_SECRET_{suffix}"
        value = self._source.get(variable)
        if value is None or not value:
            raise KeyError(f"missing controller secret for {reference}")
        return value


@contextmanager
def git_environment(
    token: str | None,
    source: Mapping[str, str] | None = None,
) -> Generator[dict[str, str], None, None]:
    """Yield a controller-only Git environment with an ephemeral GitHub askpass."""

    values = os.environ if source is None else source
    environment = {
        key: value for key, value in values.items() if key in _BASE_ENVIRONMENT_KEYS
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Repogents",
            "GIT_AUTHOR_EMAIL": "repogents@localhost",
            "GIT_COMMITTER_NAME": "Repogents",
            "GIT_COMMITTER_EMAIL": "repogents@localhost",
        }
    )
    if not token:
        yield environment
        return

    with tempfile.TemporaryDirectory(prefix="repogents-git-askpass-") as directory:
        askpass = Path(directory) / "askpass.py"
        askpass.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ''\n"
            "print('x-access-token' if 'username' in prompt else "
            "os.environ['REPOGENTS_GITHUB_TOKEN'])\n",
            encoding="utf-8",
        )
        askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        environment["GIT_ASKPASS"] = str(askpass)
        environment["REPOGENTS_GITHUB_TOKEN"] = token
        yield environment


class RunProcessSupervisor:
    """Tracks and terminates controller processes owned by a run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, set[subprocess.Popen[str]]] = {}
        self._canceled: set[str] = set()

    def run(
        self,
        run_id: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        with self._lock:
            if run_id in self._canceled:
                return subprocess.CompletedProcess(
                    list(argv), -signal.SIGTERM, "", "run canceled"
                )
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            self._processes.setdefault(run_id, set()).add(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(
                list(argv), process.returncode, stdout, stderr
            )
        except subprocess.TimeoutExpired:
            self._terminate(process)
            process.communicate()
            raise
        finally:
            with self._lock:
                active = self._processes.get(run_id)
                if active is not None:
                    active.discard(process)
                    if not active:
                        self._processes.pop(run_id, None)

    def cancel(self, run_id: str) -> None:
        with self._lock:
            self._canceled.add(run_id)
            active = tuple(self._processes.get(run_id, ()))
        for process in active:
            self._terminate(process)

    def active(self, run_id: str) -> tuple[int, ...]:
        with self._lock:
            return tuple(
                process.pid
                for process in self._processes.get(run_id, ())
                if process.poll() is None
            )

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            process.wait(timeout=1)


def run_file_backed(
    argv: Sequence[str],
    prompt: str,
    cwd: Path,
    timeout: float,
    *,
    environment: Mapping[str, str],
    supervisor: RunProcessSupervisor | None = None,
    run_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a process with a temporary @file prompt rather than a large argv value."""

    prompt_path: Path | None = None
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="model-prompt-",
            suffix=".txt",
            dir=cwd,
            delete=False,
        ) as prompt_file:
            prompt_file.write(prompt)
            prompt_path = Path(prompt_file.name)
        command = [*argv, f"@{prompt_path}"]
        if supervisor is not None:
            if not run_id:
                raise ValueError("supervised process requires a run id")
            return supervisor.run(
                run_id,
                command,
                cwd=cwd,
                environment=environment,
                timeout=timeout,
            )
        return subprocess.run(
            command,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)
