from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from repogents.database import Database
from repogents.onboarding import RepositoryInspection
from repogents.team import EvidenceTeamFormulator, TeamService, validate_team_members


class TeamTests(unittest.TestCase):
    def test_team_validation_requires_exactly_one_lead_and_unique_keys(self) -> None:
        lead = {
            "stable_key": "lead",
            "role": "lead",
            "responsibilities": "Own result",
            "permitted_tools": ["read"],
            "runtime": "mini-swe-agent",
            "model": "configured",
            "instructions": "",
        }
        validate_team_members([lead])
        with self.assertRaises(ValueError):
            validate_team_members([])
        with self.assertRaises(ValueError):
            validate_team_members([lead, dict(lead, stable_key="other-lead")])
        with self.assertRaises(ValueError):
            validate_team_members([lead, dict(lead, role="verifier")])
        with self.assertRaisesRegex(ValueError, "action timeout"):
            validate_team_members([dict(lead, action_timeout_seconds=0)])

    def test_formulation_varies_from_repository_evidence(self) -> None:
        formulator = EvidenceTeamFormulator(
            runtime="mini-swe-agent",
            model="configured",
            action_timeout_seconds=601,
        )
        small = RepositoryInspection(
            languages=("python",),
            manifests=("pyproject.toml",),
            lockfiles=(),
            instruction_files=("README.md",),
            validation_commands=(("python3", "-m", "pytest"),),
            file_count=20,
            summary="small Python project",
        )
        complex_repository = RepositoryInspection(
            languages=("javascript", "python"),
            manifests=("package.json", "pyproject.toml"),
            lockfiles=("package-lock.json",),
            instruction_files=("AGENTS.md", "README.md"),
            validation_commands=(("npm", "test"), ("python3", "-m", "pytest")),
            file_count=2000,
            summary="multi-language project",
        )
        small_members = formulator.formulate(small)
        complex_members = formulator.formulate(complex_repository)
        self.assertEqual(
            [member["role"] for member in small_members],
            ["lead", "verifier"],
        )
        self.assertEqual(
            [member["role"] for member in complex_members],
            ["lead", "implementer", "verifier"],
        )
        self.assertNotEqual(small_members, complex_members)
        self.assertTrue(
            all(member["action_timeout_seconds"] == 601 for member in complex_members)
        )
        for members in (small_members, complex_members):
            verifier = next(
                member for member in members if member["role"] == "verifier"
            )
            self.assertEqual(verifier["permitted_tools"], ["read", "run", "git_diff"])
            self.assertNotIn("write", verifier["permitted_tools"])


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
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-1', 'repo-1', 1, ?, ?)""",
                (json.dumps({"languages": ["python"]}), "2026-01-01T00:00:00Z"),
            )
            members = (
                ("member-lead", "lead", "lead"),
                ("member-verifier", "verify", "verifier"),
            )
            for member_id, stable_key, role in members:
                connection.execute(
                    """INSERT INTO team_members
                       (id, team_version_id, stable_key, role, responsibilities,
                        permitted_tools_json, runtime, model, instructions,
                        action_timeout_seconds)
                       VALUES (?, 'team-1', ?, ?, ?, '["read"]', 'mini-swe-agent',
                               'configured', '', 301)""",
                    (member_id, stable_key, role, role),
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
            [member.stable_key for member in team.members], ["lead", "verify"]
        )
        self.assertTrue(
            all(member.action_timeout_seconds == 301 for member in team.members)
        )
        assignments = self.service.assign(
            "run-1",
            ("lead", "verify"),
            "Lead implements; verifier independently checks repository-required behavior.",
        )
        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead", "verify"],
        )
        reloaded = self.service.assignments_for_run("run-1")
        self.assertEqual(len(reloaded), 2)
        self.assertTrue(all(assignment.reasoning for assignment in reloaded))

    def test_existing_run_keeps_prior_team_after_repository_version_changes(
        self,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO team_versions
                   (id, repository_id, version, evidence_json, created_at)
                   VALUES ('team-2', 'repo-1', 2, '{}', ?)""",
                ("2026-01-02T00:00:00Z",),
            )
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('member-new-lead', 'team-2', 'new-lead', 'lead',
                           'Own later runs', '["read"]', 'mini-swe-agent', 'configured',
                           '', 777)""")
            connection.execute(
                "UPDATE repositories SET current_team_version_id='team-2' WHERE id='repo-1'"
            )

        assignments = TeamService(Database(self.db.path)).assign(
            "run-1",
            ("lead", "verify"),
            "The active run continues with the team version captured at activation.",
        )

        self.assertEqual(
            [assignment.member.stable_key for assignment in assignments],
            ["lead", "verify"],
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

    def test_rejects_obsolete_omp_runtime_in_stored_member(self) -> None:
        """Stored team member with runtime='omp' must not be used for execution."""
        with self.db.transaction() as connection:
            connection.execute("""INSERT INTO team_members
                   (id, team_version_id, stable_key, role, responsibilities,
                    permitted_tools_json, runtime, model, instructions,
                    action_timeout_seconds)
                   VALUES ('legacy-lead', 'team-1', 'legacy', 'verifier', 'Legacy check',
                           '["read"]', 'omp', 'openai/legacy', '', 300)""")
        team = self.service.load("team-1")
        legacy_member = next(m for m in team.members if m.stable_key == "legacy")
        self.assertEqual(legacy_member.runtime, "omp")

    def test_rejects_assignment_outside_runs_stored_team(self) -> None:
        with self.assertRaises(ValueError):
            self.service.assign("run-1", ("unknown",), "work around missing member")


if __name__ == "__main__":
    unittest.main()
