from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .database import Database

_KEY_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_REVIEW_CATEGORIES = frozenset(
    {
        "coverage",
        "clarity",
        "observability",
        "feasibility",
        "consistency",
        "repository-alignment",
        "scope",
    }
)


class SpecificationUnavailable(Exception):
    """Raised when an active specification or approved review is not available."""


def _canonical_json(obj: Any) -> bytes:
    """Return sorted compact UTF-8 JSON for canonical hashing."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _now() -> str:
    """Return current UTC timestamp in ISO 8601 format matching database convention."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SpecificationService:
    """Controller-owned service for issue-bound atomic specifications."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit(
        self,
        run_id: str,
        author_member_id: str,
        issue_version_id: str,
        items: list[dict[str, object]],
        reason: str,
    ) -> dict[str, object]:
        """Persist a specification revision; idempotent for identical content."""

        if not isinstance(items, list):
            raise ValueError("specification items must be a list")
        if not items:
            raise ValueError("specification items must not be empty")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 2000
        ):
            raise ValueError(
                "reason must be a non-empty string of at most 2000 characters"
            )

        # Validate items structure
        self._validate_items(items)

        with self._db.transaction() as conn:
            # Authorize: only the delivery coordinator may submit
            row = conn.execute(
                """SELECT tm.id, tm.role, tm.stable_key, tm.model,
                          issues.current_version_id
                   FROM team_members tm
                   JOIN runs r ON r.team_version_id = tm.team_version_id
                   JOIN issues ON issues.id = r.issue_id
                   WHERE r.id = ? AND tm.id = ?""",
                (run_id, author_member_id),
            ).fetchone()
            if row is None:
                raise PermissionError(
                    "author member not found on this run"
                )
            if row["role"] != "lead":
                raise PermissionError(
                    "only the delivery coordinator may submit a specification"
                )

            # Validate current issue version
            if row["current_version_id"] != issue_version_id:
                raise ValueError(
                    "issue_version_id does not match the current issue version"
                )

            # Compute canonical content hash
            canonical = _canonical_json(items)
            content_sha256 = hashlib.sha256(canonical).hexdigest()

            # Idempotency applies only to repetition of the active revision.
            current = conn.execute(
                """SELECT id, run_id, issue_version_id, revision, items_json,
                          content_sha256, author_member_id, reason, created_at
                   FROM run_specification_revisions
                   WHERE run_id = ? AND issue_version_id = ?
                   ORDER BY revision DESC LIMIT 1""",
                (run_id, issue_version_id),
            ).fetchone()
            if (
                current is not None
                and current["content_sha256"] == content_sha256
            ):
                return self._row_to_spec(current)

            # New revision: compute next revision number
            last = conn.execute(
                """SELECT MAX(revision) AS max_rev FROM run_specification_revisions
                   WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            next_rev = (last["max_rev"] or 0) + 1

            ts = _now()
            spec_id = f"spec:{run_id}:{next_rev}"
            conn.execute(
                """INSERT INTO run_specification_revisions
                   (id, run_id, issue_version_id, revision, items_json,
                    content_sha256, author_member_id, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    spec_id,
                    run_id,
                    issue_version_id,
                    next_rev,
                    json.dumps(items, sort_keys=True, ensure_ascii=False),
                    content_sha256,
                    author_member_id,
                    reason,
                    ts,
                ),
            )

            stored = conn.execute(
                """SELECT id, run_id, issue_version_id, revision, items_json,
                          content_sha256, author_member_id, reason, created_at
                   FROM run_specification_revisions WHERE id = ?""",
                (spec_id,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("stored specification revision disappeared")
            return self._row_to_spec(stored)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def current(
        self, run_id: str, issue_version_id: str
    ) -> Optional[dict[str, object]]:
        """Return the latest revision for the given issue version, or None."""
        with self._db.connect() as conn:
            row = conn.execute(
                """SELECT id, run_id, issue_version_id, revision, items_json,
                          content_sha256, author_member_id, reason, created_at
                   FROM run_specification_revisions
                   WHERE run_id = ? AND issue_version_id = ?
                   ORDER BY revision DESC LIMIT 1""",
                (run_id, issue_version_id),
            ).fetchone()
            if row is None:
                return None
            review = conn.execute(
                """SELECT id, run_id, specification_revision_id,
                          reviewer_member_id, reviewer_model, rubric_version,
                          verdict, summary, findings_json, blocker,
                          input_sha256, created_at
                   FROM run_specification_reviews
                   WHERE specification_revision_id=?""",
                (row["id"],),
            ).fetchone()
        projected = self._row_to_spec(row)
        projected["review"] = (
            self._row_to_review(review) if review is not None else None
        )
        return projected

    def require_current(
        self, run_id: str, issue_version_id: str
    ) -> dict[str, object]:
        """Return the current specification or raise SpecificationUnavailable."""
        spec = self.current(run_id, issue_version_id)
        if spec is None:
            raise SpecificationUnavailable(
                f"no current specification for run {run_id} issue version {issue_version_id}"
            )
        return spec

    def history(self, run_id: str) -> tuple[dict[str, object], ...]:
        """Return all revisions for a run in chronological order."""
        with self._db.connect() as conn:
            rows = conn.execute(
                """SELECT id, run_id, issue_version_id, revision, items_json,
                          content_sha256, author_member_id, reason, created_at
                   FROM run_specification_revisions
                   WHERE run_id = ?
                   ORDER BY revision ASC""",
                (run_id,),
            ).fetchall()
        return tuple(self._row_to_spec(r) for r in rows)

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------

    def record_review(
        self,
        run_id: str,
        specification_revision_id: str,
        reviewer_member_id: str,
        reviewer_model: str,
        rubric_version: int,
        verdict: str,
        summary: str,
        findings: list[dict[str, object]],
        blocker: Optional[str] = None,
    ) -> dict[str, object]:
        """Record an independent review; idempotent for identical inputs."""

        if verdict not in ("approved", "rejected", "blocked"):
            raise ValueError(f"invalid verdict: {verdict!r}")
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > 2000
        ):
            raise ValueError(
                "summary must be a non-empty string of at most 2000 characters"
            )
        if (
            not isinstance(rubric_version, int)
            or isinstance(rubric_version, bool)
            or rubric_version <= 0
        ):
            raise ValueError("rubric version must be a positive integer")
        if not isinstance(reviewer_model, str) or not reviewer_model.strip():
            raise ValueError("reviewer model must be a non-empty string")
        self._validate_findings(findings)

        with self._db.transaction() as conn:
            # Authorize: only the independent verifier may review
            member = conn.execute(
                """SELECT tm.id, tm.role, tm.model, r.team_version_id
                   FROM team_members tm
                   JOIN runs r ON r.team_version_id = tm.team_version_id
                   WHERE r.id = ? AND tm.id = ?""",
                (run_id, reviewer_member_id),
            ).fetchone()
            if member is None:
                raise PermissionError(
                    "reviewer member not found on this run"
                )
            if member["role"] != "verifier":
                raise PermissionError(
                    "only the independent verifier may review specifications"
                )

            # Verify reviewer model matches
            if member["model"] != reviewer_model:
                raise ValueError(
                    f"reviewer model {reviewer_model!r} does not match stored model "
                    f"{member['model']!r}"
                )

            # Verify spec revision exists and belongs to run
            spec_row = conn.execute(
                """SELECT id, run_id, issue_version_id, revision, items_json,
                          content_sha256, author_member_id, reason, created_at
                   FROM run_specification_revisions
                   WHERE id = ? AND run_id = ?""",
                (specification_revision_id, run_id),
            ).fetchone()
            if spec_row is None:
                raise ValueError(
                    f"specification revision {specification_revision_id!r} not found"
                )
            specification_items = json.loads(str(spec_row["items_json"]))
            known_item_keys = {
                str(item["key"])
                for item in specification_items
                if isinstance(item, dict) and "key" in item
            }
            for finding in findings:
                finding_item_keys = finding["item_keys"]
                assert isinstance(finding_item_keys, list)
                unknown_item_keys = sorted(
                    set(finding_item_keys) - known_item_keys
                )
                if unknown_item_keys:
                    raise ValueError(
                        "review finding references unknown specification item: "
                        + ", ".join(unknown_item_keys)
                    )

            current_issue = conn.execute(
                """SELECT issues.current_version_id
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
            if (
                current_issue is None
                or spec_row["issue_version_id"]
                != current_issue["current_version_id"]
            ):
                raise ValueError(
                    "can only review the active specification for the current "
                    "issue version"
                )
            # Must review the active (latest) specification for that issue version
            latest = conn.execute(
                """SELECT revision FROM run_specification_revisions
                   WHERE run_id = ? AND issue_version_id = ?
                   ORDER BY revision DESC LIMIT 1""",
                (run_id, spec_row["issue_version_id"]),
            ).fetchone()
            if latest is None or latest["revision"] != spec_row["revision"]:
                raise ValueError(
                    "can only review the active specification for the current issue version"
                )

            # Verdict-specific validation
            if verdict == "approved":
                if any(f.get("severity") == "error" for f in findings):
                    raise ValueError(
                        "approved review must not contain error findings"
                    )
            elif verdict == "rejected":
                error_findings = [f for f in findings if f.get("severity") == "error"]
                if not error_findings:
                    raise ValueError(
                        "rejected review requires at least one actionable error finding"
                    )
            elif verdict == "blocked":
                if (
                    not isinstance(blocker, str)
                    or not blocker.strip()
                    or len(blocker) > 2000
                ):
                    raise ValueError(
                        "blocked review requires a non-empty irreducible blocker "
                        "of at most 2000 characters"
                    )
            if verdict != "blocked" and blocker is not None:
                raise ValueError(
                    "only a blocked review may include an irreducible blocker"
                )

            # Idempotency: compute canonical review input hash
            review_input = {
                "specification_revision_id": specification_revision_id,
                "reviewer_member_id": reviewer_member_id,
                "reviewer_model": reviewer_model,
                "rubric_version": rubric_version,
                "verdict": verdict,
                "summary": summary,
                "findings": findings,
                "blocker": blocker,
            }
            canonical = _canonical_json(review_input)
            input_sha256 = hashlib.sha256(canonical).hexdigest()

            # Check for existing identical review
            existing = conn.execute(
                """SELECT id, run_id, specification_revision_id,
                          reviewer_member_id, reviewer_model, rubric_version,
                          verdict, summary, findings_json, blocker,
                          input_sha256, created_at
                   FROM run_specification_reviews
                   WHERE input_sha256 = ?""",
                (input_sha256,),
            ).fetchone()
            if existing is not None:
                return self._row_to_review(existing)

            # Check for any existing review on this revision (immutable)
            any_review = conn.execute(
                """SELECT id FROM run_specification_reviews
                   WHERE specification_revision_id = ?
                   LIMIT 1""",
                (specification_revision_id,),
            ).fetchone()
            if any_review is not None:
                raise ValueError(
                    f"specification revision {specification_revision_id!r} already has "
                    "an immutable review"
                )

            ts = _now()
            review_id = f"review:{run_id}:{specification_revision_id}"
            conn.execute(
                """INSERT INTO run_specification_reviews
                   (id, run_id, specification_revision_id,
                    reviewer_member_id, reviewer_model, rubric_version,
                    verdict, summary, findings_json, blocker,
                    input_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id,
                    run_id,
                    specification_revision_id,
                    reviewer_member_id,
                    reviewer_model,
                    rubric_version,
                    verdict,
                    summary,
                    json.dumps(findings, sort_keys=True, ensure_ascii=False),
                    blocker,
                    input_sha256,
                    ts,
                ),
            )

            stored = conn.execute(
                """SELECT id, run_id, specification_revision_id,
                          reviewer_member_id, reviewer_model, rubric_version,
                          verdict, summary, findings_json, blocker,
                          input_sha256, created_at
                   FROM run_specification_reviews WHERE id = ?""",
                (review_id,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("stored specification review disappeared")
            return self._row_to_review(stored)

    def review_for(
        self, specification_revision_id: str
    ) -> Optional[dict[str, object]]:
        """Return the review for a specification revision, or None."""
        with self._db.connect() as conn:
            row = conn.execute(
                """SELECT id, run_id, specification_revision_id,
                          reviewer_member_id, reviewer_model, rubric_version,
                          verdict, summary, findings_json, blocker,
                          input_sha256, created_at
                   FROM run_specification_reviews
                   WHERE specification_revision_id = ?""",
                (specification_revision_id,),
            ).fetchone()
        return self._row_to_review(row) if row else None

    def review_history(self, run_id: str) -> list[dict[str, object]]:
        """Return all reviews for a run in chronological order."""
        with self._db.connect() as conn:
            rows = conn.execute(
                """SELECT id, run_id, specification_revision_id,
                          reviewer_member_id, reviewer_model, rubric_version,
                          verdict, summary, findings_json, blocker,
                          input_sha256, created_at
                   FROM run_specification_reviews
                   WHERE run_id = ?
                   ORDER BY created_at ASC""",
                (run_id,),
            ).fetchall()
        return [self._row_to_review(r) for r in rows]

    def require_approved(
        self, run_id: str, issue_version_id: str
    ) -> dict[str, object]:
        """Return the current spec with an approved review, or raise."""
        with self._db.connect() as conn:
            spec_row = conn.execute(
                """SELECT id, run_id, issue_version_id, revision, items_json,
                          content_sha256, author_member_id, reason, created_at
                   FROM run_specification_revisions
                   WHERE run_id = ? AND issue_version_id = ?
                   ORDER BY revision DESC LIMIT 1""",
                (run_id, issue_version_id),
            ).fetchone()
            if spec_row is None:
                raise SpecificationUnavailable(
                    f"no current specification for run {run_id} "
                    f"issue version {issue_version_id}"
                )

            review_row = conn.execute(
                """SELECT id, run_id, specification_revision_id,
                          reviewer_member_id, reviewer_model, rubric_version,
                          verdict, summary, findings_json, blocker,
                          input_sha256, created_at
                   FROM run_specification_reviews
                   WHERE run_id = ?
                     AND specification_revision_id = ?
                     AND verdict = 'approved'
                   ORDER BY created_at DESC LIMIT 1""",
                (run_id, spec_row["id"]),
            ).fetchone()
            if review_row is None:
                raise SpecificationUnavailable(
                    f"no approved review for run {run_id} "
                    f"issue version {issue_version_id}"
                )

        spec = self._row_to_spec(spec_row)
        spec["review"] = self._row_to_review(review_row)
        return spec

    def context_binding(
        self,
        run_id: str,
        issue_version_id: str,
        context_sha256: str,
    ) -> Optional[dict[str, object]]:
        """Return the newest specification binding for an information context."""
        self._validate_context_sha256(context_sha256)
        with self._db.connect() as conn:
            row = conn.execute(
                """SELECT contexts.id,
                          contexts.run_id,
                          contexts.issue_version_id,
                          contexts.context_sha256,
                          contexts.specification_revision_id,
                          contexts.reconciled_at,
                          revisions.revision AS specification_revision
                   FROM run_specification_contexts AS contexts
                   JOIN run_specification_revisions AS revisions
                     ON revisions.id=contexts.specification_revision_id
                   WHERE contexts.run_id=?
                     AND contexts.issue_version_id=?
                     AND contexts.context_sha256=?
                   ORDER BY revisions.revision DESC
                   LIMIT 1""",
                (run_id, issue_version_id, context_sha256),
            ).fetchone()
        return self._row_to_context(row) if row else None

    def bind_context(
        self,
        *,
        run_id: str,
        issue_version_id: str,
        context_sha256: str,
        specification_revision_id: str,
    ) -> dict[str, object]:
        """Durably bind new information to the active complete specification."""
        self._validate_context_sha256(context_sha256)
        with self._db.transaction() as conn:
            current = conn.execute(
                """SELECT revisions.id,
                          revisions.revision,
                          issues.current_version_id
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   LEFT JOIN run_specification_revisions AS revisions
                     ON revisions.run_id=runs.id
                    AND revisions.issue_version_id=?
                   WHERE runs.id=?
                   ORDER BY revisions.revision DESC
                   LIMIT 1""",
                (issue_version_id, run_id),
            ).fetchone()
            if current is None:
                raise KeyError(run_id)
            if current["current_version_id"] != issue_version_id:
                raise ValueError(
                    "specification context does not match the current issue version"
                )
            if (
                current["id"] is None
                or str(current["id"]) != specification_revision_id
            ):
                raise ValueError(
                    "specification context must bind to the active specification "
                    "revision"
                )
            binding_input = {
                "run_id": run_id,
                "issue_version_id": issue_version_id,
                "context_sha256": context_sha256,
                "specification_revision_id": specification_revision_id,
            }
            binding_id = "spec-context:" + hashlib.sha256(
                _canonical_json(binding_input)
            ).hexdigest()
            reconciled_at = _now()
            conn.execute(
                """INSERT OR IGNORE INTO run_specification_contexts
                   (id, run_id, issue_version_id, context_sha256,
                    specification_revision_id, reconciled_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    binding_id,
                    run_id,
                    issue_version_id,
                    context_sha256,
                    specification_revision_id,
                    reconciled_at,
                ),
            )
            stored = conn.execute(
                """SELECT contexts.id,
                          contexts.run_id,
                          contexts.issue_version_id,
                          contexts.context_sha256,
                          contexts.specification_revision_id,
                          contexts.reconciled_at,
                          revisions.revision AS specification_revision
                   FROM run_specification_contexts AS contexts
                   JOIN run_specification_revisions AS revisions
                     ON revisions.id=contexts.specification_revision_id
                   WHERE contexts.id=?""",
                (binding_id,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("stored specification context disappeared")
            return self._row_to_context(stored)

    @staticmethod
    def _validate_context_sha256(context_sha256: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", context_sha256) is None:
            raise ValueError("specification context SHA must be lowercase SHA-256")

    # ------------------------------------------------------------------
    # Item validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_items(items: list[dict[str, object]]) -> None:
        """Validate the complete bounded atomic specification structure."""
        item_keys: set[str] = set()
        criterion_keys: set[str] = set()
        verification_keys: set[str] = set()
        item_fields = {
            "key",
            "title",
            "objective",
            "acceptance_criteria",
            "verification",
        }
        criterion_fields = {"key", "requirement", "expected"}
        verification_fields = {"key", "criterion_keys", "scenario"}

        for item in items:
            if not isinstance(item, dict):
                raise ValueError("specification item must be an object")
            unexpected = sorted(set(item) - item_fields)
            if unexpected:
                raise ValueError(
                    f"specification item has unexpected field: {unexpected[0]}"
                )
            missing = sorted(item_fields - set(item))
            if missing:
                raise ValueError(
                    f"specification item is missing field: {missing[0]}"
                )
            key = item["key"]
            if (
                not isinstance(key, str)
                or len(key) > 100
                or _KEY_PATTERN.fullmatch(key) is None
            ):
                raise ValueError(
                    "specification item key must be lowercase kebab-case "
                    "of at most 100 characters"
                )
            if key in item_keys:
                raise ValueError(f"duplicate specification item key {key!r}")
            item_keys.add(key)

            title = item["title"]
            if (
                not isinstance(title, str)
                or not title.strip()
                or len(title) > 500
            ):
                raise ValueError(
                    "item title must be a non-empty string of at most 500 characters"
                )
            objective = item["objective"]
            if (
                not isinstance(objective, str)
                or not objective.strip()
                or len(objective) > 2000
            ):
                raise ValueError(
                    "item objective must be a non-empty string of at most 2000 characters"
                )

            criteria = item["acceptance_criteria"]
            if not isinstance(criteria, list) or not criteria:
                raise ValueError(
                    f"item {key!r} acceptance_criteria must not be empty"
                )
            item_criterion_keys: set[str] = set()
            for criterion in criteria:
                if not isinstance(criterion, dict):
                    raise ValueError("specification criterion must be an object")
                unexpected = sorted(set(criterion) - criterion_fields)
                if unexpected:
                    raise ValueError(
                        f"specification criterion has unexpected field: {unexpected[0]}"
                    )
                missing = sorted(criterion_fields - set(criterion))
                if missing:
                    raise ValueError(
                        f"specification criterion is missing field: {missing[0]}"
                    )
                criterion_key = criterion["key"]
                if (
                    not isinstance(criterion_key, str)
                    or len(criterion_key) > 100
                    or _KEY_PATTERN.fullmatch(criterion_key) is None
                ):
                    raise ValueError(
                        "criterion key must be lowercase kebab-case "
                        "of at most 100 characters"
                    )
                if criterion_key in criterion_keys:
                    raise ValueError(
                        f"duplicate criterion key {criterion_key!r}"
                    )
                criterion_keys.add(criterion_key)
                item_criterion_keys.add(criterion_key)
                requirement = criterion["requirement"]
                if (
                    not isinstance(requirement, str)
                    or not requirement.strip()
                    or len(requirement) > 1000
                ):
                    raise ValueError(
                        "criterion requirement must be a non-empty string "
                        "of at most 1000 characters"
                    )
                expected = criterion["expected"]
                if (
                    not isinstance(expected, str)
                    or not expected.strip()
                    or len(expected) > 1000
                ):
                    raise ValueError(
                        "criterion expected must be a non-empty string "
                        "of at most 1000 characters"
                    )

            verifications = item["verification"]
            if not isinstance(verifications, list) or not verifications:
                raise ValueError(f"item {key!r} has no verification scenarios")
            mapped_criteria: set[str] = set()
            for verification in verifications:
                if not isinstance(verification, dict):
                    raise ValueError("specification verification must be an object")
                unexpected = sorted(set(verification) - verification_fields)
                if unexpected:
                    raise ValueError(
                        "specification verification has unexpected field: "
                        f"{unexpected[0]}"
                    )
                missing = sorted(verification_fields - set(verification))
                if missing:
                    raise ValueError(
                        f"specification verification is missing field: {missing[0]}"
                    )
                verification_key = verification["key"]
                if (
                    not isinstance(verification_key, str)
                    or len(verification_key) > 100
                    or _KEY_PATTERN.fullmatch(verification_key) is None
                ):
                    raise ValueError(
                        "verification key must be lowercase kebab-case "
                        "of at most 100 characters"
                    )
                if verification_key in verification_keys:
                    raise ValueError(
                        f"duplicate verification key {verification_key!r}"
                    )
                verification_keys.add(verification_key)
                scenario = verification["scenario"]
                if (
                    not isinstance(scenario, str)
                    or not scenario.strip()
                    or len(scenario) > 1000
                ):
                    raise ValueError(
                        "verification scenario must be a non-empty string "
                        "of at most 1000 characters"
                    )
                mapped = verification["criterion_keys"]
                if (
                    not isinstance(mapped, list)
                    or not mapped
                    or any(not isinstance(value, str) for value in mapped)
                    or len(set(mapped)) != len(mapped)
                ):
                    raise ValueError(
                        f"verification {verification_key!r} has invalid criterion_keys"
                    )
                for criterion_key in mapped:
                    if criterion_key not in item_criterion_keys:
                        raise ValueError(
                            f"unknown criterion key {criterion_key!r} "
                            f"in verification {verification_key!r}"
                        )
                    mapped_criteria.add(criterion_key)
            unmapped = item_criterion_keys - mapped_criteria
            if unmapped:
                raise ValueError(
                    f"criterion(s) {sorted(unmapped)} not mapped to any verification "
                    f"in item {key!r}"
                )

    @staticmethod
    def _validate_findings(findings: object) -> None:
        if not isinstance(findings, list):
            raise ValueError("review findings must be a list")
        fields = {"key", "category", "severity", "summary", "item_keys"}
        keys: set[str] = set()
        for finding in findings:
            if not isinstance(finding, dict):
                raise ValueError("review finding must be an object")
            unexpected = sorted(set(finding) - fields)
            if unexpected:
                raise ValueError(
                    f"review finding has unexpected field: {unexpected[0]}"
                )
            missing = sorted(fields - set(finding))
            if missing:
                raise ValueError(f"review finding {missing[0]} is required")
            key = finding["key"]
            if (
                not isinstance(key, str)
                or len(key) > 100
                or _KEY_PATTERN.fullmatch(key) is None
            ):
                raise ValueError(
                    "finding key must be lowercase kebab-case "
                    "of at most 100 characters"
                )
            if key in keys:
                raise ValueError(f"duplicate review finding key {key!r}")
            keys.add(key)
            if finding["category"] not in _REVIEW_CATEGORIES:
                raise ValueError("review finding category is invalid")
            if finding["severity"] not in {"warning", "error"}:
                raise ValueError("review finding severity is invalid")
            summary = finding["summary"]
            if (
                not isinstance(summary, str)
                or not summary.strip()
                or len(summary) > 1000
            ):
                raise ValueError(
                    "review finding summary must be a non-empty string "
                    "of at most 1000 characters"
                )
            item_keys = finding["item_keys"]
            if (
                not isinstance(item_keys, list)
                or not item_keys
                or any(not isinstance(value, str) for value in item_keys)
                or len(set(item_keys)) != len(item_keys)
            ):
                raise ValueError(
                    "review finding item_keys must be a non-empty unique string list"
                )

    # ------------------------------------------------------------------
    # Projection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_spec(row: dict[str, object]) -> dict[str, object]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "issue_version_id": row["issue_version_id"],
            "revision": row["revision"],
            "items": json.loads(str(row["items_json"])),
            "content_sha256": row["content_sha256"],
            "author_member_id": row["author_member_id"],
            "reason": row["reason"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _row_to_review(row: dict[str, object]) -> dict[str, object]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "specification_revision_id": row["specification_revision_id"],
            "reviewer_member_id": row["reviewer_member_id"],
            "reviewer_model": row["reviewer_model"],
            "rubric_version": row["rubric_version"],
            "verdict": row["verdict"],
            "summary": row["summary"],
            "findings": json.loads(str(row["findings_json"])),
            "blocker": row["blocker"],
            "input_sha256": row["input_sha256"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _row_to_context(row: dict[str, object]) -> dict[str, object]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "issue_version_id": row["issue_version_id"],
            "context_sha256": row["context_sha256"],
            "specification_revision_id": row["specification_revision_id"],
            "specification_revision": row["specification_revision"],
            "reconciled_at": row["reconciled_at"],
        }
