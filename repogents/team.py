from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence

from .database import Database
from .mini_swe import MINI_SWE_RUNTIME, MiniSweInference
from .workflow import (
    DeterministicOperationRegistry,
    validate_workflow_design,
    workflow_resource_tool_requirements,
)

DEFAULT_ACTION_TIMEOUT_SECONDS = 600.0
_TEAM_DESIGN_PROMPT_LIMIT_BYTES = 96_000
_TEAM_SOURCE_FILE_LIMIT_BYTES = 16_000
_TEAM_INSTRUCTION_LIMIT_BYTES = 32_000
_TEAM_EXECUTION_CLASSES = {"lead", "scout", "implementer", "verifier"}
_TEAM_TOOLS = {"read", "write", "run", "git_diff", "git_commit"}


class InspectionEvidence(Protocol):
    @property
    def languages(self) -> tuple[str, ...]: ...

    @property
    def validation_commands(self) -> tuple[tuple[str, ...], ...]: ...

    @property
    def file_count(self) -> int: ...

    @property
    def summary(self) -> str: ...


@dataclass(frozen=True)
class TeamMember:
    id: str
    stable_key: str
    role: str
    execution_class: str
    coordinates: bool
    independent_verifier: bool
    responsibilities: str
    permitted_tools: tuple[str, ...]
    runtime: str
    model: str
    instructions: str
    action_timeout_seconds: float = DEFAULT_ACTION_TIMEOUT_SECONDS


@dataclass(frozen=True)
class StoredTeam:
    id: str
    repository_id: str
    version: int
    evidence: dict[str, object]
    members: tuple[TeamMember, ...]


@dataclass(frozen=True)
class TeamDesign:
    members: tuple[dict[str, object], ...]
    workflow: dict[str, object]


class _InvalidTeamDesign(ValueError):
    pass


@dataclass(frozen=True)
class Assignment:
    id: str
    run_id: str
    member: TeamMember
    reasoning: str
    assigned_at: str


def _member_execution_class(
    *,
    coordinates: bool,
    independent_verifier: bool,
    permitted_tools: Sequence[object],
) -> str:
    if coordinates:
        return "lead"
    if independent_verifier:
        return "verifier"
    if "write" in permitted_tools:
        return "implementer"
    return "scout"


def validate_team_members(members: Sequence[dict[str, object]]) -> None:
    coordinators = [member for member in members if member.get("coordinates") is True]
    if len(coordinators) != 1:
        raise ValueError("repository team must contain exactly one coordinating member")
    verifiers = [
        member for member in members if member.get("independent_verifier") is True
    ]
    if len(verifiers) != 1:
        raise ValueError(
            "repository team must contain exactly one independent verifier"
        )
    keys = [member.get("stable_key") for member in members]
    if any(
        not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", key)
        for key in keys
    ) or len(keys) != len(set(keys)):
        raise ValueError(
            "repository team member identities (stable keys) must be safe, "
            "nonempty, and unique"
        )
    has_implementation_member = False
    for member in members:
        role = member.get("role")
        if (
            not isinstance(role, str)
            or not role.strip()
            or len(role) > 128
            or any(ord(character) < 32 for character in role)
        ):
            raise ValueError(
                "repository team member atomic role must be a safe nonempty string"
            )
        coordinates = member.get("coordinates")
        independent_verifier = member.get("independent_verifier")
        if not isinstance(coordinates, bool) or not isinstance(
            independent_verifier,
            bool,
        ):
            raise ValueError(
                "team coordination and independent-verifier markers must be booleans"
            )
        if coordinates and independent_verifier:
            raise ValueError(
                "the coordinating member cannot also be the independent verifier"
            )
        tools = member.get("permitted_tools")
        if (
            not isinstance(tools, list)
            or not tools
            or not all(isinstance(tool, str) and tool for tool in tools)
        ):
            raise ValueError(
                "team member permitted tools must be a nonempty string list"
            )
        if len(tools) != len(set(tools)):
            raise ValueError("team member permitted tools must be unique")
        unsupported = set(tools) - _TEAM_TOOLS
        if unsupported:
            raise ValueError(
                "team member has unsupported controller tool permissions: "
                + ", ".join(sorted(unsupported))
            )
        expected_class = _member_execution_class(
            coordinates=coordinates,
            independent_verifier=independent_verifier,
            permitted_tools=tools,
        )
        execution_class = member.get("execution_class")
        if (
            execution_class not in _TEAM_EXECUTION_CLASSES
            or execution_class != expected_class
        ):
            raise ValueError(
                "team member execution class does not match its markers and tools"
            )
        if coordinates and "write" in tools:
            raise ValueError("the coordinating member cannot receive the write tool")
        if independent_verifier:
            if "write" in tools:
                raise ValueError(
                    "the independent verifier cannot receive the write tool"
                )
            if not {"read", "run"}.issubset(tools):
                raise ValueError("the independent verifier requires read and run tools")
        if (
            not coordinates
            and not independent_verifier
            and execution_class == "implementer"
        ):
            has_implementation_member = True
        for field in ("responsibilities", "runtime", "model"):
            value = member.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"team member {field} must be a nonempty string")
        if not isinstance(member.get("instructions"), str):
            raise ValueError("team member instructions must be a string")
        action_timeout = member.get(
            "action_timeout_seconds",
            DEFAULT_ACTION_TIMEOUT_SECONDS,
        )
        if (
            isinstance(action_timeout, bool)
            or not isinstance(action_timeout, (int, float))
            or action_timeout <= 0
        ):
            raise ValueError("team member action timeout must be a positive number")
    if not has_implementation_member:
        raise ValueError(
            "repository team must contain an implementation member distinct "
            "from coordination and independent verification"
        )


