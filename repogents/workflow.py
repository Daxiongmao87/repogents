from __future__ import annotations

import json
import hashlib
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .database import Database
from .sandbox import redact_text

_STABLE_KEY = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_FORBIDDEN_CONFIGURATION_KEY = re.compile(
    r"(?:^|_)(?:argv|code|command|credential|executable|password|program|"
    r"script|secret|shell|token)(?:_|$)",
    re.IGNORECASE,
)
_SHELL_EXPRESSION = re.compile(
    r"(?:\$\([^)\n]*\)|`[^`\n]+`|(?:^|\s)(?:&&|\|\|)(?:\s|$)|"
    r"[<>]\([^)]+\))"
)
_RESOURCE_TOOL_REQUIREMENTS = {
    "checkout:read": "read",
    "checkout:write": "write",
    "diff:read": "git_diff",
    "issue:read": "read",
    "validation:read": "run",
    "workspace:read": "read",
    "workspace:write": "write",
}
_ALLOWED_RESOURCES = frozenset(_RESOURCE_TOOL_REQUIREMENTS)
_TERMINAL_NODE_STATES = frozenset({"succeeded", "skipped", "canceled"})
_PROMPT_MAX_BYTES = 8_000
_RATIONALE_MAX_BYTES = 4_000


def workflow_resource_tool_requirements() -> dict[str, str]:
    """Return the agent resource-to-stored-tool compatibility contract."""
    return dict(_RESOURCE_TOOL_REQUIREMENTS)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_id(*parts: object) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, ":".join(str(part) for part in parts))
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_json(value: object, default: object) -> object:
    if not isinstance(value, str) or not value:
        return default
    return json.loads(value)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return list(value)


