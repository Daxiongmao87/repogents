from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .controller import RunProcessSupervisor
from .database import Database
from .lifecycle import RunLifecycle, RunState
from .mini_swe import MINI_SWE_RUNTIME, MiniSweInference
from .sandbox import (
    Mount,
    RunLayout,
    SandboxManager,
    SandboxPolicy,
    SecretScanner,
    redact_text,
)
from .team import Assignment, StoredTeam, TeamMember, TeamService
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
    "start",
    "end",
    "pattern",
    "count",
    "members",
    "reason",
    "summary",
    "old",
    "new",
    "content",
)
_ACTION_STRING_LIMITS = {
    "action": 64,
    "path": 1_024,
    "argv": 96,
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
{"action":"run","argv":["program","arg"],"timeout":120}
{"action":"assign","members":["lead","member-key"],"reason":"why these stored members are needed"}
{"action":"revise","members":["member-key"],"reason":"why these assigned implementers must run again"}
{"action":"note","summary":"concise findings and exact next action"}
{"action":"finish","summary":"implemented behavior and why it satisfies the issue"}
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
                    "assign",
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
            "members": {
                "type": "array",
                "items": {"type": "string"},
            },
            "reason": {"type": "string"},
            "summary": {"type": "string"},
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


class AgentToolExecutor:
    _TOOL_PERMISSION = {
        "list": "read",
        "read": "read",
        "search": "read",
        "write": "write",
        "replace": "write",
        "run": "run",
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
    ) -> str:
        name = action.get("action")
        if not isinstance(name, str) or name not in self._TOOL_PERMISSION:
            raise ValueError(f"unsupported agent action: {name}")
        permission = self._TOOL_PERMISSION[name]
        if permission not in member.permitted_tools:
            raise PermissionError(
                f"stored team member {member.stable_key} is not permitted to use {permission}"
            )
        if name == "run":
            argv = action.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(argument, str) and argument for argument in argv)
            ):
                raise ValueError("run action argv must be a nonempty string array")
            timeout_value = action.get("timeout", 120)
            if isinstance(timeout_value, bool) or not isinstance(
                timeout_value, (int, float)
            ):
                raise ValueError("run action timeout must be a number")
            timeout = min(max(float(timeout_value), 1), 300)
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
            result = self.sandbox.run(
                policy,
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
            return json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
        encoded = base64.urlsafe_b64encode(
            json.dumps(action, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
        self.scanner = SecretScanner()

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
        policy = self._resource_command_policy(
            str(run["sandbox_version_id"]),
            _sandbox_policy(sandbox_row),
        )
        source_base_sha = comparison_base_sha or str(run["base_sha"])
        if not re.fullmatch(r"[0-9a-f]{40}", source_base_sha):
            raise ValueError("source comparison base SHA is invalid")
        secret_bindings = _secret_bindings(sandbox_row)
        variable_bindings = _variable_bindings(sandbox_row)
        resolved_secret_values: set[str] = set()
        try:
            self._ensure_validation_baselines(
                run,
                str(run["sandbox_version_id"]),
                policy,
                layout,
                secret_bindings,
                variable_bindings,
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
            run, issue, sandbox_row, team, assignments, additional_context
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
                    variable_bindings,
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
    ) -> tuple[str | None, bool]:
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
                    transcript.append(f"{label} finished: {safe_summary}")
                    self._store_transcript(layout, transcript)
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
                    member, policy, layout, action, secrets=secrets
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

    def _resource_command_policy(
        self, sandbox_version_id: str, policy: SandboxPolicy
    ) -> SandboxPolicy:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT policy_json FROM sandbox_versions WHERE id=?",
                (sandbox_version_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("sandbox version not found for repository resources")
        payload = json.loads(str(row["policy_json"]))
        artifacts = payload.get("artifact_bindings", [])
        if not isinstance(artifacts, list):
            raise ValueError("stored artifact bindings must be a list")
        mounts = list(policy.mounts)
        for value in artifacts:
            if not isinstance(value, dict):
                raise ValueError("stored artifact binding is invalid")
            storage_path = value.get("storage_path")
            sandbox_path = value.get("sandbox_path")
            if not isinstance(storage_path, str) or not Path(storage_path).is_absolute():
                raise ValueError("stored artifact storage path is invalid")
            if not isinstance(sandbox_path, str) or not sandbox_path.startswith("/"):
                raise ValueError("stored artifact sandbox path is invalid")
            if not Path(storage_path).is_file():
                name = str(value.get("name") or sandbox_path)
                revision = value.get("revision")
                raise BaselineUnavailable(
                    f"required artifact {name} revision {revision} is missing or inaccessible"
                )
            mounts.append(Mount(Path(storage_path), sandbox_path, writable=False))
        return SandboxPolicy(
            persistent_root=policy.persistent_root,
            mounts=tuple(mounts),
            allowed_services=policy.allowed_services,
            allowed_secret_names=policy.allowed_secret_names,
        )

    def _ensure_validation_baselines(
        self,
        run: dict[str, object],
        sandbox_version_id: str,
        policy: SandboxPolicy,
        layout: RunLayout,
        secret_bindings: tuple[SecretBinding, ...],
        variable_bindings: tuple[tuple[str, str, tuple[tuple[str, ...], ...]], ...],
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
        status = self._git(
            policy,
            layout,
            ("status", "--porcelain", "--untracked-files=no"),
            allow_failure=True,
        )
        if (
            head.returncode != 0
            or head.stdout.strip() != base_sha
            or status.returncode != 0
            or bool(status.stdout.strip())
        ):
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
            environment_bindings = {
                name: value
                for name, value, commands in variable_bindings
                if tuple(command) in commands
            }
            result = self.sandbox.run(
                self._resource_command_policy(sandbox_version_id, policy),
                layout,
                tuple(command),
                timeout=600,
                secrets=secrets,
                environment_bindings=environment_bindings or None,
            )
            if result.canceled:
                raise _RunCanceled(run_id)
            self._ensure_not_canceled(run_id)
            checked_head = self._git(
                policy,
                layout,
                ("rev-parse", "HEAD"),
                allow_failure=True,
            )
            checked_status = self._git(
                policy,
                layout,
                ("status", "--porcelain", "--untracked-files=no"),
                allow_failure=True,
            )
            if (
                checked_head.returncode != 0
                or checked_head.stdout.strip() != base_sha
                or checked_status.returncode != 0
                or bool(checked_status.stdout.strip())
            ):
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
        variable_bindings: tuple[tuple[str, str, tuple[tuple[str, ...], ...]], ...],
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
            environment_bindings = {
                name: value
                for name, value, commands in variable_bindings
                if tuple(command) in commands
            }
            self._ensure_not_canceled(run_id)
            result = self.sandbox.run(
                self._resource_command_policy(sandbox_version_id, policy),
                layout,
                tuple(command),
                timeout=600,
                secrets=secrets,
                environment_bindings=environment_bindings or None,
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
    def _base_prompt(
        run: dict[str, object],
        issue: dict[str, object],
        sandbox_row: dict[str, object],
        team: StoredTeam,
        assignments: Sequence[Assignment],
        additional_context: str | None,
    ) -> str:
        evidence = json.loads(str(sandbox_row["evidence_json"]))
        payload = {
            "task": "Implement and validate the GitHub issue in the isolated checkout",
            "repository_evidence": evidence,
            "issue": issue,
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
            ],
        }
        if run.get("reason"):
            payload["revision_feedback"] = run["reason"]
        if additional_context:
            payload["additional_context"] = additional_context
        return json.dumps(payload, sort_keys=True, indent=2)


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


def _resource_command_policy(
    row: dict[str, object], base_policy: SandboxPolicy
) -> SandboxPolicy:
    payload = json.loads(str(row["policy_json"]))
    artifacts = payload.get("artifact_bindings", [])
    if not isinstance(artifacts, list):
        raise ValueError("stored artifact bindings must be a list")
    mounts = list(base_policy.mounts)
    for value in artifacts:
        if not isinstance(value, dict):
            raise ValueError("stored artifact binding is invalid")
        storage_path = value.get("storage_path")
        sandbox_path = value.get("sandbox_path")
        if not isinstance(storage_path, str) or not Path(storage_path).is_absolute():
            raise ValueError("stored artifact storage path is invalid")
        if not isinstance(sandbox_path, str) or not sandbox_path.startswith("/"):
            raise ValueError("stored artifact sandbox path is invalid")
        mounts.append(Mount(Path(storage_path), sandbox_path, writable=False))
    return SandboxPolicy(
        persistent_root=base_policy.persistent_root,
        mounts=tuple(mounts),
        allowed_services=base_policy.allowed_services,
        allowed_secret_names=base_policy.allowed_secret_names,
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


def _variable_bindings(
    row: dict[str, object],
) -> tuple[tuple[str, str, tuple[tuple[str, ...], ...]], ...]:
    payload = json.loads(str(row["policy_json"]))
    values = payload.get("variable_bindings", [])
    if not isinstance(values, list):
        raise ValueError("stored variable bindings must be a list")
    bindings: list[tuple[str, str, tuple[tuple[str, ...], ...]]] = []
    names: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("stored variable binding is invalid")
        name = item.get("name")
        value = item.get("value")
        commands = item.get("commands")
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", name
        ):
            raise ValueError("stored variable binding name is invalid")
        if name in names:
            raise ValueError(f"duplicate stored variable binding name: {name}")
        if not isinstance(value, str):
            raise ValueError(f"stored variable binding value is invalid for {name}")
        if not isinstance(commands, list) or not commands:
            raise ValueError(f"stored variable binding commands are invalid for {name}")
        normalized_commands: list[tuple[str, ...]] = []
        for command in commands:
            if (
                not isinstance(command, list)
                or not command
                or not all(
                    isinstance(argument, str) and argument for argument in command
                )
            ):
                raise ValueError(
                    f"stored variable binding command is invalid for {name}"
                )
            normalized_commands.append(tuple(command))
        names.add(name)
        bindings.append((name, value, tuple(normalized_commands)))
    return tuple(bindings)


def _stable_id(value: str) -> str:
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