def _validate_legacy_team_members(
    members: Sequence[dict[str, object]],
) -> None:
    """Validate immutable teams created before agent-designed composition."""
    execution_classes = [member.get("execution_class") for member in members]
    if execution_classes.count("lead") != 1:
        raise ValueError("legacy repository team must contain exactly one stored lead")
    keys = [member.get("stable_key") for member in members]
    if any(
        not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", key)
        for key in keys
    ) or len(keys) != len(set(keys)):
        raise ValueError(
            "legacy repository team member identities must be safe and unique"
        )
    for member in members:
        role = member.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError("legacy repository team member role must be nonempty")
        execution_class = member.get("execution_class")
        if execution_class not in _TEAM_EXECUTION_CLASSES:
            raise ValueError(
                "legacy repository team member execution class is unsupported"
            )
        tools = member.get("permitted_tools")
        if (
            not isinstance(tools, list)
            or not tools
            or not all(isinstance(tool, str) and tool for tool in tools)
            or len(tools) != len(set(tools))
        ):
            raise ValueError(
                "legacy team member permitted tools must be a unique string list"
            )
        unsupported = set(tools) - _TEAM_TOOLS
        if unsupported:
            raise ValueError(
                "legacy team member has unsupported controller tools: "
                + ", ".join(sorted(unsupported))
            )
        for field in ("responsibilities", "runtime", "model"):
            value = member.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"legacy team member {field} must be a nonempty string"
                )
        if not isinstance(member.get("instructions"), str):
            raise ValueError("legacy team member instructions must be a string")
        action_timeout = member.get(
            "action_timeout_seconds",
            DEFAULT_ACTION_TIMEOUT_SECONDS,
        )
        if (
            isinstance(action_timeout, bool)
            or not isinstance(action_timeout, (int, float))
            or action_timeout <= 0
        ):
            raise ValueError(
                "legacy team member action timeout must be a positive number"
            )