def _collect_values(values: dict[str, object]) -> dict[str, object]:
    items = _list(values.get("items"), "collect items")
    return {
        "items": items,
        "summary": "\n".join(str(item) for item in items),
    }


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def _bounded_string(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> str:
    normalized = _string(value, label)
    size = len(normalized.encode("utf-8"))
    if size < minimum:
        raise ValueError(f"{label} is too generic")
    if size > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes")
    return normalized


def _resource_identity(resource: str) -> tuple[str, str]:
    namespace, access = resource.rsplit(":", 1)
    if namespace in {"checkout", "workspace", "diff", "validation"}:
        namespace = "workspace"
    return namespace, access


def _validate_safe_value(
    value: object,
    *,
    path: str,
    reject_shell: bool = False,
) -> None:
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError(f"{path} contains an invalid null byte")
        if "secret://" in value.lower():
            raise ValueError(
                f"{path} cannot contain secret references or values"
            )
        if reject_shell and _SHELL_EXPRESSION.search(value):
            raise ValueError(f"{path} cannot contain a shell expression")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_safe_value(
                item,
                path=f"{path}[{index}]",
                reject_shell=reject_shell,
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            if _FORBIDDEN_CONFIGURATION_KEY.search(key):
                raise ValueError(
                    f"{path}.{key} cannot contain executable or secret "
                    "configuration"
                )
            _validate_safe_value(
                item,
                path=f"{path}.{key}",
                reject_shell=reject_shell,
            )
        return
    raise ValueError(f"{path} is not JSON-compatible")


def _validate_schema(
    value: object, schema: Mapping[str, object], label: str
) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        expected_types = tuple(str(item) for item in expected)
    elif isinstance(expected, str):
        expected_types = (expected,)
    elif expected is None:
        expected_types = ()
    else:
        raise ValueError(f"{label} schema type is invalid")

    matches = not expected_types
    for item in expected_types:
        if item == "object" and isinstance(value, Mapping):
            matches = True
        elif item == "array" and isinstance(value, list):
            matches = True
        elif item == "string" and isinstance(value, str):
            matches = True
        elif (
            item == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            matches = True
        elif (
            item == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            matches = True
        elif item == "boolean" and isinstance(value, bool):
            matches = True
        elif item == "null" and value is None:
            matches = True
    if not matches:
        expected_label = " or ".join(expected_types) or "declared type"
        raise ValueError(f"{label} must be {expected_label}")

    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or value not in choices:
            raise ValueError(f"{label} is not one of the allowed values")

    if isinstance(value, Mapping):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise ValueError(f"{label} schema required list is invalid")
        missing = [item for item in required if item not in value]
        if missing:
            raise ValueError(f"{label} is missing required field {missing[0]}")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError(f"{label} schema properties are invalid")
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                raise ValueError(
                    f"{label} contains unsupported field {sorted(extras)[0]}"
                )
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, Mapping):
                _validate_schema(item, child, f"{label}.{key}")
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, f"{label}[{index}]")
    elif isinstance(value, str):
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{label} is shorter than {minimum}")


def _normalize_schema(value: object, label: str) -> dict[str, object]:
    schema = _mapping(value, label)
    schema_type = schema.get("type")
    if schema_type is not None and not isinstance(schema_type, (str, list)):
        raise ValueError(f"{label} type is invalid")
    _validate_safe_value(schema, path=label)
    return schema


@dataclass(frozen=True, init=False)
class DeterministicOperation:
    name: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    resources: tuple[str, ...]
    pure: bool
    version: str
    handler: Callable[[dict[str, object]], object]

    def __init__(
        self,
        *,
        name: str | None = None,
        key: str | None = None,
        input_schema: Mapping[str, object],
        output_schema: Mapping[str, object],
        handler: Callable[[dict[str, object]], object],
        resources: Sequence[str] = (),
        pure: bool = True,
        version: str = "1",
    ) -> None:
        resolved = name if name is not None else key
        object.__setattr__(self, "name", _string(resolved, "operation name"))
        object.__setattr__(
            self,
            "input_schema",
            _normalize_schema(input_schema, "operation input schema"),
        )
        object.__setattr__(
            self,
            "output_schema",
            _normalize_schema(output_schema, "operation output schema"),
        )
        object.__setattr__(self, "handler", handler)
        object.__setattr__(
            self, "resources", tuple(str(item) for item in resources)
        )
        object.__setattr__(self, "pure", bool(pure))
        object.__setattr__(
            self, "version", _string(version, "operation version")
        )

    @property
    def key(self) -> str:
        return self.name

    @property
    def version_hash(self) -> str:
        payload = {
            "name": self.name,
            "version": self.version,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "resources": list(self.resources),
            "pure": self.pure,
        }
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


class DeterministicOperationRegistry:
    def __init__(self) -> None:
        self._operations: dict[str, DeterministicOperation] = {}
        self._lock = threading.Lock()

    @classmethod
    def with_defaults(cls) -> DeterministicOperationRegistry:
        registry = cls()
        registry.register(
            DeterministicOperation(
                name="collect",
                input_schema={
                    "type": "object",
                    "properties": {"items": {"type": "array"}},
                    "required": ["items"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "items": {"type": "array"},
                        "summary": {"type": "string"},
                    },
                    "required": ["items", "summary"],
                    "additionalProperties": False,
                },
                resources=(),
                pure=True,
                handler=_collect_values,
            )
        )
        return registry

    def register(self, operation: DeterministicOperation) -> None:
        if not _STABLE_KEY.fullmatch(operation.name):
            raise ValueError(
                "deterministic operation name must be a stable key"
            )
        if not operation.pure:
            raise ValueError(
                "registered deterministic operations must be pure"
            )
        for resource in operation.resources:
            if resource not in _ALLOWED_RESOURCES:
                raise ValueError(
                    f"unsupported deterministic operation resource {resource}"
                )
        with self._lock:
            if operation.name in self._operations:
                raise ValueError(
                    f"deterministic operation {operation.name} is registered"
                )
            self._operations[operation.name] = operation

    def catalog(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {
                name: {
                    "input_schema": operation.input_schema,
                    "output_schema": operation.output_schema,
                    "resources": list(operation.resources),
                    "pure": operation.pure,
                    "version_hash": operation.version_hash,
                }
                for name, operation in sorted(self._operations.items())
            }

    def get(self, name: str) -> DeterministicOperation:
        with self._lock:
            operation = self._operations.get(name)
        if operation is None:
            raise ValueError(
                f"deterministic operation {name!r} is not registered"
            )
        return operation

    def execute(
        self, name: str, values: Mapping[str, object]
    ) -> dict[str, object]:
        operation = self.get(name)
        payload = dict(values)
        _validate_schema(payload, operation.input_schema, f"{name} input")
        result = operation.handler(payload)
        if not isinstance(result, Mapping):
            raise ValueError(f"{name} output must be an object")
        normalized = {str(key): value for key, value in result.items()}
        _validate_schema(normalized, operation.output_schema, f"{name} output")
        _validate_safe_value(normalized, path=f"{name} output")
        return normalized


@dataclass(frozen=True)
class WorkflowEdge:
    source: str
    target: str


@dataclass(frozen=True)
class WorkflowNode:
    id: str
    stable_key: str
    kind: str
    member_id: str | None
    member_key: str | None
    role: str | None
    execution_class: str | None
    operation: str | None
    operation_version: str | None
    prompt: str
    parameters: dict[str, object]
    bindings: dict[str, object]
    expected_output: dict[str, object]
    resources: tuple[str, ...]
    position: int
    state: str = "pending"
    output: dict[str, object] | None = None
    error: dict[str, object] | None = None
    reused_from_node_id: str | None = None
    resource_wait_count: int = 0


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    team_version_id: str
    rationale: str
    assessment_prompt: str
    nodes: tuple[WorkflowNode, ...]
    edges: tuple[WorkflowEdge, ...]


@dataclass(frozen=True)
class RunWorkflowGraph:
    id: str
    run_id: str
    template_id: str | None
    generation: int
    issue_version_id: str | None
    team_version_id: str
    sandbox_version_id: str
    base_sha: str
    state: str
    reason: str
    active: bool
    rationale: str
    assessment_prompt: str
    nodes: tuple[WorkflowNode, ...]
    edges: tuple[WorkflowEdge, ...]
    assessments: tuple[dict[str, object], ...] = ()
    assessment: dict[str, object] | None = None


@dataclass(frozen=True)
class WorkflowNodeContext:
    run_id: str
    workflow_id: str
    generation: int
    node_id: str
    stable_key: str
    member_id: str | None
    member_key: str | None
    role: str | None
    prompt: str
    parameters: dict[str, object]
    bindings: dict[str, object]
    expected_output: dict[str, object]
    inputs: dict[str, object]
    resources: tuple[str, ...]
    dependency_outputs: dict[str, dict[str, object]]


@dataclass(frozen=True)
class WorkflowExecutionResult:
    run_id: str
    workflow_id: str
    generation: int
    state: str


class WorkflowExecutionError(RuntimeError):
    pass


class WorkflowCanceled(RuntimeError):
    pass


def _topological_order(
    node_keys: Sequence[str], edges: Sequence[Mapping[str, str] | WorkflowEdge]
) -> list[str]:
    order_index = {key: index for index, key in enumerate(node_keys)}
    incoming = {key: 0 for key in node_keys}
    outgoing: dict[str, list[str]] = {key: [] for key in node_keys}
    for edge in edges:
        source = (
            edge.source if isinstance(edge, WorkflowEdge) else edge["source"]
        )
        target = (
            edge.target if isinstance(edge, WorkflowEdge) else edge["target"]
        )
        outgoing[source].append(target)
        incoming[target] += 1
    ready = sorted(
        (key for key, count in incoming.items() if count == 0),
        key=order_index.__getitem__,
    )
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for target in sorted(outgoing[current], key=order_index.__getitem__):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort(key=order_index.__getitem__)
    if len(ordered) != len(node_keys):
        raise ValueError("workflow graph must be acyclic")
    return ordered


def _layout(
    nodes: Sequence[WorkflowNode], edges: Sequence[WorkflowEdge]
) -> dict[str, tuple[int, int]]:
    keys = [node.stable_key for node in nodes]
    predecessors: dict[str, list[str]] = {key: [] for key in keys}
    for edge in edges:
        predecessors[edge.target].append(edge.source)
    columns: dict[str, int] = {}
    for key in _topological_order(keys, edges):
        columns[key] = max(
            (columns[source] + 1 for source in predecessors[key]), default=0
        )
    rows: dict[int, int] = {}
    result: dict[str, tuple[int, int]] = {}
    for node in nodes:
        column = columns[node.stable_key]
        row = rows.get(column, 0)
        rows[column] = row + 1
        result[node.stable_key] = (column, row)
    return result


def validate_workflow_design(
    design: Mapping[str, object],
    members: Sequence[Mapping[str, object]],
    operation_catalog: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload = _mapping(design, "workflow design")
    rationale = _bounded_string(
        payload.get("rationale"),
        "workflow rationale",
        minimum=32,
        maximum=_RATIONALE_MAX_BYTES,
    )
    _validate_safe_value(
        rationale,
        path="workflow rationale",
        reject_shell=True,
    )
    assessment_prompt = payload.get("assessment_prompt")
    if assessment_prompt is None:
        assessment_prompt = (
            "Assess completed member outputs for evidence quality, "
            "handoff quality, and avoidable workflow bottlenecks. Retain "
            "the graph unless a concrete revision is justified."
        )
    assessment_prompt = _bounded_string(
        assessment_prompt,
        "workflow assessment prompt",
        minimum=32,
        maximum=_PROMPT_MAX_BYTES,
    )
    _validate_safe_value(
        assessment_prompt,
        path="workflow assessment prompt",
        reject_shell=True,
    )
    member_by_key = {
        _string(member.get("stable_key"), "member stable key"): dict(member)
        for member in members
    }
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("workflow nodes must be a nonempty list")
    normalized_nodes: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, raw_node in enumerate(raw_nodes):
        node = _mapping(raw_node, f"workflow node {index}")
        stable_key = _string(
            node.get("stable_key"), "workflow node stable key"
        )
        if not _STABLE_KEY.fullmatch(stable_key):
            raise ValueError("workflow node stable key is invalid")
        if stable_key in seen:
            raise ValueError(
                f"duplicate workflow node stable key {stable_key}"
            )
        seen.add(stable_key)
        kind = _string(node.get("kind"), f"workflow node {stable_key} kind")
        if kind not in {"agent", "deterministic"}:
            raise ValueError(f"workflow node {stable_key} kind is unsupported")
        member_key_raw = node.get("member_key", "")
        operation_raw = node.get("operation", "")
        member_key = (
            member_key_raw.strip() if isinstance(member_key_raw, str) else ""
        )
        operation = (
            operation_raw.strip() if isinstance(operation_raw, str) else ""
        )
        operation_spec: Mapping[str, object] | None = None
        operation_version = ""
        if kind == "agent":
            if member_key not in member_by_key:
                raise ValueError(
                    f"workflow agent node {stable_key} must reference "
                    "one stored member"
                )
            if operation:
                raise ValueError(
                    f"workflow agent node {stable_key} cannot select "
                    "an operation"
                )
        else:
            if member_key:
                raise ValueError(
                    f"deterministic workflow node {stable_key} cannot "
                    "reference a member"
                )
            if not operation or not _STABLE_KEY.fullmatch(operation):
                raise ValueError(
                    f"workflow node {stable_key} requires a registered "
                    "deterministic operation"
                )
            raw_spec = (
                operation_catalog.get(operation)
                if operation_catalog is not None
                else None
            )
            if not isinstance(raw_spec, Mapping):
                raise ValueError(
                    f"workflow node {stable_key} requires a registered "
                    "deterministic operation"
                )
            operation_spec = raw_spec
            operation_version = _string(
                operation_spec.get("version_hash"),
                f"workflow node {stable_key} operation version",
            )
        prompt = _bounded_string(
            node.get("prompt"),
            f"workflow node {stable_key} prompt",
            minimum=16,
            maximum=_PROMPT_MAX_BYTES,
        )
        _validate_safe_value(
            prompt,
            path=f"workflow node {stable_key} prompt",
            reject_shell=True,
        )
        parameters = _mapping(
            node.get("parameters", {}), f"{stable_key} parameters"
        )
        bindings = _mapping(node.get("bindings", {}), f"{stable_key} bindings")
        _validate_safe_value(
            parameters,
            path=f"{stable_key} parameters",
            reject_shell=True,
        )
        _validate_safe_value(
            bindings,
            path=f"{stable_key} bindings",
            reject_shell=True,
        )
        raw_resources = node.get("resources", [])
        if not isinstance(raw_resources, list) or not all(
            isinstance(item, str) for item in raw_resources
        ):
            raise ValueError(
                f"workflow node {stable_key} resources must be a list"
            )
        resources = [str(item) for item in raw_resources]
        if len(resources) != len(set(resources)):
            raise ValueError(
                f"workflow node {stable_key} resource is duplicated"
            )
        unsupported = sorted(set(resources) - _ALLOWED_RESOURCES)
        if unsupported:
            raise ValueError(
                f"workflow node {stable_key} resource {unsupported[0]} "
                "is unsupported"
            )
        if kind == "deterministic" and operation_spec is not None:
            declared_resources = operation_spec.get("resources", [])
            if not isinstance(declared_resources, list) or resources != [
                str(item) for item in declared_resources
            ]:
                raise ValueError(
                    f"workflow node {stable_key} resources must match "
                    "its registered operation"
                )
        if kind == "agent":
            member = member_by_key[member_key]
            execution_class = str(member.get("execution_class", ""))
            if execution_class == "lead" and any(
                resource.endswith(":write") for resource in resources
            ):
                raise ValueError(
                    "workflow coordinating member cannot claim write resources"
                )
            if execution_class == "verifier" and any(
                resource.endswith(":write") for resource in resources
            ):
                raise ValueError(
                    "workflow verifier cannot claim write resources"
                )
            permitted = {
                str(item)
                for item in _list(
                    member.get("permitted_tools", []),
                    "stored member permitted tools",
                )
            }
            for resource in resources:
                required_tool = _RESOURCE_TOOL_REQUIREMENTS[resource]
                if required_tool not in permitted:
                    raise ValueError(
                        f"workflow node {stable_key} resource expands "
                        "stored member permissions"
                    )
        raw_expected = node.get("expected_output")
        if raw_expected is None and operation_spec is not None:
            raw_expected = operation_spec.get("output_schema")
        if raw_expected is None:
            raw_expected = {"type": "object", "additionalProperties": True}
        expected_output = _normalize_schema(
            raw_expected, f"workflow node {stable_key} expected output"
        )
        if expected_output.get("type") != "object":
            raise ValueError(
                f"workflow node {stable_key} expected output must be an object"
            )
        if operation_spec is not None:
            registered_output = _mapping(
                operation_spec.get("output_schema"),
                f"workflow node {stable_key} registered output",
            )
            if expected_output != registered_output:
                raise ValueError(
                    f"workflow node {stable_key} output must match its "
                    "registered operation"
                )
        normalized_nodes.append(
            {
                "stable_key": stable_key,
                "kind": kind,
                "member_key": member_key,
                "operation": operation,
                "operation_version": operation_version,
                "prompt": prompt,
                "parameters": parameters,
                "bindings": bindings,
                "expected_output": expected_output,
                "resources": resources,
            }
        )

    raw_edges = payload.get("edges")
    if not isinstance(raw_edges, list):
        raise ValueError("workflow edges must be a list")
    normalized_edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for index, raw_edge in enumerate(raw_edges):
        edge = _mapping(raw_edge, f"workflow edge {index}")
        source = _string(edge.get("source"), "workflow edge source")
        target = _string(edge.get("target"), "workflow edge target")
        if source not in seen or target not in seen:
            raise ValueError("workflow edge must reference stored nodes")
        if source == target:
            raise ValueError("workflow graph must be acyclic")
        pair = (source, target)
        if pair in seen_edges:
            raise ValueError(f"duplicate workflow edge {source} -> {target}")
        seen_edges.add(pair)
        normalized_edges.append({"source": source, "target": target})

    node_keys = [str(node["stable_key"]) for node in normalized_nodes]
    predecessors = {key: set() for key in node_keys}
    outgoing = {key: set() for key in node_keys}
    for edge in normalized_edges:
        predecessors[edge["target"]].add(edge["source"])
        outgoing[edge["source"]].add(edge["target"])

    lead_nodes = [
        node
        for node in normalized_nodes
        if node["kind"] == "agent"
        and member_by_key[str(node["member_key"])].get("execution_class")
        == "lead"
    ]
    verifier_nodes = [
        node
        for node in normalized_nodes
        if node["kind"] == "agent"
        and member_by_key[str(node["member_key"])].get("execution_class")
        == "verifier"
    ]
    if len(lead_nodes) != 1:
        raise ValueError(
            "workflow must include exactly one coordinating member node"
        )
    if len(verifier_nodes) != 1:
        raise ValueError(
            "workflow must include exactly one independent verifier node"
        )
    lead_key = str(lead_nodes[0]["stable_key"])
    verifier_key = str(verifier_nodes[0]["stable_key"])
    lead_resources = _list(
        lead_nodes[0]["resources"], "coordinating node resources"
    )
    verifier_resources = _list(
        verifier_nodes[0]["resources"], "verifier node resources"
    )
    if any(str(resource).endswith(":write") for resource in lead_resources):
        raise ValueError(
            "workflow coordinating member cannot claim write resources"
        )
    if any(
        str(resource).endswith(":write") for resource in verifier_resources
    ):
        raise ValueError("workflow verifier cannot claim write resources")
    if outgoing[verifier_key]:
        raise ValueError("workflow verifier node must be terminal")
    _topological_order(node_keys, normalized_edges)

    def reaches(source: str, target: str) -> bool:
        pending = [source]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(outgoing[current] - visited)
        return False

    if not reaches(lead_key, verifier_key):
        raise ValueError(
            "workflow coordinating member must reach the independent verifier"
        )
    for key in node_keys:
        if key == verifier_key:
            continue
        if not reaches(key, verifier_key):
            raise ValueError(
                f"workflow node {key} must reach the independent verifier"
            )
        if key == lead_key:
            continue
        if not reaches(key, lead_key):
            raise ValueError(
                f"workflow node {key} must reach the coordinating member "
                "before verification"
            )
    for node in normalized_nodes:
        stable_key = str(node["stable_key"])
        bindings = _mapping(node["bindings"], f"{stable_key} bindings")
        for name, raw_binding in bindings.items():
            if not isinstance(raw_binding, Mapping):
                continue
            if isinstance(raw_binding.get("node"), str):
                source = str(raw_binding["node"])
                if source not in predecessors[stable_key]:
                    raise ValueError(
                        f"workflow node {stable_key} binding {name} "
                        "must reference a direct predecessor"
                    )
            if isinstance(raw_binding.get("nodes"), list):
                sources = [str(item) for item in raw_binding["nodes"]]
                if not sources or len(sources) != len(set(sources)):
                    raise ValueError(
                        f"workflow node {stable_key} binding {name} nodes "
                        "are invalid"
                    )
                if not set(sources).issubset(predecessors[stable_key]):
                    raise ValueError(
                        f"workflow node {stable_key} binding {name} "
                        "must reference direct predecessors"
                    )
    return {
        "rationale": rationale,
        "assessment_prompt": assessment_prompt,
        "nodes": normalized_nodes,
        "edges": normalized_edges,
    }


class WorkflowService:
    def __init__(
        self,
        database: Database,
        *,
        registry: DeterministicOperationRegistry | None = None,
    ) -> None:
        self.database = database
        self.registry = (
            registry or DeterministicOperationRegistry.with_defaults()
        )

    @staticmethod
    def _members_from_connection(
        connection: Any,
        team_version_id: str,
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            """SELECT id, stable_key, role AS execution_class,
                      atomic_role AS role, responsibilities,
                      permitted_tools_json, runtime, model, instructions,
                      action_timeout_seconds
                 FROM team_members
                WHERE team_version_id=?
                ORDER BY rowid""",
            (team_version_id,),
        ).fetchall()
        if not rows:
            raise ValueError(f"team version {team_version_id} has no members")
        return [
            {
                "id": str(row["id"]),
                "stable_key": str(row["stable_key"]),
                "execution_class": str(row["execution_class"]),
                "role": str(row["role"]),
                "responsibilities": str(row["responsibilities"]),
                "permitted_tools": _load_json(row["permitted_tools_json"], []),
                "runtime": str(row["runtime"]),
                "model": str(row["model"]),
                "instructions": str(row["instructions"]),
                "action_timeout_seconds": int(row["action_timeout_seconds"]),
            }
            for row in rows
        ]

    def _members(self, team_version_id: str) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            return self._members_from_connection(connection, team_version_id)

    def store_template(
        self, team_version_id: str, design: Mapping[str, object]
    ) -> WorkflowTemplate:
        members = self._members(team_version_id)
        normalized = validate_workflow_design(
            design, members, self.registry.catalog()
        )
        with self.database.transaction() as connection:
            self._insert_template(
                connection,
                team_version_id=team_version_id,
                members=members,
                normalized=normalized,
            )
        return self.load_template(team_version_id)

    def store_template_in_transaction(
        self,
        connection: Any,
        team_version_id: str,
        design: Mapping[str, object],
    ) -> str:
        members = self._members_from_connection(connection, team_version_id)
        normalized = validate_workflow_design(
            design, members, self.registry.catalog()
        )
        return self._insert_template(
            connection,
            team_version_id=team_version_id,
            members=members,
            normalized=normalized,
        )

    @staticmethod
    def _insert_template(
        connection: Any,
        *,
        team_version_id: str,
        members: Sequence[Mapping[str, object]],
        normalized: Mapping[str, object],
    ) -> str:
        template_id = _stable_id(team_version_id, "workflow-template", 1)
        now = _utc_now()
        member_ids = {
            str(member["stable_key"]): str(member["id"]) for member in members
        }
        raw_nodes = normalized["nodes"]
        raw_edges = normalized["edges"]
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise ValueError("normalized workflow graph is invalid")
        node_ids = {
            str(node["stable_key"]): _stable_id(
                template_id, "node", node["stable_key"]
            )
            for node in raw_nodes
        }
        if (
            connection.execute(
                """
                SELECT 1
                  FROM team_workflow_templates
                 WHERE team_version_id=?
                """,
                (team_version_id,),
            ).fetchone()
            is not None
        ):
            raise ValueError("team version already has a workflow template")
        connection.execute(
            """INSERT INTO team_workflow_templates
               (id, team_version_id, contract_version, rationale,
                assessment_prompt, created_at)
               VALUES (?, ?, 1, ?, ?, ?)""",
            (
                template_id,
                team_version_id,
                normalized["rationale"],
                normalized["assessment_prompt"],
                now,
            ),
        )
        for position, raw_node in enumerate(raw_nodes):
            node = dict(raw_node)
            stable_key = str(node["stable_key"])
            kind = str(node["kind"])
            connection.execute(
                """INSERT INTO team_workflow_nodes
                   (id, template_id, stable_key, kind, team_member_id,
                    operation_key, operation_version, prompt,
                    parameters_json, bindings_json, expected_output_json,
                    resources_json, position, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    node_ids[stable_key],
                    template_id,
                    stable_key,
                    kind,
                    (
                        member_ids[str(node["member_key"])]
                        if kind == "agent"
                        else None
                    ),
                    (
                        str(node["operation"])
                        if kind == "deterministic"
                        else None
                    ),
                    (
                        str(node["operation_version"])
                        if kind == "deterministic"
                        else None
                    ),
                    node["prompt"],
                    _json(node["parameters"]),
                    _json(node["bindings"]),
                    _json(node["expected_output"]),
                    _json(node["resources"]),
                    position,
                    now,
                ),
            )
        for position, raw_edge in enumerate(raw_edges):
            edge = dict(raw_edge)
            source = str(edge["source"])
            target = str(edge["target"])
            connection.execute(
                """INSERT INTO team_workflow_edges
                   (id, template_id, source_node_id, target_node_id,
                    position, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    _stable_id(template_id, "edge", source, target),
                    template_id,
                    node_ids[source],
                    node_ids[target],
                    position,
                    now,
                ),
            )
        return template_id

    def load_template(self, team_version_id: str) -> WorkflowTemplate:
        with self.database.connect() as connection:
            template = connection.execute(
                """SELECT id, team_version_id, rationale, assessment_prompt
                     FROM team_workflow_templates
                    WHERE team_version_id=?""",
                (team_version_id,),
            ).fetchone()
            if template is None:
                raise KeyError(team_version_id)
            node_rows = connection.execute(
                """
                SELECT workflow_nodes.*,
                       team_members.stable_key AS member_key,
                       team_members.atomic_role AS member_role,
                       team_members.role AS execution_class
                  FROM team_workflow_nodes AS workflow_nodes
                  LEFT JOIN team_members
                    ON team_members.id=workflow_nodes.team_member_id
                 WHERE workflow_nodes.template_id=?
                 ORDER BY workflow_nodes.position
                """,
                (template["id"],),
            ).fetchall()
            edge_rows = connection.execute(
                """SELECT source.stable_key AS source_key,
                          target.stable_key AS target_key
                     FROM team_workflow_edges AS edges
                     JOIN team_workflow_nodes AS source
                       ON source.id=edges.source_node_id
                     JOIN team_workflow_nodes AS target
                       ON target.id=edges.target_node_id
                    WHERE edges.template_id=?
                    ORDER BY edges.position""",
                (template["id"],),
            ).fetchall()
        nodes = tuple(self._node_from_row(row) for row in node_rows)
        edges = tuple(
            WorkflowEdge(str(row["source_key"]), str(row["target_key"]))
            for row in edge_rows
        )
        return WorkflowTemplate(
            id=str(template["id"]),
            team_version_id=str(template["team_version_id"]),
            rationale=str(template["rationale"]),
            assessment_prompt=str(template["assessment_prompt"]),
            nodes=nodes,
            edges=edges,
        )

    @staticmethod
    def _node_from_row(row: Mapping[str, object]) -> WorkflowNode:
        values = dict(row)
        output = _load_json(values.get("output_json"), None)
        error = _load_json(values.get("error_json"), None)
        return WorkflowNode(
            id=str(values["id"]),
            stable_key=str(values["stable_key"]),
            kind=str(values["kind"]),
            member_id=(
                str(values["team_member_id"])
                if values.get("team_member_id") is not None
                else None
            ),
            member_key=(
                str(values["member_key"])
                if values.get("member_key") is not None
                else None
            ),
            role=(
                str(values["member_role"])
                if values.get("member_role") is not None
                else None
            ),
            execution_class=(
                str(values["execution_class"])
                if values.get("execution_class") is not None
                else None
            ),
            operation=(
                str(values["operation_key"])
                if values.get("operation_key") is not None
                else None
            ),
            operation_version=(
                str(values["operation_version"])
                if values.get("operation_version") is not None
                else None
            ),
            prompt=str(values["prompt"]),
            parameters=_mapping(
                _load_json(values["parameters_json"], {}),
                "stored workflow node parameters",
            ),
            bindings=_mapping(
                _load_json(values["bindings_json"], {}),
                "stored workflow node bindings",
            ),
            expected_output=_mapping(
                _load_json(values["expected_output_json"], {}),
                "stored workflow node expected output",
            ),
            resources=tuple(
                str(item)
                for item in _list(
                    _load_json(values["resources_json"], []),
                    "stored workflow node resources",
                )
            ),
            position=int(str(values["position"])),
            state=str(values.get("state", "pending")),
            output=dict(output) if isinstance(output, Mapping) else None,
            error=dict(error) if isinstance(error, Mapping) else None,
            reused_from_node_id=(
                str(values["reused_from_node_id"])
                if values.get("reused_from_node_id") is not None
                else None
            ),
            resource_wait_count=int(
                str(values.get("resource_wait_count") or 0)
            ),
        )

    def _run_context(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT runs.id, runs.team_version_id,
                          runs.sandbox_version_id, runs.base_sha,
                          activation_events.issue_version_id
                              AS issue_version_id
                     FROM runs
                     JOIN activation_events
                       ON activation_events.id=runs.activation_event_id
                    WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def _selected_members(self, run_id: str) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT team_members.id, team_members.stable_key,
                          team_members.role AS execution_class,
                          team_members.atomic_role AS role,
                          team_members.responsibilities,
                          agent_assignments.assigned_at
                     FROM agent_assignments
                     JOIN team_members
                       ON team_members.id=agent_assignments.team_member_id
                    WHERE agent_assignments.run_id=?
                    ORDER BY agent_assignments.assigned_at,
                             team_members.stable_key""",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def compile_run(
        self, run_id: str, issue_version_id: str | None = None
    ) -> RunWorkflowGraph:
        try:
            existing = self.active_run_graph(run_id)
        except KeyError:
            existing = None
        if existing is not None:
            if (
                issue_version_id is not None
                and existing.issue_version_id != issue_version_id
            ):
                raise ValueError("run workflow issue version is immutable")
            return existing
        context = self._run_context(run_id)
        resolved_issue_version = issue_version_id or context.get(
            "issue_version_id"
        )
        if issue_version_id is not None and context.get(
            "issue_version_id"
        ) not in {
            None,
            issue_version_id,
        }:
            raise ValueError(
                "run workflow does not match the exact issue version"
            )
        team_version_id = str(context["team_version_id"])
        selected = self._selected_members(run_id)
        selected_member_keys = {
            str(member["stable_key"]) for member in selected
        }
        try:
            template = self.load_template(team_version_id)
        except KeyError:
            template = None
        if template is not None:
            template_nodes = list(template.nodes)
            if selected_member_keys:
                template_member_keys = {
                    str(node.member_key)
                    for node in template_nodes
                    if node.kind == "agent" and node.member_key is not None
                }
                missing_members = (
                    selected_member_keys - template_member_keys
                )
                if missing_members:
                    missing = sorted(missing_members)[0]
                    raise ValueError(
                        f"selected workflow member {missing} has no "
                        "template node"
                    )
                selected_node_keys = {
                    node.stable_key
                    for node in template_nodes
                    if node.kind == "agent"
                    and node.member_key in selected_member_keys
                }
                boundary_node_keys = {
                    node.stable_key
                    for node in template_nodes
                    if node.execution_class in {"lead", "verifier"}
                }
                template_by_key = {
                    node.stable_key: node for node in template_nodes
                }
                template_predecessors = {
                    node.stable_key: set() for node in template_nodes
                }
                for edge in template.edges:
                    template_predecessors[edge.target].add(edge.source)
                dependency_node_keys: set[str] = set()
                for selected_key in sorted(
                    selected_node_keys - boundary_node_keys
                ):
                    pending_dependencies = list(
                        template_predecessors[selected_key]
                    )
                    visited_dependencies: set[str] = set()
                    while pending_dependencies:
                        dependency_key = pending_dependencies.pop()
                        if dependency_key in visited_dependencies:
                            continue
                        visited_dependencies.add(dependency_key)
                        dependency = template_by_key[dependency_key]
                        if (
                            dependency.kind == "agent"
                            and dependency_key not in selected_node_keys
                            and dependency_key not in boundary_node_keys
                        ):
                            selected_member = template_by_key[
                                selected_key
                            ].member_key
                            raise ValueError(
                                "selected workflow member "
                                f"{selected_member} requires unassigned "
                                f"member {dependency.member_key}"
                            )
                        dependency_node_keys.add(dependency_key)
                        pending_dependencies.extend(
                            template_predecessors[dependency_key]
                        )

                candidates = {
                    node.stable_key
                    for node in template_nodes
                    if node.kind == "deterministic"
                    or node.stable_key in selected_node_keys
                    or node.stable_key in boundary_node_keys
                }
                outgoing = {key: set() for key in candidates}
                for edge in template.edges:
                    if edge.source in candidates and edge.target in candidates:
                        outgoing[edge.source].add(edge.target)
                required: set[str] = set()
                pending = list(
                    selected_node_keys
                    | boundary_node_keys
                    | dependency_node_keys
                )
                while pending:
                    key = pending.pop()
                    if key in required:
                        continue
                    required.add(key)
                    pending.extend(outgoing[key] - required)
                lead_node_keys = {
                    node.stable_key
                    for node in template_nodes
                    if node.execution_class == "lead"
                }
                for selected_key in sorted(
                    selected_node_keys - boundary_node_keys
                ):
                    reachable = {selected_key}
                    pending = [selected_key]
                    while pending:
                        key = pending.pop()
                        for target in outgoing[key] - reachable:
                            reachable.add(target)
                            pending.append(target)
                    if reachable.isdisjoint(lead_node_keys):
                        node = template_by_key[selected_key]
                        raise ValueError(
                            "selected workflow member "
                            f"{node.member_key} is disconnected by an "
                            "unassigned agent dependency"
                        )
                nodes = [
                    node
                    for node in template_nodes
                    if node.stable_key in required
                ]
            else:
                nodes = template_nodes
            kept = {node.stable_key for node in nodes}
            edges = [
                edge
                for edge in template.edges
                if edge.source in kept and edge.target in kept
            ]
            nodes = [self._filter_node_bindings(node, kept) for node in nodes]
            self._validate_compiled_graph(
                team_version_id,
                template.rationale,
                template.assessment_prompt,
                nodes,
                edges,
            )
            template_id = template.id
            rationale = template.rationale
            assessment_prompt = template.assessment_prompt
        else:
            if not selected:
                raise ValueError("legacy run has no selected team assignments")
            ordering = {"scout": 0, "implementer": 0, "lead": 1, "verifier": 2}
            selected.sort(
                key=lambda member: (
                    ordering.get(str(member["execution_class"]), 0),
                    str(member["assigned_at"]),
                    str(member["stable_key"]),
                )
            )
            nodes = [
                WorkflowNode(
                    id="",
                    stable_key=str(member["stable_key"]),
                    kind="agent",
                    member_id=str(member["id"]),
                    member_key=str(member["stable_key"]),
                    role=str(member["role"]),
                    execution_class=str(member["execution_class"]),
                    operation=None,
                    operation_version=None,
                    prompt=str(member["responsibilities"]),
                    parameters={},
                    bindings={},
                    expected_output={
                        "type": "object",
                        "additionalProperties": True,
                    },
                    resources=(
                        ("workspace:write",)
                        if str(member["execution_class"]) == "implementer"
                        else ("workspace:read",)
                    ),
                    position=0,
                )
                for member in selected
            ]
            edges = [
                WorkflowEdge(
                    nodes[index].stable_key, nodes[index + 1].stable_key
                )
                for index in range(len(nodes) - 1)
            ]
            template_id = None
            rationale = "Preserve the legacy selected-member execution order."
            assessment_prompt = "Assess the completed legacy workflow."
        self._insert_generation(
            run_id=run_id,
            template_id=template_id,
            issue_version_id=(
                str(resolved_issue_version)
                if resolved_issue_version is not None
                else None
            ),
            generation=1,
            reason="initial issue graph",
            rationale=rationale,
            assessment_prompt=assessment_prompt,
            nodes=nodes,
            edges=edges,
            assessment=None,
            previous=None,
        )
        self.refresh_readiness(run_id)
        return self.active_run_graph(run_id)

    def _validate_compiled_graph(
        self,
        team_version_id: str,
        rationale: str,
        assessment_prompt: str,
        nodes: Sequence[WorkflowNode],
        edges: Sequence[WorkflowEdge],
    ) -> None:
        for node in nodes:
            if node.kind != "deterministic":
                continue
            operation = self.registry.get(node.operation or "")
            if node.operation_version != operation.version_hash:
                raise ValueError(
                    f"workflow node {node.stable_key} operation version "
                    "is stale; re-onboard the repository"
                )
        validate_workflow_design(
            {
                "rationale": rationale,
                "assessment_prompt": assessment_prompt,
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
                    }
                    for node in nodes
                ],
                "edges": [
                    {"source": edge.source, "target": edge.target}
                    for edge in edges
                ],
            },
            self._members(team_version_id),
            self.registry.catalog(),
        )

    @staticmethod
    def _filter_node_bindings(
        node: WorkflowNode, kept: set[str]
    ) -> WorkflowNode:
        filtered: dict[str, object] = {}
        for name, raw in node.bindings.items():
            if isinstance(raw, Mapping) and isinstance(raw.get("nodes"), list):
                value = dict(raw)
                value["nodes"] = [
                    item for item in raw["nodes"] if item in kept
                ]
                if not value["nodes"]:
                    raise ValueError(
                        f"workflow node {node.stable_key} binding {name} "
                        "lost every selected dependency"
                    )
                filtered[name] = value
            elif isinstance(raw, Mapping) and raw.get("node") not in {
                None,
                "",
            }:
                if raw.get("node") not in kept:
                    raise ValueError(
                        f"workflow node {node.stable_key} binding {name} "
                        "references an unselected dependency"
                    )
                filtered[name] = dict(raw)
            else:
                filtered[name] = raw
        return WorkflowNode(**{**node.__dict__, "bindings": filtered})

    def _insert_generation(
        self,
        *,
        run_id: str,
        template_id: str | None,
        issue_version_id: str | None,
        generation: int,
        reason: str,
        rationale: str,
        assessment_prompt: str,
        nodes: Sequence[WorkflowNode],
        edges: Sequence[WorkflowEdge],
        assessment: Mapping[str, object] | None,
        previous: RunWorkflowGraph | None,
    ) -> str:
        workflow_id = _stable_id(run_id, "workflow", generation)
        now = _utc_now()
        member_by_key = {
            str(member["stable_key"]): str(member["id"])
            for member in self._members(
                str(self._run_context(run_id)["team_version_id"])
            )
        }
        previous_by_key = (
            {node.stable_key: node for node in previous.nodes}
            if previous
            else {}
        )
        old_predecessors: dict[str, set[str]] = {}
        if previous:
            old_predecessors = {
                node.stable_key: set() for node in previous.nodes
            }
            for edge in previous.edges:
                old_predecessors[edge.target].add(edge.source)
        new_predecessors = {node.stable_key: set() for node in nodes}
        for edge in edges:
            new_predecessors[edge.target].add(edge.source)
        ordered = _topological_order(
            [node.stable_key for node in nodes], edges
        )
        reusable: set[str] = set()
        for key in ordered:
            current = next(node for node in nodes if node.stable_key == key)
            old = previous_by_key.get(key)
            if (
                old is not None
                and old.state == "succeeded"
                and self._same_node_definition(old, current)
                and old_predecessors.get(key, set()) == new_predecessors[key]
                and new_predecessors[key].issubset(reusable)
            ):
                reusable.add(key)
        node_ids = {
            node.stable_key: _stable_id(workflow_id, "node", node.stable_key)
            for node in nodes
        }
        with self.database.transaction() as connection:
            if previous is not None:
                connection.execute(
                    """UPDATE run_workflows
                          SET active=0, state='superseded', updated_at=?
                        WHERE id=? AND active=1""",
                    (now, previous.id),
                )
            connection.execute(
                """INSERT INTO run_workflows
                   (id, run_id, team_workflow_template_id, issue_version_id,
                    generation, state, reason, assessment_json, active,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 1, ?, ?)""",
                (
                    workflow_id,
                    run_id,
                    template_id,
                    issue_version_id,
                    generation,
                    reason,
                    _json(
                        {
                            "rationale": rationale,
                            "assessment_prompt": assessment_prompt,
                        }
                    ),
                    now,
                    now,
                ),
            )
            for position, node in enumerate(nodes):
                old = previous_by_key.get(node.stable_key)
                if node.stable_key in reusable and old is not None:
                    state = "succeeded"
                    output_json = (
                        _json(old.output) if old.output is not None else None
                    )
                    reused_from_node_id = old.id
                else:
                    state = "pending"
                    output_json = None
                    reused_from_node_id = None
                connection.execute(
                    """INSERT INTO run_workflow_nodes
                       (id, run_workflow_id, stable_key, kind,
                        team_member_id, operation_key, operation_version,
                        prompt, parameters_json, bindings_json,
                        expected_output_json, resources_json, state,
                        position, output_json, error_json,
                        reused_from_node_id, created_at, updated_at,
                        completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, NULL, ?, ?, ?, ?)""",
                    (
                        node_ids[node.stable_key],
                        workflow_id,
                        node.stable_key,
                        node.kind,
                        (
                            member_by_key.get(node.member_key or "")
                            if node.kind == "agent"
                            else None
                        ),
                        (
                            node.operation
                            if node.kind == "deterministic"
                            else None
                        ),
                        (
                            node.operation_version
                            if node.kind == "deterministic"
                            else None
                        ),
                        node.prompt,
                        _json(node.parameters),
                        _json(node.bindings),
                        _json(node.expected_output),
                        _json(list(node.resources)),
                        state,
                        position,
                        output_json,
                        reused_from_node_id,
                        now,
                        now,
                        now if reused_from_node_id is not None else None,
                    ),
                )
            for position, edge in enumerate(edges):
                connection.execute(
                    """INSERT INTO run_workflow_edges
                       (id, run_workflow_id, source_node_id, target_node_id,
                        position, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        _stable_id(
                            workflow_id, "edge", edge.source, edge.target
                        ),
                        workflow_id,
                        node_ids[edge.source],
                        node_ids[edge.target],
                        position,
                        now,
                    ),
                )
            if assessment is not None:
                assessment_workflow_id = (
                    previous.id if previous is not None else workflow_id
                )
                self._insert_assessment(
                    connection,
                    assessment_workflow_id,
                    run_id,
                    assessment,
                    now,
                )
        return workflow_id

    @staticmethod
    def _same_node_definition(
        first: WorkflowNode, second: WorkflowNode
    ) -> bool:
        return (
            first.kind,
            first.member_key,
            first.operation,
            first.operation_version,
            first.prompt,
            first.parameters,
            first.bindings,
            first.expected_output,
            first.resources,
        ) == (
            second.kind,
            second.member_key,
            second.operation,
            second.operation_version,
            second.prompt,
            second.parameters,
            second.bindings,
            second.expected_output,
            second.resources,
        )

    def active_run_graph(self, run_id: str) -> RunWorkflowGraph:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT run_workflows.*, runs.team_version_id,
                          runs.sandbox_version_id, runs.base_sha
                     FROM run_workflows
                     JOIN runs ON runs.id=run_workflows.run_id
                    WHERE run_workflows.run_id=? AND run_workflows.active=1""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._load_run_graph(str(row["id"]))

    def _load_run_graph(self, workflow_id: str) -> RunWorkflowGraph:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT run_workflows.*, runs.team_version_id,
                          runs.sandbox_version_id, runs.base_sha
                     FROM run_workflows
                     JOIN runs ON runs.id=run_workflows.run_id
                    WHERE run_workflows.id=?""",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise KeyError(workflow_id)
            nodes = connection.execute(
                """SELECT workflow_nodes.*,
                          team_members.stable_key AS member_key,
                          team_members.atomic_role AS member_role,
                          team_members.role AS execution_class
                     FROM run_workflow_nodes AS workflow_nodes
                     LEFT JOIN team_members
                       ON team_members.id=workflow_nodes.team_member_id
                    WHERE workflow_nodes.run_workflow_id=?
                    ORDER BY workflow_nodes.position""",
                (workflow_id,),
            ).fetchall()
            edges = connection.execute(
                """SELECT source.stable_key AS source_key,
                          target.stable_key AS target_key
                     FROM run_workflow_edges AS workflow_edges
                     JOIN run_workflow_nodes AS source
                       ON source.id=workflow_edges.source_node_id
                     JOIN run_workflow_nodes AS target
                       ON target.id=workflow_edges.target_node_id
                    WHERE workflow_edges.run_workflow_id=?
                    ORDER BY workflow_edges.position""",
                (workflow_id,),
            ).fetchall()
            assessment_rows = connection.execute(
                """SELECT outcome, evidence, proposal_json, created_at
                     FROM workflow_assessments
                    WHERE run_workflow_id=?
                    ORDER BY rowid""",
                (workflow_id,),
            ).fetchall()
        metadata = _mapping(
            _load_json(row["assessment_json"], {}),
            "stored workflow metadata",
        )
        assessment_values: list[dict[str, object]] = []
        for assessment_row in assessment_rows:
            assessment_value = {
                "outcome": str(assessment_row["outcome"]),
                "evidence": str(assessment_row["evidence"]),
                "created_at": str(assessment_row["created_at"]),
            }
            proposal = _load_json(assessment_row["proposal_json"], None)
            if proposal is not None:
                assessment_value["proposal"] = proposal
            assessment_values.append(assessment_value)
        assessment_value = (
            assessment_values[-1] if assessment_values else None
        )
        return RunWorkflowGraph(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            template_id=(
                str(row["team_workflow_template_id"])
                if row["team_workflow_template_id"] is not None
                else None
            ),
            generation=int(row["generation"]),
            issue_version_id=(
                str(row["issue_version_id"])
                if row["issue_version_id"] is not None
                else None
            ),
            team_version_id=str(row["team_version_id"]),
            sandbox_version_id=str(row["sandbox_version_id"]),
            base_sha=str(row["base_sha"]),
            state=str(row["state"]),
            reason=str(row["reason"]),
            active=bool(row["active"]),
            rationale=str(metadata.get("rationale", "")),
            assessment_prompt=str(metadata.get("assessment_prompt", "")),
            nodes=tuple(self._node_from_row(node) for node in nodes),
            edges=tuple(
                WorkflowEdge(str(edge["source_key"]), str(edge["target_key"]))
                for edge in edges
            ),
            assessments=tuple(assessment_values),
            assessment=assessment_value,
        )

    def refresh_readiness(self, run_id: str) -> tuple[str, ...]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT id FROM run_workflows
                    WHERE run_id=? AND active=1""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._refresh_workflow_readiness(str(row["id"]))

    def _refresh_workflow_readiness(self, workflow_id: str) -> tuple[str, ...]:
        now = _utc_now()
        with self.database.transaction() as connection:
            workflow = connection.execute(
                "SELECT state FROM run_workflows WHERE id=?",
                (workflow_id,),
            ).fetchone()
            if workflow is None:
                raise KeyError(workflow_id)
            rows = connection.execute(
                """SELECT id, stable_key, state
                     FROM run_workflow_nodes
                    WHERE run_workflow_id=?
                    ORDER BY position""",
                (workflow_id,),
            ).fetchall()
            state_by_id = {str(row["id"]): str(row["state"]) for row in rows}
            key_by_id = {
                str(row["id"]): str(row["stable_key"]) for row in rows
            }
            predecessors = {node_id: set() for node_id in state_by_id}
            for edge in connection.execute(
                """SELECT source_node_id, target_node_id
                     FROM run_workflow_edges
                    WHERE run_workflow_id=?""",
                (workflow_id,),
            ).fetchall():
                predecessors[str(edge["target_node_id"])].add(
                    str(edge["source_node_id"])
                )
            changed = True
            while changed:
                changed = False
                for node_id, current in tuple(state_by_id.items()):
                    if current not in {"pending", "ready"}:
                        continue
                    dependency_states = {
                        state_by_id[source] for source in predecessors[node_id]
                    }
                    failed_dependencies = [
                        key_by_id[source]
                        for source in predecessors[node_id]
                        if state_by_id[source]
                        in {"failed", "blocked", "canceled"}
                    ]
                    if failed_dependencies:
                        target = "blocked"
                    elif dependency_states.issubset(_TERMINAL_NODE_STATES):
                        target = "ready"
                    else:
                        target = "pending"
                    if target == current:
                        continue
                    state_by_id[node_id] = target
                    changed = True
                    error_json = (
                        _json(
                            {
                                "type": "DependencyBlocked",
                                "message": (
                                    "required predecessor did not succeed"
                                ),
                                "dependencies": sorted(failed_dependencies),
                            }
                        )
                        if target == "blocked"
                        else None
                    )
                    connection.execute(
                        """UPDATE run_workflow_nodes
                              SET state=?, error_json=?, updated_at=?,
                                  completed_at=?
                            WHERE id=?""",
                        (
                            target,
                            error_json,
                            now,
                            now if target == "blocked" else None,
                            node_id,
                        ),
                    )
            workflow_state = str(workflow["state"])
            if workflow_state not in {"canceled", "superseded"}:
                states = set(state_by_id.values())
                if "running" in states:
                    workflow_state = "running"
                elif "failed" in states:
                    workflow_state = "failed"
                elif states.issubset(_TERMINAL_NODE_STATES):
                    workflow_state = "succeeded"
                else:
                    workflow_state = "pending"
                connection.execute(
                    """UPDATE run_workflows
                          SET state=?, updated_at=?
                        WHERE id=?""",
                    (workflow_state, now, workflow_id),
                )
        return tuple(
            node_id
            for node_id, state in state_by_id.items()
            if state == "ready"
        )

    def record_resource_waits(self, node_ids: Iterable[str]) -> None:
        values = tuple(dict.fromkeys(str(node_id) for node_id in node_ids))
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        with self.database.transaction() as connection:
            connection.execute(
                f"""UPDATE run_workflow_nodes
                       SET resource_wait_count=resource_wait_count + 1,
                           updated_at=?
                     WHERE id IN ({placeholders}) AND state='ready'""",
                (_utc_now(), *values),
            )

    def begin_attempt(
        self, node_id: str, inputs: Mapping[str, object] | None = None
    ) -> str:
        now = _utc_now()
        with self.database.transaction() as connection:
            node = connection.execute(
                """SELECT run_workflow_id, state, resources_json
                     FROM run_workflow_nodes WHERE id=?""",
                (node_id,),
            ).fetchone()
            if node is None:
                raise KeyError(node_id)
            if str(node["state"]) != "ready":
                raise ValueError(
                    f"workflow node cannot begin from {node['state']}"
                )
            attempt = int(
                connection.execute(
                    """SELECT COALESCE(MAX(attempt), 0) + 1
                         FROM run_workflow_attempts
                        WHERE run_workflow_node_id=?""",
                    (node_id,),
                ).fetchone()[0]
            )
            attempt_id = _stable_id(node_id, "attempt", attempt)
            connection.execute(
                """INSERT INTO run_workflow_attempts
                   (id, run_workflow_node_id, attempt, state, input_json,
                    started_at)
                   VALUES (?, ?, ?, 'running', ?, ?)""",
                (attempt_id, node_id, attempt, _json(dict(inputs or {})), now),
            )
            resources = _list(
                _load_json(node["resources_json"], []),
                "stored workflow node resources",
            )
            for resource_value in resources:
                resource = str(resource_value)
                _, access = _resource_identity(resource)
                connection.execute(
                    """INSERT INTO run_workflow_resource_claims
                       (id, run_workflow_attempt_id, resource, access,
                        acquired_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        _stable_id(attempt_id, "resource", resource),
                        attempt_id,
                        resource,
                        access,
                        now,
                    ),
                )
            connection.execute(
                """UPDATE run_workflow_nodes
                      SET state='running', error_json=NULL, started_at=?,
                          updated_at=?
                    WHERE id=?""",
                (now, now, node_id),
            )
            connection.execute(
                """UPDATE run_workflows SET state='running', updated_at=?
                    WHERE id=?""",
                (now, node["run_workflow_id"]),
            )
        return attempt_id

    def complete_attempt(
        self,
        node_id: str,
        output: Mapping[str, object],
        *,
        log_path: str | None = None,
    ) -> None:
        normalized = dict(output)
        now = _utc_now()
        with self.database.transaction() as connection:
            node = connection.execute(
                """SELECT run_workflow_id, state, expected_output_json
                     FROM run_workflow_nodes WHERE id=?""",
                (node_id,),
            ).fetchone()
            if node is None:
                raise KeyError(node_id)
            if str(node["state"]) != "running":
                raise ValueError("workflow node has no running attempt")
            schema = _mapping(
                _load_json(node["expected_output_json"], {}),
                "stored workflow output schema",
            )
            _validate_schema(normalized, schema, "workflow node output")
            _validate_safe_value(normalized, path="workflow node output")
            updated = connection.execute(
                """UPDATE run_workflow_attempts
                      SET state='succeeded', output_json=?, log_path=?,
                          completed_at=?
                    WHERE id=(
                        SELECT id FROM run_workflow_attempts
                         WHERE run_workflow_node_id=? AND state='running'
                         ORDER BY attempt DESC LIMIT 1
                    )""",
                (_json(normalized), log_path, now, node_id),
            ).rowcount
            if updated != 1:
                raise ValueError("workflow node running attempt is missing")
            connection.execute(
                """UPDATE run_workflow_resource_claims
                      SET released_at=?
                    WHERE run_workflow_attempt_id IN (
                        SELECT id FROM run_workflow_attempts
                         WHERE run_workflow_node_id=? AND state='succeeded'
                         ORDER BY attempt DESC LIMIT 1
                    ) AND released_at IS NULL""",
                (now, node_id),
            )
            connection.execute(
                """UPDATE run_workflow_nodes
                      SET state='succeeded', output_json=?, error_json=NULL,
                          updated_at=?, completed_at=?
                    WHERE id=?""",
                (_json(normalized), now, now, node_id),
            )
        self._refresh_workflow_readiness(str(node["run_workflow_id"]))

    def fail_attempt(
        self,
        node_id: str,
        error: BaseException,
        *,
        known_secrets: Iterable[str] = (),
        log_path: str | None = None,
    ) -> None:
        now = _utc_now()
        message = redact_text(str(error), known_secrets)[:2000]
        error_value = {"type": type(error).__name__, "message": message}
        with self.database.transaction() as connection:
            node = connection.execute(
                """SELECT run_workflow_id, state
                     FROM run_workflow_nodes WHERE id=?""",
                (node_id,),
            ).fetchone()
            if node is None:
                raise KeyError(node_id)
            connection.execute(
                """UPDATE run_workflow_attempts
                      SET state='failed', error_json=?, log_path=?,
                          completed_at=?
                    WHERE id=(
                        SELECT id FROM run_workflow_attempts
                         WHERE run_workflow_node_id=? AND state='running'
                         ORDER BY attempt DESC LIMIT 1
                    )""",
                (_json(error_value), log_path, now, node_id),
            )
            connection.execute(
                """UPDATE run_workflow_resource_claims
                      SET released_at=?
                    WHERE run_workflow_attempt_id IN (
                        SELECT id FROM run_workflow_attempts
                         WHERE run_workflow_node_id=? AND state='failed'
                         ORDER BY attempt DESC LIMIT 1
                    ) AND released_at IS NULL""",
                (now, node_id),
            )
            connection.execute(
                """UPDATE run_workflow_nodes
                      SET state='failed', error_json=?, updated_at=?,
                          completed_at=?
                    WHERE id=?""",
                (_json(error_value), now, now, node_id),
            )
            connection.execute(
                """UPDATE run_workflows
                      SET state='failed', updated_at=? WHERE id=?""",
                (now, node["run_workflow_id"]),
            )
        self._refresh_workflow_readiness(str(node["run_workflow_id"]))

    def recover_interrupted(self, run_id: str) -> tuple[str, ...]:
        now = _utc_now()
        recovered: list[str] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT workflow_nodes.id, workflow_nodes.run_workflow_id
                     FROM run_workflow_nodes AS workflow_nodes
                     JOIN run_workflows
                       ON run_workflows.id=workflow_nodes.run_workflow_id
                    WHERE run_workflows.run_id=? AND run_workflows.active=1
                      AND workflow_nodes.state='running'
                    ORDER BY workflow_nodes.position""",
                (run_id,),
            ).fetchall()
            for row in rows:
                node_id = str(row["id"])
                recovered.append(node_id)
                error = _json(
                    {
                        "type": "InterruptedError",
                        "message": (
                            "controller stopped before the node completed"
                        ),
                    }
                )
                connection.execute(
                    """UPDATE run_workflow_attempts
                          SET state='interrupted', error_json=?, completed_at=?
                        WHERE id=(
                            SELECT id FROM run_workflow_attempts
                             WHERE run_workflow_node_id=? AND state='running'
                             ORDER BY attempt DESC LIMIT 1
                        )""",
                    (error, now, node_id),
                )
                connection.execute(
                    """UPDATE run_workflow_resource_claims
                          SET released_at=?
                        WHERE run_workflow_attempt_id IN (
                            SELECT id
                              FROM run_workflow_attempts
                             WHERE run_workflow_node_id=?
                               AND state='interrupted'
                             ORDER BY attempt DESC LIMIT 1
                        ) AND released_at IS NULL""",
                    (now, node_id),
                )
                connection.execute(
                    """UPDATE run_workflow_nodes
                          SET state='pending', error_json=NULL, updated_at=?,
                              started_at=NULL, completed_at=NULL
                        WHERE id=?""",
                    (now, node_id),
                )
            if rows:
                connection.execute(
                    """UPDATE run_workflows
                          SET state='pending', updated_at=?
                        WHERE id=?""",
                    (now, rows[0]["run_workflow_id"]),
                )
        self.refresh_readiness(run_id)
        return tuple(recovered)

    def revise(
        self,
        run_id: str,
        *,
        reason: str,
        assessment: Mapping[str, object],
        design: Mapping[str, object],
    ) -> RunWorkflowGraph:
        reason = _string(reason, "workflow revision reason")
        current = self.active_run_graph(run_id)
        members = self._members(current.team_version_id)
        normalized = validate_workflow_design(
            design, members, self.registry.catalog()
        )
        allowed_member_keys = {
            str(member["stable_key"])
            for member in self._selected_members(run_id)
        }
        allowed_member_keys.update(
            node.member_key
            for node in current.nodes
            if node.kind == "agent" and node.member_key is not None
        )
        revised_member_keys: set[str] = set()
        for raw_node in _list(normalized["nodes"], "workflow nodes"):
            node = _mapping(raw_node, "workflow node")
            if node["kind"] == "agent":
                revised_member_keys.add(str(node["member_key"]))
        if not revised_member_keys.issubset(allowed_member_keys):
            raise ValueError(
                "workflow revision cannot broaden member authority"
            )
        node_values = self._nodes_from_design(normalized, members)
        edge_values: list[WorkflowEdge] = []
        for raw_edge in _list(normalized["edges"], "workflow edges"):
            edge = _mapping(raw_edge, "workflow edge")
            edge_values.append(
                WorkflowEdge(str(edge["source"]), str(edge["target"]))
            )
        edges = tuple(edge_values)
        current_by_key = {node.stable_key: node for node in current.nodes}
        unchanged_nodes = (
            len(current_by_key) == len(node_values)
            and all(
                (
                    prior := current_by_key.get(node.stable_key)
                )
                is not None
                and self._same_node_definition(prior, node)
                for node in node_values
            )
        )
        unchanged_edges = {
            (edge.source, edge.target) for edge in current.edges
        } == {(edge.source, edge.target) for edge in edges}
        if unchanged_nodes and unchanged_edges:
            raise ValueError(
                "workflow revision must change an executable node or dependency"
            )

        self._insert_generation(
            run_id=run_id,
            template_id=current.template_id,
            issue_version_id=current.issue_version_id,
            generation=current.generation + 1,
            reason=reason,
            rationale=str(normalized["rationale"]),
            assessment_prompt=str(normalized["assessment_prompt"]),
            nodes=node_values,
            edges=edges,
            assessment=assessment,
            previous=current,
        )
        self.refresh_readiness(run_id)
        return self.active_run_graph(run_id)

    @staticmethod
    def _nodes_from_design(
        normalized: Mapping[str, object],
        members: Sequence[Mapping[str, object]],
    ) -> tuple[WorkflowNode, ...]:
        member_by_key = {
            str(member["stable_key"]): _mapping(member, "team member")
            for member in members
        }
        values: list[WorkflowNode] = []
        for position, raw in enumerate(
            _list(normalized["nodes"], "workflow nodes")
        ):
            node = _mapping(raw, "workflow node")
            member = member_by_key.get(str(node["member_key"]))
            values.append(
                WorkflowNode(
                    id="",
                    stable_key=str(node["stable_key"]),
                    kind=str(node["kind"]),
                    member_id=str(member["id"]) if member else None,
                    member_key=str(node["member_key"]) or None,
                    role=str(member["role"]) if member else None,
                    execution_class=(
                        str(member["execution_class"]) if member else None
                    ),
                    operation=str(node["operation"]) or None,
                    operation_version=str(node["operation_version"]) or None,
                    prompt=str(node["prompt"]),
                    parameters=_mapping(
                        node["parameters"], "workflow node parameters"
                    ),
                    bindings=_mapping(
                        node["bindings"], "workflow node bindings"
                    ),
                    expected_output=_mapping(
                        node["expected_output"],
                        "workflow node expected output",
                    ),
                    resources=tuple(
                        str(item)
                        for item in _list(
                            node["resources"], "workflow node resources"
                        )
                    ),
                    position=position,
                )
            )
        return tuple(values)

    @staticmethod
    def _insert_assessment(
        connection: Any,
        workflow_id: str,
        run_id: str,
        assessment: Mapping[str, object],
        now: str,
    ) -> None:
        outcome = _string(
            assessment.get("outcome"), "workflow assessment outcome"
        )
        if outcome not in {"accept", "revise"}:
            raise ValueError(
                "workflow assessment outcome must be accept or revise"
            )
        evidence = _bounded_string(
            assessment.get("evidence"),
            "workflow assessment evidence",
            minimum=16,
            maximum=_PROMPT_MAX_BYTES,
        )
        leader = connection.execute(
            """SELECT team_members.id
                 FROM runs
                 JOIN team_members
                   ON team_members.team_version_id=runs.team_version_id
                WHERE runs.id=? AND team_members.role='lead'
                ORDER BY team_members.stable_key LIMIT 1""",
            (run_id,),
        ).fetchone()
        if leader is None:
            raise ValueError("workflow assessment requires a stored lead")
        proposal = {
            key: value
            for key, value in assessment.items()
            if key not in {"outcome", "evidence"}
        }
        _validate_safe_value(proposal, path="workflow assessment proposal")
        sequence = int(
            connection.execute(
                """SELECT COUNT(*) + 1 FROM workflow_assessments
                    WHERE run_workflow_id=?""",
                (workflow_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO workflow_assessments
               (id, run_workflow_id, leader_team_member_id, outcome, evidence,
                proposal_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                _stable_id(workflow_id, "assessment", sequence),
                workflow_id,
                leader["id"],
                outcome,
                evidence,
                _json(proposal) if proposal else None,
                now,
            ),
        )

    def record_assessment(
        self, run_id: str, assessment: Mapping[str, object]
    ) -> None:
        graph = self.active_run_graph(run_id)
        now = _utc_now()
        with self.database.transaction() as connection:
            self._insert_assessment(
                connection, graph.id, run_id, assessment, now
            )

    def cancel_pending(self, run_id: str) -> None:
        graph = self.active_run_graph(run_id)
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE run_workflow_nodes
                      SET state='canceled', updated_at=?, completed_at=?
                    WHERE run_workflow_id=?
                      AND state IN (
                          'pending', 'ready', 'failed', 'blocked'
                      )""",
                (now, now, graph.id),
            )
            connection.execute(
                """UPDATE run_workflows
                      SET state='canceled', updated_at=?, completed_at=?
                    WHERE id=?""",
                (now, now, graph.id),
            )

    def mark_succeeded(self, workflow_id: str) -> None:
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE run_workflows
                      SET state='succeeded', updated_at=?, completed_at=?
                    WHERE id=?""",
                (now, now, workflow_id),
            )

    def project_template(self, team_version_id: str) -> dict[str, object]:
        template = self.load_template(team_version_id)
        layout = _layout(template.nodes, template.edges)
        nodes = [
            self._project_node(node, layout[node.stable_key])
            for node in template.nodes
        ]
        edges = [
            {"source": edge.source, "target": edge.target}
            for edge in template.edges
        ]
        boundaries, boundary_edges = self._project_controller_boundaries(
            nodes
        )
        system_boundaries, lifecycle_edges = (
            self._project_system_lifecycle(
                nodes,
                edges,
                boundaries,
                contract={
                    "mode": "template",
                    "run_id": None,
                    "issue_version_id": None,
                    "team_version_id": template.team_version_id,
                    "sandbox_version_id": None,
                    "base_sha": None,
                    "generation": "N",
                },
                run_state=None,
            )
        )
        return {
            "id": template.id,
            "team_version_id": template.team_version_id,
            "rationale": template.rationale,
            "assessment_prompt": template.assessment_prompt,
            "nodes": nodes,
            "edges": edges,
            "controller_boundaries": boundaries,
            "controller_edges": boundary_edges,
            "system_boundaries": system_boundaries,
            "lifecycle_edges": lifecycle_edges,
        }

    def project_run(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT run_workflows.id, run_workflows.generation,
                          run_workflows.active, runs.state AS run_state
                     FROM run_workflows
                     JOIN runs ON runs.id=run_workflows.run_id
                    WHERE run_workflows.run_id=?
                    ORDER BY run_workflows.generation""",
                (run_id,),
            ).fetchall()
        generations: list[dict[str, object]] = []
        active_generation: int | None = None
        for row in rows:
            graph = self._load_run_graph(str(row["id"]))
            layout = _layout(graph.nodes, graph.edges)
            nodes: list[dict[str, object]] = []
            for node in graph.nodes:
                projected = self._project_node(node, layout[node.stable_key])
                projected["attempts"] = self._project_attempts(node.id)
                nodes.append(projected)
            edges = [
                {"source": edge.source, "target": edge.target}
                for edge in graph.edges
            ]
            boundaries, boundary_edges = self._project_controller_boundaries(
                nodes
            )
            system_boundaries, lifecycle_edges = (
                self._project_system_lifecycle(
                    nodes,
                    edges,
                    boundaries,
                    contract={
                        "mode": "run",
                        "run_id": graph.run_id,
                        "issue_version_id": graph.issue_version_id,
                        "team_version_id": graph.team_version_id,
                        "sandbox_version_id": graph.sandbox_version_id,
                        "base_sha": graph.base_sha,
                        "generation": graph.generation,
                    },
                    run_state=str(row["run_state"]),
                )
            )
            generation = {
                "id": graph.id,
                "generation": graph.generation,
                "active": graph.active,
                "state": graph.state,
                "reason": graph.reason,
                "rationale": graph.rationale,
                "assessment_prompt": graph.assessment_prompt,
                "assessment": graph.assessment,
                "assessments": list(graph.assessments),
                "nodes": nodes,
                "edges": edges,
                "controller_boundaries": boundaries,
                "controller_edges": boundary_edges,
                "system_boundaries": system_boundaries,
                "lifecycle_edges": lifecycle_edges,
            }
            generations.append(generation)
            if graph.active:
                active_generation = graph.generation
        return {
            "active_generation": active_generation,
            "generations": generations,
        }

    def _project_attempts(self, node_id: str) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, attempt, state, input_json, output_json,
                          error_json, log_path, started_at, completed_at
                     FROM run_workflow_attempts
                    WHERE run_workflow_node_id=?
                    ORDER BY attempt""",
                (node_id,),
            ).fetchall()
            attempts: list[dict[str, object]] = []
            for row in rows:
                claims = connection.execute(
                    """SELECT resource, access, acquired_at, released_at
                         FROM run_workflow_resource_claims
                        WHERE run_workflow_attempt_id=?
                        ORDER BY resource""",
                    (row["id"],),
                ).fetchall()
                attempts.append(
                    {
                        "attempt": int(row["attempt"]),
                        "state": str(row["state"]),
                        "input": _load_json(row["input_json"], {}),
                        "output": _load_json(row["output_json"], None),
                        "error": _load_json(row["error_json"], None),
                        "error_json": (
                            str(row["error_json"])
                            if row["error_json"] is not None
                            else None
                        ),
                        "log_path": (
                            str(row["log_path"])
                            if row["log_path"] is not None
                            else None
                        ),
                        "started_at": str(row["started_at"]),
                        "completed_at": (
                            str(row["completed_at"])
                            if row["completed_at"] is not None
                            else None
                        ),
                        "resource_claims": [
                            {
                                "resource": str(claim["resource"]),
                                "access": str(claim["access"]),
                                "acquired_at": str(claim["acquired_at"]),
                                "released_at": (
                                    str(claim["released_at"])
                                    if claim["released_at"] is not None
                                    else None
                                ),
                            }
                            for claim in claims
                        ],
                    }
                )
        return attempts

    @staticmethod
    def _project_node(
        node: WorkflowNode, location: tuple[int, int]
    ) -> dict[str, object]:
        labels = {
            "pending": "Pending",
            "ready": "Ready",
            "running": "Running",
            "succeeded": "Succeeded",
            "failed": "Failed",
            "blocked": "Blocked",
            "skipped": "Skipped",
            "canceled": "Canceled",
        }
        return {
            "id": node.id,
            "stable_key": node.stable_key,
            "kind": node.kind,
            "member_key": node.member_key,
            "role": node.role,
            "operation": node.operation,
            "operation_version": node.operation_version,
            "execution_class": node.execution_class,
            "prompt": node.prompt,
            "parameters": node.parameters,
            "bindings": node.bindings,
            "expected_output": node.expected_output,
            "resources": list(node.resources),
            "position": node.position,
            "column": location[0],
            "row": location[1],
            "state": node.state,
            "status_label": labels.get(node.state, node.state.title()),
            "output": node.output,
            "error": node.error,
            "reused": node.reused_from_node_id is not None,
            "reused_from_node_id": node.reused_from_node_id,
            "resource_wait_count": node.resource_wait_count,
            "boundary": (
                "coordinator"
                if node.execution_class == "lead"
                else (
                    "independent-verifier"
                    if node.execution_class == "verifier"
                    else (
                        "controller-owned"
                        if node.kind == "deterministic"
                        else "specialist"
                    )
                )
            ),
        }

    @staticmethod
    def _project_controller_boundaries(
        nodes: Sequence[Mapping[str, object]],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if not nodes:
            return [], []
        source = next(
            (
                str(node["stable_key"])
                for node in nodes
                if node.get("execution_class") == "verifier"
            ),
            str(nodes[-1]["stable_key"]),
        )
        max_column = max(int(str(node.get("column", 0))) for node in nodes)
        specs = (
            (
                "exact-sha-validation",
                "Exact-SHA validation",
                "deterministic",
                "validation:read",
                (
                    "Controller runs stored validation commands against "
                    "the exact committed candidate SHA."
                ),
                "validation",
                "Validate exact candidate SHA",
                "independent verifier succeeds",
                "validation boundary",
            ),
            (
                "independent-acceptance",
                "Independent acceptance",
                "controller",
                "validation:read",
                (
                    "Independent acceptance checks every issue claim and "
                    "its durable evidence at the exact candidate SHA."
                ),
                "acceptance",
                "Check issue acceptance",
                "exact-SHA validation passes",
                "acceptance boundary",
            ),
            (
                "controller-publication",
                "Controller publication",
                "controller",
                "workspace:read",
                (
                    "Controller scope review and publication run only "
                    "after validation and acceptance succeed."
                ),
                "publication",
                "Publish accepted candidate",
                "independent acceptance passes",
                "published pull request",
            ),
            (
                "feedback-resolution",
                "Feedback resolution",
                "controller",
                "issue:read",
                (
                    "Persisted pull-request feedback may compile a later "
                    "immutable source graph generation."
                ),
                "feedback-monitoring",
                "Monitor persisted pull-request feedback",
                "controller publication succeeds",
                "feedback monitoring",
            ),
        )
        projected: list[dict[str, object]] = []
        edges: list[dict[str, object]] = []
        previous = source
        for offset, (
            key,
            title,
            kind,
            resource,
            prompt,
            edge_type,
            label,
            trigger,
            next_unit,
        ) in enumerate(specs, start=1):
            projected.append(
                {
                    "id": f"boundary:{key}",
                    "stable_key": key,
                    "title": title,
                    "kind": kind,
                    "member_key": None,
                    "role": title,
                    "operation": key if kind == "deterministic" else None,
                    "operation_version": None,
                    "execution_class": None,
                    "prompt": prompt,
                    "parameters": {},
                    "bindings": {},
                    "expected_output": {},
                    "resources": [resource],
                    "position": len(nodes) + offset - 1,
                    "column": max_column + offset,
                    "row": 0,
                    "state": "boundary",
                    "status_label": "Controller boundary",
                    "output": None,
                    "error": None,
                    "reused": False,
                    "reused_from_node_id": None,
                    "resource_wait_count": 0,
                    "boundary": "controller-owned",
                    "virtual": True,
                    "attempts": [],
                }
            )
            edges.append(
                {
                    "source": previous,
                    "target": key,
                    "type": edge_type,
                    "label": label,
                    "trigger": trigger,
                    "next_unit": next_unit,
                    "projection_only": True,
                }
            )
            previous = key
        return projected, edges

    @staticmethod
    def _project_system_lifecycle(
        nodes: Sequence[Mapping[str, object]],
        edges: Sequence[Mapping[str, object]],
        controller_boundaries: Sequence[Mapping[str, object]],
        *,
        contract: Mapping[str, object],
        run_state: str | None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if not nodes:
            return [], []
        origin_key = "controller:run-contract"
        terminal_key = "controller:terminal-outcome"
        incoming = {str(edge["target"]) for edge in edges}
        roots = [
            str(node["stable_key"])
            for node in nodes
            if str(node["stable_key"]) not in incoming
        ]
        coordinator = next(
            (
                str(node["stable_key"])
                for node in nodes
                if node.get("execution_class") == "lead"
            ),
            str(nodes[-1]["stable_key"]),
        )
        all_nodes = [*nodes, *controller_boundaries]
        max_column = max(
            int(str(node.get("column", 0))) for node in all_nodes
        )
        generation = contract.get("generation", "N")
        current_generation = f"generation {generation}"
        next_generation = (
            f"generation {int(generation) + 1}"
            if isinstance(generation, int)
            else "generation N+1"
        )
        status = (
            f"Generation {generation} contract"
            if isinstance(generation, int)
            else "Template contract"
        )
        terminal_status = (
            run_state.title()
            if run_state in {"closed", "canceled"}
            else "Cycle continues"
        )
        origin = {
            "id": "boundary:run-contract",
            "stable_key": origin_key,
            "title": "Run contract / issue activation",
            "kind": "controller",
            "member_key": None,
            "role": "Controller-owned origin",
            "operation": None,
            "operation_version": None,
            "execution_class": None,
            "prompt": (
                "The controller commits the immutable issue, team, sandbox, "
                "exact base SHA, and graph generation before work starts."
            ),
            "parameters": {},
            "bindings": {},
            "expected_output": {},
            "resources": ["issue:read"],
            "position": -1,
            "column": -1,
            "row": 0,
            "state": "boundary",
            "status_label": status,
            "output": None,
            "error": None,
            "reused": False,
            "reused_from_node_id": None,
            "resource_wait_count": 0,
            "boundary": "system-origin",
            "virtual": True,
            "attempts": [],
            "contract": dict(contract),
        }
        terminal = {
            "id": "boundary:terminal-outcome",
            "stable_key": terminal_key,
            "title": "Terminal outcomes",
            "kind": "controller",
            "member_key": None,
            "role": "Controller-owned terminal boundary",
            "operation": None,
            "operation_version": None,
            "execution_class": None,
            "prompt": (
                "Closed and canceled durable run states terminate the "
                "controller cycle. Accepted work passes through publication "
                "and feedback monitoring before closure."
            ),
            "parameters": {},
            "bindings": {},
            "expected_output": {},
            "resources": [],
            "position": len(all_nodes),
            "column": max_column + 1,
            "row": 0,
            "state": (
                run_state
                if run_state in {"closed", "canceled"}
                else "boundary"
            ),
            "status_label": terminal_status,
            "output": None,
            "error": None,
            "reused": False,
            "reused_from_node_id": None,
            "resource_wait_count": 0,
            "boundary": "system-terminal",
            "virtual": True,
            "attempts": [],
            "run_state": run_state,
            "outcomes": ["closed", "canceled"],
        }

        def transition(
            source: str,
            target: str,
            edge_type: str,
            label: str,
            trigger: str,
            next_unit: str,
        ) -> dict[str, object]:
            return {
                "source": source,
                "target": target,
                "type": edge_type,
                "label": label,
                "trigger": trigger,
                "next_unit": next_unit,
                "projection_only": True,
            }

        lifecycle_edges = [
            transition(
                origin_key,
                root,
                "activation",
                f"Activate immutable {current_generation}",
                "issue activation or later generation compilation",
                current_generation,
            )
            for root in roots
        ]
        lifecycle_edges.extend(
            (
                transition(
                    origin_key,
                    origin_key,
                    "retry",
                    "Retry failed node",
                    "retryable node failure",
                    "attempt N+1",
                ),
                transition(
                    coordinator,
                    origin_key,
                    "revision",
                    "Coordinator revision",
                    "workflow assessment requests revision",
                    next_generation,
                ),
                transition(
                    "exact-sha-validation",
                    origin_key,
                    "validation-remediation",
                    "Validation remediation",
                    "exact-SHA validation fails",
                    next_generation,
                ),
                transition(
                    "independent-acceptance",
                    origin_key,
                    "acceptance-remediation",
                    "Acceptance remediation",
                    "independent acceptance requests remediation",
                    next_generation,
                ),
                transition(
                    "feedback-resolution",
                    origin_key,
                    "feedback",
                    "Feedback generation",
                    "new persisted pull-request feedback",
                    next_generation,
                ),
                transition(
                    "feedback-resolution",
                    terminal_key,
                    "termination",
                    "Close completed run",
                    "pull request closes or merges",
                    "closed run",
                ),
                transition(
                    origin_key,
                    terminal_key,
                    "termination",
                    "Cancel run",
                    "operator or controller cancellation",
                    "canceled run",
                ),
            )
        )
        return [origin, terminal], lifecycle_edges


class WorkflowScheduler:
    def __init__(
        self,
        database: Database,
        *,
        registry: DeterministicOperationRegistry | None = None,
        max_workers: int = 4,
        cancellation_check: Callable[[str], bool] | None = None,
        known_secret_values: Callable[[str], Iterable[str]] | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("workflow max workers must be positive")
        self.database = database
        self.registry = (
            registry or DeterministicOperationRegistry.with_defaults()
        )
        self.max_workers = max_workers
        self.cancellation_check = cancellation_check or (lambda _run_id: False)
        self.known_secret_values = known_secret_values or (lambda _run_id: ())
        self.service = WorkflowService(database, registry=self.registry)

    def advance(
        self,
        run_id: str,
        *,
        agent_executor: Callable[
            [WorkflowNode, dict[str, object]], Mapping[str, object]
        ],
    ) -> RunWorkflowGraph:
        self.service.recover_interrupted(run_id)
        self._reset_failed(run_id)
        self.service.refresh_readiness(run_id)
        while True:
            graph = self.service.active_run_graph(run_id)
            if self.cancellation_check(run_id):
                self.service.cancel_pending(run_id)
                raise WorkflowCanceled(f"workflow for {run_id} was canceled")
            if all(
                node.state in {"succeeded", "skipped"} for node in graph.nodes
            ):
                self.service.mark_succeeded(graph.id)
                return self.service.active_run_graph(run_id)
            ready = self._ready_nodes(graph)
            if not ready:
                failed = [
                    node for node in graph.nodes if node.state == "failed"
                ]
                if failed:
                    error = failed[0].error or {}
                    raise WorkflowExecutionError(
                        str(
                            error.get(
                                "message",
                                f"workflow node {failed[0].stable_key} failed",
                            )
                        )
                    )
                canceled = [
                    node for node in graph.nodes if node.state == "canceled"
                ]
                if canceled:
                    raise WorkflowCanceled(
                        f"workflow for {run_id} was canceled"
                    )
                blocked = [
                    node for node in graph.nodes if node.state == "blocked"
                ]
                if blocked:
                    raise WorkflowExecutionError(
                        f"workflow node {blocked[0].stable_key} is "
                        "dependency-blocked"
                    )
                raise WorkflowExecutionError(
                    "workflow has no durable ready node"
                )
            batch, contended = self._select_batch(ready)
            self.service.record_resource_waits(node.id for node in contended)
            started: list[tuple[WorkflowNode, dict[str, object]]] = []
            failures: list[BaseException] = []
            for node in batch:
                try:
                    inputs = self._inputs(graph, node)
                    self.service.begin_attempt(node.id, inputs)
                    started.append((node, inputs))
                except BaseException as error:
                    self.service.begin_attempt(node.id, {})
                    self.service.fail_attempt(
                        node.id,
                        error,
                        known_secrets=self.known_secret_values(run_id),
                    )
                    failures.append(error)
            if started:
                with ThreadPoolExecutor(max_workers=len(started)) as executor:
                    futures = {
                        executor.submit(
                            self._execute_node, node, inputs, agent_executor
                        ): node
                        for node, inputs in started
                    }
                    for future in as_completed(futures):
                        node = futures[future]
                        try:
                            output = future.result()
                            self.service.complete_attempt(node.id, output)
                        except BaseException as error:
                            self.service.fail_attempt(
                                node.id,
                                error,
                                known_secrets=self.known_secret_values(run_id),
                            )
                            failures.append(error)
            if self.cancellation_check(run_id):
                self.service.cancel_pending(run_id)
                raise WorkflowCanceled(f"workflow for {run_id} was canceled")

            if failures:
                raise WorkflowExecutionError(str(failures[0])) from failures[0]

    def _reset_failed(self, run_id: str) -> None:
        now = _utc_now()
        try:
            graph = self.service.active_run_graph(run_id)
        except KeyError:
            return
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE run_workflow_nodes
                      SET state='pending', error_json=NULL, updated_at=?,
                          started_at=NULL, completed_at=NULL
                    WHERE run_workflow_id=?
                      AND state IN ('failed', 'blocked')""",
                (now, graph.id),
            )
            connection.execute(
                """UPDATE run_workflows
                      SET state='pending', updated_at=?
                    WHERE id=? AND state='failed'""",
                (now, graph.id),
            )
        self.service.refresh_readiness(run_id)

    @staticmethod
    def _ready_nodes(graph: RunWorkflowGraph) -> list[WorkflowNode]:
        return [node for node in graph.nodes if node.state == "ready"]

    def _select_batch(
        self, ready: Sequence[WorkflowNode]
    ) -> tuple[list[WorkflowNode], list[WorkflowNode]]:
        selected: list[WorkflowNode] = []
        contended: list[WorkflowNode] = []
        for node in ready:
            if len(selected) >= self.max_workers:
                break
            if any(
                self._resources_conflict(node, other) for other in selected
            ):
                contended.append(node)
                continue
            selected.append(node)
        return selected, contended

    @staticmethod
    def _resources_conflict(first: WorkflowNode, second: WorkflowNode) -> bool:
        for first_resource in first.resources:
            first_identity, first_access = _resource_identity(first_resource)
            for second_resource in second.resources:
                second_identity, second_access = _resource_identity(
                    second_resource
                )
                if first_identity != second_identity:
                    continue
                if "write" in {first_access, second_access}:
                    return True
        return False

    def _inputs(
        self, graph: RunWorkflowGraph, node: WorkflowNode
    ) -> dict[str, object]:
        by_key = {value.stable_key: value for value in graph.nodes}
        dependency_keys = [
            edge.source
            for edge in graph.edges
            if edge.target == node.stable_key
        ]
        dependency_outputs = {
            key: by_key[key].output or {} for key in dependency_keys
        }
        values = dict(node.parameters)
        for name, raw_binding in node.bindings.items():
            if not isinstance(raw_binding, Mapping):
                values[name] = raw_binding
                continue
            path = raw_binding.get("path")
            if isinstance(raw_binding.get("nodes"), list):
                values[name] = [
                    self._output_path(
                        dependency_outputs.get(str(key), {}), path
                    )
                    for key in raw_binding["nodes"]
                    if str(key) in dependency_outputs
                ]
            elif isinstance(raw_binding.get("node"), str):
                values[name] = self._output_path(
                    dependency_outputs.get(str(raw_binding["node"]), {}), path
                )
            elif "value" in raw_binding:
                values[name] = raw_binding["value"]
        if node.kind == "deterministic" and not node.bindings:
            operation = self.registry.get(node.operation or "")
            required_value = operation.input_schema.get("required", [])
            required = (
                _list(
                    required_value, "deterministic operation required fields"
                )
                if isinstance(required_value, list)
                else []
            )
            if "parameters" in required or "dependencies" in required:
                values = {
                    "parameters": dict(node.parameters),
                    "dependencies": dependency_outputs,
                }
        if node.kind == "agent":
            values["dependencies"] = dependency_outputs
        return values

    @staticmethod
    def _output_path(output: Mapping[str, object], path: object) -> object:
        if path in {None, ""}:
            return dict(output)
        current: object = output
        for component in str(path).split("."):
            if not isinstance(current, Mapping) or component not in current:
                raise ValueError(
                    f"workflow binding path {path} is unavailable"
                )
            current = current[component]
        return current

    def _execute_node(
        self,
        node: WorkflowNode,
        inputs: dict[str, object],
        agent_executor: Callable[
            [WorkflowNode, dict[str, object]], Mapping[str, object]
        ],
    ) -> dict[str, object]:
        if node.kind == "deterministic":
            operation = self.registry.get(node.operation or "")
            if node.operation_version != operation.version_hash:
                raise ValueError(
                    f"workflow node {node.stable_key} operation version "
                    "changed"
                )
            output = self.registry.execute(node.operation or "", inputs)
        else:
            result = agent_executor(node, inputs)
            if not isinstance(result, Mapping):
                raise ValueError(
                    "agent workflow node output must be an object"
                )
            output = dict(result)
        _validate_schema(output, node.expected_output, "workflow node output")
        _validate_safe_value(output, path="workflow node output")
        return output


