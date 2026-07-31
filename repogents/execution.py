from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .controller import RunProcessSupervisor
from .database import Database
from .lifecycle import RunLifecycle, RunState
from .mini_swe import MINI_SWE_RUNTIME, MiniSweInference
from .sandbox import (
    Mount,
    RestrictedNetworkPolicy,
    RunLayout,
    SandboxManager,
    SandboxPolicy,
    SecretScanner,
    redact_text,
)
from .specification import SpecificationService
from .team import Assignment, StoredTeam, TeamMember, TeamService
from .workflow import (
    DeterministicOperationRegistry,
    WorkflowCanceled,
    WorkflowExecutionEngine,
    WorkflowExecutionError,
    WorkflowNodeContext,
    WorkflowTemplate,
    WorkflowService,
    validate_workflow_output,
)
from .validation import compare_findings, extract_findings

_ACTION_HISTORY_LIMIT = 2_000
_PROBE_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_PROBE_ENVIRONMENT = frozenset(
    {
        "CARGO_HOME",
        "CHROME_BIN",
        "GOCACHE",
        "GOMODCACHE",
        "HOME",
        "LANG",
        "LC_ALL",
        "NODE_PATH",
        "PATH",
        "PIP_CACHE_DIR",
        "PIP_TARGET",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "TMPDIR",
        "VIRTUAL_ENV",
        "XDG_CACHE_HOME",
        "npm_config_cache",
        "npm_config_prefix",
    }
)
_SENSITIVE_PROBE_ENVIRONMENT = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE|ACCESS_KEY|API_KEY)",
    re.IGNORECASE,
)
_ACTION_FIELD_ORDER = (
    "action",
    "path",
    "argv",
    "dependency_services",
    "start",
    "end",
    "pattern",
    "count",
    "members",
    "reason",
    "summary",
    "output",
    "old",
    "new",
    "content",
)
_ACTION_STRING_LIMITS = {
    "action": 64,
    "path": 1_024,
    "argv": 96,
    "dependency_services": 253,
    "pattern": 512,
    "reason": 512,
    "summary": 512,
    "old": 256,
    "new": 256,
    "content": 256,
}

_VALIDATION_POLICY_NAMES = frozenset(
    {
        ".eslintignore",
        ".gitignore",
        ".prettierignore",
        ".stylelintignore",
        "Cargo.lock",
        "Cargo.toml",
        "Gemfile",
        "Gemfile.lock",
        "Makefile",
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
        "go.mod",
        "go.sum",
        "mypy.ini",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "pytest.ini",
        "ruff.toml",
        "setup.cfg",
        "tox.ini",
        "yarn.lock",
    }
)


def _is_validation_policy_path(value: str) -> bool:
    name = Path(value).name
    return (
        name in _VALIDATION_POLICY_NAMES
        or name.startswith((".eslintrc", ".prettierrc", "tsconfig."))
        or name.startswith(("jest.config.", "vitest.config."))
    )


_DIRECT_VALIDATION_IGNORE_NAMES = frozenset(
    {".eslintignore", ".prettierignore", ".stylelintignore"}
)
_SUPPRESSION_KEY = re.compile(
    r"\b(ignore|ignores|ignorepatterns|exclude|excludedfiles|omit|"
    r"extend-ignore|per-file-ignores)\b\s*[:=]",
    re.IGNORECASE,
)
_DISABLED_SETTING = re.compile(
    r"[:=]\s*(?:[\"'](?:off|warn)[\"']|0)\s*[,}]?\s*$",
    re.IGNORECASE,
)


