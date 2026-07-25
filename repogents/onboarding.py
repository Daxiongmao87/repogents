from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .controller import git_environment
from .database import Database
from .mini_swe import MiniSweInference
from .github import RepositoryInfo
from .sandbox import (
    Mount,
    RestrictedNetworkPolicy,
    RunLayout,
    SandboxManager,
    SandboxPolicy,
)
from .team import DEFAULT_ACTION_TIMEOUT_SECONDS, validate_team_members

_SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "vendor",
    "target",
    "dist",
    "build",
    ".venv",
    "venv",
}
_INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "README.md")
_LOCKFILE_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
}
_MANIFEST_NAMES = {
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "Cargo.toml",
    "go.mod",
    "Makefile",
}

_INFERENCE_PROMPT_LIMIT_BYTES = 96_000
_INFERENCE_FILE_LIST_LIMIT_BYTES = 16_000
_INFERENCE_OBSERVATION_LIMIT_BYTES = 8_000


class MissingRepositoryInput(RuntimeError):
    def __init__(self, name: str, detail: str) -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"missing repository input {name}: {detail}")


@dataclass(frozen=True)
class RepositoryInspection:
    languages: tuple[str, ...]
    manifests: tuple[str, ...]
    lockfiles: tuple[str, ...]
    instruction_files: tuple[str, ...]
    validation_commands: tuple[tuple[str, ...], ...]
    file_count: int
    summary: str
    instructions: tuple[tuple[str, str], ...] = ()
    source_files: tuple[str, ...] = ()
    source_evidence: tuple[tuple[str, str], ...] = ()
    provisioning_commands: tuple[tuple[str, ...], ...] = ()
    dependency_services: tuple[str, ...] = ()

    def evidence(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("source_evidence")
        value["validation_commands"] = [
            list(command) for command in self.validation_commands
        ]
        value["provisioning_commands"] = [
            list(command) for command in self.provisioning_commands
        ]
        return value


@dataclass(frozen=True)
class RepositoryInference:
    languages: tuple[str, ...]
    manifests: tuple[str, ...]
    provisioning_commands: tuple[tuple[str, ...], ...]
    dependency_services: tuple[str, ...]
    validation_commands: tuple[tuple[str, ...], ...]


class RepositoryEvidenceAnalyzer(Protocol):
    def analyze(
        self,
        inspection: RepositoryInspection,
        *,
        prior_failure: str | None = None,
    ) -> RepositoryInference: ...


def _repository_inference_prompt(
    inspection: RepositoryInspection,
    *,
    prior_failure: str | None = None,
) -> str:
    task = (
        "Infer the repository's languages, operational manifests, "
        "dependency provisioning argv commands, required outbound "
        "dependency services, and complete validation argv commands "
        "from the supplied repository evidence. Include idempotent, "
        "noninteractive toolchain bootstrap commands before dependency "
        "commands whenever evidence requires an executable that may not "
        "exist in baseline Linux. Provisioning has no root privileges. "
        "/usr, /bin, /lib, and /lib64 are read-only; /etc is an empty "
        "non-writable directory. "
        "Do not invoke system package managers or commands that write "
        "outside /workspace, /repository-state, /repository-cache, "
        "/run-data, and /tmp. If a required tool is absent, bootstrap a "
        "repository-supported user-space artifact and place its "
        "executable under /repository-state/bin. Provisioning runs from "
        "a writable "
        "repository snapshot at /workspace with "
        "HOME=/repository-state/home. Direct every reusable dependency "
        "output to /run-data/dependency-delta; its complete tree is "
        "persisted and restored into later run checkouts. For Node "
        "projects, /workspace/node_modules is already a writable mount "
        "of /run-data/dependency-delta/node/node_modules; use it "
        "directly and never replace it with a symlink. Install or link "
        "every required executable under /repository-state/bin; "
        "tool-specific bin directories immediately below that "
        "persistent home are also discovered for later commands. "
        "Include every installer and package host in "
        "dependency_services. Do not assume a fixed language or "
        "package-manager allowlist. If prior_failure is present, treat it "
        "as observed execution evidence and return a complete corrected "
        "decision without repeating the failed assumption. Return only "
        "claims supported by the evidence."
    )
    ordered_files = _ordered_inference_files(inspection)
    sampled_files = _fit_json_items(
        ordered_files,
        _INFERENCE_FILE_LIST_LIMIT_BYTES,
    )
    observations = {
        "languages": _fit_json_items(
            inspection.languages,
            _INFERENCE_OBSERVATION_LIMIT_BYTES,
        ),
        "manifests": _fit_json_items(
            inspection.manifests,
            _INFERENCE_OBSERVATION_LIMIT_BYTES,
        ),
        "lockfiles": _fit_json_items(
            inspection.lockfiles,
            _INFERENCE_OBSERVATION_LIMIT_BYTES,
        ),
        "validation_commands": _fit_json_items(
            tuple(list(command) for command in inspection.validation_commands),
            _INFERENCE_OBSERVATION_LIMIT_BYTES,
        ),
    }
    contents: list[dict[str, object]] = []
    repository: dict[str, object] = {
        "file_count": inspection.file_count,
        "files": sampled_files,
        "contents": contents,
        "initial_observations": observations,
    }
    payload: dict[str, object] = {
        "task": task,
        "repository": repository,
        "response_schema": {
            "languages": ["nonempty string"],
            "manifests": ["path present in repository.files"],
            "provisioning_commands": [["argv", "..."]],
            "dependency_services": ["dns-host:port"],
            "validation_commands": [["argv", "..."]],
        },
    }
    if prior_failure:
        payload["prior_failure"] = _bounded_utf8(
            prior_failure,
            _INFERENCE_OBSERVATION_LIMIT_BYTES,
        )
    prompt = _compact_json(payload)
    if len(prompt.encode("utf-8")) > _INFERENCE_PROMPT_LIMIT_BYTES:
        raise RuntimeError("fixed repository inference context exceeds prompt limit")

    evidence_by_path = dict(inspection.source_evidence)
    for path in ordered_files:
        content = evidence_by_path.get(path)
        if content is None:
            continue
        entry: dict[str, object] = {"path": path, "content": content}
        contents.append(entry)
        candidate = _compact_json(payload)
        if len(candidate.encode("utf-8")) <= _INFERENCE_PROMPT_LIMIT_BYTES:
            prompt = candidate
            continue
        contents.pop()
        prefix = _largest_fitting_content_prefix(payload, contents, path, content)
        if prefix:
            contents.append(
                {
                    "path": path,
                    "content": prefix,
                    "truncated": len(prefix) < len(content),
                }
            )
            prompt = _compact_json(payload)
        break
    return prompt


def _ordered_inference_files(
    inspection: RepositoryInspection,
) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    groups = (
        inspection.instruction_files,
        inspection.manifests,
        inspection.lockfiles,
        tuple(path for path, _content in inspection.source_evidence),
        inspection.source_files,
    )
    for group in groups:
        for path in group:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
    return tuple(ordered)


def _fit_json_items(
    values: Sequence[object],
    byte_limit: int,
) -> list[object]:
    retained: list[object] = []
    used = 2
    for value in values:
        encoded = _compact_json(value).encode("utf-8")
        additional = len(encoded) + (1 if retained else 0)
        if used + additional > byte_limit:
            break
        retained.append(value)
        used += additional
    return retained


def _largest_fitting_content_prefix(
    payload: dict[str, object],
    contents: list[dict[str, object]],
    path: str,
    content: str,
) -> str:
    lower = 0
    upper = len(content)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        contents.append(
            {
                "path": path,
                "content": content[:middle],
                "truncated": middle < len(content),
            }
        )
        fits = (
            len(_compact_json(payload).encode("utf-8")) <= _INFERENCE_PROMPT_LIMIT_BYTES
        )
        contents.pop()
        if fits:
            lower = middle
        else:
            upper = middle - 1
    return content[:lower]


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _bounded_utf8(value: str, byte_limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    return encoded[:byte_limit].decode("utf-8", "ignore")


class MiniSweRepositoryEvidenceAnalyzer:
    """Infers repository operations from a bounded, ecosystem-neutral packet."""

    _RESPONSE_SCHEMA: dict[str, object] = {
        "type": "object",
        "properties": {
            "languages": {
                "type": "array",
                "items": {"type": "string"},
            },
            "manifests": {
                "type": "array",
                "items": {"type": "string"},
            },
            "provisioning_commands": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "dependency_services": {
                "type": "array",
                "items": {"type": "string"},
            },
            "validation_commands": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "required": [
            "languages",
            "manifests",
            "provisioning_commands",
            "dependency_services",
            "validation_commands",
        ],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        model: str | None,
        state_root: Path,
        base_url: str | None = None,
        api_key: str | None = None,
        configuration_resolver: (
            Callable[[], tuple[str, str | None, str | None]] | None
        ) = None,
        timeout: float = 600,
    ) -> None:
        if configuration_resolver is None and (
            not isinstance(model, str) or not model.strip()
        ):
            raise ValueError("repository evidence analyzer requires an explicit model")
        if timeout <= 0:
            raise ValueError("repository evidence analyzer timeout must be positive")
        self.model = model
        self.state_root = Path(state_root).expanduser().resolve()
        self.base_url = base_url
        self.api_key = api_key
        self.configuration_resolver = configuration_resolver
        self.timeout = timeout

    def analyze(
        self,
        inspection: RepositoryInspection,
        *,
        prior_failure: str | None = None,
    ) -> RepositoryInference:
        prompt = _repository_inference_prompt(
            inspection,
            prior_failure=prior_failure,
        )
        state_directory = (
            self.state_root
            / uuid.uuid5(
                uuid.NAMESPACE_URL,
                prompt,
            ).hex
        )
        model = self.model
        base_url = self.base_url
        api_key = self.api_key
        if self.configuration_resolver is not None:
            model, base_url, api_key = self.configuration_resolver()
        inference = MiniSweInference(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=self.timeout,
        )
        try:
            value = inference.infer(
                system_prompt=(
                    "Return exactly one JSON object matching the requested "
                    "schema and no prose."
                ),
                prompt=prompt,
                response_schema=self._RESPONSE_SCHEMA,
                state_directory=state_directory,
            )
            return _parse_repository_inference(
                value,
                inspection.source_files,
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"invalid repository inference: {error}") from error


class GitHubRepositoryClient(Protocol):
    def get_repository(self, identity: str) -> RepositoryInfo: ...


class SourceManager(Protocol):
    def prepare(self, repository: RepositoryInfo, destination: Path) -> str: ...


ProvisioningSecretBinding = tuple[str, str, tuple[tuple[str, ...], ...]]


class TeamFormulator(Protocol):
    def formulate(
        self, inspection: RepositoryInspection
    ) -> list[dict[str, object]]: ...


class EnvironmentProvisioner(Protocol):
    def provision(
        self,
        *,
        repository_id: str,
        version: int,
        source_path: Path,
        sandbox_path: Path,
        inspection: RepositoryInspection,
        policy: SandboxPolicy,
        provisioning_commands: tuple[tuple[str, ...], ...],
        secret_bindings: tuple[ProvisioningSecretBinding, ...] = (),
    ) -> dict[str, object]: ...


class SandboxEnvironmentProvisioner:
    """Builds reusable dependency state through the repository sandbox."""

    def __init__(
        self,
        *,
        data_root: Path,
        sandbox: SandboxManager,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.sandbox = sandbox
        self.secret_resolver = secret_resolver

    def provision(
        self,
        *,
        repository_id: str,
        version: int,
        source_path: Path,
        sandbox_path: Path,
        inspection: RepositoryInspection,
        policy: SandboxPolicy,
        provisioning_commands: tuple[tuple[str, ...], ...],
        secret_bindings: tuple[ProvisioningSecretBinding, ...] = (),
    ) -> dict[str, object]:
        requirements = source_path / "requirements.txt"
        package = source_path / "package.json"
        package_lock = source_path / "package-lock.json"
        layout = RunLayout.create(
            self.data_root,
            repository_id,
            f"onboarding-v{version}",
        )
        shutil.copytree(
            source_path,
            layout.checkout,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git"),
        )
        records: list[dict[str, object]] = []

        def command_secrets(command: tuple[str, ...]) -> dict[str, str] | None:
            matched = [
                (name, reference)
                for name, reference, commands in secret_bindings
                if command in commands
            ]
            if not matched:
                return None
            if self.secret_resolver is None:
                raise RuntimeError(
                    "environment provisioning requires a controller secret resolver"
                )
            return {
                name: self.secret_resolver(reference) for name, reference in matched
            }

        def run(command: tuple[str, ...], timeout: float = 600) -> None:
            result = self.sandbox.run(
                policy,
                layout,
                command,
                secrets=command_secrets(command),
                timeout=timeout,
                persistent_writable=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "environment provisioning command failed: "
                    + (result.stderr.strip() or " ".join(command))
                )
            records.append({"command": list(command), "log_path": str(result.log_path)})

        if provisioning_commands:
            for command in provisioning_commands:
                run(command)
        else:
            if requirements.is_file():
                run(
                    (
                        "python3",
                        "-m",
                        "venv",
                        "/repository-state/python-venv",
                    )
                )
                run(
                    (
                        "/repository-state/python-venv/bin/python3",
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--requirement",
                        "/mnt/inputs/source/requirements.txt",
                    )
                )
            if package.is_file():
                run(
                    (
                        "npm",
                        "ci" if package_lock.is_file() else "install",
                    )
                )
        if any(layout.dependency_delta.iterdir()):
            shutil.copytree(
                layout.dependency_delta,
                sandbox_path / "dependencies",
                dirs_exist_ok=True,
                symlinks=True,
            )
        return {
            "commands": records,
            "manifests": list(inspection.manifests),
        }


class GitSourceManager:
    """Creates controller-owned source snapshots without passing credentials to repository code."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def prepare(self, repository: RepositoryInfo, destination: Path) -> str:
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with git_environment(self.token) as environment:
            clone = subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--branch",
                    repository.default_branch,
                    "--",
                    f"https://github.com/{repository.owner}/{repository.name}.git",
                    str(destination),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
                env=environment,
            )
            if clone.returncode != 0:
                raise RuntimeError(f"git clone failed: {clone.stderr.strip()}")
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=destination,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
                env=environment,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"cannot resolve cloned repository head: {result.stderr.strip()}"
                )
        sha = result.stdout.strip()
        if len(sha) != 40:
            raise RuntimeError("cloned repository returned an invalid commit SHA")
        return sha


class RepositoryInspector:
    """Derives non-executing repository evidence without target-specific rules."""

    def inspect(self, checkout: Path) -> RepositoryInspection:
        checkout_path = Path(checkout).expanduser()
        if checkout_path.is_symlink():
            raise RuntimeError(f"repository source is a symlink: {checkout_path}")
        try:
            source_root = checkout_path.resolve(strict=True)
        except OSError as error:
            raise RuntimeError(f"cannot inspect repository source: {error}") from error
        if not source_root.is_dir():
            raise RuntimeError(f"repository source is not a directory: {source_root}")
        relative_files: list[str] = []
        suffixes: set[str] = set()
        for walk_root, directories, files in os.walk(source_root):
            root_path = _safe_inspection_path(source_root, Path(walk_root))
            retained_directories: list[str] = []
            for name in sorted(directories):
                if name in _SKIP_DIRECTORIES:
                    continue
                _safe_inspection_path(source_root, root_path / name)
                retained_directories.append(name)
            directories[:] = retained_directories
            for name in sorted(files):
                path = _safe_inspection_path(source_root, root_path / name)
                relative = path.relative_to(source_root).as_posix()
                relative_files.append(relative)
                suffixes.add(path.suffix.lower())
                if len(relative_files) >= 50_000:
                    break
            if len(relative_files) >= 50_000:
                break

        manifests = tuple(
            sorted(
                path for path in relative_files if Path(path).name in _MANIFEST_NAMES
            )
        )
        lockfiles = tuple(
            sorted(
                path for path in relative_files if Path(path).name in _LOCKFILE_NAMES
            )
        )
        instruction_files = tuple(
            sorted(
                path for path in relative_files if Path(path).name in _INSTRUCTION_NAMES
            )
        )
        instruction_contents: list[tuple[str, str]] = []
        remaining_instruction_bytes = 256_000
        for relative in instruction_files:
            if remaining_instruction_bytes <= 0:
                break
            with _safe_inspection_path(
                source_root,
                Path(relative),
            ).open("rb") as source:
                raw = source.read(min(64_000, remaining_instruction_bytes))
            remaining_instruction_bytes -= len(raw)
            instruction_contents.append((relative, raw.decode("utf-8", "replace")))
        source_evidence = _bounded_source_evidence(source_root, relative_files)
        languages = self._languages(manifests, suffixes)
        commands = self._validation_commands(
            source_root, relative_files, manifests, lockfiles
        )
        summary = (
            f"{len(relative_files)} files; languages={','.join(languages) or 'unknown'}; "
            f"manifests={','.join(manifests) or 'none'}; "
            f"validation={'; '.join(' '.join(command) for command in commands) or 'not discovered'}"
        )
        return RepositoryInspection(
            languages=languages,
            manifests=manifests,
            lockfiles=lockfiles,
            instruction_files=instruction_files,
            validation_commands=commands,
            file_count=len(relative_files),
            summary=summary,
            instructions=tuple(instruction_contents),
            source_files=tuple(relative_files),
            source_evidence=source_evidence,
        )

    @staticmethod
    def _languages(manifests: Sequence[str], suffixes: set[str]) -> tuple[str, ...]:
        values: set[str] = set()
        manifest_names = {Path(path).name for path in manifests}
        if "package.json" in manifest_names or suffixes & {
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
        }:
            values.add("javascript")
        if (
            manifest_names
            & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"}
            or ".py" in suffixes
        ):
            values.add("python")
        if "Cargo.toml" in manifest_names or ".rs" in suffixes:
            values.add("rust")
        if "go.mod" in manifest_names or ".go" in suffixes:
            values.add("go")
        return tuple(sorted(values))

    def _validation_commands(
        self,
        checkout: Path,
        relative_files: Sequence[str],
        manifests: Sequence[str],
        lockfiles: Sequence[str],
    ) -> tuple[tuple[str, ...], ...]:
        commands: list[tuple[str, ...]] = []
        by_name = {Path(path).name: path for path in manifests}
        locks = {Path(path).name for path in lockfiles}
        package_path = by_name.get("package.json")
        if package_path:
            try:
                package = json.loads(
                    _safe_inspection_path(checkout, Path(package_path)).read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RuntimeError(f"cannot inspect {package_path}: {error}") from error
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            if not isinstance(scripts, dict):
                scripts = {}
            if "pnpm-lock.yaml" in locks:
                manager = "pnpm"
            elif "yarn.lock" in locks:
                manager = "yarn"
            elif locks & {"bun.lock", "bun.lockb"}:
                manager = "bun"
            else:
                manager = "npm"
            for script in ("test", "lint", "typecheck", "build"):
                if script not in scripts:
                    continue
                if manager == "npm" and script == "test":
                    commands.append(("npm", "test"))
                else:
                    commands.append((manager, "run", script))
        has_python = any(Path(path).suffix.lower() == ".py" for path in relative_files)
        has_python_tests = any(
            Path(path).suffix.lower() == ".py" and Path(path).name.startswith("test")
            for path in relative_files
        )
        has_pytest = False
        if "pyproject.toml" in by_name:
            text = _safe_inspection_path(
                checkout,
                Path(by_name["pyproject.toml"]),
            ).read_text(encoding="utf-8", errors="replace")
            has_pytest = "pytest" in text
            if has_pytest:
                commands.append(("python3", "-m", "pytest"))
        if has_python_tests and not has_pytest:
            commands.append(("python3", "-m", "unittest", "discover"))
        if has_python:
            commands.append(("python3", "-m", "compileall", "-q", "."))
        if "Cargo.toml" in by_name:
            commands.append(
                ("cargo", "test", "--locked")
                if "Cargo.lock" in locks
                else ("cargo", "test")
            )
        if "go.mod" in by_name:
            commands.append(("go", "test", "./..."))
        if "Makefile" in by_name:
            text = _safe_inspection_path(
                checkout,
                Path(by_name["Makefile"]),
            ).read_text(encoding="utf-8", errors="replace")
            if any(line.startswith("test:") for line in text.splitlines()):
                commands.append(("make", "test"))
        seen: set[tuple[str, ...]] = set()
        return tuple(
            command
            for command in commands
            if not (command in seen or seen.add(command))
        )


def _inspection_summary(
    file_count: int,
    languages: Sequence[str],
    manifests: Sequence[str],
    validation_commands: Sequence[Sequence[str]],
) -> str:
    return (
        f"{file_count} files; languages={','.join(languages) or 'unknown'}; "
        f"manifests={','.join(manifests) or 'none'}; "
        "validation="
        + (
            "; ".join(" ".join(command) for command in validation_commands)
            or "not discovered"
        )
    )


def _bounded_source_evidence(
    source_root: Path,
    relative_files: Sequence[str],
    *,
    total_limit: int = 2_000_000,
    file_limit: int = 64_000,
) -> tuple[tuple[str, str], ...]:
    remaining = total_limit
    evidence: list[tuple[str, str]] = []
    for relative in relative_files:
        if remaining <= 0:
            break
        path = _safe_inspection_path(source_root, Path(relative))
        with path.open("rb") as source:
            raw = source.read(min(file_limit, remaining))
        if b"\0" in raw:
            continue
        remaining -= len(raw)
        evidence.append((relative, raw.decode("utf-8", "replace")))
    return tuple(evidence)


def _safe_inspection_path(source_root: Path, path: Path) -> Path:
    root = Path(source_root).resolve(strict=True)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        raise RuntimeError(f"repository inspection rejects symlink: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RuntimeError(
            f"cannot resolve repository inspection path: {error}"
        ) from error
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(
            f"repository inspection path resolves outside source: {candidate}"
        )
    return resolved


class OnboardingService:
    def __init__(
        self,
        *,
        database: Database,
        data_root: Path,
        github: GitHubRepositoryClient,
        sources: SourceManager,
        inspector: RepositoryInspector,
        team_formulator: TeamFormulator,
        evidence_analyzer: RepositoryEvidenceAnalyzer | None = None,
        provisioner: EnvironmentProvisioner | None = None,
    ) -> None:
        self.database = database
        self.data_root = Path(data_root)
        self.github = github
        self.sources = sources
        self.inspector = inspector
        self.evidence_analyzer = evidence_analyzer
        self.team_formulator = team_formulator
        self.provisioner = provisioner

    def onboard(self, identity: str, inputs: dict[str, object] | None = None) -> str:
        repository = self.github.get_repository(identity)
        repository_id = f"github-{repository.database_id}"
        try:
            normalized_inputs = _normalize_repository_inputs(inputs or {})
        except Exception as error:
            normalized_inputs = {}
            initial_state = "blocked"
            blocking_reason = str(error)
        else:
            initial_state = "inspecting"
            blocking_reason = None
        now = _utc_now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                """SELECT onboarding_state, removed_at
                   FROM repositories WHERE id = ?""",
                (repository_id,),
            ).fetchone()
            if existing is not None and existing["onboarding_state"] == "ready":
                connection.execute(
                    """UPDATE repositories
                       SET enabled=1, removed_at=NULL, updated_at=?
                       WHERE id=?""",
                    (now, repository_id),
                )
                return repository_id
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, blocking_reason, inputs_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     github_node_id=excluded.github_node_id,
                     owner=excluded.owner,
                     name=excluded.name,
                     url=excluded.url,
                     default_branch=excluded.default_branch,
                     onboarding_state=excluded.onboarding_state,
                     blocking_reason=excluded.blocking_reason,
                     inputs_json=excluded.inputs_json,
                     enabled=1,
                     removed_at=NULL,
                     updated_at=excluded.updated_at""",
                (
                    repository_id,
                    repository.node_id,
                    repository.owner,
                    repository.name,
                    repository.url,
                    repository.default_branch,
                    initial_state,
                    blocking_reason,
                    _json(normalized_inputs),
                    now,
                    now,
                ),
            )
        if initial_state == "blocked":
            return repository_id
        try:
            return self._build_versions(repository_id, repository, normalized_inputs)
        except MissingRepositoryInput as error:
            self._set_failure(repository_id, "needs_input", str(error))
            return repository_id
        except Exception as error:
            self._set_failure(repository_id, "blocked", str(error))
            return repository_id

    def reonboard(
        self,
        repository_id: str,
        inputs: dict[str, object] | None = None,
    ) -> str:
        repository_record = self.get_repository(repository_id)
        prior_failure_value = repository_record["blocking_reason"]
        prior_failure = (
            str(prior_failure_value) if prior_failure_value is not None else None
        )
        try:
            retained_inputs = json.loads(str(repository_record["inputs_json"]))
            normalized_inputs = _normalize_repository_inputs(
                retained_inputs if inputs is None else inputs
            )
            repository = self.github.get_repository(
                f"{repository_record['owner']}/{repository_record['name']}"
            )
            if f"github-{repository.database_id}" != repository_id:
                raise RuntimeError(
                    "refreshed GitHub repository identity does not match stored repository"
                )
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE repositories
                       SET github_node_id=?, owner=?, name=?, url=?,
                           default_branch=?, onboarding_state='inspecting',
                           blocking_reason=NULL, inputs_json=?, updated_at=?
                       WHERE id=?""",
                    (
                        repository.node_id,
                        repository.owner,
                        repository.name,
                        repository.url,
                        repository.default_branch,
                        _json(normalized_inputs),
                        _utc_now(),
                        repository_id,
                    ),
                )
            return self._build_versions(
                repository_id,
                repository,
                normalized_inputs,
                prior_failure=prior_failure,
            )
        except MissingRepositoryInput as error:
            self._set_failure(repository_id, "needs_input", str(error))
        except Exception as error:
            self._set_failure(repository_id, "blocked", str(error))
        return repository_id

    def _build_versions(
        self,
        repository_id: str,
        repository: RepositoryInfo,
        inputs: dict[str, Any],
        *,
        prior_failure: str | None = None,
    ) -> str:
        repository_root = self.data_root / "repositories" / repository_id
        source_path = repository_root / "source"
        base_sha = self.sources.prepare(repository, source_path)
        inspection = self.inspector.inspect(source_path)
        if self.evidence_analyzer is not None:
            inference = self.evidence_analyzer.analyze(
                inspection,
                prior_failure=prior_failure,
            )
            inspection = replace(
                inspection,
                languages=inference.languages,
                manifests=inference.manifests,
                provisioning_commands=inference.provisioning_commands,
                dependency_services=inference.dependency_services,
                validation_commands=inference.validation_commands,
                summary=_inspection_summary(
                    inspection.file_count,
                    inference.languages,
                    inference.manifests,
                    inference.validation_commands,
                ),
            )
        validation_commands = (
            tuple(tuple(command) for command in inputs["validation_commands"])
            if "validation_commands" in inputs
            else inspection.validation_commands
        )
        if not validation_commands:
            raise MissingRepositoryInput(
                "validation_commands",
                "supply at least one command that can validate repository changes",
            )
        if "validation_commands" in inputs:
            inspection = replace(
                inspection,
                validation_commands=validation_commands,
            )
        provisioning_commands = (
            tuple(tuple(command) for command in inputs["provisioning_commands"])
            if "provisioning_commands" in inputs
            else inspection.provisioning_commands
        )
        members = [
            dict(
                member,
                action_timeout_seconds=member.get(
                    "action_timeout_seconds",
                    DEFAULT_ACTION_TIMEOUT_SECONDS,
                ),
            )
            for member in self.team_formulator.formulate(inspection)
        ]
        validate_team_members(members)
        with self.database.connect() as connection:
            sandbox_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM sandbox_versions WHERE repository_id=?",
                    (repository_id,),
                ).fetchone()[0]
            )
            team_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM team_versions WHERE repository_id=?",
                    (repository_id,),
                ).fetchone()[0]
            )
        sandbox_id = str(uuid.uuid4())
        team_id = str(uuid.uuid4())
        sandbox_path = repository_root / "sandbox" / str(sandbox_version)
        if sandbox_path.exists():
            shutil.rmtree(sandbox_path)
        sandbox_path.mkdir(parents=True, exist_ok=False)
        host_mounts = tuple(
            Mount(
                Path(str(value["path"])),
                str(value["target"]),
                writable=value["mode"] == "writable",
            )
            for value in inputs.get("allowed_host_paths", [])
        )
        inferred_services = set(_dependency_services(source_path))
        if self.evidence_analyzer is not None:
            inferred_services.update(inspection.dependency_services)
        allowed_services = tuple(
            sorted(set(inputs.get("allowed_services", [])) | set(inferred_services))
        )
        secret_bindings: tuple[ProvisioningSecretBinding, ...] = tuple(
            (
                str(value["name"]),
                str(value["reference"]),
                tuple(tuple(command) for command in value["commands"]),
            )
            for value in inputs.get("secret_bindings", [])
        )
        secret_names = tuple(value[0] for value in secret_bindings)
        sandbox_policy = SandboxPolicy(
            persistent_root=sandbox_path,
            mounts=(
                Mount(source_path, "/mnt/inputs/source", writable=False),
                *host_mounts,
            ),
            allowed_services=allowed_services,
            allowed_secret_names=secret_names,
        )
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE repositories
                   SET onboarding_state='provisioning', updated_at=?
                   WHERE id=?""",
                (_utc_now(), repository_id),
            )
        provisioning = (
            self.provisioner.provision(
                repository_id=repository_id,
                version=sandbox_version,
                source_path=source_path,
                sandbox_path=sandbox_path,
                inspection=inspection,
                policy=sandbox_policy,
                provisioning_commands=provisioning_commands,
                secret_bindings=secret_bindings,
            )
            if self.provisioner is not None
            else {"commands": [], "manifests": list(inspection.manifests)}
        )
        policy = {
            "version": sandbox_version,
            "base_sha": base_sha,
            "allowed_host_paths": inputs.get("allowed_host_paths", []),
            "allowed_services": list(allowed_services),
            "secret_bindings": inputs.get("secret_bindings", []),
            "persistent_root": str(sandbox_path),
        }
        evidence = inspection.evidence() | {
            "base_sha": base_sha,
            "provisioning": provisioning,
        }
        (sandbox_path / "policy.json").write_text(_json(policy), encoding="utf-8")
        (sandbox_path / "evidence.json").write_text(_json(evidence), encoding="utf-8")
        team_path = repository_root / "team"
        team_path.mkdir(parents=True, exist_ok=True)
        (team_path / f"{team_version}.json").write_text(
            _json({"version": team_version, "evidence": evidence, "members": members}),
            encoding="utf-8",
        )
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    sandbox_id,
                    repository_id,
                    sandbox_version,
                    str(sandbox_path),
                    _json(policy),
                    _json(evidence),
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json,
                    design_contract_version, created_at)
                   VALUES (?, ?, ?, ?, 2, ?)""",
                (team_id, repository_id, team_version, _json(evidence), now),
            )
            for member in members:
                connection.execute(
                    """INSERT INTO team_members
                       (id, team_version_id, stable_key, role, atomic_role,
                        responsibilities, permitted_tools_json, runtime, model,
                        instructions, action_timeout_seconds)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        team_id,
                        member["stable_key"],
                        member["execution_class"],
                        member["role"],
                        member["responsibilities"],
                        _json(member["permitted_tools"]),
                        member["runtime"],
                        member["model"],
                        member["instructions"],
                        member["action_timeout_seconds"],
                    ),
                )
            validation_source = (
                "repository input override"
                if "validation_commands" in inputs
                else (
                    "repository inference"
                    if self.evidence_analyzer is not None
                    else "repository inspection"
                )
            )
            for position, command in enumerate(validation_commands):
                connection.execute(
                    """INSERT INTO validation_commands
                       (id, sandbox_version_id, position, command_json, source, required)
                       VALUES (?, ?, ?, ?, ?, 1)""",
                    (
                        str(uuid.uuid4()),
                        sandbox_id,
                        position,
                        _json(list(command)),
                        validation_source,
                    ),
                )
            connection.execute(
                """UPDATE repositories
                   SET current_sandbox_version_id=?, current_team_version_id=?,
                       onboarding_state='ready', blocking_reason=NULL, updated_at=?
                   WHERE id=?""",
                (sandbox_id, team_id, now, repository_id),
            )
        return repository_id

    def recover_interrupted(self) -> tuple[str, ...]:
        """Make startup-interrupted onboarding records explicitly retriable."""
        now = _utc_now()
        recovered: list[str] = []
        with self.database.transaction() as connection:
            rows = connection.execute("""SELECT id, onboarding_state FROM repositories
                   WHERE onboarding_state IN ('inspecting', 'provisioning')
                   ORDER BY id""").fetchall()
            for row in rows:
                repository_id = str(row["id"])
                interrupted_state = str(row["onboarding_state"])
                connection.execute(
                    """UPDATE repositories
                       SET onboarding_state='blocked', blocking_reason=?, updated_at=?
                       WHERE id=? AND onboarding_state=?""",
                    (
                        f"onboarding interrupted during {interrupted_state}; "
                        "retry re-onboarding with the retained repository inputs",
                        now,
                        repository_id,
                        interrupted_state,
                    ),
                )
                recovered.append(repository_id)
        return tuple(recovered)

    def _set_failure(self, repository_id: str, state: str, reason: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE repositories SET onboarding_state=?, blocking_reason=?, updated_at=? WHERE id=?",
                (state, reason, _utc_now(), repository_id),
            )

    def list_repositories(self) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT * FROM repositories
                   WHERE removed_at IS NULL
                   ORDER BY owner COLLATE NOCASE, name COLLATE NOCASE""").fetchall()
        return [dict(row) for row in rows]

    def get_repository(self, repository_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM repositories WHERE id=?", (repository_id,)
            ).fetchone()
        if row is None:
            raise KeyError(repository_id)
        return dict(row)


def _normalize_repository_inputs(inputs: dict[str, object]) -> dict[str, Any]:
    allowed_keys = {
        "allowed_host_paths",
        "allowed_services",
        "secret_bindings",
        "provisioning_commands",
        "validation_commands",
    }
    unknown = sorted(set(inputs) - allowed_keys)
    if unknown:
        raise ValueError(f"unsupported repository input: {unknown[0]}")
    normalized: dict[str, Any] = {}

    if "allowed_host_paths" in inputs:
        values = inputs["allowed_host_paths"]
        if not isinstance(values, list):
            raise ValueError("allowed_host_paths must be a list")
        mounts: list[dict[str, str]] = []
        for index, value in enumerate(values):
            if not isinstance(value, dict) or "path" not in value:
                raise ValueError(f"allowed_host_paths[{index}] must contain path")
            mode = value.get("mode", "read-only")
            if mode not in {"read-only", "writable"}:
                raise ValueError(
                    f"allowed_host_paths[{index}] mode must be read-only or writable"
                )
            target = value.get("target", f"/mnt/inputs/{index}")
            if not isinstance(target, str) or not target:
                raise ValueError(f"allowed_host_paths[{index}] target must be a string")
            mount = Mount(
                Path(str(value["path"])),
                target,
                writable=mode == "writable",
            )
            mounts.append(
                {
                    "path": str(mount.host_path),
                    "target": mount.sandbox_path,
                    "mode": mode,
                }
            )
        normalized["allowed_host_paths"] = mounts

    if "allowed_services" in inputs:
        values = inputs["allowed_services"]
        if not isinstance(values, list):
            raise ValueError("allowed_services must be a list")
        services: list[str] = []
        for index, value in enumerate(values):
            if not isinstance(value, str):
                raise ValueError(f"allowed_services[{index}] must be a string")
            host, port = RestrictedNetworkPolicy._parse_rule(value)
            service = f"{host}:{port}"
            if service not in services:
                services.append(service)
        normalized["allowed_services"] = services

    if "secret_bindings" in inputs:
        values = inputs["secret_bindings"]
        if not isinstance(values, list):
            raise ValueError("secret_bindings must be a list")
        bindings: list[dict[str, object]] = []
        names: set[str] = set()
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise ValueError(f"secret_bindings[{index}] must be an object")
            name = value.get("name")
            reference = value.get("reference")
            if not isinstance(name, str) or not name:
                raise ValueError(f"secret_bindings[{index}] name must be a string")
            if name in names:
                raise ValueError(f"duplicate secret binding name: {name}")
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError(f"secret_bindings[{index}] reference must be a string")
            reference_value = reference.strip()
            if (
                re.fullmatch(
                    r"secret://[A-Za-z0-9][A-Za-z0-9_.-]*",
                    reference_value,
                )
                is None
            ):
                raise ValueError(
                    f"secret_bindings[{index}] reference must use secret://name"
                )
            commands = _normalize_commands(
                value.get("commands"),
                f"secret_bindings[{index}].commands",
                require_nonempty=True,
            )
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
                raise ValueError(
                    f"secret_bindings[{index}] name is not a valid environment name"
                )
            names.add(name)
            bindings.append(
                {
                    "name": name,
                    "reference": reference_value,
                    "commands": [list(command) for command in commands],
                }
            )
        normalized["secret_bindings"] = bindings

    for key in ("provisioning_commands", "validation_commands"):
        if key not in inputs:
            continue
        commands = _normalize_commands(
            inputs[key],
            key,
            require_nonempty=False,
        )
        if key == "validation_commands":
            commands = tuple(command for command in commands if command[0].strip())
        normalized[key] = [list(command) for command in commands]
    return normalized


def _normalize_commands(
    value: object,
    name: str,
    *,
    require_nonempty: bool,
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    if require_nonempty and not value:
        raise ValueError(f"{name} must contain at least one command")
    commands: list[tuple[str, ...]] = []
    for index, command in enumerate(value):
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(argument, str) and argument for argument in command)
        ):
            raise ValueError(f"{name}[{index}] must be a nonempty argv list")
        normalized = tuple(command)
        if normalized not in commands:
            commands.append(normalized)
    return tuple(commands)


def _parse_repository_inference(
    value: object,
    source_files: Sequence[str],
) -> RepositoryInference:
    if not isinstance(value, dict):
        raise TypeError("response must be an object")
    fields = {
        "languages",
        "manifests",
        "provisioning_commands",
        "dependency_services",
        "validation_commands",
    }
    missing = sorted(fields - set(value))
    unexpected = sorted(set(value) - fields)
    if missing:
        raise ValueError(f"missing field: {missing[0]}")
    if unexpected:
        raise ValueError(f"unexpected field: {unexpected[0]}")

    def strings(name: str) -> tuple[str, ...]:
        raw = value[name]
        if not isinstance(raw, list) or not all(
            isinstance(item, str) and item and item == item.strip() for item in raw
        ):
            raise ValueError(f"{name} must be a list of nonempty strings")
        return tuple(dict.fromkeys(raw))

    languages = strings("languages")
    manifests = strings("manifests")
    known_files = set(source_files)
    unknown_manifest = next(
        (manifest for manifest in manifests if manifest not in known_files),
        None,
    )
    if unknown_manifest is not None:
        raise ValueError(f"manifest is not present in repository: {unknown_manifest}")
    provisioning_commands = _normalize_commands(
        value["provisioning_commands"],
        "provisioning_commands",
        require_nonempty=False,
    )
    validation_commands = _normalize_commands(
        value["validation_commands"],
        "validation_commands",
        require_nonempty=False,
    )
    services: list[str] = []
    for service in strings("dependency_services"):
        host, port = RestrictedNetworkPolicy._parse_rule(service)
        if (
            re.fullmatch(
                r"(?:\*\.)?[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?",
                host,
            )
            is None
            or ".." in host
        ):
            raise ValueError(f"invalid dependency service: {service}")
        normalized = f"{host}:{port}"
        if normalized not in services:
            services.append(normalized)
    return RepositoryInference(
        languages=languages,
        manifests=manifests,
        provisioning_commands=provisioning_commands,
        dependency_services=tuple(services),
        validation_commands=validation_commands,
    )


def _dependency_services(source_path: Path) -> tuple[str, ...]:
    services: set[str] = set()
    if (source_path / "requirements.txt").is_file():
        services.update(("pypi.org:443", "files.pythonhosted.org:443"))
    if (source_path / "package.json").is_file():
        services.update(("nodejs.org:443", "registry.npmjs.org:443"))
    return tuple(sorted(services))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
