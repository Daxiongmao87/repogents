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

_ACTION_HISTORY_LIMIT = 2_000
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
{"action":"note","summary":"concise findings and exact next action"}
{"action":"finish","summary":"implemented behavior and why it satisfies the issue"}
{"action":"block","reason":"specific irreducible missing or contradictory prerequisite"}
Read before editing. Do not reread evidence already present in action history unless its result was incomplete or the source changed. Once inspection supports a decision, persist one concise note with the findings and exact next action, then execute that action instead of continuing to inspect. After a note, the next decision must execute the stated action; the controller rejects another note until a repository write or replacement succeeds. Assignment is available only before issue work begins. A note neither finishes nor blocks the work. Keep changes strictly in issue scope. Do not create or retain plans, specification ledgers, coordination files, agent instructions, or other process artifacts in the repository; change only product source, repository-required tests, and directly required configuration. Never publish, merge, close, push, expose credentials, or invent missing external resources."""
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
            result = self.sandbox.run(
                policy,
                layout,
                tuple(argv),
                timeout=timeout,
                secrets=secrets,
                checkout_writable=checkout_writable,
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
        self, run_id: str, *, additional_context: str | None = None
    ) -> str | None:
        run, issue, sandbox_row = self._load_context(run_id)
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
        lead = next(member for member in team.members if member.role == "lead")
        layout = RunLayout.create(
            Path(str(run["run_path"])).parents[3],
            str(run["repository_id"]),
            run_id,
        )
        policy = _sandbox_policy(sandbox_row)
        secret_bindings = _secret_bindings(sandbox_row)
        resolved_secret_values: set[str] = set()
        base_context = self._base_prompt(
            run, issue, sandbox_row, team, additional_context
        )
        transcript = self._load_transcript(layout)
        assignments = self.teams.assignments_for_run(run_id)
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
            )
            return None
        if not resume_validation:
            for assignment in assignments:
                member = assignment.member
                if member.role == "lead" or self._member_finished(transcript, member):
                    continue
                outcome, yielded = self._agent_cycle(
                    self._runtime(member, run_id),
                    member,
                    policy,
                    layout,
                    self._member_prompt(base_context, assignment),
                    transcript,
                    secret_bindings,
                    resolved_secret_values,
                )
                if yielded or outcome is None:
                    return None
        runtime = self._runtime(lead, run_id)
        for cycle in range(self.max_revision_cycles):
            if not (resume_validation and cycle == 0):
                outcome, yielded = self._agent_cycle(
                    runtime,
                    lead,
                    policy,
                    layout,
                    base_context,
                    transcript,
                    secret_bindings,
                    resolved_secret_values,
                )
                if yielded or outcome is None:
                    return None
            try:
                self._ensure_not_canceled(run_id)
                state = RunState(str(self.lifecycle.get_run(run_id)["state"]))
                if state in {RunState.IMPLEMENTING, RunState.RESOLVING_FEEDBACK}:
                    self.lifecycle.transition(run_id, RunState.VALIDATING)
                self._ensure_not_canceled(run_id)
                commit_sha = self._commit(
                    run, issue, policy, layout, resolved_secret_values
                )
            except _RunCanceled:
                return None
            except RevisionRequired as error:
                if self._is_canceled(run_id):
                    return None
                transcript.append(
                    "Commit preparation found a source-fixable problem. "
                    "Revise the implementation:\n"
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
                )
            except _RunCanceled:
                return None
            except MissingValidationCommands as error:
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
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE runs SET validated_sha=?, updated_at=? WHERE id=?",
                        (commit_sha, _utc_now(), run_id),
                    )
                if self._is_canceled(run_id):
                    return None
                self._clear_transcript(layout)
                self.lifecycle.transition(run_id, RunState.PUBLISHING)
                return commit_sha
            if self._is_canceled(run_id):
                return None
            transcript.append(
                "Validation for commit "
                + commit_sha
                + " failed. Revise the implementation:\n"
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
    ) -> tuple[str | None, bool]:
        for _ in range(self.max_actions):
            context = base_context
            if transcript:
                context += "\n\nAction history:\n" + "\n".join(transcript[-24:])
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
                        raise ValueError(
                            "assignment is allowed only before issue work begins"
                        )
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
                    self.teams.assign(
                        layout.run_id,
                        tuple(members),
                        safe_reason,
                    )
                    transcript.append(
                        "Lead assigned " + ", ".join(members) + ": " + safe_reason
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
                        "Lead"
                        if member.role == "lead"
                        else f"Member {member.stable_key}"
                    )
                    safe_summary = _bounded_redacted_text(
                        summary,
                        resolved_secret_values,
                    )
                    transcript.append(f"{label} note: {safe_summary}")
                    self._store_transcript(layout, transcript)
                    continue
                if name == "finish":
                    if allow_assignment:
                        raise ValueError(
                            "the stored lead must assign issue members before finishing"
                        )
                    summary = action.get("summary")
                    if not isinstance(summary, str) or not summary.strip():
                        raise ValueError("finish action requires a nonempty summary")
                    label = (
                        "Lead"
                        if member.role == "lead"
                        else f"Member {member.stable_key}"
                    )
                    safe_summary = _bounded_redacted_text(
                        summary,
                        resolved_secret_values,
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
        return value[-24:]

    def _store_transcript(self, layout: RunLayout, transcript: list[str]) -> None:
        path = self._transcript_path(layout)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(transcript[-24:], ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _clear_transcript(self, layout: RunLayout) -> None:
        self._transcript_path(layout).unlink(missing_ok=True)

    def _commit(
        self,
        run: dict[str, object],
        issue: dict[str, object],
        policy: SandboxPolicy,
        layout: RunLayout,
        resolved_secret_values: set[str],
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
            ("diff", "--binary", str(run["base_sha"]), "--"),
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
            commit = self._git(
                policy,
                layout,
                (
                    "-c",
                    "user.name=Repogents",
                    "-c",
                    "user.email=repogents@localhost",
                    "commit",
                    "--quiet",
                    "-m",
                    message,
                ),
                allow_failure=True,
            )
            if commit.returncode != 0:
                raise RuntimeError(commit.stderr.strip() or "git commit failed")
        head = self._git(policy, layout, ("rev-parse", "HEAD")).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise RuntimeError("git returned an invalid commit SHA")
        if head == str(run["base_sha"]):
            raise RevisionRequired("agent produced no committed issue change")
        return head

    def _validate(
        self,
        run_id: str,
        commit_sha: str,
        sandbox_version_id: str,
        policy: SandboxPolicy,
        layout: RunLayout,
        secret_bindings: tuple[SecretBinding, ...],
        resolved_secret_values: set[str],
    ) -> tuple[bool, str]:
        self._ensure_not_canceled(run_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT command_json FROM validation_commands
                   WHERE sandbox_version_id=? AND required=1 ORDER BY position""",
                (sandbox_version_id,),
            ).fetchall()
        if not rows:
            raise MissingValidationCommands(
                "repository validation commands could not be derived; explicit input is required"
            )
        failures: list[str] = []
        for row in rows:
            command = json.loads(row["command_json"])
            if not isinstance(command, list) or not all(
                isinstance(value, str) for value in command
            ):
                raise RuntimeError("stored validation command is invalid")
            self._ensure_not_canceled(run_id)
            secrets = self._command_secrets(
                tuple(command), secret_bindings, resolved_secret_values
            )
            self._ensure_not_canceled(run_id)
            result = self.sandbox.run(
                policy, layout, tuple(command), timeout=600, secrets=secrets
            )
            if result.canceled:
                raise _RunCanceled(run_id)
            self._ensure_not_canceled(run_id)
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT OR REPLACE INTO validation_results
                       (id, run_id, commit_sha, command_json, started_at,
                        completed_at, exit_status, log_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _stable_id(f"{run_id}:{commit_sha}:{_json(command)}"),
                        run_id,
                        commit_sha,
                        _json(command),
                        result.started_at,
                        result.completed_at,
                        result.returncode,
                        str(result.log_path),
                    ),
                )
            if result.returncode != 0:
                failures.append(
                    "$ "
                    + " ".join(command)
                    + f"\nexit={result.returncode}\n"
                    + result.stdout[-8_000:]
                    + "\n"
                    + result.stderr[-8_000:]
                )
        head_result = self._git(
            policy, layout, ("rev-parse", "HEAD"), allow_failure=True
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
                connection.execute(
                    """INSERT OR REPLACE INTO validation_results
                       (id, run_id, commit_sha, command_json, started_at,
                        completed_at, exit_status, log_path)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        _stable_id(f"{run_id}:{commit_sha}:{_json(command)}"),
                        run_id,
                        commit_sha,
                        _json(command),
                        status_result.started_at,
                        status_result.completed_at,
                        str(status_result.log_path),
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
        return (
            RunState(str(self.lifecycle.get_run(run_id)["state"])) == RunState.CANCELED
        )

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
        prefix = f"Member {member.stable_key} finished:"
        return any(item.startswith(prefix) for item in transcript)

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
                },
                sort_keys=True,
                indent=2,
            )
        )

    def _load_context(
        self, run_id: str
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT runs.*, issues.number AS issue_number, issues.title AS issue_title,
                          issues.body AS issue_body, issues.discussion_json,
                          issues.url AS issue_url
                   FROM runs JOIN issues ON issues.id=runs.issue_id WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            sandbox_row = connection.execute(
                "SELECT * FROM sandbox_versions WHERE id=?",
                (row["sandbox_version_id"],),
            ).fetchone()
        run = dict(row)
        issue = {
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
            "assignment": (
                "If this run has no durable assignment yet, inspect enough "
                "repository evidence to select stored members, then emit assign. "
                "Include lead and select only members needed for this issue."
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