def _validation_weakening_reasons(
    path: str,
    diff: str,
    baseline_findings: tuple[str, ...],
) -> tuple[str, ...]:
    name = Path(path).name.lower()
    additions = tuple(
        line[1:].strip()
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    meaningful = tuple(
        line for line in additions if line and not line.startswith(("#", "//"))
    )
    if name in _DIRECT_VALIDATION_IGNORE_NAMES and meaningful:
        return (f"{path}: added validation exclusion",)
    if name == ".gitignore" and any(
        _ignore_matches_finding(line, baseline_findings)
        for line in meaningful
        if not line.startswith("!")
    ):
        return (f"{path}: ignored a baseline finding path",)
    if any(_line_adds_suppression(line) for line in meaningful):
        return (f"{path}: added validation suppression or exclusion",)
    if "deleted file mode" in diff and _is_validation_config_path(name):
        return (f"{path}: removed validation configuration",)
    return ()


def _line_adds_suppression(line: str) -> bool:
    lowered = line.lower()
    if any(
        marker in lowered
        for marker in (
            "eslint-disable",
            "@ts-ignore",
            "# noqa",
            "|| true",
            "--max-warnings=-1",
        )
    ):
        return True
    if _DISABLED_SETTING.search(line):
        return True
    if re.search(
        r"\b(skiplibcheck|strict|noemitonerror)\b\s*[:=]\s*false\b",
        lowered,
    ):
        return True
    match = _SUPPRESSION_KEY.search(line)
    if match is None:
        return False
    value = line[match.end() :].strip().rstrip(",")
    return value.lower() not in {"", "[]", "{}", "false", "none", "null"}


def _ignore_matches_finding(
    entry: str,
    baseline_findings: tuple[str, ...],
) -> bool:
    pattern = entry.strip().lstrip("/")
    if not pattern:
        return False
    finding_paths = (
        finding.partition("|")[0] for finding in baseline_findings if "|" in finding
    )
    return any(
        candidate == pattern
        or candidate.startswith(pattern.rstrip("/") + "/")
        or Path(candidate).match(pattern)
        for candidate in finding_paths
    )


def _is_validation_config_path(name: str) -> bool:
    return name in {
        "mypy.ini",
        "pyproject.toml",
        "pytest.ini",
        "ruff.toml",
        "setup.cfg",
        "tox.ini",
    } or name.startswith(
        (
            ".eslintrc",
            ".prettierrc",
            "eslint.config.",
            "jest.config.",
            "tsconfig.",
            "vitest.config.",
        )
    )


_SOURCE_SUPPRESSION_MARKER = re.compile(
    r"(eslint-disable|@ts-(?:ignore|expect-error|nocheck)|"
    r"#\s*(?:noqa|type:\s*ignore|pyright:\s*ignore)|"
    r"rubocop:\s*disable|//\s*nolint\b|noinspection)",
    re.IGNORECASE,
)


def _source_suppression_analysis(
    path: str,
    diff: str,
) -> tuple[bool, tuple[str, ...]]:
    additions = tuple(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    marker_lines = tuple(
        line for line in additions if _SOURCE_SUPPRESSION_MARKER.search(line)
    )
    if not marker_lines:
        return False, ()
    if any(_is_broad_source_suppression(line) for line in marker_lines):
        return True, (f"{path}: added broad source suppression",)

    deletions = {
        line[1:].strip()
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    }
    substantive = False
    for line in additions:
        stripped = line.strip()
        if not stripped:
            continue
        if _SOURCE_SUPPRESSION_MARKER.search(line):
            remainder = _without_source_suppression(line)
            if remainder and remainder not in deletions:
                substantive = True
            continue
        if stripped.startswith(("#", "//", "/*", "*")):
            continue
        if stripped not in deletions:
            substantive = True
    if not substantive:
        return True, (f"{path}: added suppression without source behavior",)
    return True, ()


def _is_broad_source_suppression(line: str) -> bool:
    lowered = line.lower()
    return (
        "@ts-nocheck" in lowered
        or "flake8: noqa" in lowered
        or "ruff: noqa" in lowered
        or "mypy: ignore-errors" in lowered
        or re.search(
            r"eslint-disable(?!-(?:next-line|line))",
            lowered,
        )
        is not None
    )


def _without_source_suppression(line: str) -> str:
    marker = _SOURCE_SUPPRESSION_MARKER.search(line)
    if marker is None:
        return line.strip()
    prefix = line[: marker.start()].rstrip()
    return "" if prefix.strip() in {"#", "//", "/*", "*"} else prefix.strip()


def _bounded_redacted_text(
    value: str,
    secret_values: set[str],
    limit: int = _ACTION_HISTORY_LIMIT,
) -> str:
    return redact_text(value, secret_values)[:limit]


def _contains_resolved_secret(
    value: object,
    secret_values: set[str],
) -> bool:
    if isinstance(value, str):
        return redact_text(value, secret_values) != value
    if isinstance(value, dict):
        return any(
            _contains_resolved_secret(key, secret_values)
            or _contains_resolved_secret(item, secret_values)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _contains_resolved_secret(item, secret_values)
            for item in value
        )
    return False


def _action_history_value(
    value: object,
    secret_values: set[str],
    *,
    string_limit: int = 256,
) -> object:
    if isinstance(value, str):
        return _bounded_redacted_text(
            value,
            secret_values,
            string_limit,
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        retained = [
            _action_history_value(
                item,
                secret_values,
                string_limit=min(string_limit, 128),
            )
            for item in value[:16]
        ]
        if len(value) > 16:
            retained.append(f"[{len(value) - 16} items truncated]")
        return retained
    if isinstance(value, dict):
        retained_dict: dict[str, object] = {}
        items = list(value.items())
        for key, item in items[:16]:
            retained_dict[_bounded_redacted_text(str(key), secret_values, 128)] = (
                _action_history_value(item, secret_values)
            )
        if len(items) > 16:
            retained_dict["[truncated]"] = len(items) - 16
        return retained_dict
    return _bounded_redacted_text(
        str(value),
        secret_values,
        string_limit,
    )


def _serialize_action_for_history(
    action: dict[str, object],
    secret_values: set[str],
) -> str:
    ordered: dict[str, object] = {}
    keys = [
        *(key for key in _ACTION_FIELD_ORDER if key in action),
        *(key for key in sorted(action) if key not in _ACTION_FIELD_ORDER),
    ]
    for key in keys:
        ordered[_bounded_redacted_text(key, secret_values, 128)] = (
            _action_history_value(
                action[key],
                secret_values,
                string_limit=_ACTION_STRING_LIMITS.get(key, 256),
            )
        )
    return json.dumps(
        ordered,
        ensure_ascii=False,
        separators=(",", ":"),
    )[:_ACTION_HISTORY_LIMIT]


def _has_unadvanced_coordination_note(transcript: Sequence[str]) -> bool:
    for entry in reversed(transcript):
        if entry.startswith("Action ") and "\nResult " in entry:
            serialized_action, _, serialized_result = entry.removeprefix(
                "Action "
            ).partition("\nResult ")
            try:
                action = json.loads(serialized_action)
                result = json.loads(serialized_result)
            except json.JSONDecodeError:
                action = None
                result = None
            if (
                isinstance(action, dict)
                and isinstance(result, dict)
                and "error" not in result
                and (
                    result.get("changed") is True
                    or (
                        action.get("action") == "replace"
                        and action.get("old") != action.get("new")
                        and isinstance(result.get("replacements"), int)
                        and result["replacements"] > 0
                    )
                )
            ):
                return False
        first_line = entry.partition("\n")[0]
        if first_line.startswith("Lead note:") or (
            first_line.startswith("Member ") and " note:" in first_line
        ):
            return True
    return False


class ModelRuntime(Protocol):
    def next_action(self, context: str, state_directory: Path) -> dict[str, object]: ...


class ScriptedRuntime:
    """Deterministic runtime used for contract and adapter validation."""

    def __init__(self, actions: Sequence[dict[str, object]]) -> None:
        self._actions = list(actions)
        self.contexts: list[str] = []

    def next_action(self, context: str, state_directory: Path) -> dict[str, object]:
        del state_directory
        self.contexts.append(context)
        if not self._actions:
            raise RuntimeError("scripted model runtime exhausted its actions")
        return self._actions.pop(0)


class MiniSweModelRuntime:
    """Uses mini-SWE-agent as an isolated structured inference process."""

    _SYSTEM_PROMPT = """You are the stored lead for one repository. Infer the issue from repository evidence. Return exactly one controller action. Available actions:
{"action":"list","path":"relative/path"}
{"action":"read","path":"relative/file","start":1,"end":400}
{"action":"search","path":"relative/path","pattern":"regex"}
{"action":"write","path":"relative/file","content":"complete UTF-8 content"}
{"action":"replace","path":"relative/file","old":"exact text","new":"replacement","count":1}
{"action":"run","argv":["program","arg"],"timeout":120,"dependency_services":["exact-package-host.example:443"]}
If a required package, toolchain, or browser is missing, retrieve it yourself inside the sandbox. Add only its exact public HTTP/HTTPS hosts to dependency_services for that run action, install reusable files beneath /run-data/dependency-delta, and invoke them from there in later actions. The controller grants those hosts only to that action; direct, private, wildcard, and undeclared network access remains blocked. Do not block merely because a dependency was not preinstalled.
{"action":"git_diff"}
{"action":"assign","members":["lead","member-key"],"reason":"why these stored members are needed"}
{"action":"specify","issue_version_id":"immutable issue version id","items":[{"key":"stable-key","title":"concise title","objective":"bounded behavior","acceptance_criteria":[{"key":"criterion-key","requirement":"required observable behavior","expected":"specific expected observation"}],"verification":[{"key":"verification-key","criterion_keys":["criterion-key"],"scenario":"scenario that observes the criteria"}]}],"reason":"why this specification matches the issue"} (lead only; required before assignment or source mutation)
{"action":"review_specification","specification_revision_id":"immutable revision id","verdict":"approved|rejected|blocked","summary":"concise semantic conclusion","findings":[{"key":"finding-key","category":"coverage|clarity|observability|feasibility|consistency|repository-alignment|scope","severity":"warning|error","summary":"specific semantic finding","item_keys":["specification-item-key"]}],"blocker":"required only for an irreducible blocked verdict"} (independent verifier only)
{"action":"revise","members":["member-key"],"reason":"why these assigned implementers must run again"}
{"action":"note","summary":"concise findings and exact next action"}
{"action":"finish","summary":"concise result","output":{"field":"typed node result"}} (workflow nodes must include output matching their expected schema)
{"action":"block","reason":"specific irreducible missing or contradictory prerequisite"} (lead only; non-leads finish with blocker evidence for the lead)
Read before editing. Do not reread evidence already present in action history unless its result was incomplete or the source changed. Once inspection supports a decision, persist one concise note with the findings and exact next action, then execute that action instead of continuing to inspect. After a note, the next decision must execute the stated action; the controller rejects another note until a repository write or replacement succeeds. Only the stored lead may assign or request a targeted revision. Before issue work begins, the lead must select the initial assignment. If a later issue revision, feedback item, or base conflict requires stored-team responsibilities or permissions outside the current assignment, the lead may expand it by emitting the complete strict superset of selected member keys; never remove or replace an assigned member. If later work instead requires one or more already-assigned implementers to run again, the lead may request those exact members with a revise action; never select the lead, verifier, unassigned members, or duplicate keys. A note neither finishes nor blocks the work. Keep changes strictly in issue scope. Do not create or retain plans, specification ledgers, coordination files, agent instructions, or other process artifacts in the repository; change only product source, repository-required tests, and directly required configuration. Never publish, merge, close, push, expose credentials, or invent missing external resources."""
    _RESPONSE_SCHEMA: dict[str, object] = {
        "type": "object",
        "properties": {
            "action": {
                "enum": [
                    "list",
                    "read",
                    "search",
                    "write",
                    "replace",
                    "run",
                    "git_diff",
                    "assign",
                    "specify",
                    "review_specification",
                    "revise",
                    "note",
                    "finish",
                    "block",
                ]
            },
            "path": {"type": "string"},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
            "pattern": {"type": "string"},
            "content": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
            "count": {"type": "integer"},
            "argv": {
                "type": "array",
                "items": {"type": "string"},
            },
            "timeout": {"type": "number"},
            "dependency_services": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 16,
                "uniqueItems": True,
            },
            "members": {
                "type": "array",
                "items": {"type": "string"},
            },
            "reason": {"type": "string"},
            "issue_version_id": {"type": "string"},
            "specification_revision_id": {"type": "string"},
            "items": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "verdict": {
                "enum": ["approved", "rejected", "blocked"],
            },
            "findings": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "blocker": {"type": "string"},
            "summary": {"type": "string"},
            "output": {
                "type": "object",
                "additionalProperties": True,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 600,
        supervisor: RunProcessSupervisor | None = None,
        run_id: str | None = None,
        system_prompt: str | None = None,
        response_schema: dict[str, object] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("stored-agent model timeout must be positive")
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.supervisor = supervisor
        self.run_id = run_id
        self.system_prompt = system_prompt or self._SYSTEM_PROMPT
        self.response_schema = response_schema or self._RESPONSE_SCHEMA

    def next_action(
        self,
        context: str,
        state_directory: Path,
    ) -> dict[str, object]:
        inference = MiniSweInference(
            self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            supervisor=self.supervisor,
            run_id=self.run_id,
        )
        return inference.infer(
            system_prompt=self.system_prompt,
            prompt=context,
            response_schema=self.response_schema,
            state_directory=state_directory,
        )

    def inspect_image(
        self,
        *,
        system_prompt: str,
        prompt: str,
        response_schema: dict[str, object],
        image_path: Path,
        state_directory: Path,
    ) -> dict[str, object]:
        inference = MiniSweInference(
            self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            supervisor=self.supervisor,
            run_id=self.run_id,
        )
        return inference.infer(
            system_prompt=system_prompt,
            prompt=prompt,
            response_schema=response_schema,
            state_directory=state_directory,
            image_paths=(image_path,),
        )


RuntimeFactory = Callable[[str, str, float], ModelRuntime]
SecretResolver = Callable[[str], str]
SecretBinding = tuple[str, str, tuple[tuple[str, ...], ...]]


def _default_runtime_factory(
    runtime: str,
    model: str,
    action_timeout_seconds: float,
) -> ModelRuntime:
    if runtime != MINI_SWE_RUNTIME:
        raise ValueError(f"unsupported stored model runtime: {runtime}")
    return MiniSweModelRuntime(
        model=model,
        timeout=action_timeout_seconds,
    )


def _probe_runtime_environment(
    action: dict[str, object],
) -> dict[str, str] | None:
    remediation = action.get("remediation")
    if remediation is None:
        return None
    if not isinstance(remediation, dict):
        raise ValueError("run action remediation must be an object")
    environment = remediation.get("environment")
    corrected_target = remediation.get("corrected_target")
    environment_name = (
        environment.get("name") if isinstance(environment, dict) else None
    )
    environment_value = (
        environment.get("value") if isinstance(environment, dict) else None
    )
    if (
        remediation.get("kind") != "probe_configuration"
        or not isinstance(environment_name, str)
        or not environment_name
        or len(environment_name) > 128
        or not _PROBE_ENVIRONMENT_NAME.fullmatch(environment_name)
        or environment_name in _RESERVED_PROBE_ENVIRONMENT
        or environment_name.startswith(("LD_", "DYLD_", "GIT_", "SSH_"))
        or _SENSITIVE_PROBE_ENVIRONMENT.search(environment_name)
        or not isinstance(environment_value, str)
        or not environment_value
        or len(environment_value) > 2_048
        or "\x00" in environment_value
        or environment_value != corrected_target
    ):
        raise ValueError(
            "probe remediation requires a safe corrected environment binding"
        )
    return {environment_name: environment_value}


_MAX_ACTION_DEPENDENCY_SERVICES = 16
_DEPENDENCY_SERVICE_PORTS = frozenset((80, 443))


def _action_dependency_services(
    action: Mapping[str, object],
) -> tuple[str, ...]:
    value = action.get("dependency_services")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("run action dependency_services must be a list")
    if len(value) > _MAX_ACTION_DEPENDENCY_SERVICES:
        raise ValueError(
            "run action dependency_services exceeds the 16-service limit"
        )
    services: list[str] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 253
        ):
            raise ValueError(
                f"dependency_services[{index}] must be a bounded host:port"
            )
        try:
            host, port = RestrictedNetworkPolicy._parse_rule(item)
        except (UnicodeError, ValueError) as error:
            raise ValueError(
                f"dependency_services[{index}] must be an exact public "
                "HTTP or HTTPS host:port"
            ) from error
        if (
            host.startswith("*.")
            or port not in _DEPENDENCY_SERVICE_PORTS
            or any(character in host for character in "/@?#[]:")
        ):
            raise ValueError(
                f"dependency_services[{index}] must be an exact public "
                "HTTP or HTTPS host:port"
            )
        service = f"{host}:{port}"
        if service not in services:
            services.append(service)
    return tuple(services)


class AgentToolExecutor:
    _TOOL_PERMISSION = {
        "list": "read",
        "read": "read",
        "search": "read",
        "write": "write",
        "replace": "write",
        "run": "run",
        "git_diff": "git_diff",
    }

    def __init__(self, sandbox: SandboxManager) -> None:
        self.sandbox = sandbox

    def execute(
        self,
        member: TeamMember,
        policy: SandboxPolicy,
        layout: RunLayout,
        action: dict[str, object],
        secrets: dict[str, str] | None = None,
        checkout_writable: bool = True,
        workflow_resources: tuple[str, ...] | None = None,
    ) -> str:
        name = action.get("action")
        if not isinstance(name, str) or name not in self._TOOL_PERMISSION:
            raise ValueError(f"unsupported agent action: {name}")
        permission = self._TOOL_PERMISSION[name]
        if permission not in member.permitted_tools:
            raise PermissionError(
                f"stored team member {member.stable_key} is not "
                f"permitted to use {permission}"
            )
        if workflow_resources is not None:
            claims = set(workflow_resources)
            readable = bool(
                claims
                & {
                    "checkout:read",
                    "checkout:write",
                    "workspace:read",
                    "workspace:write",
                }
            )
            writable = bool(
                claims & {"checkout:write", "workspace:write"}
            )
            runnable = writable or "validation:read" in claims
            diffable = "diff:read" in claims
            authorized = (
                readable
                if name in {"list", "read", "search"}
                else writable
                if name in {"write", "replace"}
                else diffable
                if name == "git_diff"
                else runnable
            )
            if not authorized:
                raise PermissionError(
                    "workflow node resources do not permit agent action "
                    f"{name}"
                )
            checkout_writable = checkout_writable and writable
        if name == "run":
            argv = action.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(
                    isinstance(argument, str) and argument
                    for argument in argv
                )
            ):
                raise ValueError(
                    "run action argv must be a nonempty string array"
                )
            timeout_value = action.get("timeout", 120)
            if isinstance(timeout_value, bool) or not isinstance(
                timeout_value, (int, float)
            ):
                raise ValueError("run action timeout must be a number")
            timeout = min(max(float(timeout_value), 1), 300)
            dependency_services = _action_dependency_services(action)
            runtime_environment = _probe_runtime_environment(action)
            command = tuple(argv)
            if runtime_environment is not None:
                environment_name, environment_value = next(
                    iter(runtime_environment.items())
                )
                command = (
                    "/usr/bin/env",
                    "--",
                    f"{environment_name}={environment_value}",
                    *command,
                )
            effective_policy = policy
            if dependency_services:
                effective_policy = replace(
                    policy,
                    allowed_services=tuple(
                        dict.fromkeys(
                            (*policy.allowed_services, *dependency_services)
                        )
                    ),
                )
            result = self.sandbox.run(
                effective_policy,
                layout,
                command,
                timeout=timeout,
                secrets=secrets,
                checkout_writable=checkout_writable,
            )
            if result.canceled:
                raise _RunCanceled(layout.run_id)
            payload: dict[str, object] = {
                "returncode": result.returncode,
                "stdout": result.stdout[-32_000:],
                "stderr": result.stderr[-32_000:],
                "timed_out": result.timed_out,
                "log_path": str(result.log_path),
            }
            if runtime_environment is not None:
                payload["configured_environment"] = runtime_environment
            if dependency_services:
                payload["dependency_services"] = list(dependency_services)
            return json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
        if name == "git_diff":
            result = self.sandbox.run(
                policy,
                layout,
                (
                    "git",
                    "--no-pager",
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                ),
                timeout=60,
                secrets=secrets,
                checkout_writable=False,
            )
            if result.canceled:
                raise _RunCanceled(layout.run_id)
            return json.dumps(
                {
                    "returncode": result.returncode,
                    "stdout": result.stdout[-32_000:],
                    "stderr": result.stderr[-32_000:],
                    "timed_out": result.timed_out,
                    "log_path": str(result.log_path),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        encoded = base64.urlsafe_b64encode(
            json.dumps(
                action, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).rstrip(b"=")
        result = self.sandbox.run(
            policy,
            layout,
            (
                "python3",
                "/opt/repogents/repository_tools.py",
                encoded.decode("ascii"),
            ),
            timeout=60,
            checkout_writable=checkout_writable,
        )
        if result.canceled:
            raise _RunCanceled(layout.run_id)
        if result.returncode != 0:
            detail = result.stderr.strip() or "sandboxed repository tool failed"
            if (
                "path escapes the isolated checkout" in detail
                or "path must be relative to the isolated checkout" in detail
            ):
                raise PermissionError(detail)
            return json.dumps(
                {
                    "error": detail,
                    "returncode": result.returncode,
                    "log_path": str(result.log_path),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        return result.stdout.strip()


class MissingValidationCommands(RuntimeError):
    pass


class BaselineUnavailable(RuntimeError):
    pass


class RevisionRequired(RuntimeError):
    """A safe, source-fixable revision that must return to the stored agent."""

    pass


class _RunCanceled(RuntimeError):
    pass


class ExecutionService:
    def __init__(
        self,
        *,
        database: Database,
        lifecycle: RunLifecycle,
        teams: TeamService,
        sandbox: SandboxManager,
        runtime_factory: RuntimeFactory | None = None,
        max_actions: int = 80,
        process_supervisor: RunProcessSupervisor | None = None,
        max_revision_cycles: int = 4,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle
        self.teams = teams
        self.sandbox = sandbox
        self.runtime_factory = runtime_factory or _default_runtime_factory
        self.max_actions = max_actions
        self.max_revision_cycles = max_revision_cycles
        self.secret_resolver = secret_resolver
        self.process_supervisor = process_supervisor
        self.tools = AgentToolExecutor(sandbox)
        self.workflow_operations = (
            DeterministicOperationRegistry.with_defaults()
        )
        self.workflows = WorkflowService(
            database,
            registry=self.workflow_operations,
        )
        self.scanner = SecretScanner()
        self.specifications = SpecificationService(database)

    def execute(
        self,
        run_id: str,
        *,
        additional_context: str | None = None,
        comparison_base_sha: str | None = None,
    ) -> str | None:
        if self._is_canceled(run_id):
            return None
        issue_version_id = self.lifecycle.current_issue_version(run_id)
        run, issue, sandbox_row = self._load_context(run_id, issue_version_id)
        current = RunState(str(run["state"]))
        resume_validation = current == RunState.VALIDATING
        if current == RunState.QUEUED:
            self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        elif current not in {
            RunState.IMPLEMENTING,
            RunState.RESOLVING_FEEDBACK,
            RunState.VALIDATING,
        }:
            raise ValueError(f"run cannot execute from state {current.value}")
        team = self.teams.load(str(run["team_version_id"]))
        lead = next(member for member in team.members if member.coordinates)
        layout = RunLayout.create(
            Path(str(run["run_path"])).parents[3],
            str(run["repository_id"]),
            run_id,
        )
        policy = _sandbox_policy(sandbox_row)
        source_base_sha = comparison_base_sha or str(run["base_sha"])
        if not re.fullmatch(r"[0-9a-f]{40}", source_base_sha):
            raise ValueError("source comparison base SHA is invalid")
        secret_bindings = _secret_bindings(sandbox_row)
        resolved_secret_values: set[str] = set()
        try:
            self._ensure_validation_baselines(
                run,
                str(run["sandbox_version_id"]),
                policy,
                layout,
                secret_bindings,
                resolved_secret_values,
            )
        except _RunCanceled:
            return None
        except (MissingValidationCommands, BaselineUnavailable) as error:
            if not self._is_canceled(run_id):
                self.lifecycle.transition(
                    run_id,
                    RunState.BLOCKED,
                    reason=str(error),
                )
            return None
        transcript = self._load_transcript(layout)
        assignments = self.teams.assignments_for_run(run_id)
        try:
            workflow_template = self.workflows.load_template(
                str(run["team_version_id"])
            )
        except KeyError:
            workflow_template = None
        workflow_assignment_contract = (
            self._workflow_assignment_contract(workflow_template)
            if workflow_template is not None
            else None
        )
        specification = self.specifications.current(run_id, issue_version_id)
        specification_context_sha256 = (
            hashlib.sha256(additional_context.encode("utf-8")).hexdigest()
            if additional_context
            else None
        )
        base_context = self._base_prompt(
            run,
            issue,
            sandbox_row,
            team,
            assignments,
            additional_context,
            workflow_assignment_contract,
            specification,
        )
        context_binding = (
            self.specifications.context_binding(
                run_id,
                issue_version_id,
                specification_context_sha256,
            )
            if specification_context_sha256 is not None
            else None
        )
        context_requires_reconciliation = (
            specification_context_sha256 is not None
            and (
                specification is None
                or context_binding is None
                or context_binding["specification_revision_id"]
                != specification["id"]
            )
        )
        if specification is None or context_requires_reconciliation:
            specification_prompt = base_context
            if specification_context_sha256 is not None:
                specification_prompt = self._specification_reconciliation_prompt(
                    base_context,
                    specification,
                )
            self._agent_cycle(
                self._runtime(lead, run_id),
                lead,
                policy,
                layout,
                specification_prompt,
                transcript,
                secret_bindings,
                resolved_secret_values,
                require_specification=True,
                specification_context_sha256=specification_context_sha256,
                checkout_writable=False,
            )
            return None
        review = specification.get("review")
        if not isinstance(review, dict):
            verifier = next(
                member for member in team.members if member.independent_verifier
            )
            self._agent_cycle(
                self._runtime(verifier, run_id),
                verifier,
                policy,
                layout,
                self._specification_review_prompt(base_context, specification),
                transcript,
                secret_bindings,
                resolved_secret_values,
                require_specification_review=True,
                checkout_writable=False,
            )
            return None
        review_verdict = str(review.get("verdict") or "")
        if review_verdict == "rejected":
            self._agent_cycle(
                self._runtime(lead, run_id),
                lead,
                policy,
                layout,
                base_context,
                transcript,
                secret_bindings,
                resolved_secret_values,
                require_specification=True,
                supersede_specification_id=str(specification["id"]),
                specification_context_sha256=specification_context_sha256,
                checkout_writable=False,
            )
            return None
        if review_verdict == "blocked":
            blocker = str(review.get("blocker") or "specification review blocked")
            self.lifecycle.transition(
                run_id,
                RunState.BLOCKED,
                reason=blocker,
            )
            return None
        if review_verdict != "approved":
            raise RuntimeError("stored specification review verdict is invalid")
        if assignments and not any(
            assignment.member.independent_verifier for assignment in assignments
        ):
            verifier = next(
                member for member in team.members if member.independent_verifier
            )
            self.teams.assign(
                run_id,
                tuple(
                    assignment.member.stable_key for assignment in assignments
                )
                + (verifier.stable_key,),
                "Complete the existing assignment with mandatory independent review.",
            )
            assignments = self.teams.assignments_for_run(run_id)
            base_context = self._base_prompt(
                run,
                issue,
                sandbox_row,
                team,
                assignments,
                additional_context,
                workflow_assignment_contract,
                specification,
            )
        if not assignments:
            self._agent_cycle(
                self._runtime(lead, run_id),
                lead,
                policy,
                layout,
                base_context,
                transcript,
                secret_bindings,
                resolved_secret_values,
                allow_assignment=True,
                require_assignment=True,
            )
            return None
        if workflow_template is not None:
            return self._execute_workflow(
                run=run,
                issue=issue,
                sandbox_row=sandbox_row,
                team=team,
                lead=lead,
                policy=policy,
                layout=layout,
                base_context=base_context,
                secret_bindings=secret_bindings,
                resolved_secret_values=resolved_secret_values,
                revision_context=additional_context,
                source_base_sha=source_base_sha,
                issue_version_id=issue_version_id,
                resume_validation=resume_validation,
            )
        runtime = self._runtime(lead, run_id)
        for cycle in range(self.max_revision_cycles):
            if not (resume_validation and cycle == 0):
                for assignment in assignments:
                    member = assignment.member
                    if (
                        member.coordinates
                        or member.independent_verifier
                        or self._member_finished(transcript, member)
                    ):
                        continue
                    member_outcome, yielded = self._agent_cycle(
                        self._runtime(member, run_id),
                        member,
                        policy,
                        layout,
                        self._member_prompt(base_context, assignment),
                        transcript,
                        secret_bindings,
                        resolved_secret_values,
                    )
                    if yielded or member_outcome is None:
                        return None
                if not self._member_finished(transcript, lead):
                    outcome, yielded = self._agent_cycle(
                        runtime,
                        lead,
                        policy,
                        layout,
                        base_context,
                        transcript,
                        secret_bindings,
                        resolved_secret_values,
                        allow_assignment=True,
                    )
                    if yielded or outcome is None:
                        return None
                verifier_assignment = next(
                    assignment
                    for assignment in assignments
                    if assignment.member.independent_verifier
                )
                try:
                    verifier_outcome, yielded = self._agent_cycle(
                        self._runtime(verifier_assignment.member, run_id),
                        verifier_assignment.member,
                        policy,
                        layout,
                        self._member_prompt(base_context, verifier_assignment),
                        transcript,
                        secret_bindings,
                        resolved_secret_values,
                    )
                except RevisionRequired as error:
                    transcript.append(
                        "Revision requested for assigned members: independent "
                        "review rejected the candidate:\n"
                        + redact_text(str(error), resolved_secret_values)
                    )
                    self._store_transcript(layout, transcript)
                    if cycle + 1 >= self.max_revision_cycles:
                        return None
                    continue
                if yielded or verifier_outcome is None:
                    return None
            try:
                self._ensure_not_canceled(run_id)
                state = RunState(str(self.lifecycle.get_run(run_id)["state"]))
                if state in {RunState.IMPLEMENTING, RunState.RESOLVING_FEEDBACK}:
                    self.lifecycle.transition(run_id, RunState.VALIDATING)
                self._ensure_not_canceled(run_id)
                commit_sha = self._commit(
                    run,
                    issue,
                    policy,
                    layout,
                    resolved_secret_values,
                    source_base_sha,
                )
            except _RunCanceled:
                return None
            except RevisionRequired as error:
                if self._is_canceled(run_id):
                    return None
                transcript.append(
                    "Revision requested for assigned members: commit preparation "
                    "found a source-fixable problem:\n"
                    + redact_text(str(error), resolved_secret_values)
                )
                self._store_transcript(layout, transcript)
                self.lifecycle.transition(
                    run_id,
                    RunState.IMPLEMENTING,
                    reason="continuing automatically after commit preparation feedback",
                )
                if cycle + 1 >= self.max_revision_cycles:
                    return None
                continue
            try:
                passed, validation_feedback = self._validate(
                    run_id,
                    commit_sha,
                    str(run["sandbox_version_id"]),
                    policy,
                    layout,
                    secret_bindings,
                    resolved_secret_values,
                    source_base_sha,
                )
            except _RunCanceled:
                return None
            except (MissingValidationCommands, BaselineUnavailable) as error:
                if self._is_canceled(run_id):
                    return None
                self.lifecycle.transition(run_id, RunState.BLOCKED, reason=str(error))
                return None
            except Exception:
                if self._is_canceled(run_id):
                    return None
                raise
            if self._is_canceled(run_id):
                return None
            if passed:
                self._ensure_not_canceled(run_id)
                if not self.lifecycle.record_validated_revision(
                    run_id,
                    commit_sha,
                    issue_version_id,
                ):
                    return None
                self._clear_transcript(layout)
                return commit_sha
            if self._is_canceled(run_id):
                return None
            transcript.append(
                "Revision requested for assigned members: validation for commit "
                + commit_sha
                + " failed:\n"
                + validation_feedback
            )
            self._store_transcript(layout, transcript)
            if cycle + 1 >= self.max_revision_cycles:
                if self._is_canceled(run_id):
                    return None
                self.lifecycle.transition(
                    run_id,
                    RunState.IMPLEMENTING,
                    reason="continuing automatically after repository validation feedback",
                )
                return None
            if self._is_canceled(run_id):
                return None
            self.lifecycle.transition(run_id, RunState.IMPLEMENTING)
        raise AssertionError("unreachable revision loop")

    def _execute_workflow(
        self,
        *,
        run: dict[str, object],
        issue: dict[str, object],
        sandbox_row: dict[str, object],
        team: StoredTeam,
        lead: TeamMember,
        policy: SandboxPolicy,
        layout: RunLayout,
        base_context: str,
        secret_bindings: tuple[SecretBinding, ...],
        resolved_secret_values: set[str],
        revision_context: str | None,
        source_base_sha: str,
        issue_version_id: str,
        resume_validation: bool,
    ) -> str | None:
        del sandbox_row
        run_id = str(run["id"])
        members = {member.stable_key: member for member in team.members}
        self.workflows.compile_run(run_id, issue_version_id)
        if revision_context:
            self._revise_workflow_from_feedback(
                run_id=run_id,
                lead=lead,
                policy=policy,
                layout=layout,
                base_context=base_context,
                feedback=revision_context,
                secret_bindings=secret_bindings,
                resolved_secret_values=resolved_secret_values,
            )

        def agent_runner(context: WorkflowNodeContext) -> Mapping[str, object]:
            if context.member_key is None or context.member_key not in members:
                raise WorkflowExecutionError(
                    f"workflow node {context.stable_key} has no stored member"
                )
            member = members[context.member_key]
            node_layout = self._workflow_layout(
                layout,
                f"node-{context.node_id}",
            )
            transcript = self._load_transcript(node_layout)
            outcome, yielded = self._agent_cycle(
                self._runtime(member, run_id),
                member,
                policy,
                node_layout,
                self._workflow_node_prompt(base_context, member, context),
                transcript,
                secret_bindings,
                resolved_secret_values,
                workflow_resources=context.resources,
                workflow_expected_output=context.expected_output,
            )
            if yielded:
                raise WorkflowExecutionError(
                    f"workflow node {context.stable_key} exhausted its "
                    "action quantum"
                )
            if outcome is None:
                raise WorkflowExecutionError(
                    f"workflow node {context.stable_key} did not produce "
                    "an output"
                )
            if isinstance(outcome, Mapping):
                return dict(outcome)
            return {"summary": outcome}

        def assessment_runner(
            payload: dict[str, object],
        ) -> Mapping[str, object]:
            return self._run_workflow_assessment(
                run_id=run_id,
                lead=lead,
                policy=policy,
                layout=layout,
                base_context=base_context,
                payload=payload,
                secret_bindings=secret_bindings,
                resolved_secret_values=resolved_secret_values,
            )

        engine = WorkflowExecutionEngine(
            database=self.database,
            workflow=self.workflows,
            agent_runner=agent_runner,
            operations=self.workflow_operations,
            assessment_runner=assessment_runner,
            cancellation_check=self._is_canceled,
            known_secret_values=lambda _run_id: tuple(resolved_secret_values),
            max_workers=4,
            max_generations=max(2, self.max_revision_cycles + 1),
        )
        for cycle in range(self.max_revision_cycles):
            if not (resume_validation and cycle == 0):
                graph = self.workflows.active_run_graph(run_id)
                accepted = (
                    graph.state == "succeeded"
                    and isinstance(graph.assessment, Mapping)
                    and graph.assessment.get("outcome") == "accept"
                )
                if not accepted:
                    try:
                        engine.execute(run_id)
                    except WorkflowCanceled:
                        return None
            try:
                self._ensure_not_canceled(run_id)
                state = RunState(str(self.lifecycle.get_run(run_id)["state"]))
                if state in {
                    RunState.IMPLEMENTING,
                    RunState.RESOLVING_FEEDBACK,
                }:
                    self.lifecycle.transition(
                        run_id,
                        RunState.VALIDATING,
                    )
                self._ensure_not_canceled(run_id)
                commit_sha = self._commit(
                    run,
                    issue,
                    policy,
                    layout,
                    resolved_secret_values,
                    source_base_sha,
                )
            except _RunCanceled:
                return None
            except RevisionRequired as error:
                if self._is_canceled(run_id):
                    return None
                feedback = redact_text(str(error), resolved_secret_values)
                self.lifecycle.transition(
                    run_id,
                    RunState.IMPLEMENTING,
                    reason=(
                        "continuing automatically after workflow commit "
                        "preparation feedback"
                    ),
                )
                self._revise_workflow_from_feedback(
                    run_id=run_id,
                    lead=lead,
                    policy=policy,
                    layout=layout,
                    base_context=base_context,
                    feedback=(
                        "Commit preparation rejected the candidate:\n"
                        + feedback
                    ),
                    secret_bindings=secret_bindings,
                    resolved_secret_values=resolved_secret_values,
                )
                if cycle + 1 >= self.max_revision_cycles:
                    return None
                continue
            try:
                passed, validation_feedback = self._validate(
                    run_id,
                    commit_sha,
                    str(run["sandbox_version_id"]),
                    policy,
                    layout,
                    secret_bindings,
                    resolved_secret_values,
                    source_base_sha,
                )
            except _RunCanceled:
                return None
            except (MissingValidationCommands, BaselineUnavailable) as error:
                if self._is_canceled(run_id):
                    return None
                self.lifecycle.transition(
                    run_id,
                    RunState.BLOCKED,
                    reason=str(error),
                )
                return None
            except Exception:
                if self._is_canceled(run_id):
                    return None
                raise
            if self._is_canceled(run_id):
                return None
            if passed:
                self._ensure_not_canceled(run_id)
                if not self.lifecycle.record_validated_revision(
                    run_id,
                    commit_sha,
                    issue_version_id,
                ):
                    return None
                self._clear_transcript(layout)
                return commit_sha
            self.lifecycle.transition(
                run_id,
                RunState.IMPLEMENTING,
                reason=(
                    "continuing automatically after workflow validation "
                    "feedback"
                ),
            )
            self._revise_workflow_from_feedback(
                run_id=run_id,
                lead=lead,
                policy=policy,
                layout=layout,
                base_context=base_context,
                feedback=(
                    f"Repository validation rejected commit {commit_sha}:\n"
                    + validation_feedback
                ),
                secret_bindings=secret_bindings,
                resolved_secret_values=resolved_secret_values,
            )
            if cycle + 1 >= self.max_revision_cycles:
                return None
        raise AssertionError("unreachable workflow revision loop")

    @staticmethod
    def _workflow_layout(layout: RunLayout, key: str) -> RunLayout:
        agent_state = layout.agent_state / "workflow" / key
        agent_state.mkdir(parents=True, exist_ok=True)
        return RunLayout(
            repository_id=layout.repository_id,
            run_id=layout.run_id,
            root=layout.root,
            checkout=layout.checkout,
            agent_state=agent_state,
            logs=layout.logs,
            temp=layout.temp,
            validation=layout.validation,
            dependency_delta=layout.dependency_delta,
            build=layout.build,
        )

    @staticmethod
    def _workflow_node_prompt(
        base_context: str,
        member: TeamMember,
        context: WorkflowNodeContext,
    ) -> str:
        return (
            base_context
            + "\n\nCurrent model-designed workflow node:\n"
            + json.dumps(
                {
                    "generation": context.generation,
                    "stable_key": context.stable_key,
                    "member": {
                        "stable_key": member.stable_key,
                        "role": member.role,
                        "responsibilities": member.responsibilities,
                        "permitted_tools": member.permitted_tools,
                        "instructions": member.instructions,
                    },
                    "objective": context.prompt,
                    "inputs": context.inputs,
                    "dependency_outputs": context.dependency_outputs,
                    "expected_output": context.expected_output,
                    "resources": list(context.resources),
                    "completion_contract": (
                        "Complete only this node objective. Finish with a "
                        "concise summary and an output object matching "
                        "expected_output. Output keys cannot be argv, code, "
                        "command, credential, executable, password, program, "
                        "script, secret, shell, or token; record passive "
                        "verification with scenario and result fields. Do not "
                        "assign members or alter the outer graph. Report "
                        "recoverable concerns in the typed output rather than "
                        "blocking the repository."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )

    def _run_workflow_assessment(
        self,
        *,
        run_id: str,
        lead: TeamMember,
        policy: SandboxPolicy,
        layout: RunLayout,
        base_context: str,
        payload: Mapping[str, object],
        secret_bindings: tuple[SecretBinding, ...],
        resolved_secret_values: set[str],
    ) -> Mapping[str, object]:
        generation = payload.get("generation", "unknown")
        assessment_layout = self._workflow_layout(
            layout,
            f"assessment-{generation}",
        )
        transcript = self._load_transcript(assessment_layout)
        prompt = (
            base_context
            + "\n\nWorkflow coordination assessment:\n"
            + json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + (
                "\n\nAssess actual node outputs and graph performance. "
                "Finish with an output object. To retain the graph, return "
                '{"outcome":"accept","evidence":"specific evidence"}. '
                "To adapt it, return outcome=revise, evidence, reason, and "
                "complete nodes and edges arrays. A revision must change "
                "at least one relevant prompt, parameter, dependency, "
                "join, resource, or selected specialist. Never return "
                "executable source, shell expressions, environment "
                "values, or secrets."
            )
        )
        outcome, yielded = self._agent_cycle(
            self._runtime(lead, run_id),
            lead,
            policy,
            assessment_layout,
            prompt,
            transcript,
            secret_bindings,
            resolved_secret_values,
            allow_assignment=True,
            continue_after_assignment=True,
        )
        if yielded:
            raise WorkflowExecutionError(
                "workflow assessment exhausted its action quantum"
            )
        if not isinstance(outcome, Mapping):
            raise WorkflowExecutionError(
                "workflow assessment must finish with a structured output"
            )
        return dict(outcome)

    def _revise_workflow_from_feedback(
        self,
        *,
        run_id: str,
        lead: TeamMember,
        policy: SandboxPolicy,
        layout: RunLayout,
        base_context: str,
        feedback: str,
        secret_bindings: tuple[SecretBinding, ...],
        resolved_secret_values: set[str],
    ) -> None:
        revision_marker = (
            "controller-revision:"
            + hashlib.sha256(feedback.encode("utf-8")).hexdigest()[:16]
        )
        graph = self.workflows.active_run_graph(run_id)
        if revision_marker in graph.reason:
            return
        payload = {
            "run_id": run_id,
            "generation": graph.generation,
            "assessment_prompt": graph.assessment_prompt,
            "rationale": graph.rationale,
            "required_revision_feedback": feedback,
            "nodes": [
                {
                    "stable_key": node.stable_key,
                    "kind": node.kind,
                    "member_key": node.member_key or "",
                    "operation": node.operation or "",
                    "prompt": node.prompt,
                    "parameters": node.parameters,
                    "bindings": node.bindings,
                    "expected_output": node.expected_output,
                    "resources": list(node.resources),
                    "state": node.state,
                    "output": node.output,
                    "reused": node.reused_from_node_id is not None,
                }
                for node in graph.nodes
            ],
            "edges": [
                {"source": edge.source, "target": edge.target}
                for edge in graph.edges
            ],
        }
        assessment = dict(
            self._run_workflow_assessment(
                run_id=run_id,
                lead=lead,
                policy=policy,
                layout=layout,
                base_context=base_context,
                payload=payload,
                secret_bindings=secret_bindings,
                resolved_secret_values=resolved_secret_values,
            )
        )
        if assessment.get("outcome") != "revise":
            raise WorkflowExecutionError(
                "controller feedback requires a workflow revision"
            )
        nodes = assessment.get("nodes")
        edges = assessment.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise WorkflowExecutionError(
                "workflow revision must include complete nodes and edges"
            )
        reason = assessment.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise WorkflowExecutionError(
                "workflow revision must include a specific reason"
            )
        self.workflows.revise(
            run_id,
            reason=f"{reason} [{revision_marker}]",
            assessment=assessment,
            design={
                "rationale": graph.rationale,
                "assessment_prompt": graph.assessment_prompt,
                "nodes": nodes,
                "edges": edges,
            },
        )

    def _handle_specification_action(
        self,
        action: dict[str, object],
        member: TeamMember,
        layout: RunLayout,
        transcript: list[str],
        resolved_secret_values: set[str],
        *,
        require_specification: bool,
        supersede_specification_id: str | None,
        specification_context_sha256: str | None,
    ) -> None:
        if not require_specification or not member.coordinates:
            raise ValueError(
                "only the stored lead may specify the issue contract "
                "at the specification boundary"
            )
        specified_issue_version_id = action.get("issue_version_id")
        items = action.get("items")
        reason = action.get("reason")
        if (
            not isinstance(specified_issue_version_id, str)
            or not isinstance(items, list)
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValueError(
                "specify action requires an issue version, atomic "
                "items, and specific reasoning"
            )
        revision = self.specifications.submit(
            run_id=layout.run_id,
            author_member_id=member.id,
            issue_version_id=specified_issue_version_id,
            items=items,
            reason=reason,
        )
        if specification_context_sha256 is not None:
            self.specifications.bind_context(
                run_id=layout.run_id,
                issue_version_id=specified_issue_version_id,
                context_sha256=specification_context_sha256,
                specification_revision_id=str(revision["id"]),
            )
        if (
            supersede_specification_id is not None
            and revision["id"] == supersede_specification_id
        ):
            raise ValueError(
                "a rejected specification must be materially revised"
            )
        safe_reason = _bounded_redacted_text(
            reason,
            resolved_secret_values,
        )
        transcript.append(
            "Lead specified revision "
            + str(revision["revision"])
            + ": "
            + safe_reason
        )
        self._store_transcript(layout, transcript)

    def _handle_specification_review_action(
        self,
        action: dict[str, object],
        member: TeamMember,
        layout: RunLayout,
        transcript: list[str],
        *,
        require_specification_review: bool,
    ) -> None:
        if (
            not require_specification_review
            or not member.independent_verifier
        ):
            raise ValueError(
                "only the stored independent verifier may review "
                "the active issue specification"
            )
        specification_revision_id = action.get(
            "specification_revision_id"
        )
        verdict = action.get("verdict")
        findings = action.get("findings")
        blocker = action.get("blocker")
        summary = action.get("summary")
        if (
            not isinstance(specification_revision_id, str)
            or not isinstance(verdict, str)
            or not isinstance(findings, list)
            or not isinstance(summary, str)
            or not summary.strip()
            or (blocker is not None and not isinstance(blocker, str))
        ):
            raise ValueError(
                "review_specification requires a revision, verdict, "
                "summary, and structured findings"
            )
        stored_review = self.specifications.record_review(
            run_id=layout.run_id,
            specification_revision_id=specification_revision_id,
            reviewer_member_id=member.id,
            reviewer_model=member.model,
            rubric_version=1,
            verdict=verdict,
            summary=summary,
            findings=findings,
            blocker=blocker,
        )
        transcript.append(
            "Independent verifier reviewed specification revision "
            + str(stored_review["specification_revision_id"])
            + ": "
            + str(stored_review["verdict"])
        )
        self._store_transcript(layout, transcript)
        if stored_review["verdict"] == "blocked":
            self.lifecycle.transition(
                layout.run_id,
                RunState.BLOCKED,
                reason=str(stored_review["blocker"]),
            )

    def _agent_cycle(
        self,
        runtime: ModelRuntime,
        member: TeamMember,
        policy: SandboxPolicy,
        layout: RunLayout,
        base_context: str,
        transcript: list[str],
        secret_bindings: tuple[SecretBinding, ...],
        resolved_secret_values: set[str],
        *,
        allow_assignment: bool = False,
        require_assignment: bool = False,
        continue_after_assignment: bool = False,
        workflow_resources: tuple[str, ...] | None = None,
        workflow_expected_output: Mapping[str, object] | None = None,
        require_specification: bool = False,
        supersede_specification_id: str | None = None,
        specification_context_sha256: str | None = None,
        require_specification_review: bool = False,
        checkout_writable: bool = True,
    ) -> tuple[str | dict[str, object] | None, bool]:
        for _ in range(self.max_actions):
            context = base_context
            model_history = self._bounded_transcript(transcript)
            if model_history:
                context += "\n\nAction history:\n" + "\n".join(model_history)
            try:
                action = runtime.next_action(context, layout.agent_state)
            except _RunCanceled:
                return None, False
            except Exception:
                if self._is_canceled(layout.run_id):
                    return None, False
                raise
            try:
                self._ensure_not_canceled(layout.run_id)
                name = action.get("action")
                if require_specification and name not in {
                    "list",
                    "read",
                    "search",
                    "git_diff",
                    "note",
                    "block",
                    "specify",
                }:
                    raise ValueError(
                        "the coordinator must persist the issue specification "
                        "before assignment or source mutation"
                    )
                if require_specification_review and name not in {
                    "list",
                    "read",
                    "search",
                    "git_diff",
                    "note",
                    "review_specification",
                }:
                    raise ValueError(
                        "the independent verifier must review the specification "
                        "before assignment or source mutation"
                    )
                if name == "specify":
                    self._handle_specification_action(
                        action,
                        member,
                        layout,
                        transcript,
                        resolved_secret_values,
                        require_specification=require_specification,
                        supersede_specification_id=supersede_specification_id,
                        specification_context_sha256=(
                            specification_context_sha256
                        ),
                    )
                    return None, False
                if name == "review_specification":
                    self._handle_specification_review_action(
                        action,
                        member,
                        layout,
                        transcript,
                        require_specification_review=(
                            require_specification_review
                        ),
                    )
                    return None, False
                if name == "assign":
                    if not allow_assignment:
                        raise ValueError("only the stored lead may assign issue members")
                    members = action.get("members")
                    reason = action.get("reason")
                    if (
                        not isinstance(members, list)
                        or not all(isinstance(value, str) for value in members)
                        or not isinstance(reason, str)
                    ):
                        raise ValueError(
                            "assign action requires stored member keys and reasoning"
                        )
                    safe_reason = _bounded_redacted_text(
                        reason,
                        resolved_secret_values,
                    )
                    if require_assignment:
                        self.teams.assign(
                            layout.run_id,
                            tuple(members),
                            safe_reason,
                        )
                        label = "Lead assigned "
                    else:
                        self.teams.expand_assignment(
                            layout.run_id,
                            tuple(members),
                            safe_reason,
                        )
                        label = "Lead expanded assignment to "
                    transcript.append(label + ", ".join(members) + ": " + safe_reason)
                    self._store_transcript(layout, transcript)
                    if continue_after_assignment:
                        continue
                    return None, False
                if name == "revise":
                    if not allow_assignment or not member.coordinates:
                        raise ValueError(
                            "only the stored lead may request targeted member revisions"
                        )
                    members = action.get("members")
                    reason = action.get("reason")
                    if (
                        not isinstance(members, list)
                        or not members
                        or not all(
                            isinstance(value, str) and bool(value)
                            for value in members
                        )
                        or len(set(members)) != len(members)
                    ):
                        raise ValueError(
                            "revise action requires nonempty and unique "
                            "stored member keys"
                        )
                    if not isinstance(reason, str) or not reason.strip():
                        raise ValueError(
                            "revise action requires a specific revision reason"
                        )
                    assigned = {
                        assignment.member.stable_key: assignment.member
                        for assignment in self.teams.assignments_for_run(
                            layout.run_id
                        )
                    }
                    selected = [assigned.get(key) for key in members]
                    if any(
                        selected_member is not None
                        and (
                            selected_member.coordinates
                            or selected_member.independent_verifier
                        )
                        for selected_member in selected
                    ):
                        raise ValueError(
                            "targeted revisions cannot select the lead or "
                            "independent verifier"
                        )
                    if any(
                        selected_member is None
                        or selected_member.execution_class != "implementer"
                        for selected_member in selected
                    ):
                        raise ValueError(
                            "revise action may select only currently assigned "
                            "implementation members"
                        )
                    safe_reason = _bounded_redacted_text(
                        reason,
                        resolved_secret_values,
                    )
                    transcript.extend(
                        f"Revision requested for member {key}: {safe_reason}"
                        for key in members
                    )
                    self._store_transcript(layout, transcript)
                    return None, False
                if name == "note":
                    if _has_unadvanced_coordination_note(transcript):
                        raise ValueError(
                            "a coordination note already records an exact next action; "
                            "execute that action before writing another note"
                        )
                    summary = action.get("summary")
                    if not isinstance(summary, str) or not summary.strip():
                        raise ValueError("note action requires a nonempty summary")
                    label = (
                        "Lead" if member.coordinates else f"Member {member.stable_key}"
                    )
                    safe_summary = _bounded_redacted_text(
                        summary,
                        resolved_secret_values,
                        limit=max(
                            0,
                            _ACTION_HISTORY_LIMIT - len(label) - len(" note: "),
                        ),
                    )
                    transcript.append(f"{label} note: {safe_summary}")
                    self._store_transcript(layout, transcript)
                    continue
                if name == "finish":
                    if require_assignment:
                        raise ValueError(
                            "the stored lead must assign issue members before finishing"
                        )
                    summary = action.get("summary")
                    if not isinstance(summary, str) or not summary.strip():
                        raise ValueError("finish action requires a nonempty summary")
                    output = action.get("output")
                    if output is not None and not isinstance(output, dict):
                        raise ValueError("finish output must be an object")
                    label = (
                        "Lead" if member.coordinates else f"Member {member.stable_key}"
                    )
                    safe_summary = _bounded_redacted_text(
                        summary,
                        resolved_secret_values,
                        limit=max(
                            0,
                            _ACTION_HISTORY_LIMIT - len(label) - len(" finished: "),
                        ),
                    )
                    normalized_output: dict[str, object]
                    if isinstance(output, dict):
                        json.dumps(
                            output,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        if _contains_resolved_secret(
                            output,
                            resolved_secret_values,
                        ):
                            raise PermissionError(
                                "workflow output contains a resolved "
                                "secret value"
                            )
                        normalized_output = dict(output)
                    else:
                        normalized_output = {"summary": safe_summary}
                    if workflow_expected_output is not None:
                        validate_workflow_output(
                            normalized_output,
                            workflow_expected_output,
                        )
                    transcript.append(f"{label} finished: {safe_summary}")
                    self._store_transcript(layout, transcript)
                    if isinstance(output, dict):
                        return normalized_output, False
                    return safe_summary, False
                if name == "block":
                    reason = action.get("reason")
                    if not isinstance(reason, str) or not reason.strip():
                        raise ValueError("block action requires a specific reason")
                    self._ensure_not_canceled(layout.run_id)
                    safe_reason = _bounded_redacted_text(
                        reason,
                        resolved_secret_values,
                    )
                    if member.independent_verifier:
                        raise RevisionRequired(safe_reason)
                    if not member.coordinates:
                        handoff = "reported blocker for lead decision: " + safe_reason
                        transcript.append(
                            f"Member {member.stable_key} finished: {handoff}"
                        )
                        self._store_transcript(layout, transcript)
                        return handoff, False
                    self.lifecycle.transition(
                        layout.run_id,
                        RunState.BLOCKED,
                        reason=safe_reason,
                    )
                    return None, False
                self._ensure_not_canceled(layout.run_id)
                secrets: dict[str, str] | None = None
                argv = action.get("argv")
                if name == "run" and isinstance(argv, list):
                    secrets = self._command_secrets(
                        tuple(argv), secret_bindings, resolved_secret_values
                    )
                self._ensure_not_canceled(layout.run_id)
                result = self.tools.execute(
                    member,
                    policy,
                    layout,
                    action,
                    secrets=secrets,
                    workflow_resources=workflow_resources,
                    checkout_writable=checkout_writable,
                )
                self._ensure_not_canceled(layout.run_id)
                serialized_action = _serialize_action_for_history(
                    action,
                    resolved_secret_values,
                )
                transcript.append(
                    "Action "
                    + serialized_action
                    + "\nResult "
                    + redact_text(result, resolved_secret_values)[-4_000:]
                )
                self._store_transcript(layout, transcript)
            except _RunCanceled:
                return None, False
            except (PermissionError, ValueError) as error:
                if self._is_canceled(layout.run_id):
                    return None, False
                serialized_action = _serialize_action_for_history(
                    action,
                    resolved_secret_values,
                )
                transcript.append(
                    "Action "
                    + serialized_action
                    + "\nRejected safely; correct it and continue: "
                    + _bounded_redacted_text(
                        str(error),
                        resolved_secret_values,
                    )
                )
                self._store_transcript(layout, transcript)
            except Exception:
                if self._is_canceled(layout.run_id):
                    return None, False
                raise
        if self._is_canceled(layout.run_id):
            return None, False
        return None, True

    @staticmethod
    def _transcript_path(layout: RunLayout) -> Path:
        return layout.agent_state / "action-history.json"

    @staticmethod
    def _member_completion_key(item: str) -> str | None:
        if item.startswith("Lead finished:"):
            return "Lead"
        if not item.startswith("Member "):
            return None
        marker = " finished:"
        marker_index = item.find(marker)
        if marker_index <= len("Member "):
            return None
        return item[:marker_index]

    @staticmethod
    def _targeted_revision_key(item: str) -> str | None:
        prefix = "Revision requested for member "
        if not item.startswith(prefix):
            return None
        marker_index = item.find(":", len(prefix))
        if marker_index <= len(prefix):
            return None
        return "Member " + item[len(prefix):marker_index]

    @classmethod
    def _bounded_transcript(cls, transcript: Sequence[str]) -> list[str]:
        tail_start = max(0, len(transcript) - 24)
        latest_general_revision = max(
            (
                index
                for index, item in enumerate(transcript)
                if item.startswith("Revision requested for assigned members:")
            ),
            default=-1,
        )
        completions: dict[str, int] = {}
        targeted_revisions: dict[str, int] = {}
        for index in range(latest_general_revision + 1, len(transcript)):
            item = transcript[index]
            completion_key = cls._member_completion_key(item)
            if completion_key is not None:
                completions[completion_key] = index
            revision_key = cls._targeted_revision_key(item)
            if revision_key is not None:
                targeted_revisions[revision_key] = index
        checkpoint_indices = {
            index
            for index in (*completions.values(), *targeted_revisions.values())
            if index < tail_start
        }
        return [
            transcript[index]
            for index in sorted(checkpoint_indices)
        ] + list(transcript[tail_start:])

    def _load_transcript(self, layout: RunLayout) -> list[str]:
        path = self._transcript_path(layout)
        if not path.exists():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("stored agent action history is unreadable") from error
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RuntimeError("stored agent action history is invalid")
        return self._bounded_transcript(value)

    def _store_transcript(self, layout: RunLayout, transcript: list[str]) -> None:
        path = self._transcript_path(layout)
        temporary = path.with_name(path.name + ".tmp")
        bounded = self._bounded_transcript(transcript)
        temporary.write_text(
            json.dumps(bounded, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
        self.database.notify_activity_change()

    def _clear_transcript(self, layout: RunLayout) -> None:
        self._transcript_path(layout).unlink(missing_ok=True)

    def _head_is_controller_issue_commit(
        self,
        run: dict[str, object],
        message: str,
        policy: SandboxPolicy,
        layout: RunLayout,
    ) -> bool:
        metadata = self._git(
            policy,
            layout,
            ("show", "-s", "--format=%H%x09%ce%x09%s", "HEAD"),
            allow_failure=True,
        )
        if metadata.returncode != 0:
            raise RuntimeError(
                metadata.stderr.strip()
                or metadata.stdout.strip()
                or "cannot inspect current commit"
            )
        fields = metadata.stdout.rstrip("\r\n").split("\t", 2)
        if len(fields) != 3:
            raise RuntimeError("current commit metadata is invalid")
        commit_sha, committer_email, subject = fields
        return (
            commit_sha != str(run["base_sha"])
            and committer_email == "repogents@localhost"
            and subject == message
        )

    def _commit(
        self,
        run: dict[str, object],
        issue: dict[str, object],
        policy: SandboxPolicy,
        layout: RunLayout,
        resolved_secret_values: set[str],
        source_base_sha: str,
    ) -> str:
        self._ensure_not_canceled(layout.run_id)
        self._git(policy, layout, ("add", "-A"))
        check = self._git(
            policy, layout, ("diff", "--cached", "--check"), allow_failure=True
        )
        if check.returncode != 0:
            raise RevisionRequired(check.stderr.strip() or check.stdout.strip())
        full_diff = self._git(
            policy,
            layout,
            ("diff", "--binary", source_base_sha, "--"),
            allow_failure=False,
        ).stdout
        findings = self.scanner.scan(full_diff, resolved_secret_values)
        if findings:
            raise RevisionRequired(
                "potential secret in committed changes: " + ", ".join(findings)
            )
        staged = self._git(
            policy, layout, ("diff", "--cached", "--quiet"), allow_failure=True
        )
        if staged.returncode not in {0, 1}:
            raise RuntimeError(staged.stderr.strip() or "cannot inspect staged changes")
        if staged.returncode == 1:
            title = " ".join(str(issue["title"]).split())[:120]
            message = f"Resolve issue #{issue['number']}: {title}"
            commit_arguments = [
                "-c",
                "user.name=Repogents",
                "-c",
                "user.email=repogents@localhost",
                "commit",
                "--quiet",
            ]
            if self._head_is_controller_issue_commit(
                run,
                message,
                policy,
                layout,
            ):
                commit_arguments.extend(("--amend", "--no-edit"))
            else:
                commit_arguments.extend(("-m", message))
            commit = self._git(
                policy,
                layout,
                tuple(commit_arguments),
                allow_failure=True,
            )
            if commit.returncode != 0:
                raise RuntimeError(commit.stderr.strip() or "git commit failed")
        head = self._git(policy, layout, ("rev-parse", "HEAD")).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise RuntimeError("git returned an invalid commit SHA")
        ancestor = self._git(
            policy,
            layout,
            ("merge-base", "--is-ancestor", source_base_sha, head),
            allow_failure=True,
        )
        if ancestor.returncode != 0:
            raise RevisionRequired(
                "candidate does not descend from the prepared source base"
            )
        if head == source_base_sha:
            raise RevisionRequired("agent produced no committed issue change")
        return head

    def _ensure_validation_baselines(
        self,
        run: dict[str, object],
        sandbox_version_id: str,
        policy: SandboxPolicy,
        layout: RunLayout,
        secret_bindings: tuple[SecretBinding, ...],
        resolved_secret_values: set[str],
    ) -> None:
        run_id = str(run["id"])
        base_sha = str(run["base_sha"])
        self._ensure_not_canceled(run_id)
        with self.database.connect() as connection:
            commands = connection.execute(
                """SELECT id, command_json
                   FROM validation_commands
                   WHERE sandbox_version_id=? AND required=1
                   ORDER BY position""",
                (sandbox_version_id,),
            ).fetchall()
            baselines = connection.execute(
                """SELECT validation_command_id, base_sha, command_json
                   FROM validation_baselines
                   WHERE run_id=?""",
                (run_id,),
            ).fetchall()
        if not commands:
            raise MissingValidationCommands(
                "repository validation commands could not be derived; "
                "explicit input is required"
            )
        baseline_by_command = {
            str(row["validation_command_id"]): (
                str(row["base_sha"]),
                str(row["command_json"]),
            )
            for row in baselines
        }
        required_ids = {str(row["id"]) for row in commands}
        command_by_id = {str(row["id"]): str(row["command_json"]) for row in commands}
        for command_id, (
            stored_base_sha,
            stored_command,
        ) in baseline_by_command.items():
            if command_id not in required_ids:
                continue
            if stored_base_sha != base_sha:
                raise BaselineUnavailable(
                    "stored validation baseline does not match the run base SHA"
                )
            if stored_command != command_by_id[command_id]:
                raise BaselineUnavailable("stored validation baseline command changed")
        if required_ids.issubset(baseline_by_command):
            return

        head = self._git(
            policy,
            layout,
            ("rev-parse", "HEAD"),
            allow_failure=True,
        )
        head_sha = self._git_probe_output(head, "HEAD")
        status = self._git(
            policy,
            layout,
            ("status", "--porcelain", "--untracked-files=no"),
            allow_failure=True,
        )
        status_output = self._git_probe_output(status, "status")
        if head_sha != base_sha or status_output:
            raise BaselineUnavailable(
                "validation baseline is missing and the checkout is no longer "
                "the clean exact run base"
            )

        for row in commands:
            command_id = str(row["id"])
            if command_id in baseline_by_command:
                continue
            command = json.loads(str(row["command_json"]))
            if not isinstance(command, list) or not all(
                isinstance(value, str) for value in command
            ):
                raise RuntimeError("stored validation command is invalid")
            self._ensure_not_canceled(run_id)
            secrets = self._command_secrets(
                tuple(command),
                secret_bindings,
                resolved_secret_values,
            )
            result = self.sandbox.run(
                policy,
                layout,
                tuple(command),
                timeout=600,
                secrets=secrets,
            )
            if result.canceled:
                raise _RunCanceled(run_id)
            self._ensure_not_canceled(run_id)
            try:
                checked_head = self._git(
                    policy,
                    layout,
                    ("rev-parse", "HEAD"),
                    allow_failure=True,
                )
                checked_head_sha = self._git_probe_output(checked_head, "HEAD")
                checked_status = self._git(
                    policy,
                    layout,
                    ("status", "--porcelain", "--untracked-files=no"),
                    allow_failure=True,
                )
                checked_status_output = self._git_probe_output(
                    checked_status,
                    "status",
                )
            except _RunCanceled:
                raise
            except Exception:
                try:
                    self._git(
                        policy,
                        layout,
                        ("reset", "--hard", base_sha),
                        allow_failure=True,
                    )
                except _RunCanceled:
                    raise
                except Exception:
                    pass
                raise
            if checked_head_sha != base_sha or checked_status_output:
                self._git(
                    policy,
                    layout,
                    ("reset", "--hard", base_sha),
                    allow_failure=False,
                )
                raise BaselineUnavailable(
                    "validation baseline command changed the exact base checkout"
                )
            stdout = redact_text(result.stdout, resolved_secret_values)
            stderr = redact_text(result.stderr, resolved_secret_values)
            findings = extract_findings(stdout, stderr)
            if result.returncode != 0 and not findings:
                raise BaselineUnavailable(
                    "validation baseline failed without usable normalized findings: "
                    + " ".join(command)
                )
            mode = "strict" if result.returncode == 0 else "delta"
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO validation_baselines
                       (id, run_id, validation_command_id, command_json,
                        base_sha, mode, started_at, completed_at, exit_status,
                        log_path, findings_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _stable_id(f"{run_id}:{command_id}:baseline"),
                        run_id,
                        command_id,
                        str(row["command_json"]),
                        base_sha,
                        mode,
                        result.started_at,
                        result.completed_at,
                        result.returncode,
                        str(result.log_path),
                        _json(findings),
                    ),
                )

    def _validation_contract_changes(
        self,
        policy: SandboxPolicy,
        layout: RunLayout,
        base_sha: str,
        commit_sha: str,
        baseline_findings: tuple[str, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        changed = self._git(
            policy,
            layout,
            ("diff", "--name-only", base_sha, commit_sha, "--"),
        )
        changed_paths = tuple(path for path in changed.stdout.splitlines() if path)
        contract_paths: list[str] = []
        weakening: list[str] = []
        for path in changed_paths:
            path_diff = self._git(
                policy,
                layout,
                (
                    "diff",
                    "--unified=0",
                    base_sha,
                    commit_sha,
                    "--",
                    path,
                ),
            )
            policy_path = _is_validation_policy_path(path)
            source_marker, source_weakening = _source_suppression_analysis(
                path, path_diff.stdout
            )
            if policy_path or source_marker:
                contract_paths.append(path)
            if policy_path:
                weakening.extend(
                    _validation_weakening_reasons(
                        path,
                        path_diff.stdout,
                        baseline_findings,
                    )
                )
            weakening.extend(source_weakening)
        return tuple(contract_paths), tuple(weakening)

    def _validate(
        self,
        run_id: str,
        commit_sha: str,
        sandbox_version_id: str,
        policy: SandboxPolicy,
        layout: RunLayout,
        secret_bindings: tuple[SecretBinding, ...],
        resolved_secret_values: set[str],
        source_base_sha: str,
    ) -> tuple[bool, str]:
        self._ensure_not_canceled(run_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT validation_commands.id AS validation_command_id,
                          validation_commands.command_json AS command_json,
                          validation_baselines.command_json
                              AS baseline_command_json,
                          validation_baselines.base_sha AS baseline_base_sha,
                          validation_baselines.mode,
                          validation_baselines.findings_json,
                          runs.base_sha AS run_base_sha
                   FROM validation_commands
                   JOIN runs ON runs.id=?
                   LEFT JOIN validation_baselines
                     ON validation_baselines.run_id=runs.id
                    AND validation_baselines.validation_command_id=
                        validation_commands.id
                   WHERE validation_commands.sandbox_version_id=?
                     AND validation_commands.required=1
                   ORDER BY validation_commands.position""",
                (run_id, sandbox_version_id),
            ).fetchall()
        if not rows:
            raise MissingValidationCommands(
                "repository validation commands could not be derived; "
                "explicit input is required"
            )
        if any(row["mode"] is None for row in rows):
            raise BaselineUnavailable(
                "required validation baseline evidence is missing"
            )
        base_sha = str(rows[0]["run_base_sha"])
        if any(str(row["baseline_base_sha"]) != base_sha for row in rows):
            raise BaselineUnavailable("required validation baseline evidence is stale")
        if any(
            str(row["baseline_command_json"]) != str(row["command_json"])
            for row in rows
        ):
            raise BaselineUnavailable("stored validation baseline command changed")
        baseline_by_command: dict[str, tuple[str, ...]] = {}
        for row in rows:
            baseline_value = json.loads(str(row["findings_json"]))
            if not isinstance(baseline_value, list) or not all(
                isinstance(value, str) for value in baseline_value
            ):
                raise BaselineUnavailable(
                    "stored validation baseline findings are invalid"
                )
            baseline_by_command[str(row["validation_command_id"])] = tuple(
                baseline_value
            )
        all_baseline_findings = tuple(
            finding for findings in baseline_by_command.values() for finding in findings
        )
        contract_changes, weakening_detected = self._validation_contract_changes(
            policy,
            layout,
            source_base_sha,
            commit_sha,
            all_baseline_findings,
        )
        failures: list[str] = []
        for row in rows:
            command = json.loads(str(row["command_json"]))
            if not isinstance(command, list) or not all(
                isinstance(value, str) for value in command
            ):
                raise RuntimeError("stored validation command is invalid")
            baseline_findings = baseline_by_command[str(row["validation_command_id"])]
            self._ensure_not_canceled(run_id)
            secrets = self._command_secrets(
                tuple(command),
                secret_bindings,
                resolved_secret_values,
            )
            self._ensure_not_canceled(run_id)
            result = self.sandbox.run(
                policy,
                layout,
                tuple(command),
                timeout=600,
                secrets=secrets,
            )
            if result.canceled:
                raise _RunCanceled(run_id)
            self._ensure_not_canceled(run_id)
            stdout = redact_text(result.stdout, resolved_secret_values)
            stderr = redact_text(result.stderr, resolved_secret_values)
            candidate_findings = extract_findings(stdout, stderr)
            delta = compare_findings(
                baseline_findings,
                candidate_findings,
            )
            mode = str(row["mode"])
            output_usable = result.returncode == 0 or bool(candidate_findings)
            if mode == "strict":
                policy_passed = result.returncode == 0
            elif mode == "delta":
                policy_passed = output_usable and delta.passed
            else:
                raise BaselineUnavailable(
                    f"stored validation baseline mode is invalid: {mode}"
                )
            passed = policy_passed and not weakening_detected
            comparison = {
                "mode": mode,
                "baseline_count": len(baseline_findings),
                "candidate_count": len(candidate_findings),
                "new_count": len(delta.new),
                "resolved_count": len(delta.resolved),
                "unchanged_count": len(delta.unchanged),
                "new_findings": list(delta.new),
                "contract_changed": list(contract_changes),
                "weakening_detected": list(weakening_detected),
                "output_usable": output_usable,
            }
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT OR REPLACE INTO validation_results
                       (id, run_id, validation_command_id, commit_sha,
                        command_json, started_at, completed_at, exit_status,
                        log_path, verdict, findings_json, comparison_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _stable_id(f"{run_id}:{commit_sha}:{_json(command)}"),
                        run_id,
                        str(row["validation_command_id"]),
                        commit_sha,
                        _json(command),
                        result.started_at,
                        result.completed_at,
                        result.returncode,
                        str(result.log_path),
                        "pass" if passed else "fail",
                        _json(candidate_findings),
                        _json(comparison),
                    ),
                )
            if not passed:
                details = [
                    "$ " + " ".join(command),
                    f"mode={mode}",
                    f"exit={result.returncode}",
                ]
                if weakening_detected:
                    details.append(
                        "validation weakening detected: "
                        + "; ".join(weakening_detected)
                    )
                if not output_usable:
                    details.append("nonzero validation output had no usable findings")
                if delta.new:
                    details.append("new findings:\n" + "\n".join(delta.new))
                details.extend((stdout[-8_000:], stderr[-8_000:]))
                failures.append("\n".join(details))
        head_result = self._git(
            policy,
            layout,
            ("rev-parse", "HEAD"),
            allow_failure=True,
        )
        status_result = self._git(
            policy,
            layout,
            ("status", "--porcelain", "--untracked-files=no"),
            allow_failure=True,
        )
        invariant_failure = (
            head_result.returncode != 0
            or head_result.stdout.strip() != commit_sha
            or status_result.returncode != 0
            or bool(status_result.stdout.strip())
        )
        if invariant_failure:
            failures.append(
                "validation changed the tested commit or tracked worktree"
                + f"\nexpected HEAD={commit_sha}"
                + f"\nactual HEAD={head_result.stdout.strip()}"
                + f"\ntracked status={status_result.stdout.strip()}"
            )
            self._ensure_not_canceled(run_id)
            with self.database.transaction() as connection:
                command = ["repogents", "verify-commit-invariant"]
                comparison = {
                    "mode": "invariant",
                    "contract_changed": [],
                    "output_usable": True,
                }
                connection.execute(
                    """INSERT OR REPLACE INTO validation_results
                       (id, run_id, commit_sha, command_json, started_at,
                        completed_at, exit_status, log_path, verdict,
                        findings_json, comparison_json)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'fail', '[]', ?)""",
                    (
                        _stable_id(f"{run_id}:{commit_sha}:{_json(command)}"),
                        run_id,
                        commit_sha,
                        _json(command),
                        status_result.started_at,
                        status_result.completed_at,
                        str(status_result.log_path),
                        _json(comparison),
                    ),
                )
            self._git(
                policy,
                layout,
                ("reset", "--hard", commit_sha),
                allow_failure=False,
            )
        return not failures, "\n\n".join(failures)

    @staticmethod
    def _git_probe_output(result, probe: str) -> str:
        if result.returncode == 0:
            return result.stdout.strip()
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeError(
            f"validation baseline exact-base {probe} probe failed: {detail}"
        )

    def _git(
        self,
        policy: SandboxPolicy,
        layout: RunLayout,
        arguments: Sequence[str],
        *,
        allow_failure: bool = False,
    ):
        self._ensure_not_canceled(layout.run_id)
        result = self.sandbox.run(policy, layout, ("git", *arguments), timeout=120)
        if result.canceled:
            raise _RunCanceled(layout.run_id)
        self._ensure_not_canceled(layout.run_id)
        if result.returncode != 0 and not allow_failure:
            raise RuntimeError(
                result.stderr.strip() or result.stdout.strip() or "git command failed"
            )
        return result

    def _command_secrets(
        self,
        command: tuple[object, ...],
        bindings: tuple[SecretBinding, ...],
        resolved_secret_values: set[str],
    ) -> dict[str, str]:
        secrets: dict[str, str] = {}
        for name, reference, authorized_commands in bindings:
            if command not in authorized_commands:
                continue
            if self.secret_resolver is None:
                raise RuntimeError(
                    f"secret resolver is not configured for authorized binding {name}"
                )
            value = self.secret_resolver(reference)
            if not isinstance(value, str):
                raise TypeError(
                    f"secret resolver returned a non-string value for {name}"
                )
            secrets[name] = value
            resolved_secret_values.add(value)
        return secrets

    def _is_canceled(self, run_id: str) -> bool:
        if RunState(str(self.lifecycle.get_run(run_id)["state"])) == RunState.CANCELED:
            return True
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT repositories.enabled, repositories.removed_at
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        return row is None or not bool(row["enabled"]) or row["removed_at"] is not None

    def _ensure_not_canceled(self, run_id: str) -> None:
        if self._is_canceled(run_id):
            raise _RunCanceled(run_id)

    def _runtime(self, member: TeamMember, run_id: str) -> ModelRuntime:
        if member.runtime != MINI_SWE_RUNTIME:
            raise ValueError(f"unsupported stored model runtime: {member.runtime}")
        runtime = self.runtime_factory(
            member.runtime,
            member.model,
            member.action_timeout_seconds,
        )
        if isinstance(runtime, MiniSweModelRuntime):
            runtime.supervisor = self.process_supervisor
            runtime.run_id = run_id
        return runtime

    @staticmethod
    def _member_finished(transcript: Sequence[str], member: TeamMember) -> bool:
        prefix = (
            "Lead finished:"
            if member.coordinates
            else f"Member {member.stable_key} finished:"
        )
        general_revision_prefix = "Revision requested for assigned members:"
        targeted_revision_prefix = (
            f"Revision requested for member {member.stable_key}:"
        )
        for item in reversed(transcript):
            if item.startswith(prefix):
                return True
            if item.startswith(general_revision_prefix):
                return False
            if item.startswith("Revision requested for member ") and (
                member.coordinates
                or member.independent_verifier
                or item.startswith(targeted_revision_prefix)
            ):
                return False
        return False

    @staticmethod
    def _member_prompt(base_context: str, assignment: Assignment) -> str:
        member = assignment.member
        return (
            base_context
            + "\n\nCurrent stored member:\n"
            + json.dumps(
                {
                    "stable_key": member.stable_key,
                    "role": member.role,
                    "responsibilities": member.responsibilities,
                    "permitted_tools": member.permitted_tools,
                    "runtime": member.runtime,
                    "model": member.model,
                    "instructions": member.instructions,
                    "assignment_reason": assignment.reasoning,
                    "handoff": (
                        (
                            "Finish only to approve the candidate for commit and "
                            "validation. Block with concrete revision feedback when "
                            "the candidate is not correct; the controller will return "
                            "it to the assigned implementation members without any "
                            "external effect."
                        )
                        if member.independent_verifier
                        else (
                            "Only the stored lead owns the final blocked decision. "
                            "If required work exceeds your permissions, finish with "
                            "the exact evidence and required lead action."
                        )
                    ),
                },
                sort_keys=True,
                indent=2,
            )
        )

    def _load_context(
        self,
        run_id: str,
        issue_version_id: str,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT runs.*, issues.number AS issue_number,
                          issue_versions.title AS issue_title,
                          issue_versions.body AS issue_body,
                          issue_versions.discussion_json,
                          issues.url AS issue_url
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN issue_versions
                     ON issue_versions.issue_id=issues.id
                    AND issue_versions.id=?
                   WHERE runs.id=?""",
                (issue_version_id, run_id),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            sandbox_row = connection.execute(
                "SELECT * FROM sandbox_versions WHERE id=?",
                (row["sandbox_version_id"],),
            ).fetchone()
        run = dict(row)
        issue = {
            "version_id": issue_version_id,
            "number": row["issue_number"],
            "title": row["issue_title"],
            "body": row["issue_body"],
            "discussion": json.loads(row["discussion_json"]),
            "url": row["issue_url"],
        }
        return run, issue, dict(sandbox_row)

    @staticmethod
    def _workflow_assignment_contract(
        template: WorkflowTemplate,
    ) -> dict[str, object]:
        return {
            "agent_nodes": [
                {
                    "stable_key": node.stable_key,
                    "member_key": node.member_key,
                    "execution_class": node.execution_class,
                }
                for node in template.nodes
                if node.kind == "agent"
            ],
            "deterministic_nodes": [
                {
                    "stable_key": node.stable_key,
                    "operation": node.operation,
                }
                for node in template.nodes
                if node.kind == "deterministic"
            ],
            "edges": [
                {"source": edge.source, "target": edge.target}
                for edge in template.edges
            ],
            "assignment_rule": (
                "Select the full issue-relevant branch. Every selected "
                "specialist must include all upstream agent dependencies "
                "required by its path, plus the coordinating member and "
                "independent verifier."
            ),
        }

    @staticmethod
    def _base_prompt(
        run: dict[str, object],
        issue: dict[str, object],
        sandbox_row: dict[str, object],
        team: StoredTeam,
        assignments: Sequence[Assignment],
        additional_context: str | None,
        workflow_assignment_contract: Mapping[str, object] | None = None,
        atomic_specification: Mapping[str, object] | None = None,
    ) -> str:
        evidence = json.loads(str(sandbox_row["evidence_json"]))
        payload = {
            "task": "Implement and validate the GitHub issue in the isolated checkout",
            "repository_evidence": evidence,
            "issue": issue,
            "atomic_specification": (
                dict(atomic_specification)
                if atomic_specification is not None
                else None
            ),
            "base_sha": run["base_sha"],
            "stored_team": [
                {
                    "stable_key": member.stable_key,
                    "role": member.role,
                    "responsibilities": member.responsibilities,
                    "permitted_tools": member.permitted_tools,
                    "runtime": member.runtime,
                    "model": member.model,
                    "instructions": member.instructions,
                }
                for member in team.members
            ],
            "current_assignment": [
                assignment.member.stable_key for assignment in assignments
            ],
            "assignment": (
                "If this run has no durable assignment yet, inspect enough "
                "repository evidence to select stored members, then emit assign. "
                "Every assignment must include the stored lead and stored "
                "independent verifier. If later work requires an unselected stored "
                "member, the lead may emit assign again with the complete strict "
                "superset of current_assignment; never remove or replace members."
            ),
            "constraints": [
                "Infer terse requirements from issue discussion, repository instructions, source, and tests.",
                "Change only what the issue requires.",
                "Use only controller actions; all repository operations are sandboxed.",
                "Do not publish, push, merge, close, or access credentials.",
                "Do not create planning, specification, coordination, or status files in the checkout; persist controller-owned records through controller actions.",
                "Assignment and source mutation require the active atomic specification to have an approved independent semantic review.",
                "Derive specification behavior from the issue and repository evidence, not from a proposed implementation.",
                "Resolve fixable ambiguity through read-only inspection; block only for an irreducible unmet external prerequisite.",
                "Each specification criterion must state independently observable behavior and map to a concrete verification scenario.",
            ],
        }
        if atomic_specification is None:
            payload["specification_gate"] = (
                "The coordinator must persist the atomic issue specification "
                "before assignment or source mutation."
            )
        else:
            review = atomic_specification.get("review")
            verdict = review.get("verdict") if isinstance(review, Mapping) else None
            if verdict == "rejected":
                payload["specification_gate"] = (
                    "The coordinator must revise the rejected specification "
                    "before assignment or source mutation."
                )
            elif verdict == "approved":
                payload["specification_gate"] = (
                    "The active specification has approved independent review."
                )
            else:
                payload["specification_gate"] = (
                    "The independent verifier must review the active "
                    "specification before assignment or source mutation."
                )
        if workflow_assignment_contract is not None:
            payload["workflow_assignment_contract"] = dict(
                workflow_assignment_contract
            )
        if run.get("reason"):
            payload["revision_feedback"] = run["reason"]
        if additional_context:
            payload["additional_context"] = additional_context
        return json.dumps(payload, sort_keys=True, indent=2)

    @staticmethod
    def _specification_reconciliation_prompt(
        base_context: str,
        specification: Mapping[str, object] | None,
    ) -> str:
        reconciliation_contract = {
            "task": (
                "Reconcile the complete atomic issue specification against "
                "the new feedback or information context"
            ),
            "current_specification_revision_id": (
                specification.get("id") if specification is not None else None
            ),
            "required_action": "specify",
            "decision_rules": [
                (
                    "If the new context does not change required observable "
                    "behavior, resubmit every current specification item unchanged."
                ),
                (
                    "If requirements changed, submit one complete corrected "
                    "revision containing every retained and new requirement."
                ),
            ],
            "constraints": [
                "Do not assign work or mutate the checkout before reconciliation.",
                "Do not submit implementation plans or implementation-derived criteria.",
                "Preserve stable item and criterion keys for unchanged behavior.",
            ],
        }
        return (
            base_context
            + "\n\n"
            + json.dumps(reconciliation_contract, sort_keys=True, indent=2)
        )


    @staticmethod
    def _specification_review_prompt(
        base_context: str,
        specification: Mapping[str, object],
    ) -> str:
        review_contract = {
            "task": "Independently review the active atomic issue specification",
            "specification_revision_id": specification["id"],
            "required_action": "review_specification",
            "rubric": [
                "semantic issue coverage",
                "behavioral clarity and independently observable acceptance criteria",
                "verification feasibility against repository evidence",
                "internal consistency, repository alignment, and issue scope discipline",
            ],
            "constraints": [
                "Use read-only repository evidence.",
                "Do not authorize or perform assignment or source mutation.",
                "Approve only complete and internally consistent issue behavior.",
                "Reject criteria coupled to a proposed implementation instead of observable behavior.",
                "Verify that every criterion is covered by a feasible verification scenario.",
                "Reject fixable gaps with structured findings; block only an irreducible external prerequisite.",
            ],
        }
        return (
            base_context
            + "\n\n"
            + json.dumps(review_contract, sort_keys=True, indent=2)
        )



def _sandbox_policy(row: dict[str, object]) -> SandboxPolicy:
    payload = json.loads(str(row["policy_json"]))
    mounts: list[Mount] = []
    for index, value in enumerate(payload.get("allowed_host_paths", [])):
        if not isinstance(value, dict) or "path" not in value:
            raise ValueError("stored host-path policy is invalid")
        target = str(value.get("target") or f"/mnt/inputs/{index}")
        mounts.append(
            Mount(
                Path(str(value["path"])),
                target,
                writable=str(value.get("mode", "read-only")) == "writable",
            )
        )
    names = tuple(binding[0] for binding in _secret_bindings(row))
    return SandboxPolicy(
        persistent_root=Path(str(row["root_path"])),
        mounts=tuple(mounts),
        allowed_services=tuple(
            str(value) for value in payload.get("allowed_services", [])
        ),
        allowed_secret_names=names,
    )


def _secret_bindings(row: dict[str, object]) -> tuple[SecretBinding, ...]:
    payload = json.loads(str(row["policy_json"]))
    values = payload.get("secret_bindings", [])
    if not isinstance(values, list):
        raise ValueError("stored secret bindings must be a list")
    bindings: list[SecretBinding] = []
    names: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("stored secret binding is invalid")
        name = value.get("name")
        reference = value.get("reference")
        commands = value.get("commands")
        if not isinstance(name, str) or not name:
            raise ValueError("stored secret binding name is invalid")
        if name in names:
            raise ValueError(f"duplicate stored secret binding name: {name}")
        if not isinstance(reference, str) or not reference:
            raise ValueError(f"stored secret binding reference is invalid for {name}")
        if not isinstance(commands, list) or not commands:
            raise ValueError(f"stored secret binding commands are invalid for {name}")
        normalized_commands: list[tuple[str, ...]] = []
        for command in commands:
            if (
                not isinstance(command, list)
                or not command
                or not all(
                    isinstance(argument, str) and argument for argument in command
                )
            ):
                raise ValueError(f"stored secret binding command is invalid for {name}")
            normalized_commands.append(tuple(command))
        names.add(name)
        bindings.append((name, reference, tuple(normalized_commands)))
    return tuple(bindings)


def _stable_id(value: str) -> str:
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
