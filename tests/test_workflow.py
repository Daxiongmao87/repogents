from __future__ import annotations
import copy

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from repogents.database import Database
from repogents.workflow import (
    DeterministicOperation,
    DeterministicOperationRegistry,
    WorkflowScheduler,
    WorkflowService,
    validate_workflow_design,
)

NOW = "2026-01-01T00:00:00Z"
BASE_SHA = "a" * 40


def _members() -> list[dict[str, object]]:
    return [
        {
            "id": "member-lead",
            "stable_key": "coordination",
            "role": "delivery coordinator",
            "execution_class": "lead",
            "coordinates": True,
            "independent_verifier": False,
            "responsibilities": "Coordinate and integrate issue work.",
            "permitted_tools": ["read", "git_diff", "git_commit"],
            "runtime": "mini-swe-agent",
            "model": "test",
            "instructions": "Repository instructions.",
            "action_timeout_seconds": 60,
        },
        {
            "id": "member-a",
            "stable_key": "read-a",
            "role": "first read-only investigator",
            "execution_class": "scout",
            "coordinates": False,
            "independent_verifier": False,
            "responsibilities": "Investigate the first bounded concern.",
            "permitted_tools": ["read", "run"],
            "runtime": "mini-swe-agent",
            "model": "test",
            "instructions": "Repository instructions.",
            "action_timeout_seconds": 60,
        },
        {
            "id": "member-b",
            "stable_key": "read-b",
            "role": "second read-only investigator",
            "execution_class": "scout",
            "coordinates": False,
            "independent_verifier": False,
            "responsibilities": "Investigate the second bounded concern.",
            "permitted_tools": ["read", "run"],
            "runtime": "mini-swe-agent",
            "model": "test",
            "instructions": "Repository instructions.",
            "action_timeout_seconds": 60,
        },
        {
            "id": "member-write-a",
            "stable_key": "write-a",
            "role": "first implementation specialist",
            "execution_class": "implementer",
            "coordinates": False,
            "independent_verifier": False,
            "responsibilities": "Implement the first bounded concern.",
            "permitted_tools": ["read", "write", "run", "git_diff"],
            "runtime": "mini-swe-agent",
            "model": "test",
            "instructions": "Repository instructions.",
            "action_timeout_seconds": 60,
        },
        {
            "id": "member-write-b",
            "stable_key": "write-b",
            "role": "second implementation specialist",
            "execution_class": "implementer",
            "coordinates": False,
            "independent_verifier": False,
            "responsibilities": "Implement the second bounded concern.",
            "permitted_tools": ["read", "write", "run", "git_diff"],
            "runtime": "mini-swe-agent",
            "model": "test",
            "instructions": "Repository instructions.",
            "action_timeout_seconds": 60,
        },
        {
            "id": "member-verifier",
            "stable_key": "verification",
            "role": "independent behavior verifier",
            "execution_class": "verifier",
            "coordinates": False,
            "independent_verifier": True,
            "responsibilities": (
                "Verify the integrated behavior independently."
            ),
            "permitted_tools": ["read", "run", "git_diff"],
            "runtime": "mini-swe-agent",
            "model": "test",
            "instructions": "Repository instructions.",
            "action_timeout_seconds": 60,
        },
    ]


def _agent_node(
    stable_key: str,
    member_key: str,
    prompt: str,
    resources: list[str],
) -> dict[str, object]:
    return {
        "stable_key": stable_key,
        "kind": "agent",
        "member_key": member_key,
        "operation": "",
        "prompt": prompt,
        "parameters": {},
        "bindings": {},
        "resources": resources,
    }