class EvidenceTeamFormulator:
    """Asks the configured model to design an evidence-specific stored team."""

    _RESPONSE_SCHEMA: dict[str, object] = {
        "type": "object",
        "properties": {
            "members": {
                "type": "array",
                "minItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "stable_key": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                            "pattern": "^[a-z0-9][a-z0-9-]*$",
                        },
                        "role": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                        },
                        "coordinates": {"type": "boolean"},
                        "independent_verifier": {"type": "boolean"},
                        "responsibilities": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "permitted_tools": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {
                                "enum": [
                                    "read",
                                    "write",
                                    "run",
                                    "git_diff",
                                    "git_commit",
                                ]
                            },
                        },
                    },
                    "required": [
                        "stable_key",
                        "role",
                        "coordinates",
                        "independent_verifier",
                        "responsibilities",
                        "permitted_tools",
                    ],
                    "additionalProperties": False,
                },
            },
            "workflow": {
                "type": "object",
                "properties": {
                    "rationale": {"type": "string", "minLength": 1},
                    "assessment_prompt": {"type": "string", "minLength": 1},
                    "nodes": {
                        "type": "array",
                        "minItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "stable_key": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 64,
                                    "pattern": "^[a-z][a-z0-9-]*$",
                                },
                                "kind": {
                                    "enum": ["agent", "deterministic"],
                                },
                                "member_key": {"type": "string"},
                                "operation": {
                                    "enum": ["", "collect"],
                                },
                                "prompt": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                                "parameters": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                                "bindings": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                                "expected_output": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                                "resources": {
                                    "type": "array",
                                    "uniqueItems": True,
                                    "items": {
                                        "enum": [
                                            "checkout:read",
                                            "checkout:write",
                                            "diff:read",
                                            "issue:read",
                                            "validation:read",
                                            "workspace:read",
                                            "workspace:write",
                                        ]
                                    },
                                },
                            },
                            "required": [
                                "stable_key",
                                "kind",
                                "member_key",
                                "operation",
                                "prompt",
                                "parameters",
                                "bindings",
                                "expected_output",
                                "resources",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "edges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "target": {"type": "string"},
                            },
                            "required": ["source", "target"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "rationale",
                    "assessment_prompt",
                    "nodes",
                    "edges",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["members", "workflow"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        runtime: str = MINI_SWE_RUNTIME,
        model: str | None,
        action_timeout_seconds: float = DEFAULT_ACTION_TIMEOUT_SECONDS,
        model_resolver: Callable[[str], str] | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        configuration_resolver: (
            Callable[[], tuple[str, str | None, str | None]] | None
        ) = None,
        state_root: Path | None = None,
        timeout: float = 600,
    ) -> None:
        if configuration_resolver is None and (
            not isinstance(model, str) or not model.strip()
        ):
            raise ValueError("team formulator requires an explicit model")
        if isinstance(action_timeout_seconds, bool) or action_timeout_seconds <= 0:
            raise ValueError("team member action timeout must be positive")
        if timeout <= 0:
            raise ValueError("team formulation timeout must be positive")
        self.runtime = runtime
        self.model = model
        self.model_resolver = model_resolver
        self.base_url = base_url
        self.api_key = api_key
        self.configuration_resolver = configuration_resolver
        self.state_root = (
            Path(state_root).expanduser().resolve()
            if state_root is not None
            else (Path.cwd() / ".repogents-model-state" / "teams").resolve()
        )
        self.timeout = timeout
        self.action_timeout_seconds = action_timeout_seconds

    def formulate(self, inspection: InspectionEvidence) -> TeamDesign:
        prompt = _team_design_prompt(inspection)
        model = self.model
        base_url = self.base_url
        api_key = self.api_key
        if self.configuration_resolver is not None:
            model, base_url, api_key = self.configuration_resolver()
        if not isinstance(model, str) or not model.strip():
            raise RuntimeError("team formulator requires a configured model")
        inference = MiniSweInference(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=self.timeout,
        )
        for attempt in range(2):
            state_directory = (
                self.state_root
                / uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    prompt,
                ).hex
            )
            value = inference.infer(
                system_prompt=(
                    "Return exactly one JSON object matching the requested "
                    "schema and no prose."
                ),
                prompt=prompt,
                response_schema=self._RESPONSE_SCHEMA,
                state_directory=state_directory,
            )
            try:
                return self._design_from_value(value, inspection, model)
            except _InvalidTeamDesign as error:
                if attempt == 1:
                    raise
                prompt = _team_design_correction_prompt(prompt, error)
        raise AssertionError("unreachable team formulation attempt")

    def _design_from_value(
        self,
        value: object,
        inspection: InspectionEvidence,
        model: str,
    ) -> TeamDesign:
        try:
            if not isinstance(value, dict):
                raise ValueError("repository team design must be an object")
            raw_members = value.get("members")
            if not isinstance(raw_members, list):
                raise ValueError(
                    "repository team design must contain a members array"
                )
            instruction_text = _team_instruction_text(inspection)
            members: list[dict[str, object]] = []
            for value_member in raw_members:
                if not isinstance(value_member, dict):
                    raise ValueError(
                        "repository team member design must be an object"
                    )
                role = value_member.get("role")
                if not isinstance(role, str):
                    raise ValueError(
                        "repository team member role must be a string"
                    )
                stable_key = value_member.get("stable_key")
                responsibilities = value_member.get("responsibilities")
                tools = value_member.get("permitted_tools")
                coordinates = value_member.get("coordinates")
                independent_verifier = value_member.get("independent_verifier")
                if not isinstance(stable_key, str) or not stable_key.strip():
                    raise ValueError(
                        "repository team member stable_key must be nonempty"
                    )
                if (
                    not isinstance(responsibilities, str)
                    or not responsibilities.strip()
                ):
                    raise ValueError(
                        "repository team member responsibilities must be "
                        "nonempty"
                    )
                if not isinstance(tools, list):
                    raise ValueError(
                        "repository team member permitted_tools must be an "
                        "array"
                    )
                if not isinstance(coordinates, bool) or not isinstance(
                    independent_verifier,
                    bool,
                ):
                    raise ValueError(
                        "repository team coordination markers must be booleans"
                    )
                execution_class = _member_execution_class(
                    coordinates=coordinates,
                    independent_verifier=independent_verifier,
                    permitted_tools=tools,
                )
                members.append(
                    {
                        "stable_key": stable_key.strip(),
                        "role": role.strip(),
                        "execution_class": execution_class,
                        "coordinates": coordinates,
                        "independent_verifier": independent_verifier,
                        "responsibilities": responsibilities.strip(),
                        "permitted_tools": list(tools),
                        "runtime": self.runtime,
                        "model": model.strip(),
                        "instructions": instruction_text,
                        "action_timeout_seconds": self.action_timeout_seconds,
                    }
                )
            validate_team_members(members)
            workflow_value = value.get("workflow")
            if not isinstance(workflow_value, Mapping):
                raise ValueError(
                    "repository team design must contain a workflow object"
                )
            workflow = validate_workflow_design(
                workflow_value,
                members,
                DeterministicOperationRegistry.with_defaults().catalog(),
            )
        except ValueError as error:
            raise _InvalidTeamDesign(str(error)) from error
        resolved_members = tuple(
            dict(
                member,
                model=self._model_for_execution_class(
                    str(member["execution_class"]),
                    model,
                ),
            )
            for member in members
        )
        return TeamDesign(members=resolved_members, workflow=workflow)

    def _model_for_execution_class(
        self,
        execution_class: str,
        inferred_model: str,
    ) -> str:
        model = (
            self.model_resolver(execution_class)
            if self.model_resolver is not None
            else inferred_model
        )
        if not isinstance(model, str) or not model.strip():
            raise ValueError(
                f"repository team {execution_class} model must be nonempty"
            )
        return model.strip()


def _team_design_prompt(inspection: InspectionEvidence) -> str:
    task = (
        "Design the complete persistent development team and reusable "
        "workflow graph for this repository from the supplied evidence. "
        "Design repository-specific atomic role names: each member owns "
        "one bounded concern, and you choose how many members are "
        "warranted, their stable identities, role names, responsibilities, "
        "and controller-tool permissions. Do not choose from a "
        "controller-authored role taxonomy. Mark exactly one member as "
        "coordinates=true and give that member exactly one workflow node. "
        "Every other non-verifier node must reach that coordinating node, "
        "which must then reach the terminal independent-verifier node. Do "
        "not emit a separate pre-work coordinator node; issue-member "
        "selection is controller-owned. The coordinator integrates outputs "
        "and assesses team performance; it must not implement or verify "
        "repository changes. Mark exactly one different member as "
        "independent_verifier=true, "
        "and make its workflow node terminal. Keep implementation and "
        "independent verification in separate roles. Include actual "
        "development capacity even for a small repository. Design an "
        "acyclic node graph rather than a serial role list. Use fan-out "
        "for independent work, explicit join nodes for multi-input "
        "handoffs, and serialize only real dependencies or conflicting "
        "writes. Every agent node needs a node-specific objective, "
        "expected output schema, handoff bindings, and least-authority "
        "resources. Agent nodes must reference stored member stable keys. "
        "Every agent resource claim must match a tool held by its stored "
        "member through workflow_contract.resource_tool_requirements. "
        "Coordinators and independent verifiers must follow "
        "workflow_contract.role_resource_restrictions. "
        "Registered deterministic nodes may use only operations from "
        "workflow_contract.operations; never emit executable source, "
        "shell expressions, environment or secret values. The "
        "coordinator's assessment prompt must require evidence-based "
        "retain or adjust decisions for prompts, dependencies, joins, and "
        "specialist selection. This is a reusable repository team and "
        "orchestration template, not one issue's implementation plan. "
        "Choose member tools only from read, write, run, git_diff, and "
        "git_commit. Keep prompts concrete and bounded: name the "
        "repository evidence to inspect, the typed result to return, and "
        "the downstream handoff. The compiled issue graph includes only "
        "assigned issue members, the mandatory coordinator and verifier, "
        "and connecting deterministic nodes."
    )
    repository = {
        "languages": list(getattr(inspection, "languages", ())),
        "manifests": list(getattr(inspection, "manifests", ())),
        "lockfiles": list(getattr(inspection, "lockfiles", ())),
        "instruction_files": list(
            getattr(inspection, "instruction_files", ())
        ),
        "validation_commands": [
            list(command)
            for command in getattr(
                inspection, "validation_commands", ()
            )
        ],
        "file_count": inspection.file_count,
        "summary": _bounded_text(inspection.summary, 8_000),
        "source_files": _bounded_strings(
            getattr(inspection, "source_files", ()),
            _TEAM_SOURCE_FILE_LIMIT_BYTES,
        ),
        "instructions": _bounded_instructions(
            getattr(inspection, "instructions", ())
        ),
    }
    prompt = json.dumps(
        {
            "task": task,
            "repository": repository,
            "workflow_contract": {
                "contract_version": 1,
                "operations": (
                    DeterministicOperationRegistry.with_defaults().catalog()
                ),
                "resources": {
                    resource: {
                        "access": "exclusive"
                        if resource.endswith(":write")
                        else "shared"
                    }
                    for resource in (
                        "checkout:read",
                        "checkout:write",
                        "diff:read",
                        "issue:read",
                        "validation:read",
                        "workspace:read",
                        "workspace:write",
                    )
                },
                "resource_tool_requirements": (
                    workflow_resource_tool_requirements()
                ),
                "role_resource_restrictions": {
                    "coordinator": "no write resources",
                    "independent_verifier": "no write resources",
                },
                "topology": {
                    "all_other_nodes_reach_coordinator": True,
                    "coordinator_agent_node_count": 1,
                    "coordinator_reaches_verifier": True,
                    "independent_verifier_agent_node_count": 1,
                    "independent_verifier_terminal": True,
                },
                "scheduling": (
                    "dependency-ready nodes may run concurrently when their "
                    "declared resource claims do not conflict. Writes are "
                    "serialized only against overlapping reads or writes."
                ),
                "compilation": (
                    "The controller compiles assigned issue members plus the "
                    "mandatory coordinator and independent verifier into an "
                    "immutable exact issue-version graph."
                ),
                "safety": (
                    "Execution is controller-owned. Node prompts and "
                    "parameters contain no executable code or secrets; "
                    "member tools and node resource claims both authorize "
                    "every action."
                ),
                "controller_boundaries": [
                    "source graph",
                    "exact-SHA validation",
                    "independent acceptance",
                    "publication",
                    "feedback resolution",
                ],
            },
            "response_schema": {
                "members": [
                    {
                        "stable_key": "repository-specific atomic identity",
                        "role": "free repository-specific atomic role name",
                        "coordinates": "boolean",
                        "independent_verifier": "boolean",
                        "responsibilities": "one bounded responsibility",
                        "permitted_tools": [
                            "read|write|run|git_diff|git_commit"
                        ],
                    }
                ],
                "workflow": {
                    "rationale": "why this topology fits the repository",
                    "assessment_prompt": (
                        "how the coordinator assesses and adjusts"
                    ),
                    "nodes": [
                        {
                            "stable_key": "node identity",
                            "kind": "agent|deterministic",
                            "member_key": "stored member key or empty",
                            "operation": "collect or empty",
                            "prompt": "node-specific objective and handoff",
                            "parameters": {},
                            "bindings": {},
                            "expected_output": {"type": "object"},
                            "resources": ["workspace:read"],
                        }
                    ],
                    "edges": [{"source": "node", "target": "node"}],
                },
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(prompt.encode("utf-8")) > _TEAM_DESIGN_PROMPT_LIMIT_BYTES:
        raise RuntimeError(
            "fixed repository team-design context exceeds prompt limit"
        )
    return prompt


def _team_design_correction_prompt(prompt: str, error: ValueError) -> str:
    packet = json.loads(prompt)
    if not isinstance(packet, dict):
        raise AssertionError("team design prompt must be an object")
    packet["correction"] = {
        "attempt": 2,
        "rejected_reason": _bounded_text(str(error), 1_000),
        "instruction": (
            "The prior response was rejected by the controller. Return one "
            "complete replacement team and workflow matching the full "
            "contract. Do not patch the prior response, expand member "
            "permissions, or omit required work."
        ),
    }
    corrected = json.dumps(
        packet,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(corrected.encode("utf-8")) > _TEAM_DESIGN_PROMPT_LIMIT_BYTES:
        raise RuntimeError(
            "corrected repository team-design context exceeds prompt limit"
        )
    return corrected


def _team_instruction_text(inspection: InspectionEvidence) -> str:
    sections = [_bounded_text(inspection.summary, 8_000)]
    instructions = _bounded_instructions(getattr(inspection, "instructions", ()))
    if instructions:
        sections.append(
            "Repository instructions:\n"
            + "\n\n".join(
                f"--- {entry['path']} ---\n{entry['content']}" for entry in instructions
            )
        )
    return "\n\n".join(section for section in sections if section)


def _bounded_strings(values: Sequence[object], byte_limit: int) -> list[str]:
    retained: list[str] = []
    used = 2
    for raw in values:
        value = str(raw)
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        additional = len(encoded) + (1 if retained else 0)
        if used + additional > byte_limit:
            break
        retained.append(value)
        used += additional
    return retained


def _bounded_instructions(values: Sequence[object]) -> list[dict[str, str]]:
    retained: list[dict[str, str]] = []
    remaining = _TEAM_INSTRUCTION_LIMIT_BYTES
    for raw in values:
        if not isinstance(raw, (tuple, list)) or len(raw) != 2:
            continue
        path = str(raw[0])
        content = _bounded_text(str(raw[1]), remaining)
        entry = {"path": path, "content": content}
        size = len(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if size > remaining:
            break
        retained.append(entry)
        remaining -= size
        if remaining <= 0:
            break
    return retained


def _bounded_text(value: str, byte_limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    return encoded[:byte_limit].decode("utf-8", "ignore")


class TeamService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def load(self, team_version_id: str) -> StoredTeam:
        with self.database.connect() as connection:
            team_row = connection.execute(
                "SELECT * FROM team_versions WHERE id=?", (team_version_id,)
            ).fetchone()
            if team_row is None:
                raise KeyError(team_version_id)
            member_rows = connection.execute(
                """SELECT * FROM team_members WHERE team_version_id=?
                   ORDER BY CASE role WHEN 'lead' THEN 0 WHEN 'scout' THEN 1
                                      WHEN 'implementer' THEN 2 ELSE 3 END,
                            stable_key""",
                (team_version_id,),
            ).fetchall()
        members = tuple(self._member(row) for row in member_rows)
        member_values = [self._member_dict(member) for member in members]
        if int(team_row["design_contract_version"]) >= 2:
            validate_team_members(member_values)
        else:
            _validate_legacy_team_members(member_values)
        return StoredTeam(
            id=str(team_row["id"]),
            repository_id=str(team_row["repository_id"]),
            version=int(team_row["version"]),
            evidence=json.loads(team_row["evidence_json"]),
            members=members,
        )

    def assign(
        self,
        run_id: str,
        stable_keys: Sequence[str],
        reasoning: str,
    ) -> tuple[Assignment, ...]:
        return self._assign(
            run_id,
            stable_keys,
            reasoning,
            require_expansion=False,
        )

    def expand_assignment(
        self,
        run_id: str,
        stable_keys: Sequence[str],
        reasoning: str,
    ) -> tuple[Assignment, ...]:
        return self._assign(
            run_id,
            stable_keys,
            reasoning,
            require_expansion=True,
        )

    def _assign(
        self,
        run_id: str,
        stable_keys: Sequence[str],
        reasoning: str,
        *,
        require_expansion: bool,
    ) -> tuple[Assignment, ...]:
        if not reasoning.strip():
            raise ValueError("assignment reasoning is required")
        if not stable_keys or len(stable_keys) != len(set(stable_keys)):
            raise ValueError("assignment member keys must be nonempty and unique")
        with self.database.connect() as connection:
            run = connection.execute(
                "SELECT team_version_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if run is None:
            raise KeyError(run_id)
        team = self.load(str(run["team_version_id"]))
        by_key = {member.stable_key: member for member in team.members}
        requested_keys = set(stable_keys)
        unknown = requested_keys - set(by_key)
        if unknown:
            raise ValueError(
                f"assignment contains members outside the stored team: {','.join(sorted(unknown))}"
            )
        lead_key = next(
            member.stable_key for member in team.members if member.coordinates
        )
        if lead_key not in requested_keys:
            raise ValueError("every issue assignment must include the stored lead")
        verifier_key = next(
            member.stable_key
            for member in team.members
            if member.independent_verifier
        )
        if verifier_key not in requested_keys:
            raise ValueError(
                "every issue assignment must include the stored independent verifier"
            )
        now = _utc_now()
        with self.database.transaction() as connection:
            existing_rows = connection.execute(
                """SELECT team_members.stable_key
                   FROM agent_assignments
                   JOIN team_members
                     ON team_members.id=agent_assignments.team_member_id
                   WHERE agent_assignments.run_id=?""",
                (run_id,),
            ).fetchall()
            existing_keys = {str(row["stable_key"]) for row in existing_rows}
            if require_expansion:
                if not existing_keys:
                    raise ValueError(
                        "assignment expansion requires an existing assignment"
                    )
                if not existing_keys.issubset(requested_keys):
                    raise ValueError(
                        "expanded assignment must retain every currently assigned member"
                    )
                if requested_keys == existing_keys:
                    raise ValueError(
                        "expanded assignment must add at least one previously unassigned member"
                    )
            for key in stable_keys:
                connection.execute(
                    """INSERT OR IGNORE INTO agent_assignments
                       (id, run_id, team_member_id, reasoning, assigned_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), run_id, by_key[key].id, reasoning, now),
                )
        return self.assignments_for_run(run_id)

    def assignments_for_run(self, run_id: str) -> tuple[Assignment, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT agent_assignments.id AS assignment_id,
                          agent_assignments.run_id,
                          agent_assignments.reasoning,
                          agent_assignments.assigned_at,
                          team_members.*
                   FROM agent_assignments
                   JOIN team_members ON team_members.id = agent_assignments.team_member_id
                   WHERE agent_assignments.run_id=?
                   ORDER BY CASE team_members.role WHEN 'lead' THEN 0 WHEN 'scout' THEN 1
                                                   WHEN 'implementer' THEN 2 ELSE 3 END,
                            team_members.stable_key""",
                (run_id,),
            ).fetchall()
        return tuple(
            Assignment(
                id=str(row["assignment_id"]),
                run_id=str(row["run_id"]),
                member=self._member(row),
                reasoning=str(row["reasoning"]),
                assigned_at=str(row["assigned_at"]),
            )
            for row in rows
        )

    @staticmethod
    def _member(row: object) -> TeamMember:
        execution_class = str(row["role"])  # type: ignore[index]
        atomic_role = str(row["atomic_role"]) or execution_class  # type: ignore[index]
        return TeamMember(
            id=str(row["id"]),  # type: ignore[index]
            stable_key=str(row["stable_key"]),  # type: ignore[index]
            role=atomic_role,
            execution_class=execution_class,
            coordinates=execution_class == "lead",
            independent_verifier=execution_class == "verifier",
            responsibilities=str(row["responsibilities"]),  # type: ignore[index]
            permitted_tools=tuple(json.loads(row["permitted_tools_json"])),  # type: ignore[index]
            runtime=str(row["runtime"]),  # type: ignore[index]
            model=str(row["model"]),  # type: ignore[index]
            instructions=str(row["instructions"]),  # type: ignore[index]
            action_timeout_seconds=float(row["action_timeout_seconds"]),  # type: ignore[index]
        )

    @staticmethod
    def _member_dict(member: TeamMember) -> dict[str, object]:
        return {
            "stable_key": member.stable_key,
            "role": member.role,
            "execution_class": member.execution_class,
            "coordinates": member.coordinates,
            "independent_verifier": member.independent_verifier,
            "responsibilities": member.responsibilities,
            "permitted_tools": list(member.permitted_tools),
            "runtime": member.runtime,
            "model": member.model,
            "instructions": member.instructions,
            "action_timeout_seconds": member.action_timeout_seconds,
        }


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
