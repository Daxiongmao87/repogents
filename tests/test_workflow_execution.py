from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from repogents.database import Database
from repogents.workflow import (
    DeterministicOperation,
    DeterministicOperationRegistry,
    WorkflowCanceled,
    WorkflowExecutionEngine,
    WorkflowExecutionError,
    WorkflowNodeContext,
    WorkflowService,
)


class RecordingAgentRunner:
    def __init__(self, *, fail_once: str | None = None) -> None:
        self.fail_once = fail_once
        self.calls: list[str] = []
        self.call_count: dict[str, int] = {}
        self.parallel_branches_observed = False
        self._barrier = threading.Barrier(2)
        self._lock = threading.Lock()

    def __call__(self, context: WorkflowNodeContext) -> dict[str, object]:
        with self._lock:
            self.calls.append(context.stable_key)
            self.call_count[context.stable_key] = (
                self.call_count.get(context.stable_key, 0) + 1
            )
        if context.stable_key in {"research-a", "research-b"}:
            try:
                self._barrier.wait(timeout=2)
                self.parallel_branches_observed = True
            except threading.BrokenBarrierError as error:
                raise AssertionError(
                    "independent branches did not overlap"
                ) from error
        if (
            context.stable_key == self.fail_once
            and self.call_count[context.stable_key] == 1
        ):
            raise RuntimeError(f"transient {context.stable_key} failure")
        return {
            "summary": context.stable_key,
            "dependencies": sorted(context.dependency_outputs),
        }


class WorkflowExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.database = Database(self.root / "db.sqlite3")
        self.database.initialize()
        now = "2026-01-01T00:00:00Z"
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, inputs_json, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo',
                           'https://github.com/owner/repo', 'main', 'ready',
                           '{}', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json,
                    evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, ?, '{}', '{}', ?)""",
                (str(self.root / "sandbox"), now),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, '{}', ?)""",
                (now,),
            )
            members = (
                ("lead-id", "lead", "lead", "delivery coordinator"),
                ("a-id", "research-a", "implementer", "researcher a"),
                ("b-id", "research-b", "implementer", "researcher b"),
                ("verify-id", "verification", "verifier", "verifier"),
            )
            for member_id, stable_key, role, atomic_role in members:
                connection.execute(
                    """INSERT INTO team_members
                       (id, team_version_id, stable_key, role, atomic_role,
                        responsibilities, permitted_tools_json, runtime, model,
                        instructions)
                       VALUES (?, 'team-1', ?, ?, ?, 'work', '["read"]',
                               'test', 'test', 'work')""",
                    (member_id, stable_key, role, atomic_role),
                )
            connection.execute("""UPDATE repositories
                   SET current_sandbox_version_id='sandbox-1',
                       current_team_version_id='team-1'
                   WHERE id='repo-1'""")
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title,
                    body, discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 1,
                           'https://github.com/owner/repo/issues/1',
                           'Build graph', 'Issue body', '[]', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO issue_versions
                   (id, issue_id, version, previous_version_id,
                    github_updated_at, content_sha256, title, body,
                    discussion_json, observed_at)
                   VALUES ('issue-version-1', 'issue-1', 1, NULL, ?, ?,
                           'Build graph', 'Issue body', '[]', ?)""",
                (now, "c" * 64, now),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id,
                    issue_version_id, applied_at)
                   VALUES ('activation-1', 'repo-1', 'issue-1', 'event-1',
                           'issue-version-1', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id,
                    intended_base_branch, base_sha, state, checkout_path,
                    run_path, created_at, updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'activation-1',
                           'sandbox-1', 'team-1', 'main', ?,
                           'implementing', ?, ?, ?, ?)""",
                (
                    "a" * 40,
                    str(self.root / "checkout"),
                    str(self.root / "run"),
                    now,
                    now,
                ),
            )
        self.operations = self.registry()
        self.workflow = WorkflowService(
            self.database,
            registry=self.operations,
        )
        self.workflow.store_template("team-1", self.design())
        self.workflow.compile_run("run-1", "issue-version-1")

    @staticmethod
    def design() -> dict[str, object]:
        output = {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
            "additionalProperties": True,
        }
        return {
            "rationale": (
                "Research in parallel, merge, coordinate, and verify."
            ),
            "assessment_prompt": (
                "Assess evidence quality and revise weak nodes."
            ),
            "nodes": [
                {
                    "stable_key": "research-a",
                    "kind": "agent",
                    "member_key": "research-a",
                    "prompt": "Research subsystem A.",
                    "expected_output": output,
                    "resources": ["workspace:read"],
                },
                {
                    "stable_key": "research-b",
                    "kind": "agent",
                    "member_key": "research-b",
                    "prompt": "Research subsystem B.",
                    "expected_output": output,
                    "resources": ["workspace:read"],
                },
                {
                    "stable_key": "merge",
                    "kind": "deterministic",
                    "operation": "merge-summaries",
                    "prompt": "Merge research outputs.",
                    "parameters": {"separator": ","},
                    "expected_output": {
                        "type": "object",
                        "required": ["summary"],
                        "properties": {"summary": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "resources": [],
                },
                {
                    "stable_key": "lead",
                    "kind": "agent",
                    "member_key": "lead",
                    "prompt": "Integrate the merged evidence.",
                    "expected_output": output,
                    "resources": ["workspace:read"],
                },
                {
                    "stable_key": "verification",
                    "kind": "agent",
                    "member_key": "verification",
                    "prompt": "Verify the integrated result.",
                    "expected_output": output,
                    "resources": ["workspace:read"],
                },
            ],
            "edges": [
                {"source": "research-a", "target": "merge"},
                {"source": "research-b", "target": "merge"},
                {"source": "merge", "target": "lead"},
                {"source": "lead", "target": "verification"},
            ],
        }

    @staticmethod
    def registry() -> DeterministicOperationRegistry:
        registry = DeterministicOperationRegistry()
        registry.register(
            DeterministicOperation(
                key="merge-summaries",
                input_schema={
                    "type": "object",
                    "required": ["parameters", "dependencies"],
                    "properties": {
                        "parameters": {"type": "object"},
                        "dependencies": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                    "additionalProperties": False,
                },
                handler=lambda value: {
                    "summary": value["parameters"]["separator"].join(
                        sorted(value["dependencies"])
                    )
                },
            )
        )
        return registry

    def test_ready_nodes_run_concurrently_and_join_through_typed_code(
        self,
    ) -> None:
        runner = RecordingAgentRunner()
        engine = WorkflowExecutionEngine(
            database=self.database,
            workflow=self.workflow,
            agent_runner=runner,
            operations=self.operations,
            max_workers=2,
        )

        result = engine.execute("run-1")

        self.assertTrue(runner.parallel_branches_observed)
        self.assertEqual(result.state, "completed")
        projected = self.workflow.project_run("run-1")
        generation = projected["generations"][0]
        self.assertEqual(
            [node["state"] for node in generation["nodes"]],
            ["succeeded", "succeeded", "succeeded", "succeeded", "succeeded"],
        )
        merged = next(
            node
            for node in generation["nodes"]
            if node["stable_key"] == "merge"
        )
        self.assertEqual(
            merged["output"], {"summary": "research-a,research-b"}
        )
        self.assertEqual(len(merged["attempts"]), 1)

    def test_retry_resumes_only_the_failed_node_and_its_dependents(
        self,
    ) -> None:
        runner = RecordingAgentRunner(fail_once="lead")
        engine = WorkflowExecutionEngine(
            database=self.database,
            workflow=self.workflow,
            agent_runner=runner,
            operations=self.operations,
            max_workers=2,
        )

        with self.assertRaisesRegex(
            WorkflowExecutionError, "transient lead failure"
        ):
            engine.execute("run-1")
        before_retry = self.workflow.project_run("run-1")["generations"][0][
            "nodes"
        ]
        self.assertEqual(
            {node["stable_key"]: node["state"] for node in before_retry},
            {
                "research-a": "succeeded",
                "research-b": "succeeded",
                "merge": "succeeded",
                "lead": "failed",
                "verification": "blocked",
            },
        )

        result = engine.execute("run-1")

        self.assertEqual(result.state, "completed")
        self.assertEqual(runner.call_count["research-a"], 1)
        self.assertEqual(runner.call_count["research-b"], 1)
        self.assertEqual(runner.call_count["lead"], 2)
        self.assertEqual(runner.call_count["verification"], 1)
        lead = next(
            node
            for node in self.workflow.project_run("run-1")["generations"][0][
                "nodes"
            ]
            if node["stable_key"] == "lead"
        )
        self.assertEqual(
            [attempt["state"] for attempt in lead["attempts"]],
            ["failed", "succeeded"],
        )

    def test_cancellation_preserves_completed_outputs_and_halts_downstream(
        self,
    ) -> None:
        runner = RecordingAgentRunner()
        cancel = threading.Event()

        def cancel_after_research(
            context: WorkflowNodeContext,
        ) -> dict[str, object]:
            result = runner(context)
            if {"research-a", "research-b"}.issubset(runner.call_count):
                cancel.set()
            return result

        engine = WorkflowExecutionEngine(
            database=self.database,
            workflow=self.workflow,
            agent_runner=cancel_after_research,
            operations=self.operations,
            cancellation_check=lambda _run_id: cancel.is_set(),
            max_workers=2,
        )

        with self.assertRaises(WorkflowCanceled):
            engine.execute("run-1")

        nodes = self.workflow.project_run("run-1")["generations"][0]["nodes"]
        states = {node["stable_key"]: node["state"] for node in nodes}
        self.assertEqual(states["research-a"], "succeeded")
        self.assertEqual(states["research-b"], "succeeded")
        self.assertEqual(states["merge"], "canceled")
        self.assertEqual(states["lead"], "canceled")
        self.assertEqual(states["verification"], "canceled")

    def test_cancellation_wins_over_an_inflight_node_failure(self) -> None:
        cancel = threading.Event()
        barrier = threading.Barrier(2)

        def stop_during_research(
            context: WorkflowNodeContext,
        ) -> dict[str, object]:
            if context.stable_key in {"research-a", "research-b"}:
                barrier.wait(timeout=2)
            if context.stable_key == "research-a":
                cancel.set()
                raise RuntimeError("node observed run cancellation")
            return {"summary": context.stable_key}

        engine = WorkflowExecutionEngine(
            database=self.database,
            workflow=self.workflow,
            agent_runner=stop_during_research,
            operations=self.operations,
            cancellation_check=lambda _run_id: cancel.is_set(),
            max_workers=2,
        )

        with self.assertRaises(WorkflowCanceled):
            engine.execute("run-1")

        nodes = self.workflow.project_run("run-1")["generations"][0]["nodes"]
        states = {node["stable_key"]: node["state"] for node in nodes}
        self.assertEqual(states["research-a"], "canceled")
        self.assertEqual(states["research-b"], "succeeded")
        self.assertEqual(states["merge"], "canceled")
        self.assertEqual(states["lead"], "canceled")
        self.assertEqual(states["verification"], "canceled")

    def test_leader_revision_versions_graph_and_reuses_unchanged_nodes(
        self,
    ) -> None:
        runner = RecordingAgentRunner()
        assessments = [
            {
                "outcome": "revise",
                "evidence": "Verification needs an explicit scope check.",
                "reason": "strengthen verification",
                "nodes": [
                    *self.design()["nodes"][:-1],
                    {
                        **self.design()["nodes"][-1],
                        "prompt": "Verify behavior and changed-file scope.",
                    },
                ],
                "edges": self.design()["edges"],
            },
            {
                "outcome": "accept",
                "evidence": "Behavior and scope evidence are complete.",
            },
        ]
        engine = WorkflowExecutionEngine(
            database=self.database,
            workflow=self.workflow,
            agent_runner=runner,
            operations=self.operations,
            assessment_runner=lambda _context: assessments.pop(0),
            max_workers=2,
        )

        result = engine.execute("run-1")

        self.assertEqual(result.state, "completed")
        projected = self.workflow.project_run("run-1")
        self.assertEqual(projected["active_generation"], 2)
        first, second = projected["generations"]
        self.assertEqual(second["assessment"]["outcome"], "accept")
        self.assertEqual(
            {node["stable_key"] for node in second["nodes"] if node["reused"]},
            {"research-a", "research-b", "merge", "lead"},
        )
        self.assertEqual(runner.call_count["research-a"], 1)
        self.assertEqual(runner.call_count["lead"], 1)
        self.assertEqual(runner.call_count["verification"], 2)
        self.assertIn(
            "changed-file scope",
            next(
                node["prompt"]
                for node in second["nodes"]
                if node["stable_key"] == "verification"
            ),
        )

    def test_invalid_revision_is_returned_to_coordinator_before_retry(
        self,
    ) -> None:
        runner = RecordingAgentRunner()
        assessments = [
            {
                "outcome": "revise",
                "evidence": "The graph should change.",
                "reason": "repeat the existing graph",
                "nodes": self.design()["nodes"],
                "edges": self.design()["edges"],
            },
            {
                "outcome": "revise",
                "evidence": "Verification needs a corrected objective.",
                "reason": "strengthen verification",
                "nodes": [
                    *self.design()["nodes"][:-1],
                    {
                        **self.design()["nodes"][-1],
                        "prompt": "Verify behavior and exact changed-file scope.",
                    },
                ],
                "edges": self.design()["edges"],
            },
            {
                "outcome": "accept",
                "evidence": "The corrected graph completed.",
            },
        ]
        contexts: list[dict[str, object]] = []

        def assess(context: dict[str, object]) -> dict[str, object]:
            contexts.append(context)
            return assessments.pop(0)

        result = WorkflowExecutionEngine(
            database=self.database,
            workflow=self.workflow,
            agent_runner=runner,
            operations=self.operations,
            assessment_runner=assess,
            max_workers=2,
        ).execute("run-1")

        self.assertEqual(result.generation, 2)
        self.assertIn(
            "change an executable node or dependency",
            contexts[1]["controller_rejection"],
        )
        projected = self.workflow.project_run("run-1")
        self.assertEqual(len(projected["generations"]), 2)
        self.assertEqual(
            projected["generations"][0]["assessments"][0]["outcome"],
            "revise",
        )

    def test_exhausted_revision_bound_preserves_last_valid_generation(
        self,
    ) -> None:
        runner = RecordingAgentRunner()
        contexts: list[dict[str, object]] = []

        def assess(context: dict[str, object]) -> dict[str, object]:
            contexts.append(context)
            return {
                "outcome": "revise",
                "evidence": "The coordinator still requests another graph.",
                "reason": "request another revision",
                "nodes": self.design()["nodes"],
                "edges": self.design()["edges"],
            }

        engine = WorkflowExecutionEngine(
            database=self.database,
            workflow=self.workflow,
            agent_runner=runner,
            operations=self.operations,
            assessment_runner=assess,
            max_workers=2,
            max_generations=1,
        )

        with self.assertRaisesRegex(
            WorkflowExecutionError,
            "remained invalid after controller feedback",
        ):
            engine.execute("run-1")

        self.assertIn("revision limit reached", contexts[1]["controller_rejection"])
        graph = self.workflow.active_run_graph("run-1")
        self.assertEqual(graph.generation, 1)
        self.assertEqual(graph.state, "succeeded")
        self.assertEqual(len(graph.assessments), 2)
        self.assertEqual(runner.call_count["research-a"], 1)
        self.assertEqual(runner.call_count["verification"], 1)

    def test_unregistered_or_schema_invalid_code_node_fails_closed(
        self,
    ) -> None:
        runner = RecordingAgentRunner()
        empty_registry = DeterministicOperationRegistry()
        engine = WorkflowExecutionEngine(
            database=self.database,
            workflow=self.workflow,
            agent_runner=runner,
            operations=empty_registry,
            max_workers=2,
        )

        with self.assertRaisesRegex(WorkflowExecutionError, "not registered"):
            engine.execute("run-1")
        merge = next(
            node
            for node in self.workflow.project_run("run-1")["generations"][0][
                "nodes"
            ]
            if node["stable_key"] == "merge"
        )
        self.assertEqual(merge["state"], "failed")
        self.assertNotIn("lead", runner.calls)
        self.assertEqual(
            json.loads(merge["attempts"][0]["error_json"])["type"],
            "ValueError",
        )


if __name__ == "__main__":
    unittest.main()
