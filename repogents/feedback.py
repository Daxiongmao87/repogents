from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from .controller import RunProcessSupervisor
from .database import Database
from .github import FeedbackItem, FeedbackOutput, PullRequestInfo
from .mini_swe import MINI_SWE_RUNTIME, MiniSweInference
from .lifecycle import RunLifecycle, RunState


@dataclass(frozen=True)
class FeedbackDecision:
    action: str
    reason: str
    response: str

    def __post_init__(self) -> None:
        if self.action not in {"revise", "answer", "decline", "ignore"}:
            raise ValueError(f"unsupported feedback decision: {self.action}")
        if not self.reason.strip():
            raise ValueError("feedback decision requires reasoning")
        if self.action != "ignore" and not self.response.strip():
            raise ValueError("feedback decision requires a response")


class FeedbackGateway(Protocol):
    def get_pull_request(
        self, owner: str, name: str, number: int
    ) -> PullRequestInfo: ...

    def list_feedback(
        self, owner: str, name: str, pull_number: int
    ) -> list[FeedbackItem]: ...

    def find_response(
        self,
        owner: str,
        name: str,
        pull_number: int,
        feedback: FeedbackItem,
        body: str,
        attempted_at: str,
    ) -> FeedbackOutput | None: ...

    def post_response(
        self,
        owner: str,
        name: str,
        pull_number: int,
        feedback: FeedbackItem,
        body: str,
    ) -> FeedbackOutput: ...


class FeedbackEvaluator(Protocol):
    def evaluate(self, context: dict[str, object]) -> FeedbackDecision: ...


class SourceExecutor(Protocol):
    def execute(self, run_id: str, *, additional_context: str | None = None) -> str | None: ...


class RevisionPublisher(Protocol):
    def publish(self, run_id: str) -> object | None: ...


class QuietStarter(Protocol):
    def start(self, run_id: str) -> None: ...


class MiniSweFeedbackEvaluator:
    """Evaluate arbitrary feedback through a durable mini-SWE boundary."""

    _RESPONSE_SCHEMA: dict[str, object] = {
        "type": "object",
        "properties": {
            "action": {
                "enum": ["revise", "answer", "decline", "ignore"],
            },
            "reason": {"type": "string", "minLength": 1},
            "response": {"type": "string"},
        },
        "required": ["action", "reason", "response"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        state_root: Path | None = None,
        processes: RunProcessSupervisor | None = None,
        timeout: float = 600,
    ) -> None:
        if timeout <= 0:
            raise ValueError("feedback evaluator timeout must be positive")
        self.model = model
        self.base_url = base_url
        self.state_root = (
            Path(state_root).expanduser().resolve()
            if state_root is not None
            else (Path.cwd() / ".repogents-model-state" / "feedback").resolve()
        )
        self.processes = processes
        self.timeout = timeout

    @staticmethod
    def _build_inference(
        inference: MiniSweInference,
    ) -> MiniSweInference:
        return inference

    def evaluate(self, context: dict[str, object]) -> FeedbackDecision:
        model = self.model
        stored_lead = context.get("stored_lead")
        if stored_lead is not None:
            if not isinstance(stored_lead, dict):
                raise RuntimeError(
                    "stored feedback lead configuration is invalid"
                )
            if stored_lead.get("runtime") != MINI_SWE_RUNTIME:
                raise RuntimeError(
                    "unsupported stored feedback runtime: "
                    f"{stored_lead.get('runtime')}"
                )
            stored_model = stored_lead.get("model")
            if not isinstance(stored_model, str) or not stored_model:
                raise RuntimeError("stored feedback model is invalid")
            if model is None:
                model = stored_model
        if not model:
            raise RuntimeError(
                "feedback evaluator requires an explicit stored model"
            )
        prompt = json.dumps(
            {
                "task": (
                    "Evaluate this pull-request feedback against the original "
                    "issue, discussion, repository instructions/evidence, current "
                    "implementation state, prior feedback, and scope."
                ),
                "context": context,
                "actions": {
                    "revise": "valid in-scope source change required",
                    "answer": (
                        "relevant question or explanation without source change"
                    ),
                    "decline": (
                        "incorrect, contradictory, or out-of-scope request"
                    ),
                    "ignore": "no response or source action is warranted",
                },
                "response_schema": {
                    "action": "revise|answer|decline|ignore",
                    "reason": "specific reasoning",
                    "response": "comment text, empty only for ignore",
                },
            },
            sort_keys=True,
        )
        run_id = str(context["run_id"]) if "run_id" in context else None
        state_key = (
            re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)
            if run_id
            else uuid.uuid5(uuid.NAMESPACE_URL, prompt).hex
        )
        inference = self._build_inference(
            MiniSweInference(
                model=model,
                base_url=self.base_url,
                timeout=self.timeout,
                supervisor=self.processes,
                run_id=run_id,
            )
        )
        value = inference.infer(
            system_prompt=(
                "Return exactly one JSON object matching the requested schema "
                "and no prose."
            ),
            prompt=prompt,
            response_schema=self._RESPONSE_SCHEMA,
            state_directory=self.state_root / state_key,
        )
        if not isinstance(value, dict):
            raise RuntimeError("feedback evaluator returned a non-object")
        return FeedbackDecision(
            action=str(value.get("action", "")),
            reason=str(value.get("reason", "")),
            response=str(value.get("response", "")),
        )


