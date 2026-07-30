from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from repogents.database import Database
from repogents.execution import (
    AgentToolExecutor,
    ExecutionService,
    MiniSweModelRuntime,
    ScriptedRuntime,
)
from repogents.lifecycle import RunLifecycle, RunState
from repogents.sandbox import RunLayout, SandboxManager, SandboxPolicy
from repogents.team import TeamService
from repogents.specification import SpecificationService, SpecificationUnavailable
from repogents.workflow import WorkflowService


class NoActivationClient:
    def list_ready_events(self, owner: str, name: str) -> list[object]:
        return []

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        return "a" * 40


class NoCheckoutManager:
    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        return None


class RuleAwareRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def next_action(self, context: str, state_directory: Path) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            target = (
                "allowed.py" if "Do not modify locked.py" in context else "locked.py"
            )
            return {
                "action": "replace",
                "path": target,
                "old": "return 1",
                "new": "return 2",
                "count": 1,
            }
        if self.calls == 2:
            return {
                "action": "replace",
                "path": "value.py",
                "old": "return 1",
                "new": "return 2",
                "count": 1,
            }
        return {"action": "finish", "summary": "repository instruction honored"}


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.checkout = (
            self.data_root / "repositories" / "repo-1" / "runs" / "run-1" / "checkout"
        )
        self.checkout.mkdir(parents=True)
        (self.checkout / "value.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        (self.checkout / "test_value.py").write_text(
            "import unittest\nfrom value import value\n\n"
            "class ValueTest(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(value(), 2)\n",
            encoding="utf-8",
        )
        self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "base",
        )
        self.base_sha = self._git("rev-parse", "HEAD").strip()
        self.sandbox_root = self.data_root / "repositories" / "repo-1" / "sandbox" / "1"
        self.sandbox_root.mkdir(parents=True)
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        run_root = self.checkout.parent
        policy = {
            "persistent_root": str(self.sandbox_root),
            "allowed_host_paths": [],
            "allowed_services": [],
            "secret_bindings": [],
        }
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'repo-url', 'main', 'ready', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, ?, ?, ?, ?)""",
                (
                    str(self.sandbox_root),
                    json.dumps(policy),
                    json.dumps(
                        {"instruction_files": [], "summary": "small Python fixture"}
                    ),
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO validation_commands
                   (id, sandbox_version_id, position, command_json, source, required)
                   VALUES ('validation-command-1', 'sandbox-1', 0, ?, 'fixture', 1)""",
                (
                    json.dumps(
                        [
                            "python3",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            ".",
                            "-p",
                            "test_*.py",
                        ]
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, '{}', ?)""",
                (now,),
            )
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES
                   ('lead-1', 'team-1', 'lead', 'lead',
                    'repository delivery planner', 'Coordinate and integrate the result',
                    '["read","git_diff","git_commit"]',
                    'mini-swe-agent', 'test/lead', ''),
                   ('implementation-1', 'team-1', 'implementation', 'implementer',
                    'python implementation maintainer', 'Implement the source change',
                    '["read","write","run","git_diff"]',
                    'mini-swe-agent', 'test/implementation', ''),
                   ('verification-1', 'team-1', 'verification', 'verifier',
                    'repository behavior reviewer', 'Independently verify behavior',
                    '["read","run","git_diff"]',
                    'mini-swe-agent', 'test/verification', '')""")
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 3, 'issue-url',
                           'Return two', 'Make value() return 2', '[]', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-1', 'repo-1', 'issue-1', 'event-1', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, checkout_path, run_path, created_at, updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'activation-1',
                           'sandbox-1', 'team-1', 'main', ?, 'queued', ?, ?, ?, ?)""",
                (self.base_sha, str(self.checkout), str(run_root), now, now),
            )
            connection.executemany(
                """INSERT INTO agent_assignments
                   (id, run_id, team_member_id, reasoning, assigned_at)
                   VALUES (?, 'run-1', ?, 'Explicit fixture assignment', ?)""",
                (
                    ("fixture-lead-assignment", "lead-1", now),
                    ("fixture-implementation-assignment", "implementation-1", now),
                ),
            )
            connection.execute(
                """UPDATE repositories SET current_sandbox_version_id='sandbox-1',
                                             current_team_version_id='team-1'
                   WHERE id='repo-1'"""
            )
        self.sandbox = SandboxManager()
        self.lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )
        self.issue_version_id = self.lifecycle.current_issue_version("run-1")
        self.specifications = SpecificationService(self.db)
        self.specification = self.specifications.submit(
            run_id="run-1",
            author_member_id="lead-1",
            issue_version_id=self.issue_version_id,
            items=self.specification_items(),
            reason="Specify the issue contract before fixture execution.",
        )
        self.specification_review = self.specifications.record_review(
            run_id="run-1",
            specification_revision_id=str(self.specification["id"]),
            reviewer_member_id="verification-1",
            reviewer_model="test/verification",
            rubric_version=1,
            verdict="approved",
            summary="The fixture specification is complete and verifiable.",
            findings=[],
        )

    @staticmethod
    def specification_items() -> list[dict[str, object]]:
        return [
            {
                "key": "return-two",
                "title": "Return the requested value",
                "objective": "The public value function returns two.",
                "acceptance_criteria": [
                    {
                        "key": "value-returns-two",
                        "requirement": "Calling value returns the requested integer.",
                        "expected": "value() returns 2.",
                    }
                ],
                "verification": [
                    {
                        "key": "call-value",
                        "criterion_keys": ["value-returns-two"],
                        "scenario": "Call value() and compare its return value with 2.",
                    }
                ],
            }
        ]

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.checkout,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout

    def service(
        self,
        actions: list[dict[str, object]],
    ) -> tuple[ExecutionService, ScriptedRuntime]:
        implementation_runtime = ScriptedRuntime(actions)
        lead_runtime = ScriptedRuntime(
            [
                {"action": "finish", "summary": "integrated member work"}
                for _ in range(10)
            ]
        )
        verifier_runtime = ScriptedRuntime(
            [
                {"action": "finish", "summary": "candidate independently approved"}
                for _ in range(10)
            ]
        )

        def runtime_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            if stored_model == "test/lead":
                return lead_runtime
            if stored_model == "test/verification":
                return verifier_runtime
            return implementation_runtime

        return (
            ExecutionService(
                database=self.db,
                lifecycle=self.lifecycle,
                teams=TeamService(self.db),
                sandbox=self.sandbox,
                runtime_factory=runtime_factory,
                max_actions=20,
                max_revision_cycles=3,
            ),
            implementation_runtime,
        )
    def test_coordinator_specification_precedes_assignment_and_source_mutation(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM run_specification_reviews WHERE run_id='run-1'"
            )
            connection.execute("DELETE FROM agent_assignments WHERE run_id='run-1'")
            connection.execute("DELETE FROM run_specification_revisions")
            connection.execute(
                """UPDATE team_members
                   SET permitted_tools_json='["read","write","git_diff"]'
                   WHERE id='lead-1'"""
            )
        specifying = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": ["lead", "implementation", "verification"],
                    "reason": "This assignment must wait for a durable specification.",
                },
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 9",
                    "count": 1,
                },
                {
                    "action": "specify",
                    "issue_version_id": self.issue_version_id,
                    "items": self.specification_items(),
                    "reason": "Define observable completion before assigning work.",
                },
            ]
        )
        unused = ScriptedRuntime([])

        def first_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return specifying if stored_model == "test/lead" else unused

        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=first_factory,
            max_actions=5,
        )

        self.assertIsNone(first.execute("run-1"))

        self.assertIn("return 1", (self.checkout / "value.py").read_text())
        self.assertEqual(
            TeamService(self.db).assignments_for_run("run-1"),
            (),
        )
        current = self.specifications.require_current(
            "run-1",
            self.issue_version_id,
        )
        self.assertEqual(current["revision"], 1)
        history = (
            self.checkout.parent / "agent-state" / "action-history.json"
        ).read_text(encoding="utf-8")
        self.assertIn("specification", history)

        reviewing = ScriptedRuntime(
            [
                {
                    "action": "review_specification",
                    "specification_revision_id": str(current["id"]),
                    "verdict": "approved",
                    "findings": [],
                },
                {
                    "action": "review_specification",
                    "specification_revision_id": str(current["id"]),
                    "verdict": "approved",
                    "summary": (
                        "The specification covers the issue with observable criteria."
                    ),
                    "findings": [],
                }
            ]
        )

        def review_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return reviewing if stored_model == "test/verification" else unused

        reviewer = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=review_factory,
            max_actions=3,
        )

        self.assertIsNone(reviewer.execute("run-1"))
        approved = self.specifications.require_approved(
            "run-1",
            self.issue_version_id,
        )
        self.assertEqual(approved["review"]["verdict"], "approved")
        self.assertEqual(len(reviewing.contexts), 2)
        self.assertEqual(
            TeamService(self.db).assignments_for_run("run-1"),
            (),
        )
        self.assertIn("semantic issue coverage", reviewing.contexts[0])
        self.assertIn("repository evidence", reviewing.contexts[0])
        self.assertIn("return 1", (self.checkout / "value.py").read_text())

        assigning = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": ["lead", "implementation", "verification"],
                    "reason": "The approved issue contract selects the stored delivery team.",
                }
            ]
        )

        def assignment_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return assigning if stored_model == "test/lead" else unused

        restarted = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=assignment_factory,
            max_actions=3,
        )

        self.assertIsNone(restarted.execute("run-1"))

        assignments = TeamService(self.db).assignments_for_run("run-1")
        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead", "implementation", "verification"],
        )
        context = assigning.contexts[0]
        self.assertIn('"atomic_specification"', context)
        self.assertIn('"review"', context)
        self.assertIn(
            "Do not create planning, specification, coordination, or status files",
            context,
        )


    def test_rejected_specification_review_requires_coordinator_revision(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM run_specification_reviews WHERE run_id='run-1'"
            )
            connection.execute("DELETE FROM agent_assignments WHERE run_id='run-1'")
        reviewing = ScriptedRuntime(
            [
                {
                    "action": "review_specification",
                    "specification_revision_id": str(self.specification["id"]),
                    "verdict": "rejected",
                    "summary": "The output boundary is incomplete.",
                    "findings": [
                        {
                            "key": "missing-output-boundary",
                            "category": "coverage",
                            "severity": "error",
                            "summary": "Constrain unrelated output.",
                            "item_keys": ["return-two"],
                        }
                    ],
                }
            ]
        )
        empty = ScriptedRuntime([])

        def review_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return reviewing if stored_model == "test/verification" else empty

        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=review_factory,
            max_actions=3,
        )
        self.assertIsNone(service.execute("run-1"))
        with self.assertRaises(SpecificationUnavailable):
            self.specifications.require_approved("run-1", self.issue_version_id)

        next_items = self.specification_items()
        next_items[0]["objective"] = (
            "The public value function returns two without unrelated output."
        )
        revising = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": ["lead", "implementation", "verification"],
                    "reason": "This must wait for a corrected specification.",
                },
                {
                    "action": "specify",
                    "issue_version_id": self.issue_version_id,
                    "items": next_items,
                    "reason": "Address the independent verifier finding.",
                },
            ]
        )

        def revision_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return revising if stored_model == "test/lead" else empty

        restarted = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=revision_factory,
            max_actions=4,
        )
        self.assertIsNone(restarted.execute("run-1"))

        current = self.specifications.require_current(
            "run-1",
            self.issue_version_id,
        )
        self.assertEqual(current["revision"], 2)
        self.assertIsNone(current["review"])
        self.assertEqual(
            TeamService(self.db).assignments_for_run("run-1"),
            (),
        )
        self.assertIn("missing-output-boundary", revising.contexts[0])
        self.assertIn("must revise", revising.contexts[0])
        self.assertIn("return 1", (self.checkout / "value.py").read_text())


    def test_new_issue_version_requires_matching_specification_before_mutation(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            prior = connection.execute(
                "SELECT * FROM issue_versions WHERE id=?",
                (self.issue_version_id,),
            ).fetchone()
            assert prior is not None
            connection.execute(
                """INSERT INTO issue_versions
                   (id, issue_id, version, previous_version_id,
                    github_updated_at, content_sha256, title, body,
                    discussion_json, observed_at)
                   VALUES ('issue-version-2', 'issue-1', 2, ?, ?,
                           ?, 'Return three', 'Make value() return 3',
                           '[]', ?)""",
                (
                    self.issue_version_id,
                    "2026-01-02T00:00:00Z",
                    "c" * 64,
                    "2026-01-02T00:00:00Z",
                ),
            )
            connection.execute(
                """UPDATE issues
                   SET current_version_id='issue-version-2',
                       title='Return three',
                       body='Make value() return 3',
                       updated_at='2026-01-02T00:00:00Z'
                   WHERE id='issue-1'"""
            )
        revised_items = self.specification_items()
        revised_items[0]["objective"] = "The public value function returns three."
        specifying = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 3",
                    "count": 1,
                },
                {
                    "action": "specify",
                    "issue_version_id": "issue-version-2",
                    "items": revised_items,
                    "reason": "Bind the contract to the revised issue requirements.",
                },
            ]
        )
        unused = ScriptedRuntime([])

        def runtime_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return specifying if stored_model == "test/lead" else unused

        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=runtime_factory,
            max_actions=4,
        )

        self.assertIsNone(service.execute("run-1"))

        current = self.specifications.require_current(
            "run-1",
            "issue-version-2",
        )
        self.assertEqual(current["revision"], 2)
        self.assertIsNone(current["review"])
        self.assertEqual(len(self.specifications.history("run-1")), 2)
        self.assertIn("return 1", (self.checkout / "value.py").read_text())

    def test_feedback_context_requires_durable_specification_reconciliation(
        self,
    ) -> None:
        context = (
            "Pull-request feedback confirms the current behavior but requires "
            "the complete issue contract to remain authoritative."
        )
        reconciling = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 9",
                    "count": 1,
                },
                {
                    "action": "specify",
                    "issue_version_id": self.issue_version_id,
                    "items": self.specification_items(),
                    "reason": (
                        "The new feedback does not change the observable contract."
                    ),
                },
            ]
        )
        unused = ScriptedRuntime([])

        def reconciliation_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return reconciling if stored_model == "test/lead" else unused

        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=reconciliation_factory,
            max_actions=4,
        )

        self.assertIsNone(first.execute("run-1", additional_context=context))
        self.assertIn("return 1", (self.checkout / "value.py").read_text())
        self.assertEqual(len(self.specifications.history("run-1")), 1)
        self.assertEqual(len(self.specifications.review_history("run-1")), 1)
        context_sha256 = hashlib.sha256(context.encode("utf-8")).hexdigest()
        binding = self.specifications.context_binding(
            "run-1",
            self.issue_version_id,
            context_sha256,
        )
        self.assertIsNotNone(binding)
        self.assertEqual(
            binding["specification_revision_id"],
            self.specification["id"],
        )
        self.assertIn("reconcile", reconciling.contexts[0].lower())

        with self.db.transaction() as connection:
            connection.execute("DELETE FROM agent_assignments WHERE run_id='run-1'")
        assigning = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": ["lead", "implementation", "verification"],
                    "reason": "Use the stored team for the reconciled contract.",
                }
            ]
        )

        def restarted_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return assigning if stored_model == "test/lead" else unused

        restarted = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=restarted_factory,
            max_actions=3,
        )

        self.assertIsNone(restarted.execute("run-1", additional_context=context))
        self.assertEqual(
            [
                assignment.member.stable_key
                for assignment in TeamService(self.db).assignments_for_run("run-1")
            ],
            ["lead", "implementation", "verification"],
        )
        self.assertNotIn("reconcile", assigning.contexts[0].lower())
        self.assertEqual(len(self.specifications.history("run-1")), 1)
        self.assertEqual(len(self.specifications.review_history("run-1")), 1)

    def test_changed_feedback_specification_requires_exact_revision_approval(
        self,
    ) -> None:
        context = (
            "Review feedback adds an explicit requirement to preserve unrelated "
            "command output."
        )
        revised_items = self.specification_items()
        revised_items[0]["objective"] = (
            "The public value function returns two and preserves unrelated output."
        )
        revising = ScriptedRuntime(
            [
                {
                    "action": "specify",
                    "issue_version_id": self.issue_version_id,
                    "items": revised_items,
                    "reason": "Incorporate the new feedback requirement.",
                }
            ]
        )
        unused = ScriptedRuntime([])

        def lead_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return revising if stored_model == "test/lead" else unused

        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lead_factory,
            max_actions=3,
        )
        self.assertIsNone(first.execute("run-1", additional_context=context))
        second = self.specifications.require_current(
            "run-1",
            self.issue_version_id,
        )
        self.assertEqual(second["revision"], 2)
        self.assertIsNone(second["review"])
        self.assertIn("return 1", (self.checkout / "value.py").read_text())

        rejecting = ScriptedRuntime(
            [
                {
                    "action": "review_specification",
                    "specification_revision_id": str(second["id"]),
                    "verdict": "rejected",
                    "summary": "The preservation boundary is not independently observable.",
                    "findings": [
                        {
                            "key": "preservation-not-observable",
                            "category": "observability",
                            "severity": "error",
                            "summary": (
                                "Add an explicit expected observation for preserved output."
                            ),
                            "item_keys": ["return-two"],
                        }
                    ],
                }
            ]
        )

        def rejection_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return rejecting if stored_model == "test/verification" else unused

        reviewer = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=rejection_factory,
            max_actions=3,
        )
        self.assertIsNone(reviewer.execute("run-1", additional_context=context))
        self.assertEqual(
            self.specifications.require_current(
                "run-1",
                self.issue_version_id,
            )["review"]["verdict"],
            "rejected",
        )
        self.assertIn("return 1", (self.checkout / "value.py").read_text())

        corrected_items = self.specification_items()
        corrected_items[0]["objective"] = (
            "The public value function returns two without altering other output."
        )
        corrected_items[0]["acceptance_criteria"][0]["expected"] = (
            "value() returns 2 and unrelated command output is unchanged."
        )
        correcting = ScriptedRuntime(
            [
                {
                    "action": "specify",
                    "issue_version_id": self.issue_version_id,
                    "items": corrected_items,
                    "reason": "Make the feedback preservation boundary observable.",
                }
            ]
        )

        def correction_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return correcting if stored_model == "test/lead" else unused

        coordinator = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=correction_factory,
            max_actions=3,
        )
        self.assertIsNone(coordinator.execute("run-1", additional_context=context))
        third = self.specifications.require_current(
            "run-1",
            self.issue_version_id,
        )
        self.assertEqual(third["revision"], 3)
        self.assertIsNone(third["review"])
        self.assertIn("return 1", (self.checkout / "value.py").read_text())

        approving = ScriptedRuntime(
            [
                {
                    "action": "review_specification",
                    "specification_revision_id": str(third["id"]),
                    "verdict": "approved",
                    "summary": (
                        "The corrected specification is complete and observable."
                    ),
                    "findings": [],
                }
            ]
        )

        def approval_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return approving if stored_model == "test/verification" else unused

        approver = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=approval_factory,
            max_actions=3,
        )
        self.assertIsNone(approver.execute("run-1", additional_context=context))
        self.assertEqual(
            self.specifications.require_approved(
                "run-1",
                self.issue_version_id,
            )["id"],
            third["id"],
        )
        self.assertIn("return 1", (self.checkout / "value.py").read_text())

        executor, implementation = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {
                    "action": "finish",
                    "summary": "implemented the approved feedback revision",
                },
            ]
        )
        source_sha = executor.execute("run-1", additional_context=context)

        self.assertIsNotNone(source_sha)
        self.assertIn("return 2", (self.checkout / "value.py").read_text())
        self.assertIn(
            "without altering other output",
            implementation.contexts[0],
        )
        context_sha256 = hashlib.sha256(context.encode("utf-8")).hexdigest()
        self.assertEqual(
            self.specifications.context_binding(
                "run-1",
                self.issue_version_id,
                context_sha256,
            )["specification_revision_id"],
            third["id"],
        )

    def test_durable_action_history_write_signals_activity(self) -> None:
        service, _ = self.service([])
        layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        revision = self.db.activity_revision

        service._store_transcript(layout, ["Lead inspected src/app.py"])

        self.assertEqual(
            json.loads(
                (layout.agent_state / "action-history.json").read_text(encoding="utf-8")
            ),
            ["Lead inspected src/app.py"],
        )
        self.assertGreater(
            self.db.wait_for_activity_change(revision, timeout=0),
            revision,
        )

    def test_workflow_node_resources_restrict_member_tools_before_sandbox_use(
        self,
    ) -> None:
        member = next(
            value
            for value in TeamService(self.db).load("team-1").members
            if value.stable_key == "implementation"
        )
        policy = SandboxPolicy(persistent_root=self.sandbox_root)
        layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        tools = AgentToolExecutor(self.sandbox)
        attempts = (
            (
                {"action": "read", "path": "value.py"},
                ("issue:read",),
            ),
            (
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "1",
                    "new": "2",
                },
                ("workspace:read",),
            ),
            (
                {"action": "run", "argv": ["python3", "-V"]},
                ("workspace:read",),
            ),
            (
                {"action": "git_diff"},
                ("workspace:read",),
            ),
        )
        with mock.patch.object(self.sandbox, "run") as sandbox_run:
            for action, resources in attempts:
                with self.subTest(action=action["action"]):
                    with self.assertRaisesRegex(
                        PermissionError,
                        "workflow node resource",
                    ):
                        tools.execute(
                            member,
                            policy,
                            layout,
                            action,
                            workflow_resources=resources,
                        )
            sandbox_run.assert_not_called()

    def test_workflow_diff_claim_runs_fixed_read_only_diff(self) -> None:
        member = next(
            value
            for value in TeamService(self.db).load("team-1").members
            if value.stable_key == "implementation"
        )
        policy = SandboxPolicy(persistent_root=self.sandbox_root)
        layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        tools = AgentToolExecutor(self.sandbox)
        result = mock.Mock(
            canceled=False,
            returncode=0,
            stdout="diff --git a/value.py b/value.py\n",
            stderr="",
            timed_out=False,
            log_path=layout.logs / "diff.json",
        )
        with mock.patch.object(
            self.sandbox,
            "run",
            return_value=result,
        ) as sandbox_run:
            output = tools.execute(
                member,
                policy,
                layout,
                {"action": "git_diff"},
                workflow_resources=("diff:read",),
            )

        self.assertIn("diff --git", output)
        self.assertEqual(
            sandbox_run.call_args.args[2],
            (
                "git",
                "--no-pager",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--",
            ),
        )
        self.assertFalse(sandbox_run.call_args.kwargs["checkout_writable"])

    def test_run_action_scopes_dependency_services_to_one_command(self) -> None:
        member = next(
            value
            for value in TeamService(self.db).load("team-1").members
            if value.stable_key == "implementation"
        )
        policy = SandboxPolicy(
            persistent_root=self.sandbox_root,
            allowed_services=("pypi.org:443",),
        )
        layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        tools = AgentToolExecutor(self.sandbox)
        result = mock.Mock(
            canceled=False,
            returncode=0,
            stdout="dependency-ready",
            stderr="",
            timed_out=False,
            log_path=layout.logs / "dependency.json",
        )
        with mock.patch.object(
            self.sandbox,
            "run",
            return_value=result,
        ) as sandbox_run:
            output = tools.execute(
                member,
                policy,
                layout,
                {
                    "action": "run",
                    "argv": ["python3", "-V"],
                    "dependency_services": [
                        "cdn.playwright.dev:443",
                        "pypi.org:443",
                    ],
                },
            )

        effective_policy = sandbox_run.call_args.args[0]
        self.assertEqual(json.loads(output)["stdout"], "dependency-ready")
        self.assertEqual(policy.allowed_services, ("pypi.org:443",))
        self.assertEqual(
            effective_policy.allowed_services,
            ("pypi.org:443", "cdn.playwright.dev:443"),
        )

    def test_run_action_rejects_unsafe_dependency_services_before_execution(
        self,
    ) -> None:
        member = next(
            value
            for value in TeamService(self.db).load("team-1").members
            if value.stable_key == "implementation"
        )
        policy = SandboxPolicy(persistent_root=self.sandbox_root)
        layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        tools = AgentToolExecutor(self.sandbox)
        invalid_values: tuple[object, ...] = (
            "cdn.playwright.dev:443",
            ["*.playwright.dev:443"],
            ["cdn.playwright.dev:22"],
            [f"dependency-{index}.example.test:443" for index in range(17)],
        )
        result = mock.Mock(
            canceled=False,
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            log_path=layout.logs / "unexpected.json",
        )
        with mock.patch.object(
            self.sandbox,
            "run",
            return_value=result,
        ) as sandbox_run:
            for dependency_services in invalid_values:
                with self.subTest(dependency_services=dependency_services):
                    with self.assertRaisesRegex(
                        ValueError,
                        "dependency_services",
                    ):
                        tools.execute(
                            member,
                            policy,
                            layout,
                            {
                                "action": "run",
                                "argv": ["python3", "-V"],
                                "dependency_services": dependency_services,
                            },
                        )
        sandbox_run.assert_not_called()

    def test_agent_prompt_exposes_run_local_dependency_retrieval(self) -> None:
        properties = MiniSweModelRuntime._RESPONSE_SCHEMA["properties"]

        self.assertIn("dependency_services", properties)
        self.assertIn("dependency_services", MiniSweModelRuntime._SYSTEM_PROMPT)
        self.assertIn(
            "/run-data/dependency-delta",
            MiniSweModelRuntime._SYSTEM_PROMPT,
        )

    def test_workflow_assessment_can_expand_assignment_and_continue(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES ('research-1', 'team-1', 'research', 'scout',
                           'repository investigator',
                           'Inspect the newly relevant concern',
                           '["read","run"]', 'mini-swe-agent',
                           'test/research', '')"""
            )
        runtime = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": [
                        "lead",
                        "implementation",
                        "verification",
                        "research",
                    ],
                    "reason": "Feedback requires repository investigation.",
                },
                {
                    "action": "finish",
                    "summary": "revised the workflow assignment",
                    "output": {
                        "outcome": "revise",
                        "evidence": "Research is now durably assigned.",
                    },
                },
            ]
        )
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda runtime, model, timeout: runtime,
            max_actions=10,
        )
        team = TeamService(self.db).load("team-1")
        lead = next(member for member in team.members if member.coordinates)
        layout = RunLayout.create(self.data_root, "repo-1", "run-1")

        outcome, yielded = service._agent_cycle(
            runtime,
            lead,
            SandboxPolicy(persistent_root=self.sandbox_root),
            layout,
            "Assess the workflow.",
            [],
            (),
            set(),
            allow_assignment=True,
            continue_after_assignment=True,
        )

        self.assertFalse(yielded)
        self.assertEqual(outcome["outcome"], "revise")
        self.assertIn(
            "research",
            {
                assignment.member.stable_key
                for assignment in TeamService(self.db).assignments_for_run(
                    "run-1"
                )
            },
        )

    def _amend_base(self, files: dict[str, str]) -> None:
        for relative, content in files.items():
            path = self.checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "--amend",
            "-qm",
            "base",
        )
        self.base_sha = self._git("rev-parse", "HEAD").strip()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE runs SET base_sha=? WHERE id='run-1'",
                (self.base_sha,),
            )

    def _set_validation_command(self, command: list[str]) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE validation_commands
                   SET command_json=?
                   WHERE id='validation-command-1'""",
                (json.dumps(command),),
            )

    def _seed_default_delta_baseline(self) -> None:
        with self.db.transaction() as connection:
            command_json = connection.execute("""SELECT command_json
                   FROM validation_commands
                   WHERE id='validation-command-1'""").fetchone()[0]
            connection.execute(
                """INSERT INTO validation_baselines
                   (id, run_id, validation_command_id, command_json,
                    base_sha, mode, started_at, completed_at, exit_status,
                    log_path, findings_json)
                   VALUES ('baseline-1', 'run-1', 'validation-command-1', ?,
                           ?, 'delta', '2026-01-01T00:00:00Z',
                           '2026-01-01T00:00:01Z', 1,
                           '/logs/baseline.json', ?)""",
                (
                    command_json,
                    self.base_sha,
                    json.dumps(["unittest|fail|" "test_value (test_value.ValueTest)"]),
                ),
            )

    def test_exact_base_validation_is_persisted_before_the_first_agent_action(
        self,
    ) -> None:
        test = self

        class BaselineAwareRuntime:
            contexts: list[str] = []

            def next_action(
                self,
                context: str,
                state_directory: Path,
            ) -> dict[str, object]:
                del state_directory
                self.contexts.append(context)
                with test.db.connect() as connection:
                    test.assertEqual(
                        connection.execute("""SELECT COUNT(*)
                               FROM validation_baselines
                               WHERE run_id='run-1'""").fetchone()[0],
                        1,
                    )
                return {"action": "block", "reason": "fixture observation complete"}

        with self.db.transaction() as connection:
            connection.execute("""DELETE FROM agent_assignments
                   WHERE run_id='run-1' AND team_member_id='implementation-1'""")
        runtime = BaselineAwareRuntime()
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: runtime,
        )

        self.assertIsNone(service.execute("run-1"))

        with self.db.connect() as connection:
            baseline = connection.execute(
                """SELECT base_sha, mode, exit_status, findings_json
                   FROM validation_baselines
                   WHERE run_id='run-1'"""
            ).fetchone()
        self.assertEqual(baseline["base_sha"], self.base_sha)
        self.assertEqual(baseline["mode"], "delta")
        self.assertEqual(baseline["exit_status"], 1)
        self.assertTrue(json.loads(baseline["findings_json"]))
        self.assertEqual(len(runtime.contexts), 1)

    def test_failed_exact_base_head_probe_remains_retryable(self) -> None:
        service, _ = self.service([])
        failure = mock.Mock(
            returncode=1,
            stdout="",
            stderr="bwrap: Can't find source path /missing/browser",
        )

        with mock.patch.object(service, "_git", return_value=failure):
            with self.assertRaisesRegex(
                RuntimeError,
                "validation baseline exact-base HEAD probe failed:.*missing/browser",
            ):
                service.execute("run-1")

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")

    def test_failed_exact_base_status_probe_remains_retryable(self) -> None:
        service, _ = self.service([])
        head = mock.Mock(
            returncode=0,
            stdout=f"{self.base_sha}\n",
            stderr="",
        )
        failure = mock.Mock(
            returncode=1,
            stdout="",
            stderr="bwrap: Can't find source path /missing/browser",
        )

        with mock.patch.object(service, "_git", side_effect=(head, failure)):
            with self.assertRaisesRegex(
                RuntimeError,
                "validation baseline exact-base status probe failed:.*missing/browser",
            ):
                service.execute("run-1")

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")

    def test_post_baseline_probe_failure_resets_before_retry(self) -> None:
        self._set_validation_command(
            [
                "python3",
                "-c",
                (
                    "from pathlib import Path; "
                    "marker = Path('/run-data/temp/mutated-once'); "
                    "target = Path('value.py'); "
                    "target.write_text('def value():\\n    return 99\\n', "
                    "encoding='utf-8') if not marker.exists() else None; "
                    "marker.touch(exist_ok=True)"
                ),
            ]
        )
        service, _ = self.service([])
        layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        policy = SandboxPolicy(persistent_root=self.sandbox_root)
        run = self.lifecycle.get_run("run-1")
        original_git = service._git
        head_calls = 0

        def fail_post_command_head(
            policy: SandboxPolicy,
            layout: RunLayout,
            arguments: tuple[str, ...],
            *,
            allow_failure: bool = False,
        ):
            nonlocal head_calls
            if arguments == ("rev-parse", "HEAD"):
                head_calls += 1
                if head_calls == 2:
                    return mock.Mock(
                        returncode=1,
                        stdout="",
                        stderr="transient post-command probe failure",
                    )
            return original_git(
                policy,
                layout,
                arguments,
                allow_failure=allow_failure,
            )

        with mock.patch.object(
            service,
            "_git",
            side_effect=fail_post_command_head,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "validation baseline exact-base HEAD probe failed:"
                ".*post-command probe failure",
            ):
                service._ensure_validation_baselines(
                    run,
                    "sandbox-1",
                    policy,
                    layout,
                    (),
                    set(),
                )

        self.assertEqual(
            self._git("status", "--porcelain", "--untracked-files=no"),
            "",
        )
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), self.base_sha)

        service._ensure_validation_baselines(
            run,
            "sandbox-1",
            policy,
            layout,
            (),
            set(),
        )

        with self.db.connect() as connection:
            baseline = connection.execute(
                """SELECT base_sha, mode, exit_status
                   FROM validation_baselines WHERE run_id='run-1'"""
            ).fetchone()
        self.assertEqual(
            tuple(baseline),
            (self.base_sha, "strict", 0),
        )

    def test_tracked_exact_base_change_still_blocks_missing_baseline(
        self,
    ) -> None:
        (self.checkout / "value.py").write_text(
            "def value():\n    return 99\n",
            encoding="utf-8",
        )
        service, _ = self.service([])

        self.assertIsNone(service.execute("run-1"))

        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertEqual(
            run["reason"],
            "validation baseline is missing and the checkout is no longer "
            "the clean exact run base",
        )

    def test_reduced_baseline_debt_passes_with_nonzero_exit_status(self) -> None:
        self._amend_base(
            {
                "test_value.py": (
                    "import unittest\n"
                    "from value import value\n\n"
                    "class ValueTest(unittest.TestCase):\n"
                    "    def test_at_least_two(self):\n"
                    "        self.assertGreaterEqual(value(), 2)\n\n"
                    "    def test_three(self):\n"
                    "        self.assertEqual(value(), 3)\n"
                )
            }
        )
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "reduced existing failures"},
            ]
        )

        validated_sha = service.execute("run-1")

        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        with self.db.connect() as connection:
            result = connection.execute(
                """SELECT exit_status, verdict, comparison_json
                   FROM validation_results
                   WHERE commit_sha=?""",
                (validated_sha,),
            ).fetchone()
        comparison = json.loads(result["comparison_json"])
        self.assertEqual((result["exit_status"], result["verdict"]), (1, "pass"))
        self.assertEqual(
            (
                comparison["new_count"],
                comparison["resolved_count"],
                comparison["unchanged_count"],
            ),
            (0, 1, 1),
        )

    def _model_workflow_service(
        self,
    ) -> tuple[ExecutionService, dict[str, ScriptedRuntime]]:
        WorkflowService(self.db).store_template(
            "team-1",
            {
                "rationale": (
                    "Implement, coordinate, and independently verify."
                ),
                "assessment_prompt": (
                    "Assess evidence and retain or revise prompts, "
                    "dependencies, joins, and specialist selection."
                ),
                "nodes": [
                    {
                        "stable_key": "implementation",
                        "kind": "agent",
                        "member_key": "implementation",
                        "operation": "",
                        "prompt": "Implement value() returning two.",
                        "parameters": {},
                        "bindings": {},
                        "expected_output": {
                            "type": "object",
                            "required": ["summary"],
                            "properties": {
                                "summary": {"type": "string"}
                            },
                        },
                        "resources": ["checkout:write"],
                    },
                    {
                        "stable_key": "lead",
                        "kind": "agent",
                        "member_key": "lead",
                        "operation": "",
                        "prompt": "Integrate the implementation evidence.",
                        "parameters": {},
                        "bindings": {},
                        "expected_output": {
                            "type": "object",
                            "required": ["summary"],
                            "properties": {
                                "summary": {"type": "string"}
                            },
                        },
                        "resources": ["workspace:read"],
                    },
                    {
                        "stable_key": "verification",
                        "kind": "agent",
                        "member_key": "verification",
                        "operation": "",
                        "prompt": "Independently verify value() returns two.",
                        "parameters": {},
                        "bindings": {},
                        "expected_output": {
                            "type": "object",
                            "required": ["summary"],
                            "properties": {
                                "summary": {"type": "string"}
                            },
                        },
                        "resources": ["workspace:read"],
                    },
                ],
                "edges": [
                    {"source": "implementation", "target": "lead"},
                    {"source": "lead", "target": "verification"},
                ],
            },
        )
        runtimes = {
            "test/implementation": ScriptedRuntime(
                [
                    {
                        "action": "replace",
                        "path": "value.py",
                        "old": "return 1",
                        "new": "return 2",
                        "count": 1,
                    },
                    {
                        "action": "finish",
                        "summary": "implemented value two",
                        "output": {
                            "summary": "implemented value two"
                        },
                    },
                ]
            ),
            "test/lead": ScriptedRuntime(
                [
                    {
                        "action": "finish",
                        "summary": "integrated the implementation",
                        "output": {
                            "summary": "integrated the implementation"
                        },
                    },
                    {
                        "action": "finish",
                        "summary": "accepted graph evidence",
                        "output": {
                            "outcome": "accept",
                            "evidence": (
                                "all configured nodes completed"
                            ),
                        },
                    },
                ]
            ),
            "test/verification": ScriptedRuntime(
                [
                    {
                        "action": "finish",
                        "summary": "verified value two",
                        "output": {"summary": "verified value two"},
                    }
                ]
            ),
        }

        def runtime_factory(
            _stored_runtime: str,
            stored_model: str,
            _stored_timeout: float,
        ) -> ScriptedRuntime:
            return runtimes[stored_model]

        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=runtime_factory,
            max_actions=10,
        )
        return service, runtimes

    def test_initial_assignment_context_exposes_workflow_dependencies(
        self,
    ) -> None:
        service, _ = self._model_workflow_service()
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM agent_assignments WHERE run_id='run-1'"
            )
        assigning_lead = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": ["lead", "implementation", "verification"],
                    "reason": "Select the complete workflow dependency branch.",
                }
            ]
        )
        service.runtime_factory = (
            lambda _runtime, _model, _stored_timeout: assigning_lead
        )

        self.assertIsNone(service.execute("run-1"))

        payload = json.loads(assigning_lead.contexts[0])
        contract = payload["workflow_assignment_contract"]
        self.assertEqual(
            {
                (node["stable_key"], node["member_key"])
                for node in contract["agent_nodes"]
            },
            {
                ("implementation", "implementation"),
                ("lead", "lead"),
                ("verification", "verification"),
            },
        )
        self.assertEqual(
            contract["edges"],
            [
                {"source": "implementation", "target": "lead"},
                {"source": "lead", "target": "verification"},
            ],
        )
        self.assertIn("upstream agent dependencies", contract["assignment_rule"])

    def test_model_designed_workflow_drives_source_execution(self) -> None:
        service, _ = self._model_workflow_service()

        validated_sha = service.execute("run-1")

        self.assertIsNotNone(validated_sha)
        graph = service.workflows.active_run_graph("run-1")
        self.assertEqual(graph.state, "succeeded")
        self.assertEqual(graph.assessment["outcome"], "accept")
        self.assertEqual(
            [node.state for node in graph.nodes],
            ["succeeded", "succeeded", "succeeded"],
        )
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            "publishing",
        )

    def test_invalid_workflow_finish_output_is_corrected_in_same_attempt(
        self,
    ) -> None:
        service, runtimes = self._model_workflow_service()
        runtimes["test/implementation"] = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {
                    "action": "finish",
                    "summary": "invalid command evidence",
                    "output": {
                        "summary": "implemented value two",
                        "tests": [
                            {
                                "command": "python -m unittest test_value -v",
                                "result": "passed",
                            }
                        ],
                    },
                },
                {
                    "action": "finish",
                    "summary": "corrected passive test evidence",
                    "output": {
                        "summary": "implemented value two",
                        "tests": [
                            {
                                "scenario": "Run the focused value test.",
                                "result": "passed",
                            }
                        ],
                    },
                },
            ]
        )

        validated_sha = service.execute("run-1")

        self.assertIsNotNone(validated_sha)
        graph = service.workflows.active_run_graph("run-1")
        implementation = next(
            node for node in graph.nodes if node.stable_key == "implementation"
        )
        self.assertEqual(implementation.state, "succeeded")
        self.assertEqual(
            implementation.output,
            {
                "summary": "implemented value two",
                "tests": [
                    {
                        "scenario": "Run the focused value test.",
                        "result": "passed",
                    }
                ],
            },
        )
        projected = service.workflows.project_run("run-1")
        projected_implementation = next(
            node
            for node in projected["generations"][0]["nodes"]
            if node["stable_key"] == "implementation"
        )
        self.assertEqual(
            [attempt["state"] for attempt in projected_implementation["attempts"]],
            ["succeeded"],
        )
        history = (
            self.checkout.parent
            / "agent-state"
            / "workflow"
            / f"node-{implementation.id}"
            / "action-history.json"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Rejected safely; correct it and continue: "
            "workflow node output.tests[0].command cannot contain "
            "executable or secret configuration",
            history,
        )
        self.assertNotIn("finished: invalid command evidence", history)
        self.assertEqual(len(runtimes["test/implementation"].contexts), 3)

    def test_invalid_workflow_output_secret_is_redacted_during_correction(
        self,
    ) -> None:
        fixture_secret = 'workflow-"canary"\\line\nvalue'
        runtime = ScriptedRuntime(
            [
                {
                    "action": "finish",
                    "summary": f"accidentally reported {fixture_secret}",
                    "output": {"summary": fixture_secret},
                },
                {
                    "action": "finish",
                    "summary": "reported only passive evidence",
                    "output": {"summary": "safe evidence"},
                },
            ]
        )
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: runtime,
            max_actions=3,
        )
        member = next(
            member
            for member in TeamService(self.db).load("team-1").members
            if member.stable_key == "implementation"
        )
        layout = RunLayout.create(self.data_root, "repo-1", "run-1")

        outcome, yielded = service._agent_cycle(
            runtime,
            member,
            SandboxPolicy(persistent_root=self.sandbox_root),
            layout,
            "Complete the workflow node.",
            [],
            (),
            {fixture_secret},
            workflow_expected_output={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        )

        self.assertFalse(yielded)
        self.assertEqual(outcome, {"summary": "safe evidence"})
        history = (layout.agent_state / "action-history.json").read_text(
            encoding="utf-8"
        )
        observed = history + "\n" + "\n".join(runtime.contexts)
        self.assertIn("[REDACTED]", observed)
        self.assertNotIn(fixture_secret, observed)
        self.assertNotIn(json.dumps(fixture_secret)[1:-1], observed)
        self.assertNotIn("finished: accidentally reported", history)

    def test_feedback_opens_one_restart_safe_workflow_generation(
        self,
    ) -> None:
        service, runtimes = self._model_workflow_service()
        self.assertIsNotNone(service.execute("run-1"))
        self.lifecycle.transition(
            "run-1",
            RunState.WAITING_FOR_FEEDBACK,
        )
        self.lifecycle.transition(
            "run-1",
            RunState.RESOLVING_FEEDBACK,
        )
        current = service.workflows.active_run_graph("run-1")
        revised_nodes = [
            {
                "stable_key": node.stable_key,
                "kind": node.kind,
                "member_key": node.member_key or "",
                "operation": node.operation or "",
                "prompt": (
                    "Implement value() returning three."
                    if node.stable_key == "implementation"
                    else node.prompt
                ),
                "parameters": node.parameters,
                "bindings": node.bindings,
                "expected_output": node.expected_output,
                "resources": list(node.resources),
            }
            for node in current.nodes
        ]
        revised_edges = [
            {"source": edge.source, "target": edge.target}
            for edge in current.edges
        ]
        feedback = json.dumps(
            {
                "feedback": [
                    {
                        "instruction": (
                            "Update value() and its test to return three."
                        )
                    }
                ]
            },
            sort_keys=True,
        )
        feedback_items = [
            {
                "key": "return-three",
                "title": "Return the revised requested value",
                "objective": "The public value function returns three.",
                "acceptance_criteria": [
                    {
                        "key": "value-returns-three",
                        "requirement": (
                            "Calling value returns the revised requested integer."
                        ),
                        "expected": "value() returns 3.",
                    }
                ],
                "verification": [
                    {
                        "key": "call-value-three",
                        "criterion_keys": ["value-returns-three"],
                        "scenario": (
                            "Call value() and compare its return value with 3."
                        ),
                    }
                ],
            }
        ]
        feedback_specification = self.specifications.submit(
            run_id="run-1",
            author_member_id="lead-1",
            issue_version_id=self.issue_version_id,
            items=feedback_items,
            reason="Reconcile the accepted feedback before workflow revision.",
        )
        self.specifications.record_review(
            run_id="run-1",
            specification_revision_id=str(feedback_specification["id"]),
            reviewer_member_id="verification-1",
            reviewer_model="test/verification",
            rubric_version=1,
            verdict="approved",
            summary="The feedback revision is complete and observable.",
            findings=[],
        )
        self.specifications.bind_context(
            run_id="run-1",
            issue_version_id=self.issue_version_id,
            context_sha256=hashlib.sha256(feedback.encode("utf-8")).hexdigest(),
            specification_revision_id=str(feedback_specification["id"]),
        )
        runtimes["test/lead"] = ScriptedRuntime(
            [
                {
                    "action": "finish",
                    "summary": "planned the feedback revision",
                    "output": {
                        "outcome": "revise",
                        "evidence": (
                            "the accepted graph predates new feedback"
                        ),
                        "reason": "apply the requested value change",
                        "nodes": revised_nodes,
                        "edges": revised_edges,
                    },
                }
            ]
        )
        with mock.patch(
            "repogents.execution.WorkflowExecutionEngine.execute",
            side_effect=RuntimeError("interrupt after revision"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "interrupt after revision",
            ):
                service.execute(
                    "run-1",
                    additional_context=feedback,
                )

        interrupted = service.workflows.active_run_graph("run-1")
        self.assertEqual(interrupted.generation, 2)
        self.assertIn("controller-revision:", interrupted.reason)
        self.assertEqual(
            len(service.workflows.project_run("run-1")["generations"]),
            2,
        )

        runtimes["test/implementation"] = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 2",
                    "new": "return 3",
                    "count": 1,
                },
                {
                    "action": "replace",
                    "path": "test_value.py",
                    "old": "self.assertEqual(value(), 2)",
                    "new": "self.assertEqual(value(), 3)",
                    "count": 1,
                },
                {
                    "action": "finish",
                    "summary": "implemented value three",
                    "output": {"summary": "implemented value three"},
                },
            ]
        )
        runtimes["test/lead"] = ScriptedRuntime(
            [
                {
                    "action": "finish",
                    "summary": "integrated the feedback change",
                    "output": {
                        "summary": "integrated the feedback change"
                    },
                },
                {
                    "action": "finish",
                    "summary": "accepted feedback evidence",
                    "output": {
                        "outcome": "accept",
                        "evidence": (
                            "the requested value and test now return three"
                        ),
                    },
                },
            ]
        )
        runtimes["test/verification"] = ScriptedRuntime(
            [
                {
                    "action": "finish",
                    "summary": "verified value three",
                    "output": {"summary": "verified value three"},
                }
            ]
        )

        validated_sha = service.execute(
            "run-1",
            additional_context=feedback,
        )

        self.assertIsNotNone(validated_sha)
        completed = service.workflows.active_run_graph("run-1")
        self.assertEqual(completed.generation, 2)
        self.assertEqual(completed.state, "succeeded")
        self.assertEqual(
            len(service.workflows.project_run("run-1")["generations"]),
            2,
        )
        self.assertIn(
            "return 3",
            (self.checkout / "value.py").read_text(encoding="utf-8"),
        )

    def test_lower_finding_count_with_a_new_finding_fails(self) -> None:
        baseline = (
            "src/a.py:10:2: error: alpha [RULE_A]\n"
            "src/b.py:20:3: error: beta [RULE_B]\n"
            "src/c.py:30:4: error: gamma [RULE_C]\n"
        )
        candidate = (
            "src/a.py:110:12: error: alpha [RULE_A]\n"
            "src/d.py:40:5: error: new debt [RULE_D]\n"
        )
        self._amend_base({"diagnostics.txt": baseline})
        self._set_validation_command(
            [
                "python3",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "print(Path('diagnostics.txt').read_text()); sys.exit(1)"
                ),
            ]
        )
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "diagnostics.txt",
                    "old": baseline,
                    "new": candidate,
                    "count": 1,
                },
                {"action": "finish", "summary": "net reduction with new debt"},
            ]
        )
        service.max_revision_cycles = 1

        self.assertIsNone(service.execute("run-1"))

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")
        with self.db.connect() as connection:
            result = connection.execute("""SELECT verdict, comparison_json
                   FROM validation_results
                   ORDER BY started_at DESC
                   LIMIT 1""").fetchone()
        comparison = json.loads(result["comparison_json"])
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(
            (
                comparison["new_count"],
                comparison["resolved_count"],
                comparison["unchanged_count"],
            ),
            (1, 2, 1),
        )

    def test_validation_policy_change_cannot_erase_baseline_debt(self) -> None:
        self._amend_base({".eslintignore": "# baseline\n"})
        self._set_validation_command(
            [
                "python3",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "ignored=Path('.eslintignore').read_text(); "
                    "finding='src/a.py:10:2: error: alpha [RULE_A]'; "
                    "print('' if 'src/a.py' in ignored else finding); "
                    "sys.exit(0 if 'src/a.py' in ignored else 1)"
                ),
            ]
        )
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": ".eslintignore",
                    "old": "# baseline",
                    "new": "src/a.py",
                    "count": 1,
                },
                {"action": "finish", "summary": "suppressed baseline finding"},
            ]
        )
        service.max_revision_cycles = 1

        self.assertIsNone(service.execute("run-1"))

        with self.db.connect() as connection:
            result = connection.execute("""SELECT verdict, comparison_json
                   FROM validation_results
                   ORDER BY started_at DESC
                   LIMIT 1""").fetchone()
        comparison = json.loads(result["comparison_json"])
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(comparison["contract_changed"], [".eslintignore"])
        self.assertTrue(comparison["weakening_detected"])

    def test_non_suppressing_policy_file_change_remains_eligible(self) -> None:
        finding = "src/a.py:10:2: error: alpha [RULE_A]\n"
        self._amend_base(
            {
                "diagnostics.txt": finding,
                "pyproject.toml": 'version = "1.0"\n',
            }
        )
        self._set_validation_command(
            [
                "python3",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "print(Path('diagnostics.txt').read_text()); sys.exit(1)"
                ),
            ]
        )
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "pyproject.toml",
                    "old": 'version = "1.0"',
                    "new": 'version = "1.1"',
                    "count": 1,
                },
                {"action": "finish", "summary": "updated project metadata"},
            ]
        )

        validated_sha = service.execute("run-1")

        self.assertIsNotNone(validated_sha)
        with self.db.connect() as connection:
            result = connection.execute(
                """SELECT verdict, comparison_json
                   FROM validation_results
                   WHERE commit_sha=?""",
                (validated_sha,),
            ).fetchone()
        comparison = json.loads(result["comparison_json"])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(comparison["contract_changed"], ["pyproject.toml"])
        self.assertEqual(comparison["weakening_detected"], [])

    def test_source_only_suppression_cannot_erase_baseline_finding(
        self,
    ) -> None:
        self._amend_base({"src/a.py": "value = 1\n"})
        self._set_validation_command(
            [
                "python3",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "source=Path('src/a.py').read_text(); "
                    "finding='src/a.py:10:2: error: alpha [RULE_A]'; "
                    "print('' if 'noqa' in source else finding); "
                    "sys.exit(0 if 'noqa' in source else 1)"
                ),
            ]
        )
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "src/a.py",
                    "old": "value = 1",
                    "new": "# noqa: RULE_A\nvalue = 1",
                    "count": 1,
                },
                {"action": "finish", "summary": "suppressed baseline finding"},
            ]
        )
        service.max_revision_cycles = 1

        self.assertIsNone(service.execute("run-1"))

        with self.db.connect() as connection:
            result = connection.execute("""SELECT verdict, comparison_json
                   FROM validation_results
                   ORDER BY started_at DESC
                   LIMIT 1""").fetchone()
        comparison = json.loads(result["comparison_json"])
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(comparison["contract_changed"], ["src/a.py"])
        self.assertTrue(comparison["weakening_detected"])

    def test_prepared_feedback_base_excludes_inherited_source_suppression(
        self,
    ) -> None:
        self._seed_default_delta_baseline()
        inherited = self.checkout / "src" / "base.py"
        inherited.parent.mkdir(parents=True)
        inherited.write_text("# noqa\nvalue = 1\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Upstream",
            "-c",
            "user.email=upstream@example.com",
            "commit",
            "-qm",
            "prepared intended base",
        )
        prepared_base_sha = self._git("rev-parse", "HEAD").strip()
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented issue delta"},
            ]
        )

        validated_sha = service.execute(
            "run-1",
            comparison_base_sha=prepared_base_sha,
        )

        self.assertIsNotNone(validated_sha)
        with self.db.connect() as connection:
            result = connection.execute(
                """SELECT verdict, comparison_json
                   FROM validation_results
                   WHERE commit_sha=?""",
                (validated_sha,),
            ).fetchone()
        comparison = json.loads(result["comparison_json"])
        self.assertEqual(result["verdict"], "pass")
        self.assertNotIn("src/base.py", comparison["contract_changed"])

    def test_prepared_feedback_validation_resume_skips_model_replay(
        self,
    ) -> None:
        self._seed_default_delta_baseline()
        inherited = self.checkout / "src" / "base.py"
        inherited.parent.mkdir(parents=True)
        inherited.write_text("# noqa\nvalue = 1\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Upstream",
            "-c",
            "user.email=upstream@example.com",
            "commit",
            "-qm",
            "prepared intended base",
        )
        prepared_base_sha = self._git("rev-parse", "HEAD").strip()
        (self.checkout / "value.py").write_text(
            "def value():\n    return 2\n",
            encoding="utf-8",
        )
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "existing feedback candidate",
        )
        candidate_sha = self._git("rev-parse", "HEAD").strip()
        self.lifecycle.transition("run-1", RunState.IMPLEMENTING)
        self.lifecycle.transition("run-1", RunState.VALIDATING)
        self.lifecycle.transition("run-1", RunState.PUBLISHING)
        self.lifecycle.transition("run-1", RunState.WAITING_FOR_FEEDBACK)
        self.lifecycle.transition("run-1", RunState.RESOLVING_FEEDBACK)
        recovery_reason = (
            "automatic feedback validation retry against prepared base"
        )
        self.lifecycle.transition(
            "run-1",
            RunState.VALIDATING,
            reason=recovery_reason,
        )
        runtime = ScriptedRuntime([])
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: runtime,
        )

        validated_sha = service.execute(
            "run-1",
            comparison_base_sha=prepared_base_sha,
        )

        self.assertEqual(validated_sha, candidate_sha)
        self.assertEqual(runtime.contexts, [])
        self.assertEqual(
            self.lifecycle.get_run("run-1")["reason"],
            recovery_reason,
        )

    def test_prepared_feedback_base_must_be_candidate_ancestor(self) -> None:
        self._seed_default_delta_baseline()
        self._git("checkout", "-qb", "prepared-base")
        inherited = self.checkout / "src" / "base.py"
        inherited.parent.mkdir(parents=True)
        inherited.write_text("value = 1\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Upstream",
            "-c",
            "user.email=upstream@example.com",
            "commit",
            "-qm",
            "prepared intended base",
        )
        prepared_base_sha = self._git("rev-parse", "HEAD").strip()
        self._git("checkout", "-q", "main")
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented without prepared base"},
            ]
        )
        service.max_revision_cycles = 1

        self.assertIsNone(
            service.execute(
                "run-1",
                comparison_base_sha=prepared_base_sha,
            )
        )

        layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        self.assertIn(
            "candidate does not descend from the prepared source base",
            "\n".join(service._load_transcript(layout)),
        )
        with self.db.connect() as connection:
            validation_count = connection.execute(
                "SELECT COUNT(*) FROM validation_results WHERE run_id='run-1'"
            ).fetchone()[0]
        self.assertEqual(validation_count, 0)

    def test_narrow_suppression_with_source_change_remains_reviewable(
        self,
    ) -> None:
        self._amend_base({"src/a.py": "value = 1\n"})
        self._set_validation_command(
            [
                "python3",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "source=Path('src/a.py').read_text(); "
                    "finding='src/a.py:10:2: error: alpha [RULE_A]'; "
                    "print('' if 'value = 2' in source else finding); "
                    "sys.exit(0 if 'value = 2' in source else 1)"
                ),
            ]
        )
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "src/a.py",
                    "old": "value = 1",
                    "new": "value = 2  # noqa: RULE_A",
                    "count": 1,
                },
                {
                    "action": "finish",
                    "summary": "changed behavior with narrow suppression",
                },
            ]
        )

        validated_sha = service.execute("run-1")

        self.assertIsNotNone(validated_sha)
        with self.db.connect() as connection:
            result = connection.execute(
                """SELECT verdict, comparison_json
                   FROM validation_results
                   WHERE commit_sha=?""",
                (validated_sha,),
            ).fetchone()
        comparison = json.loads(result["comparison_json"])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(comparison["contract_changed"], ["src/a.py"])
        self.assertEqual(comparison["weakening_detected"], [])

    def test_prettier_baseline_debt_is_stored_before_agent_action(self) -> None:
        self._set_validation_command(
            [
                "python3",
                "-c",
                (
                    "import sys; "
                    "sys.stderr.write('[warn] scripts/core/tool-loop-handler.js\\n"
                    "[warn] README.md\\n"
                    "[warn] Code style issues found in 2 files. "
                    "Run Prettier with --write to fix.\\n'); "
                    "raise SystemExit(1)"
                ),
            ]
        )
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM agent_assignments WHERE run_id='run-1'")

        class BaselineObservedRuntime:
            def __init__(self) -> None:
                self.contexts: list[str] = []

            def next_action(
                self, context: str, state_directory: Path
            ) -> dict[str, object]:
                del state_directory
                self.contexts.append(context)
                return {
                    "action": "block",
                    "reason": "fixture observed execution after baseline",
                }

        runtime = BaselineObservedRuntime()
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: runtime,
        )

        self.assertIsNone(service.execute("run-1"))

        with self.db.connect() as connection:
            baseline = connection.execute(
                """SELECT mode, exit_status, findings_json
                   FROM validation_baselines WHERE run_id='run-1'"""
            ).fetchone()
        self.assertEqual(baseline["mode"], "delta")
        self.assertEqual(baseline["exit_status"], 1)
        self.assertEqual(
            json.loads(baseline["findings_json"]),
            [
                "scripts/core/tool-loop-handler.js|warning|prettier|File is not formatted",
                "README.md|warning|prettier|File is not formatted",
            ],
        )
        self.assertEqual(len(runtime.contexts), 1)

    def test_unparseable_failed_baseline_blocks_before_agent_action(self) -> None:
        self._set_validation_command(
            ["python3", "-c", "print('command failed'); raise SystemExit(1)"]
        )
        service, runtime = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                }
            ]
        )

        self.assertIsNone(service.execute("run-1"))

        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("baseline", run["reason"])
        self.assertEqual(runtime.contexts, [])

    def test_agent_edits_only_isolated_checkout_and_validates_exact_commit(
        self,
    ) -> None:
        service, runtime = self.service(
            [
                {"action": "read", "path": "value.py"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {
                    "action": "run",
                    "argv": ["python3", "-m", "unittest", "test_value.py"],
                },
                {"action": "finish", "summary": "value now returns two"},
            ]
        )
        validated_sha = service.execute("run-1")
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "publishing")
        self.assertEqual(run["validated_sha"], validated_sha)
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), validated_sha)
        self.assertEqual(
            (self.checkout / "value.py").read_text(encoding="utf-8"),
            "def value():\n    return 2\n",
        )
        with self.db.connect() as connection:
            validations = connection.execute(
                "SELECT * FROM validation_results"
            ).fetchall()
            assignments = connection.execute(
                "SELECT * FROM agent_assignments"
            ).fetchall()
        self.assertEqual(len(validations), 1)
        self.assertEqual(validations[0]["commit_sha"], validated_sha)
        self.assertEqual(validations[0]["exit_status"], 0)
        self.assertEqual(len(assignments), 3)
        self.assertTrue(runtime.contexts)

    def test_pause_interrupts_sandbox_action_then_same_run_resumes(self) -> None:
        service, runtime = self.service(
            [
                {
                    "action": "run",
                    "argv": [
                        "python3",
                        "-c",
                        "import time; time.sleep(60)",
                    ],
                },
                {
                    "action": "write",
                    "path": "later.txt",
                    "content": "started only after resume\n",
                },
                {
                    "action": "finish",
                    "summary": "continued the same persisted run",
                },
            ]
        )
        results: list[str | None] = []
        worker = threading.Thread(
            target=lambda: results.append(service.execute("run-1"))
        )
        worker.start()
        deadline = time.monotonic() + 10
        while (
            not runtime.contexts or not self.sandbox.is_active("run-1")
        ) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(runtime.contexts)
        self.assertTrue(self.sandbox.is_active("run-1"))
        state_at_pause = self.lifecycle.get_run("run-1")["state"]

        paused_runs = self.lifecycle.set_repository_paused("repo-1", True)
        worker.join(10)

        self.assertEqual(paused_runs, ("run-1",))
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], state_at_pause)
        self.assertNotIn(
            self.lifecycle.get_run("run-1")["state"],
            {
                RunState.BLOCKED.value,
                RunState.CANCELED.value,
                RunState.CLOSED.value,
            },
        )
        self.assertFalse((self.checkout / "later.txt").exists())

        self.assertEqual(
            self.lifecycle.set_repository_paused("repo-1", False),
            ("run-1",),
        )
        validated_sha = service.execute("run-1")

        self.assertIsNotNone(validated_sha)
        self.assertEqual(
            self.lifecycle.get_run("run-1")["state"],
            RunState.PUBLISHING.value,
        )
        self.assertEqual(
            (self.checkout / "later.txt").read_text(encoding="utf-8"),
            "started only after resume\n",
        )

    def test_inspection_note_survives_action_quantum_and_guides_edit(self) -> None:
        first_runtime = ScriptedRuntime(
            [
                {
                    "action": "note",
                    "summary": "value.py returns one; replace it with two next.",
                }
            ]
        )
        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: first_runtime,
            max_actions=1,
        )

        with mock.patch.object(
            first.tools,
            "execute",
            wraps=first.tools.execute,
        ) as tool_execute:
            self.assertIsNone(first.execute("run-1"))
        tool_execute.assert_not_called()
        second_runtime = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented stored note"},
            ]
        )
        lead_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "integrated stored implementation"}]
        )
        verifier_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "stored note candidate approved"}]
        )
        restarted = ExecutionService(
            database=Database(self.db.path),
            lifecycle=self.lifecycle,
            teams=TeamService(Database(self.db.path)),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, model, _timeout: (
                lead_runtime
                if model == "test/lead"
                else verifier_runtime
                if model == "test/verification"
                else second_runtime
            ),
            max_actions=10,
        )

        self.assertIsNotNone(restarted.execute("run-1"))
        self.assertIn(
            "Member implementation note: value.py returns one; replace it with two next.",
            second_runtime.contexts[0],
        )

    def test_repeated_note_without_repository_mutation_is_rejected(self) -> None:
        service, runtime = self.service(
            [
                {
                    "action": "note",
                    "summary": "value.py returns one; replace it with two next.",
                },
                {
                    "action": "assign",
                    "members": ["lead"],
                    "reason": "repeat the existing assignment",
                },
                {"action": "read", "path": "value.py"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 1",
                    "count": 1,
                },
                {
                    "action": "write",
                    "path": "value.py",
                    "content": "def value():\n    return 1\n",
                },
                {
                    "action": "note",
                    "summary": "value.py still returns one; replace it with two next.",
                },
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {
                    "action": "note",
                    "summary": "value.py now returns two; finish next.",
                },
                {"action": "finish", "summary": "implemented the recorded next action"},
            ]
        )

        self.assertIsNotNone(service.execute("run-1"))
        self.assertEqual(
            (self.checkout / "value.py").read_text(encoding="utf-8"),
            "def value():\n    return 2\n",
        )
        self.assertTrue(
            any(
                "a coordination note already records an exact next action" in context
                for context in runtime.contexts
            )
        )
        self.assertIn(
            "Member implementation note: value.py now returns two; finish next.",
            runtime.contexts[-1],
        )

    def test_uses_runtime_and_model_recorded_on_stored_lead(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE team_members SET runtime='mini-swe-agent', "
                "model='openai/stored-model', action_timeout_seconds=321 "
                "WHERE id='lead-1'"
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-2', 'repo-1', 2, '{}', ?)""",
                ("2026-01-02T00:00:00Z",),
            )
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions, action_timeout_seconds)
                   VALUES
                   ('lead-2', 'team-2', 'lead', 'lead', 'release coordinator',
                    'Coordinate later runs', '["read","git_diff","git_commit"]',
                    'mini-swe-agent', 'openai/new-model', '', 777),
                   ('implementation-2', 'team-2', 'implementation', 'implementer',
                    'release implementation maintainer', 'Implement later runs',
                    '["read","write","run","git_diff"]',
                    'mini-swe-agent', 'openai/new-model', '', 777),
                   ('verification-2', 'team-2', 'verification', 'verifier',
                    'release behavior verifier', 'Verify later runs',
                    '["read","run","git_diff"]',
                    'mini-swe-agent', 'openai/new-model', '', 777)""")
            connection.execute(
                """UPDATE repositories SET current_team_version_id='team-2'
                   WHERE id='repo-1'"""
            )
        runtime = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "used stored model"},
            ]
        )
        lead_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "integrated stored model work"}]
        )
        verifier_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "stored model candidate approved"}]
        )
        requested: list[tuple[str, str, float]] = []

        def runtime_factory(
            stored_runtime: str,
            stored_model: str,
            stored_timeout: float,
        ) -> ScriptedRuntime:
            requested.append((stored_runtime, stored_model, stored_timeout))
            if stored_model == "openai/stored-model":
                return lead_runtime
            if stored_model == "test/verification":
                return verifier_runtime
            return runtime

        restarted_database = Database(self.db.path)
        restarted_database.initialize()
        service = ExecutionService(
            database=restarted_database,
            lifecycle=self.lifecycle,
            teams=TeamService(restarted_database),
            sandbox=self.sandbox,
            runtime_factory=runtime_factory,
            max_actions=10,
        )
        self.assertIsNotNone(service.execute("run-1"))
        self.assertEqual(
            requested,
            [
                ("mini-swe-agent", "openai/stored-model", 321),
                ("mini-swe-agent", "test/implementation", 300),
                ("mini-swe-agent", "test/verification", 300),
            ],
        )
        self.assertTrue(runtime.contexts)

    def test_rejects_unsupported_stored_runtime(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE team_members SET runtime='shell', model='unsafe' WHERE id='lead-1'"
            )
        factory = mock.Mock()
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=factory,
            max_actions=10,
        )
        with self.assertRaisesRegex(ValueError, "unsupported stored model runtime"):
            service.execute("run-1")
        factory.assert_not_called()

    def test_cancellation_during_inference_prevents_later_effects(self) -> None:
        lifecycle = self.lifecycle

        class CancelDuringInference:
            def next_action(
                self, context: str, state_directory: Path
            ) -> dict[str, object]:
                lifecycle.transition(
                    "run-1", RunState.CANCELED, reason="user canceled during inference"
                )
                return {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                }

        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: CancelDuringInference(),
            max_actions=10,
        )
        self.assertIsNone(service.execute("run-1"))
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "canceled")
        self.assertEqual(
            (self.checkout / "value.py").read_text(encoding="utf-8"),
            "def value():\n    return 1\n",
        )
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), self.base_sha)
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM validation_results"
                ).fetchone()[0],
                0,
            )

    def test_command_scoped_secret_is_authorized_redacted_and_not_persisted(
        self,
    ) -> None:
        command = [
            "python3",
            "-c",
            "import os; print(os.environ['FIXTURE_TOKEN'])",
        ]
        fixture_secret = 'fixture-"canary"\\line\nvalue'
        bindings = [
            {
                "name": "FIXTURE_TOKEN",
                "reference": "secret://fixture",
                "commands": [command],
            },
            {
                "name": "OTHER_TOKEN",
                "reference": "secret://other",
                "commands": [["python3", "-c", "print('other')"]],
            },
        ]
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT policy_json FROM sandbox_versions WHERE id='sandbox-1'"
            ).fetchone()
            policy = json.loads(row["policy_json"])
            policy["secret_bindings"] = bindings
            connection.execute(
                "UPDATE sandbox_versions SET policy_json=? WHERE id='sandbox-1'",
                (json.dumps(policy),),
            )
        runtime = ScriptedRuntime(
            [
                {"action": "run", "argv": command},
                {
                    "action": "note",
                    "summary": (
                        f"Observed {fixture_secret}; edit next. " + ("x" * 5_000)
                    ),
                },
                {
                    "action": "read",
                    "path": f"{fixture_secret}/../../escape.txt",
                },
            ]
        )
        resolved: list[str] = []

        def resolve(reference: str) -> str:
            resolved.append(reference)
            return {
                "secret://fixture": fixture_secret,
                "secret://other": "other-canary-value",
            }[reference]

        original_run = self.sandbox.run
        dispatched: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

        def record_run(
            policy: object,
            layout: object,
            argv: tuple[str, ...],
            **kwargs: object,
        ) -> object:
            secrets = kwargs.get("secrets")
            dispatched.append((argv, secrets if isinstance(secrets, dict) else None))
            return original_run(policy, layout, argv, **kwargs)

        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: runtime,
            secret_resolver=resolve,
            max_actions=3,
        )
        with mock.patch.object(self.sandbox, "run", side_effect=record_run):
            self.assertIsNone(service.execute("run-1"))
        history_path = self.checkout.parent / "agent-state" / "action-history.json"
        stored_history = json.loads(history_path.read_text(encoding="utf-8"))
        note_entry = next(
            entry
            for entry in stored_history
            if entry.startswith("Member implementation note:")
        )
        self.assertLessEqual(len(note_entry), 2_020)
        restarted_runtime = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "secret stayed scoped"},
            ]
        )
        lead_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "integrated scoped secret work"}]
        )
        verifier_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "scoped secret candidate approved"}]
        )
        restarted_database = Database(self.db.path)
        restarted = ExecutionService(
            database=restarted_database,
            lifecycle=self.lifecycle,
            teams=TeamService(restarted_database),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, model, _stored_timeout: (
                lead_runtime
                if model == "test/lead"
                else verifier_runtime
                if model == "test/verification"
                else restarted_runtime
            ),
            secret_resolver=resolve,
            max_actions=10,
        )
        with mock.patch.object(self.sandbox, "run", side_effect=record_run):
            self.assertIsNotNone(restarted.execute("run-1"))
        self.assertEqual(resolved, ["secret://fixture"])
        self.assertIn(
            (tuple(command), {"FIXTURE_TOKEN": fixture_secret}),
            dispatched,
        )
        self.assertFalse(
            any(
                secrets and "OTHER_TOKEN" in secrets
                for _dispatched_command, secrets in dispatched
            )
        )
        observed = "\n".join(
            [
                *stored_history,
                *runtime.contexts,
                *restarted_runtime.contexts,
                *(
                    path.read_text(encoding="utf-8")
                    for path in (self.checkout.parent / "logs").glob("command-*.json")
                ),
            ]
        )
        self.assertIn("[REDACTED]", observed)
        self.assertNotIn(fixture_secret, observed)
        self.assertNotIn(json.dumps(fixture_secret)[1:-1], observed)

    def test_resolved_command_secret_returns_to_agent_for_automatic_correction(
        self,
    ) -> None:
        command = [
            "python3",
            "-c",
            "import os; print(os.environ['FIXTURE_TOKEN'])",
        ]
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT policy_json FROM sandbox_versions WHERE id='sandbox-1'"
            ).fetchone()
            policy = json.loads(row["policy_json"])
            policy["secret_bindings"] = [
                {
                    "name": "FIXTURE_TOKEN",
                    "reference": "secret://fixture",
                    "commands": [command],
                }
            ]
            connection.execute(
                "UPDATE sandbox_versions SET policy_json=? WHERE id='sandbox-1'",
                (json.dumps(policy),),
            )
        runtime = ScriptedRuntime(
            [
                {"action": "run", "argv": command},
                {
                    "action": "write",
                    "path": "value.py",
                    "content": "fixture-canary-value\n",
                },
                {"action": "finish", "summary": "attempted secret leak"},
                {
                    "action": "write",
                    "path": "value.py",
                    "content": "def value():\n    return 2\n",
                },
                {"action": "finish", "summary": "removed the secret"},
            ]
        )
        lead_runtime = ScriptedRuntime(
            [
                {"action": "finish", "summary": "reviewed corrected secret handling"},
                {"action": "finish", "summary": "integrated corrected secret handling"},
            ]
        )
        verifier_runtime = ScriptedRuntime(
            [
                {"action": "finish", "summary": "first candidate reviewed"},
                {"action": "finish", "summary": "secret-free candidate approved"},
            ]
        )
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, model, _stored_timeout: (
                lead_runtime
                if model == "test/lead"
                else verifier_runtime
                if model == "test/verification"
                else runtime
            ),
            secret_resolver=lambda reference: {
                "secret://fixture": "fixture-canary-value"
            }[reference],
            max_actions=10,
        )

        validated_sha = service.execute("run-1")

        run = self.lifecycle.get_run("run-1")
        self.assertIsNotNone(validated_sha)
        self.assertEqual(run["state"], "publishing")
        self.assertNotEqual(self._git("rev-parse", "HEAD").strip(), self.base_sha)
        self.assertTrue(
            any("potential secret" in context for context in runtime.contexts)
        )
        self.assertTrue(
            all("fixture-canary-value" not in context for context in runtime.contexts)
        )

    def test_stored_repository_instruction_constrains_agent_edit(self) -> None:
        (self.checkout / "allowed.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        (self.checkout / "locked.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT evidence_json FROM sandbox_versions WHERE id='sandbox-1'"
            ).fetchone()
            evidence = json.loads(row["evidence_json"])
            evidence["instructions"] = [
                ["AGENTS.md", "Do not modify locked.py; use allowed.py instead."]
            ]
            connection.execute(
                "UPDATE sandbox_versions SET evidence_json=? WHERE id='sandbox-1'",
                (json.dumps(evidence),),
            )
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: RuleAwareRuntime(),
            max_actions=10,
        )
        self.assertIsNotNone(service.execute("run-1"))
        self.assertIn(
            "return 2",
            (self.checkout / "allowed.py").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "return 1",
            (self.checkout / "locked.py").read_text(encoding="utf-8"),
        )

    def test_assignment_reason_redacts_prior_command_secret_and_is_bounded(
        self,
    ) -> None:
        command = [
            "python3",
            "-c",
            "import os; print(os.environ['FIXTURE_TOKEN'])",
        ]
        fixture_secret = 'assignment-"secret"\\line\nvalue'
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM agent_assignments WHERE run_id='run-1'")
            row = connection.execute(
                "SELECT policy_json FROM sandbox_versions WHERE id='sandbox-1'"
            ).fetchone()
            policy = json.loads(row["policy_json"])
            policy["secret_bindings"] = [
                {
                    "name": "FIXTURE_TOKEN",
                    "reference": "secret://fixture",
                    "commands": [command],
                }
            ]
            connection.execute(
                "UPDATE sandbox_versions SET policy_json=? WHERE id='sandbox-1'",
                (json.dumps(policy),),
            )
        runtime = ScriptedRuntime(
            [
                {"action": "run", "argv": command},
                {
                    "action": "assign",
                    "members": ["lead", "verification"],
                    "reason": (
                        f"Observed {fixture_secret}; lead-only implementation. "
                        + ("x" * 5_000)
                    ),
                },
            ]
        )
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: runtime,
            secret_resolver=lambda _reference: fixture_secret,
            max_actions=3,
        )

        self.assertIsNone(service.execute("run-1"))

        assignments = TeamService(self.db).assignments_for_run("run-1")
        self.assertEqual(len(assignments), 2)
        self.assertLessEqual(len(assignments[0].reasoning), 2_000)
        history = (
            self.checkout.parent / "agent-state" / "action-history.json"
        ).read_text(encoding="utf-8")
        observed = assignments[0].reasoning + "\n" + history
        self.assertIn("[REDACTED]", observed)
        self.assertNotIn(fixture_secret, observed)
        self.assertNotIn(json.dumps(fixture_secret)[1:-1], observed)

    def test_stored_lead_selects_atomic_implementer_for_small_issue(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM agent_assignments WHERE run_id='run-1'")
        reason = "The one-line source change needs the stored implementation member."
        assigning = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": ["lead", "implementation", "verification"],
                    "reason": reason,
                }
            ]
        )
        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _stored_timeout: assigning,
        )

        self.assertIsNone(first.execute("run-1"))

        assignments = TeamService(self.db).assignments_for_run("run-1")
        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead", "implementation", "verification"],
        )
        self.assertEqual(assignments[0].reasoning, reason)
        service, _runtime = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented the focused change"},
            ]
        )
        self.assertIsNotNone(service.execute("run-1"))

    def test_stored_lead_assigns_and_runs_selected_team_members_across_restart(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM agent_assignments WHERE run_id='run-1'")
            connection.execute(
                "UPDATE team_members SET model='test/lead' WHERE id='lead-1'"
            )
            connection.execute("""UPDATE team_members
                   SET instructions=CASE stable_key
                     WHEN 'implementation' THEN 'Follow implementation instructions'
                     WHEN 'verification' THEN 'Follow verification instructions'
                     ELSE instructions
                   END
                   WHERE team_version_id='team-1'""")
        assignment_reason = "Implementation changes the function; verification independently runs its test."
        assigning_lead = ScriptedRuntime(
            [
                {
                    "action": "assign",
                    "members": ["lead", "implementation", "verification"],
                    "reason": assignment_reason,
                }
            ]
        )
        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _stored_timeout: assigning_lead,
            max_actions=5,
        )

        self.assertIsNone(first.execute("run-1"))

        assignments = TeamService(self.db).assignments_for_run("run-1")
        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead", "implementation", "verification"],
        )
        self.assertTrue(
            all(assignment.reasoning == assignment_reason for assignment in assignments)
        )
        self.assertIn('"stable_key": "implementation"', assigning_lead.contexts[0])
        self.assertIn('"repository_evidence"', assigning_lead.contexts[0])

        lead = ScriptedRuntime(
            [{"action": "finish", "summary": "integrated member work"}]
        )
        implementation = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented return value"},
            ]
        )

        class FailOnceRuntime(ScriptedRuntime):
            def __init__(self, actions: list[dict[str, object]]) -> None:
                super().__init__(actions)
                self.failed = False

            def next_action(
                self, context: str, state_directory: Path
            ) -> dict[str, object]:
                if not self.failed:
                    self.failed = True
                    self.contexts.append(context)
                    raise TimeoutError("verification inference interrupted")
                return super().next_action(context, state_directory)

        verification = FailOnceRuntime(
            [
                {
                    "action": "run",
                    "argv": ["python3", "-m", "unittest", "test_value.py"],
                },
                {"action": "finish", "summary": "verified return value"},
            ]
        )
        runtimes = {
            "test/lead": lead,
            "test/implementation": implementation,
            "test/verification": verification,
        }
        runtime_requests: list[tuple[str, str, float]] = []

        def runtime_factory(
            stored_runtime: str,
            stored_model: str,
            stored_timeout: float,
        ):
            runtime_requests.append((stored_runtime, stored_model, stored_timeout))
            return runtimes[stored_model]

        restarted = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=runtime_factory,
            max_actions=10,
        )

        with self.assertRaisesRegex(TimeoutError, "verification inference interrupted"):
            restarted.execute("run-1")
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")

        resumed = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=runtime_factory,
            max_actions=10,
        )
        validated_sha = resumed.execute("run-1")

        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertEqual(
            runtime_requests,
            [
                ("mini-swe-agent", "test/lead", 300),
                ("mini-swe-agent", "test/implementation", 300),
                ("mini-swe-agent", "test/verification", 300),
                ("mini-swe-agent", "test/lead", 300),
                ("mini-swe-agent", "test/verification", 300),
            ],
        )
        self.assertIn('"stable_key": "implementation"', implementation.contexts[0])
        self.assertIn("Follow implementation instructions", implementation.contexts[0])
        self.assertIn('"stable_key": "verification"', verification.contexts[0])
        self.assertIn("Follow verification instructions", verification.contexts[0])
        self.assertIn(
            "Member implementation finished: implemented return value",
            lead.contexts[0],
        )
        self.assertIn(
            "Lead finished: integrated member work",
            verification.contexts[0],
        )

    def test_lead_repeats_one_assigned_member_across_restart(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES ('other-1', 'team-1', 'other', 'implementer',
                           'unrelated maintainer', 'Maintain unrelated source',
                           '["read","write","run","git_diff"]',
                           'mini-swe-agent', 'test/other', '')"""
            )
        team_service = TeamService(self.db)
        team_service.assign(
            "run-1",
            ("lead", "implementation", "other", "verification"),
            "Complete the existing issue with independent verification.",
        )
        self.lifecycle.transition("run-1", RunState.IMPLEMENTING)
        requesting_lead = ScriptedRuntime(
            [
                {
                    "action": "revise",
                    "members": ["implementation"],
                    "reason": "Remove the remaining out-of-scope source change.",
                }
            ]
        )
        unused = ScriptedRuntime([])
        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=team_service,
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, model, _timeout: (
                requesting_lead if model == "test/lead" else unused
            ),
            max_actions=10,
            max_revision_cycles=1,
        )
        layout = RunLayout.create(self.data_root, "repo-1", "run-1")
        prior_history = [
            "Member implementation finished: completed the prior candidate",
            "Member other finished: completed unrelated prior work",
        ]
        prior_history.extend(
            f"Action retained prior evidence {index}\nResult observed"
            for index in range(30)
        )
        first._store_transcript(layout, prior_history)
        before = {
            assignment.id: assignment.reasoning
            for assignment in team_service.assignments_for_run("run-1")
        }

        self.assertIsNone(first.execute("run-1"))

        interrupted = first._load_transcript(layout)
        interrupted.extend(
            f"Action after handoff {index}\nResult observed"
            for index in range(30)
        )
        first._store_transcript(layout, interrupted)
        interrupted = first._load_transcript(layout)
        self.assertTrue(
            any(
                item.startswith(
                    "Revision requested for member implementation:"
                )
                for item in interrupted
            )
        )
        self.assertEqual(
            {
                assignment.id: assignment.reasoning
                for assignment in team_service.assignments_for_run("run-1")
            },
            before,
        )
        implementation = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "removed the remaining change"},
            ]
        )
        lead = ScriptedRuntime(
            [{"action": "finish", "summary": "integrated the targeted revision"}]
        )
        verification = ScriptedRuntime(
            [{"action": "finish", "summary": "targeted revision approved"}]
        )
        other = ScriptedRuntime([])
        runtimes = {
            "test/lead": lead,
            "test/implementation": implementation,
            "test/other": other,
            "test/verification": verification,
        }
        resumed = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, model, _timeout: runtimes[model],
            max_actions=10,
            max_revision_cycles=1,
        )

        self.assertIsNotNone(resumed.execute("run-1"))
        self.assertEqual(other.contexts, [])
        self.assertIn(
            "Revision requested for member implementation:",
            implementation.contexts[0],
        )

    def test_targeted_revision_rejects_invalid_requesters_and_members(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES ('other-1', 'team-1', 'other', 'implementer',
                           'unassigned maintainer', 'Maintain unrelated source',
                           '["read","write","run","git_diff"]',
                           'mini-swe-agent', 'test/other', '')"""
            )
        team_service = TeamService(self.db)
        team_service.assign(
            "run-1",
            ("lead", "implementation", "verification"),
            "Complete the existing issue with independent verification.",
        )
        self.lifecycle.transition("run-1", RunState.IMPLEMENTING)
        implementation = ScriptedRuntime(
            [
                {
                    "action": "revise",
                    "members": ["implementation"],
                    "reason": "A non-lead cannot request another execution.",
                },
                {"action": "finish", "summary": "implementation unchanged"},
            ]
        )
        invalid_lead_actions = [
            {
                "action": "revise",
                "members": members,
                "reason": "Invalid targeted revision request.",
            }
            for members in (
                [],
                ["implementation", "implementation"],
                ["lead"],
                ["verification"],
                ["missing"],
                ["other"],
            )
        ]
        invalid_lead_actions.append(
            {
                "action": "block",
                "reason": "invalid requests were safely rejected",
            }
        )
        lead = ScriptedRuntime(invalid_lead_actions)
        verification = ScriptedRuntime([])
        runtimes = {
            "test/lead": lead,
            "test/implementation": implementation,
            "test/verification": verification,
        }
        before = {
            assignment.id
            for assignment in team_service.assignments_for_run("run-1")
        }
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=team_service,
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, model, _timeout: runtimes[model],
            max_actions=10,
            max_revision_cycles=1,
        )

        self.assertIsNone(service.execute("run-1"))

        history = service._load_transcript(
            RunLayout.create(self.data_root, "repo-1", "run-1")
        )
        self.assertFalse(
            any(
                item.startswith("Revision requested for member ")
                for item in history
            )
        )
        rejections = "\n".join(history)
        self.assertIn("only the stored lead", rejections)
        self.assertIn("nonempty and unique", rejections)
        self.assertIn("lead or independent verifier", rejections)
        self.assertIn("currently assigned implementation members", rejections)
        self.assertEqual(
            {
                assignment.id
                for assignment in team_service.assignments_for_run("run-1")
            },
            before,
        )

    def test_action_history_is_bounded_for_model_context(self) -> None:
        actions: list[dict[str, object]] = [
            {
                "action": "run",
                "argv": ["python3", "-c", "print('x' * 20000)"],
                "timeout": 30,
            }
            for _ in range(16)
        ]
        actions.extend(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "bounded history"},
            ]
        )
        service, runtime = self.service(actions)
        self.assertIsNotNone(service.execute("run-1"))
        self.assertLess(max(len(context) for context in runtime.contexts), 120_000)

    def test_action_quantum_yields_and_restart_continues_same_run_without_block(
        self,
    ) -> None:
        first_runtime = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                }
            ]
        )
        first = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: first_runtime,
            max_actions=1,
            max_revision_cycles=3,
        )

        self.assertIsNone(first.execute("run-1"))
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")
        with self.db.connect() as connection:
            blocked = connection.execute(
                "SELECT COUNT(*) FROM run_transitions WHERE run_id=? AND to_state='blocked'",
                ("run-1",),
            ).fetchone()[0]
        self.assertEqual(blocked, 0)

        second_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "continued after execution quantum"}]
        )
        lead_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "integrated continued work"}]
        )
        verifier_runtime = ScriptedRuntime(
            [{"action": "finish", "summary": "continued candidate approved"}]
        )
        restarted = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, model, _stored_timeout: (
                lead_runtime
                if model == "test/lead"
                else verifier_runtime
                if model == "test/verification"
                else second_runtime
            ),
            max_actions=1,
            max_revision_cycles=3,
        )

        validated_sha = restarted.execute("run-1")

        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertIn("Action history:", second_runtime.contexts[0])
        self.assertIn('"action":"replace"', second_runtime.contexts[0])

    def test_transient_inference_failure_preserves_run_for_next_execution(self) -> None:
        class FailOnceRuntime:
            def __init__(self) -> None:
                self.calls = 0

            def next_action(
                self, context: str, state_directory: Path
            ) -> dict[str, object]:
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("temporary model timeout")
                if self.calls == 2:
                    return {
                        "action": "replace",
                        "path": "value.py",
                        "old": "return 1",
                        "new": "return 2",
                        "count": 1,
                    }
                return {"action": "finish", "summary": "recovered automatically"}

        runtime = FailOnceRuntime()
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _stored_runtime, _stored_model, _stored_timeout: runtime,
            max_actions=10,
        )

        with self.assertRaisesRegex(TimeoutError, "temporary model timeout"):
            service.execute("run-1")

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "implementing")
        with self.db.connect() as connection:
            blocked = connection.execute(
                "SELECT COUNT(*) FROM run_transitions WHERE run_id=? AND to_state='blocked'",
                ("run-1",),
            ).fetchone()[0]
        self.assertEqual(blocked, 0)
        self.assertIsNotNone(service.execute("run-1"))
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")

    def test_transient_validation_infrastructure_failure_resumes_validation(
        self,
    ) -> None:
        first, _runtime = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "ready for validation"},
            ]
        )
        with mock.patch.object(
            first, "_validate", side_effect=RuntimeError("temporary validator outage")
        ):
            with self.assertRaisesRegex(RuntimeError, "temporary validator outage"):
                first.execute("run-1")

        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "validating")
        restarted, runtime = self.service([])
        self.assertIsNotNone(restarted.execute("run-1"))
        self.assertEqual(runtime.contexts, [])
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")

    def test_source_revision_reason_reaches_agent_context(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET state='implementing',
                       reason='scope review rejected: Docker cache optimization is unrelated'
                   WHERE id='run-1'""")
        service, runtime = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "removed unrelated change"},
            ]
        )
        self.assertIsNotNone(service.execute("run-1"))
        self.assertIn("Docker cache optimization is unrelated", runtime.contexts[0])

    def test_missing_repository_path_is_returned_to_agent_for_recovery(self) -> None:
        service, runtime = self.service(
            [
                {"action": "list", "path": "spec"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "recovered from absent optional path"},
            ]
        )
        validated_sha = service.execute("run-1")
        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertTrue(
            any("repository tool failed" in context for context in runtime.contexts)
        )

    def test_restart_from_validating_resumes_dirty_checkout_without_reasking_agent(
        self,
    ) -> None:
        self._seed_default_delta_baseline()
        (self.checkout / "value.py").write_text(
            "def value():\n    return 2\n", encoding="utf-8"
        )
        with self.db.transaction() as connection:
            connection.execute("""UPDATE runs
                   SET state='validating', last_completed_state='implementing'
                   WHERE id='run-1'""")
        service, runtime = self.service([])
        validated_sha = service.execute("run-1")
        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), validated_sha)
        self.assertEqual(runtime.contexts, [])
        with self.db.connect() as connection:
            result = connection.execute(
                "SELECT commit_sha, exit_status FROM validation_results"
            ).fetchone()
        self.assertEqual(
            (result["commit_sha"], result["exit_status"]), (validated_sha, 0)
        )

    def test_failed_validation_returns_to_agent_then_records_new_passing_sha(
        self,
    ) -> None:
        self._amend_base(
            {
                "test_value.py": (
                    "import unittest\n"
                    "from value import value\n\n"
                    "class ValueTest(unittest.TestCase):\n"
                    "    def test_value(self):\n"
                    "        self.assertIn(value(), (1, 2))\n"
                )
            }
        )
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 3",
                    "count": 1,
                },
                {"action": "finish", "summary": "first attempt"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 3",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "corrected after validation"},
            ]
        )
        passing_sha = service.execute("run-1")
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT commit_sha, exit_status FROM validation_results ORDER BY started_at"
            ).fetchall()
        self.assertEqual([row["exit_status"] for row in rows], [1, 0])
        self.assertEqual(rows[-1]["commit_sha"], passing_sha)
        self.assertNotEqual(rows[0]["commit_sha"], rows[1]["commit_sha"])
        self.assertEqual(
            self._git(
                "rev-list",
                "--count",
                f"{self.base_sha}..{passing_sha}",
            ).strip(),
            "1",
        )
        self.assertEqual(
            self._git("show", "-s", "--format=%s", passing_sha).strip(),
            "Resolve issue #3: Return two",
        )

    def test_feedback_revision_replaces_current_controller_commit(self) -> None:
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "initial implementation"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "def value():\n    return 2\n",
                    "new": (
                        "def value():\n"
                        "    # Preserve the requested value after feedback.\n"
                        "    return 2\n"
                    ),
                    "count": 1,
                },
                {"action": "finish", "summary": "feedback revision"},
            ]
        )

        initial_sha = service.execute("run-1")
        self.assertIsNotNone(initial_sha)
        self.lifecycle.transition("run-1", RunState.WAITING_FOR_FEEDBACK)
        self.lifecycle.transition("run-1", RunState.RESOLVING_FEEDBACK)
        feedback_context = "Apply the accepted pull-request feedback."
        self.specifications.bind_context(
            run_id="run-1",
            issue_version_id=self.issue_version_id,
            context_sha256=hashlib.sha256(
                feedback_context.encode("utf-8")
            ).hexdigest(),
            specification_revision_id=str(
                self.specifications.require_approved(
                    "run-1",
                    self.issue_version_id,
                )["id"]
            ),
        )

        revised_sha = service.execute(
            "run-1",
            additional_context=feedback_context,
        )

        self.assertIsNotNone(revised_sha)
        self.assertNotEqual(revised_sha, initial_sha)
        self.assertEqual(
            self._git(
                "rev-list",
                "--count",
                f"{self.base_sha}..{revised_sha}",
            ).strip(),
            "1",
        )
        self.assertEqual(
            self._git("rev-parse", f"{revised_sha}^").strip(),
            self.base_sha,
        )
        with self.db.connect() as connection:
            validation_shas = {
                str(row["commit_sha"])
                for row in connection.execute(
                    "SELECT commit_sha FROM validation_results WHERE run_id='run-1'"
                ).fetchall()
            }
        self.assertEqual(validation_shas, {initial_sha, revised_sha})

    def test_first_controller_commit_preserves_non_controller_head(self) -> None:
        self._seed_default_delta_baseline()
        (self.checkout / "maintainer-note.txt").write_text(
            "preserve this commit\n",
            encoding="utf-8",
        )
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "maintainer preparation",
        )
        maintainer_sha = self._git("rev-parse", "HEAD").strip()
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implementation"},
            ]
        )

        validated_sha = service.execute("run-1")

        self.assertIsNotNone(validated_sha)
        self.assertEqual(
            self._git("rev-parse", f"{validated_sha}^").strip(),
            maintainer_sha,
        )
        self.assertEqual(
            self._git(
                "rev-list",
                "--count",
                f"{self.base_sha}..{validated_sha}",
            ).strip(),
            "2",
        )

    def test_missing_validation_commands_block_without_autonomous_resume(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM validation_commands")
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implementation complete"},
            ]
        )
        self.assertIsNone(service.execute("run-1"))
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("validation commands", run["reason"])

    def test_validation_mutation_cannot_pass_and_returns_to_agent(self) -> None:
        self._seed_default_delta_baseline()
        mutating_command = [
            "python3",
            "-c",
            "from pathlib import Path; Path('value.py').write_text('def value():\\n    return 99\\n')",
        ]
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE validation_commands SET command_json=?",
                (json.dumps(mutating_command),),
            )
        service, _ = self.service(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implementation complete"},
            ]
        )
        service.max_revision_cycles = 1
        self.assertIsNone(service.execute("run-1"))
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIsNone(run["validated_sha"])
        self.assertIn("baseline command changed", run["reason"])
        self.assertEqual(
            self._git("status", "--porcelain", "--untracked-files=no"),
            "",
        )
        self.assertEqual(
            (self.checkout / "value.py").read_text(encoding="utf-8"),
            "def value():\n    return 1\n",
        )

    def test_path_escape_error_is_returned_to_agent_for_safe_correction(self) -> None:
        service, runtime = self.service(
            [
                {"action": "read", "path": "../../etc/passwd"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "used an isolated path"},
            ]
        )

        result = service.execute("run-1")

        self.assertIsNotNone(result)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertTrue(
            any(
                '"action":"read","path":"../../etc/passwd"' in context
                for context in runtime.contexts
            )
        )
        self.assertNotIn("root:", "\n".join(runtime.contexts))

    def test_large_rejected_write_retains_action_and_path(self) -> None:
        service, runtime = self.service(
            [
                {
                    "action": "write",
                    "path": "../../escape.txt",
                    "content": "x" * 5_000,
                },
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "corrected rejected write"},
            ]
        )

        self.assertIsNotNone(service.execute("run-1"))
        self.assertIn(
            '"action":"write","path":"../../escape.txt"',
            runtime.contexts[1],
        )

    def test_verifier_rejection_revises_before_commit_and_validation(self) -> None:
        implementation = ScriptedRuntime(
            [
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2  # unreviewed",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented first candidate"},
                {
                    "action": "replace",
                    "path": "value.py",
                    "old": "return 2  # unreviewed",
                    "new": "return 2",
                    "count": 1,
                },
                {"action": "finish", "summary": "implemented verifier correction"},
            ]
        )
        verification = ScriptedRuntime(
            [
                {
                    "action": "block",
                    "reason": "Remove the unreviewed marker before approval",
                },
                {"action": "finish", "summary": "corrected candidate approved"},
            ]
        )
        lead = ScriptedRuntime(
            [
                {"action": "finish", "summary": "integrated first candidate"},
                {"action": "finish", "summary": "integrated reviewed candidate"},
            ]
        )
        runtimes = {
            "test/lead": lead,
            "test/implementation": implementation,
            "test/verification": verification,
        }
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=(lambda _runtime, model, _timeout: runtimes[model]),
            max_actions=10,
        )

        validated_sha = service.execute("run-1")

        self.assertIsNotNone(validated_sha)
        self.assertEqual(self.lifecycle.get_run("run-1")["state"], "publishing")
        self.assertEqual(len(verification.contexts), 2)
        self.assertIn(
            "Remove the unreviewed marker before approval",
            implementation.contexts[2],
        )
        self.assertEqual(
            (self.checkout / "value.py").read_text(encoding="utf-8"),
            "def value():\n    return 2\n",
        )
        with self.db.connect() as connection:
            assignment_count = connection.execute(
                """SELECT COUNT(*) FROM agent_assignments
                   WHERE run_id='run-1' AND team_member_id='verification-1'"""
            ).fetchone()[0]
            blocked = connection.execute(
                """SELECT COUNT(*) FROM run_transitions
                   WHERE run_id='run-1' AND to_state='blocked'"""
            ).fetchone()[0]
            validation_count = connection.execute(
                "SELECT COUNT(*) FROM validation_results WHERE run_id='run-1'"
            ).fetchone()[0]
        self.assertEqual(assignment_count, 1)
        self.assertEqual(blocked, 0)
        self.assertEqual(validation_count, 1)

    def test_model_block_action_records_irreducible_reason(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("""DELETE FROM agent_assignments
                   WHERE run_id='run-1' AND team_member_id='implementation-1'""")
        runtime = ScriptedRuntime(
            [{"action": "block", "reason": "Required licensed SDK is not available"}]
        )
        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=lambda _runtime, _model, _timeout: runtime,
            max_actions=10,
        )
        self.assertIsNone(service.execute("run-1"))
        run = self.lifecycle.get_run("run-1")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("licensed SDK", run["reason"])


