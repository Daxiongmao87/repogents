from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .controller import RunProcessSupervisor, git_environment
from .database import Database
from .github import GitHubClient, PullRequestInfo
from .mini_swe import MINI_SWE_RUNTIME, MiniSweInference
from .lifecycle import RunLifecycle, RunState
from .sandbox import SecretScanner


@dataclass(frozen=True)
class ScopeDecision:
    in_scope: bool
    reason: str


class ScopeReviewer(Protocol):
    def review(
        self,
        issue: dict[str, object],
        diff: str,
        changed_files: tuple[str, ...],
    ) -> ScopeDecision: ...


class PublicationGateway(Protocol):
    def get_remote_branch_head(self, owner: str, name: str, branch: str) -> str | None: ...

    def fetch_intended_base_head(
        self,
        checkout: Path,
        owner: str,
        name: str,
        branch: str,
    ) -> str: ...

    def push_branch(
        self, checkout: Path, owner: str, name: str, branch: str, sha: str
    ) -> None: ...

    def find_pull_request(
        self, owner: str, name: str, branch: str
    ) -> PullRequestInfo | None: ...

    def create_pull_request(
        self,
        owner: str,
        name: str,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestInfo: ...


class GitPublicationGateway:
    """Runs controller-owned GitHub and Git publication outside the sandbox."""

    def __init__(
        self,
        github: GitHubClient,
        *,
        token: str | None = None,
        git: str = "git",
    ) -> None:
        self.github = github
        self.token = token
        self.git = git

    def get_remote_branch_head(self, owner: str, name: str, branch: str) -> str | None:
        return self.github.get_remote_branch_head(owner, name, branch)

    def fetch_intended_base_head(
        self,
        checkout: Path,
        owner: str,
        name: str,
        branch: str,
    ) -> str:
        with git_environment(self.token) as environment:
            fetch = subprocess.run(
                [
                    self.git,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "fetch",
                    "--no-tags",
                    "--force",
                    f"https://github.com/{owner}/{name}.git",
                    f"refs/heads/{branch}",
                ],
                cwd=checkout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
                env=environment,
            )
            if fetch.returncode != 0:
                raise RuntimeError(
                    f"git fetch failed: {fetch.stderr.strip() or fetch.stdout.strip()}"
                )
            resolve = subprocess.run(
                [self.git, "rev-parse", "FETCH_HEAD"],
                cwd=checkout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
                env=environment,
            )
        head = resolve.stdout.strip()
        if resolve.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise RuntimeError(
                "cannot resolve fetched intended-base head: "
                + (resolve.stderr.strip() or resolve.stdout.strip())
            )
        return head

    def push_branch(
        self, checkout: Path, owner: str, name: str, branch: str, sha: str
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("push SHA is invalid")
        with git_environment(self.token) as environment:
            result = subprocess.run(
                [
                    self.git,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "push",
                    "--porcelain",
                    f"https://github.com/{owner}/{name}.git",
                    f"{sha}:refs/heads/{branch}",
                ],
                cwd=checkout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
                env=environment,
            )
        if result.returncode != 0:
            raise RuntimeError(f"git push failed: {result.stderr.strip() or result.stdout.strip()}")

    def find_pull_request(
        self, owner: str, name: str, branch: str
    ) -> PullRequestInfo | None:
        return self.github.find_pull_request(owner, name, branch)

    def create_pull_request(
        self,
        owner: str,
        name: str,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestInfo:
        return self.github.create_pull_request(owner, name, branch, base, title, body)


class MiniSweScopeReviewer:
    """Use mini-SWE to review the complete committed diff."""

    _RESPONSE_SCHEMA: dict[str, object] = {
        "type": "object",
        "properties": {
            "in_scope": {"type": "boolean"},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["in_scope", "reason"],
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
            raise ValueError("scope reviewer timeout must be positive")
        self.model = model
        self.base_url = base_url
        self.state_root = (
            Path(state_root).expanduser().resolve()
            if state_root is not None
            else (Path.cwd() / ".repogents-model-state" / "scope-review").resolve()
        )
        self.processes = processes
        self.timeout = timeout

    @staticmethod
    def _build_inference(
        inference: MiniSweInference,
    ) -> MiniSweInference:
        return inference

    def review(
        self,
        issue: dict[str, object],
        diff: str,
        changed_files: tuple[str, ...],
    ) -> ScopeDecision:
        model = self.model
        stored_lead = issue.get("stored_lead")
        if stored_lead is not None:
            if not isinstance(stored_lead, dict):
                raise RuntimeError(
                    "stored scope-review lead configuration is invalid"
                )
            if stored_lead.get("runtime") != MINI_SWE_RUNTIME:
                raise RuntimeError(
                    "unsupported stored scope-review runtime: "
                    f"{stored_lead.get('runtime')}"
                )
            stored_model = stored_lead.get("model")
            if not isinstance(stored_model, str) or not stored_model:
                raise RuntimeError("stored scope-review model is invalid")
            if model is None:
                model = stored_model
        if not model:
            raise RuntimeError("scope reviewer requires an explicit stored model")
        prompt = json.dumps(
            {
                "task": (
                    "Decide whether the complete committed "
                    "base-to-validated-head diff is required by the issue, "
                    "complies with the immutable stored repository evidence and "
                    "instructions, and excludes plans, logs, caches, credentials, "
                    "licensed artifacts, generated environment state, and "
                    "unrelated work."
                ),
                "decision_rules": [
                    (
                        "Use only the issue and immutable stored repository "
                        "evidence and instructions; do not apply ambient host "
                        "rules."
                    ),
                    (
                        "Local plans, specification ledgers, and coordination "
                        "files are controller process state and must not be "
                        "required in the committed diff; their absence is "
                        "compliant."
                    ),
                    (
                        "If plans, specification ledgers, or coordination files "
                        "appear in changed_files, reject them when present unless "
                        "immutable stored repository instructions explicitly "
                        "require those tracked files."
                    ),
                ],
                "issue": issue,
                "changed_files": changed_files,
                "diff": diff,
                "response_schema": {
                    "in_scope": "boolean",
                    "reason": "specific string",
                },
            },
            sort_keys=True,
        )
        run_id = str(issue["run_id"]) if "run_id" in issue else None
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
                "Return exactly one JSON object with in_scope and reason. "
                "No prose."
            ),
            prompt=prompt,
            response_schema=self._RESPONSE_SCHEMA,
            state_directory=self.state_root / state_key,
        )
        if (
            not isinstance(value.get("in_scope"), bool)
            or not isinstance(value.get("reason"), str)
            or not str(value["reason"]).strip()
        ):
            raise RuntimeError("scope reviewer returned an invalid decision")
        return ScopeDecision(
            bool(value["in_scope"]),
            str(value["reason"]),
        )


class PublicationBlocked(RuntimeError):
    pass
class PublicationRevisionRequired(PublicationBlocked):
    pass


def _no_secret_values(run_id: str) -> tuple[str, ...]:
    del run_id
    return ()


class PublicationService:
    def __init__(
        self,
        *,
        database: Database,
        lifecycle: RunLifecycle,
        gateway: PublicationGateway,
        scope_reviewer: ScopeReviewer,
        known_secret_values: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle
        self.gateway = gateway
        self.scope_reviewer = scope_reviewer
        self.known_secret_values = known_secret_values or _no_secret_values
        self.scanner = SecretScanner()

    def publish(self, run_id: str) -> PullRequestInfo | None:
        try:
            context = self._context(run_id)
            if context["state"] != RunState.PUBLISHING.value:
                raise PublicationBlocked(
                    f"run cannot publish from state {context['state']}"
                )
            validated_sha = str(context.get("validated_sha") or "")
            checkout = Path(str(context["checkout_path"]))
            issue = self._scope_review_issue(context, validated_sha)
            diff, changed_files = self._preflight(context, checkout, validated_sha)
            scope = self.scope_reviewer.review(issue, diff, changed_files)
            if not scope.in_scope:
                raise PublicationRevisionRequired(
                    f"scope review rejected publication: {scope.reason}"
                )
            branch = f"agent/issue-{context['issue_number']}-{run_id}"
            pull_row = self._stage_pull_request(context, branch, validated_sha)
            self._reconcile_push(context, pull_row, checkout, branch, validated_sha)
            pull = self._reconcile_pull_request(context, branch, validated_sha)
            self.lifecycle.transition(run_id, RunState.WAITING_FOR_FEEDBACK)
            return pull
        except PublicationRevisionRequired as error:
            run = self.lifecycle.get_run(run_id)
            if run["state"] == RunState.PUBLISHING.value:
                self.lifecycle.transition(
                    run_id,
                    RunState.IMPLEMENTING,
                    reason=f"publication revision required: {error}",
                )
            return None
        except PublicationBlocked as error:
            run = self.lifecycle.get_run(run_id)
            if run["state"] == RunState.PUBLISHING.value:
                self.lifecycle.transition(
                    run_id,
                    RunState.BLOCKED,
                    reason=f"publication blocked: {error}",
                )
            return None
        except Exception:
            return None

    @staticmethod
    def _scope_review_issue(
        context: dict[str, object],
        validated_sha: str,
    ) -> dict[str, object]:
        try:
            sandbox_evidence = json.loads(
                str(context["sandbox_evidence_json"])
            )
            team_evidence = json.loads(str(context["team_evidence_json"]))
            discussion = json.loads(str(context["discussion_json"]))
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"stored repository scope context is unreadable: {error}"
            ) from error
        if (
            not isinstance(sandbox_evidence, dict)
            or not isinstance(team_evidence, dict)
            or sandbox_evidence != team_evidence
        ):
            raise RuntimeError(
                "stored sandbox and team scope evidence is missing or inconsistent"
            )
        summary = sandbox_evidence.get("summary")
        instructions = sandbox_evidence.get("instructions")
        if not isinstance(summary, str) or not summary.strip():
            raise RuntimeError("stored repository scope summary is missing")
        if not isinstance(instructions, list) or any(
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
            for item in instructions
        ):
            raise RuntimeError("stored repository scope instructions are invalid")
        if not isinstance(discussion, list):
            raise RuntimeError("stored issue discussion is invalid")
        return {
            "run_id": context["id"],
            "number": context["issue_number"],
            "url": context["issue_url"],
            "title": context["issue_title"],
            "body": context["issue_body"],
            "discussion": discussion,
            "base_sha": context["base_sha"],
            "validated_sha": validated_sha,
            "intended_base_branch": context["intended_base_branch"],
            "sandbox_version_id": context["sandbox_version_id"],
            "team_version_id": context["team_version_id"],
            "repository_evidence": sandbox_evidence,
            "stored_lead": {
                "runtime": context["lead_runtime"],
                "model": context["lead_model"],
            },
            "lead_instructions": context["lead_instructions"],
        }


    def _preflight(
        self,
        context: dict[str, object],
        checkout: Path,
        validated_sha: str,
    ) -> tuple[str, tuple[str, ...]]:
        if not re.fullmatch(r"[0-9a-f]{40}", validated_sha):
            raise PublicationBlocked("run has no valid tested commit SHA")
        head = _git(checkout, ("rev-parse", "HEAD")).strip()
        if head != validated_sha:
            raise PublicationBlocked(
                f"local checkout HEAD {head} does not equal validated SHA {validated_sha}"
            )
        base_sha = str(context["base_sha"])
        ancestor = _git_result(
            checkout, ("merge-base", "--is-ancestor", base_sha, validated_sha)
        )
        if ancestor.returncode != 0:
            raise PublicationBlocked("validated commit does not descend from the stored base SHA")
        current_base_sha = self.gateway.fetch_intended_base_head(
            checkout,
            str(context["owner"]),
            str(context["name"]),
            str(context["intended_base_branch"]),
        )
        current_descends_from_stored = _git_result(
            checkout,
            ("merge-base", "--is-ancestor", base_sha, current_base_sha),
        )
        if current_descends_from_stored.returncode != 0:
            raise PublicationBlocked(
                "current intended-base head does not descend from the stored activation base"
            )
        merge = _git(
            checkout,
            ("merge-tree", base_sha, current_base_sha, validated_sha),
        )
        if "<<<<<<< .our" in merge:
            raise PublicationBlocked(
                "validated commit has a merge conflict with the current intended-base head"
            )
        status = _git(checkout, ("status", "--porcelain", "--untracked-files=no"))
        if status.strip():
            raise PublicationBlocked("tracked checkout state changed after validation")
        with self.database.connect() as connection:
            commands = connection.execute(
                """SELECT validation_commands.command_json,
                          validation_results.exit_status
                   FROM validation_commands
                   LEFT JOIN validation_results
                     ON validation_results.run_id=?
                    AND validation_results.commit_sha=?
                    AND validation_results.command_json=validation_commands.command_json
                   WHERE validation_commands.sandbox_version_id=?
                     AND validation_commands.required=1
                   ORDER BY validation_commands.position""",
                (context["id"], validated_sha, context["sandbox_version_id"]),
            ).fetchall()
        if not commands or any(row["exit_status"] != 0 for row in commands):
            raise PublicationBlocked(
                "every required validation command must pass for the exact published SHA"
            )
        changed_output = _git(
            checkout, ("diff", "--name-only", "--diff-filter=ACMRTD", base_sha, validated_sha, "--")
        )
        changed_files = tuple(line for line in changed_output.splitlines() if line)
        if not changed_files:
            raise PublicationBlocked("validated commit contains no issue change")
        forbidden = tuple(path for path in changed_files if _forbidden_artifact(path))
        if forbidden:
            raise PublicationRevisionRequired(
                "forbidden application, credential, or environment artifact in commit: "
                + ", ".join(forbidden)
            )
        diff = _git(checkout, ("diff", "--binary", base_sha, validated_sha, "--"))
        findings = self.scanner.scan(diff, self.known_secret_values(str(context["id"])))
        if findings:
            raise PublicationRevisionRequired(
                "potential secret in committed diff: " + ", ".join(findings)
            )
        return diff, changed_files

    def _stage_pull_request(
        self,
        context: dict[str, object],
        branch: str,
        validated_sha: str,
    ) -> dict[str, object]:
        pull_id = _stable_id(f"{context['id']}:pull-request")
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, url, branch_name, intended_base_branch,
                    base_sha, validated_head_sha, state, created_at, updated_at)
                   VALUES (?, ?, '', ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     validated_head_sha=excluded.validated_head_sha,
                     updated_at=excluded.updated_at""",
                (
                    pull_id,
                    context["id"],
                    branch,
                    context["intended_base_branch"],
                    context["base_sha"],
                    validated_sha,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pull_requests WHERE run_id=?", (context["id"],)
            ).fetchone()
        return dict(row)

    def _reconcile_push(
        self,
        context: dict[str, object],
        pull_row: dict[str, object],
        checkout: Path,
        branch: str,
        validated_sha: str,
    ) -> None:
        operation_id = self._stage_operation(
            str(context["id"]),
            "push_branch",
            f"{context['id']}:push:{validated_sha}",
            {"branch": branch, "sha": validated_sha},
        )
        owner = str(context["owner"])
        name = str(context["name"])
        remote = self.gateway.get_remote_branch_head(owner, name, branch)
        previous = pull_row.get("remote_head_sha")
        if remote == validated_sha:
            self._complete_operation(operation_id, "reconciled", validated_sha)
        else:
            if remote is not None and remote != previous:
                raise PublicationBlocked(
                    f"remote deterministic branch exists at unexpected SHA {remote}"
                )
            with self.lifecycle.external_effect(str(context["id"])) as active:
                if not active:
                    raise PublicationBlocked(
                        "run was canceled at publication boundary"
                    )
                self._require_publishing(str(context["id"]))
                self.gateway.push_branch(
                    checkout, owner, name, branch, validated_sha
                )
            confirmed = self._confirm_remote(owner, name, branch, validated_sha)
            if not confirmed:
                raise PublicationBlocked("remote branch head did not confirm the validated SHA")
            self._complete_operation(operation_id, "completed", validated_sha)
        with self.database.transaction() as connection:
            self._require_publishing(
                str(context["id"]),
                connection=connection,
            )
            connection.execute(
                """UPDATE pull_requests SET validated_head_sha=?, remote_head_sha=?, updated_at=?
                   WHERE run_id=?""",
                (validated_sha, validated_sha, _utc_now(), context["id"]),
            )

    def _reconcile_pull_request(
        self,
        context: dict[str, object],
        branch: str,
        validated_sha: str,
    ) -> PullRequestInfo:
        operation_id = self._stage_operation(
            str(context["id"]),
            "create_pull_request",
            f"{context['id']}:create_pull_request",
            {"branch": branch, "base": context["intended_base_branch"]},
        )
        owner = str(context["owner"])
        name = str(context["name"])
        pull = self.gateway.find_pull_request(owner, name, branch)
        reconciled = pull is not None
        if pull is None:
            with self.lifecycle.external_effect(str(context["id"])) as active:
                if not active:
                    raise PublicationBlocked(
                        "run was canceled at publication boundary"
                    )
                self._require_publishing(str(context["id"]))
                title = f"Resolve #{context['issue_number']}: {' '.join(str(context['issue_title']).split())}"
                body = (
                    f"Automated implementation for {context['issue_url']}.\n\n"
                    f"Validated commit: `{validated_sha}`\n\n"
                    "This pull request is intentionally unmerged."
                )
                pull = self.gateway.create_pull_request(
                    owner,
                    name,
                    branch,
                    str(context["intended_base_branch"]),
                    title,
                    body,
                )
        if reconciled and pull.head_branch == branch and pull.head_sha != validated_sha:
            for attempt in range(8):
                if pull.head_sha == validated_sha:
                    break
                if attempt < 7:
                    time.sleep(min(0.2 * (attempt + 1), 1.0))
                refreshed = self.gateway.find_pull_request(owner, name, branch)
                if refreshed is not None:
                    pull = refreshed
        if pull.head_branch != branch or pull.head_sha != validated_sha:
            raise PublicationBlocked(
                "pull request head does not equal the deterministic validated branch SHA"
            )
        if pull.base_branch != str(context["intended_base_branch"]):
            raise PublicationBlocked("pull request targets an unexpected base branch")
        if pull.merged or pull.state != "open":
            raise PublicationBlocked("pull request is not open and unmerged")
        now = _utc_now()
        with self.database.transaction() as connection:
            self._require_publishing(
                str(context["id"]),
                connection=connection,
            )
            connection.execute(
                """UPDATE pull_requests
                   SET github_node_id=?, number=?, url=?, remote_head_sha=?,
                       validated_head_sha=?, state='open', updated_at=?
                   WHERE run_id=?""",
                (
                    pull.node_id,
                    pull.number,
                    pull.url,
                    pull.head_sha,
                    validated_sha,
                    now,
                    context["id"],
                ),
            )
        self._complete_operation(
            operation_id,
            "reconciled" if reconciled else "completed",
            pull.node_id,
        )
        return pull

    def _confirm_remote(
        self, owner: str, name: str, branch: str, expected_sha: str
    ) -> bool:
        for attempt in range(5):
            if self.gateway.get_remote_branch_head(owner, name, branch) == expected_sha:
                return True
            if attempt < 4:
                time.sleep(0.2 * (attempt + 1))
        return False

    def _stage_operation(
        self,
        run_id: str,
        kind: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> str:
        operation_id = _stable_id(idempotency_key)
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO outbound_operations
                   (id, run_id, kind, idempotency_key, request_json, state, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    operation_id,
                    run_id,
                    kind,
                    idempotency_key,
                    _json(request),
                    _utc_now(),
                ),
            )
        return operation_id

    def _complete_operation(
        self, operation_id: str, state: str, external_id: str
    ) -> None:
        now = _utc_now()
        with self.database.transaction() as connection:
            operation = connection.execute(
                "SELECT run_id FROM outbound_operations WHERE id=?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise KeyError(operation_id)
            self._require_publishing(
                str(operation["run_id"]),
                connection=connection,
            )
            connection.execute(
                """UPDATE outbound_operations
                   SET state=?, external_id=?, attempted_at=COALESCE(attempted_at, ?),
                       completed_at=?, error=NULL WHERE id=?""",
                (state, external_id, now, now, operation_id),
            )

    def _require_publishing(
        self,
        run_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            with self.database.connect() as active_connection:
                row = active_connection.execute(
                    "SELECT state FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
        else:
            row = connection.execute(
                "SELECT state FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        if row["state"] != RunState.PUBLISHING.value:
            raise PublicationBlocked(
                f"run reached durable {row['state']} state at publication boundary"
            )

    def _context(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT runs.*, repositories.owner, repositories.name,
                          issues.number AS issue_number, issues.title AS issue_title,
                          issues.body AS issue_body, issues.discussion_json,
                          issues.url AS issue_url,
                          sandbox_versions.evidence_json AS sandbox_evidence_json,
                          team_versions.evidence_json AS team_evidence_json,
                          team_members.runtime AS lead_runtime,
                          team_members.model AS lead_model,
                          team_members.instructions AS lead_instructions
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   JOIN issues ON issues.id=runs.issue_id
                   JOIN sandbox_versions
                     ON sandbox_versions.id=runs.sandbox_version_id
                   JOIN team_versions
                     ON team_versions.id=runs.team_version_id
                   JOIN team_members
                     ON team_members.team_version_id=runs.team_version_id
                    AND team_members.role='lead'
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)


def _forbidden_artifact(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    parts = normalized.split("/")
    basename = parts[-1]
    forbidden_directories = {
        ".repogents",
        ".codex",
        "agent-state",
        "dependency-delta",
        "validation",
        "caches",
    }
    if any(part in forbidden_directories for part in parts[:-1]):
        return True
    if basename == ".env" or basename.startswith(".env."):
        return True
    if basename in {"credentials", "credentials.json", "secrets.json"}:
        return True
    return Path(basename).suffix.lower() in {".pem", ".key", ".p12", ".pfx"}


def _git(checkout: Path, arguments: Sequence[str]) -> str:
    result = _git_result(checkout, arguments)
    if result.returncode != 0:
        raise PublicationBlocked(result.stderr.strip() or result.stdout.strip() or "git inspection failed")
    return result.stdout


def _git_result(checkout: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    with git_environment(None) as environment:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *arguments],
            cwd=checkout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
            env=environment,
        )




def _stable_id(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