class FeedbackService:
    def __init__(
        self,
        *,
        database: Database,
        lifecycle: RunLifecycle,
        gateway: FeedbackGateway,
        evaluator: FeedbackEvaluator,
        executor: SourceExecutor,
        publisher: RevisionPublisher,
        quiet: QuietStarter,
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle
        self.gateway = gateway
        self.evaluator = evaluator
        self.executor = executor
        self.publisher = publisher
        self.quiet = quiet

    def poll_run(self, run_id: str) -> int:
        context = self._pull_context(run_id)
        pull = self.gateway.get_pull_request(
            str(context["owner"]),
            str(context["name"]),
            _required_int(context["pull_number"], "pull number"),
        )
        pull_state = "merged" if pull.merged else pull.state
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE pull_requests
                   SET state=?, remote_head_sha=?, updated_at=?
                   WHERE id=?""",
                (pull_state, pull.head_sha, pull.updated_at, context["pull_id"]),
            )
            if pull_state != "open":
                connection.execute(
                    """UPDATE quiet_periods
                       SET state='canceled', canceled_at=?
                       WHERE run_id=? AND state='active'""",
                    (_utc_now(), run_id),
                )
        if pull_state != "open":
            state = RunState(str(self.lifecycle.get_run(run_id)["state"]))
            if state in {
                RunState.PUBLISHING,
                RunState.WAITING_FOR_FEEDBACK,
                RunState.RESOLVING_FEEDBACK,
                RunState.QUIET_PERIOD,
                RunState.NOTIFIED,
            }:
                self.lifecycle.transition(
                    run_id,
                    RunState.CLOSED,
                    reason="pull request was merged or closed by an external actor",
                )
            return 0
        items = self.gateway.list_feedback(
            str(context["owner"]),
            str(context["name"]),
            _required_int(context["pull_number"], "pull number"),
        )
        inserted = 0
        with self.database.transaction() as connection:
            output_rows = connection.execute(
                """SELECT feedback_type, github_object_id FROM application_outputs
                   WHERE pull_request_id=?""",
                (context["pull_id"],),
            ).fetchall()
            outputs = {
                (str(row["feedback_type"]), str(row["github_object_id"]))
                for row in output_rows
            }
            for item in items:
                if (item.feedback_type, item.object_id) in outputs:
                    continue
                feedback_id = _stable_id(
                    f"{context['pull_id']}:{item.feedback_type}:{item.object_id}:{item.version}"
                )
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO feedback_versions
                       (id, pull_request_id, feedback_type, github_object_id,
                        github_version, author, body, path, line, url, state, observed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        feedback_id,
                        context["pull_id"],
                        item.feedback_type,
                        item.object_id,
                        item.version,
                        item.author,
                        item.body,
                        item.path,
                        item.line,
                        item.url,
                        _utc_now(),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def resolve_run(self, run_id: str) -> int:
        processed = 0
        self._reconcile_pending_responses(run_id)
        for _ in range(20):
            self.poll_run(run_id)
            if self._pull_context(run_id)["pull_state"] != "open":
                return processed
            if self.lifecycle.get_run(run_id)["state"] in {
                RunState.CANCELED.value,
                RunState.CLOSED.value,
            }:
                return processed
            pending = self._pending(run_id)
            if not pending:
                self._finish_feedback_cycle(run_id)
                return processed
            for row in pending:
                run = self.lifecycle.get_run(run_id)
                if str(run["state"]) == RunState.PUBLISHING.value:
                    source_sha = row.get("source_sha")
                    decision_json = row.get("decision_json")
                    if not source_sha and decision_json:
                        decision = FeedbackDecision(**json.loads(str(decision_json)))
                        if decision.action == "revise" and run.get("validated_sha"):
                            source_sha = str(run["validated_sha"])
                            with self.database.transaction() as connection:
                                connection.execute(
                                    "UPDATE feedback_versions SET source_sha=? WHERE id=?",
                                    (source_sha, row["id"]),
                                )
                            row["source_sha"] = source_sha
                    if source_sha and self.publisher.publish(run_id) is None:
                        return processed
                if not self._ensure_resolving(run_id):
                    return processed
                try:
                    decision = self._decision(run_id, row)
                    if self.lifecycle.get_run(run_id)["state"] != RunState.RESOLVING_FEEDBACK.value:
                        return processed
                    source_sha = row.get("source_sha")
                    if decision.action == "revise" and not source_sha:
                        feedback_context = json.dumps(
                            {
                                "feedback": self._feedback_payload(row),
                                "decision": asdict(decision),
                                "instruction": "Implement this valid in-scope feedback on the existing pull-request checkout, then rerun affected repository validation.",
                            },
                            sort_keys=True,
                        )
                        source_sha = self.executor.execute(
                            run_id, additional_context=feedback_context
                        )
                        if source_sha is None:
                            return processed
                        with self.database.transaction() as connection:
                            connection.execute(
                                "UPDATE feedback_versions SET source_sha=? WHERE id=?",
                                (source_sha, row["id"]),
                            )
                        if self.publisher.publish(run_id) is None:
                            return processed
                    if decision.response:
                        if not self._ensure_resolving(run_id):
                            return processed
                        self._respond(run_id, row, decision.response)
                    if self.lifecycle.get_run(run_id)["state"] != RunState.RESOLVING_FEEDBACK.value:
                        return processed
                    final_state = {
                        "revise": "resolved",
                        "answer": "answered",
                        "decline": "declined",
                        "ignore": "resolved",
                    }[decision.action]
                    with self.database.transaction() as connection:
                        connection.execute(
                            """UPDATE feedback_versions
                               SET state=?, processed_at=? WHERE id=?""",
                            (final_state, _utc_now(), row["id"]),
                        )
                    processed += 1
                except Exception:
                    return processed
            # The next loop is an immediate GitHub repoll before quiet time.
        return processed

    def _decision(
        self, run_id: str, row: dict[str, object]
    ) -> FeedbackDecision:
        existing = row.get("decision_json")
        if existing:
            value = json.loads(str(existing))
            return FeedbackDecision(**value)
        context = self._evaluation_context(run_id, row)
        decision = self.evaluator.evaluate(context)
        self.poll_run(run_id)
        if self.lifecycle.get_run(run_id)["state"] != RunState.RESOLVING_FEEDBACK.value:
            raise RuntimeError("run stopped during feedback evaluation")
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE feedback_versions
                   SET state='processing', decision_json=? WHERE id=?""",
                (_json(asdict(decision)), row["id"]),
            )
        row["decision_json"] = _json(asdict(decision))
        return decision

    def _reconcile_pending_responses(self, run_id: str) -> None:
        context = self._pull_context(run_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT outbound_operations.id AS operation_id,
                          outbound_operations.request_json,
                          outbound_operations.attempted_at,
                          feedback_versions.*
                   FROM outbound_operations
                   JOIN feedback_versions
                     ON outbound_operations.idempotency_key =
                        feedback_versions.id || ':response'
                   WHERE outbound_operations.run_id=?
                     AND outbound_operations.kind='post_feedback_response'
                     AND outbound_operations.state='pending'
                   ORDER BY outbound_operations.created_at,
                            outbound_operations.id""",
                (run_id,),
            ).fetchall()
        for stored in rows:
            row = dict(stored)
            request = json.loads(str(row["request_json"]))
            body = request.get("body") if isinstance(request, dict) else None
            if not isinstance(body, str) or not body:
                raise RuntimeError("pending feedback response body is invalid")
            feedback = self._feedback_item(row)
            output = self.gateway.find_response(
                str(context["owner"]),
                str(context["name"]),
                _required_int(context["pull_number"], "pull number"),
                feedback,
                body,
                str(row["attempted_at"]),
            )
            if output is not None:
                self._record_response(
                    context,
                    row,
                    str(row["operation_id"]),
                    output,
                )

    @staticmethod
    def _feedback_item(row: dict[str, object]) -> FeedbackItem:
        return FeedbackItem(
            feedback_type=str(row["feedback_type"]),
            object_id=str(row["github_object_id"]),
            version=str(row["github_version"]),
            author=str(row["author"]),
            body=str(row["body"]),
            path=str(row["path"]) if row.get("path") is not None else None,
            line=_required_int(row["line"], "feedback line") if row.get("line") is not None else None,
            url=str(row.get("url") or ""),
            created_at=str(row["observed_at"]),
            updated_at=str(row["github_version"]),
        )

    def _respond(
        self,
        run_id: str,
        row: dict[str, object],
        body: str,
    ) -> None:
        context = self._pull_context(run_id)
        feedback = self._feedback_item(row)
        operation_id, attempted_at = self._stage_response_operation(
            run_id, str(row["id"]), feedback, body
        )
        output = self.gateway.find_response(
            str(context["owner"]),
            str(context["name"]),
            _required_int(context["pull_number"], "pull number"),
            feedback,
            body,
            attempted_at,
        )
        if output is None:
            with self.lifecycle.external_effect(run_id) as active:
                if not active or self.lifecycle.get_run(run_id)["state"] != RunState.RESOLVING_FEEDBACK.value:
                    raise RuntimeError("run stopped before feedback response")
                output = self.gateway.post_response(
                    str(context["owner"]),
                    str(context["name"]),
                    _required_int(context["pull_number"], "pull number"),
                    feedback,
                    body,
                )
                self._record_response(context, row, operation_id, output)
            return
        self._record_response(context, row, operation_id, output)

    def _record_response(
        self,
        context: dict[str, object],
        row: dict[str, object],
        operation_id: str,
        output: FeedbackOutput,
    ) -> None:
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO application_outputs
                   (id, pull_request_id, feedback_type, github_object_id,
                    operation_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    _stable_id(f"output:{output.feedback_type}:{output.object_id}"),
                    context["pull_id"],
                    output.feedback_type,
                    output.object_id,
                    operation_id,
                    output.created_at or now,
                ),
            )
            connection.execute(
                """UPDATE outbound_operations
                   SET state='completed', external_id=?, completed_at=?, error=NULL
                   WHERE id=?""",
                (output.object_id, now, operation_id),
            )
            connection.execute(
                "UPDATE feedback_versions SET response_operation_id=? WHERE id=?",
                (operation_id, row["id"]),
            )

    def _stage_response_operation(
        self,
        run_id: str,
        feedback_id: str,
        feedback: FeedbackItem,
        body: str,
    ) -> tuple[str, str]:
        idempotency_key = f"{feedback_id}:response"
        operation_id = _stable_id(idempotency_key)
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state,
                    created_at, attempted_at)
                   VALUES (?, ?, 'post_feedback_response', ?, ?, 'pending', ?, ?)""",
                (
                    operation_id,
                    run_id,
                    idempotency_key,
                    _json(
                        {
                            "feedback_type": feedback.feedback_type,
                            "target_object_id": feedback.object_id,
                            "body": body,
                        }
                    ),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT attempted_at FROM outbound_operations WHERE id=?",
                (operation_id,),
            ).fetchone()
        return operation_id, str(row["attempted_at"])

    def _ensure_resolving(self, run_id: str) -> bool:
        run = self.lifecycle.get_run(run_id)
        state = RunState(str(run["state"]))
        if state == RunState.BLOCKED:
            return False
        if state in {
            RunState.WAITING_FOR_FEEDBACK,
            RunState.QUIET_PERIOD,
            RunState.NOTIFIED,
        }:
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE quiet_periods
                       SET state='canceled', canceled_at=?
                       WHERE run_id=? AND state='active'""",
                    (_utc_now(), run_id),
                )
            self.lifecycle.transition(run_id, RunState.RESOLVING_FEEDBACK)
            return True
        return state == RunState.RESOLVING_FEEDBACK

    def _finish_feedback_cycle(self, run_id: str) -> None:
        state = RunState(str(self.lifecycle.get_run(run_id)["state"]))
        if state == RunState.RESOLVING_FEEDBACK:
            self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
            state = RunState.WAITING_FOR_FEEDBACK
        if state == RunState.WAITING_FOR_FEEDBACK:
            self.quiet.start(run_id)

    def _pending(self, run_id: str) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT feedback_versions.*
                   FROM feedback_versions
                   JOIN pull_requests ON pull_requests.id=feedback_versions.pull_request_id
                   WHERE pull_requests.run_id=?
                     AND feedback_versions.state IN ('pending', 'processing')
                   ORDER BY feedback_versions.observed_at, feedback_versions.id""",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _pull_context(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT pull_requests.id AS pull_id,
                          pull_requests.number AS pull_number,
                          pull_requests.state AS pull_state,
                          repositories.owner, repositories.name
                   FROM pull_requests
                   JOIN runs ON runs.id=pull_requests.run_id
                   JOIN repositories ON repositories.id=runs.repository_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        if row is None or row["pull_number"] is None:
            raise KeyError(f"run has no published pull request: {run_id}")
        return dict(row)

    def _evaluation_context(
        self, run_id: str, feedback: dict[str, object]
    ) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT issues.title, issues.body, issues.discussion_json,
                          runs.base_sha, runs.validated_sha, runs.checkout_path,
                          sandbox_versions.evidence_json,
                          team_members.runtime AS lead_runtime,
                          team_members.model AS lead_model
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN sandbox_versions ON sandbox_versions.id=runs.sandbox_version_id
                   JOIN team_members
                     ON team_members.team_version_id=runs.team_version_id
                    AND team_members.role='lead'
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
            prior = connection.execute(
                """SELECT feedback_versions.feedback_type,
                          feedback_versions.author,
                          feedback_versions.body,
                          feedback_versions.state,
                          feedback_versions.decision_json
                   FROM feedback_versions
                   JOIN pull_requests ON pull_requests.id=feedback_versions.pull_request_id
                   WHERE pull_requests.run_id=? AND feedback_versions.id<>?
                   ORDER BY feedback_versions.observed_at""",
                (run_id, feedback["id"]),
            ).fetchall()
        if row is None:
            raise KeyError(run_id)
        base_sha = str(row["base_sha"])
        validated_sha = str(row["validated_sha"] or "")
        checkout = Path(str(row["checkout_path"]))
        current_diff = _committed_diff(checkout, base_sha, validated_sha)
        addressed_source = _addressed_source(
            checkout,
            validated_sha,
            feedback.get("path"),
            feedback.get("line"),
        )
        return {
            "run_id": run_id,
            "issue": {
                "title": row["title"],
                "body": row["body"],
                "discussion": json.loads(row["discussion_json"]),
            },
            "repository_evidence": json.loads(row["evidence_json"]),
            "current_base_sha": row["base_sha"],
            "current_validated_sha": row["validated_sha"],
            "current_diff": current_diff,
            "addressed_source": addressed_source,
            "stored_lead": {
                "runtime": row["lead_runtime"],
                "model": row["lead_model"],
            },
            "feedback": self._feedback_payload(feedback),
            "prior_feedback": [dict(item) for item in prior],
        }

    @staticmethod
    def _feedback_payload(row: dict[str, object]) -> dict[str, object]:
        return {
            "type": row["feedback_type"],
            "object_id": row["github_object_id"],
            "version": row["github_version"],
            "author": row["author"],
            "body": row["body"],
            "path": row.get("path"),
            "line": row.get("line"),
            "url": row.get("url"),
        }


def _committed_diff(checkout: Path, base_sha: str, validated_sha: str) -> str:
    for name, value in (("base", base_sha), ("validated", validated_sha)):
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise RuntimeError(f"feedback context has an invalid {name} commit SHA")
    result = subprocess.run(
        ["git", "diff", "--binary", base_sha, validated_sha, "--"],
        cwd=checkout,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "cannot read committed feedback diff: "
            + (result.stderr.strip() or f"git exited {result.returncode}")
        )
    return result.stdout


def _addressed_source(
    checkout: Path,
    validated_sha: str,
    raw_path: object,
    raw_line: object,
) -> dict[str, object]:
    if raw_path is None:
        return {"status": "not_applicable"}
    if not isinstance(raw_path, str) or not _safe_git_path(raw_path):
        return {"status": "invalid_path"}
    line = raw_line if isinstance(raw_line, int) and raw_line > 0 else 1
    object_name = f"{validated_sha}:{raw_path}"
    size = subprocess.run(
        ["git", "cat-file", "-s", object_name],
        cwd=checkout,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if size.returncode != 0:
        return {
            "status": "unavailable",
            "path": raw_path,
            "line": line,
        }
    try:
        byte_count = int(size.stdout.strip())
    except ValueError as error:
        raise RuntimeError("git returned an invalid source size") from error
    if byte_count > 1_000_000:
        return {"status": "too_large", "path": raw_path, "line": line}
    content = subprocess.run(
        ["git", "cat-file", "-p", object_name],
        cwd=checkout,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if content.returncode != 0:
        return {"status": "unavailable", "path": raw_path, "line": line}
    if b"\0" in content.stdout:
        return {"status": "binary", "path": raw_path, "line": line}
    try:
        text = content.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return {"status": "binary", "path": raw_path, "line": line}
    lines = text.splitlines()
    start = max(0, line - 21)
    end = min(len(lines), line + 20)
    excerpt = "\n".join(
        f"{number}: {lines[number - 1]}" for number in range(start + 1, end + 1)
    )
    return {
        "status": "available",
        "path": raw_path,
        "line": line,
        "content": excerpt,
    }


def _required_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeError(f"{field} must be an integer")
    return int(value)


def _safe_git_path(value: str) -> bool:
    if not value or "\\" in value or any(ord(character) < 32 for character in value):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "." not in path.parts
        and str(path) == value
    )


def _stable_id(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