class MiniSweExecutionIntegrationTests(unittest.TestCase):
    """Tests for mini-SWE runtime integration: persistence, rejection, cancellation."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'repo-url', 'main', 'ready', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, '{}', ?)""",
                (now,),
            )
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('lead-1', 'team-1', 'lead', 'lead', 'Own result',
                           '[\"read\",\"write\",\"run\"]',
                           'mini-swe-agent', 'openai/gpt-4', '', 321)""")

    def test_stored_runtime_model_timeout_survives_database_reopen(self) -> None:
        reopened = Database(self.db.path)
        reopened.initialize()
        with reopened.connect() as connection:
            row = connection.execute("""SELECT runtime, model, action_timeout_seconds
                   FROM team_members WHERE id='lead-1'""").fetchone()
        self.assertEqual(row["runtime"], "mini-swe-agent")
        self.assertEqual(row["model"], "openai/gpt-4")
        self.assertEqual(row["action_timeout_seconds"], 321)

    def test_obsolete_omp_runtime_rejected_without_fallback(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('legacy-1', 'team-1', 'legacy', 'verifier', 'Legacy member',
                           '[\"read\"]', 'omp', 'openai/legacy', '', 300)""")
        reopened = Database(self.db.path)
        reopened.initialize()
        with reopened.connect() as connection:
            row = connection.execute(
                """SELECT runtime FROM team_members WHERE id='legacy-1'"""
            ).fetchone()
        self.assertEqual(row["runtime"], "omp")


