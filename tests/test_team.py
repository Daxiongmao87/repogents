from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repogents.database import Database
from repogents.onboarding import RepositoryInspection
from repogents.team import EvidenceTeamFormulator, TeamService, validate_team_members


class TeamTests(unittest.TestCase):
    def test_team_validation_requires_atomic_coordinator_implementer_and_verifier(
        self,
    ) -> None:
        coordinator = {
            "stable_key": "coordination",
            "role": "delivery coordinator",
            "execution_class": "lead",
            "coordinates": True,
            "independent_verifier": False,
            "responsibilities": "Coordinate work.",
            "permitted_tools": ["read", "git_diff", "git_commit"],
            "runtime": "mini-swe-agent",
            "model": "configured",
            "instructions": "",
        }
        implementer = {
            "stable_key": "implementation",
            "role": "Python application maintainer",
            "execution_class": "implementer",
            "coordinates": False,
            "independent_verifier": False,
            "responsibilities": "Implement Python application changes.",
            "permitted_tools": ["read", "write", "run", "git_diff"],
            "runtime": "mini-swe-agent",
            "model": "configured",
            "instructions": "",
        }
        verifier = {
            "stable_key": "verification",
            "role": "behavior verifier",
            "execution_class": "verifier",
            "coordinates": False,
            "independent_verifier": True,
            "responsibilities": "Verify behavior independently.",
            "permitted_tools": ["read", "run"],
            "runtime": "mini-swe-agent",
            "model": "configured",
            "instructions": "",
        }
        validate_team_members([coordinator, implementer, verifier])
        with self.assertRaisesRegex(ValueError, "coordinating member"):
            validate_team_members([implementer, verifier])
        with self.assertRaisesRegex(ValueError, "coordinating member"):
            validate_team_members(
                [
                    coordinator,
                    dict(
                        coordinator,
                        stable_key="other-coordinator",
                        role="release coordinator",
                    ),
                    implementer,
                    verifier,
                ]
            )
        with self.assertRaisesRegex(ValueError, "independent verifier"):
            validate_team_members([coordinator, implementer])
        with self.assertRaisesRegex(ValueError, "implementation member"):
            validate_team_members([coordinator, verifier])
        with self.assertRaisesRegex(ValueError, "identities"):
            validate_team_members(
                [
                    coordinator,
                    dict(implementer, stable_key="coordination"),
                    verifier,
                ]
            )
        with self.assertRaisesRegex(ValueError, "coordinating member.*write"):
            validate_team_members(
                [
                    dict(
                        coordinator,
                        permitted_tools=["read", "write"],
                    ),
                    implementer,
                    verifier,
                ]
            )
        with self.assertRaisesRegex(ValueError, "verifier.*write"):
            validate_team_members(
                [
                    coordinator,
                    implementer,
                    dict(verifier, permitted_tools=["read", "write", "run"]),
                ]
            )
        with self.assertRaisesRegex(ValueError, "action timeout"):
            validate_team_members(
                [
                    dict(coordinator, action_timeout_seconds=0),
                    implementer,
                    verifier,
                ]
            )
        with self.assertRaisesRegex(ValueError, "stable key"):
            validate_team_members(
                [
                    coordinator,
                    dict(implementer, stable_key="Invalid Key"),
                    verifier,
                ]
            )
        with self.assertRaisesRegex(ValueError, "model"):
            validate_team_members([coordinator, dict(implementer, model=""), verifier])

    @patch("repogents.team.MiniSweInference")
    def test_configured_agent_designs_atomic_team_from_repository_evidence(
        self,
        inference_type: object,
    ) -> None:
        inference = inference_type.return_value
        inference.infer.return_value = {
            "members": [
                {
                    "stable_key": "coordination",
                    "role": "delivery coordinator",
                    "coordinates": True,
                    "independent_verifier": False,
                    "responsibilities": (
                        "Coordinate assignments and integrate completed work."
                    ),
                    "permitted_tools": ["read", "git_diff", "git_commit"],
                },
                {
                    "stable_key": "battle-engine",
                    "role": "turn-based battle engine developer",
                    "coordinates": False,
                    "independent_verifier": False,
                    "responsibilities": (
                        "Implement turn-based battle engine behavior."
                    ),
                    "permitted_tools": ["read", "write", "run", "git_diff"],
                },
                {
                    "stable_key": "schema-verification",
                    "role": "battle schema compatibility verifier",
                    "coordinates": False,
                    "independent_verifier": True,
                    "responsibilities": (
                        "Independently verify battle schema compatibility."
                    ),
                    "permitted_tools": ["read", "run", "git_diff"],
                },
            ]
        }
        observed_classes: list[str] = []

        def resolve(execution_class: str) -> str:
            observed_classes.append(execution_class)
            return f"openai/{execution_class}"

        with tempfile.TemporaryDirectory() as state_root:
            formulator = EvidenceTeamFormulator(
                runtime="mini-swe-agent",
                model="openai/team-architect",
                model_resolver=resolve,
                state_root=Path(state_root),
                action_timeout_seconds=601,
            )
            inspection = RepositoryInspection(
                languages=("python",),
                manifests=("pyproject.toml",),
                lockfiles=(),
                instruction_files=("README.md",),
                validation_commands=(("python3", "-m", "pytest"),),
                file_count=20,
                summary="small Python battle simulator",
                instructions=(("README.md", "Battle schemas are public."),),
                source_files=("src/martite/battle.py", "tests/test_battle.py"),
            )

            members = formulator.formulate(inspection)

        self.assertEqual(
            [member["stable_key"] for member in members],
            ["coordination", "battle-engine", "schema-verification"],
        )
        self.assertEqual(
            [member["role"] for member in members],
            [
                "delivery coordinator",
                "turn-based battle engine developer",
                "battle schema compatibility verifier",
            ],
        )
        self.assertEqual(
            [member["execution_class"] for member in members],
            ["lead", "implementer", "verifier"],
        )
        self.assertEqual(
            [member["model"] for member in members],
            ["openai/lead", "openai/implementer", "openai/verifier"],
        )
        self.assertEqual(
            observed_classes,
            ["lead", "implementer", "verifier"],
        )
        self.assertTrue(
            all(member["runtime"] == "mini-swe-agent" for member in members)
        )
        self.assertTrue(
            all(member["action_timeout_seconds"] == 601 for member in members)
        )

        request = inference.infer.call_args.kwargs
        packet = json.loads(request["prompt"])
        self.assertEqual(packet["repository"]["file_count"], 20)
        self.assertIn(
            "src/martite/battle.py",
            packet["repository"]["source_files"],
        )
        self.assertIn("design", packet["task"].lower())
        self.assertIn("atomic", packet["task"].lower())
        self.assertIn(
            "must not implement or verify",
            packet["task"].lower(),
        )
        self.assertIn("role names", packet["task"].lower())

    @patch("repogents.team.MiniSweInference")
    def test_invalid_or_failed_agent_design_has_no_fallback_team(
        self,
        inference_type: object,
    ) -> None:
        inference = inference_type.return_value
        inference.infer.return_value = {
            "members": [
                {
                    "stable_key": "verification",
                    "role": "repository behavior verifier",
                    "coordinates": False,
                    "independent_verifier": True,
                    "responsibilities": "Verify repository behavior.",
                    "permitted_tools": ["read", "run"],
                }
            ]
        }
        inspection = RepositoryInspection(
            languages=("python",),
            manifests=("pyproject.toml",),
            lockfiles=(),
            instruction_files=(),
            validation_commands=(("python3", "-m", "unittest"),),
            file_count=10,
            summary="small repository",
        )
        with tempfile.TemporaryDirectory() as state_root:
            formulator = EvidenceTeamFormulator(
                runtime="mini-swe-agent",
                model="openai/team-architect",
                state_root=Path(state_root),
            )
            with self.assertRaisesRegex(ValueError, "coordinating member"):
                formulator.formulate(inspection)

            inference.infer.side_effect = RuntimeError("model unavailable")
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                formulator.formulate(inspection)

    def test_team_validation_rejects_unsupported_controller_tool(self) -> None:
        coordinator = {
            "stable_key": "coordination",
            "role": "delivery coordinator",
            "execution_class": "lead",
            "coordinates": True,
            "independent_verifier": False,
            "responsibilities": "Coordinate work.",
            "permitted_tools": ["read", "network"],
            "runtime": "mini-swe-agent",
            "model": "configured",
            "instructions": "",
        }
        implementer = {
            "stable_key": "implementation",
            "role": "Python maintainer",
            "execution_class": "implementer",
            "coordinates": False,
            "independent_verifier": False,
            "responsibilities": "Implement Python changes.",
            "permitted_tools": ["read", "write", "run"],
            "runtime": "mini-swe-agent",
            "model": "configured",
            "instructions": "",
        }
        verifier = {
            "stable_key": "verification",
            "role": "behavior verifier",
            "execution_class": "verifier",
            "coordinates": False,
            "independent_verifier": True,
            "responsibilities": "Verify repository behavior.",
            "permitted_tools": ["read", "run"],
            "runtime": "mini-swe-agent",
            "model": "configured",
            "instructions": "",
        }
        with self.assertRaisesRegex(ValueError, "unsupported"):
            validate_team_members([coordinator, implementer, verifier])


class TeamServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db = Database(Path(self.tempdir.name) / "db.sqlite3")
        self.db.initialize()
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'url', 'main', 'ready', ?, ?)""",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO sandbox_versions
                   (id, repository_id, version, root_path, policy_json, evidence_json, created_at)
                   VALUES ('sandbox-1', 'repo-1', 1, '/tmp/sandbox', '{}', '{}', ?)""",
                ("2026-01-01T00:00:00Z",),
            )
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json,
                    design_contract_version, created_at)
                   VALUES ('team-1', 'repo-1', 1, ?, 2, ?)""",
                (json.dumps({"languages": ["python"]}), "2026-01-01T00:00:00Z"),
            )
            members = (
                (
                    "member-lead",
                    "lead",
                    "lead",
                    "delivery coordinator",
                    "Coordinate and integrate work",
                    '["read","git_diff","git_commit"]',
                ),
                (
                    "member-build",
                    "build",
                    "implementer",
                    "Python application maintainer",
                    "Implement repository changes",
                    '["read","write","run","git_diff"]',
                ),
                (
                    "member-verifier",
                    "verify",
                    "verifier",
                    "behavior verifier",
                    "Independently verify behavior",
                    '["read","run"]',
                ),
            )
            for (
                member_id,
                stable_key,
                role,
                atomic_role,
                responsibility,
                tools,
            ) in members:
                connection.execute(
                    """INSERT INTO team_members
                       (id, team_version_id, stable_key, role, atomic_role,
                        responsibilities, permitted_tools_json, runtime, model,
                        instructions, action_timeout_seconds)
                       VALUES (?, 'team-1', ?, ?, ?, ?, ?, 'mini-swe-agent',
                               'configured', '', 301)""",
                    (
                        member_id,
                        stable_key,
                        role,
                        atomic_role,
                        responsibility,
                        tools,
                    ),
                )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 3, 'issue-url', 'Issue', 'Body', '[]', ?)""",
                ("2026-01-01T00:00:00Z",),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-1', 'repo-1', 'issue-1', 'event-1', ?)""",
                ("2026-01-01T00:00:00Z",),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, created_at, updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'activation-1',
                           'sandbox-1', 'team-1', 'main', ?, 'queued', ?, ?)""",
                ("a" * 40, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        self.service = TeamService(self.db)

    def test_loads_stored_version_and_records_valid_assignment(self) -> None:
        team = self.service.load("team-1")
        self.assertEqual(team.version, 1)
        self.assertEqual(
            [member.stable_key for member in team.members],
            ["lead", "build", "verify"],
        )
        self.assertTrue(
            all(member.action_timeout_seconds == 301 for member in team.members)
        )
        self.assertEqual(
            [member.role for member in team.members],
            [
                "delivery coordinator",
                "Python application maintainer",
                "behavior verifier",
            ],
        )
        self.assertEqual(
            [member.execution_class for member in team.members],
            ["lead", "implementer", "verifier"],
        )
        assignments = self.service.assign(
            "run-1",
            ("lead", "build", "verify"),
            "The coordinator assigns implementation and independent verification.",
        )
        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead", "build", "verify"],
        )
        reloaded = self.service.assignments_for_run("run-1")
        self.assertEqual(len(reloaded), 3)
        self.assertTrue(all(assignment.reasoning for assignment in reloaded))

    def test_assignment_expansion_requires_a_strict_superset(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions, action_timeout_seconds)
                   VALUES ('member-database', 'team-1', 'z-database', 'implementer',
                           'database migration maintainer',
                           'Resolve later database conflicts',
                           '["read","write","run","git_diff"]',
                           'mini-swe-agent', 'configured', '', 301)"""
            )
        initial_reason = "Implement the original issue and verify it."
        self.service.assign(
            "run-1",
            ("lead", "build", "verify"),
            initial_reason,
        )
        expansion_reason = "Later feedback introduces a database conflict."

        expanded = self.service.expand_assignment(
            "run-1",
            ("lead", "build", "z-database", "verify"),
            expansion_reason,
        )

        self.assertEqual(
            [assignment.member.stable_key for assignment in expanded],
            ["lead", "build", "z-database", "verify"],
        )
        reasons = {
            assignment.member.stable_key: assignment.reasoning
            for assignment in expanded
        }
        self.assertEqual(reasons["build"], initial_reason)
        self.assertEqual(reasons["z-database"], expansion_reason)
        with self.assertRaisesRegex(ValueError, "retain every currently assigned"):
            self.service.expand_assignment(
                "run-1",
                ("lead", "z-database", "verify"),
                "Replace the original implementer.",
            )
        with self.assertRaisesRegex(ValueError, "at least one previously unassigned"):
            self.service.expand_assignment(
                "run-1",
                ("lead", "build", "z-database", "verify"),
                "Repeat the expanded assignment.",
            )
        self.assertEqual(
            [
                assignment.member.stable_key
                for assignment in self.service.assignments_for_run("run-1")
            ],
            ["lead", "build", "z-database", "verify"],
        )

    def test_existing_run_keeps_prior_team_after_repository_version_changes(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json,
                    design_contract_version, created_at)
                   VALUES ('team-2', 'repo-1', 2, '{}', 2, ?)""",
                ("2026-01-02T00:00:00Z",),
            )
            connection.executemany(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions, action_timeout_seconds)
                   VALUES (?, 'team-2', ?, ?, ?, ?, ?, 'mini-swe-agent',
                           'configured', '', 777)""",
                (
                    (
                        "member-new-lead",
                        "new-lead",
                        "lead",
                        "release coordinator",
                        "Coordinate later runs",
                        '["read","git_diff","git_commit"]',
                    ),
                    (
                        "member-new-build",
                        "new-build",
                        "implementer",
                        "release implementation maintainer",
                        "Implement later changes",
                        '["read","write","run","git_diff"]',
                    ),
                    (
                        "member-new-verify",
                        "new-verify",
                        "verifier",
                        "release behavior verifier",
                        "Verify later changes",
                        '["read","run"]',
                    ),
                ),
            )
            connection.execute(
                "UPDATE repositories SET current_team_version_id='team-2' WHERE id='repo-1'"
            )

        assignments = TeamService(Database(self.db.path)).assign(
            "run-1",
            ("lead", "build", "verify"),
            "The active run continues with the team version captured at activation.",
        )

        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead", "build", "verify"],
        )
        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT team_version_id FROM runs WHERE id='run-1'"
            ).fetchone()
            repository = connection.execute(
                "SELECT current_team_version_id FROM repositories WHERE id='repo-1'"
            ).fetchone()
        self.assertEqual(run["team_version_id"], "team-1")
        self.assertEqual(repository["current_team_version_id"], "team-2")
        self.assertTrue(
            all(
                assignment.member.action_timeout_seconds == 301
                for assignment in assignments
            )
        )
        self.assertEqual(
            self.service.load("team-2").members[0].action_timeout_seconds,
            777,
        )

    def test_legacy_team_contract_remains_loadable_for_active_run(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("""UPDATE team_versions
                   SET design_contract_version=1
                   WHERE id='team-1'""")
            connection.execute("DELETE FROM team_members WHERE id='member-build'")
            connection.execute("""UPDATE team_members
                   SET permitted_tools_json='["read","write","run","git_diff"]'
                   WHERE id='member-lead'""")

        team = self.service.load("team-1")

        self.assertEqual(
            [member.stable_key for member in team.members],
            ["lead", "verify"],
        )
        self.assertIn("write", team.members[0].permitted_tools)

    def test_rejects_obsolete_omp_runtime_in_stored_member(self) -> None:
        """Stored team member with runtime='omp' must not be used for execution."""
        with self.db.transaction() as connection:
            connection.execute("""UPDATE team_members
                   SET runtime='omp', model='openai/legacy'
                   WHERE id='member-verifier'""")
        team = self.service.load("team-1")
        legacy_member = next(m for m in team.members if m.stable_key == "verify")
        self.assertEqual(legacy_member.runtime, "omp")

    def test_rejects_assignment_without_stored_verifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "independent verifier"):
            self.service.assign(
                "run-1",
                ("lead", "build"),
                "Implementation without independent review",
            )
        self.assertEqual(self.service.assignments_for_run("run-1"), ())


    def test_rejects_assignment_outside_runs_stored_team(self) -> None:
        with self.assertRaises(ValueError):
            self.service.assign("run-1", ("unknown",), "work around missing member")


if __name__ == "__main__":
    unittest.main()
