from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from repogents.database import Database
from repogents.lifecycle import RunLifecycle
from repogents.specification import SpecificationService, SpecificationUnavailable


class NoActivationClient:
    def list_ready_events(self, owner: str, name: str) -> list[object]:
        return []

    def get_branch_head(self, owner: str, name: str, branch: str) -> str:
        return "a" * 40


class NoCheckoutManager:
    def create(self, source: Path, base_sha: str, destination: Path) -> None:
        return None


class NoSandbox:
    pass


class SpecificationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.db = Database(self.root / "repogents.sqlite3")
        self.db.initialize()
        now = "2026-07-29T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, created_at, updated_at)
                   VALUES ('repo-1', 'R1', 'owner', 'repo', 'repo-url', 'main',
                           'ready', ?, ?)""",
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
            connection.execute(
                """INSERT INTO team_members
                   (id, team_version_id, stable_key, role, atomic_role,
                    responsibilities, permitted_tools_json, runtime, model,
                    instructions)
                   VALUES
                   ('lead-1', 'team-1', 'lead', 'lead',
                    'repository delivery planner',
                    'Coordinate delivery', '["read"]', 'mini-swe-agent',
                    'test/lead', ''),
                   ('implementation-1', 'team-1', 'implementation',
                    'implementer', 'implementation owner', 'Implement changes',
                    '["read","write"]', 'mini-swe-agent',
                    'test/implementation', ''),
                   ('verifier-1', 'team-1', 'verification', 'verifier',
                    'repository behavior reviewer',
                    'Review issue specifications independently',
                    '["read"]', 'mini-swe-agent', 'test/verifier', '')"""
            )
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', 'repo-1', 'I1', 7, 'issue-url',
                           'Set value', 'Set VALUE to 2 and prove output.', '[]', ?)""",
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
                    base_sha, state, created_at, updated_at)
                   VALUES ('run-1', 'repo-1', 'issue-1', 'activation-1',
                           'sandbox-1', 'team-1', 'main', ?, 'implementing', ?, ?)""",
                ("a" * 40, now, now),
            )
        self.lifecycle = RunLifecycle(
            database=self.db,
            data_root=self.root,
            github=NoActivationClient(),
            checkouts=NoCheckoutManager(),
            sandbox=NoSandbox(),  # type: ignore[arg-type]
        )
        self.issue_version_id = self.lifecycle.current_issue_version("run-1")
        self.service = SpecificationService(self.db)

    @staticmethod
    def items() -> list[dict[str, object]]:
        return [
            {
                "key": "value-output",
                "title": "Return and report the requested value",
                "objective": "The repository behavior returns VALUE 2 through its public command.",
                "acceptance_criteria": [
                    {
                        "key": "value-command-output",
                        "requirement": "Running the repository command exposes the requested value.",
                        "expected": "The command exits successfully and stdout contains VALUE=2.",
                    }
                ],
                "verification": [
                    {
                        "key": "run-public-command",
                        "criterion_keys": ["value-command-output"],
                        "scenario": "Run the repository-defined public command and inspect exit status and stdout.",
                    }
                ],
            }
        ]

    def submit(self, items: list[dict[str, object]] | None = None) -> dict[str, object]:
        return self.service.submit(
            run_id="run-1",
            author_member_id="lead-1",
            issue_version_id=self.issue_version_id,
            items=self.items() if items is None else items,
            reason="Specify observable issue completion before implementation.",
        )
    @staticmethod
    def rejection_findings() -> list[dict[str, object]]:
        return [
            {
                "key": "missing-output-boundary",
                "category": "coverage",
                "severity": "error",
                "summary": "The contract does not constrain unrelated output.",
                "item_keys": ["value-output"],
            }
        ]

    def review(
        self,
        specification: dict[str, object],
        *,
        verdict: str = "approved",
        findings: list[dict[str, object]] | None = None,
        blocker: str | None = None,
    ) -> dict[str, object]:
        return self.service.record_review(
            run_id="run-1",
            specification_revision_id=str(specification["id"]),
            reviewer_member_id="verifier-1",
            reviewer_model="test/verifier",
            rubric_version=1,
            verdict=verdict,
            summary=(
                "The specification covers the issue with observable criteria."
                if verdict == "approved"
                else "The specification requires a corrected revision."
            ),
            findings=[] if findings is None else findings,
            blocker=blocker,
        )


    def test_valid_submission_is_idempotent_and_changed_content_appends_revision(
        self,
    ) -> None:
        first = self.submit()
        repeated = self.submit()
        changed_items = self.items()
        changed_items[0]["objective"] = (
            "The public command returns VALUE 2 without changing unrelated output."
        )
        second = self.submit(changed_items)

        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        history = self.service.history("run-1")
        self.assertEqual([item["revision"] for item in history], [1, 2])
        self.assertEqual(history[0]["items"], self.items())
        canonical = json.dumps(
            self.items(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(history[0]["content_sha256"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(
            self.service.current("run-1", self.issue_version_id)["id"],
            second["id"],
        )

    def test_reverting_to_older_content_appends_from_the_active_revision(
        self,
    ) -> None:
        first = self.submit()
        changed_items = self.items()
        changed_items[0]["objective"] = "A temporary changed objective."
        second = self.submit(changed_items)

        reverted = self.submit()
        repeated = self.submit()

        self.assertEqual(
            [first["revision"], second["revision"], reverted["revision"]],
            [1, 2, 3],
        )
        self.assertNotEqual(first["id"], reverted["id"])
        self.assertEqual(reverted["id"], repeated["id"])
        self.assertEqual(
            [revision["revision"] for revision in self.service.history("run-1")],
            [1, 2, 3],
        )

    def test_context_reconciliation_is_durable_idempotent_and_revision_bound(
        self,
    ) -> None:
        first = self.submit()
        self.review(first)
        context_sha256 = hashlib.sha256(
            b"Pull-request feedback requires preserving unrelated output."
        ).hexdigest()

        self.assertIsNone(
            self.service.context_binding(
                "run-1",
                self.issue_version_id,
                context_sha256,
            )
        )
        bound = self.service.bind_context(
            run_id="run-1",
            issue_version_id=self.issue_version_id,
            context_sha256=context_sha256,
            specification_revision_id=str(first["id"]),
        )
        repeated = SpecificationService(self.db).bind_context(
            run_id="run-1",
            issue_version_id=self.issue_version_id,
            context_sha256=context_sha256,
            specification_revision_id=str(first["id"]),
        )

        self.assertEqual(bound["id"], repeated["id"])
        self.assertEqual(
            SpecificationService(self.db)
            .context_binding("run-1", self.issue_version_id, context_sha256)[
                "specification_revision_id"
            ],
            first["id"],
        )
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM run_specification_contexts
                       WHERE run_id='run-1' AND context_sha256=?""",
                    (context_sha256,),
                ).fetchone()[0],
                1,
            )

        changed_items = self.items()
        changed_items[0]["objective"] = (
            "The public command returns VALUE 2 without unrelated output."
        )
        second = self.submit(changed_items)
        with self.assertRaisesRegex(ValueError, "active specification"):
            self.service.bind_context(
                run_id="run-1",
                issue_version_id=self.issue_version_id,
                context_sha256=context_sha256,
                specification_revision_id=str(first["id"]),
            )
        rebound = self.service.bind_context(
            run_id="run-1",
            issue_version_id=self.issue_version_id,
            context_sha256=context_sha256,
            specification_revision_id=str(second["id"]),
        )

        self.assertNotEqual(bound["id"], rebound["id"])
        self.assertEqual(
            self.service.context_binding(
                "run-1",
                self.issue_version_id,
                context_sha256,
            )["specification_revision_id"],
            second["id"],
        )
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM run_specification_contexts
                       WHERE run_id='run-1' AND context_sha256=?""",
                    (context_sha256,),
                ).fetchone()[0],
                2,
            )
        with self.assertRaisesRegex(ValueError, "context SHA"):
            self.service.context_binding(
                "run-1",
                self.issue_version_id,
                "not-a-sha",
            )

    def test_invalid_or_unauthorized_submission_is_atomic(self) -> None:
        invalid_items: list[tuple[str, list[dict[str, object]]]] = []
        duplicate_item = self.items() + copy.deepcopy(self.items())
        invalid_items.append(("duplicate specification item key", duplicate_item))
        no_criteria = self.items()
        no_criteria[0]["acceptance_criteria"] = []
        invalid_items.append(("acceptance_criteria", no_criteria))
        no_verification = self.items()
        no_verification[0]["verification"] = []
        invalid_items.append(("verification", no_verification))
        unknown_mapping = self.items()
        unknown_mapping[0]["verification"][0]["criterion_keys"] = ["unknown-criterion"]  # type: ignore[index]
        invalid_items.append(("unknown criterion", unknown_mapping))
        unmapped = self.items()
        unmapped[0]["acceptance_criteria"].append(  # type: ignore[union-attr]
            {
                "key": "second-criterion",
                "requirement": "A second required behavior is observable.",
                "expected": "The second behavior is present.",
            }
        )
        invalid_key = self.items()
        invalid_key[0]["key"] = "Not-Kebab"
        invalid_items.append(("lowercase kebab-case", invalid_key))
        unexpected_field = self.items()
        unexpected_field[0]["implementation"] = "Change app.py."
        invalid_items.append(("unexpected field", unexpected_field))
        invalid_items.append(("must be a list", {"key": "not-a-list"}))
        invalid_items.append(("not mapped", unmapped))
        oversized = self.items()
        oversized[0]["objective"] = "x" * 4001
        invalid_items.append(("objective", oversized))

        for message, items in invalid_items:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.submit(items)
        with self.assertRaisesRegex(PermissionError, "coordinator"):
            self.service.submit(
                run_id="run-1",
                author_member_id="implementation-1",
                issue_version_id=self.issue_version_id,
                items=self.items(),
                reason="Unauthorized specification attempt.",
            )
        with self.assertRaisesRegex(ValueError, "current issue version"):
            self.service.submit(
                run_id="run-1",
                author_member_id="lead-1",
                issue_version_id="stale-version",
                items=self.items(),
                reason="Stale specification attempt.",
            )
        self.assertEqual(self.service.history("run-1"), ())

    def test_independent_review_is_authorized_bound_and_immutable(self) -> None:
        specification = self.submit()
        approved = self.review(specification)
        repeated = self.review(specification)

        self.assertEqual(approved["id"], repeated["id"])
        ready = self.service.require_approved(
            "run-1",
            self.issue_version_id,
        )
        self.assertEqual(ready["id"], specification["id"])
        self.assertEqual(ready["review"]["verdict"], "approved")
        self.assertEqual(
            [review["id"] for review in self.service.review_history("run-1")],
            [approved["id"]],
        )
        with self.assertRaisesRegex(PermissionError, "independent verifier"):
            self.service.record_review(
                run_id="run-1",
                specification_revision_id=str(specification["id"]),
                reviewer_member_id="lead-1",
                reviewer_model="test/lead",
                rubric_version=1,
                verdict="approved",
                summary="Unauthorized approval.",
                findings=[],
            )
        with self.assertRaisesRegex(ValueError, "already has an immutable review"):
            self.service.record_review(
                run_id="run-1",
                specification_revision_id=str(specification["id"]),
                reviewer_member_id="verifier-1",
                reviewer_model="test/verifier",
                rubric_version=1,
                verdict="rejected",
                summary="Contradict the durable review.",
                findings=self.rejection_findings(),
            )

    def test_rejected_specification_requires_corrected_revision_and_review(
        self,
    ) -> None:
        first = self.submit()
        rejected = self.review(
            first,
            verdict="rejected",
            findings=self.rejection_findings(),
        )
        with self.assertRaises(SpecificationUnavailable):
            self.service.require_approved("run-1", self.issue_version_id)
        next_items = self.items()
        next_items[0]["objective"] = (
            "The public command returns VALUE 2 without unrelated output."
        )
        second = self.submit(next_items)
        with self.assertRaises(SpecificationUnavailable):
            self.service.require_approved("run-1", self.issue_version_id)
        approved = self.review(second)

        ready = self.service.require_approved("run-1", self.issue_version_id)
        self.assertEqual(ready["id"], second["id"])
        self.assertEqual(ready["review"]["id"], approved["id"])
        self.assertEqual(
            [review["verdict"] for review in self.service.review_history("run-1")],
            ["rejected", "approved"],
        )
        self.assertEqual(rejected["specification_revision_id"], first["id"])

    def test_review_validation_rejects_invalid_or_stale_verdicts(self) -> None:
        first = self.submit()
        changed_items = self.items()
        changed_items[0]["objective"] = "A corrected active objective."
        self.submit(changed_items)

        with self.assertRaisesRegex(ValueError, "active specification"):
            self.review(first)
        current = self.service.current("run-1", self.issue_version_id)
        assert current is not None
        with self.assertRaisesRegex(ValueError, "rejected review requires"):
            self.review(current, verdict="rejected", findings=[])
        with self.assertRaisesRegex(ValueError, "blocked review requires"):
            self.review(
                current,
                verdict="blocked",
                findings=self.rejection_findings(),
            )
        with self.assertRaisesRegex(ValueError, "at most 2000"):
            self.review(
                current,
                verdict="blocked",
                blocker="x" * 2001,
                findings=[],
            )
        invalid_findings = (
            (
                "finding category",
                [
                    {
                        "key": "missing-category",
                        "severity": "error",
                        "summary": "Missing category.",
                        "item_keys": ["value-output"],
                    }
                ],
            ),
            (
                "finding summary",
                [
                    {
                        "key": "oversized-summary",
                        "category": "coverage",
                        "severity": "error",
                        "summary": "x" * 1001,
                        "item_keys": ["value-output"],
                    }
                ],
            ),
            (
                "unknown specification item",
                [
                    {
                        "key": "unknown-item",
                        "category": "coverage",
                        "severity": "error",
                        "summary": "References an unknown item.",
                        "item_keys": ["missing-item"],
                    }
                ],
            ),
        )
        for message, findings in invalid_findings:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.service.record_review(
                        run_id="run-1",
                        specification_revision_id=str(current["id"]),
                        reviewer_member_id="verifier-1",
                        reviewer_model="test/verifier",
                        rubric_version=1,
                        verdict="rejected",
                        summary="Invalid structured findings.",
                        findings=findings,
                    )
        with self.assertRaisesRegex(ValueError, "reviewer model"):
            self.service.record_review(
                run_id="run-1",
                specification_revision_id=str(current["id"]),
                reviewer_member_id="verifier-1",
                reviewer_model="wrong/model",
                rubric_version=1,
                verdict="approved",
                summary="Invalid reviewer identity.",
                findings=[],
            )

    def test_new_issue_version_requires_new_revision_without_rewriting_history(
        self,
    ) -> None:
        first = self.submit()
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE issues
                   SET body='Set VALUE to 3 and prove output.',
                       updated_at='2026-07-29T00:01:00Z'
                   WHERE id='issue-1'"""
            )
        next_issue_version_id = self.lifecycle.current_issue_version("run-1")

        self.assertNotEqual(next_issue_version_id, self.issue_version_id)
        self.assertIsNone(self.service.current("run-1", next_issue_version_id))
        with self.assertRaises(SpecificationUnavailable):
            self.service.require_current("run-1", next_issue_version_id)
        second = self.service.submit(
            run_id="run-1",
            author_member_id="lead-1",
            issue_version_id=next_issue_version_id,
            items=self.items(),
            reason="Revise the contract for the newly observed issue version.",
        )
        self.assertEqual(second["revision"], 2)
        self.assertEqual(self.service.history("run-1")[0]["id"], first["id"])
        self.assertEqual(
            self.service.current("run-1", next_issue_version_id)["id"],
            second["id"],
        )


if __name__ == "__main__":
    unittest.main()
