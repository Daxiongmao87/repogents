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

from .acceptance import (
    AcceptanceUnavailable,
    render_acceptance_failure,
    render_acceptance_markdown,
)
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


class AcceptanceGate(Protocol):
    def verify(
        self,
        run_id: str,
        commit_sha: str,
        changed_files: Sequence[str],
    ) -> dict[str, object]: ...


class PublicationGateway(Protocol):
    def get_remote_branch_head(
        self, owner: str, name: str, branch: str
    ) -> str | None: ...

    def fetch_intended_base_head(
        self,
        checkout: Path,
        owner: str,
        name: str,
        branch: str,
    ) -> str: ...

    def push_branch(
        self,
        checkout: Path,
        owner: str,
        name: str,
        branch: str,
        sha: str,
        expected_remote_sha: str | None,
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

    def update_pull_request_body(
        self,
        owner: str,
        name: str,
        number: int,
        body: str,
    ) -> None: ...


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
        self,
        checkout: Path,
        owner: str,
        name: str,
        branch: str,
        sha: str,
        expected_remote_sha: str | None,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("push SHA is invalid")
        if expected_remote_sha is not None and not re.fullmatch(
            r"[0-9a-f]{40}",
            expected_remote_sha,
        ):
            raise ValueError("expected remote SHA is invalid")
        lease = (
            f"--force-with-lease=refs/heads/{branch}:"
            + (expected_remote_sha or "")
        )
        with git_environment(self.token) as environment:
            result = subprocess.run(
                [
                    self.git,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "push",
                    "--porcelain",
                    lease,
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
            raise RuntimeError(
                f"git push failed: {result.stderr.strip() or result.stdout.strip()}"
            )

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

    def update_pull_request_body(
        self,
        owner: str,
        name: str,
        number: int,
        body: str,
    ) -> None:
        self.github.update_pull_request_body(owner, name, number, body)


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
        api_key: str | None = None,
        connection_resolver: (
            Callable[[str], tuple[str | None, str | None]] | None
        ) = None,
        state_root: Path | None = None,
        processes: RunProcessSupervisor | None = None,
        timeout: float = 600,
    ) -> None:
        if timeout <= 0:
            raise ValueError("scope reviewer timeout must be positive")
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.connection_resolver = connection_resolver
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
        stored_verifier = issue.get("stored_verifier")
        if stored_verifier is not None:
            if not isinstance(stored_verifier, dict):
                raise RuntimeError("stored verifier configuration is invalid")
            if stored_verifier.get("runtime") != MINI_SWE_RUNTIME:
                raise RuntimeError(
                    "unsupported stored verifier runtime: "
                    f"{stored_verifier.get('runtime')}"
                )
            stored_model = stored_verifier.get("model")
            if not isinstance(stored_model, str) or not stored_model:
                raise RuntimeError("stored verifier model is invalid")
            if model is None:
                model = stored_model
        if not model:
            raise RuntimeError("scope reviewer requires an explicit stored model")
        prompt = json.dumps(
            {
                "task": (
                    "Independently review the complete committed "
                    "base-to-validated-head diff before publication. Approve only "
                    "when it correctly and fully implements the issue, has adequate "
                    "validation, complies with immutable stored repository evidence "
                    "and instructions, and excludes unrelated or forbidden artifacts."
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
                "Return exactly one JSON object with in_scope and reason. " "No prose."
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


class PublicationBaseChanged(PublicationBlocked):
    def __init__(self, expected_base_sha: str, current_base_sha: str) -> None:
        self.expected_base_sha = expected_base_sha
        self.current_base_sha = current_base_sha
        super().__init__(
            "pull-request base changed from "
            f"{expected_base_sha} to {current_base_sha} "
            "before conflict preparation"
        )


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
        acceptance: AcceptanceGate,
        known_secret_values: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle
        self.gateway = gateway
        self.scope_reviewer = scope_reviewer
        self.known_secret_values = known_secret_values or _no_secret_values
        self.acceptance = acceptance
        self.scanner = SecretScanner()

    def prepare_base_revision(
        self,
        run_id: str,
        expected_base_sha: str,
    ) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", expected_base_sha):
            raise ValueError("expected pull-request base SHA is invalid")
        context = self._context(run_id)
        with self.database.connect() as connection:
            pull = connection.execute(
                """SELECT state FROM pull_requests
                   WHERE run_id=?""",
                (run_id,),
            ).fetchone()
        if pull is None or pull["state"] != "open":
            raise PublicationBlocked(
                "base conflict revision requires the existing open pull request"
            )
        checkout = Path(str(context["checkout_path"]))
        fetched_sha = self.gateway.fetch_intended_base_head(
            checkout,
            str(context["owner"]),
            str(context["name"]),
            str(context["intended_base_branch"]),
        )
        if fetched_sha != expected_base_sha:
            raise PublicationBaseChanged(expected_base_sha, fetched_sha)
        _git(
            checkout,
            (
                "update-ref",
                f"refs/repogents/pull-bases/{fetched_sha}",
                fetched_sha,
            ),
        )
        return fetched_sha

    def publish(self, run_id: str) -> PullRequestInfo | None:
        try:
            context = self._context(run_id)
            if context["state"] != RunState.PUBLISHING.value:
                raise PublicationBlocked(
                    f"run cannot publish from state {context['state']}"
                )
            self._require_publishing(
                run_id,
                validated_sha=str(context.get("validated_sha") or ""),
                issue_version_id=str(context.get("validated_issue_version_id") or ""),
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
            try:
                acceptance = self.acceptance.verify(
                    run_id,
                    validated_sha,
                    changed_files,
                )
            except AcceptanceUnavailable as error:
                raise PublicationBlocked(
                    f"issue acceptance verification unavailable: {error}"
                ) from error
            acceptance_state = acceptance.get("state")
            if acceptance_state == "failed":
                raise PublicationRevisionRequired(
                    "issue acceptance verification failed:\n"
                    + render_acceptance_failure(acceptance)
                )
            if acceptance_state == "blocked":
                raise PublicationBlocked(
                    "issue acceptance verification blocked: "
                    + str(acceptance.get("summary") or "no blocking detail")
                )
            if acceptance_state != "passed":
                raise PublicationBlocked(
                    "issue acceptance verification did not produce a passing report"
                )
            if acceptance.get("issue_version_id") != context.get(
                "validated_issue_version_id"
            ):
                raise PublicationBlocked(
                    "issue acceptance proof does not match the validated issue version"
                )
            self._require_feedback_resolved(run_id, validated_sha)
            proof_body = self._pull_body(context, validated_sha, acceptance)
            branch = f"agent/issue-{context['issue_number']}-{run_id}"
            pull_row = self._stage_pull_request(context, branch, validated_sha)
            self._reconcile_push(context, pull_row, checkout, branch, validated_sha)
            pull = self._reconcile_pull_request(
                context,
                branch,
                validated_sha,
                proof_body,
            )
            self._require_publishing(
                run_id,
                validated_sha=validated_sha,
                issue_version_id=str(context["validated_issue_version_id"]),
            )
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
            sandbox_evidence = json.loads(str(context["sandbox_evidence_json"]))
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
            "issue_version_id": context["validated_issue_version_id"],
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
            "stored_verifier": {
                "runtime": context["verifier_runtime"],
                "model": context["verifier_model"],
                "instructions": context["verifier_instructions"],
            },
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
            raise PublicationBlocked(
                "validated commit does not descend from the stored base SHA"
            )
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
        comparison_base_sha = current_base_sha
        includes_current_base = (
            _git_result(
                checkout,
                (
                    "merge-base",
                    "--is-ancestor",
                    current_base_sha,
                    validated_sha,
                ),
            ).returncode
            == 0
        )
        if not includes_current_base:
            comparison_base_sha = _git(
                checkout,
                ("merge-base", current_base_sha, validated_sha),
            ).strip()
            if not re.fullmatch(r"[0-9a-f]{40}", comparison_base_sha):
                raise PublicationBlocked(
                    "cannot determine the candidate/current-base merge base"
                )
            merge = _git(
                checkout,
                (
                    "merge-tree",
                    comparison_base_sha,
                    current_base_sha,
                    validated_sha,
                ),
            )
            if "<<<<<<< .our" in merge:
                raise PublicationBlocked(
                    "validated commit has a merge conflict with the current "
                    "intended-base head"
                )
        status = _git(checkout, ("status", "--porcelain", "--untracked-files=no"))
        if status.strip():
            raise PublicationBlocked("tracked checkout state changed after validation")
        with self.database.connect() as connection:
            commands = connection.execute(
                """SELECT validation_commands.command_json,
                          validation_results.verdict,
                          validation_baselines.base_sha AS baseline_base_sha,
                          validation_baselines.command_json
                              AS baseline_command_json
                   FROM validation_commands
                   LEFT JOIN validation_results
                     ON validation_results.run_id=?
                    AND validation_results.commit_sha=?
                    AND validation_results.validation_command_id=
                        validation_commands.id
                   LEFT JOIN validation_baselines
                     ON validation_baselines.run_id=?
                    AND validation_baselines.validation_command_id=
                        validation_commands.id
                   WHERE validation_commands.sandbox_version_id=?
                     AND validation_commands.required=1
                   ORDER BY validation_commands.position""",
                (
                    context["id"],
                    validated_sha,
                    context["id"],
                    context["sandbox_version_id"],
                ),
            ).fetchall()
        if not commands or any(
            row["baseline_base_sha"] != context["base_sha"]
            or row["baseline_command_json"] != row["command_json"]
            for row in commands
        ):
            raise PublicationBlocked(
                "every required validation command must have matching "
                "exact-base baseline evidence"
            )
        if not commands or any(row["verdict"] != "pass" for row in commands):
            raise PublicationBlocked(
                "every required validation command must have a passing policy "
                "verdict for the exact published SHA"
            )
        changed_result = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                "--diff-filter=ACMRTD",
                comparison_base_sha,
                validated_sha,
                "--",
            ],
            cwd=checkout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if changed_result.returncode != 0:
            raise PublicationBlocked("changed paths are unavailable for publication scanning")
        if changed_result.stdout and not changed_result.stdout.endswith(b"\0"):
            raise PublicationBlocked("changed path metadata is malformed")
        raw_changed_files = changed_result.stdout.split(b"\0")
        if raw_changed_files and raw_changed_files[-1] == b"":
            raw_changed_files.pop()
        if any(not path for path in raw_changed_files):
            raise PublicationBlocked("changed path metadata is malformed")
        changed_files = tuple(
            path.decode("utf-8", "surrogateescape") for path in raw_changed_files
        )
        if not changed_files:
            raise PublicationBlocked("validated commit contains no issue change")
        forbidden = tuple(path for path in changed_files if _forbidden_artifact(path))
        if forbidden:
            raise PublicationRevisionRequired(
                "forbidden application, credential, or environment artifact in commit: "
                + ", ".join(forbidden)
            )
        diff = _git(
            checkout,
            ("diff", "--binary", comparison_base_sha, validated_sha, "--"),
        )
        findings = self.scanner.scan(diff, self.known_secret_values(str(context["id"])))
        if findings:
            raise PublicationRevisionRequired(
                "potential secret in committed diff: " + ", ".join(findings)
            )
        if self._contains_artifact_content(
            str(context["id"]), diff, committed_diff=True
        ):
            raise PublicationRevisionRequired(
                "committed diff contains uploaded artifact content"
            )
        if self._contains_committed_artifact_blob(
            str(context["id"]), checkout, validated_sha, changed_files
        ):
            raise PublicationRevisionRequired(
                "committed file contains uploaded artifact content"
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
            self._require_publishing(
                str(context["id"]),
                validated_sha=validated_sha,
                issue_version_id=str(context["validated_issue_version_id"]),
                connection=connection,
            )
            connection.execute(
                """INSERT INTO pull_requests
                   (id, run_id, url, branch_name, intended_base_branch,
                    base_sha, validated_head_sha, validated_issue_version_id,
                    state, created_at, updated_at)
                   VALUES (?, ?, '', ?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     validated_head_sha=excluded.validated_head_sha,
                     validated_issue_version_id=excluded.validated_issue_version_id,
                     updated_at=excluded.updated_at""",
                (
                    pull_id,
                    context["id"],
                    branch,
                    context["intended_base_branch"],
                    context["base_sha"],
                    validated_sha,
                    context["validated_issue_version_id"],
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
                    raise PublicationBlocked("run was canceled at publication boundary")
                self._require_publishing(
                    str(context["id"]),
                    validated_sha=validated_sha,
                    issue_version_id=str(context["validated_issue_version_id"]),
                )
                self.gateway.push_branch(
                    checkout,
                    owner,
                    name,
                    branch,
                    validated_sha,
                    None if previous is None else str(previous),
                )
            confirmed = self._confirm_remote(owner, name, branch, validated_sha)
            if not confirmed:
                raise PublicationBlocked(
                    "remote branch head did not confirm the validated SHA"
                )
            self._complete_operation(operation_id, "completed", validated_sha)
        with self.database.transaction() as connection:
            self._require_publishing(
                str(context["id"]),
                connection=connection,
                validated_sha=validated_sha,
                issue_version_id=str(context["validated_issue_version_id"]),
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
        proof_body: str,
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
                    raise PublicationBlocked("run was canceled at publication boundary")
                self._require_publishing(
                    str(context["id"]),
                    validated_sha=validated_sha,
                    issue_version_id=str(context["validated_issue_version_id"]),
                )
                title = f"Resolve #{context['issue_number']}: {' '.join(str(context['issue_title']).split())}"
                body = proof_body
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
        if reconciled:
            update_id = self._stage_operation(
                str(context["id"]),
                "update_pull_request",
                f"{context['id']}:update_pull_request:{validated_sha}",
                {
                    "number": pull.number,
                    "commit_sha": validated_sha,
                    "body_hash": _stable_id(proof_body),
                },
            )
            if not self._operation_completed(update_id):
                with self.lifecycle.external_effect(str(context["id"])) as active:
                    if not active:
                        raise PublicationBlocked(
                            "run was canceled at pull-request proof boundary"
                        )
                    self._require_publishing(
                        str(context["id"]),
                        validated_sha=validated_sha,
                        issue_version_id=str(context["validated_issue_version_id"]),
                    )
                    self.gateway.update_pull_request_body(
                        owner,
                        name,
                        pull.number,
                        proof_body,
                    )
                self._complete_operation(
                    update_id,
                    "completed",
                    pull.node_id,
                )
        now = _utc_now()
        with self.database.transaction() as connection:
            self._require_publishing(
                str(context["id"]),
                connection=connection,
                validated_sha=validated_sha,
                issue_version_id=str(context["validated_issue_version_id"]),
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

    def _contains_artifact_content(
        self, run_id: str, candidate: str, *, committed_diff: bool = False
    ) -> bool:
        """Scan publishable candidate text for uploaded artifact content."""
        import base64

        candidate_bytes = candidate.encode("utf-8", "surrogatepass")
        short_probes: set[bytes] = set()
        window_probes: set[bytes] = set()
        probe_width = 16

        def add_probe(value: bytes) -> None:
            value = value.strip()
            if not value:
                return
            if len(value) < probe_width:
                if len(value) >= 12 and (
                    any(byte < 32 or byte > 126 for byte in value)
                    or len(set(value)) >= 6
                ):
                    short_probes.add(value)
                return
            for offset in range(len(value) - probe_width + 1):
                window_probes.add(value[offset : offset + probe_width])

        # Committed diffs publish only added lines. Pull-request proof bodies are
        # not diffs and must be scanned in full.
        if committed_diff:
            probe_sources = [
                line[1:]
                for line in candidate_bytes.splitlines()
                if line.startswith(b"+") and not line.startswith(b"+++")
            ]
        else:
            probe_sources = [candidate_bytes]
        for source in probe_sources:
            if len(source) <= 5:
                add_probe(source)
            words = list(re.finditer(rb"\S+", source))
            for word in words:
                value = word.group(0)
                if len(value) >= 6 or any(
                    byte < ord("a") or byte > ord("z") for byte in value
                ):
                    add_probe(value)
            for index in range(len(words) - 1):
                phrase = source[words[index].start() : words[index + 1].end()]
                if len(phrase) <= 5:
                    add_probe(phrase)
            for token in re.findall(rb"[A-Za-z0-9_./:+\-=]{3,}", source):
                add_probe(token)
            for phrase in re.findall(rb"(?<!\S)(?:\S\s+)+\S(?!\S)", source):
                add_probe(phrase)
            for encoded in re.findall(
                rb"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2,}(?![0-9A-Fa-f])", source
            ):
                if len(encoded) % 2 == 0:
                    try:
                        add_probe(bytes.fromhex(encoded.decode("ascii")))
                    except ValueError:
                        pass
            for encoded in re.findall(
                rb"(?<![A-Za-z0-9_\-/+])(?:[A-Za-z0-9_\-/+]{4,}={0,2}|[A-Za-z0-9_\-/+]{2,3}={1,2})(?![A-Za-z0-9_\-/+])",
                source,
            ):
                padded = encoded + b"=" * (-len(encoded) % 4)
                for decoder in (base64.b64decode, base64.urlsafe_b64decode):
                    try:
                        add_probe(decoder(padded))
                    except (ValueError, base64.binascii.Error):
                        pass

        if not short_probes and not window_probes:
            return False
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT sandbox_versions.policy_json
                   FROM runs
                   JOIN sandbox_versions
                     ON sandbox_versions.id=runs.sandbox_version_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise PublicationBlocked(
                "run sandbox version is unavailable for artifact scanning"
            )
        payload = json.loads(str(row["policy_json"]))
        bindings = payload.get("artifact_bindings", [])
        if not isinstance(bindings, list):
            raise PublicationBlocked("stored artifact bindings are invalid")
        for binding in bindings:
            if not isinstance(binding, dict):
                raise PublicationBlocked("stored artifact binding is invalid")
            storage_path = binding.get("storage_path")
            if not isinstance(storage_path, str):
                raise PublicationBlocked("stored artifact path is invalid")
            try:
                with Path(storage_path).open("rb") as artifact:
                    overlap = b""
                    while chunk := artifact.read(1024 * 1024):
                        block = overlap + chunk
                        if any(probe in block for probe in short_probes):
                            return True
                        if any(
                            block[offset : offset + probe_width] in window_probes
                            for offset in range(max(0, len(block) - probe_width + 1))
                        ):
                            return True
                        overlap = block[-(probe_width - 1) :]
            except OSError as error:
                name = str(binding.get("name") or storage_path)
                raise PublicationBlocked(
                    f"required artifact {name} is unavailable for publication scanning"
                ) from error
        return False

    def _contains_committed_artifact_blob(
        self,
        run_id: str,
        checkout: Path,
        validated_sha: str,
        changed_files: Sequence[str],
    ) -> bool:
        """Compare changed committed regular-file blobs with pinned artifacts."""
        import hashlib

        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT sandbox_versions.policy_json
                   FROM runs
                   JOIN sandbox_versions
                     ON sandbox_versions.id=runs.sandbox_version_id
                   WHERE runs.id=?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise PublicationBlocked(
                "run sandbox version is unavailable for artifact scanning"
            )
        payload = json.loads(str(row["policy_json"]))
        bindings = payload.get("artifact_bindings", [])
        if not isinstance(bindings, list):
            raise PublicationBlocked("stored artifact bindings are invalid")

        def contains_artifact_bytes(blob: bytes, artifact: bytes) -> bool:
            if not artifact:
                return False
            if artifact in blob:
                return True
            # Binary patches do not expose raw bytes in the textual diff. Scan
            # overlapping, sufficiently distinctive raw windows so embedded or
            # partial binary copies are rejected without reviving arbitrary
            # six-byte source-token false positives. The overlap guarantees any
            # copied run of at least 95 bytes contains a complete 64-byte probe.
            width = 64
            stride = 32
            if len(artifact) < width:
                return False
            offsets = range(0, len(artifact) - width + 1, stride)
            if any(artifact[offset : offset + width] in blob for offset in offsets):
                return True
            final_offset = len(artifact) - width
            return final_offset % stride != 0 and artifact[final_offset:] in blob

        # Keep artifact memory bounded by releasing each pinned artifact before
        # loading the next one. This deliberately trades repeated Git blob reads
        # for a bound independent of the aggregate size of repository artifacts.
        for binding in bindings:
            if not isinstance(binding, dict):
                raise PublicationBlocked("stored artifact binding is invalid")
            storage_path = binding.get("storage_path")
            if not isinstance(storage_path, str):
                raise PublicationBlocked("stored artifact path is invalid")
            name = str(binding.get("name") or storage_path)
            try:
                artifact_bytes = Path(storage_path).read_bytes()
            except OSError as error:
                raise PublicationBlocked(
                    f"required artifact {name} is unavailable for publication scanning"
                ) from error

            for path in changed_files:
                raw_path = path.encode("utf-8", "surrogateescape")
                tree = subprocess.run(
                    [
                        b"git",
                        b"--literal-pathspecs",
                        b"ls-tree",
                        b"-z",
                        validated_sha.encode("ascii"),
                        b"--",
                        raw_path,
                    ],
                    cwd=checkout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if tree.returncode != 0:
                    raise PublicationBlocked(
                        f"committed path {path} is unavailable for artifact scanning"
                    )
                if not tree.stdout:
                    continue  # the raw changed path is genuinely absent at this tree
                if not tree.stdout.endswith(b"\0") or tree.stdout.count(b"\0") != 1:
                    raise PublicationBlocked(
                        f"committed path {path} has invalid Git metadata"
                    )
                header, separator, recorded_path = tree.stdout[:-1].partition(b"\t")
                fields = header.split()
                if not separator or len(fields) != 3 or recorded_path != raw_path:
                    raise PublicationBlocked(
                        f"committed path {path} has invalid Git metadata"
                    )
                mode, object_type, object_id = fields
                if mode not in {b"100644", b"100755"} or object_type != b"blob":
                    continue  # symlink, submodule, or non-regular entry
                blob = subprocess.run(
                    ["git", "cat-file", "blob", object_id.decode("ascii")],
                    cwd=checkout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if blob.returncode != 0:
                    raise PublicationBlocked(
                        f"committed blob {path} is unavailable for artifact scanning"
                    )
                if contains_artifact_bytes(blob.stdout, artifact_bytes):
                    return True
        return False

    def _pull_body(
        self,
        context: dict[str, object],
        validated_sha: str,
        acceptance: dict[str, object],
    ) -> str:
        proof = render_acceptance_markdown(acceptance)
        body = (
            f"Automated implementation for {context['issue_url']}.\n\n"
            f"Closes #{context['issue_number']}\n\n"
            f"Validated issue revision: `{context['validated_issue_version_id']}`.\n\n"
            f"{proof}\n\n"
            "This pull request is intentionally unmerged."
        )
        if validated_sha not in body:
            raise PublicationBlocked(
                "acceptance proof does not identify the validated commit SHA"
            )
        if self._contains_artifact_content(str(context["id"]), body):
            raise PublicationBlocked(
                "pull-request proof contains uploaded artifact content"
            )
        if len(body.encode("utf-8")) > 60_000:
            raise PublicationBlocked(
                "acceptance proof exceeds the pull-request body limit"
            )
        return body

    def _confirm_remote(
        self, owner: str, name: str, branch: str, expected_sha: str
    ) -> bool:
        for attempt in range(5):
            if self.gateway.get_remote_branch_head(owner, name, branch) == expected_sha:
                return True
            if attempt < 4:
                time.sleep(0.2 * (attempt + 1))
        return False

    def _operation_completed(self, operation_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state FROM outbound_operations WHERE id=?",
                (operation_id,),
            ).fetchone()
        return row is not None and row["state"] in {"completed", "reconciled"}

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

    def _require_feedback_resolved(
        self,
        run_id: str,
        validated_sha: str,
    ) -> None:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT feedback_versions.decision_json,
                          feedback_versions.source_sha
                   FROM feedback_versions
                   JOIN pull_requests
                     ON pull_requests.id=feedback_versions.pull_request_id
                   WHERE pull_requests.run_id=?
                     AND feedback_versions.state IN ('pending', 'processing')""",
                (run_id,),
            ).fetchall()
        for row in rows:
            value = row["decision_json"]
            if not value:
                raise PublicationRevisionRequired(
                    "unresolved pull-request feedback exists at publication boundary"
                )
            try:
                action = json.loads(str(value)).get("action")
            except (json.JSONDecodeError, AttributeError):
                action = None
            if action != "revise" or row["source_sha"] != validated_sha:
                raise PublicationRevisionRequired(
                    "unresolved pull-request feedback exists at publication boundary"
                )

    def _require_publishing(
        self,
        run_id: str,
        *,
        validated_sha: str | None = None,
        issue_version_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        query = """SELECT runs.state, runs.validated_sha,
                          runs.validated_issue_version_id,
                          issues.current_version_id
                   FROM runs
                   JOIN issues ON issues.id=runs.issue_id
                   WHERE runs.id=?"""
        if connection is None:
            with self.database.connect() as active_connection:
                row = active_connection.execute(query, (run_id,)).fetchone()
        else:
            row = connection.execute(query, (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        if row["state"] != RunState.PUBLISHING.value:
            raise PublicationBlocked(
                f"run reached durable {row['state']} state at publication boundary"
            )
        if (
            row["validated_issue_version_id"] is None
            or row["validated_issue_version_id"] != row["current_version_id"]
            or (
                issue_version_id is not None
                and row["validated_issue_version_id"] != issue_version_id
            )
        ):
            raise PublicationBlocked(
                "validated issue version is stale at publication boundary"
            )
        if validated_sha is not None and row["validated_sha"] != validated_sha:
            raise PublicationBlocked("validated commit changed at publication boundary")

    def _context(self, run_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT runs.*, repositories.owner, repositories.name,
                          issues.number AS issue_number,
                          issue_versions.title AS issue_title,
                          issue_versions.body AS issue_body,
                          issue_versions.discussion_json,
                          issues.url AS issue_url,
                          issues.current_version_id,
                          sandbox_versions.evidence_json AS sandbox_evidence_json,
                          team_versions.evidence_json AS team_evidence_json,
                          team_members.runtime AS verifier_runtime,
                          team_members.model AS verifier_model,
                          team_members.instructions AS verifier_instructions
                   FROM runs
                   JOIN repositories ON repositories.id=runs.repository_id
                   JOIN issues ON issues.id=runs.issue_id
                   LEFT JOIN issue_versions
                     ON issue_versions.id=runs.validated_issue_version_id
                   JOIN sandbox_versions
                     ON sandbox_versions.id=runs.sandbox_version_id
                   JOIN team_versions
                     ON team_versions.id=runs.team_version_id
                   JOIN team_members
                     ON team_members.team_version_id=runs.team_version_id
                    AND team_members.role='verifier'
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
        raise PublicationBlocked(
            result.stderr.strip() or result.stdout.strip() or "git inspection failed"
        )
    return result.stdout


def _git_result(
    checkout: Path, arguments: Sequence[str]
) -> subprocess.CompletedProcess[str]:
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
