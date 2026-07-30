from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol

from .controller import RunProcessSupervisor
from .database import Database
from .github import FeedbackItem, FeedbackOutput, PullRequestInfo
from .mini_swe import MINI_SWE_RUNTIME, MiniSweInference
from .lifecycle import (
    FEEDBACK_VALIDATION_RECOVERY_REASONS,
    RunLifecycle,
    RunState,
)
from .publication import PublicationBaseChanged


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

    def get_remote_branch_head(
        self, owner: str, name: str, branch: str
    ) -> str | None: ...

    def list_feedback(
        self, owner: str, name: str, pull_number: int
    ) -> list[FeedbackItem]: ...

    def application_login(self) -> str: ...

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
    def resolve_review_thread(self, thread_id: str) -> None: ...


class FeedbackEvaluator(Protocol):
    def evaluate(self, context: dict[str, object]) -> FeedbackDecision: ...


class SourceExecutor(Protocol):
    def execute(
        self,
        run_id: str,
        *,
        additional_context: str | None = None,
        comparison_base_sha: str | None = None,
    ) -> str | None: ...


class RevisionPublisher(Protocol):
    def publish(self, run_id: str) -> object | None: ...
    def prepare_base_revision(self, run_id: str, expected_base_sha: str) -> str: ...


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
        api_key: str | None = None,
        connection_resolver: (
            Callable[[str], tuple[str | None, str | None]] | None
        ) = None,
        state_root: Path | None = None,
        processes: RunProcessSupervisor | None = None,
        timeout: float = 600,
    ) -> None:
        if timeout <= 0:
            raise ValueError("feedback evaluator timeout must be positive")
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.connection_resolver = connection_resolver
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
                raise RuntimeError("stored feedback lead configuration is invalid")
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
            raise RuntimeError("feedback evaluator requires an explicit stored model")
        prompt = json.dumps(
            {
                "task": (
                    "Evaluate this pull-request feedback against the original "
                    "issue, discussion, repository instructions/evidence, current "
                    "implementation state, prior feedback, and scope."
                ),
                "context": context,
                "actions": {
                    "revise": "concrete valid in-scope source change required",
                    "answer": (
                        "actual concrete question or requested explanation "
                        "without source change"
                    ),
                    "decline": (
                        "concrete incorrect, contradictory, or out-of-scope request"
                    ),
                    "ignore": "no response or source action is warranted",
                },
                "routing": {
                    "eligible": [
                        (
                            "a general comment explicitly addressed to "
                            "context.application_login"
                        ),
                        (
                            "a concrete pull-request question, requested change, "
                            "contradiction, or substantive review finding, even "
                            "when it begins with another account mention"
                        ),
                        (
                            "an unresolved inline review finding, which does not "
                            "require an @ mention"
                        ),
                    ],
                    "ignore": [
                        (
                            "a recognized standalone external-integration command "
                            "whose leading @login addresses another account, "
                            "including @login review and @login address that feedback"
                        ),
                        (
                            "a generic review wrapper, acknowledgment, status "
                            "remark, or input with no concrete question, request, "
                            "contradiction, or finding"
                        ),
                    ],
                    "response": (
                        "Never invent a review or status summary for non-actionable "
                        "input. Return ignore with an empty response instead."
                    ),
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
        base_url = self.base_url
        api_key = self.api_key
        if self.connection_resolver is not None:
            base_url, api_key = self.connection_resolver(model)
        inference = self._build_inference(
            MiniSweInference(
                model=model,
                base_url=base_url,
                api_key=api_key,
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
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle
        self.gateway = gateway
        self.evaluator = evaluator
        self.executor = executor
        self.publisher = publisher

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
            self.lifecycle.close_pull_request_attempt(run_id, pull)
            return 0
        conflict_item: FeedbackItem | None = None
        if pull.mergeable is False:
            current_base_sha = self.gateway.get_remote_branch_head(
                str(context["owner"]),
                str(context["name"]),
                pull.base_branch,
            )
            if current_base_sha is None:
                raise RuntimeError(
                    "conflicting pull request base branch has no current head"
                )
            conflict_item = _base_conflict_item(pull, current_base_sha)
        items = self.gateway.list_feedback(
            str(context["owner"]),
            str(context["name"]),
            _required_int(context["pull_number"], "pull number"),
        )
        if conflict_item is not None:
            items.insert(0, conflict_item)
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
            current_conflict_id: str | None = None
            for item in items:
                if (item.feedback_type, item.object_id) in outputs:
                    continue
                feedback_id = _stable_id(
                    f"{context['pull_id']}:{item.feedback_type}:{item.object_id}:{item.version}"
                )
                if item.feedback_type == "base_conflict":
                    current_conflict_id = feedback_id
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO feedback_versions
                       (id, pull_request_id, feedback_type, github_object_id,
                        github_version, author, body, path, line, url,
                        review_thread_id, review_thread_resolved, state,
                        observed_at, decision_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
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
                        item.review_thread_id,
                        (
                            int(item.review_thread_resolved)
                            if item.review_thread_resolved is not None
                            else None
                        ),
                        _utc_now(),
                        (
                            _json(
                                asdict(
                                    FeedbackDecision(
                                        action="revise",
                                        reason=(
                                            "The current open pull request is "
                                            "not mergeable into its base."
                                        ),
                                        response=(
                                            "Resolved the current base conflict, "
                                            "revalidated the result, and updated "
                                            "this pull request."
                                        ),
                                    )
                                )
                            )
                            if item.feedback_type == "base_conflict"
                            else None
                        ),
                    ),
                )
                if item.review_thread_id is not None:
                    connection.execute(
                        """UPDATE feedback_versions
                           SET review_thread_id=?, review_thread_resolved=?
                           WHERE pull_request_id=?
                             AND feedback_type=?
                             AND github_object_id=?
                             AND github_version=?""",
                        (
                            item.review_thread_id,
                            int(bool(item.review_thread_resolved)),
                            context["pull_id"],
                            item.feedback_type,
                            item.object_id,
                            item.version,
                        ),
                    )
                inserted += cursor.rowcount
            if pull.mergeable is not None:
                self._supersede_base_conflicts(
                    connection,
                    run_id=run_id,
                    pull_request_id=str(context["pull_id"]),
                    current_conflict_id=current_conflict_id,
                )
            self._activate_pending_feedback(connection, run_id)
        return inserted

    @staticmethod
    def _supersede_base_conflicts(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        pull_request_id: str,
        current_conflict_id: str | None,
    ) -> None:
        rows = connection.execute(
            """SELECT id
               FROM feedback_versions
               WHERE pull_request_id=?
                 AND feedback_type='base_conflict'
                 AND state IN ('pending', 'processing')
                 AND (? IS NULL OR id<>?)""",
            (
                pull_request_id,
                current_conflict_id,
                current_conflict_id,
            ),
        ).fetchall()
        stale_ids = {str(row["id"]) for row in rows}
        if not stale_ids:
            return
        now = _utc_now()
        for feedback_id in stale_ids:
            connection.execute(
                """UPDATE feedback_versions
                   SET state='resolved',
                       processed_at=COALESCE(processed_at, ?),
                       superseded_at=?,
                       superseded_by_feedback_id=?
                   WHERE id=?
                     AND state IN ('pending', 'processing')""",
                (
                    now,
                    now,
                    current_conflict_id,
                    feedback_id,
                ),
            )
        operations = connection.execute(
            """SELECT id, request_json
               FROM outbound_operations
               WHERE run_id=?
                 AND kind='feedback_revision_batch'
                 AND state='pending'""",
            (run_id,),
        ).fetchall()
        for operation in operations:
            try:
                request = json.loads(str(operation["request_json"]))
            except (TypeError, ValueError):
                continue
            feedback_ids = (
                request.get("feedback_ids")
                if isinstance(request, dict)
                else None
            )
            if not isinstance(feedback_ids, list) or stale_ids.isdisjoint(
                str(value) for value in feedback_ids
            ):
                continue
            connection.execute(
                """UPDATE outbound_operations
                   SET state='reconciled', external_id=?, completed_at=?,
                       error=NULL
                   WHERE id=? AND state='pending'""",
                (
                    current_conflict_id,
                    now,
                    operation["id"],
                ),
            )

    def resolve_run(self, run_id: str) -> int:
        processed = 0
        self._reconcile_pending_responses(run_id)
        for _ in range(20):
            self.poll_run(run_id)
            if self._pull_context(run_id)["pull_state"] != "open":
                return processed
            pending = self._pending(run_id)
            if not pending:
                if self._has_review_thread_work(run_id):
                    if not self._ensure_resolving(run_id):
                        return processed
                    self._resolve_review_threads(run_id)
                    if self._has_review_thread_work(run_id):
                        return processed
                self._finish_feedback_cycle(run_id)
                return processed
            if not self._ensure_resolving(run_id):
                return processed

            for row in pending:
                self._decision(run_id, row)
                if not self._ensure_resolving(run_id):
                    return processed

            pending = self._pending(run_id)
            if any(row.get("decision_json") is None for row in pending):
                continue

            revisions: list[dict[str, object]] = []
            for row in pending:
                decision = self._stored_decision(row)
                if decision.action == "revise":
                    revisions.append(row)
                    continue
                if decision.response:
                    self._respond(run_id, row, decision.response)
                final_state = {
                    "answer": "answered",
                    "decline": "declined",
                    "ignore": "resolved",
                }[decision.action]
                self._complete_feedback((row,), final_state)
                processed += 1

            self.poll_run(run_id)
            if self._pull_context(run_id)["pull_state"] != "open":
                return processed
            pending = self._pending(run_id)
            if not pending:
                continue
            if any(row.get("decision_json") is None for row in pending):
                continue
            revisions = [
                row
                for row in pending
                if self._stored_decision(row).action == "revise"
            ]
            if len(revisions) != len(pending):
                continue

            self._recover_revision_batch(run_id, revisions)
            source_shas = {
                str(row["source_sha"])
                for row in revisions
                if row.get("source_sha")
            }
            all_bound = len(source_shas) == 1 and all(
                row.get("source_sha") for row in revisions
            )
            if all_bound and self._published_revision_sha(run_id) in source_shas:
                self._complete_feedback(tuple(revisions), "resolved")
                processed += len(revisions)
                continue

            if not all_bound:
                run = self.lifecycle.get_run(run_id)
                state = RunState(str(run["state"]))
                run_reason = str(run.get("reason") or "")
                validation_only = (
                    run_reason in FEEDBACK_VALIDATION_RECOVERY_REASONS
                )
                if state == RunState.PUBLISHING:
                    self.lifecycle.transition(
                        run_id,
                        RunState.IMPLEMENTING,
                        reason=(
                            run_reason
                            if validation_only
                            else (
                                "new pull-request feedback arrived "
                                "before publication"
                            )
                        ),
                    )
                    state = RunState.IMPLEMENTING
                if validation_only and state in {
                    RunState.RESOLVING_FEEDBACK,
                    RunState.IMPLEMENTING,
                }:
                    self.lifecycle.transition(
                        run_id,
                        RunState.VALIDATING,
                        reason=run_reason,
                    )
                    state = RunState.VALIDATING
                if state not in {
                    RunState.RESOLVING_FEEDBACK,
                    RunState.IMPLEMENTING,
                    RunState.VALIDATING,
                }:
                    return processed
                try:
                    batch_operation_id = self._stage_revision_batch(
                        run_id,
                        revisions,
                    )
                    revision_context, comparison_base_sha = self._revision_context(
                        run_id,
                        revisions,
                    )
                    source_sha = self.executor.execute(
                        run_id,
                        additional_context=revision_context,
                        comparison_base_sha=comparison_base_sha,
                    )
                except PublicationBaseChanged:
                    continue
                if source_sha is None:
                    return processed
                with self.database.transaction() as connection:
                    for row in revisions:
                        connection.execute(
                            "UPDATE feedback_versions SET source_sha=? WHERE id=?",
                            (source_sha, row["id"]),
                        )
                    connection.execute(
                        """UPDATE outbound_operations
                           SET state='completed', external_id=?, completed_at=?,
                               error=NULL
                           WHERE id=?""",
                        (source_sha, _utc_now(), batch_operation_id),
                    )

                self.poll_run(run_id)
                if self._pull_context(run_id)["pull_state"] != "open":
                    return processed
                latest = self._pending(run_id)
                revision_ids = {str(row["id"]) for row in revisions}
                if any(
                    str(row["id"]) not in revision_ids
                    or row.get("decision_json") is None
                    for row in latest
                ):
                    continue

            if self._reopen_publication_rejected_batch(run_id, revisions):
                continue
            if self.publisher.publish(run_id) is None:
                if self._reopen_publication_rejected_batch(run_id, revisions):
                    continue
                return processed
            self._complete_feedback(tuple(revisions), "resolved")
            processed += len(revisions)
        return processed

    @staticmethod
    def _stored_decision(row: dict[str, object]) -> FeedbackDecision:
        value = row.get("decision_json")
        if not value:
            raise RuntimeError("feedback decision is not durable")
        return FeedbackDecision(**json.loads(str(value)))

    def _complete_feedback(
        self,
        rows: tuple[dict[str, object], ...],
        state: str,
    ) -> None:
        now = _utc_now()
        with self.database.transaction() as connection:
            for row in rows:
                connection.execute(
                    """UPDATE feedback_versions
                       SET state=?, processed_at=? WHERE id=?""",
                    (state, now, row["id"]),
                )

    def _stage_revision_batch(
        self,
        run_id: str,
        rows: list[dict[str, object]],
    ) -> str:
        feedback_ids = sorted(str(row["id"]) for row in rows)
        idempotency_key = (
            f"{run_id}:feedback-revision-batch:" + ",".join(feedback_ids)
        )
        operation_id = _stable_id(idempotency_key)
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state,
                    created_at)
                   VALUES (?, ?, 'feedback_revision_batch', ?, ?, 'pending', ?)""",
                (
                    operation_id,
                    run_id,
                    idempotency_key,
                    _json({"feedback_ids": feedback_ids}),
                    _utc_now(),
                ),
            )
        return operation_id

    def _reopen_publication_rejected_batch(
        self,
        run_id: str,
        rows: list[dict[str, object]],
    ) -> bool:
        source_shas = {str(row.get("source_sha") or "") for row in rows}
        if len(source_shas) != 1 or "" in source_shas:
            return False
        rejected_sha = next(iter(source_shas))
        feedback_ids = sorted(str(row["id"]) for row in rows)
        idempotency_key = (
            f"{run_id}:feedback-revision-batch:" + ",".join(feedback_ids)
        )
        operation_id = _stable_id(idempotency_key)
        request_json = _json({"feedback_ids": feedback_ids})
        now = _utc_now()
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT state, reason FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or run["state"] != RunState.IMPLEMENTING.value
                or not str(run["reason"] or "").startswith(
                    "publication revision required:"
                )
            ):
                return False
            current_rows = [
                connection.execute(
                    """SELECT state, source_sha, superseded_at
                       FROM feedback_versions WHERE id=?""",
                    (feedback_id,),
                ).fetchone()
                for feedback_id in feedback_ids
            ]
            if any(
                row is None
                or row["state"] not in {"pending", "processing"}
                or row["source_sha"] != rejected_sha
                or row["superseded_at"] is not None
                for row in current_rows
            ):
                return False
            for feedback_id in feedback_ids:
                connection.execute(
                    "UPDATE feedback_versions SET source_sha=NULL WHERE id=?",
                    (feedback_id,),
                )
            connection.execute(
                """INSERT OR IGNORE INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state,
                    created_at)
                   VALUES (?, ?, 'feedback_revision_batch', ?, ?, 'pending', ?)""",
                (
                    operation_id,
                    run_id,
                    idempotency_key,
                    request_json,
                    now,
                ),
            )
            connection.execute(
                """UPDATE outbound_operations
                   SET request_json=?, state='pending', external_id=NULL,
                       attempted_at=NULL, completed_at=NULL, error=NULL
                   WHERE id=? AND run_id=?
                     AND kind='feedback_revision_batch'""",
                (request_json, operation_id, run_id),
            )
        return True

    def _recover_revision_batch(
        self,
        run_id: str,
        rows: list[dict[str, object]],
    ) -> None:
        run = self.lifecycle.get_run(run_id)
        if (
            run["state"] != RunState.PUBLISHING.value
            or not run.get("validated_sha")
        ):
            return
        with self.database.connect() as connection:
            operation = connection.execute(
                """SELECT id, request_json FROM outbound_operations
                   WHERE run_id=? AND kind='feedback_revision_batch'
                     AND state='pending'
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (run_id,),
            ).fetchone()
        if operation is None:
            return
        request = json.loads(str(operation["request_json"]))
        feedback_ids = request.get("feedback_ids")
        if not isinstance(feedback_ids, list) or not all(
            isinstance(value, str) for value in feedback_ids
        ):
            raise RuntimeError("durable feedback revision batch is invalid")
        by_id = {str(row["id"]): row for row in rows}
        matched = [by_id[value] for value in feedback_ids if value in by_id]
        if not matched:
            return
        source_sha = str(run["validated_sha"])
        now = _utc_now()
        with self.database.transaction() as connection:
            for row in matched:
                connection.execute(
                    """UPDATE feedback_versions
                       SET source_sha=? WHERE id=? AND source_sha IS NULL""",
                    (source_sha, row["id"]),
                )
                row["source_sha"] = source_sha
            connection.execute(
                """UPDATE outbound_operations
                   SET state='completed', external_id=?, completed_at=?,
                       error=NULL
                   WHERE id=?""",
                (source_sha, now, operation["id"]),
            )

    def _published_revision_sha(self, run_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT remote_head_sha FROM pull_requests WHERE run_id=?""",
                (run_id,),
            ).fetchone()
        if row is None or row["remote_head_sha"] is None:
            return None
        return str(row["remote_head_sha"])

    def _latest_integrated_conflict_base(self, run_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT feedback_versions.github_version
                   FROM feedback_versions
                   JOIN pull_requests
                     ON pull_requests.id=feedback_versions.pull_request_id
                   WHERE pull_requests.run_id=?
                     AND feedback_versions.feedback_type='base_conflict'
                     AND feedback_versions.state='resolved'
                     AND feedback_versions.source_sha IS NOT NULL
                     AND feedback_versions.superseded_at IS NULL
                   ORDER BY feedback_versions.observed_at DESC,
                            feedback_versions.id DESC
                   LIMIT 1""",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return _conflict_base_sha(dict(row))

    def _revision_context(
        self,
        run_id: str,
        rows: list[dict[str, object]],
    ) -> tuple[str, str | None]:
        batch: list[dict[str, object]] = []
        comparison_base_sha: str | None = None
        for row in rows:
            decision = self._stored_decision(row)
            instruction = (
                "Implement this valid in-scope feedback on the existing "
                "pull-request checkout, then rerun affected repository validation."
            )
            if row["feedback_type"] == "base_conflict":
                current_base_sha = _conflict_base_sha(row)
                if (
                    comparison_base_sha is not None
                    and comparison_base_sha != current_base_sha
                ):
                    raise RuntimeError(
                        "feedback batch contains conflicting prepared base revisions"
                    )
                comparison_base_sha = current_base_sha
                if not row.get("source_sha"):
                    prepared_sha = self.publisher.prepare_base_revision(
                        run_id,
                        current_base_sha,
                    )
                    if prepared_sha != current_base_sha:
                        raise RuntimeError(
                            "prepared pull-request base does not match "
                            "the observed conflict generation"
                        )
                instruction = (
                    "The controller fetched the conflicting base commit "
                    f"{current_base_sha} into this checkout. Merge that exact "
                    "commit into the current issue commit, resolve every conflict "
                    "while preserving issue scope, commit the result, and rerun "
                    "required validation. Keep the existing branch and pull request."
                )
            batch.append(
                {
                    "feedback": self._feedback_payload(row),
                    "decision": asdict(decision),
                    "instruction": instruction,
                }
            )
        if comparison_base_sha is None:
            comparison_base_sha = self._latest_integrated_conflict_base(run_id)
        return (
            json.dumps(
                {
                    "instruction": (
                        "Implement every revision in this feedback batch as one "
                        "candidate, internally review it, then validate it once."
                    ),
                    "feedback_batch": batch,
                },
                sort_keys=True,
            ),
            comparison_base_sha,
        )

    def _decision(self, run_id: str, row: dict[str, object]) -> FeedbackDecision:
        existing = row.get("decision_json")
        if existing:
            if row.get("state") == "pending":
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE feedback_versions SET state='processing' WHERE id=?",
                        (row["id"],),
                    )
                row["state"] = "processing"
            value = json.loads(str(existing))
            return FeedbackDecision(**value)

        application_login: str | None = None
        if (
            row.get("feedback_type") == "inline_comment"
            and bool(row.get("review_thread_resolved"))
        ):
            decision = FeedbackDecision(
                action="ignore",
                reason="The GitHub review thread is already resolved.",
                response="",
            )
        else:
            if row.get("feedback_type") == "comment":
                application_login = self.gateway.application_login()
            decision = _addressing_decision(row, application_login)
        if decision is None:
            context = self._evaluation_context(
                run_id,
                row,
                application_login=application_login,
            )
            decision = self.evaluator.evaluate(context)

        self.poll_run(run_id)
        if not self._ensure_resolving(run_id):
            return decision
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
            line=(
                _required_int(row["line"], "feedback line")
                if row.get("line") is not None
                else None
            ),
            url=str(row.get("url") or ""),
            created_at=str(row["observed_at"]),
            updated_at=str(row["github_version"]),
            review_thread_id=(
                str(row["review_thread_id"])
                if row.get("review_thread_id") is not None
                else None
            ),
            review_thread_resolved=(
                bool(row["review_thread_resolved"])
                if row.get("review_thread_resolved") is not None
                else None
            ),
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
                if not active or not self._ensure_resolving(run_id):
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

    def _has_review_thread_work(self, run_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT
                       EXISTS (
                           SELECT 1
                           FROM feedback_versions
                           JOIN pull_requests
                             ON pull_requests.id=feedback_versions.pull_request_id
                           WHERE pull_requests.run_id=?
                             AND feedback_versions.state IN (
                                 'resolved', 'answered', 'declined'
                             )
                             AND feedback_versions.review_thread_id IS NOT NULL
                             AND feedback_versions.review_thread_resolved=0
                       ),
                       EXISTS (
                           SELECT 1
                           FROM outbound_operations
                           WHERE run_id=?
                             AND kind='resolve_review_thread'
                             AND state IN ('pending', 'attempted')
                       )""",
                (run_id, run_id),
            ).fetchone()
        return bool(row[0]) or bool(row[1])

    def _resolve_review_threads(self, run_id: str) -> None:
        self._stage_review_thread_operations(run_id)
        with self.database.connect() as connection:
            operations = connection.execute(
                """SELECT id, request_json
                   FROM outbound_operations
                   WHERE run_id=?
                     AND kind='resolve_review_thread'
                     AND state IN ('pending', 'attempted')
                   ORDER BY created_at, id""",
                (run_id,),
            ).fetchall()
        for operation in operations:
            request = json.loads(str(operation["request_json"]))
            thread_id = (
                request.get("thread_id")
                if isinstance(request, dict)
                else None
            )
            if not isinstance(thread_id, str) or not thread_id:
                raise RuntimeError(
                    "pending review-thread resolution has an invalid thread ID"
                )
            with self.database.connect() as connection:
                unresolved = connection.execute(
                    """SELECT 1
                       FROM feedback_versions
                       JOIN pull_requests
                         ON pull_requests.id=feedback_versions.pull_request_id
                       WHERE pull_requests.run_id=?
                         AND feedback_versions.review_thread_id=?
                         AND feedback_versions.state IN (
                             'resolved', 'answered', 'declined'
                         )
                         AND feedback_versions.review_thread_resolved=0
                       LIMIT 1""",
                    (run_id, thread_id),
                ).fetchone()
            if unresolved is None:
                with self.database.transaction() as connection:
                    connection.execute(
                        """UPDATE outbound_operations
                           SET state='completed', external_id=?,
                               completed_at=?, error=NULL
                           WHERE id=?""",
                        (thread_id, _utc_now(), operation["id"]),
                    )
                continue
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        """UPDATE outbound_operations
                           SET attempted_at=?, error=NULL WHERE id=?""",
                        (_utc_now(), operation["id"]),
                    )
                with self.lifecycle.external_effect(run_id) as active:
                    if not active or not self._ensure_resolving(run_id):
                        raise RuntimeError(
                            "run stopped before review-thread resolution"
                        )
                    self.gateway.resolve_review_thread(thread_id)
                with self.database.transaction() as connection:
                    now = _utc_now()
                    connection.execute(
                        """UPDATE feedback_versions
                           SET review_thread_resolved=1
                           WHERE pull_request_id IN (
                               SELECT id FROM pull_requests WHERE run_id=?
                           )
                             AND review_thread_id=?""",
                        (run_id, thread_id),
                    )
                    connection.execute(
                        """UPDATE outbound_operations
                           SET state='completed', external_id=?,
                               completed_at=?, error=NULL
                           WHERE id=?""",
                        (thread_id, now, operation["id"]),
                    )
            except Exception as error:
                with self.database.transaction() as connection:
                    connection.execute(
                        """UPDATE outbound_operations
                           SET error=? WHERE id=?""",
                        (str(error) or error.__class__.__name__, operation["id"]),
                    )
                raise

    def _stage_review_thread_operations(self, run_id: str) -> None:
        with self.database.transaction() as connection:
            threads = connection.execute(
                """SELECT DISTINCT feedback_versions.review_thread_id
                   FROM feedback_versions
                   JOIN pull_requests
                     ON pull_requests.id=feedback_versions.pull_request_id
                   WHERE pull_requests.run_id=?
                     AND feedback_versions.state IN (
                         'resolved', 'answered', 'declined'
                     )
                     AND feedback_versions.review_thread_id IS NOT NULL
                     AND feedback_versions.review_thread_resolved=0
                   ORDER BY feedback_versions.review_thread_id""",
                (run_id,),
            ).fetchall()
            for row in threads:
                thread_id = str(row["review_thread_id"])
                existing = connection.execute(
                    """SELECT id
                       FROM outbound_operations
                       WHERE run_id=?
                         AND kind='resolve_review_thread'
                         AND state IN ('pending', 'attempted')
                         AND json_extract(request_json, '$.thread_id')=?
                       LIMIT 1""",
                    (run_id, thread_id),
                ).fetchone()
                if existing is not None:
                    continue
                generation = int(
                    connection.execute(
                        """SELECT COUNT(*)
                           FROM outbound_operations
                           WHERE run_id=?
                             AND kind='resolve_review_thread'
                             AND json_extract(
                                 request_json,
                                 '$.thread_id'
                             )=?""",
                        (run_id, thread_id),
                    ).fetchone()[0]
                ) + 1
                idempotency_key = (
                    f"{run_id}:resolve-review-thread:{thread_id}:{generation}"
                )
                connection.execute(
                    """INSERT INTO outbound_operations
                       (id, run_id, kind, idempotency_key, request_json, state,
                        created_at)
                       VALUES (?, ?, 'resolve_review_thread', ?, ?, 'pending', ?)""",
                    (
                        _stable_id(idempotency_key),
                        run_id,
                        idempotency_key,
                        _json({"thread_id": thread_id}),
                        _utc_now(),
                    ),
                )

    def _activate_pending_feedback(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> None:
        row = connection.execute(
            """SELECT runs.state,
                      repositories.enabled AS repository_enabled,
                      repositories.removed_at AS repository_removed_at,
                      EXISTS (
                          SELECT 1
                          FROM feedback_versions
                          JOIN pull_requests
                            ON pull_requests.id=feedback_versions.pull_request_id
                          WHERE pull_requests.run_id=runs.id
                            AND feedback_versions.state IN ('pending', 'processing')
                      ) AS has_pending
               FROM runs
               JOIN repositories ON repositories.id=runs.repository_id
               WHERE runs.id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        state = RunState(str(row["state"]))
        if (
            not bool(row["has_pending"])
            or not bool(row["repository_enabled"])
            or row["repository_removed_at"] is not None
            or state != RunState.WAITING_FOR_FEEDBACK
        ):
            return
        now = _utc_now()
        connection.execute(
            """UPDATE runs
               SET state='resolving_feedback', last_completed_state=?,
                   reason=NULL, updated_at=?
               WHERE id=?""",
            (state.value, now, run_id),
        )
        connection.execute(
            """INSERT INTO run_transitions
               (run_id, from_state, to_state, occurred_at)
               VALUES (?, ?, 'resolving_feedback', ?)""",
            (run_id, state.value, now),
        )

    def _ensure_resolving(self, run_id: str) -> bool:
        run = self.lifecycle.get_run(run_id)
        state = RunState(str(run["state"]))
        if state in {
            RunState.BLOCKED,
            RunState.CANCELED,
            RunState.CLOSED,
        }:
            return False
        if state == RunState.WAITING_FOR_FEEDBACK:
            self.lifecycle.transition(run_id, RunState.RESOLVING_FEEDBACK)
            return True
        return state in {
            RunState.RESOLVING_FEEDBACK,
            RunState.IMPLEMENTING,
            RunState.VALIDATING,
            RunState.PUBLISHING,
        }

    def _finish_feedback_cycle(self, run_id: str) -> None:
        if self._pending(run_id) or self._has_review_thread_work(run_id):
            return
        state = RunState(str(self.lifecycle.get_run(run_id)["state"]))
        if state == RunState.RESOLVING_FEEDBACK:
            self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)

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
        self,
        run_id: str,
        feedback: dict[str, object],
        *,
        application_login: str | None = None,
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
            "application_login": application_login,
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


def _addressing_decision(
    row: dict[str, object],
    application_login: str | None,
) -> FeedbackDecision | None:
    if row.get("feedback_type") != "comment":
        return None
    match = re.fullmatch(
        (
            r"\s*@(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))"
            r"(?:\s*[:,]\s*|\s+)"
            r"(?:review|address\s+that\s+feedback)\s*[.!]?\s*"
        ),
        str(row.get("body") or ""),
        flags=re.IGNORECASE,
    )
    if (
        match is None
        or match.group("login").casefold() == str(application_login).casefold()
    ):
        return None
    return FeedbackDecision(
        action="ignore",
        reason=(
            "The recognized external-integration command is addressed to "
            f"@{match.group('login')}, not the authenticated Repogents account "
            f"@{application_login}."
        ),
        response="",
    )


def _base_conflict_item(
    pull: PullRequestInfo,
    base_sha: str | None = None,
) -> FeedbackItem | None:
    if pull.mergeable is not False:
        return None
    if not re.fullmatch(r"[0-9a-f]{40}", pull.head_sha):
        raise RuntimeError("conflicting pull request has an invalid head commit SHA")
    current_base_sha = base_sha if base_sha is not None else pull.base_sha
    if not re.fullmatch(r"[0-9a-f]{40}", current_base_sha):
        raise RuntimeError(
            "conflicting pull request has no valid current base commit SHA"
        )
    return FeedbackItem(
        feedback_type="base_conflict",
        object_id=pull.node_id,
        version=f"{pull.head_sha}:{current_base_sha}",
        author="github",
        body=(
            f"Pull request #{pull.number} at head {pull.head_sha} no longer "
            f"merges cleanly into {pull.base_branch} at {current_base_sha}. "
            "Resolve the base conflict on the existing branch, rerun required "
            "validation, and update this same pull request."
        ),
        path=None,
        line=None,
        url=pull.url,
        created_at=pull.updated_at,
        updated_at=pull.updated_at,
    )


def _conflict_base_sha(row: dict[str, object]) -> str:
    version = str(row.get("github_version") or "")
    _, separator, base_sha = version.partition(":")
    if not separator or not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise RuntimeError("stored base-conflict feedback has an invalid generation")
    return base_sha


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