class MiniSweRejectionTests(unittest.TestCase):
    """Tests that obsolete 'omp' runtime is rejected at execution time."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.checkout = (
            self.data_root / "repositories" / "repo-1" / "runs" / "run-1" / "checkout"
        )
        self.checkout.mkdir(parents=True)
        (self.checkout / "value.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "base",
        )
        self.base_sha = self._git("rev-parse", "HEAD").strip()
        self.sandbox_root = self.data_root / "repositories" / "repo-1" / "sandbox" / "1"
        self.sandbox_root.mkdir(parents=True)
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        run_root = self.checkout.parent
        policy = {
            "persistent_root": str(self.sandbox_root),
            "allowed_host_paths": [],
            "allowed_services": [],
            "secret_bindings": [],
        }
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'repo-url', 'main', 'ready', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, ?, ?, ?, ?)""",
                (
                    str(self.sandbox_root),
                    json.dumps(policy),
                    json.dumps(
                        {"instruction_files": [], "summary": "small Python fixture"}
                    ),
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO validation_commands
                   (id, sandbox_version_id, position, command_json, source, required)
                   VALUES ('validation-command-1', 'sandbox-1', 0, ?, 'fixture', 1)""",
                (
                    json.dumps(
                        [
                            "python3",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            ".",
                            "-p",
                            "test_*.py",
                        ]
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, '{}', ?)""",
                (now,),
            )
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES
                   ('lead-1', 'team-1', 'lead', 'lead', 'delivery coordinator',
                    'Coordinate result', '["read","git_diff","git_commit"]',
                    'omp', 'test/stored', ''),
                   ('implementation-1', 'team-1', 'implementation', 'implementer',
                    'python implementation maintainer', 'Implement changes',
                    '["read","write","run","git_diff"]',
                    'mini-swe-agent', 'test/stored', ''),
                   ('verification-1', 'team-1', 'verification', 'verifier',
                    'python behavior verifier', 'Verify behavior',
                    '["read","run","git_diff"]',
                    'mini-swe-agent', 'test/stored', '')""")
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 3, 'issue-url',
                           'Return two', 'Make value() return 2', '[]', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-1', 'repo-1', 'issue-1', 'event-1', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, checkout_path, run_path, created_at, updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'activation-1',
                           'sandbox-1', 'team-1', 'main', ?, 'queued', ?, ?, ?, ?)""",
                (self.base_sha, str(self.checkout), str(run_root), now, now),
            )
            connection.execute(
                """INSERT INTO agent_assignments
                   (id, run_id, team_member_id, reasoning, assigned_at)
                   VALUES ('fixture-assignment', 'run-1', 'lead-1',
                           'Explicit fixture lead assignment', ?)""",
                (now,),
            )
            connection.execute(
                """UPDATE repositories SET current_sandbox_version_id='sandbox-1',
                                             current_team_version_id='team-1'
                   WHERE id='repo-1'"""
            )
        self.sandbox = SandboxManager()
        self.lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.data_root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=self.sandbox,
        )

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.checkout,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout

    def test_obsolete_omp_stored_runtime_rejected_at_execution(self) -> None:
        """Stored team member with runtime='omp' must be rejected without fallback."""

        def factory(runtime: str, model: str, timeout: float) -> object:
            self.fail(
                f"obsolete runtime reached factory instead of being rejected: "
                f"{runtime!r}, {model!r}, {timeout!r}"
            )

        service = ExecutionService(
            database=self.db,
            lifecycle=self.lifecycle,
            teams=TeamService(self.db),
            sandbox=self.sandbox,
            runtime_factory=factory,
            max_actions=10,
        )
        with self.assertRaisesRegex(ValueError, "unsupported stored model runtime"):
            service.execute("run-1")


if __name__ == "__main__":
    unittest.main()
