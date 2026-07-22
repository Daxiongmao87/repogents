from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import shutil
import uuid
from pathlib import Path
from typing import Protocol, Sequence

from .controller import RunProcessSupervisor
from .database import Database
from .execution import (
    AgentToolExecutor,
    MiniSweModelRuntime,
    ModelRuntime,
    RuntimeFactory,
    SecretBinding,
    SecretResolver,
    _default_runtime_factory,
    _sandbox_policy,
    _secret_bindings,
)
from .lifecycle import RunLifecycle, RunState
from .mini_swe import MINI_SWE_RUNTIME
from .sandbox import RunLayout, SandboxManager, SandboxPolicy, redact_text
from .team import TeamMember, TeamService

_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
_MAX_CONTEXT_RESULT = 8_000


class AcceptanceUnavailable(RuntimeError):
    """A required acceptance boundary cannot be exercised safely."""


class AcceptanceTools(Protocol):
    def execute(
        self,
        member: TeamMember,
        policy: SandboxPolicy,
        layout: RunLayout,
        action: dict[str, object],
        secrets: dict[str, str] | None = None,
        checkout_writable: bool = True,
    ) -> str: ...


_CLAIM_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "claim": {"type": "string", "minLength": 1},
        "expected": {"type": "string", "minLength": 1},
        "method": {"type": "string", "minLength": 1},
    },
    "required": ["key", "claim", "expected", "method"],
    "additionalProperties": False,
}

_VERIFIER_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {
            "enum": [
                "list",
                "read",
                "search",
                "run",
                "acceptance_plan",
                "verify",
            ]
        },
        "path": {"type": "string"},
        "start": {"type": "integer"},
        "end": {"type": "integer"},
        "pattern": {"type": "string"},
        "argv": {"type": "array", "items": {"type": "string"}},
        "timeout": {"type": "number"},
        "claims": {"type": "array", "items": _CLAIM_SCHEMA},
        "screenshot_decision": {
            "type": "object",
            "properties": {
                "required": {"type": "boolean"},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["required", "reason"],
            "additionalProperties": False,
        },
        "verdict": {"enum": ["pass", "fail", "blocked"]},
        "commit_sha": {"type": "string"},
        "summary": {"type": "string"},
        "claim_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "result": {"enum": ["pass", "fail"]},
                    "observed": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["key", "result", "observed", "evidence"],
                "additionalProperties": False,
            },
        },
        "scope": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "claim_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "necessity": {"type": "string"},
                    "result": {"enum": ["pass", "fail"]},
                },
                "required": ["path", "claim_keys", "necessity", "result"],
                "additionalProperties": False,
            },
        },
        "screenshots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_key": {"type": "string"},
                    "path": {"type": "string"},
                    "description": {"type": "string"},
                    "metadata": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": ["claim_key", "path", "description", "metadata"],
                "additionalProperties": False,
            },
        },
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action"],
    "additionalProperties": False,
}

_VERIFIER_SYSTEM_PROMPT = """You are the independently stored verifier for one repository run. You are read-only. Derive issue acceptance from the GitHub issue, current discussion, immutable repository evidence, repository behavior, and conventions; never derive success criteria merely from what the implementation happens to do.

Return exactly one controller action. Inspection actions are:
{"action":"list","path":"relative/path"}
{"action":"read","path":"relative/file","start":1,"end":400}
{"action":"search","path":"relative/path","pattern":"regex"}

Before running an issue-behavior scenario, emit one durable plan:
{"action":"acceptance_plan","claims":[{"key":"stable-key","claim":"observable behavior","expected":"specific observation","method":"scenario that observes it"}],"screenshot_decision":{"required":true,"reason":"why visual evidence is material"}}
Use required=false for a genuinely nonvisual claim. A visual/UI claim requires screenshots when capture is possible and materially useful. Do not run behavior commands before the plan is stored.

After the plan is stored, exercise every claim against the exact committed checkout using:
{"action":"run","argv":["program","arg"],"timeout":120}
Read/search may continue. Evidence observations in later context have controller-assigned sequence numbers. Do not edit files, start publication, use GitHub credentials, or accept model prose as evidence.

Finish with:
{"action":"verify","verdict":"pass|fail|blocked","commit_sha":"exact SHA","summary":"specific conclusion","claim_results":[{"key":"planned-key","result":"pass|fail","observed":"actual controller-observed behavior","evidence":[1]}],"scope":[{"path":"every changed file","claim_keys":["planned-key"],"necessity":"why this change is required or protects a regression","result":"pass|fail"}],"screenshots":[{"claim_key":"planned-key","path":"/run-data/temp/acceptance/proof.png","description":"what the screenshot demonstrates","metadata":{"scenario":"...","viewport":"..."}}],"limitations":[]}

A pass requires every planned claim to pass with cited controller evidence, every changed file to have a passing issue/regression mapping, and every required screenshot to exist. Generic repository tests count only when they directly exercise a planned claim. Use fail with exact evidence for a source-fixable problem. Use blocked only when a required claim is irreducibly unverifiable; state the missing capability precisely. Never claim completion without evidence."""


