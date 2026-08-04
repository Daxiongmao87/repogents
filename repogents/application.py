from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from repogents.github import GitHubFeedback, PublicationCandidate, PullRequest
from repogents.semantic import SemanticRouter, validate_classification
from repogents.store import TERMINAL_RUN_STATES, Store


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    data_dir: str | Path
    default_similarity_threshold: float = 0.75
    promotion_threshold: int = 3
    stale_run_threshold: int = 3
    max_workers: int = 8
    pr_silence_seconds: float = 3600

    def __post_init__(self) -> None:
        if not 0 <= self.default_similarity_threshold < 1:
            raise ValueError(
                "default_similarity_threshold must be at least 0 and less than 1"
            )
        if self.promotion_threshold <= 0:
            raise ValueError("promotion_threshold must be positive")
        if self.stale_run_threshold <= 0:
            raise ValueError("stale_run_threshold must be positive")
        if self.max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if (
            isinstance(self.pr_silence_seconds, bool)
            or not math.isfinite(self.pr_silence_seconds)
            or self.pr_silence_seconds <= 0
        ):
            raise ValueError("pr_silence_seconds must be positive and finite")


_CLASSIFICATION_GUIDANCE = (
    "Name every classification as action/capability. A classification names a "
    "repository-reusable agent queue, not the current task. The first level "
    "names the concise kind of action the agent performs. The second level "
    "names the broad stable repository capability that distinguishes the "
    "agent. The capability is a stable repository ownership boundary and a "
    "durable area of repository ownership, not the object, technology, or "
    "deliverable mentioned by the task. Prefer the repository subsystem or "
    "professional discipline that owns the work over the behavior or outcome "
    "requested by the issue. Different issue outcomes in the same ownership "
    "boundary should share the capability. Related action levels should use "
    "the same capability when they serve the same repository area and do not "
    "require different specialists. Verification of a change should keep the "
    "changed area's capability unless it requires a genuinely different "
    "specialist. Choose the shortest lowercase label that "
    "still routes work to a meaningfully different suitable agent; use hyphens "
    "only when a level needs multiple words. Do not summarize the issue, work "
    "item, method, artifact, acceptance criterion, or failure instance. "
    "Include such detail only when it would select a meaningfully different "
    "suitable agent than the broader capability. Choose both levels "
    "semantically; no vocabulary or taxonomy is prescribed."
)


_SPECIFY_SCHEMA = {
    "specifications": [
        {
            "key": "string",
            "title": "string",
            "description": "string",
            "acceptance_criteria": ["string"],
            "dependencies": ["specification key"],
            "executable": True,
            "work_items": [
                {
                    "key": "string",
                    "title": "string",
                    "description": "string",
                    "classification": "agent-chosen concise action/capability",
                    "dependencies": ["work item key"],
                }
            ],
        }
    ]
}
_FEEDBACK_SPECIFY_SCHEMA = {
    "dispositions": [
        {
            "external_id": "string",
            "valid": True,
            "in_scope": True,
            "pr_regression": False,
            "explanation": "string",
            "evidence": ["string"],
            "specification_keys": ["specification key"],
            "follow_up_issue": {
                "title": "string",
                "observed_defect": "string",
                "affected_behavior": "string",
                "affected_paths": ["string"],
                "acceptance_criteria": ["string"],
            },
        }
    ],
    "specifications": _SPECIFY_SCHEMA["specifications"],
}
_ROLE_SCHEMA = {"role_prompt": "nonempty string"}
_WORK_SCHEMA = {
    "outcome": "ready_for_validation or continue_work",
    "output": "JSON-safe value",
    "artifacts": [],
    "test_results": [],
    "repository_state": {},
    "resolved_paths": [
        "operation_failure-only source path intentionally ready for controller staging"
    ],
    "classification": "agent-chosen concise action/capability required only for continue_work",
    "context": {},
    "dependencies": [],
    "blocking": None,
}
_VALIDATION_SCHEMA = {
    "passed": True,
    "failed_specifications": [],
    "failed_criteria": [],
    "code_review_findings": [],
    "explanation": "string",
    "evidence": [],
    "repository_state": {},
    "completed_work": [],
}


@dataclass(frozen=True, slots=True)
class _SourceTreeEntry:
    kind: str
    mode: int
    value: str | None


_SOURCE_IMPORT_JOURNAL_VERSION = 1
_SOURCE_IMPORT_SUCCESS_STATES = {"COMPLETED", "HANDED_OFF"}


@dataclass(frozen=True, slots=True)
class _SourceImportJournal:
    path: Path
    backup: Path
    repository_id: int
    run_id: int
    work_id: int

_SOURCE_ACTIVE_RUN_STATES = {
    "SPECIFYING",
    "EXECUTING",
    "WAITING_FOR_WORK_COMPLETION",
    "VALIDATING",
    "CREATING_PR",
}