def _design(
    work: tuple[str, ...] = ("read-a", "read-b"),
    *,
    resources: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    resources = resources or {}
    nodes = [
        _agent_node(
            key,
            key,
            f"Investigate {key}; return evidence and an exact handoff.",
            resources.get(key, ["workspace:read"]),
        )
        for key in work
    ]
    nodes.extend(
        [
            {
                "stable_key": "evidence-join",
                "kind": "deterministic",
                "member_key": "",
                "operation": "collect",
                "prompt": "Collect specialist handoffs for the coordinator.",
                "parameters": {},
                "bindings": {
                    "items": {
                        "nodes": list(work),
                        "path": "summary",
                    }
                },
                "resources": [],
            },
            _agent_node(
                "coordination",
                "coordination",
                (
                    "Assess team performance, integrate the handoffs, and "
                    "either retain or revise the workflow with evidence."
                ),
                ["workspace:read"],
            ),
            _agent_node(
                "verification",
                "verification",
                (
                    "Independently verify the integrated result and return "
                    "concrete evidence."
                ),
                ["workspace:read"],
            ),
        ]
    )
    edges = [{"source": key, "target": "evidence-join"} for key in work] + [
        {"source": "evidence-join", "target": "coordination"},
        {"source": "coordination", "target": "verification"},
    ]
    return {
        "rationale": (
            "Run independent repository investigations in parallel, "
            "collect their typed handoffs, then coordinate and verify once."
        ),
        "nodes": nodes,
        "edges": edges,
    }


class WorkflowGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db = Database(Path(self.tempdir.name) / "repogents.sqlite3")
        self.db.initialize()
        self.registry = DeterministicOperationRegistry.with_defaults()
        self.service = WorkflowService(self.db, registry=self.registry)
        self._seed_run()

    def _seed_run(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'repo-node', 'owner', 'repo', 'url',
                           'main', 'ready', ?, ?)""",
                (NOW, NOW),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json,
                    evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, '/tmp/sandbox', '{}',
                           '{}', ?)""",
                (NOW,),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json,
                    design_contract_version, created_at)
                   VALUES ('team-1', 'repo-1', 1, '{}', 2, ?)""",
                (NOW,),
            )
            for member in _members():
                connection.execute(
                    """INSERT INTO team_members
                       (id, team_version_id, stable_key, role, atomic_role,
                        responsibilities, permitted_tools_json, runtime, model,
                        instructions, action_timeout_seconds)
                       VALUES (?, 'team-1', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        member["id"],
                        member["stable_key"],
                        member["execution_class"],
                        member["role"],
                        member["responsibilities"],
                        json.dumps(member["permitted_tools"]),
                        member["runtime"],
                        member["model"],
                        member["instructions"],
                        member["action_timeout_seconds"],
                    ),
                )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title,
                    body, discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'issue-node', 1, 'issue-url',
                           'Issue', 'Body', '[]', ?)""",
                (NOW,),
            )
            connection.execute(
                """INSERT INTO issue_versions
                   (id, issue_id, version, previous_version_id,
                    github_updated_at, content_sha256, title, body,
                    discussion_json, observed_at)
                   VALUES ('issue-version-1', 'issue-1', 1, NULL, ?, ?,
                           'Issue', 'Body', '[]', ?)""",
                (NOW, "b" * 64, NOW),
            )
            connection.execute(
                """UPDATE issues
                      SET current_version_id='issue-version-1'
                    WHERE id='issue-1'"""
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at,
                    issue_version_id)
                   VALUES ('event-1', 'repo-1', 'issue-1', 'event-node', ?,
                           'issue-version-1')""",
                (NOW,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id,
                    intended_base_branch, base_sha, state, created_at,
                    updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'event-1',
                           'sandbox-1', 'team-1', 'main', ?,
                           'implementing', ?, ?)""",
                (BASE_SHA, NOW, NOW),
            )

    def _assign(self, keys: tuple[str, ...]) -> None:
        with self.db.transaction() as connection:
            for key in keys:
                connection.execute(
                    """INSERT INTO agent_assignments
                       (id, run_id, team_member_id, reasoning, assigned_at)
                       SELECT 'assignment-' || stable_key, 'run-1', id,
                              'issue-specific graph selection', ?
                         FROM team_members
                        WHERE team_version_id='team-1' AND stable_key=?""",
                    (NOW, key),
                )

    def test_validates_branched_graph_and_rejects_unsafe_topologies(
        self,
    ) -> None:
        normalized = validate_workflow_design(
            _design(),
            _members(),
            self.registry.catalog(),
        )
        self.assertEqual(
            [node["stable_key"] for node in normalized["nodes"]],
            [
                "read-a",
                "read-b",
                "evidence-join",
                "coordination",
                "verification",
            ],
        )

        cyclic = _design()
        cyclic["edges"] = list(cyclic["edges"]) + [
            {"source": "verification", "target": "read-a"}
        ]
        with self.assertRaisesRegex(ValueError, "acyclic|verifier.*terminal"):
            validate_workflow_design(
                cyclic, _members(), self.registry.catalog()
            )

        unknown_operation = _design()
        unknown_operation["nodes"][2]["operation"] = "inline-python"
        unknown_operation["nodes"][2]["parameters"] = {
            "code": "open('/etc/passwd')"
        }
        with self.assertRaisesRegex(
            ValueError, "registered deterministic operation"
        ):
            validate_workflow_design(
                unknown_operation,
                _members(),
                self.registry.catalog(),
            )

        nonterminal_verifier = _design()
        nonterminal_verifier["edges"] = list(nonterminal_verifier["edges"]) + [
            {"source": "verification", "target": "coordination"}
        ]
        with self.assertRaisesRegex(ValueError, "verifier.*terminal"):
            validate_workflow_design(
                nonterminal_verifier,
                _members(),
                self.registry.catalog(),
            )

    def test_rejects_unsafe_boundary_topologies(
        self,
    ) -> None:
        duplicate_coordinator = _design()
        duplicate_coordinator["nodes"].append(
            _agent_node(
                "coordination-copy",
                "coordination",
                "Coordinate the same handoffs a second time.",
                ["workspace:read"],
            )
        )
        duplicate_coordinator["edges"].append(
            {"source": "coordination-copy", "target": "verification"}
        )
        with self.assertRaisesRegex(ValueError, "exactly one coordinating"):
            validate_workflow_design(
                duplicate_coordinator,
                _members(),
                self.registry.catalog(),
            )

        unreachable = _design()
        unreachable["edges"] = [
            edge
            for edge in unreachable["edges"]
            if edge != {"source": "read-a", "target": "evidence-join"}
        ]
        with self.assertRaisesRegex(
            ValueError, "reach (the coordinating|the independent verifier)"
        ):
            validate_workflow_design(
                unreachable,
                _members(),
                self.registry.catalog(),
            )

        writing_coordinator = _design()
        writing_coordinator["nodes"][3]["resources"] = ["workspace:write"]
        with self.assertRaisesRegex(ValueError, "coordinating.*write"):
            validate_workflow_design(
                writing_coordinator,
                _members(),
                self.registry.catalog(),
            )

        bypass = _design()
        bypass["edges"] = [
            edge
            for edge in bypass["edges"]
            if edge != {"source": "read-a", "target": "evidence-join"}
        ]
        bypass["edges"].append({"source": "read-a", "target": "verification"})
        with self.assertRaisesRegex(ValueError, "coordinating"):
            validate_workflow_design(
                bypass,
                _members(),
                self.registry.catalog(),
            )

        post_coordinator_work = _design(("read-a",))
        post_coordinator_work["nodes"][1]["bindings"] = {}
        post_coordinator_work["edges"] = [
            {"source": "evidence-join", "target": "coordination"},
            {"source": "coordination", "target": "read-a"},
            {"source": "read-a", "target": "verification"},
        ]
        with self.assertRaisesRegex(ValueError, "coordinating"):
            validate_workflow_design(
                post_coordinator_work,
                _members(),
                self.registry.catalog(),
            )

    def test_rejects_executable_shell_and_secret_graph_payloads(self) -> None:
        executable = _design()
        executable["nodes"][0]["parameters"] = {
            "code": "print('not controller-owned')",
        }
        with self.assertRaisesRegex(ValueError, "executable|configuration"):
            validate_workflow_design(
                executable,
                _members(),
                self.registry.catalog(),
            )

        shell_expression = _design()
        shell_expression["nodes"][0]["prompt"] = (
            "Inspect the repository, then execute $(touch /tmp/unsafe)."
        )
        with self.assertRaisesRegex(ValueError, "shell expression"):
            validate_workflow_design(
                shell_expression,
                _members(),
                self.registry.catalog(),
            )

        secret_reference = _design()
        secret_reference["nodes"][0]["prompt"] = (
            "Inspect the repository using secret://PRODUCTION_TOKEN."
        )
        with self.assertRaisesRegex(ValueError, "secret"):
            validate_workflow_design(
                secret_reference,
                _members(),
                self.registry.catalog(),
            )

    def test_readiness_and_failed_dependency_are_durable_states(self) -> None:
        self.service.store_template("team-1", _design(("read-a",)))
        self._assign(("coordination", "read-a", "verification"))
        graph = self.service.compile_run("run-1")

        self.service.refresh_readiness("run-1")
        graph = self.service.active_run_graph("run-1")
        states = {node.stable_key: node.state for node in graph.nodes}
        self.assertEqual(states["read-a"], "ready")
        self.assertEqual(states["evidence-join"], "pending")

        read_node = next(
            node for node in graph.nodes if node.stable_key == "read-a"
        )
        self.service.begin_attempt(read_node.id)
        self.service.fail_attempt(
            read_node.id, RuntimeError("research failed")
        )
        self.service.refresh_readiness("run-1")

        states = {
            node.stable_key: node.state
            for node in self.service.active_run_graph("run-1").nodes
        }
        self.assertEqual(states["read-a"], "failed")
        self.assertEqual(states["evidence-join"], "blocked")
        self.assertEqual(states["coordination"], "blocked")
        self.assertEqual(states["verification"], "blocked")

    def test_persists_template_and_compiles_exact_run_graph(self) -> None:
        template = self.service.store_template("team-1", _design())
        self._assign(("coordination", "read-a", "verification"))
        graph = self.service.compile_run("run-1")

        self.assertEqual(template.team_version_id, "team-1")
        self.assertEqual(graph.generation, 1)
        self.assertEqual(graph.issue_version_id, "issue-version-1")
        self.assertEqual(graph.team_version_id, "team-1")
        self.assertEqual(graph.sandbox_version_id, "sandbox-1")
        self.assertEqual(graph.base_sha, BASE_SHA)
        self.assertEqual(
            {node.stable_key for node in graph.nodes},
            {"read-a", "evidence-join", "coordination", "verification"},
        )
        self.assertEqual(
            self.service.load_template("team-1"),
            template,
        )
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*)
                         FROM run_workflows
                        WHERE run_id='run-1'"""
                ).fetchone()[0],
                1,
            )

    def test_compilation_selects_members_when_node_keys_are_distinct(
        self,
    ) -> None:
        design = _design(("read-a",))
        renamed = {
            "read-a": "repository-research",
            "coordination": "integrate-results",
            "verification": "verify-result",
        }
        for node in design["nodes"]:
            node["stable_key"] = renamed.get(
                str(node["stable_key"]),
                str(node["stable_key"]),
            )
            bindings = node.get("bindings")
            if isinstance(bindings, dict):
                for binding in bindings.values():
                    if isinstance(binding, dict) and isinstance(
                        binding.get("nodes"),
                        list,
                    ):
                        binding["nodes"] = [
                            renamed.get(str(key), str(key))
                            for key in binding["nodes"]
                        ]
        for edge in design["edges"]:
            edge["source"] = renamed.get(
                str(edge["source"]),
                str(edge["source"]),
            )
            edge["target"] = renamed.get(
                str(edge["target"]),
                str(edge["target"]),
            )

        self.service.store_template("team-1", design)
        self._assign(("coordination", "read-a", "verification"))
        graph = self.service.compile_run("run-1")

        self.assertEqual(
            {node.stable_key for node in graph.nodes},
            {
                "repository-research",
                "evidence-join",
                "integrate-results",
                "verify-result",
            },
        )

    def test_compilation_rejects_selected_branch_with_unassigned_agent(
        self,
    ) -> None:
        design = _design(("read-a", "write-a"))
        design["nodes"][0]["stable_key"] = "repository-research"
        design["edges"] = [
            {"source": "repository-research", "target": "write-a"},
            {"source": "write-a", "target": "evidence-join"},
            {"source": "evidence-join", "target": "coordination"},
            {"source": "coordination", "target": "verification"},
        ]
        design["nodes"][2]["parameters"] = {"items": []}
        design["nodes"][2]["bindings"] = {}
        self.service.store_template("team-1", design)
        self._assign(("coordination", "read-a", "verification"))

        with self.assertRaisesRegex(
            ValueError,
            "selected workflow member read-a is disconnected",
        ):
            self.service.compile_run("run-1")

    def test_compilation_rejects_selected_agent_with_unassigned_predecessor(
        self,
    ) -> None:
        design = _design(("read-a", "write-a"))
        design["edges"] = [
            {"source": "read-a", "target": "write-a"},
            {"source": "write-a", "target": "evidence-join"},
            {"source": "evidence-join", "target": "coordination"},
            {"source": "coordination", "target": "verification"},
        ]
        design["nodes"][2]["bindings"]["items"]["nodes"] = ["write-a"]
        self.service.store_template("team-1", design)
        self._assign(("coordination", "write-a", "verification"))

        with self.assertRaisesRegex(
            ValueError,
            "selected workflow member write-a requires unassigned member read-a",
        ):
            self.service.compile_run("run-1")
    def test_compilation_drops_deterministic_nodes_from_unselected_branches(
        self,
    ) -> None:
        design = _design()
        design["nodes"].insert(
            2,
            {
                "stable_key": "read-b-transform",
                "kind": "deterministic",
                "member_key": "",
                "operation": "collect",
                "prompt": "Normalize only the read-b specialist handoff.",
                "parameters": {},
                "bindings": {
                    "items": {
                        "nodes": ["read-b"],
                        "path": "summary",
                    }
                },
                "resources": [],
            },
        )
        design["nodes"][3]["bindings"]["items"]["nodes"] = [
            "read-a",
            "read-b-transform",
        ]
        design["edges"] = [
            {"source": "read-a", "target": "evidence-join"},
            {"source": "read-b", "target": "read-b-transform"},
            {"source": "read-b-transform", "target": "evidence-join"},
            {"source": "evidence-join", "target": "coordination"},
            {"source": "coordination", "target": "verification"},
        ]
        self.service.store_template("team-1", design)
        self._assign(("coordination", "read-a", "verification"))

        graph = self.service.compile_run("run-1")

        self.assertEqual(
            {node.stable_key for node in graph.nodes},
            {"read-a", "evidence-join", "coordination", "verification"},
        )


    def test_legacy_team_compiles_without_rewriting_team_version(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE team_versions
                      SET design_contract_version=2
                    WHERE id='team-1'"""
            )
        self._assign(("coordination", "write-a", "verification"))
        graph = self.service.compile_run("run-1")

        self.assertEqual(
            [node.stable_key for node in graph.nodes],
            ["write-a", "coordination", "verification"],
        )
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT design_contract_version
                         FROM team_versions
                        WHERE id='team-1'"""
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*)
                         FROM team_workflow_templates
                        WHERE team_version_id='team-1'"""
                ).fetchone()[0],
                0,
            )

    def test_custom_typed_deterministic_operation_fails_closed(self) -> None:
        registry = DeterministicOperationRegistry.with_defaults()
        registry.register(
            DeterministicOperation(
                name="append-suffix",
                input_schema={
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "suffix": {"type": "string"},
                    },
                    "required": ["value", "suffix"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                resources=(),
                pure=True,
                handler=lambda values: {
                    "value": str(values["value"]) + str(values["suffix"])
                },
            )
        )
        registry.execute("append-suffix", {"value": "ready", "suffix": "!"})
        self.assertEqual(
            registry.execute(
                "append-suffix",
                {"value": "ready", "suffix": "!"},
            ),
            {"value": "ready!"},
        )
        with self.assertRaisesRegex(ValueError, "input"):
            registry.execute("append-suffix", {"value": "ready"})

        bad = DeterministicOperation(
            name="bad-output",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            resources=(),
            pure=True,
            handler=lambda values: {"value": 4},
        )
        registry.register(bad)
        with self.assertRaisesRegex(ValueError, "output"):
            registry.execute("bad-output", {})

    def test_scheduler_runs_parallel_reads_and_serializes_writers(
        self,
    ) -> None:
        self.service.store_template("team-1", _design())
        self._assign(("coordination", "read-a", "read-b", "verification"))
        self.service.compile_run("run-1")
        self.service.refresh_readiness("run-1")
        self.assertEqual(
            [
                node.stable_key
                for node in self.service.active_run_graph("run-1").nodes
                if node.state == "ready"
            ],
            ["read-a", "read-b"],
        )
        barrier = threading.Barrier(2)
        observed: list[str] = []
        observed_lock = threading.Lock()

        def read_executor(
            node: object, inputs: dict[str, object]
        ) -> dict[str, object]:
            del inputs
            key = str(getattr(node, "stable_key"))
            if key in {"read-a", "read-b"}:
                barrier.wait(timeout=2)
            with observed_lock:
                observed.append(key)
            return {"summary": f"{key} complete"}

        WorkflowScheduler(
            self.db,
            registry=self.registry,
            max_workers=2,
        ).advance("run-1", agent_executor=read_executor)
        self.assertIn("read-a", observed)
        self.assertIn("read-b", observed)

        with self.db.transaction() as connection:
            connection.execute("DELETE FROM run_workflow_attempts")
            connection.execute("DELETE FROM run_workflow_edges")
            connection.execute("DELETE FROM run_workflow_nodes")
            connection.execute("DELETE FROM run_workflows")
            connection.execute("DELETE FROM team_workflow_edges")
            connection.execute("DELETE FROM team_workflow_nodes")
            connection.execute("DELETE FROM team_workflow_templates")
            connection.execute("DELETE FROM agent_assignments")
        writer_design = _design(
            ("write-a", "write-b"),
            resources={
                "write-a": ["checkout:write"],
                "write-b": ["checkout:write"],
            },
        )
        self.service.store_template("team-1", writer_design)
        self._assign(("coordination", "write-a", "write-b", "verification"))
        self.service.compile_run("run-1")
        active = 0
        maximum = 0
        active_lock = threading.Lock()

        def write_executor(
            node: object, inputs: dict[str, object]
        ) -> dict[str, object]:
            nonlocal active, maximum
            del inputs
            key = str(getattr(node, "stable_key"))
            if key.startswith("write-"):
                with active_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with active_lock:
                    active -= 1
            return {"summary": f"{key} complete"}

        WorkflowScheduler(
            self.db,
            registry=self.registry,
            max_workers=2,
        ).advance("run-1", agent_executor=write_executor)
        self.assertEqual(maximum, 1)

    def test_checkout_writer_serializes_validation_reader(self) -> None:
        design = _design(
            ("write-a", "write-b"),
            resources={
                "write-a": ["checkout:write"],
                "write-b": ["validation:read"],
            },
        )
        self.service.store_template("team-1", design)
        self._assign(("coordination", "write-a", "write-b", "verification"))
        self.service.compile_run("run-1")
        active = 0
        maximum = 0
        active_lock = threading.Lock()

        def executor(
            node: object, inputs: dict[str, object]
        ) -> dict[str, object]:
            nonlocal active, maximum
            del inputs
            key = str(getattr(node, "stable_key"))
            if key in {"write-a", "write-b"}:
                with active_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with active_lock:
                    active -= 1
            return {"summary": f"{key} complete"}

        WorkflowScheduler(
            self.db,
            registry=self.registry,
            max_workers=2,
        ).advance("run-1", agent_executor=executor)

        self.assertEqual(maximum, 1)

    def test_runtime_output_may_describe_commands_as_evidence(self) -> None:
        self.service.store_template("team-1", _design(("read-a",)))
        self._assign(("coordination", "read-a", "verification"))
        self.service.compile_run("run-1")

        graph = WorkflowScheduler(
            self.db,
            registry=self.registry,
            max_workers=2,
        ).advance(
            "run-1",
            agent_executor=lambda node, inputs: {
                "summary": "Verified with `python -m unittest`.",
            },
        )

        self.assertEqual(graph.state, "succeeded")

    def test_nonconflicting_writer_and_issue_reader_can_overlap(self) -> None:
        design = _design(
            ("write-a", "read-a"),
            resources={
                "write-a": ["checkout:write"],
                "read-a": ["issue:read"],
            },
        )
        self.service.store_template("team-1", design)
        self._assign(("coordination", "write-a", "read-a", "verification"))
        self.service.compile_run("run-1")
        barrier = threading.Barrier(2)
        overlapping: set[str] = set()
        lock = threading.Lock()

        def executor(
            node: object, inputs: dict[str, object]
        ) -> dict[str, object]:
            del inputs
            key = str(getattr(node, "stable_key"))
            if key in {"write-a", "read-a"}:
                barrier.wait(timeout=2)
                with lock:
                    overlapping.add(key)
            return {"summary": f"{key} complete"}

        WorkflowScheduler(
            self.db,
            registry=self.registry,
            max_workers=2,
        ).advance("run-1", agent_executor=executor)

        self.assertEqual(overlapping, {"write-a", "read-a"})

    def test_restart_retries_only_abandoned_running_node(self) -> None:
        self.service.store_template("team-1", _design(("read-a",)))
        self._assign(("coordination", "read-a", "verification"))
        graph = self.service.compile_run("run-1")
        node = next(
            value for value in graph.nodes if value.stable_key == "read-a"
        )
        self.service.begin_attempt(node.id)

        recovered = WorkflowService(
            self.db,
            registry=self.registry,
        ).recover_interrupted("run-1")
        self.assertEqual(recovered, (node.id,))
        calls: list[str] = []

        def executor(
            value: object, inputs: dict[str, object]
        ) -> dict[str, object]:
            del inputs
            calls.append(str(getattr(value, "stable_key")))
            return {"summary": "complete"}

        WorkflowScheduler(self.db, registry=self.registry).advance(
            "run-1",
            agent_executor=executor,
        )
        self.assertEqual(calls.count("read-a"), 1)
        attempts = self.service.project_run("run-1")["generations"][0][
            "nodes"
        ][0]["attempts"]
        self.assertEqual(
            [attempt["state"] for attempt in attempts],
            ["interrupted", "succeeded"],
        )

    def test_leader_revision_is_immutable_and_reuses_only_exact_nodes(
        self,
    ) -> None:
        self.service.store_template("team-1", _design())
        self._assign(("coordination", "read-a", "read-b", "verification"))
        graph = self.service.compile_run("run-1")
        read_a = next(
            node for node in graph.nodes if node.stable_key == "read-a"
        )
        self.service.begin_attempt(read_a.id)
        self.service.complete_attempt(
            read_a.id, {"summary": "stable evidence"}
        )

        revised = _design()
        revised["nodes"][1]["prompt"] = (
            "Recheck read-b using the failed handoff evidence and report "
            "an exact correction."
        )
        generation = self.service.revise(
            "run-1",
            reason="read-b produced an incomplete handoff",
            assessment={
                "outcome": "revise",
                "evidence": (
                    "read-b attempt lacked the required source locations"
                ),
            },
            design=revised,
        )

        self.assertEqual(generation.generation, 2)
        by_key = {node.stable_key: node for node in generation.nodes}
        self.assertEqual(by_key["read-a"].state, "succeeded")
        self.assertEqual(
            by_key["read-a"].output, {"summary": "stable evidence"}
        )
        self.assertEqual(by_key["read-b"].state, "ready")
        projected = self.service.project_run("run-1")
        self.assertEqual(len(projected["generations"]), 2)
        original, active = projected["generations"]
        self.assertEqual(
            original["assessment"]["evidence"],
            "read-b attempt lacked the required source locations",
        )
        self.assertEqual(original["assessments"], [original["assessment"]])
        self.assertIsNone(active["assessment"])
        self.assertEqual(active["assessments"], [])
        self.assertEqual(original["active"], False)
        self.assertEqual(active["active"], True)

        self.service.record_assessment(
            "run-1",
            {
                "outcome": "accept",
                "evidence": "The revised specialist handoff is complete.",
            },
        )
        active = self.service.project_run("run-1")["generations"][1]
        self.assertEqual(active["assessment"]["outcome"], "accept")
        self.assertEqual(active["assessments"], [active["assessment"]])

    def test_revision_can_change_topology_and_safe_parameters(
        self,
    ) -> None:
        self.service.store_template("team-1", _design())
        self._assign(("coordination", "read-a", "read-b", "verification"))
        graph = self.service.compile_run("run-1")
        for key in ("read-a", "read-b"):
            node = next(value for value in graph.nodes if value.stable_key == key)
            self.service.begin_attempt(node.id)
            self.service.complete_attempt(
                node.id,
                {"summary": f"{key} evidence"},
            )

        revised = _design()
        revised["edges"].append({"source": "read-a", "target": "read-b"})
        revised["nodes"][1]["parameters"] = {"focus": "documentation"}
        generation = self.service.revise(
            "run-1",
            reason="sequence the dependent research",
            assessment={
                "outcome": "revise",
                "evidence": "read-b depends on read-a evidence.",
            },
            design=revised,
        )

        by_key = {node.stable_key: node for node in generation.nodes}
        self.assertIsNotNone(by_key["read-a"].reused_from_node_id)
        self.assertIsNone(by_key["read-b"].reused_from_node_id)
        self.assertEqual(
            by_key["read-b"].parameters,
            {"focus": "documentation"},
        )
        self.assertIn(
            ("read-a", "read-b"),
            {(edge.source, edge.target) for edge in generation.edges},
        )

        unsafe = _design()
        unsafe["nodes"][0]["resources"] = ["external:github"]
        with self.assertRaisesRegex(ValueError, "resource"):
            self.service.revise(
                "run-1",
                reason="broaden authority",
                assessment={"outcome": "revise", "evidence": "none"},
                design=unsafe,
            )
        self.assertEqual(
            self.service.active_run_graph("run-1").generation,
            2,
        )

    def test_noop_revision_fails_atomically_and_keeps_active_generation(
        self,
    ) -> None:
        self.service.store_template("team-1", _design())
        self._assign(("coordination", "read-a", "read-b", "verification"))
        original = self.service.compile_run("run-1")

        with self.assertRaisesRegex(ValueError, "change.*executable"):
            self.service.revise(
                "run-1",
                reason="repeat the same graph",
                assessment={
                    "outcome": "revise",
                    "evidence": "No executable adjustment was proposed.",
                },
                design=_design(),
            )

        active = self.service.active_run_graph("run-1")
        self.assertEqual(active.id, original.id)
        self.assertEqual(active.generation, 1)
        self.assertTrue(active.active)
        self.assertEqual(len(self.service.project_run("run-1")["generations"]), 1)

    def test_revision_may_add_only_newly_assigned_stored_members(self) -> None:
        self.service.store_template("team-1", _design())
        self._assign(("coordination", "read-a", "verification"))
        self.service.compile_run("run-1")

        self._assign(("read-b",))
        expanded = self.service.revise(
            "run-1",
            reason="assigned repository evidence is now required",
            assessment={
                "outcome": "revise",
                "evidence": "The coordinator expanded the durable assignment.",
            },
            design=_design(),
        )
        self.assertIn(
            "read-b",
            {node.stable_key for node in expanded.nodes},
        )

        unassigned = _design(
            ("read-a", "write-a"),
            resources={"write-a": ["workspace:write"]},
        )
        with self.assertRaisesRegex(ValueError, "broaden member authority"):
            self.service.revise(
                "run-1",
                reason="unassigned writer requested",
                assessment={
                    "outcome": "revise",
                    "evidence": "No durable writer assignment exists.",
                },
                design=unassigned,
            )

    def test_operation_version_change_prevents_stale_output_reuse(
        self,
    ) -> None:
        self.service.store_template("team-1", _design(("read-a",)))
        self._assign(("coordination", "read-a", "verification"))
        graph = self.service.compile_run("run-1")
        read_node = next(
            node for node in graph.nodes if node.stable_key == "read-a"
        )
        self.service.begin_attempt(read_node.id)
        self.service.complete_attempt(
            read_node.id, {"summary": "stable evidence"}
        )
        self.service.refresh_readiness("run-1")
        join = next(
            node
            for node in self.service.active_run_graph("run-1").nodes
            if node.stable_key == "evidence-join"
        )
        self.service.begin_attempt(join.id)
        self.service.complete_attempt(
            join.id,
            {"items": ["stable evidence"], "summary": "stable evidence"},
        )

        replacement = DeterministicOperationRegistry()
        replacement.register(
            DeterministicOperation(
                name="collect",
                version="2",
                input_schema=copy.deepcopy(
                    self.registry.catalog()["collect"]["input_schema"]
                ),
                output_schema=copy.deepcopy(
                    self.registry.catalog()["collect"]["output_schema"]
                ),
                resources=(),
                pure=True,
                handler=lambda values: {
                    "items": list(values["items"]),
                    "summary": "\n".join(
                        str(item) for item in values["items"]
                    ),
                },
            )
        )
        revised = WorkflowService(self.db, registry=replacement).revise(
            "run-1",
            reason="controller operation implementation changed",
            assessment={
                "outcome": "revise",
                "evidence": "collect version two is now active",
            },
            design=_design(("read-a",)),
        )

        by_key = {node.stable_key: node for node in revised.nodes}
        self.assertEqual(by_key["read-a"].state, "succeeded")
        self.assertEqual(by_key["evidence-join"].state, "ready")
        self.assertIsNone(by_key["evidence-join"].reused_from_node_id)
        self.assertNotEqual(
            graph.nodes[1].operation_version,
            by_key["evidence-join"].operation_version,
        )

    def test_projection_adds_controller_cycle_without_mutating_dag(
        self,
    ) -> None:
        design = _design()
        self.service.store_template("team-1", design)
        self._assign(("coordination", "read-a", "read-b", "verification"))
        graph = self.service.compile_run("run-1")

        template = self.service.project_template("team-1")
        projected = self.service.project_run("run-1")["generations"][0]

        expected_nodes = [node["stable_key"] for node in design["nodes"]]
        expected_edges = {
            (edge["source"], edge["target"]) for edge in design["edges"]
        }
        self.assertEqual(
            [node["stable_key"] for node in template["nodes"]],
            expected_nodes,
        )
        self.assertEqual(
            {
                (edge["source"], edge["target"])
                for edge in template["edges"]
            },
            expected_edges,
        )
        self.assertEqual(
            [node["stable_key"] for node in projected["nodes"]],
            expected_nodes,
        )
        self.assertEqual(
            {
                (edge["source"], edge["target"])
                for edge in projected["edges"]
            },
            expected_edges,
        )
        self.assertEqual(
            {(edge.source, edge.target) for edge in graph.edges},
            expected_edges,
        )

        template_system = {
            node["stable_key"]: node
            for node in template["system_boundaries"]
        }
        run_system = {
            node["stable_key"]: node
            for node in projected["system_boundaries"]
        }
        self.assertEqual(
            set(run_system),
            {"controller:run-contract", "controller:terminal-outcome"},
        )
        self.assertEqual(
            template_system["controller:run-contract"]["contract"],
            {
                "mode": "template",
                "run_id": None,
                "issue_version_id": None,
                "team_version_id": "team-1",
                "sandbox_version_id": None,
                "base_sha": None,
                "generation": "N",
            },
        )
        self.assertEqual(
            run_system["controller:run-contract"]["contract"],
            {
                "mode": "run",
                "run_id": "run-1",
                "issue_version_id": "issue-version-1",
                "team_version_id": "team-1",
                "sandbox_version_id": "sandbox-1",
                "base_sha": BASE_SHA,
                "generation": 1,
            },
        )
        self.assertEqual(
            run_system["controller:terminal-outcome"]["run_state"],
            "implementing",
        )

        transitions = projected["lifecycle_edges"]
        transition_types = {edge["type"] for edge in transitions}
        self.assertTrue(
            {
                "activation",
                "retry",
                "revision",
                "validation-remediation",
                "acceptance-remediation",
                "feedback",
                "termination",
            }.issubset(transition_types)
        )
        self.assertEqual(
            {
                edge["target"]
                for edge in transitions
                if edge["type"] == "activation"
            },
            {"read-a", "read-b"},
        )
        for edge in transitions:
            self.assertTrue(edge["projection_only"])
            if edge["type"] in {
                "retry",
                "revision",
                "validation-remediation",
                "acceptance-remediation",
                "feedback",
            }:
                self.assertEqual(edge["target"], "controller:run-contract")
                expected = (
                    "attempt N+1"
                    if edge["type"] == "retry"
                    else "generation 2"
                )
                self.assertEqual(edge["next_unit"], expected)

        encoded = json.dumps(
            {
                "template": template["system_boundaries"],
                "run": projected["system_boundaries"],
                "transitions": transitions,
            },
            sort_keys=True,
        )
        self.assertNotIn("secret", encoded.lower())

    def test_projection_is_stable_and_omits_executable_or_secret_values(
        self,
    ) -> None:
        self.service.store_template("team-1", _design())
        self._assign(("coordination", "read-a", "read-b", "verification"))
        self.service.compile_run("run-1")
        template = self.service.project_template("team-1")
        run = self.service.project_run("run-1")

        self.assertEqual(
            [node["column"] for node in template["nodes"]],
            [0, 0, 1, 2, 3],
        )
        self.assertEqual(
            [node["row"] for node in template["nodes"][:2]],
            [0, 1],
        )
        encoded = json.dumps(
            {"template": template, "run": run}, sort_keys=True
        )
        self.assertNotIn("handler", encoded)
        self.assertNotIn("executable", encoded)
        self.assertNotIn("secret", encoded.lower())
        self.assertEqual(run["active_generation"], 1)


if __name__ == "__main__":
    unittest.main()