class AcceptanceService:
    def __init__(
        self,
        *,
        database: Database,
        lifecycle: RunLifecycle,
        teams: TeamService,
        sandbox: SandboxManager,
        data_root: Path,
        runtime_factory: RuntimeFactory | None = None,
        tools: AcceptanceTools | None = None,
        max_actions: int = 40,
        secret_resolver: SecretResolver | None = None,
        process_supervisor: RunProcessSupervisor | None = None,
    ) -> None:
        if max_actions <= 0:
            raise ValueError("acceptance verifier action limit must be positive")
        self.database = database
        self.lifecycle = lifecycle
        self.teams = teams
        self.sandbox = sandbox
        self.data_root = Path(data_root).expanduser().resolve()
        self.runtime_factory = runtime_factory or _default_runtime_factory
        self.tools = tools or AgentToolExecutor(sandbox)
        self.max_actions = max_actions
        self.secret_resolver = secret_resolver
        self.process_supervisor = process_supervisor

    def verify(
        self,
        run_id: str,
        commit_sha: str,
        changed_files: Sequence[str],
    ) -> dict[str, object]:
        normalized_files = _changed_files(changed_files)
        context, sandbox_row, verifier = self._load_context(run_id, commit_sha)
        cached = self._passed_report(run_id, commit_sha, normalized_files)
        if cached is not None:
            return cached
        verification, created = self._start_or_resume(
            run_id,
            commit_sha,
            verifier,
        )
        verification_id = str(verification["id"])
        claims = _json_list(verification["claims_json"], "stored acceptance claims")
        screenshot_decision = _json_object(
            verification["screenshot_decision_json"],
            "stored screenshot decision",
        )
        observations = self._observations(verification_id)
        policy = _sandbox_policy(sandbox_row)
        bindings = _secret_bindings(sandbox_row)
        layout = RunLayout.create(
            self.data_root,
            str(context["repository_id"]),
            run_id,
        )
        if created:
            _reset_artifact_stage(layout)
        runtime = self._runtime(verifier, run_id)
        resolved_secret_values: set[str] = set()

        for _ in range(self.max_actions):
            self._require_current(run_id, commit_sha)
            prompt = self._prompt(
                context=context,
                commit_sha=commit_sha,
                changed_files=normalized_files,
                claims=claims,
                screenshot_decision=screenshot_decision,
                observations=observations,
            )
            action = runtime.next_action(
                prompt,
                layout.agent_state / "acceptance" / verification_id,
            )
            if not isinstance(action, dict):
                raise RuntimeError("acceptance verifier returned a non-object action")
            name = action.get("action")
            if name == "acceptance_plan":
                if claims:
                    raise RuntimeError(
                        "acceptance verifier attempted to replace a durable plan"
                    )
                claims, screenshot_decision = _validate_plan(action)
                with self.database.transaction() as connection:
                    self._require_current(
                        run_id,
                        commit_sha,
                        connection=connection,
                    )
                    connection.execute(
                        """UPDATE acceptance_verifications
                           SET claims_json=?, screenshot_decision_json=?
                           WHERE id=? AND state='verifying'""",
                        (
                            _json(claims),
                            _json(screenshot_decision),
                            verification_id,
                        ),
                    )
                continue
            if name == "verify":
                if not claims:
                    raise RuntimeError(
                        "acceptance verifier returned a verdict before its plan"
                    )
                completion = _validate_completion(
                    action,
                    commit_sha=commit_sha,
                    changed_files=normalized_files,
                    claims=claims,
                    screenshot_decision=screenshot_decision,
                    observations=observations,
                )
                artifacts, artifact_rows = self._capture_artifacts(
                    verification_id=verification_id,
                    commit_sha=commit_sha,
                    layout=layout,
                    screenshots=completion.pop("screenshots"),
                )
                state = {
                    "pass": "passed",
                    "fail": "failed",
                    "blocked": "blocked",
                }[str(completion.pop("verdict"))]
                report = {
                    "id": verification_id,
                    "run_id": run_id,
                    "commit_sha": commit_sha,
                    "state": state,
                    "summary": completion["summary"],
                    "claims": completion["claims"],
                    "scope": completion["scope"],
                    "screenshot_decision": screenshot_decision,
                    "evidence": observations,
                    "artifacts": artifacts,
                    "limitations": completion["limitations"],
                }
                self._complete(
                    run_id=run_id,
                    commit_sha=commit_sha,
                    verification_id=verification_id,
                    state=state,
                    report=report,
                    artifact_rows=artifact_rows,
                )
                return report
            if name not in {"list", "read", "search", "run"}:
                raise RuntimeError(f"unsupported acceptance verifier action: {name}")
            if name == "run" and not claims:
                raise RuntimeError(
                    "acceptance verifier must persist its plan before running behavior"
                )
            started_at = _utc_now()
            secrets = self._command_secrets(
                action,
                bindings,
                resolved_secret_values,
            )
            result = self.tools.execute(
                verifier,
                policy,
                layout,
                action,
                secrets=secrets,
                checkout_writable=False,
            )
            result = redact_text(result, resolved_secret_values)
            completed_at = _utc_now()
            observation = self._record_observation(
                verification_id=verification_id,
                sequence=len(observations) + 1,
                action=action,
                result=result,
                started_at=started_at,
                completed_at=completed_at,
            )
            observations.append(observation)
        raise RuntimeError(
            "acceptance verifier exhausted its action limit without a final verdict"
        )

    def current(self, run_id: str) -> dict[str, object] | None:
        return current_acceptance_verification(self.database, run_id)

    def _load_context(
        self,
        run_id: str,
        commit_sha: str,
    ) -> tuple[dict[str, object], dict[str, object], TeamMember]:
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise AcceptanceUnavailable("acceptance commit SHA is invalid")
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT runs.*, repositories.owner, repositories.name,
                          issues.number AS issue_number,
                          issues.url AS issue_url,
                          issues.title AS issue_title,
                          issues.body AS issue_body,
                          issues.discussion_json,
                          sandbox_versions.root_path,
                          sandbox_versions.policy_json,
                          sandbox_versions.evidence_json AS sandbox_evidence_json
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN sandbox_versions
                     ON sandbox_versions.id=runs.sandbox_version_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        context = dict(row)
        self._require_current(run_id, commit_sha)
        team = self.teams.load(str(context["team_version_id"]))
        verifiers = [member for member in team.members if member.role == "verifier"]
        if len(verifiers) != 1:
            raise AcceptanceUnavailable(
                "stored repository team must contain exactly one verifier; re-onboard the repository"
            )
        verifier = verifiers[0]
        permissions = set(verifier.permitted_tools)
        if not {"read", "run"}.issubset(permissions) or "write" in permissions:
            raise AcceptanceUnavailable(
                "stored verifier must have read/run access and no write access"
            )
        sandbox_evidence = _json_object(
            context["sandbox_evidence_json"],
            "stored sandbox evidence",
        )
        if sandbox_evidence != team.evidence:
            raise AcceptanceUnavailable(
                "stored sandbox and team evidence is inconsistent"
            )
        context["repository_evidence"] = sandbox_evidence
        context["discussion"] = _json_list(
            context["discussion_json"],
            "stored issue discussion",
        )
        return context, context, verifier

    def _passed_report(
        self,
        run_id: str,
        commit_sha: str,
        changed_files: tuple[str, ...],
    ) -> dict[str, object] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT report_json FROM acceptance_verifications
                   WHERE run_id=? AND commit_sha=? AND state='passed'
                   ORDER BY attempt DESC LIMIT 1""",
                (run_id, commit_sha),
            ).fetchone()
        if row is None or row["report_json"] is None:
            return None
        report = _json_object(row["report_json"], "stored acceptance report")
        report_paths = tuple(
            sorted(str(item.get("path", "")) for item in _report_scope(report))
        )
        if report_paths != tuple(sorted(changed_files)):
            raise AcceptanceUnavailable(
                "stored acceptance proof does not map the current changed files"
            )
        return report

    def _start_or_resume(
        self,
        run_id: str,
        commit_sha: str,
        verifier: TeamMember,
    ) -> tuple[dict[str, object], bool]:
        now = _utc_now()
        with self.database.transaction() as connection:
            self._require_current(
                run_id,
                commit_sha,
                connection=connection,
            )
            connection.execute(
                """UPDATE acceptance_verifications
                   SET state='superseded', completed_at=COALESCE(completed_at, ?)
                   WHERE run_id=? AND commit_sha<>?
                     AND state IN ('verifying', 'passed')""",
                (now, run_id, commit_sha),
            )
            active = connection.execute(
                """SELECT * FROM acceptance_verifications
                   WHERE run_id=? AND commit_sha=? AND state='verifying'
                   ORDER BY attempt DESC LIMIT 1""",
                (run_id, commit_sha),
            ).fetchone()
            if active is not None:
                return dict(active), False
            attempt = int(
                connection.execute(
                    """SELECT COALESCE(MAX(attempt), 0) + 1
                       FROM acceptance_verifications
                       WHERE run_id=? AND commit_sha=?""",
                    (run_id, commit_sha),
                ).fetchone()[0]
            )
            verification_id = _stable_id(f"{run_id}:acceptance:{commit_sha}:{attempt}")
            connection.execute(
                """INSERT INTO acceptance_verifications
                   (id, run_id, commit_sha, attempt, verifier_member_id, state,
                    claims_json, screenshot_decision_json, started_at)
                   VALUES (?, ?, ?, ?, ?, 'verifying', '[]', '{}', ?)""",
                (
                    verification_id,
                    run_id,
                    commit_sha,
                    attempt,
                    verifier.id,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM acceptance_verifications WHERE id=?",
                (verification_id,),
            ).fetchone()
        return dict(row), True

    def _runtime(self, member: TeamMember, run_id: str) -> ModelRuntime:
        if member.runtime != MINI_SWE_RUNTIME:
            raise AcceptanceUnavailable(
                f"unsupported stored verifier runtime: {member.runtime}"
            )
        runtime = self.runtime_factory(
            member.runtime,
            member.model,
            member.action_timeout_seconds,
        )
        if isinstance(runtime, MiniSweModelRuntime):
            runtime.supervisor = self.process_supervisor
            runtime.run_id = run_id
            runtime.system_prompt = _VERIFIER_SYSTEM_PROMPT
            runtime.response_schema = _VERIFIER_RESPONSE_SCHEMA
        return runtime

    @staticmethod
    def _prompt(
        *,
        context: dict[str, object],
        commit_sha: str,
        changed_files: tuple[str, ...],
        claims: list[object],
        screenshot_decision: dict[str, object],
        observations: list[dict[str, object]],
    ) -> str:
        payload = {
            "task": "Independently prove whether the exact commit resolves the issue.",
            "issue": {
                "number": context["issue_number"],
                "url": context["issue_url"],
                "title": context["issue_title"],
                "body": context["issue_body"],
                "discussion": context["discussion"],
            },
            "repository_evidence": context["repository_evidence"],
            "base_sha": context["base_sha"],
            "candidate_commit_sha": commit_sha,
            "changed_files": changed_files,
            "screenshot_directory": "/run-data/temp/acceptance",
            "durable_plan": {
                "claims": claims,
                "screenshot_decision": screenshot_decision,
            },
            "controller_observations": [
                _context_observation(value) for value in observations
            ],
            "constraints": [
                "The checkout is the exact candidate commit and is read-only to you.",
                "Cite controller observation sequence numbers, not unobserved claims.",
                "Map every changed file to an issue claim or necessary regression protection.",
            ],
        }
        return json.dumps(payload, sort_keys=True, indent=2)

    def _observations(self, verification_id: str) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT sequence, action_json, result_json, started_at,
                          completed_at, log_path
                   FROM acceptance_evidence
                   WHERE verification_id=? ORDER BY sequence""",
                (verification_id,),
            ).fetchall()
        return [_observation(row) for row in rows]

    def _record_observation(
        self,
        *,
        verification_id: str,
        sequence: int,
        action: dict[str, object],
        result: str,
        started_at: str,
        completed_at: str,
    ) -> dict[str, object]:
        parsed_result = _parse_result(result)
        log_path = (
            str(parsed_result.get("log_path"))
            if isinstance(parsed_result, dict) and parsed_result.get("log_path")
            else None
        )
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO acceptance_evidence
                   (id, verification_id, sequence, action_json, result_json,
                    started_at, completed_at, log_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _stable_id(f"{verification_id}:evidence:{sequence}"),
                    verification_id,
                    sequence,
                    _json(action),
                    _json(parsed_result),
                    started_at,
                    completed_at,
                    log_path,
                ),
            )
            row = connection.execute(
                """SELECT sequence, action_json, result_json, started_at,
                          completed_at, log_path
                   FROM acceptance_evidence
                   WHERE verification_id=? AND sequence=?""",
                (verification_id, sequence),
            ).fetchone()
        return _observation(row)

    def _command_secrets(
        self,
        action: dict[str, object],
        bindings: tuple[SecretBinding, ...],
        resolved_secret_values: set[str],
    ) -> dict[str, str]:
        if action.get("action") != "run":
            return {}
        argv = action.get("argv")
        if not isinstance(argv, list) or not all(
            isinstance(argument, str) for argument in argv
        ):
            return {}
        command = tuple(argv)
        values: dict[str, str] = {}
        for name, reference, authorized_commands in bindings:
            if command not in authorized_commands:
                continue
            if self.secret_resolver is None:
                raise AcceptanceUnavailable(
                    f"secret resolver is not configured for authorized binding {name}"
                )
            value = self.secret_resolver(reference)
            if not isinstance(value, str):
                raise TypeError(
                    f"secret resolver returned a non-string value for {name}"
                )
            values[name] = value
            resolved_secret_values.add(value)
        return values

    def _capture_artifacts(
        self,
        *,
        verification_id: str,
        commit_sha: str,
        layout: RunLayout,
        screenshots: object,
    ) -> tuple[list[dict[str, object]], list[tuple[object, ...]]]:
        if not isinstance(screenshots, list):
            raise RuntimeError("acceptance screenshots must be a list")
        artifacts: list[dict[str, object]] = []
        rows: list[tuple[object, ...]] = []
        source_root = (layout.temp / "acceptance").resolve()
        target_root = layout.validation / "acceptance" / verification_id
        target_root.mkdir(parents=True, exist_ok=True)
        for index, screenshot in enumerate(screenshots, start=1):
            if not isinstance(screenshot, dict):
                raise RuntimeError("acceptance screenshot entry must be an object")
            sandbox_path = _required_string(screenshot, "path", "screenshot")
            prefix = "/run-data/temp/acceptance/"
            if not sandbox_path.startswith(prefix):
                raise RuntimeError(
                    "acceptance screenshot must be written below the current "
                    "acceptance attempt directory"
                )
            relative = Path(sandbox_path[len(prefix) :])
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("acceptance screenshot path is unsafe")
            try:
                source = (source_root / relative).resolve(strict=True)
            except OSError as error:
                raise RuntimeError(
                    "acceptance screenshot is unavailable for the current "
                    "acceptance attempt"
                ) from error
            if source_root not in source.parents or not source.is_file():
                raise RuntimeError(
                    "acceptance screenshot escapes controller temp storage"
                )
            size = source.stat().st_size
            if size <= 0 or size > _MAX_ARTIFACT_BYTES:
                raise RuntimeError("acceptance screenshot size is invalid")
            body = source.read_bytes()
            media_type, extension = _image_type(body)
            digest = hashlib.sha256(body).hexdigest()
            target = target_root / f"{index:03d}-{digest[:16]}{extension}"
            temporary = target.with_suffix(target.suffix + ".tmp")
            with source.open("rb") as reader, temporary.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            temporary.replace(target)
            claim_key = _required_string(
                screenshot,
                "claim_key",
                "screenshot",
            )
            description = _required_string(
                screenshot,
                "description",
                "screenshot",
            )
            metadata = screenshot.get("metadata")
            if not isinstance(metadata, dict):
                raise RuntimeError("acceptance screenshot metadata must be an object")
            artifact_id = _stable_id(
                f"{verification_id}:screenshot:{claim_key}:{index}:{digest}"
            )
            artifact = {
                "id": artifact_id,
                "claim_key": claim_key,
                "kind": "screenshot",
                "path": str(target),
                "sha256": digest,
                "media_type": media_type,
                "description": description,
                "commit_sha": commit_sha,
                "metadata": metadata,
            }
            artifacts.append(artifact)
            rows.append(
                (
                    artifact_id,
                    verification_id,
                    claim_key,
                    "screenshot",
                    str(target),
                    digest,
                    media_type,
                    description,
                    _json(metadata),
                    _utc_now(),
                )
            )
        return artifacts, rows

    def _complete(
        self,
        *,
        run_id: str,
        commit_sha: str,
        verification_id: str,
        state: str,
        report: dict[str, object],
        artifact_rows: list[tuple[object, ...]],
    ) -> None:
        completed_at = _utc_now()
        with self.database.transaction() as connection:
            self._require_current(
                run_id,
                commit_sha,
                connection=connection,
            )
            for row in artifact_rows:
                connection.execute(
                    """INSERT OR REPLACE INTO acceptance_artifacts
                       (id, verification_id, claim_key, kind, path, sha256,
                        media_type, description, metadata_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    row,
                )
            updated = connection.execute(
                """UPDATE acceptance_verifications
                   SET state=?, report_json=?, completed_at=?
                   WHERE id=? AND state='verifying'""",
                (state, _json(report), completed_at, verification_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError(
                    "acceptance verification no longer owns its active attempt"
                )

    def _require_current(
        self,
        run_id: str,
        commit_sha: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            run = self.lifecycle.get_run(run_id)
        else:
            run_row = connection.execute(
                "SELECT state, validated_sha FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            run = dict(run_row)
        if run["state"] != RunState.PUBLISHING.value:
            raise AcceptanceUnavailable(
                f"run cannot verify acceptance from state {run['state']}"
            )
        if str(run.get("validated_sha") or "") != commit_sha:
            raise AcceptanceUnavailable(
                "acceptance candidate does not equal the run's validated commit SHA"
            )


def current_acceptance_verification(
    database: Database,
    run_id: str,
) -> dict[str, object] | None:
    with database.connect() as connection:
        row = connection.execute(
            """SELECT acceptance_verifications.*
               FROM acceptance_verifications
               JOIN runs ON runs.id=acceptance_verifications.run_id
               WHERE acceptance_verifications.run_id=?
                 AND acceptance_verifications.commit_sha=runs.validated_sha
               ORDER BY acceptance_verifications.attempt DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        artifacts = connection.execute(
            """SELECT id, claim_key, kind, path, sha256, media_type,
                      description, metadata_json
               FROM acceptance_artifacts
               WHERE verification_id=? ORDER BY created_at, id""",
            (row["id"],),
        ).fetchall()
    if row["report_json"] is not None:
        report = _json_object(row["report_json"], "stored acceptance report")
    else:
        report = {
            "id": row["id"],
            "run_id": run_id,
            "commit_sha": row["commit_sha"],
            "state": row["state"],
            "summary": "Issue acceptance verification is in progress.",
            "claims": _json_list(row["claims_json"], "stored acceptance claims"),
            "scope": [],
            "screenshot_decision": _json_object(
                row["screenshot_decision_json"],
                "stored screenshot decision",
            ),
            "evidence": [],
            "artifacts": [],
            "limitations": [],
        }
    report["artifacts"] = [
        {
            "id": artifact["id"],
            "claim_key": artifact["claim_key"],
            "kind": artifact["kind"],
            "path": artifact["path"],
            "sha256": artifact["sha256"],
            "media_type": artifact["media_type"],
            "description": artifact["description"],
            "commit_sha": row["commit_sha"],
            "metadata": _json_object(
                artifact["metadata_json"],
                "stored acceptance artifact metadata",
            ),
        }
        for artifact in artifacts
    ]
    return report


def load_acceptance_artifact(
    database: Database,
    artifact_id: str,
) -> tuple[bytes, str]:
    with database.connect() as connection:
        row = connection.execute(
            """SELECT path, sha256, media_type
               FROM acceptance_artifacts WHERE id=?""",
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise KeyError(artifact_id)
    path = Path(str(row["path"]))
    if not path.is_file():
        raise RuntimeError("stored acceptance artifact is unavailable")
    if path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise RuntimeError("stored acceptance artifact exceeds the size limit")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != row["sha256"]:
        raise RuntimeError("stored acceptance artifact hash does not match")
    return body, str(row["media_type"])


def render_acceptance_markdown(report: dict[str, object]) -> str:
    commit_sha = _required_string(report, "commit_sha", "acceptance report")
    state = _required_string(report, "state", "acceptance report")
    summary = _required_string(report, "summary", "acceptance report")
    lines = [
        "## Issue acceptance verification",
        "",
        f"- Status: **{_markdown(state)}**",
        f"- Verified commit: `{_markdown(commit_sha)}`",
        f"- Summary: {_markdown(summary)}",
        "",
        "### Claims",
        "",
        "| Claim | Method | Observed | Result | Evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    claims = report.get("claims")
    if not isinstance(claims, list):
        raise RuntimeError("acceptance report claims must be a list")
    for claim in claims:
        if not isinstance(claim, dict):
            raise RuntimeError("acceptance report claim must be an object")
        references = claim.get("evidence", [])
        evidence = (
            ", ".join(f"#{int(value)}" for value in references)
            if isinstance(references, list)
            else ""
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown(str(claim.get("claim", ""))),
                    _markdown(str(claim.get("method", ""))),
                    _markdown(str(claim.get("observed", ""))),
                    _markdown(str(claim.get("result", ""))),
                    _markdown(evidence),
                )
            )
            + " |"
        )
    if not claims:
        lines.append("| No claims recorded |  |  |  |  |")
    lines.extend(
        [
            "",
            "### Controller evidence",
            "",
            "| # | Scenario or command | Result |",
            "| --- | --- | --- |",
        ]
    )
    evidence_values = report.get("evidence", [])
    if isinstance(evidence_values, list):
        for evidence in evidence_values:
            if not isinstance(evidence, dict):
                continue
            action = evidence.get("action", {})
            result = evidence.get("result")
            lines.append(
                f"| {evidence.get('sequence', '')} | "
                f"{_markdown(_action_label(action))} | "
                f"{_markdown(_result_label(result))} |"
            )
    if not evidence_values:
        lines.append("|  | No controller evidence projected |  |")
    lines.extend(
        [
            "",
            "### Changed-file scope",
            "",
            "| Path | Claim keys | Necessity | Result |",
            "| --- | --- | --- | --- |",
        ]
    )
    scope = _report_scope(report)
    for mapping in scope:
        keys = mapping.get("claim_keys", [])
        if not isinstance(keys, list):
            keys = []
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{_markdown(str(mapping.get('path', '')))}`",
                    _markdown(", ".join(str(value) for value in keys)),
                    _markdown(str(mapping.get("necessity", ""))),
                    _markdown(str(mapping.get("result", ""))),
                )
            )
            + " |"
        )
    if not scope:
        lines.append("| No scope mapping recorded |  |  |  |")
    screenshot = report.get("screenshot_decision", {})
    if isinstance(screenshot, dict):
        lines.extend(
            [
                "",
                "### Visual evidence",
                "",
                "- Screenshots required: "
                + ("yes" if screenshot.get("required") else "no"),
                "- Decision: " + _markdown(str(screenshot.get("reason", ""))),
            ]
        )
    artifacts = report.get("artifacts", [])
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            description = _markdown(str(artifact.get("description", "artifact")))
            digest = _markdown(str(artifact.get("sha256", "")))
            url = artifact.get("url")
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                description = f"[{description}]({_markdown(url)})"
            lines.append(f"- {description}; SHA-256 `{digest}`")
    limitations = report.get("limitations", [])
    lines.extend(["", "### Limitations", ""])
    if isinstance(limitations, list) and limitations:
        lines.extend(f"- {_markdown(str(value))}" for value in limitations)
    else:
        lines.append("- None recorded.")
    return "\n".join(lines)


def render_acceptance_failure(report: dict[str, object]) -> str:
    lines = [_required_string(report, "summary", "acceptance report")]
    evidence_values = report.get("evidence", [])
    evidence_by_sequence = (
        {
            value.get("sequence"): value
            for value in evidence_values
            if isinstance(value, dict) and isinstance(value.get("sequence"), int)
        }
        if isinstance(evidence_values, list)
        else {}
    )
    cited: set[int] = set()
    claims = report.get("claims", [])
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict) or claim.get("result") != "fail":
                continue
            key = str(claim.get("key", "claim"))
            lines.append(
                f"Failed claim {key}: {claim.get('observed', 'no observation')}"
            )
            references = claim.get("evidence", [])
            if isinstance(references, list):
                cited.update(
                    value
                    for value in references
                    if isinstance(value, int) and not isinstance(value, bool)
                )
    for sequence in sorted(cited):
        observation = evidence_by_sequence.get(sequence)
        if observation is None:
            lines.append(f"Evidence #{sequence}: durable reference recorded")
            continue
        lines.append(
            f"Evidence #{sequence}: "
            f"{_action_label(observation.get('action'))} => "
            + json.dumps(
                observation.get("result"),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    for mapping in _report_scope(report):
        if mapping.get("result") == "fail":
            lines.append(
                "Failed scope " f"{mapping.get('path')}: {mapping.get('necessity', '')}"
            )
    limitations = report.get("limitations", [])
    if isinstance(limitations, list):
        lines.extend(f"Limitation: {value}" for value in limitations)
    return "\n".join(lines)


def _validate_plan(
    action: dict[str, object],
) -> tuple[list[object], dict[str, object]]:
    claims = action.get("claims")
    if not isinstance(claims, list) or not claims:
        raise RuntimeError("acceptance plan must contain at least one claim")
    normalized: list[object] = []
    keys: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise RuntimeError("acceptance plan claim must be an object")
        key = _required_string(claim, "key", "acceptance claim")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", key):
            raise RuntimeError("acceptance claim key must be lowercase kebab-case")
        if key in keys:
            raise RuntimeError(f"duplicate acceptance claim key: {key}")
        keys.add(key)
        normalized.append(
            {
                "key": key,
                "claim": _required_string(claim, "claim", "acceptance claim"),
                "expected": _required_string(
                    claim,
                    "expected",
                    "acceptance claim",
                ),
                "method": _required_string(claim, "method", "acceptance claim"),
            }
        )
    screenshot = action.get("screenshot_decision")
    if not isinstance(screenshot, dict):
        raise RuntimeError("acceptance screenshot decision must be an object")
    required = screenshot.get("required")
    if not isinstance(required, bool):
        raise RuntimeError("acceptance screenshot required decision must be boolean")
    decision = {
        "required": required,
        "reason": _required_string(
            screenshot,
            "reason",
            "acceptance screenshot decision",
        ),
    }
    return normalized, decision


def _validate_completion(
    action: dict[str, object],
    *,
    commit_sha: str,
    changed_files: tuple[str, ...],
    claims: list[object],
    screenshot_decision: dict[str, object],
    observations: list[dict[str, object]],
) -> dict[str, object]:
    verdict = action.get("verdict")
    if verdict not in {"pass", "fail", "blocked"}:
        raise RuntimeError("acceptance verdict is invalid")
    if action.get("commit_sha") != commit_sha:
        raise RuntimeError("acceptance verdict commit SHA is stale or invalid")
    summary = _required_string(action, "summary", "acceptance verdict")
    claim_keys = {
        str(claim["key"])
        for claim in claims
        if isinstance(claim, dict) and "key" in claim
    }
    raw_results = action.get("claim_results")
    if not isinstance(raw_results, list):
        raise RuntimeError("acceptance claim results must be a list")
    by_key: dict[str, dict[str, object]] = {}
    evidence_by_sequence: dict[int, dict[str, object]] = {}
    for value in observations:
        sequence = value.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise RuntimeError("stored acceptance evidence sequence is invalid")
        evidence_by_sequence[sequence] = value
    for result in raw_results:
        if not isinstance(result, dict):
            raise RuntimeError("acceptance claim result must be an object")
        key = _required_string(result, "key", "acceptance claim result")
        if key in by_key:
            raise RuntimeError(f"duplicate acceptance claim result: {key}")
        references = result.get("evidence")
        if not isinstance(references, list) or not references:
            raise RuntimeError(f"acceptance claim {key} has no controller evidence")
        if any(
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence not in evidence_by_sequence
            for sequence in references
        ):
            raise RuntimeError(f"acceptance claim {key} references unknown evidence")
        result_value = result.get("result")
        if result_value not in {"pass", "fail"}:
            raise RuntimeError(f"acceptance claim {key} result is invalid")
        if result_value == "pass" and any(
            not bool(evidence_by_sequence[sequence]["successful"])
            for sequence in references
        ):
            raise RuntimeError(
                f"acceptance claim {key} cites failed evidence as a pass"
            )
        if result_value == "pass" and not any(
            _behavior_observation(evidence_by_sequence[sequence])
            for sequence in references
        ):
            raise RuntimeError(
                f"acceptance claim {key} cites no successful behavior command"
            )
        by_key[key] = {
            "key": key,
            "result": result_value,
            "observed": _required_string(
                result,
                "observed",
                "acceptance claim result",
            ),
            "evidence": references,
        }
    if set(by_key) != claim_keys:
        missing = sorted(claim_keys - set(by_key))
        extra = sorted(set(by_key) - claim_keys)
        raise RuntimeError(
            "acceptance verdict does not cover planned claims"
            f"; missing={missing}; extra={extra}"
        )
    if verdict == "pass" and any(
        result["result"] != "pass" for result in by_key.values()
    ):
        raise RuntimeError("passing acceptance verdict contains a failed claim")
    if verdict == "fail" and all(
        result["result"] == "pass" for result in by_key.values()
    ):
        raise RuntimeError("failed acceptance verdict identifies no failed claim")
    scope = action.get("scope")
    if not isinstance(scope, list):
        raise RuntimeError("acceptance scope mapping must be a list")
    normalized_scope: list[dict[str, object]] = []
    paths: set[str] = set()
    for mapping in scope:
        if not isinstance(mapping, dict):
            raise RuntimeError("acceptance scope mapping must be an object")
        path = _required_string(mapping, "path", "acceptance scope mapping")
        if path in paths:
            raise RuntimeError(f"duplicate acceptance changed file mapping: {path}")
        paths.add(path)
        mapped_keys = mapping.get("claim_keys")
        if (
            not isinstance(mapped_keys, list)
            or not mapped_keys
            or any(
                not isinstance(key, str) or key not in claim_keys for key in mapped_keys
            )
        ):
            raise RuntimeError(f"acceptance changed file {path} has invalid claim keys")
        result = mapping.get("result")
        if result not in {"pass", "fail"}:
            raise RuntimeError(f"acceptance changed file {path} result is invalid")
        normalized_scope.append(
            {
                "path": path,
                "claim_keys": mapped_keys,
                "necessity": _required_string(
                    mapping,
                    "necessity",
                    "acceptance scope mapping",
                ),
                "result": result,
            }
        )
    expected_paths = set(changed_files)
    if paths != expected_paths:
        raise RuntimeError(
            "acceptance changed file mapping is incomplete"
            f"; missing={sorted(expected_paths - paths)}"
            f"; extra={sorted(paths - expected_paths)}"
        )
    if verdict == "pass" and any(
        mapping["result"] != "pass" for mapping in normalized_scope
    ):
        raise RuntimeError("passing acceptance verdict contains failed scope")
    screenshots = action.get("screenshots")
    if not isinstance(screenshots, list):
        raise RuntimeError("acceptance screenshots must be a list")
    for screenshot in screenshots:
        if not isinstance(screenshot, dict):
            raise RuntimeError("acceptance screenshot must be an object")
        if screenshot.get("claim_key") not in claim_keys:
            raise RuntimeError("acceptance screenshot references an unknown claim")
    if (
        verdict == "pass"
        and screenshot_decision.get("required") is True
        and not screenshots
    ):
        raise RuntimeError("required screenshot evidence is missing")
    limitations = action.get("limitations")
    if not isinstance(limitations, list) or any(
        not isinstance(value, str) or not value.strip() for value in limitations
    ):
        raise RuntimeError("acceptance limitations must be nonempty strings")
    if verdict == "blocked" and not limitations:
        raise RuntimeError(
            "blocked acceptance verdict must identify a concrete limitation"
        )
    if verdict == "blocked" and all(
        result["result"] == "pass" for result in by_key.values()
    ):
        raise RuntimeError("blocked acceptance verdict identifies no unverified claim")
    merged_claims = []
    for claim in claims:
        assert isinstance(claim, dict)
        merged_claims.append({**claim, **by_key[str(claim["key"])]})
    return {
        "verdict": verdict,
        "summary": summary,
        "claims": merged_claims,
        "scope": normalized_scope,
        "screenshots": screenshots,
        "limitations": limitations,
    }


def _reset_artifact_stage(layout: RunLayout) -> None:
    stage = layout.temp / "acceptance"
    if stage.is_symlink() or (stage.exists() and not stage.is_dir()):
        stage.unlink()
    elif stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)


def _changed_files(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized or any(
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or ".." in Path(path).parts
        for path in normalized
    ):
        raise AcceptanceUnavailable("acceptance changed files are invalid")
    if len(normalized) != len(set(normalized)):
        raise AcceptanceUnavailable("acceptance changed files contain duplicates")
    return normalized


def _observation(row: sqlite3.Row) -> dict[str, object]:
    action = _json_object(row["action_json"], "stored acceptance action")
    result = json.loads(str(row["result_json"]))
    return {
        "sequence": int(row["sequence"]),
        "action": action,
        "result": result,
        "successful": _successful_observation(action, result),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "log_path": row["log_path"],
    }


def _behavior_observation(observation: dict[str, object]) -> bool:
    action = observation.get("action")
    return (
        bool(observation.get("successful"))
        and isinstance(action, dict)
        and action.get("action") == "run"
    )


def _successful_observation(
    action: dict[str, object],
    result: object,
) -> bool:
    if isinstance(result, dict):
        if result.get("error"):
            return False
        returncode = result.get("returncode")
        if action.get("action") == "run":
            return returncode == 0 and result.get("timed_out") is not True
        if returncode is not None:
            return returncode == 0
    return True


def _context_observation(value: dict[str, object]) -> dict[str, object]:
    result = value.get("result")
    if isinstance(result, str):
        bounded_result: object = result[:_MAX_CONTEXT_RESULT]
    elif isinstance(result, dict):
        bounded_result = {
            key: (item[:_MAX_CONTEXT_RESULT] if isinstance(item, str) else item)
            for key, item in result.items()
        }
    else:
        bounded_result = result
    return {
        "sequence": value.get("sequence"),
        "action": value.get("action"),
        "result": bounded_result,
        "successful": value.get("successful"),
        "log_path": value.get("log_path"),
    }


def _parse_result(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _image_type(body: bytes) -> tuple[str, str]:
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise RuntimeError("acceptance screenshot is not a supported image")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_list(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{label} is unreadable") from error
    if not isinstance(parsed, list):
        raise RuntimeError(f"{label} must be a list")
    return parsed


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{label} is unreadable") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{label} must be an object")
    return parsed


def _required_string(
    value: dict[str, object],
    key: str,
    label: str,
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise RuntimeError(f"{label} {key} must be a nonempty string")
    return item.strip()


def _report_scope(report: dict[str, object]) -> list[dict[str, object]]:
    scope = report.get("scope")
    if not isinstance(scope, list) or any(
        not isinstance(value, dict) for value in scope
    ):
        raise RuntimeError("acceptance report scope must be a list of objects")
    return scope


def _action_label(value: object) -> str:
    if not isinstance(value, dict):
        return str(value)
    if value.get("action") == "run":
        argv = value.get("argv", [])
        if isinstance(argv, list):
            return "$ " + " ".join(str(item) for item in argv)
    name = str(value.get("action", "action"))
    path = value.get("path")
    return f"{name} {path}" if path else name


def _result_label(value: object) -> str:
    if isinstance(value, dict):
        if "returncode" in value:
            suffix = " (timed out)" if value.get("timed_out") else ""
            return f"exit={value.get('returncode')}{suffix}"
        if value.get("error"):
            return f"error: {value['error']}"
        return json.dumps(value, sort_keys=True)[:300]
    return str(value)[:300]


def _markdown(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")[:1_000]


def _stable_id(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
