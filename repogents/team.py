from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from .database import Database
from .mini_swe import MINI_SWE_RUNTIME

DEFAULT_ACTION_TIMEOUT_SECONDS = 600.0


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
class Assignment:
    id: str
    run_id: str
    member: TeamMember
    reasoning: str
    assigned_at: str


def validate_team_members(members: Sequence[dict[str, object]]) -> None:
    leads = [member for member in members if member.get("role") == "lead"]
    if len(leads) != 1:
        raise ValueError("repository team must contain exactly one lead")
    keys = [str(member.get("stable_key", "")) for member in members]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise ValueError(
            "repository team member identities must be nonempty and unique"
        )
    allowed_roles = {"lead", "scout", "implementer", "verifier"}
    for member in members:
        if member.get("role") not in allowed_roles:
            raise ValueError(f"unsupported repository team role: {member.get('role')}")
        tools = member.get("permitted_tools")
        if not isinstance(tools, list) or not all(
            isinstance(tool, str) and tool for tool in tools
        ):
            raise ValueError(
                "team member permitted tools must be a nonempty string list"
            )
        for field in ("responsibilities", "runtime", "model", "instructions"):
            if not isinstance(member.get(field), str):
                raise ValueError(f"team member {field} must be a string")
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


class EvidenceTeamFormulator:
    """Forms a minimal repository-specific team from source evidence.

    The configured lead remains free to use only itself for a small issue. Extra
    stored members are added only where repository scale or validation surfaces
    make their responsibilities useful across issues.
    """

    def __init__(
        self,
        *,
        runtime: str = MINI_SWE_RUNTIME,
        model: str = "configured",
        action_timeout_seconds: float = DEFAULT_ACTION_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(action_timeout_seconds, bool) or action_timeout_seconds <= 0:
            raise ValueError("team member action timeout must be positive")
        self.runtime = runtime
        self.model = model
        self.action_timeout_seconds = action_timeout_seconds

    def formulate(self, inspection: InspectionEvidence) -> list[dict[str, object]]:
        repository_instructions = getattr(inspection, "instructions", ())
        instruction_text = inspection.summary
        if repository_instructions:
            instruction_text += "\n\nRepository instructions:\n" + "\n\n".join(
                f"--- {path} ---\n{content}"
                for path, content in repository_instructions
            )
        members: list[dict[str, object]] = [
            {
                "stable_key": "lead",
                "role": "lead",
                "responsibilities": "Infer issue intent; own implementation, integration, validation, and final scope",
                "permitted_tools": ["read", "write", "run", "git_diff", "git_commit"],
                "runtime": self.runtime,
                "model": self.model,
                "instructions": instruction_text,
                "action_timeout_seconds": self.action_timeout_seconds,
            }
        ]
        complex_repository = (
            inspection.file_count >= 500 or len(inspection.languages) > 1
        )
        if complex_repository:
            members.append(
                {
                    "stable_key": "implementation",
                    "role": "implementer",
                    "responsibilities": "Implement bounded changes assigned by the lead in the isolated checkout",
                    "permitted_tools": ["read", "write", "run", "git_diff"],
                    "runtime": self.runtime,
                    "model": self.model,
                    "instructions": instruction_text,
                    "action_timeout_seconds": self.action_timeout_seconds,
                }
            )
        members.append(
            {
                "stable_key": "verification",
                "role": "verifier",
                "responsibilities": (
                    "Independently verify issue behavior, scope, and "
                    "repository-required validation"
                ),
                "permitted_tools": ["read", "run", "git_diff"],
                "runtime": self.runtime,
                "model": self.model,
                "instructions": instruction_text,
                "action_timeout_seconds": self.action_timeout_seconds,
            }
        )
        validate_team_members(members)
        return members


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
        validate_team_members([self._member_dict(member) for member in members])
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
        unknown = set(stable_keys) - set(by_key)
        if unknown:
            raise ValueError(
                f"assignment contains members outside the stored team: {','.join(sorted(unknown))}"
            )
        lead_key = next(
            member.stable_key for member in team.members if member.role == "lead"
        )
        if lead_key not in stable_keys:
            raise ValueError("every issue assignment must include the stored lead")
        now = _utc_now()
        with self.database.transaction() as connection:
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
        return TeamMember(
            id=str(row["id"]),  # type: ignore[index]
            stable_key=str(row["stable_key"]),  # type: ignore[index]
            role=str(row["role"]),  # type: ignore[index]
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