class Application:
    def __init__(
        self,
        store: Store,
        github,
        runtime,
        router: SemanticRouter,
        config: ApplicationConfig,
        executor=None,
        clock=None,
    ):
        self.store = store
        self.github = github
        self.runtime = runtime
        self.router = router
        self.config = config
        self._clock = clock or time.time
        self.data_dir = Path(config.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._source_lock = threading.RLock()
        self._recover_source_import_journals()
        self.store.recover_interrupted_work()
        self._executor = executor or ThreadPoolExecutor(
            max_workers=config.max_workers,
            thread_name_prefix="repogents-node",
        )
        self._owns_executor = executor is None
        self._workers: dict[int, Future] = {}
        self._worker_lock = threading.Lock()
        self._closed = False

    def add_repository(
        self, github_repository: str, target_branch: str | None = None
    ) -> dict:
        metadata = self.github.repository(github_repository)
        branch = target_branch or metadata["default_branch"]
        return self.store.add_repository(
            github_repository,
            branch,
            self.config.default_similarity_threshold,
        )

    def remove_repository(self, repository_id: int) -> None:
        self.store.remove_repository(repository_id)

    def state(self) -> dict:
        repositories = []
        for repository in self.store.list_repositories():
            projected = dict(repository)
            projected["nodes"] = self.store.list_nodes(repository["id"])
            projected_runs = []
            for run in self.store.list_runs(repository["id"]):
                run_projection = dict(run)
                run_projection["passes"] = self.store.list_passes(run["id"])
                run_projection["specifications"] = self.store.list_specifications(
                    run["id"]
                )
                run_projection["work_items"] = self.store.list_work_items(run["id"])
                run_projection["validations"] = self.store.list_validations(run["id"])
                run_projection["feedback"] = self.store.list_feedback(run["id"])
                projected_runs.append(run_projection)
            projected["runs"] = projected_runs
            repositories.append(projected)
        return {"repositories": repositories}

    def poll_once(self) -> None:
        if self._closed:
            raise RuntimeError("application is closed")
        self._reap_workers()
        repositories = self.store.list_repositories()
        for repository in repositories:
            for issue in self.github.list_ready_issues(
                repository["github_repository"]
            ):
                self.store.create_run(
                    repository["id"],
                    issue.number,
                    {
                        "number": issue.number,
                        "title": issue.title,
                        "body": issue.body,
                        "url": issue.url,
                    },
                )

        repositories_by_id = {item["id"]: item for item in repositories}
        for run in self.store.list_runs():
            repository = repositories_by_id.get(run["repository_id"])
            if repository is None:
                continue
            if run["state"] in TERMINAL_RUN_STATES:
                self.store.adapt_nodes_after_run(
                    run["id"], self.config.stale_run_threshold
                )
            elif run.get("pull_request") is not None:
                self._poll_pull_request(repository, run)

        focused_run_ids: set[int] = set()
        for repository in repositories:
            runs = [
                run
                for run in self.store.list_runs(repository["id"])
                if run["state"] not in TERMINAL_RUN_STATES
            ]
            source_active = [
                run
                for run in runs
                if run["state"] in _SOURCE_ACTIVE_RUN_STATES
            ]
            if source_active:
                focused = source_active[0]
                focused_run_ids.add(focused["id"])
                self._advance_run(repository, focused)
                continue

            pending_feedback = self._pending_feedback_selection(runs)
            if pending_feedback is not None:
                focused, packages = pending_feedback
                if packages is not None:
                    self.store.create_pass(
                        focused["id"],
                        "feedback",
                        {"feedback": packages},
                    )
                self.store.transition_run(
                    focused["id"],
                    "SPECIFYING",
                    branch=focused.get("branch"),
                    pull_request=focused.get("pull_request"),
                )
                focused_run_ids.add(focused["id"])
                continue

            now = self._clock()
            listening_runs = [
                run for run in runs if run["state"] == "PR_LISTENING"
            ]
            if any(
                run.get("pr_listening_since") is None
                or now - float(run["pr_listening_since"])
                < self.config.pr_silence_seconds
                for run in listening_runs
            ):
                continue
            direct_publication_blocked = False
            for listening_run in listening_runs:
                pull_request = listening_run["pull_request"]
                validated_head = pull_request.get("validated_head_sha")
                if (
                    not isinstance(validated_head, str)
                    or not validated_head
                    or pull_request["head_sha"] != validated_head
                ):
                    direct_publication_blocked = True
                    continue
                if not self.github.publish_validated_to_target(
                    repository["github_repository"],
                    repository["target_branch"],
                    self._workspace(repository["id"], listening_run["id"]),
                    validated_head,
                    issue_branch=pull_request["branch"],
                ):
                    direct_publication_blocked = True
                    continue
                self.store.transition_run(listening_run["id"], "COMPLETED")
                self.store.adapt_nodes_after_run(
                    listening_run["id"], self.config.stale_run_threshold
                )

            if direct_publication_blocked:
                continue
            queued = next(
                (run for run in runs if run["state"] == "QUEUED"),
                None,
            )
            if queued is not None:
                focused_run_ids.add(queued["id"])
                self._advance_run(repository, queued)
        self._start_workers(focused_run_ids)

    def _pending_feedback_selection(
        self,
        runs: list[dict],
    ) -> tuple[dict, list[dict] | None] | None:
        for run in runs:
            if run["state"] != "PR_LISTENING":
                continue
            pending = [
                item
                for item in self.store.list_feedback(run["id"])
                if item["status"] == "PENDING"
            ]
            if not pending:
                continue
            claimed_ids = self._claimed_feedback_ids(
                self.store.list_passes(run["id"])
            )
            if any(item["external_id"] in claimed_ids for item in pending):
                return run, None
            packages = [
                item["package"]
                for item in pending
                if item["external_id"] not in claimed_ids
            ]
            if packages:
                return run, packages
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=True)
        self._reap_workers()

    def _workspace(self, repository_id: int, run_id: int) -> Path:
        return self.data_dir / "workspaces" / str(repository_id) / str(run_id)

    @staticmethod
    def _source_copy_ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {".git", ".repogents"}}

    @staticmethod
    def _manifest_path(root: Path, relative_path: str) -> Path:
        return root.joinpath(*PurePosixPath(relative_path).parts)

    @staticmethod
    def _validated_source_link_target(
        relative_path: str,
        target: str,
    ) -> None:
        target_path = PurePosixPath(target)
        if target_path.is_absolute():
            raise ValueError(
                f"source symlink target must be relative: {relative_path}"
            )
        resolved = list(PurePosixPath(relative_path).parent.parts)
        for part in target_path.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    raise ValueError(
                        "source symlink target escapes the source tree: "
                        f"{relative_path}"
                    )
                resolved.pop()
                continue
            if part in {".git", ".repogents"}:
                raise ValueError(
                    "source symlink targets controller metadata: "
                    f"{relative_path}"
                )
            resolved.append(part)

    @classmethod
    def _source_manifest(
        cls,
        root: Path,
        *,
        excluded_roots: set[str] | None = None,
    ) -> dict[str, _SourceTreeEntry]:
        excluded_roots = excluded_roots or set()
        manifest: dict[str, _SourceTreeEntry] = {}

        def visit(directory: Path, parent: PurePosixPath | None = None) -> None:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
            for child in children:
                if child.name in {".git", ".repogents"}:
                    continue
                if parent is None and child.name in excluded_roots:
                    continue
                relative = (
                    PurePosixPath(child.name)
                    if parent is None
                    else parent / child.name
                )
                relative_path = relative.as_posix()
                source_path = directory / child.name
                metadata = child.stat(follow_symlinks=False)
                mode = stat.S_IMODE(metadata.st_mode)
                if stat.S_ISLNK(metadata.st_mode):
                    target = os.readlink(source_path)
                    cls._validated_source_link_target(
                        relative_path,
                        target,
                    )
                    manifest[relative_path] = _SourceTreeEntry(
                        "symlink",
                        mode,
                        target,
                    )
                elif stat.S_ISDIR(metadata.st_mode):
                    manifest[relative_path] = _SourceTreeEntry(
                        "directory",
                        mode,
                        None,
                    )
                    visit(source_path, relative)
                elif stat.S_ISREG(metadata.st_mode):
                    digest = hashlib.sha256()
                    with source_path.open("rb") as source_file:
                        while chunk := source_file.read(1024 * 1024):
                            digest.update(chunk)
                    manifest[relative_path] = _SourceTreeEntry(
                        "file",
                        mode,
                        digest.hexdigest(),
                    )
                else:
                    raise ValueError(
                        f"unsupported source path type: {relative_path}"
                    )

        visit(root)
        return manifest


    @contextmanager
    def _source_snapshot(self, workspace: Path):
        with tempfile.TemporaryDirectory(
            prefix="repogents-source-",
            dir=self.data_dir,
        ) as temporary_directory:
            snapshot = Path(temporary_directory) / "workspace"
            with self._source_lock:
                if workspace.exists():
                    self._source_manifest(workspace)
                if workspace.exists():
                    shutil.copytree(
                        workspace,
                        snapshot,
                        symlinks=True,
                        ignore=self._source_copy_ignore,
                    )
                else:
                    snapshot.mkdir()
            yield snapshot

    @staticmethod
    def _remove_source_path(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
            metadata.st_mode
        ):
            shutil.rmtree(path)
        else:
            path.unlink()

    @staticmethod
    def _source_path_depth(relative_path: str) -> int:
        return len(PurePosixPath(relative_path).parts)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _fsync_source_tree(
        cls,
        root: Path,
        *,
        exclude_controller_metadata: bool = False,
    ) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | no_follow
        )
        file_flags = os.O_RDONLY | no_follow

        def sync_directory(descriptor: int) -> None:
            with os.scandir(descriptor) as scanned:
                entries = list(scanned)
            for entry in entries:
                if (
                    exclude_controller_metadata
                    and entry.name in {".git", ".repogents"}
                ):
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    child_descriptor = os.open(
                        entry.name,
                        directory_flags,
                        dir_fd=descriptor,
                    )
                    try:
                        opened = os.fstat(child_descriptor)
                        if (
                            not stat.S_ISDIR(opened.st_mode)
                            or opened.st_dev != metadata.st_dev
                            or opened.st_ino != metadata.st_ino
                        ):
                            raise RuntimeError(
                                "source directory changed while being flushed"
                            )
                        sync_directory(child_descriptor)
                    finally:
                        os.close(child_descriptor)
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    child_descriptor = os.open(
                        entry.name,
                        file_flags,
                        dir_fd=descriptor,
                    )
                    try:
                        opened = os.fstat(child_descriptor)
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or opened.st_dev != metadata.st_dev
                            or opened.st_ino != metadata.st_ino
                        ):
                            raise RuntimeError(
                                "source file changed while being flushed"
                            )
                        os.fsync(child_descriptor)
                    finally:
                        os.close(child_descriptor)
                    continue
                raise ValueError(
                    f"unsupported source path type while flushing: {entry.name}"
                )
            os.fsync(descriptor)

        root_descriptor = os.open(root, directory_flags)
        try:
            sync_directory(root_descriptor)
        finally:
            os.close(root_descriptor)

    @classmethod
    def _make_tree_removable(cls, root: Path) -> None:
        try:
            metadata = root.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(
            metadata.st_mode
        ):
            return
        os.chmod(
            root,
            stat.S_IMODE(metadata.st_mode)
            | stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR,
        )
        with os.scandir(root) as scanned:
            children = [Path(entry.path) for entry in scanned]
        for child in children:
            cls._make_tree_removable(child)

    def _source_import_journal_root(self) -> Path:
        return self.data_dir / "source-import-journals"

    def _create_source_import_journal(
        self,
        workspace: Path,
        repository_id: int,
        run_id: int,
        work_id: int,
    ) -> _SourceImportJournal:
        root = self._source_import_journal_root()
        root.mkdir(parents=True, exist_ok=True)
        self._fsync_directory(self.data_dir)
        journal_path = root / f"work-{work_id}"
        if os.path.lexists(journal_path):
            raise RuntimeError(
                f"source import recovery journal already exists for work {work_id}"
            )
        staging = Path(
            tempfile.mkdtemp(prefix=".pending-", dir=root)
        )
        try:
            backup = staging / "source"
            if workspace.exists():
                shutil.copytree(
                    workspace,
                    backup,
                    symlinks=True,
                    ignore=self._source_copy_ignore,
                    copy_function=os.link,
                )
            else:
                backup.mkdir()
            self._fsync_source_tree(backup)
            metadata = {
                "version": _SOURCE_IMPORT_JOURNAL_VERSION,
                "repository_id": repository_id,
                "run_id": run_id,
                "work_id": work_id,
            }
            intent = staging / "intent.json"
            with intent.open("x", encoding="utf-8") as intent_file:
                json.dump(
                    metadata,
                    intent_file,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                intent_file.flush()
                os.fsync(intent_file.fileno())
            self._fsync_directory(staging)
            os.replace(staging, journal_path)
            self._fsync_directory(root)
        except BaseException:
            if os.path.lexists(staging):
                self._make_tree_removable(staging)
                shutil.rmtree(staging)
            raise
        return _SourceImportJournal(
            path=journal_path,
            backup=journal_path / "source",
            repository_id=repository_id,
            run_id=run_id,
            work_id=work_id,
        )

    @staticmethod
    def _strict_source_import_id(value: object, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"source import journal {label} must be a positive integer"
            )
        return value

    def _load_source_import_journal(
        self,
        journal_path: Path,
    ) -> _SourceImportJournal:
        try:
            metadata = json.loads(
                (journal_path / "intent.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"invalid source import journal: {journal_path.name}"
            ) from error
        expected_keys = {
            "version",
            "repository_id",
            "run_id",
            "work_id",
        }
        if not isinstance(metadata, dict) or set(metadata) != expected_keys:
            raise ValueError(
                f"invalid source import journal identity: {journal_path.name}"
            )
        if metadata["version"] != _SOURCE_IMPORT_JOURNAL_VERSION:
            raise ValueError(
                f"unsupported source import journal version: {journal_path.name}"
            )
        repository_id = self._strict_source_import_id(
            metadata["repository_id"],
            "repository_id",
        )
        run_id = self._strict_source_import_id(metadata["run_id"], "run_id")
        work_id = self._strict_source_import_id(
            metadata["work_id"],
            "work_id",
        )
        if journal_path.name != f"work-{work_id}":
            raise ValueError(
                f"source import journal path does not match work {work_id}"
            )
        backup = journal_path / "source"
        try:
            backup_metadata = backup.lstat()
        except FileNotFoundError as error:
            raise ValueError(
                f"source import journal has no backup: {journal_path.name}"
            ) from error
        if not stat.S_ISDIR(backup_metadata.st_mode) or stat.S_ISLNK(
            backup_metadata.st_mode
        ):
            raise ValueError(
                f"source import journal backup is not a directory: {journal_path.name}"
            )
        return _SourceImportJournal(
            path=journal_path,
            backup=backup,
            repository_id=repository_id,
            run_id=run_id,
            work_id=work_id,
        )

    def _source_import_work_state(
        self,
        journal: _SourceImportJournal,
    ) -> str:
        repository = self.store.get_repository(journal.repository_id)
        run = self.store.get_run(journal.run_id)
        if (
            repository is None
            or run is None
            or run["repository_id"] != journal.repository_id
        ):
            raise ValueError(
                "source import journal repository/run identity mismatch: "
                f"work {journal.work_id}"
            )
        work = next(
            (
                item
                for item in self.store.list_work_items(journal.run_id)
                if item["id"] == journal.work_id
            ),
            None,
        )
        if work is None or work["run_id"] != journal.run_id:
            raise ValueError(
                "source import journal work identity mismatch: "
                f"work {journal.work_id}"
            )
        return cast(str, work["state"])

    def _restore_source_import_journal(
        self,
        journal: _SourceImportJournal,
    ) -> None:
        workspace = self._workspace(
            journal.repository_id,
            journal.run_id,
        )
        workspace.mkdir(parents=True, exist_ok=True)
        current = self._source_manifest(workspace)
        desired = self._source_manifest(journal.backup)
        self._import_source_delta(
            workspace,
            journal.backup,
            current,
            desired,
        )
        self._fsync_source_tree(
            workspace,
            exclude_controller_metadata=True,
        )

    def _discard_source_import_journal(
        self,
        journal_path: Path,
    ) -> None:
        self._make_tree_removable(journal_path)
        shutil.rmtree(journal_path)
        self._fsync_directory(self._source_import_journal_root())

    def _recover_source_import_journals(self) -> None:
        root = self._source_import_journal_root()
        if not root.exists():
            return
        with self._source_lock:
            with os.scandir(root) as scanned:
                paths = sorted(
                    (Path(entry.path) for entry in scanned),
                    key=lambda path: path.name,
                )
            for path in paths:
                if path.name.startswith(".pending-"):
                    self._make_tree_removable(path)
                    shutil.rmtree(path)
                    self._fsync_directory(root)
                    continue
                journal = self._load_source_import_journal(path)
                state = self._source_import_work_state(journal)
                if state not in _SOURCE_IMPORT_SUCCESS_STATES:
                    self._restore_source_import_journal(journal)
                self._discard_source_import_journal(path)

    @staticmethod
    def _atomic_replace_source_file(
        source: Path,
        destination: Path,
        mode: int,
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".repogents-import-",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                descriptor = -1
                with source.open("rb") as source_file:
                    shutil.copyfileobj(
                        source_file,
                        temporary_file,
                        length=1024 * 1024,
                    )
                temporary_file.flush()
                os.fchmod(temporary_file.fileno(), mode)
                os.fsync(temporary_file.fileno())
            os.replace(temporary, destination)
            Application._fsync_directory(destination.parent)
        finally:
            if os.path.lexists(temporary):
                temporary.unlink()

    @contextmanager
    def _durable_source_import(
        self,
        workspace: Path,
        desired_root: Path,
        baseline: dict[str, _SourceTreeEntry],
        desired: dict[str, _SourceTreeEntry],
        *,
        repository_id: int,
        run_id: int,
        work_id: int,
    ):
        changed_paths = {
            path
            for path in baseline.keys() | desired.keys()
            if baseline.get(path) != desired.get(path)
        }
        with self._source_lock:
            journal = None
            if changed_paths:
                self._source_manifest(workspace)
                journal = self._create_source_import_journal(
                    workspace,
                    repository_id,
                    run_id,
                    work_id,
                )
            try:
                applied_paths = self._import_source_delta(
                    workspace,
                    desired_root,
                    baseline,
                    desired,
                )
                if journal is not None:
                    self._fsync_source_tree(
                        workspace,
                        exclude_controller_metadata=True,
                    )
                yield applied_paths
            except BaseException:
                if journal is None:
                    raise
                state = self._source_import_work_state(journal)
                if state in _SOURCE_IMPORT_SUCCESS_STATES:
                    self._discard_source_import_journal(journal.path)
                    return
                self._restore_source_import_journal(journal)
                self._discard_source_import_journal(journal.path)
                raise
            else:
                if journal is not None:
                    self._discard_source_import_journal(journal.path)

    def _import_source_delta(
        self,
        workspace: Path,
        desired_root: Path,
        baseline: dict[str, _SourceTreeEntry],
        desired: dict[str, _SourceTreeEntry],
    ) -> list[str]:
        changed_paths = {
            path
            for path in baseline.keys() | desired.keys()
            if baseline.get(path) != desired.get(path)
        }
        if not changed_paths:
            return []

        with self._source_lock:
            current = self._source_manifest(workspace)
            checked_paths = set(changed_paths)
            for relative_path in changed_paths:
                for parent in PurePosixPath(relative_path).parents:
                    if parent != PurePosixPath("."):
                        checked_paths.add(parent.as_posix())
                baseline_entry = baseline.get(relative_path)
                desired_entry = desired.get(relative_path)
                if (
                    baseline_entry is not None
                    and baseline_entry.kind == "directory"
                    and (
                        desired_entry is None
                        or desired_entry.kind != "directory"
                    )
                ):
                    prefix = relative_path + "/"
                    checked_paths.update(
                        path for path in current if path.startswith(prefix)
                    )

            for relative_path in sorted(checked_paths):
                current_entry = current.get(relative_path)
                if current_entry not in {
                    baseline.get(relative_path),
                    desired.get(relative_path),
                }:
                    raise ValueError(
                        "stale overlapping source path: "
                        f"{relative_path}"
                    )

            paths_to_apply = {
                path
                for path in changed_paths
                if current.get(path) != desired.get(path)
            }
            if not paths_to_apply:
                return sorted(changed_paths)

            permission_directories: set[str] = set()
            for relative_path in paths_to_apply:
                for parent in PurePosixPath(relative_path).parents:
                    if parent == PurePosixPath("."):
                        continue
                    parent_path = parent.as_posix()
                    parent_entry = current.get(parent_path)
                    if (
                        parent_entry is not None
                        and parent_entry.kind == "directory"
                    ):
                        permission_directories.add(parent_path)
                current_entry = current.get(relative_path)
                if (
                    current_entry is not None
                    and current_entry.kind == "directory"
                ):
                    permission_directories.add(relative_path)

            for relative_path in sorted(
                permission_directories,
                key=self._source_path_depth,
            ):
                directory = self._manifest_path(workspace, relative_path)
                try:
                    metadata = directory.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    os.chmod(
                        directory,
                        stat.S_IMODE(metadata.st_mode)
                        | stat.S_IRUSR
                        | stat.S_IWUSR
                        | stat.S_IXUSR,
                    )

            desired_directories = [
                path
                for path in paths_to_apply
                if desired.get(path) is not None
                and cast(_SourceTreeEntry, desired[path]).kind
                == "directory"
            ]
            try:
                for relative_path in sorted(
                    paths_to_apply,
                    key=self._source_path_depth,
                    reverse=True,
                ):
                    current_entry = current.get(relative_path)
                    desired_entry = desired.get(relative_path)
                    if current_entry is None:
                        continue
                    if (
                        desired_entry is None
                        or current_entry.kind != desired_entry.kind
                        or (
                            current_entry.kind == "symlink"
                            and current_entry != desired_entry
                        )
                    ):
                        self._remove_source_path(
                            self._manifest_path(workspace, relative_path)
                        )

                for relative_path in sorted(
                    desired_directories,
                    key=self._source_path_depth,
                ):
                    directory = self._manifest_path(workspace, relative_path)
                    if not os.path.lexists(directory):
                        directory.mkdir()

                for relative_path in sorted(
                    paths_to_apply,
                    key=self._source_path_depth,
                ):
                    desired_entry = desired.get(relative_path)
                    if desired_entry is None:
                        continue
                    destination = self._manifest_path(
                        workspace,
                        relative_path,
                    )
                    if desired_entry.kind == "file":
                        self._atomic_replace_source_file(
                            self._manifest_path(
                                desired_root,
                                relative_path,
                            ),
                            destination,
                            desired_entry.mode,
                        )
                    elif desired_entry.kind == "symlink":
                        if not os.path.lexists(destination):
                            os.symlink(
                                cast(str, desired_entry.value),
                                destination,
                            )
            finally:
                directories_to_restore = permission_directories | set(
                    desired_directories
                )
                for relative_path in sorted(
                    directories_to_restore,
                    key=self._source_path_depth,
                    reverse=True,
                ):
                    desired_entry = desired.get(relative_path)
                    if (
                        desired_entry is None
                        or desired_entry.kind != "directory"
                    ):
                        continue
                    directory = self._manifest_path(
                        workspace,
                        relative_path,
                    )
                    try:
                        metadata = directory.lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        os.chmod(directory, desired_entry.mode)

        return sorted(changed_paths)

    @staticmethod
    def _validated_relative_path(value: object, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a nonempty relative path")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
            or any(part in {".git", ".repogents"} for part in path.parts)
        ):
            raise ValueError(f"{label} must be a normalized source path")
        return value

    @classmethod
    def _validated_operation_path_list(
        cls,
        operation_state: dict,
        field: str,
        *,
        allow_missing: bool = False,
    ) -> list[str]:
        if field not in operation_state and allow_missing:
            return []
        values = operation_state.get(field)
        label = field.replace("_", " ")
        if (
            not isinstance(values, list)
            or any(
                not isinstance(path, str) or not path
                for path in values
            )
            or values != sorted(set(values))
        ):
            raise ValueError(
                f"repository operation {label} must be sorted source paths"
            )
        for path in values:
            cls._validated_relative_path(
                path,
                f"repository operation {label.removesuffix('s')}",
            )
        return list(values)

    @classmethod
    def _validated_operation_artifact_manifest(
        cls,
        manifest: object,
        destination: Path,
    ) -> dict[str, dict[str, str]]:
        if not isinstance(manifest, dict):
            raise ValueError(
                "repository operation artifact manifest must be an object"
            )
        normalized: dict[str, dict[str, str]] = {}
        for semantic_path, artifacts in manifest.items():
            semantic_path = cls._validated_relative_path(
                semantic_path,
                "repository operation semantic path",
            )
            if not isinstance(artifacts, dict):
                raise ValueError(
                    "repository operation path artifacts must be an object"
                )
            unexpected_stages = set(artifacts) - {
                "base",
                "ours",
                "theirs",
            }
            if unexpected_stages:
                raise ValueError(
                    "repository operation artifact stage is invalid"
                )
            normalized_artifacts: dict[str, str] = {}
            for stage in ("base", "ours", "theirs"):
                if stage not in artifacts:
                    continue
                relative_path = cls._validated_relative_path(
                    artifacts[stage],
                    f"repository operation {stage} artifact",
                )
                artifact_path = cls._manifest_path(
                    destination,
                    relative_path,
                )
                try:
                    metadata = artifact_path.lstat()
                except FileNotFoundError as error:
                    raise ValueError(
                        "repository operation artifact is missing: "
                        f"{relative_path}"
                    ) from error
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(
                        "repository operation artifact must be a regular file: "
                        f"{relative_path}"
                    )
                normalized_artifacts[stage] = relative_path
            normalized[semantic_path] = normalized_artifacts
        return normalized

    def _operation_artifacts_directory(
        self,
        run_id: int,
        trigger: dict,
    ) -> Path:
        failed_pass_id = trigger.get("failed_pass_id")
        failed_stage = trigger.get("failed_stage")
        if (
            isinstance(failed_pass_id, bool)
            or not isinstance(failed_pass_id, int)
            or failed_stage
            not in {
                "continue_repository_operation",
                "prepare_publication",
            }
        ):
            raise ValueError("operation failure artifact identity is invalid")
        return (
            self.data_dir
            / "operation-artifacts"
            / str(run_id)
            / f"{failed_pass_id}-{failed_stage}"
        )

    def _export_operation_artifacts(
        self,
        run_id: int,
        workspace: Path,
        trigger: dict,
    ) -> None:
        destination = self._operation_artifacts_directory(run_id, trigger)
        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        self._fsync_directory(parent)
        self._fsync_directory(parent.parent)
        self._fsync_directory(self.data_dir)

        def validate_exact_tree(
            root: Path,
            manifest: dict[str, dict[str, str]],
            *,
            includes_manifest: bool,
        ) -> None:
            try:
                root_metadata = root.lstat()
            except FileNotFoundError as error:
                raise ValueError(
                    "repository operation artifact tree is unavailable"
                ) from error
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise ValueError(
                    "repository operation artifact tree must be a directory"
                )

            expected_files = {
                relative_path
                for artifacts in manifest.values()
                for relative_path in artifacts.values()
            }
            if ".manifest.json" in expected_files:
                raise ValueError(
                    "repository operation artifact conflicts with its manifest"
                )
            if includes_manifest:
                expected_files.add(".manifest.json")
            expected_directories: set[str] = set()
            for relative_path in expected_files:
                for ancestor in PurePosixPath(relative_path).parents:
                    if ancestor != PurePosixPath("."):
                        expected_directories.add(ancestor.as_posix())

            actual_files: set[str] = set()
            actual_directories: set[str] = set()

            def visit(
                directory: Path,
                parent_path: PurePosixPath | None = None,
            ) -> None:
                with os.scandir(directory) as scanned:
                    entries = list(scanned)
                for entry in entries:
                    relative = (
                        PurePosixPath(entry.name)
                        if parent_path is None
                        else parent_path / entry.name
                    )
                    relative_path = relative.as_posix()
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        actual_directories.add(relative_path)
                        visit(Path(entry.path), relative)
                    elif stat.S_ISREG(metadata.st_mode):
                        actual_files.add(relative_path)
                    else:
                        raise ValueError(
                            "repository operation artifact tree contains "
                            f"an unsupported path: {relative_path}"
                        )

            visit(root)
            if (
                actual_files != expected_files
                or actual_directories != expected_directories
            ):
                raise ValueError(
                    "repository operation artifact manifest does not exactly "
                    "describe its tree"
                )

        def validate_published_tree(
            root: Path,
        ) -> dict[str, dict[str, str]]:
            manifest_path = root / ".manifest.json"
            try:
                metadata = manifest_path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(
                        "repository operation artifact manifest must be a "
                        "regular file"
                    )
                manifest_bytes = manifest_path.read_bytes()
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(
                    "repository operation artifacts are unavailable"
                ) from error
            normalized = self._validated_operation_artifact_manifest(
                manifest,
                root,
            )
            expected_manifest = json.dumps(
                normalized,
                sort_keys=True,
            ).encode("utf-8")
            if manifest_bytes != expected_manifest:
                raise ValueError(
                    "repository operation artifact manifest is not exact"
                )
            validate_exact_tree(
                root,
                normalized,
                includes_manifest=True,
            )
            return normalized

        if os.path.lexists(destination):
            validate_published_tree(destination)
            self._fsync_source_tree(destination)
            self._fsync_directory(parent)
            return

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.pending-",
                dir=parent,
            )
        )
        try:
            manifest = self.github.export_repository_operation_artifacts(
                workspace,
                staging,
            )
            normalized = self._validated_operation_artifact_manifest(
                manifest,
                staging,
            )
            validate_exact_tree(
                staging,
                normalized,
                includes_manifest=False,
            )
            manifest_bytes = json.dumps(
                normalized,
                sort_keys=True,
            ).encode("utf-8")
            with (staging / ".manifest.json").open("xb") as manifest_file:
                manifest_file.write(manifest_bytes)
                manifest_file.flush()
                os.fsync(manifest_file.fileno())
            validate_published_tree(staging)
            self._fsync_source_tree(staging)
            os.replace(staging, destination)
            self._fsync_directory(parent)
        except Exception:
            if os.path.lexists(staging):
                self._make_tree_removable(staging)
                shutil.rmtree(staging)
            raise

    def _operation_artifact_manifest(
        self,
        run_id: int,
        trigger: dict,
    ) -> tuple[Path, dict[str, dict[str, str]]]:
        destination = self._operation_artifacts_directory(run_id, trigger)
        manifest_path = destination / ".manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise ValueError(
                "repository operation artifacts are unavailable"
            ) from error
        return destination, self._validated_operation_artifact_manifest(
            manifest,
            destination,
        )

    def _materialize_operation_artifacts(
        self,
        run_id: int,
        execution_pass: dict,
        snapshot: Path,
    ) -> tuple[dict[str, dict[str, str]], str]:
        source, manifest = self._operation_artifact_manifest(
            run_id,
            execution_pass["trigger_json"],
        )
        root_name = ".repogents-operation-artifacts"
        suffix = 0
        while os.path.lexists(snapshot / root_name):
            suffix += 1
            root_name = f".repogents-operation-artifacts-{suffix}"
        artifact_root = snapshot / root_name
        artifact_root.mkdir()
        materialized: dict[str, dict[str, str]] = {}
        for semantic_path, artifacts in manifest.items():
            materialized_artifacts: dict[str, str] = {}
            for stage, relative_path in artifacts.items():
                source_path = self._manifest_path(source, relative_path)
                destination_path = self._manifest_path(
                    artifact_root,
                    relative_path,
                )
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(
                    source_path,
                    destination_path,
                    follow_symlinks=False,
                )
                materialized_artifacts[stage] = (
                    PurePosixPath(root_name) / relative_path
                ).as_posix()
            materialized[semantic_path] = materialized_artifacts
        return materialized, root_name

    def _record_operation_failure(
        self,
        repository: dict,
        run: dict,
        failed_pass: dict,
        failed_stage: str,
        error: subprocess.CalledProcessError,
    ) -> None:
        workspace = self._workspace(repository["id"], run["id"])
        operation_state = self.github.repository_operation_state(workspace)
        if not isinstance(operation_state, dict):
            raise ValueError("repository operation state must be an object")
        rebase_in_progress = operation_state.get("rebase_in_progress")
        if not isinstance(rebase_in_progress, bool):
            raise ValueError(
                "repository operation rebase state must be boolean"
            )
        unmerged_paths = self._validated_operation_path_list(
            operation_state,
            "unmerged_paths",
        )
        staged_paths = self._validated_operation_path_list(
            operation_state,
            "staged_paths",
        )
        unstaged_paths = self._validated_operation_path_list(
            operation_state,
            "unstaged_paths",
        )
        untracked_paths = self._validated_operation_path_list(
            operation_state,
            "untracked_paths",
        )
        trigger = {
            "failed_stage": failed_stage,
            "command": error.cmd,
            "returncode": error.returncode,
            "stdout": error.stdout,
            "stderr": error.stderr,
            "target_branch": repository["target_branch"],
            "failed_pass_id": failed_pass["id"],
            "workspace": {
                "repository_id": repository["id"],
                "run_id": run["id"],
                "rebase_in_progress": rebase_in_progress,
                "unmerged_paths": unmerged_paths,
                "staged_paths": staged_paths,
                "unstaged_paths": unstaged_paths,
                "untracked_paths": untracked_paths,
            },
        }
        origin_feedback_pass_id = self._feedback_origin_pass_id(failed_pass)
        if origin_feedback_pass_id is not None:
            trigger["origin_feedback_pass_id"] = origin_feedback_pass_id
        self._export_operation_artifacts(
            run["id"],
            workspace,
            trigger,
        )
        self.store.create_pass(
            run["id"],
            "operation_failure",
            trigger,
        )
        self.store.transition_run(run["id"], "SPECIFYING")

    def _trajectory(self, run_id: int, name: str) -> Path:
        return self.data_dir / "trajectories" / str(run_id) / f"{name}.json"

    @staticmethod
    def _task(kind: str, instruction: str, context: dict) -> str:
        return json.dumps(
            {"kind": kind, "instruction": instruction, "context": context},
            sort_keys=True,
        )

    def _advance_run(self, repository: dict, run: dict) -> None:
        state = run["state"]
        if state == "QUEUED":
            self._begin_run(repository, run)
        elif state == "SPECIFYING":
            self._specify(repository, run)
        elif state == "EXECUTING":
            self.store.transition_run(run["id"], "WAITING_FOR_WORK_COMPLETION")
        elif state == "WAITING_FOR_WORK_COMPLETION":
            self._wait_for_work(repository, run)
        elif state == "VALIDATING":
            self._validate(repository, run)
        elif state == "CREATING_PR":
            self._publish(repository, run)

    def _begin_run(self, repository: dict, run: dict) -> None:
        workspace = self._workspace(repository["id"], run["id"])
        self.github.checkout(
            repository["github_repository"], repository["target_branch"], workspace
        )
        if not self.store.list_passes(run["id"]):
            self.store.create_pass(run["id"], "issue", run["issue_json"])
        self.store.transition_run(run["id"], "SPECIFYING")

    @staticmethod
    def _feedback_origin_pass_id(execution_pass: dict) -> int | None:
        if execution_pass["trigger_type"] == "feedback":
            return execution_pass["id"]
        if execution_pass["trigger_type"] not in {
            "operation_failure",
            "publication_revalidation",
            "validation_failure",
        }:
            return None
        origin = execution_pass["trigger_json"].get("origin_feedback_pass_id")
        if isinstance(origin, bool) or not isinstance(origin, int):
            return None
        return origin

    def _pass_feedback_context(
        self,
        run_id: int,
        execution_pass: dict,
        *,
        in_scope_only: bool = True,
    ) -> tuple[list[dict], str | None]:
        origin_pass_id = self._feedback_origin_pass_id(execution_pass)
        if origin_pass_id is None:
            return [], None
        origin_pass = next(
            (
                item
                for item in self.store.list_passes(run_id)
                if item["id"] == origin_pass_id
                and item["trigger_type"] == "feedback"
            ),
            None,
        )
        if origin_pass is None:
            return [], None
        packages = origin_pass["trigger_json"].get("feedback", [])
        if not isinstance(packages, list):
            return [], None
        run = self.store.get_run(run_id)
        reference = None if run is None else run.get("pull_request")
        pull_request_diff = (
            reference.get("diff")
            if isinstance(reference, dict)
            else None
        )
        allowed_ids: set[str] | None = None
        if in_scope_only:
            scope_result = self.store.get_feedback_scope_result(
                run_id,
                origin_pass_id,
            )
            allowed_ids = {
                item["external_id"]
                for item in (scope_result or {}).get("dispositions", [])
                if isinstance(item, dict)
                and item.get("valid") is True
                and item.get("in_scope") is True
                and isinstance(item.get("external_id"), str)
            }
        feedback = [
            {
                field: package.get(field)
                for field in (
                    "external_id",
                    "kind",
                    "body",
                    "path",
                    "line",
                    "review_thread_id",
                    "top_level_comment_id",
                )
            }
            for package in packages
            if isinstance(package, dict)
            and (
                allowed_ids is None
                or package.get("external_id") in allowed_ids
            )
        ]
        return feedback, pull_request_diff

    @staticmethod
    def _claimed_feedback_ids(execution_passes: list[dict]) -> set[str]:
        claimed_feedback_ids = set()
        for execution_pass in execution_passes:
            if execution_pass["trigger_type"] != "feedback":
                continue
            trigger_feedback = execution_pass["trigger_json"].get("feedback", [])
            if not isinstance(trigger_feedback, list):
                continue
            claimed_feedback_ids.update(
                item["external_id"]
                for item in trigger_feedback
                if isinstance(item, dict)
                and isinstance(item.get("external_id"), str)
            )
        return claimed_feedback_ids

    @staticmethod
    def _specification_definition(specification: dict) -> dict:
        return {
            field: specification[field]
            for field in (
                "key",
                "title",
                "description",
                "acceptance_criteria",
                "dependencies",
                "executable",
            )
        }

    @staticmethod
    def _work_identity_and_outcome(work: dict) -> dict:
        return {
            field: work[field]
            for field in (
                "key",
                "classification",
                "dependencies",
                "state",
            )
        }

    @staticmethod
    def _validation_evidence(validation: dict) -> dict:
        result = validation.get("result")
        if not isinstance(result, dict):
            result = {}
        return {
            "pass_id": validation["pass_id"],
            "result": {
                field: result.get(field)
                for field in (
                    "passed",
                    "failed_specifications",
                    "failed_criteria",
                    "code_review_findings",
                    "explanation",
                    "evidence",
                )
            },
        }

    @classmethod
    def _specification_dependency_closure(
        cls,
        specifications: list[dict],
        specification: dict,
    ) -> list[dict]:
        specifications_by_key = {
            item["key"]: item for item in specifications
        }
        dependency_keys: set[str] = set()
        pending = list(specification["dependencies"])
        while pending:
            dependency_key = pending.pop()
            if dependency_key in dependency_keys:
                continue
            dependency_keys.add(dependency_key)
            dependency = specifications_by_key.get(dependency_key)
            if dependency is not None:
                pending.extend(dependency["dependencies"])
        return [
            cls._specification_definition(item)
            for item in specifications
            if item["key"] in dependency_keys
        ]

    def _specify_context(
        self,
        repository: dict,
        run: dict,
        execution_pass: dict,
        *,
        in_scope_only: bool = True,
    ) -> dict:
        validations = self.store.list_validations(run["id"])
        feedback, pull_request_diff = self._pass_feedback_context(
            run["id"],
            execution_pass,
            in_scope_only=in_scope_only,
        )
        reference = run.get("pull_request")
        pull_request_head_sha = (
            reference.get("head_sha")
            if isinstance(reference, dict)
            else None
        )
        context = {
            "original_issue": run["issue_json"],
            "repository": repository,
            "feedback": feedback,
            "pull_request_diff": pull_request_diff,
            "pull_request_head_sha": pull_request_head_sha,
            "existing_specifications": [
                self._specification_definition(item)
                for item in self.store.list_specifications(run["id"])
            ],
            "existing_work": [
                self._work_identity_and_outcome(item)
                for item in self.store.list_work_items(run["id"])
            ],
            "relevant_validation": (
                self._validation_evidence(validations[-1])
                if validations
                else None
            ),
        }
        if execution_pass["trigger_type"] == "operation_failure":
            context["operation_failure"] = execution_pass["trigger_json"]
        return context

    @staticmethod
    def _specify_instruction() -> str:
        return (
            "Convert only the issue, controller operation failure, validation "
            "deficiency, or feedback in context into atomic specifications, "
            "acceptance criteria, classified work items, and dependencies. "
            + _CLASSIFICATION_GUIDANCE
        )

    def _specify(self, repository: dict, run: dict) -> None:
        execution_passes = self.store.list_passes(run["id"])
        if not execution_passes:
            raise RuntimeError("specifying run has no execution pass")
        execution_pass = execution_passes[-1]
        existing = [
            item
            for item in self.store.list_specifications(run["id"])
            if item["pass_id"] == execution_pass["id"]
        ]
        if execution_pass["trigger_type"] == "feedback":
            packages = execution_pass["trigger_json"].get("feedback", [])
            result = self.store.get_feedback_scope_result(
                run["id"],
                execution_pass["id"],
            )
            reference = run.get("pull_request")
            head_sha = (
                reference.get("head_sha")
                if isinstance(reference, dict)
                else None
            )
            if not isinstance(head_sha, str) or not head_sha:
                raise RuntimeError(
                    "feedback Specify run has no current pull-request head"
                )
            if result is None:
                with self._source_snapshot(
                    self._workspace(repository["id"], run["id"])
                ) as source_workspace:
                    result = self.runtime.run(
                        self._task(
                            "specify",
                            "Disposition every claimed feedback item against the current pull-request head, original issue, accepted specifications, prior work, and validation evidence before creating correction work. A pull-request regression must be valid and in scope. Map only valid in-scope items to returned specifications. Give valid out-of-scope items a bounded follow-up issue and give invalid items no follow-up issue. "
                            + _CLASSIFICATION_GUIDANCE,
                            self._specify_context(
                                repository,
                                run,
                                execution_pass,
                                in_scope_only=False,
                            ),
                        ),
                        source_workspace,
                        result_schema=_FEEDBACK_SPECIFY_SCHEMA,
                        trajectory_path=self._trajectory(
                            run["id"], f"specify-{execution_pass['id']}"
                        ),
                    )
                result = self._validated_feedback_scope_result(
                    result,
                    packages,
                )
                result = dict(result)
                result["head_sha"] = head_sha
                result = self.store.record_feedback_scope_result(
                    run["id"],
                    execution_pass["id"],
                    result,
                )
            else:
                result = self._validated_feedback_scope_result(
                    result,
                    packages,
                )
                if result.get("head_sha") != head_sha:
                    raise RuntimeError(
                        "feedback scope result belongs to a different "
                        "pull-request head"
                    )
            for disposition in result["dispositions"]:
                self.store.record_feedback_disposition(
                    run["id"],
                    disposition["external_id"],
                    self._feedback_disposition_name(disposition),
                    disposition,
                )
            if result["specifications"] and not existing:
                self.store.save_specification_package(
                    run["id"],
                    execution_pass["id"],
                    {"specifications": result["specifications"]},
                )
            self._resolve_no_code_feedback(
                repository,
                run,
                result["dispositions"],
            )
            if not result["specifications"]:
                reference = run.get("pull_request")
                if not reference:
                    raise RuntimeError(
                        "feedback disposition run has no pull request"
                    )
                self.store.transition_run(
                    run["id"],
                    "PR_LISTENING",
                    branch=reference["branch"],
                    pull_request=reference,
                    pr_listening_since=self._clock(),
                )
                return
        elif not existing:
            with self._source_snapshot(
                self._workspace(repository["id"], run["id"])
            ) as source_workspace:
                result = self.runtime.run(
                    self._task(
                        "specify",
                        self._specify_instruction(),
                        self._specify_context(repository, run, execution_pass),
                    ),
                    source_workspace,
                    result_schema=_SPECIFY_SCHEMA,
                    trajectory_path=self._trajectory(
                        run["id"], f"specify-{execution_pass['id']}"
                    ),
                )
            self.store.save_specification_package(
                run["id"], execution_pass["id"], result
            )
        self._route_unassigned(repository, run, execution_pass)
        self.store.transition_run(run["id"], "EXECUTING")
        self.store.transition_run(run["id"], "WAITING_FOR_WORK_COMPLETION")

    @staticmethod
    def _validated_feedback_scope_result(
        result: dict,
        packages: list[dict],
    ) -> dict:
        if (
            not isinstance(result, dict)
            or not {"dispositions", "specifications"}.issubset(result)
        ):
            raise ValueError(
                "feedback Specify must return dispositions and specifications"
            )
        dispositions = result["dispositions"]
        specifications = result["specifications"]
        if not isinstance(dispositions, list):
            raise ValueError("feedback dispositions must be a list")
        if not isinstance(specifications, list):
            raise ValueError("feedback specifications must be a list")
        if not isinstance(packages, list) or any(
            not isinstance(package, dict)
            or not isinstance(package.get("external_id"), str)
            or not package["external_id"].strip()
            for package in packages
        ):
            raise ValueError("feedback pass must contain claimed feedback items")
        claimed_ids = [package["external_id"] for package in packages]
        if len(claimed_ids) != len(set(claimed_ids)):
            raise ValueError("feedback pass external IDs must be unique")

        required_disposition_fields = {
            "external_id",
            "valid",
            "in_scope",
            "pr_regression",
            "explanation",
            "evidence",
            "specification_keys",
            "follow_up_issue",
        }
        seen_ids: set[str] = set()
        mapped_specification_keys: set[str] = set()
        for disposition in dispositions:
            if (
                not isinstance(disposition, dict)
                or not required_disposition_fields.issubset(disposition)
            ):
                raise ValueError(
                    "feedback disposition must contain every required field"
                )
            external_id = disposition["external_id"]
            if (
                not isinstance(external_id, str)
                or not external_id.strip()
                or external_id in seen_ids
            ):
                raise ValueError(
                    "feedback disposition external IDs must be nonempty and unique"
                )
            seen_ids.add(external_id)
            for field in ("valid", "in_scope", "pr_regression"):
                if not isinstance(disposition[field], bool):
                    raise ValueError(
                        f"feedback disposition {field} must be boolean"
                    )
            explanation = disposition["explanation"]
            if not isinstance(explanation, str) or not explanation.strip():
                raise ValueError(
                    "feedback disposition explanation must be nonempty"
                )
            evidence = disposition["evidence"]
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in evidence
                )
            ):
                raise ValueError(
                    "feedback disposition evidence must be nonempty strings"
                )
            specification_keys = disposition["specification_keys"]
            if (
                not isinstance(specification_keys, list)
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in specification_keys
                )
                or len(specification_keys) != len(set(specification_keys))
            ):
                raise ValueError(
                    "feedback disposition specification_keys must be unique strings"
                )
            if disposition["pr_regression"] and not (
                disposition["valid"] and disposition["in_scope"]
            ):
                raise ValueError(
                    "a pull-request regression must be valid and in scope"
                )
            if disposition["in_scope"] and not disposition["valid"]:
                raise ValueError("in-scope feedback must be valid")

            follow_up = disposition["follow_up_issue"]
            if disposition["valid"] and disposition["in_scope"]:
                if not specification_keys:
                    raise ValueError(
                        "in-scope feedback must map to a specification"
                    )
                if follow_up is not None:
                    raise ValueError(
                        "in-scope feedback cannot create a follow-up issue"
                    )
                mapped_specification_keys.update(specification_keys)
            elif disposition["valid"]:
                if specification_keys:
                    raise ValueError(
                        "out-of-scope feedback cannot map to specifications"
                    )
                if not isinstance(follow_up, dict):
                    raise ValueError(
                        "out-of-scope feedback requires a follow-up issue"
                    )
                required_follow_up_fields = {
                    "title",
                    "observed_defect",
                    "affected_behavior",
                    "affected_paths",
                    "acceptance_criteria",
                }
                if not required_follow_up_fields.issubset(follow_up):
                    raise ValueError(
                        "follow-up issue must contain every required field"
                    )
                for field in (
                    "title",
                    "observed_defect",
                    "affected_behavior",
                ):
                    if (
                        not isinstance(follow_up[field], str)
                        or not follow_up[field].strip()
                    ):
                        raise ValueError(
                            f"follow-up issue {field} must be nonempty"
                        )
                for field in ("affected_paths", "acceptance_criteria"):
                    values = follow_up[field]
                    if (
                        not isinstance(values, list)
                        or any(
                            not isinstance(item, str) or not item.strip()
                            for item in values
                        )
                    ):
                        raise ValueError(
                            f"follow-up issue {field} must be strings"
                        )
                if not follow_up["acceptance_criteria"]:
                    raise ValueError(
                        "follow-up issue acceptance_criteria must be nonempty"
                    )
            else:
                if disposition["in_scope"]:
                    raise ValueError("invalid feedback cannot be in scope")
                if specification_keys:
                    raise ValueError(
                        "invalid feedback cannot map to specifications"
                    )
                if follow_up is not None:
                    raise ValueError(
                        "invalid feedback cannot create a follow-up issue"
                    )

        if seen_ids != set(claimed_ids):
            raise ValueError(
                "feedback Specify must disposition every claimed item exactly once"
            )
        returned_specification_keys: set[str] = set()
        for specification in specifications:
            if (
                not isinstance(specification, dict)
                or not isinstance(specification.get("key"), str)
                or not specification["key"].strip()
                or specification["key"] in returned_specification_keys
            ):
                raise ValueError(
                    "feedback specification keys must be nonempty and unique"
                )
            returned_specification_keys.add(specification["key"])
        if mapped_specification_keys != returned_specification_keys:
            raise ValueError(
                "only in-scope feedback may map to returned specifications"
            )
        return dict(result)

    @staticmethod
    def _feedback_disposition_name(disposition: dict) -> str:
        if not disposition["valid"]:
            return "INVALID"
        if disposition["in_scope"]:
            return "IN_SCOPE"
        return "OUT_OF_SCOPE"

    @staticmethod
    def _github_feedback(feedback_row: dict) -> GitHubFeedback:
        package = feedback_row["package"]
        return GitHubFeedback(
            external_id=feedback_row["external_id"],
            kind=package["kind"],
            body=package["body"],
            path=package.get("path"),
            line=package.get("line"),
            review_thread_id=package.get("review_thread_id"),
            top_level_comment_id=package.get("top_level_comment_id"),
        )

    @staticmethod
    def _feedback_source_url(pull_url: str, feedback_row: dict) -> str:
        package = feedback_row["package"]
        comment_id = package.get("top_level_comment_id")
        if package.get("kind") == "inline" and isinstance(comment_id, int):
            return f"{pull_url}#discussion_r{comment_id}"
        external_id = feedback_row["external_id"]
        if package.get("kind") == "review" and external_id.startswith("review:"):
            return (
                f"{pull_url}#pullrequestreview-"
                f"{external_id.partition(':')[2]}"
            )
        return f"{pull_url}#feedback-{external_id}"

    @classmethod
    def _follow_up_body(
        cls,
        pull_url: str,
        feedback_row: dict,
        disposition: dict,
    ) -> str:
        follow_up = disposition["follow_up_issue"]
        paths = "\n".join(
            f"- `{path}`" for path in follow_up["affected_paths"]
        ) or "- No repository path was identified."
        evidence = "\n".join(
            f"- {item}" for item in disposition["evidence"]
        )
        criteria = "\n".join(
            f"- {item}" for item in follow_up["acceptance_criteria"]
        )
        feedback_url = cls._feedback_source_url(pull_url, feedback_row)
        return (
            "## Observed defect\n\n"
            f"{follow_up['observed_defect']}\n\n"
            "## Affected behavior\n\n"
            f"{follow_up['affected_behavior']}\n\n"
            "## Affected paths\n\n"
            f"{paths}\n\n"
            "## Supporting evidence\n\n"
            f"{evidence}\n\n"
            "## Acceptance criteria\n\n"
            f"{criteria}\n\n"
            "## Why this is outside the current issue\n\n"
            f"{disposition['explanation']}\n\n"
            "## Source\n\n"
            f"- Pull request: {pull_url}\n"
            f"- Feedback: {feedback_url}"
        )

    @staticmethod
    def _no_code_response(
        disposition: dict,
        follow_up_issue: dict | None,
    ) -> str:
        evidence = "\n".join(
            f"- {item}" for item in disposition["evidence"]
        )
        if follow_up_issue is not None:
            return (
                "This is valid feedback, but it is outside the current "
                "issue's scope.\n\n"
                f"Evidence:\n{evidence}\n\n"
                f"Scope reason: {disposition['explanation']}\n\n"
                f"Follow-up issue: {follow_up_issue['url']}"
            )
        return (
            "No current-branch change is needed because this feedback is "
            "invalid or no longer present.\n\n"
            f"Evidence:\n{evidence}\n\n"
            f"Conclusion: {disposition['explanation']}"
        )

    def _resolve_no_code_feedback(
        self,
        repository: dict,
        run: dict,
        dispositions: list[dict],
    ) -> None:
        reference = run.get("pull_request")
        if not reference:
            raise RuntimeError("feedback disposition run has no pull request")
        rows = {
            item["external_id"]: item
            for item in self.store.list_feedback(run["id"])
        }
        for disposition in dispositions:
            disposition_name = self._feedback_disposition_name(disposition)
            if disposition_name == "IN_SCOPE":
                continue
            external_id = disposition["external_id"]
            feedback_row = rows[external_id]
            if feedback_row["status"] != "PENDING":
                continue
            follow_up_issue = feedback_row.get("follow_up_issue")
            if disposition_name == "OUT_OF_SCOPE" and follow_up_issue is None:
                requested = disposition["follow_up_issue"]
                issue = self.github.ensure_follow_up_issue(
                    repository["github_repository"],
                    external_id,
                    requested["title"],
                    self._follow_up_body(
                        reference["url"],
                        feedback_row,
                        disposition,
                    ),
                )
                feedback_row = self.store.record_feedback_follow_up(
                    run["id"],
                    external_id,
                    asdict(issue),
                )
                follow_up_issue = feedback_row["follow_up_issue"]
            feedback = self._github_feedback(feedback_row)
            address = self.github.resolve_feedback_without_code(
                repository["github_repository"],
                int(reference["number"]),
                feedback,
                self._no_code_response(disposition, follow_up_issue),
            )
            self.store.mark_feedback_without_code(
                run["id"],
                external_id,
                address.status,
                address.response_url,
            )

    def _route_unassigned(
        self, repository: dict, run: dict, execution_pass: dict
    ) -> None:
        for work in self.store.list_work_items(run["id"], execution_pass["id"]):
            if work["state"] != "UNASSIGNED":
                continue
            classification = validate_classification(work["classification"])
            nodes = self.store.list_dynamic_nodes(repository["id"])
            cached_vector = self.store.get_classification_vector(
                repository["id"], classification
            )
            node, vector = self.router.route(
                classification,
                nodes,
                repository["similarity_threshold"],
                vector=cached_vector,
            )
            if cached_vector is None:
                self.store.save_classification_vector(
                    repository["id"], classification, vector
                )
            if node is None:
                with self._source_snapshot(
                    self._workspace(repository["id"], run["id"])
                ) as source_workspace:
                    role_result = self.runtime.run(
                        self._task(
                            "node_role",
                            "Generate a flexible role prompt for this repository-reusable agent queue. Describe responsibilities broad enough to serve this classification across repository issues without prescribing a fixed implementation workflow. Use the current work only as context; do not narrow the role to this issue or work item.",
                            {
                                "classification": classification,
                                "work_item": work,
                                "repository": repository,
                            },
                        ),
                        source_workspace,
                        result_schema=_ROLE_SCHEMA,
                        trajectory_path=self._trajectory(
                            run["id"], f"role-{work['id']}"
                        ),
                    )
                role_prompt = role_result.get("role_prompt")
                if not isinstance(role_prompt, str) or not role_prompt.strip():
                    raise ValueError("Node Role Agent must return a nonempty role_prompt")
                node = self.store.create_dynamic_node(
                    repository["id"], classification, vector, role_prompt
                )
            self.store.assign_work(work["id"], node["id"])

    def _wait_for_work(self, repository: dict, run: dict) -> None:
        execution_passes = self.store.list_passes(run["id"])
        if not execution_passes:
            raise RuntimeError("waiting run has no execution pass")
        execution_pass = execution_passes[-1]
        self._route_unassigned(repository, run, execution_pass)
        if self.store.validation_barrier_ready(run["id"], execution_pass["id"]):
            self.store.transition_run(run["id"], "VALIDATING")

    def _start_workers(self, focused_run_ids: set[int]) -> None:
        for run_id in sorted(focused_run_ids):
            run = self.store.get_run(run_id)
            if run is None or run["state"] not in _SOURCE_ACTIVE_RUN_STATES:
                continue
            repository = self.store.get_repository(run["repository_id"])
            if repository is None:
                continue
            for node in self.store.list_dynamic_nodes(repository["id"]):
                with self._worker_lock:
                    if node["id"] in self._workers:
                        continue
                    work = self.store.claim_node_work(node["id"], run_id)
                    if work is None:
                        continue
                    future = self._executor.submit(self._run_work, node, work)
                    self._workers[node["id"]] = future

    def _reap_workers(self) -> None:
        with self._worker_lock:
            done = [node_id for node_id, future in self._workers.items() if future.done()]
            futures = [self._workers.pop(node_id) for node_id in done]
        for future in futures:
            try:
                future.result()
            except Exception:
                pass

    @classmethod
    def _validated_work_result(
        cls,
        result: object,
        execution_pass: dict,
        work_keys: set[str],
    ) -> dict:
        required = {
            "outcome",
            "output",
            "artifacts",
            "test_results",
            "repository_state",
        }
        if not isinstance(result, dict) or not required.issubset(result):
            raise ValueError("work must return the complete work result")
        try:
            json.dumps(result)
        except (TypeError, ValueError) as error:
            raise ValueError("work result must be JSON-safe") from error
        if not isinstance(result["artifacts"], list):
            raise ValueError("work artifacts must be a list")
        if not isinstance(result["test_results"], list):
            raise ValueError("work test_results must be a list")
        if not isinstance(result["repository_state"], dict):
            raise ValueError("work repository_state must be an object")

        normalized = dict(result)
        outcome = result["outcome"]
        if outcome not in {"ready_for_validation", "continue_work"}:
            raise ValueError(
                "work outcome must be ready_for_validation or continue_work"
            )

        resolved_paths = result.get("resolved_paths", [])
        if not isinstance(resolved_paths, list):
            raise ValueError("work resolved_paths must be a list")
        normalized_resolved_paths: list[str] = []
        for resolved_path in resolved_paths:
            normalized_resolved_paths.append(
                cls._validated_relative_path(
                    resolved_path,
                    "work resolved path",
                )
            )
        normalized_resolved_paths = sorted(
            set(normalized_resolved_paths)
        )
        if (
            execution_pass["trigger_type"] != "operation_failure"
            and normalized_resolved_paths
        ):
            raise ValueError(
                "non-operation work may not return resolved_paths"
            )
        normalized["resolved_paths"] = normalized_resolved_paths

        if outcome == "continue_work":
            continuation_fields = {
                "classification",
                "context",
                "dependencies",
                "blocking",
            }
            if not continuation_fields.issubset(result):
                raise ValueError(
                    "continue_work must return the complete handoff"
                )
            normalized["classification"] = validate_classification(
                result["classification"]
            )
            if not isinstance(result["context"], dict):
                raise ValueError("work handoff context must be an object")
            dependencies = result["dependencies"]
            if (
                not isinstance(dependencies, list)
                or any(
                    not isinstance(dependency, str) or not dependency
                    for dependency in dependencies
                )
                or any(
                    dependency not in work_keys
                    for dependency in dependencies
                )
            ):
                raise ValueError(
                    "work handoff dependencies must reference this pass"
                )
            normalized["dependencies"] = list(
                dict.fromkeys(dependencies)
            )
            if (
                result["blocking"] is not None
                and not isinstance(result["blocking"], dict)
            ):
                raise ValueError(
                    "work handoff blocking must be an object or null"
                )
        return normalized

    @classmethod
    def _validate_resolved_path_authorization(
        cls,
        resolved_paths: list[str],
        execution_pass: dict,
        baseline: dict[str, _SourceTreeEntry],
        desired: dict[str, _SourceTreeEntry],
    ) -> None:
        if execution_pass["trigger_type"] != "operation_failure":
            return
        trigger = execution_pass.get("trigger_json")
        operation_workspace = (
            trigger.get("workspace")
            if isinstance(trigger, dict)
            else None
        )
        if not isinstance(operation_workspace, dict):
            raise ValueError(
                "operation failure has no workspace path evidence"
            )
        unmerged_paths = cls._validated_operation_path_list(
            operation_workspace,
            "unmerged_paths",
        )
        cls._validated_operation_path_list(
            operation_workspace,
            "staged_paths",
            allow_missing=True,
        )
        unstaged_paths = cls._validated_operation_path_list(
            operation_workspace,
            "unstaged_paths",
            allow_missing=True,
        )
        untracked_paths = cls._validated_operation_path_list(
            operation_workspace,
            "untracked_paths",
            allow_missing=True,
        )
        changed_paths = {
            path
            for path in baseline.keys() | desired.keys()
            if baseline.get(path) != desired.get(path)
        }
        allowed_paths = (
            changed_paths
            | set(unmerged_paths)
            | set(unstaged_paths)
            | set(untracked_paths)
        )
        if any(path not in allowed_paths for path in resolved_paths):
            raise ValueError(
                "work resolved_paths must be evidenced by this operation "
                "failure or changed by this work"
            )
    def _run_work(self, node: dict, work: dict) -> None:
        run = self.store.get_run(work["run_id"])
        if run is None:
            raise RuntimeError("claimed work has no run")
        repository = self.store.get_repository(run["repository_id"])
        if repository is None:
            raise RuntimeError("claimed work run has no repository")
        execution_pass = next(
            item
            for item in self.store.list_passes(run["id"])
            if item["id"] == work["pass_id"]
        )
        feedback, pull_request_diff = self._pass_feedback_context(
            run["id"],
            execution_pass,
        )
        pass_specifications = [
            item
            for item in self.store.list_specifications(run["id"])
            if item["pass_id"] == work["pass_id"]
        ]
        specifications = {item["id"]: item for item in pass_specifications}
        specification = specifications[work["specification_id"]]
        specification_dependencies = self._specification_dependency_closure(
            pass_specifications,
            specification,
        )
        all_work = self.store.list_work_items(run["id"], work["pass_id"])
        dependency_keys = set(work["dependencies"])
        dependencies = [
            item for item in all_work if item["key"] in dependency_keys
        ]
        try:
            workspace = self._workspace(repository["id"], run["id"])
            with self._source_snapshot(workspace) as source_workspace:
                baseline = self._source_manifest(source_workspace)
                excluded_roots: set[str] = set()
                work_context = {
                    "original_issue": run["issue_json"],
                    "repository": repository,
                    "specification": self._specification_definition(
                        specification
                    ),
                    "work_item": work,
                    "dependency_results": dependencies,
                    "specification_dependencies": (
                        specification_dependencies
                    ),
                    "feedback": feedback,
                    "pull_request_diff": pull_request_diff,
                }
                if execution_pass["trigger_type"] == "operation_failure":
                    operation_artifacts, artifact_root = (
                        self._materialize_operation_artifacts(
                            run["id"],
                            execution_pass,
                            source_workspace,
                        )
                    )
                    excluded_roots.add(artifact_root)
                    work_context["operation_failure"] = execution_pass[
                        "trigger_json"
                    ]
                    work_context["operation_artifacts"] = (
                        operation_artifacts
                    )
                result = self.runtime.run(
                    self._task(
                        "work",
                        "Use your tools and judgment flexibly to complete this bounded work. Return ready_for_validation when no more agent work is needed, or continue_work with the next classification and handoff context. "
                        + _CLASSIFICATION_GUIDANCE,
                        work_context,
                    ),
                    source_workspace,
                    role_prompt=node["role_prompt"],
                    result_schema=_WORK_SCHEMA,
                    trajectory_path=self._trajectory(
                        run["id"], f"work-{work['id']}"
                    ),
                )
                result = self._validated_work_result(
                    result,
                    execution_pass,
                    {item["key"] for item in all_work},
                )
                desired = self._source_manifest(
                    source_workspace,
                    excluded_roots=excluded_roots,
                )
                self._validate_resolved_path_authorization(
                    result["resolved_paths"],
                    execution_pass,
                    baseline,
                    desired,
                )
                with self._durable_source_import(
                    workspace,
                    source_workspace,
                    baseline,
                    desired,
                    repository_id=repository["id"],
                    run_id=run["id"],
                    work_id=work["id"],
                ) as applied_paths:
                    repository_state = dict(result["repository_state"])
                    repository_state["_repogents"] = {
                        "applied_paths": applied_paths,
                        "resolved_paths": result["resolved_paths"],
                    }
                    persisted_result = {
                        "output": result["output"],
                        "artifacts": result["artifacts"],
                        "test_results": result["test_results"],
                        "repository_state": repository_state,
                    }
                    if result["outcome"] == "ready_for_validation":
                        self.store.complete_work(
                            work["id"],
                            persisted_result,
                        )
                    else:
                        handoff = {
                            "classification": result["classification"],
                            "context": result["context"],
                            "artifacts": result["artifacts"],
                            "dependencies": result["dependencies"],
                            "blocking": result["blocking"],
                        }
                        self.store.complete_work(
                            work["id"],
                            persisted_result,
                            handoff,
                        )
            self.store.record_node_success(
                node["id"], run["id"], self.config.promotion_threshold
            )
        except Exception as error:
            failure = {
                "output": {
                    "error": str(error),
                    "type": type(error).__name__,
                },
                "artifacts": [],
                "test_results": [],
                "repository_state": {},
            }
            try:
                self.store.fail_work(work["id"], failure)
            except (KeyError, ValueError):
                pass

    def _validation_context(
        self,
        repository: dict,
        run: dict,
        execution_pass: dict,
        candidate_diff: str,
    ) -> dict:
        feedback, _ = self._pass_feedback_context(
            run["id"],
            execution_pass,
        )
        validations = self.store.list_validations(run["id"])
        return {
            "original_issue": run["issue_json"],
            "repository": repository,
            "specifications": [
                self._specification_definition(item)
                for item in self.store.list_specifications(run["id"])
            ],
            "work_items": self.store.list_work_items(
                run["id"],
                execution_pass["id"],
            ),
            "latest_prior_validation": (
                self._validation_evidence(validations[-1])
                if validations
                else None
            ),
            "feedback": feedback,
            "candidate_diff": candidate_diff,
        }

    @staticmethod
    def _validated_validation_result(result: dict) -> dict:
        required = {
            "passed",
            "failed_specifications",
            "failed_criteria",
            "code_review_findings",
            "explanation",
            "evidence",
            "repository_state",
            "completed_work",
        }
        if not isinstance(result, dict) or not required.issubset(result):
            raise ValueError("Validate must return the complete validation result")
        if not isinstance(result["passed"], bool):
            raise ValueError("validation passed must be boolean")
        for field in (
            "failed_specifications",
            "failed_criteria",
            "code_review_findings",
        ):
            values = result[field]
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"validation {field} must be a list of strings")
        if not isinstance(result["explanation"], str):
            raise ValueError("validation explanation must be a string")
        if not isinstance(result["evidence"], list):
            raise ValueError("validation evidence must be a list")
        if not isinstance(result["repository_state"], dict):
            raise ValueError("validation repository_state must be an object")
        if not isinstance(result["completed_work"], list):
            raise ValueError("validation completed_work must be a list")
        has_failures = bool(
            result["failed_specifications"]
            or result["failed_criteria"]
            or result["code_review_findings"]
        )
        if result["passed"] == has_failures:
            raise ValueError("validation outcome and failures are inconsistent")
        return dict(result)

    @staticmethod
    def _is_current_persisted_validation(result: dict) -> bool:
        return (
            isinstance(result, dict)
            and "code_review_findings" in result
            and "publication_candidate" in result
        )

    def _pass_has_specifications(self, run_id: int, pass_id: int) -> bool:
        return any(
            item["pass_id"] == pass_id
            for item in self.store.list_specifications(run_id)
        )

    def _operation_failure_paths(
        self,
        run_id: int,
        pass_id: int,
    ) -> list[str]:
        paths: set[str] = set()
        for work in self.store.list_work_items(run_id, pass_id):
            result = work.get("result")
            if not isinstance(result, dict):
                continue
            repository_state = result.get("repository_state")
            if not isinstance(repository_state, dict):
                continue
            controller_state = repository_state.get("_repogents")
            if not isinstance(controller_state, dict):
                continue
            values = controller_state.get("resolved_paths")
            if (
                not isinstance(values, list)
                or any(
                    not isinstance(value, str) or not value
                    for value in values
                )
            ):
                raise ValueError(
                    "controller work state resolved_paths is invalid"
                )
            paths.update(
                self._validated_relative_path(
                    value,
                    "controller work state resolved_paths",
                )
                for value in values
            )
        return sorted(paths)

    def _validate(self, repository: dict, run: dict) -> None:
        execution_passes = self.store.list_passes(run["id"])
        if not execution_passes:
            raise RuntimeError("validating run has no execution pass")
        execution_pass = execution_passes[-1]
        if (
            execution_pass["trigger_type"]
            in {"feedback", "operation_failure", "validation_failure"}
            and not self._pass_has_specifications(
                run["id"], execution_pass["id"]
            )
        ):
            self.store.transition_run(run["id"], "SPECIFYING")
            return
        recorded = next(
            (
                item["result"]
                for item in self.store.list_validations(run["id"])
                if item["pass_id"] == execution_pass["id"]
            ),
            None,
        )
        if recorded is None:
            workspace = self._workspace(repository["id"], run["id"])
            if execution_pass["trigger_type"] == "operation_failure":
                continuation_paths = self._operation_failure_paths(
                    run["id"],
                    execution_pass["id"],
                )
                try:
                    self.github.continue_repository_operation(
                        workspace,
                        continuation_paths,
                    )
                except subprocess.CalledProcessError as error:
                    self._record_operation_failure(
                        repository,
                        run,
                        execution_pass,
                        "continue_repository_operation",
                        error,
                    )
                    return
            try:
                candidate, _ = self.github.prepare_publication(
                    run["issue_number"],
                    repository["target_branch"],
                    workspace,
                )
            except subprocess.CalledProcessError as error:
                self._record_operation_failure(
                    repository,
                    run,
                    execution_pass,
                    "prepare_publication",
                    error,
                )
                return

            with tempfile.TemporaryDirectory(
                prefix="repogents-validate-controller-",
                dir=self.data_dir,
            ) as temporary_directory:
                controller_workspace = (
                    Path(temporary_directory) / "workspace"
                )
                with self._source_lock:
                    shutil.copytree(
                        workspace,
                        controller_workspace,
                        symlinks=True,
                    )
                candidate_diff = self.github.candidate_diff(
                    repository["target_branch"],
                    controller_workspace,
                    candidate=candidate,
                )

            with self._source_snapshot(workspace) as validation_workspace:
                result = self.runtime.run(
                    self._task(
                        "validate",
                        "Judge the completed result against every atomic specification, acceptance criterion, and the intent of the original issue. Independently review the complete staged target-to-candidate diff for branch-introduced correctness defects, regressions, and changes not mapped to the issue, specifications, necessary prerequisites, or current in-scope feedback. Do not audit unrelated pre-existing code. Do not modify repository files or implement corrections. Return a failed validation result for any failed specification, failed criterion, or code-review finding.",
                        self._validation_context(
                            repository,
                            run,
                            execution_pass,
                            candidate_diff,
                        ),
                    ),
                    validation_workspace,
                    result_schema=_VALIDATION_SCHEMA,
                    trajectory_path=self._trajectory(
                        run["id"], f"validate-{execution_pass['id']}"
                    ),
                )
            result = self._validated_validation_result(result)
            result["publication_candidate"] = asdict(candidate)
            self.store.record_validation(
                run["id"], execution_pass["id"], result
            )
        else:
            if not self._is_current_persisted_validation(recorded):
                self._start_publication_revalidation(
                    run,
                    execution_passes,
                    None,
                )
                return
            result = self._validated_validation_result(recorded)
        if not result["passed"]:
            latest_pass = self.store.list_passes(run["id"])[-1]
            if latest_pass["id"] == execution_pass["id"]:
                trigger = dict(result)
                origin_feedback_pass_id = self._feedback_origin_pass_id(
                    execution_pass
                )
                if origin_feedback_pass_id is not None:
                    trigger["origin_feedback_pass_id"] = (
                        origin_feedback_pass_id
                    )
                self.store.create_pass(
                    run["id"],
                    "validation_failure",
                    trigger,
                )
            self.store.transition_run(run["id"], "SPECIFYING")
            return
        self.store.transition_run(run["id"], "CREATING_PR")

    def _publication_feedback_ids(
        self,
        run_id: int,
        execution_pass: dict,
    ) -> set[str]:
        origin_pass_id = self._feedback_origin_pass_id(execution_pass)
        if origin_pass_id is None:
            return set()
        result = self.store.get_feedback_scope_result(
            run_id,
            origin_pass_id,
        )
        return {
            item["external_id"]
            for item in (result or {}).get("dispositions", [])
            if isinstance(item, dict)
            and item.get("valid") is True
            and item.get("in_scope") is True
            and isinstance(item.get("external_id"), str)
        }

    @staticmethod
    def _validated_publication_candidate(
        validations: list[dict],
    ) -> PublicationCandidate | None:
        if (
            not validations
            or validations[-1]["result"].get("passed") is not True
        ):
            raise RuntimeError("publication requires a successful validation")
        payload = validations[-1]["result"].get("publication_candidate")
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise RuntimeError("validation publication candidate is invalid")
        branch = payload.get("branch")
        head_sha = payload.get("head_sha")
        target_head_sha = payload.get("target_head_sha")
        remote_head_sha = payload.get("remote_head_sha")
        if (
            not isinstance(branch, str)
            or not branch
            or not isinstance(head_sha, str)
            or not head_sha
            or not isinstance(target_head_sha, str)
            or not target_head_sha
            or not isinstance(remote_head_sha, str)
        ):
            raise RuntimeError("validation publication candidate is invalid")
        return PublicationCandidate(
            branch=branch,
            head_sha=head_sha,
            target_head_sha=target_head_sha,
            remote_head_sha=remote_head_sha,
        )

    def _start_publication_revalidation(
        self,
        run: dict,
        execution_passes: list[dict],
        candidate: PublicationCandidate | None,
    ) -> None:
        latest_pass = execution_passes[-1]
        has_latest_validation = any(
            item["pass_id"] == latest_pass["id"]
            for item in self.store.list_validations(run["id"])
        )
        if (
            latest_pass["trigger_type"] != "publication_revalidation"
            or has_latest_validation
        ):
            trigger: dict[str, Any] = {}
            if candidate is not None:
                trigger["publication_candidate"] = asdict(candidate)
            origin_feedback_pass_id = self._feedback_origin_pass_id(
                latest_pass
            )
            if origin_feedback_pass_id is not None:
                trigger["origin_feedback_pass_id"] = origin_feedback_pass_id
            self.store.create_pass(
                run["id"],
                "publication_revalidation",
                trigger,
            )
        self.store.transition_run(run["id"], "VALIDATING")

    def _publish(self, repository: dict, run: dict) -> None:
        execution_passes = self.store.list_passes(run["id"])
        if not execution_passes:
            raise RuntimeError("publication requires an execution pass")
        latest_pass_id = execution_passes[-1]["id"]
        validations = [
            item
            for item in self.store.list_validations(run["id"])
            if item["pass_id"] == latest_pass_id
        ]
        if not validations or not self._is_current_persisted_validation(
            validations[-1]["result"]
        ):
            self._start_publication_revalidation(
                run,
                execution_passes,
                None,
            )
            return
        candidate = self._validated_publication_candidate(validations)
        if candidate is None:
            self._start_publication_revalidation(
                run,
                execution_passes,
                None,
            )
            return
        existing = run.get("pull_request")
        existing_number = None if existing is None else int(existing["number"])
        pull = self.github.publish_prepared(
            repository["github_repository"],
            run["issue_number"],
            repository["target_branch"],
            self._workspace(repository["id"], run["id"]),
            candidate,
            existing_pr=existing_number,
        )
        if pull is None:
            self._start_publication_revalidation(
                run,
                execution_passes,
                candidate,
            )
            return
        claimed_feedback_ids = self._publication_feedback_ids(
            run["id"],
            execution_passes[-1],
        )
        for feedback_row in self.store.list_feedback(run["id"]):
            if (
                feedback_row["status"] != "PENDING"
                or feedback_row["external_id"] not in claimed_feedback_ids
            ):
                continue
            feedback = self._github_feedback(feedback_row)
            address = self.github.address_feedback(
                repository["github_repository"],
                pull.number,
                feedback,
                candidate.head_sha,
            )
            self.store.mark_feedback_addressed(
                run["id"],
                feedback.external_id,
                address.status,
                candidate.head_sha,
                address.response_url,
            )
        pull_request = asdict(pull)
        pull_request["validated_head_sha"] = candidate.head_sha
        self.store.transition_run(
            run["id"],
            "PR_LISTENING",
            branch=pull.branch,
            pull_request=pull_request,
            pr_listening_since=self._clock(),
        )

    @staticmethod
    def _feedback_package(
        feedback: GitHubFeedback,
        pull: PullRequest,
        run: dict,
        specifications: list[dict],
        work_items: list[dict],
        validations: list[dict],
    ) -> dict:
        return {
            "external_id": feedback.external_id,
            "kind": feedback.kind,
            "body": feedback.body,
            "path": feedback.path,
            "line": feedback.line,
            "review_thread_id": feedback.review_thread_id,
            "top_level_comment_id": feedback.top_level_comment_id,
            "diff": pull.diff,
            "original_issue": run["issue_json"],
            "specifications": specifications,
            "work_items": work_items,
            "validations": validations,
        }

    @staticmethod
    def _refreshed_pull_request(pull: PullRequest, reference: dict) -> dict:
        refreshed = asdict(pull)
        refreshed["validated_head_sha"] = reference.get("validated_head_sha")
        return refreshed

    def _poll_pull_request(self, repository: dict, run: dict) -> None:
        reference = run.get("pull_request")
        if not reference:
            raise RuntimeError("PR_LISTENING run has no pull request")
        pull = self.github.pull_request(
            repository["github_repository"], int(reference["number"])
        )
        pull_request = self._refreshed_pull_request(pull, reference)
        if pull.merged:
            self.store.transition_run(
                run["id"],
                "COMPLETED",
                branch=pull.branch,
                pull_request=pull_request,
            )
            self.store.adapt_nodes_after_run(
                run["id"], self.config.stale_run_threshold
            )
            return
        if pull.state == "closed":
            self.store.transition_run(
                run["id"],
                "CLOSED",
                branch=pull.branch,
                pull_request=pull_request,
            )
            self.store.adapt_nodes_after_run(
                run["id"], self.config.stale_run_threshold
            )
            return

        transition_fields: dict[str, Any] = {
            "branch": pull.branch,
            "pull_request": pull_request,
        }
        if (
            run["state"] == "PR_LISTENING"
            and run.get("pr_listening_since") is None
        ):
            transition_fields["pr_listening_since"] = self._clock()
        self.store.transition_run(
            run["id"],
            run["state"],
            **transition_fields,
        )
        specifications = self.store.list_specifications(run["id"])
        work_items = self.store.list_work_items(run["id"])
        validations = self.store.list_validations(run["id"])
        for feedback in self.github.list_feedback(
            repository["github_repository"], pull.number
        ):
            package = self._feedback_package(
                feedback,
                pull,
                run,
                specifications,
                work_items,
                validations,
            )
            self.store.add_feedback(run["id"], feedback.external_id, package)