class WorkflowExecutionEngine:
    def __init__(
        self,
        *,
        database: Database,
        workflow: WorkflowService,
        agent_runner: Callable[[WorkflowNodeContext], Mapping[str, object]],
        operations: DeterministicOperationRegistry,
        assessment_runner: (
            Callable[[dict[str, object]], Mapping[str, object]] | None
        ) = None,
        cancellation_check: Callable[[str], bool] | None = None,
        known_secret_values: Callable[[str], Iterable[str]] | None = None,
        max_workers: int = 4,
        max_generations: int = 5,
    ) -> None:
        self.database = database
        self.workflow = workflow
        self.agent_runner = agent_runner
        self.operations = operations
        self.assessment_runner = assessment_runner
        self.max_generations = max_generations
        self.scheduler = WorkflowScheduler(
            database,
            registry=operations,
            max_workers=max_workers,
            cancellation_check=cancellation_check,
            known_secret_values=known_secret_values,
        )

    def execute(self, run_id: str) -> WorkflowExecutionResult:
        controller_rejection: str | None = None
        rejection_attempts = 0
        while True:
            graph = self.workflow.active_run_graph(run_id)

            def execute_agent(
                node: WorkflowNode, inputs: dict[str, object]
            ) -> Mapping[str, object]:
                dependencies = inputs.get("dependencies", {})
                if not isinstance(dependencies, Mapping):
                    dependencies = {}
                context = WorkflowNodeContext(
                    run_id=run_id,
                    workflow_id=graph.id,
                    generation=graph.generation,
                    node_id=node.id,
                    stable_key=node.stable_key,
                    member_id=node.member_id,
                    member_key=node.member_key,
                    role=node.role,
                    prompt=node.prompt,
                    parameters=node.parameters,
                    bindings=node.bindings,
                    expected_output=node.expected_output,
                    inputs=dict(inputs),
                    resources=node.resources,
                    dependency_outputs={
                        str(key): dict(value)
                        for key, value in dependencies.items()
                        if isinstance(value, Mapping)
                    },
                )
                return self.agent_runner(context)

            completed = self.scheduler.advance(
                run_id, agent_executor=execute_agent
            )
            if self.assessment_runner is None:
                return WorkflowExecutionResult(
                    run_id, completed.id, completed.generation, "completed"
                )
            assessment_context: dict[str, object] = {
                "run_id": run_id,
                "generation": completed.generation,
                "assessment_prompt": completed.assessment_prompt,
                "rationale": completed.rationale,
                "nodes": [
                    {
                        "stable_key": node.stable_key,
                        "kind": node.kind,
                        "member_key": node.member_key,
                        "operation": node.operation,
                        "prompt": node.prompt,
                        "parameters": node.parameters,
                        "bindings": node.bindings,
                        "expected_output": node.expected_output,
                        "resources": list(node.resources),
                        "state": node.state,
                        "output": node.output,
                        "reused": node.reused_from_node_id is not None,
                    }
                    for node in completed.nodes
                ],
                "edges": [
                    {"source": edge.source, "target": edge.target}
                    for edge in completed.edges
                ],
            }
            if controller_rejection is not None:
                assessment_context["controller_rejection"] = (
                    controller_rejection
                )
            assessment = dict(self.assessment_runner(assessment_context))
            outcome: str | None = None
            try:
                outcome = _string(
                    assessment.get("outcome"),
                    "workflow assessment outcome",
                )
                if outcome == "accept":
                    self.workflow.record_assessment(run_id, assessment)
                    return WorkflowExecutionResult(
                        run_id,
                        completed.id,
                        completed.generation,
                        "completed",
                    )
                if outcome != "revise":
                    raise WorkflowExecutionError(
                        "workflow assessment outcome must be accept or revise"
                    )
                if completed.generation >= self.max_generations:
                    raise WorkflowExecutionError(
                        "workflow revision limit reached; retain the current "
                        "executable graph by returning accept with specific "
                        "evidence"
                    )
                raw_nodes = assessment.get("nodes")
                raw_edges = assessment.get("edges")
                if not isinstance(raw_nodes, list) or not isinstance(
                    raw_edges, list
                ):
                    raise WorkflowExecutionError(
                        "workflow revision must include nodes and edges"
                    )
                reason = _string(
                    assessment.get("reason"),
                    "workflow revision reason",
                )
                self.workflow.revise(
                    run_id,
                    reason=reason,
                    assessment=assessment,
                    design={
                        "rationale": completed.rationale,
                        "assessment_prompt": completed.assessment_prompt,
                        "nodes": raw_nodes,
                        "edges": raw_edges,
                    },
                )
            except (ValueError, WorkflowExecutionError) as error:
                if outcome in {"accept", "revise"}:
                    try:
                        self.workflow.record_assessment(run_id, assessment)
                    except ValueError:
                        pass
                rejection_attempts += 1
                controller_rejection = str(error)
                if rejection_attempts >= max(2, self.max_generations):
                    raise WorkflowExecutionError(
                        "workflow assessment remained invalid after "
                        f"controller feedback: {controller_rejection}"
                    ) from error
                continue
            controller_rejection = None
            rejection_attempts = 0
