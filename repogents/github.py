from __future__ import annotations
from base64 import b64encode

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from urllib import parse as urlparse
from urllib import request as urlrequest


_REVIEW_THREADS_QUERY = """
query ReviewThreads(
  $owner: String!
  $name: String!
  $number: Int!
  $after: String
) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes {
          id
          isResolved
          viewerCanResolve
          comments(first: 100) {
            nodes {
              id
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""

_REVIEW_THREAD_COMMENTS_QUERY = """
query ReviewThreadComments($threadId: ID!, $after: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      comments(first: 100, after: $after) {
        nodes {
          id
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""

_REVIEW_THREAD_QUERY = """
query ReviewThread($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      viewerCanResolve
    }
  }
}
"""

_RESOLVE_THREAD_MUTATION = """
mutation ResolveThread($input: ResolveReviewThreadInput!) {
  resolveReviewThread(input: $input) {
    thread {
      id
      isResolved
    }
  }
}
"""

_FEEDBACK_MARKER_PREFIX = "<!-- repogents-feedback:"


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str
    url: str


@dataclass(frozen=True)
class GitHubFeedback:
    external_id: str
    kind: str
    body: str
    path: str | None = None
    line: int | None = None
    review_thread_id: str | None = None
    top_level_comment_id: int | None = None


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    branch: str
    state: str
    merged: bool
    diff: str
    head_sha: str = ""


@dataclass(frozen=True)
class FeedbackAddress:
    status: str
    response_url: str


@dataclass(slots=True)
class _HttpTransportTask:
    request: object
    deadline: float
    outcome: queue.Queue[tuple[bool, object]]


class _BoundedHttpTransportPool:
    """Run complete urllib requests on a fixed set of daemon workers.

    Python's system resolver is synchronous and is not bounded by a socket timeout:
    ``urlopen`` may block in ``getaddrinfo`` before any socket exists. Running the
    complete request, including response consumption, behind fixed admission capacity
    lets callers enforce the configured monotonic deadline without creating one
    abandoned resolver thread or queued request per timeout. A late transport result
    has no callback into application code; it only releases its fixed worker slot.
    """

    def __init__(self, owner: "GitHubClient", max_workers: int):
        self._owner = owner
        self._capacity = threading.BoundedSemaphore(max_workers)
        self._tasks: queue.Queue[_HttpTransportTask] = queue.Queue(maxsize=max_workers)
        self._workers = []
        for index in range(max_workers):
            worker = threading.Thread(
                target=self._run,
                name=f"repogents-github-http-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def execute(self, request, deadline: float) -> tuple[bytes, str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._capacity.acquire(timeout=max(0.0, remaining)):
            raise TimeoutError("GitHub request exceeded its total transport deadline")
        task = _HttpTransportTask(request, deadline, queue.Queue(maxsize=1))
        self._tasks.put_nowait(task)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            succeeded, value = task.outcome.get(timeout=remaining)
        except queue.Empty as error:
            raise TimeoutError(
                "GitHub request exceeded its total transport deadline"
            ) from error
        if not succeeded:
            raise value
        return value

    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            try:
                try:
                    value = self._owner._perform_default_request(
                        task.request, task.deadline
                    )
                except BaseException as error:
                    task.outcome.put((False, error))
                else:
                    task.outcome.put((True, value))
            finally:
                self._capacity.release()
                self._tasks.task_done()


@dataclass(frozen=True)
class _GitWorkspaceSnapshot:
    head: str | None
    branch: str | None
    index: bytes | None = None
    tracked_worktree_patch: bytes | None = None

    @property
    def preserves_tracked_edits(self) -> bool:
        return self.index is not None and self.tracked_worktree_patch is not None


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_base: str = "https://api.github.com",
        request=None,
        command_runner=None,
        binary_command_runner=None,
        transport_timeout: float = 30.0,
        git_command_timeout: float = 300.0,
        http_transport_max_workers: int = 4,
    ):
        if not math.isfinite(transport_timeout) or transport_timeout <= 0:
            raise ValueError("transport_timeout must be finite and positive")
        if not math.isfinite(git_command_timeout) or git_command_timeout <= 0:
            raise ValueError("git_command_timeout must be finite and positive")
        if (
            isinstance(http_transport_max_workers, bool)
            or not isinstance(http_transport_max_workers, int)
            or http_transport_max_workers <= 0
        ):
            raise ValueError("http_transport_max_workers must be a positive integer")
        self._token = token
        self._transport_timeout = float(transport_timeout)
        self._git_command_timeout = float(git_command_timeout)
        self._http_transport_max_workers = http_transport_max_workers
        self._http_transport_pool = None
        self._http_transport_pool_lock = threading.Lock()
        self._api_base = api_base.rstrip("/")
        self._request = request or self._default_request
        self._command_runner = command_runner or self._default_command_runner
        if binary_command_runner is not None:
            self._binary_command_runner = binary_command_runner
        elif command_runner is None:
            self._binary_command_runner = self._default_binary_command_runner
        else:
            # Preserve the injected text-runner contract used by lightweight callers.
            # Production patch capture always uses the byte-native default runner.
            self._binary_command_runner = self._binary_runner_adapter
        credential = b64encode(f"x-access-token:{token}".encode()).decode()
        self._git_command_env = {"GIT_TERMINAL_PROMPT": "0"}
        self._git_auth_env = {
            **self._git_command_env,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
        }
        self._git_identity_env = {
            **self._git_command_env,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "Repogents",
            "GIT_CONFIG_KEY_1": "user.email",
            "GIT_CONFIG_VALUE_1": "repogents@localhost",
        }

    def _default_request(self, method, path, *, query=None, json_body=None):
        accept = "application/vnd.github+json"
        if path.endswith(".diff"):
            path = path.removesuffix(".diff")
            accept = "application/vnd.github.diff"
        url = f"{self._api_base}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urlparse.urlencode(query, doseq=True)}"
        body = None if json_body is None else json.dumps(json_body).encode("utf-8")
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "repogents",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        http_request = urlrequest.Request(url, data=body, headers=headers, method=method)
        deadline = time.monotonic() + self._transport_timeout
        with self._http_transport_pool_lock:
            if self._http_transport_pool is None:
                self._http_transport_pool = _BoundedHttpTransportPool(
                    self, self._http_transport_max_workers
                )
            transport_pool = self._http_transport_pool
        payload, content_type = transport_pool.execute(http_request, deadline)
        if not payload:
            return None
        text = payload.decode("utf-8")
        if "json" in content_type:
            return json.loads(text)
        return text

    def _perform_default_request(self, http_request, deadline: float) -> tuple[bytes, str]:
        """Own DNS, connection, headers, and body work inside one bounded task."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("GitHub request exceeded its total transport deadline")
        with urlrequest.urlopen(http_request, timeout=remaining) as response:
            payload = self._read_response_body(response, deadline)
            return payload, response.headers.get("Content-Type", "")

    @staticmethod
    def _read_response_body(response, deadline: float) -> bytes:
        """Consume one GitHub response under the request's absolute deadline.

        ``urlopen(..., timeout=...)`` applies an inactivity timeout to individual
        socket operations. A peer can otherwise retain a poller or repository-lookup
        worker indefinitely by returning each response-body byte before that timeout
        expires. Reapply only the remaining monotonic budget before each bounded read
        and reject a read that completes after the total request deadline.
        """
        chunks = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("GitHub request exceeded its total transport deadline")

            # urllib's HTTPResponse exposes the active socket through its buffered
            # file object. Custom/injected response objects need not provide it; the
            # post-read monotonic check still enforces the total deadline in tests
            # and alternate local transports.
            try:
                response.fp.raw._sock.settimeout(remaining)
            except AttributeError:
                pass

            read_once = getattr(response, "read1", response.read)
            chunk = read_once(64 * 1024)
            if time.monotonic() > deadline:
                raise TimeoutError("GitHub request exceeded its total transport deadline")
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    @staticmethod
    def _git_process_group_members(process_group: int) -> dict[int, tuple[str, int]] | None:
        """Return Linux process-group members and states, or ``None`` off Linux.

        Keeping Git alive while its helpers terminate lets Git reap the children it
        launched.  ``killpg`` cannot provide that ordering, and a zombie-only group
        still passes ``killpg(group, 0)`` under a non-reaping container PID 1.  Linux
        exposes the locally owned group through procfs, which lets timeout cleanup
        signal every non-leader first and wait until Git has reaped them.
        """
        proc = Path("/proc")
        if not proc.is_dir():
            return None
        members: dict[int, tuple[str, int]] = {}
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="ascii")
                # The command name is parenthesized and may itself contain spaces or
                # parentheses. Fields after its final ')' begin with state, PPID,
                # process group, and session.
                fields = stat[stat.rfind(")") + 2 :].split()
                if len(fields) >= 3 and int(fields[2]) == process_group:
                    members[int(entry.name)] = (fields[0], int(fields[1]))
            except (OSError, ValueError):
                # Processes may disappear between directory enumeration and read.
                continue
        return members

    @staticmethod
    def _git_process_group_exists(process_group: int) -> bool:
        """Return whether a POSIX-owned Git process group still has members."""
        members = GitHubClient._git_process_group_members(process_group)
        if members is not None:
            # A terminated process can remain visible in procfs as a zombie until
            # its parent waits for it. Zombie-only remnants cannot mutate the
            # workspace and must not reproduce killpg(0)'s false liveness result.
            return any(state != "Z" for state, _parent in members.values())
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _signal_git_descendant_leaves(
        process_group: int, leader: int, sig: int
    ) -> bool:
        """Signal only leaf descendants so each live parent can reap its children."""
        members = GitHubClient._git_process_group_members(process_group)
        if members is None:
            os.killpg(process_group, sig)
            return False
        descendants = {pid for pid in members if pid != leader}
        parents = {
            parent
            for pid, (_state, parent) in members.items()
            if pid in descendants and parent in descendants
        }
        leaves = descendants - parents
        for pid in leaves:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        return bool(leaves)

    @staticmethod
    def _wait_for_git_descendants(
        process_group: int, leader: int, deadline: float
    ) -> bool:
        """Wait until Git has reaped every non-leader group member."""
        while time.monotonic() < deadline:
            members = GitHubClient._git_process_group_members(process_group)
            if members is None:
                return False
            if not any(pid != leader for pid in members):
                return True
            time.sleep(0.01)
        members = GitHubClient._git_process_group_members(process_group)
        return members is not None and not any(pid != leader for pid in members)

    @staticmethod
    def _terminate_git_process_tree(process, *, grace_seconds: float = 0.5) -> None:
        """Terminate Git's complete tree while letting Git reap its descendants.

        On Linux, descendants are signalled before the Git group leader. Git remains
        alive to collect terminated hooks, helpers, and shells, which prevents those
        processes from being orphaned as zombies to a non-reaping PID 1. Other POSIX
        platforms retain bounded process-group escalation where procfs enumeration is
        unavailable. Windows continues to use its native task-tree termination.
        """
        deadline = time.monotonic() + grace_seconds
        if os.name == "nt":  # pragma: win32 cover
            # taskkill /T is the local authority for the complete Windows process
            # tree. A best-effort kill of only the Git leader cannot establish that
            # hooks, credential helpers, or remote helpers have exited, so every
            # taskkill failure must propagate to the default runner's unsafe-timeout
            # contract instead of being hidden by a later process.wait attempt.
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=grace_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise OSError(
                    f"could not terminate Windows Git process tree {process.pid}: {error}"
                ) from error
            if result.returncode != 0:
                raise OSError(
                    "taskkill could not confirm termination of Windows Git process "
                    f"tree {process.pid} (exit status {result.returncode})"
                )
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired) as error:
                raise OSError(
                    f"Windows Git process {process.pid} did not exit after taskkill"
                ) from error
            if process.poll() is None:
                raise OSError(
                    f"Windows Git process {process.pid} is still running after taskkill"
                )
            return

        process_group = process.pid
        members = GitHubClient._git_process_group_members(process_group)
        if members is not None:
            # Stop mutation-capable descendants first. The still-live Git leader owns
            # their wait lifecycle and can reap both exited children and zombie
            # grandchildren adopted from helper shells before it is terminated.
            descendants_deadline = time.monotonic() + grace_seconds
            # Work from leaves toward Git. A shell waiting on a helper remains alive
            # long enough to reap it, then exits and is reaped by its own parent.
            while time.monotonic() < descendants_deadline:
                members = GitHubClient._git_process_group_members(process_group) or {}
                if not any(pid != process.pid for pid in members):
                    break
                GitHubClient._signal_git_descendant_leaves(
                    process_group, process.pid, signal.SIGTERM
                )
                time.sleep(0.01)
            if not GitHubClient._wait_for_git_descendants(
                process_group, process.pid, descendants_deadline
            ):
                kill_deadline = time.monotonic() + grace_seconds
                while time.monotonic() < kill_deadline:
                    members = GitHubClient._git_process_group_members(process_group) or {}
                    if not any(pid != process.pid for pid in members):
                        break
                    GitHubClient._signal_git_descendant_leaves(
                        process_group, process.pid, signal.SIGKILL
                    )
                    time.sleep(0.01)
                if not GitHubClient._wait_for_git_descendants(
                    process_group, process.pid, kill_deadline
                ):
                    raise OSError(
                        f"Git process group {process_group} descendants were not reaped "
                        "after bounded termination"
                    )

            # No helper can mutate the workspace now. Terminate and reap Git itself.
            if process.poll() is None:
                try:
                    os.kill(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.kill(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=grace_seconds)
            if GitHubClient._git_process_group_exists(process_group):
                raise OSError(
                    f"Git process group {process_group} still exists after descendant reaping"
                )
            return

        # Portable POSIX fallback when process membership cannot be enumerated.
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        while time.monotonic() < deadline:
            if not GitHubClient._git_process_group_exists(process_group):
                break
            time.sleep(0.01)
        if GitHubClient._git_process_group_exists(process_group):
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        kill_deadline = time.monotonic() + grace_seconds
        while time.monotonic() < kill_deadline:
            if not GitHubClient._git_process_group_exists(process_group):
                break
            time.sleep(0.01)
        try:
            process.wait(timeout=max(0.0, kill_deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            pass
        if GitHubClient._git_process_group_exists(process_group):
            raise OSError(
                f"Git process group {process_group} still exists after bounded termination"
            )

    def _run_default_git_command(self, args, *, cwd=None, env=None, text: bool):
        """Run one Git command with either text or byte-preserving pipes."""
        command_env = os.environ.copy()
        if env:
            command_env.update(env)
        popen_kwargs = {
            "cwd": cwd,
            "env": command_env,
            "text": text,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":  # pragma: win32 cover
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(args, **popen_kwargs)
        try:
            stdout, stderr = process.communicate(timeout=self._git_command_timeout)
        except subprocess.TimeoutExpired as error:
            # Workspace recovery must not begin until the complete Git-owned process
            # tree is terminated. Preserve this exact timeout as the public failure.
            try:
                self._terminate_git_process_tree(process)
            except BaseException as termination_error:
                # Propagate an explicit unsafe outcome to mutating-command callers.
                # Recording a note alone is insufficient: they must not begin Git
                # recovery while this owned tree may still be changing the workspace.
                error.repogents_git_tree_termination_safe = False
                self._record_recovery_error(
                    error,
                    "could not fully terminate timed-out Git process tree: "
                    f"{termination_error}",
                )
            else:
                error.repogents_git_tree_termination_safe = True
            try:
                stdout, stderr = process.communicate(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                stdout, stderr = error.output, error.stderr
            if stdout is not None:
                error.output = stdout
                error.stdout = stdout
            if stderr is not None:
                error.stderr = stderr
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode, args, output=stdout, stderr=stderr
            )
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    def _default_command_runner(self, args, *, cwd=None, env=None):
        return self._run_default_git_command(args, cwd=cwd, env=env, text=True)

    def _default_binary_command_runner(self, args, *, cwd=None, env=None):
        return self._run_default_git_command(args, cwd=cwd, env=env, text=False)

    def _binary_runner_adapter(self, args, *, cwd=None, env=None):
        """Adapt an explicitly injected legacy runner for snapshot-focused tests."""
        result = self._command_runner(args, cwd=cwd, env=env)
        stdout = result.stdout
        stderr = getattr(result, "stderr", None)
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        return subprocess.CompletedProcess(result.args if hasattr(result, "args") else args, getattr(result, "returncode", 0), stdout, stderr)

    def repository(self, github_repository: str) -> dict:
        repository = self._request("GET", f"/repos/{github_repository}")
        if not isinstance(repository, dict):
            raise RuntimeError("GitHub returned an invalid repository")
        return repository


    def list_ready_issues(self, github_repository: str) -> list[GitHubIssue]:
        issues = []
        page = 1
        while True:
            page_issues = self._request(
                "GET",
                f"/repos/{github_repository}/issues",
                query={
                    "state": "open",
                    "labels": "agent:ready",
                    "per_page": 100,
                    "page": page,
                },
            )
            if not isinstance(page_issues, list):
                raise RuntimeError("GitHub returned an invalid issues page")
            issues.extend(page_issues)
            if len(page_issues) < 100:
                break
            page += 1
        return [
            GitHubIssue(
                number=issue["number"],
                title=issue["title"],
                body=issue.get("body") or "",
                url=issue["html_url"],
            )
            for issue in issues
            if "pull_request" not in issue
        ]

    @staticmethod
    def _workspace_snapshot(workspace: Path) -> _GitWorkspaceSnapshot:
        """Capture enough repository identity to recover a timed-out mutation."""
        git_dir = workspace / ".git"
        head_file = git_dir / "HEAD"
        try:
            head_value = head_file.read_text(encoding="utf-8").strip()
        except OSError:
            return _GitWorkspaceSnapshot(None, None)
        if head_value.startswith("ref: "):
            reference = head_value[5:].strip()
            branch = reference.removeprefix("refs/heads/")
            try:
                head = (git_dir / reference).read_text(encoding="ascii").strip()
            except OSError:
                head = None
                try:
                    packed = (git_dir / "packed-refs").read_text(encoding="ascii")
                except OSError:
                    packed = ""
                for line in packed.splitlines():
                    fields = line.split()
                    if len(fields) == 2 and fields[1] == reference:
                        head = fields[0]
                        break
            return _GitWorkspaceSnapshot(head or None, branch or None)
        return _GitWorkspaceSnapshot(head_value or None, None)

    def _publication_workspace_snapshot(self, workspace: Path) -> _GitWorkspaceSnapshot:
        """Capture publication edits before checkout can switch branches.

        Publication starts with agent-generated tracked edits in the existing worktree.
        The ordinary checkout recovery contract may discard tracked changes because
        checkout and pull normally introduce those changes themselves.  Preserve the
        exact pre-publication index plus a binary-capable HEAD-to-worktree patch so
        recovery can remove only checkout-owned changes and then reconstruct the
        agent's staged and unstaged tracked state.
        """
        snapshot = self._workspace_snapshot(workspace)
        if not snapshot.head:
            return snapshot
        index_path = workspace / ".git" / "index"
        try:
            index = index_path.read_bytes()
        except OSError as error:
            raise OSError(
                f"could not capture the Git index before publication checkout: {error}"
            ) from error
        patch = self._binary_command_runner(
            ["git", "diff", "--binary", snapshot.head],
            cwd=workspace,
            env=self._git_command_env,
        ).stdout
        return _GitWorkspaceSnapshot(
            snapshot.head,
            snapshot.branch,
            index=index,
            tracked_worktree_patch=patch,
        )

    @staticmethod
    def _record_recovery_error(error: subprocess.TimeoutExpired, detail: str) -> None:
        """Preserve recovery diagnostics without replacing the original timeout."""
        add_note = getattr(error, "add_note", None)
        if callable(add_note):
            add_note(detail)
            return
        diagnostics = getattr(error, "repogents_recovery_errors", None)
        if diagnostics is None:
            diagnostics = []
            setattr(error, "repogents_recovery_errors", diagnostics)
        diagnostics.append(detail)

    def _recover_timed_out_git_mutation(
        self, workspace: Path, snapshot: _GitWorkspaceSnapshot, command: str
    ) -> None:
        """Abort in-progress Git state, remove stale locks, and restore the old HEAD.

        Checkout and pull may update Git-owned tracked files before a hook stalls,
        so those commands force the original branch back into the worktree and use a
        hard reset to restore tracked content. Commit and rebase recovery keeps the
        existing mixed-reset contract so generated worktree content remains present.
        In every case the index and branch identity return to the pre-command commit,
        allowing a later poll or publish attempt to retry instead of inheriting an
        interrupted operation.
        """
        git_dir = workspace / ".git"
        diagnostics = []
        for lock in sorted(git_dir.rglob("*.lock")) if git_dir.is_dir() else ():
            try:
                if lock.is_file() or lock.is_symlink():
                    lock.unlink(missing_ok=True)
            except OSError as error:
                diagnostics.append(f"could not remove stale Git lock {lock}: {error}")

        rebase_state_paths = (git_dir / "rebase-merge", git_dir / "rebase-apply")
        if any(state_path.exists() for state_path in rebase_state_paths):
            try:
                self._command_runner(
                    ["git", "rebase", "--abort"],
                    cwd=workspace,
                    env=self._git_identity_env,
                )
            except subprocess.CalledProcessError as error:
                # Rebase metadata can disappear between the state check and abort
                # (for example, when Git exits after its process tree is terminated).
                # An authoritative no-rebase result is benign once no state remains;
                # the captured branch/HEAD restoration below is still required.
                if any(state_path.exists() for state_path in rebase_state_paths):
                    diagnostics.append(f"git rebase --abort failed: {error}")
            except (subprocess.TimeoutExpired, OSError) as error:
                diagnostics.append(f"git rebase --abort failed: {error}")

        # If Git could not abort its operation, remove only operation metadata after
        # restoring the durable HEAD/index below. These paths are Git-owned state,
        # not caller worktree content.
        for state_path in (
            git_dir / "rebase-merge",
            git_dir / "rebase-apply",
            git_dir / "MERGE_HEAD",
            git_dir / "CHERRY_PICK_HEAD",
            git_dir / "REVERT_HEAD",
        ):
            try:
                if state_path.is_dir() and not state_path.is_symlink():
                    shutil.rmtree(state_path)
                elif state_path.exists() or state_path.is_symlink():
                    state_path.unlink(missing_ok=True)
            except OSError as error:
                diagnostics.append(
                    f"could not remove interrupted Git state {state_path}: {error}"
                )

        if snapshot.head:
            try:
                restores_tracked_worktree = command in {"checkout", "pull"}
                # Return to the original branch before resetting its index/HEAD.
                # Resetting first could accidentally move a newly checked-out target
                # branch to the old branch's commit after a checkout timeout. Checkout
                # and pull recovery is allowed to discard only Git-owned tracked-file
                # changes introduced by those commands, so force the original branch
                # into the worktree before restoring its exact captured commit.
                if snapshot.branch:
                    checkout_args = ["git", "checkout"]
                    if restores_tracked_worktree:
                        checkout_args.append("--force")
                    checkout_args.append(snapshot.branch)
                    self._command_runner(
                        checkout_args,
                        cwd=workspace,
                        env=self._git_command_env,
                    )
                reset_mode = "--hard" if restores_tracked_worktree else "--mixed"
                self._command_runner(
                    ["git", "reset", reset_mode, snapshot.head],
                    cwd=workspace,
                    env=self._git_command_env,
                )
                if snapshot.preserves_tracked_edits:
                    # The hard reset removed checkout-owned tracked changes and also
                    # cleared the agent's pre-command edits. Restore the exact index,
                    # then reconstruct the captured tracked worktree relative to HEAD.
                    index_path = git_dir / "index"
                    temporary_index = index_path.with_name("index.repogents-recovery")
                    temporary_index.write_bytes(snapshot.index)
                    os.replace(temporary_index, index_path)
                    if snapshot.tracked_worktree_patch:
                        patch_path = None
                        try:
                            with tempfile.NamedTemporaryFile(
                                mode="wb",
                                dir=git_dir,
                                prefix="repogents-worktree-",
                                suffix=".patch",
                                delete=False,
                            ) as patch_file:
                                patch_file.write(snapshot.tracked_worktree_patch)
                                patch_path = Path(patch_file.name)
                            self._command_runner(
                                ["git", "apply", "--binary", str(patch_path)],
                                cwd=workspace,
                                env=self._git_command_env,
                            )
                        finally:
                            if patch_path is not None:
                                patch_path.unlink(missing_ok=True)
            except (
                subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError
            ) as error:
                diagnostics.append(f"could not restore pre-timeout Git HEAD: {error}")

        unusable_marker = git_dir / "repogents-workspace-unusable"
        if diagnostics:
            try:
                unusable_marker.write_text("; ".join(diagnostics), encoding="utf-8")
            except OSError:
                pass
            raise OSError("; ".join(diagnostics))
        unusable_marker.unlink(missing_ok=True)

    @staticmethod
    def _workspace_unusable_markers(workspace: Path) -> tuple[Path, Path]:
        """Return in-workspace and sidecar quarantine markers for one destination.

        Mutation recovery uses the marker inside ``.git``. An initial clone whose
        process tree may still be live cannot safely write inside the destination,
        because a surviving Git helper could replace that partial metadata. The
        sibling sidecar remains outside the clone-owned tree and therefore provides
        a durable reuse boundary without modifying caller-owned destination content.
        """
        return (
            workspace / ".git" / "repogents-workspace-unusable",
            workspace.parent / f".{workspace.name}.repogents-workspace-unusable",
        )

    @classmethod
    def _git_workspace_is_unusable(cls, workspace: Path) -> bool:
        return any(marker.exists() for marker in cls._workspace_unusable_markers(workspace))

    @staticmethod
    def _mark_git_workspace_unusable(workspace: Path, detail: str) -> None:
        """Exclude an unsafe existing workspace from reuse without Git recovery."""
        marker = workspace / ".git" / "repogents-workspace-unusable"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(detail, encoding="utf-8")

    @classmethod
    def _mark_partial_clone_unusable(cls, workspace: Path, detail: str) -> None:
        """Quarantine an unsafe clone destination without touching that destination."""
        _internal, marker = cls._workspace_unusable_markers(workspace)
        try:
            with marker.open("x", encoding="utf-8") as quarantine:
                quarantine.write(detail)
        except FileExistsError:
            # An existing sidecar already excludes reuse. Never overwrite a path in
            # the caller-owned parent merely to refresh the quarantine diagnostic.
            pass

    def _run_mutating_git(
        self, args, *, workspace: Path, env, snapshot: _GitWorkspaceSnapshot | None = None
    ):
        snapshot = snapshot or self._workspace_snapshot(workspace)
        try:
            return self._command_runner(args, cwd=workspace, env=env)
        except subprocess.TimeoutExpired as error:
            if getattr(error, "repogents_git_tree_termination_safe", True) is False:
                # The default runner could not prove its complete owned tree exited.
                # Do not run checkout/reset/rebase-abort, remove locks, or restore a
                # patch while a helper may still mutate the same workspace. Quarantine
                # immediately so another poll or publication cannot concurrently reuse it.
                detail = (
                    "Git process-tree termination could not be confirmed; in-place "
                    "timeout recovery was skipped"
                )
                try:
                    self._mark_git_workspace_unusable(workspace, detail)
                except OSError as marker_error:
                    self._record_recovery_error(
                        error,
                        f"could not mark unsafe Git workspace {workspace} unusable: "
                        f"{marker_error}",
                    )
                raise
            try:
                self._recover_timed_out_git_mutation(workspace, snapshot, args[1])
            except OSError as recovery_error:
                self._record_recovery_error(
                    error,
                    f"could not fully recover Git workspace {workspace}: {recovery_error}",
                )
            raise

    def checkout(
        self,
        github_repository: str,
        target_branch: str,
        workspace: str | Path,
    ) -> Path:
        workspace_path = Path(workspace)
        if self._git_workspace_is_unusable(workspace_path):
            raise RuntimeError(
                f"Git workspace requires recreation after timeout recovery failed: "
                f"{workspace_path}"
            )
        if (workspace_path / ".git").is_dir():
            for args, environment in (
                (["git", "fetch", "origin", target_branch], self._git_auth_env),
                (["git", "checkout", target_branch], self._git_command_env),
                (
                    ["git", "pull", "--ff-only", "origin", target_branch],
                    self._git_auth_env,
                ),
            ):
                if args[1] in {"checkout", "pull"}:
                    self._run_mutating_git(
                        args, workspace=workspace_path, env=environment
                    )
                else:
                    self._command_runner(args, cwd=workspace_path, env=environment)
            return workspace_path
        metadata = self.repository(github_repository)
        workspace_existed = workspace_path.exists() or workspace_path.is_symlink()
        preexisting_workspace_entries: set[Path] | None = None
        if workspace_path.is_dir() and not workspace_path.is_symlink():
            preexisting_workspace_entries = {
                entry.relative_to(workspace_path)
                for entry in workspace_path.rglob("*")
            }
        try:
            self._command_runner(
                [
                    "git",
                    "clone",
                    "--branch",
                    target_branch,
                    "--single-branch",
                    metadata["clone_url"],
                    str(workspace_path),
                ],
                cwd=None,
                env=self._git_auth_env,
            )
        except subprocess.TimeoutExpired as error:
            if getattr(error, "repogents_git_tree_termination_safe", True) is False:
                # A surviving hook/helper may still create, remove, or replace clone
                # metadata. Do not inspect or clean the destination concurrently.
                # Quarantine it with a sibling marker outside Git's destination tree.
                detail = (
                    "Git process-tree termination could not be confirmed; partial "
                    "clone cleanup was skipped"
                )
                try:
                    self._mark_partial_clone_unusable(workspace_path, detail)
                except OSError as marker_error:
                    self._record_recovery_error(
                        error,
                        f"could not quarantine unsafe partial clone {workspace_path}: "
                        f"{marker_error}",
                    )
                raise
            # A timed-out clone can leave enough repository metadata for the next
            # checkout to mistake the destination for a valid existing workspace.
            # A destination created by this attempt is wholly cleanup-owned. When the
            # caller supplied a directory, remove only paths that were absent before
            # clone started so the directory and all caller-owned content survive.
            try:
                if not workspace_existed:
                    if workspace_path.is_symlink() or workspace_path.is_file():
                        workspace_path.unlink(missing_ok=True)
                    elif workspace_path.exists():
                        shutil.rmtree(workspace_path)
                elif preexisting_workspace_entries is not None and workspace_path.is_dir():
                    current_entries = {
                        entry.relative_to(workspace_path)
                        for entry in workspace_path.rglob("*")
                    }
                    clone_created = current_entries - preexisting_workspace_entries
                    cleanup_roots = sorted(
                        (
                            relative
                            for relative in clone_created
                            if not any(parent in clone_created for parent in relative.parents)
                        ),
                        key=lambda relative: len(relative.parts),
                        reverse=True,
                    )
                    for relative in cleanup_roots:
                        created_path = workspace_path / relative
                        if created_path.is_symlink() or created_path.is_file():
                            created_path.unlink(missing_ok=True)
                        elif created_path.exists():
                            shutil.rmtree(created_path)
            except OSError as cleanup_error:
                self._record_recovery_error(
                    error,
                    f"could not remove partial clone workspace {workspace_path}: "
                    f"{cleanup_error}",
                )
            raise
        return workspace_path

    def pull_request(self, github_repository: str, number: int) -> PullRequest:
        path = f"/repos/{github_repository}/pulls/{number}"
        pull = self._request("GET", path)
        diff = self._request("GET", f"{path}.diff")
        if not isinstance(pull, dict):
            raise RuntimeError("GitHub returned an invalid pull request")
        if not isinstance(diff, str):
            raise RuntimeError("GitHub returned an invalid pull request diff")
        return PullRequest(
            number=pull["number"],
            url=pull["html_url"],
            branch=pull["head"]["ref"],
            state=pull["state"],
            merged=bool(pull["merged"]),
            diff=diff,
            head_sha=pull["head"]["sha"],
        )

    def publish(
        self,
        github_repository: str,
        issue_number: int,
        target_branch: str,
        workspace: str | Path,
        existing_pr: int | None = None,
    ) -> PullRequest:
        branch = f"agent/issue-{issue_number}"
        pull_number = existing_pr
        if pull_number is None:
            open_pulls = []
            page = 1
            while True:
                page_pulls = self._request(
                    "GET",
                    f"/repos/{github_repository}/pulls",
                    query={"state": "open", "per_page": 100, "page": page},
                )
                if not isinstance(page_pulls, list):
                    raise RuntimeError("GitHub returned an invalid pulls page")
                open_pulls.extend(page_pulls)
                if len(page_pulls) < 100:
                    break
                page += 1
            for pull in open_pulls:
                head = pull.get("head", {})
                head_repository = (head.get("repo") or {}).get("full_name")
                if (
                    head.get("ref") == branch
                    and pull.get("base", {}).get("ref") == target_branch
                    and (
                        head_repository is None
                        or head_repository == github_repository
                    )
                ):
                    pull_number = pull["number"]
                    break
        workspace_path = Path(workspace)
        if self._git_workspace_is_unusable(workspace_path):
            raise RuntimeError(
                f"Git workspace requires recreation after timeout recovery failed: "
                f"{workspace_path}"
            )
        publication_snapshot = self._publication_workspace_snapshot(workspace_path)
        self._run_mutating_git(
            ["git", "checkout", "-B", branch],
            workspace=workspace_path,
            env=self._git_command_env,
            snapshot=publication_snapshot,
        )
        self._command_runner(
            ["git", "fetch", "origin", target_branch],
            cwd=workspace_path,
            env=self._git_auth_env,
        )
        remote_ref = f"refs/heads/{branch}"
        remote_branch = self._command_runner(
            ["git", "ls-remote", "--heads", "origin", remote_ref],
            cwd=workspace_path,
            env=self._git_auth_env,
        ).stdout.strip()
        expected_head = ""
        if remote_branch:
            fields = remote_branch.split()
            if (
                len(fields) != 2
                or fields[1] != remote_ref
                or len(fields[0]) != 40
                or any(
                    character not in "0123456789abcdefABCDEF"
                    for character in fields[0]
                )
            ):
                raise RuntimeError("git ls-remote returned an invalid branch ref")
            expected_head = fields[0]
        self._command_runner(
            ["git", "add", "--all"],
            cwd=workspace_path,
            env=self._git_command_env,
        )
        pending = self._command_runner(
            ["git", "diff", "--cached", "--name-only"],
            cwd=workspace_path,
            env=self._git_command_env,
        )
        if pending.stdout.strip():
            self._run_mutating_git(
                ["git", "commit", "-m", f"Resolve issue #{issue_number}"],
                workspace=workspace_path,
                env=self._git_identity_env,
            )
        self._run_mutating_git(
            ["git", "rebase", f"origin/{target_branch}"],
            workspace=workspace_path,
            env=self._git_identity_env,
        )
        local_head = self._command_runner(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_path,
            env=self._git_command_env,
        ).stdout.strip()
        if (
            len(local_head) != 40
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in local_head
            )
        ):
            raise RuntimeError(
                "git rev-parse HEAD returned an invalid commit SHA"
            )
        if not expected_head or local_head != expected_head:
            self._command_runner(
                ["git", "reset", "--soft", f"origin/{target_branch}"],
                cwd=workspace_path,
                env=self._git_command_env,
            )
            squashed = self._command_runner(
                ["git", "diff", "--cached", "--name-only"],
                cwd=workspace_path,
                env=self._git_command_env,
            )
            if squashed.stdout.strip():
                self._run_mutating_git(
                    ["git", "commit", "-m", f"Resolve issue #{issue_number}"],
                    workspace=workspace_path,
                    env=self._git_identity_env,
                )
        self._command_runner(
            [
                "git",
                "push",
                f"--force-with-lease={remote_ref}:{expected_head}",
                "--set-upstream",
                "origin",
                branch,
            ],
            cwd=workspace_path,
            env=self._git_auth_env,
        )
        pushed_head = self._command_runner(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_path,
            env=self._git_command_env,
        ).stdout.strip()
        if (
            len(pushed_head) != 40
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in pushed_head
            )
        ):
            raise RuntimeError("git rev-parse HEAD returned an invalid commit SHA")

        if pull_number is None:
            created = self._request(
                "POST",
                f"/repos/{github_repository}/pulls",
                json_body={
                    "title": f"Resolve issue #{issue_number}",
                    "head": branch,
                    "base": target_branch,
                    "body": f"Closes #{issue_number}",
                },
            )
            if not isinstance(created, dict):
                raise RuntimeError("GitHub returned an invalid created pull request")
            pull_number = created["number"]
        pull = self.pull_request(github_repository, pull_number)
        return PullRequest(
            number=pull.number,
            url=pull.url,
            branch=pull.branch,
            state=pull.state,
            merged=pull.merged,
            diff=pull.diff,
            head_sha=pushed_head,
        )

    def list_feedback(
        self,
        github_repository: str,
        pull_number: int,
    ) -> list[GitHubFeedback]:
        repository_path = f"/repos/{github_repository}"
        inline_comments = []
        page = 1
        while True:
            page_comments = self._request(
                "GET",
                f"{repository_path}/pulls/{pull_number}/comments",
                query={"per_page": 100, "page": page},
            )
            if not isinstance(page_comments, list):
                raise RuntimeError(
                    "GitHub returned an invalid review comments page"
                )
            inline_comments.extend(page_comments)
            if len(page_comments) < 100:
                break
            page += 1

        reviews = []
        page = 1
        while True:
            page_reviews = self._request(
                "GET",
                f"{repository_path}/pulls/{pull_number}/reviews",
                query={"per_page": 100, "page": page},
            )
            if not isinstance(page_reviews, list):
                raise RuntimeError("GitHub returned an invalid reviews page")
            reviews.extend(page_reviews)
            if len(page_reviews) < 100:
                break
            page += 1


        inline_items = []
        comment_ids_by_node = {}
        for comment in inline_comments:
            if not isinstance(comment, dict):
                raise RuntimeError("GitHub returned an invalid review comment")
            body = comment.get("body") or ""
            if not isinstance(body, str):
                raise RuntimeError("GitHub returned an invalid review comment body")
            if _FEEDBACK_MARKER_PREFIX in body:
                continue

            comment_id = comment.get("id")
            if type(comment_id) is not int or comment_id <= 0:
                raise RuntimeError("GitHub review comment is missing a valid id")
            node_id = comment.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                raise RuntimeError("GitHub review comment is missing its node_id")
            prior_comment_id = comment_ids_by_node.get(node_id)
            if prior_comment_id is not None and prior_comment_id != comment_id:
                raise RuntimeError(
                    "GitHub review comment node_id maps to multiple REST comments"
                )
            comment_ids_by_node[node_id] = comment_id

            in_reply_to_id = comment.get("in_reply_to_id")
            top_level_comment_id = (
                comment_id if in_reply_to_id is None else in_reply_to_id
            )
            if (
                type(top_level_comment_id) is not int
                or top_level_comment_id <= 0
            ):
                raise RuntimeError(
                    "GitHub review comment is missing a valid reply root"
                )
            inline_items.append(
                (comment, body, node_id, top_level_comment_id)
            )

        thread_ids = (
            self._review_thread_ids(
                github_repository,
                pull_number,
                set(comment_ids_by_node),
            )
            if inline_items
            else {}
        )

        feedback = [
            GitHubFeedback(
                external_id=f"inline:{comment['id']}",
                kind="inline",
                body=body,
                path=comment.get("path"),
                line=comment.get("line"),
                review_thread_id=thread_ids[node_id],
                top_level_comment_id=top_level_comment_id,
            )
            for comment, body, node_id, top_level_comment_id in inline_items
        ]
        for review in reviews:
            if not isinstance(review, dict):
                raise RuntimeError("GitHub returned an invalid pull request review")
            body = review.get("body") or ""
            if not isinstance(body, str):
                raise RuntimeError("GitHub returned an invalid review body")
            if (
                review["state"] == "CHANGES_REQUESTED"
                and _FEEDBACK_MARKER_PREFIX not in body
            ):
                feedback.append(
                    GitHubFeedback(
                        external_id=f"review:{review['id']}",
                        kind="review",
                        body=body,
                    )
                )
        return feedback

    @staticmethod
    def _graphql_data(payload, operation: str) -> dict:
        if not isinstance(payload, dict):
            raise RuntimeError(f"GitHub {operation} returned a non-object response")
        if payload.get("errors"):
            raise RuntimeError(f"GitHub {operation} returned GraphQL errors")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"GitHub {operation} returned no GraphQL data")
        return data

    @staticmethod
    def _map_review_comments_page(
        connection,
        thread_id: str,
        target_node_ids: set[str],
        thread_ids: dict[str, str],
    ) -> tuple[bool, str | None]:
        if not isinstance(connection, dict):
            raise RuntimeError("GitHub review thread has no comments connection")
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise RuntimeError("GitHub review thread comments are missing")
        for comment in nodes:
            if not isinstance(comment, dict):
                raise RuntimeError("GitHub review thread contains an invalid comment")
            comment_id = comment.get("id")
            if not isinstance(comment_id, str) or not comment_id:
                raise RuntimeError("GitHub review thread comment is missing its id")
            if comment_id not in target_node_ids:
                continue
            mapped_thread_id = thread_ids.get(comment_id)
            if (
                mapped_thread_id is not None
                and mapped_thread_id != thread_id
            ):
                raise RuntimeError(
                    "GitHub review comment maps to multiple review threads"
                )
            thread_ids[comment_id] = thread_id

        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict):
            raise RuntimeError(
                "GitHub review thread comments are missing pageInfo"
            )
        has_next_page = page_info.get("hasNextPage")
        if type(has_next_page) is not bool:
            raise RuntimeError(
                "GitHub review thread comments have invalid pagination"
            )
        end_cursor = page_info.get("endCursor")
        if has_next_page and (
            not isinstance(end_cursor, str) or not end_cursor
        ):
            raise RuntimeError(
                "GitHub review thread comments have no next-page cursor"
            )
        return has_next_page, end_cursor

    def _review_thread_ids(
        self,
        github_repository: str,
        pull_number: int,
        target_node_ids: set[str],
    ) -> dict[str, str]:
        repository_parts = github_repository.split("/")
        if (
            len(repository_parts) != 2
            or not repository_parts[0]
            or not repository_parts[1]
        ):
            raise ValueError("github_repository must be in owner/name form")
        owner, name = repository_parts

        thread_ids = {}
        seen_thread_ids = set()
        seen_thread_cursors = set()
        after = None
        while True:
            payload = self._request(
                "POST",
                "/graphql",
                json_body={
                    "query": _REVIEW_THREADS_QUERY,
                    "variables": {
                        "owner": owner,
                        "name": name,
                        "number": pull_number,
                        "after": after,
                    },
                },
            )
            data = self._graphql_data(payload, "ReviewThreads")
            repository = data.get("repository")
            if not isinstance(repository, dict):
                raise RuntimeError("GitHub repository was missing from ReviewThreads")
            pull_request = repository.get("pullRequest")
            if not isinstance(pull_request, dict):
                raise RuntimeError(
                    "GitHub pull request was missing from ReviewThreads"
                )
            connection = pull_request.get("reviewThreads")
            if not isinstance(connection, dict):
                raise RuntimeError("GitHub reviewThreads connection is missing")
            threads = connection.get("nodes")
            if not isinstance(threads, list):
                raise RuntimeError("GitHub reviewThreads nodes are missing")

            for thread in threads:
                if not isinstance(thread, dict):
                    raise RuntimeError("GitHub returned an invalid review thread")
                thread_id = thread.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise RuntimeError("GitHub review thread is missing its id")
                if thread_id in seen_thread_ids:
                    raise RuntimeError(
                        "GitHub returned the same review thread more than once"
                    )
                seen_thread_ids.add(thread_id)
                if type(thread.get("isResolved")) is not bool:
                    raise RuntimeError(
                        "GitHub review thread has invalid resolution state"
                    )
                if type(thread.get("viewerCanResolve")) is not bool:
                    raise RuntimeError(
                        "GitHub review thread has invalid resolution capability"
                    )

                has_more_comments, comments_after = (
                    self._map_review_comments_page(
                        thread.get("comments"),
                        thread_id,
                        target_node_ids,
                        thread_ids,
                    )
                )
                seen_comment_cursors = set()
                while has_more_comments:
                    if comments_after in seen_comment_cursors:
                        raise RuntimeError(
                            "GitHub repeated a review-comment page cursor"
                        )
                    seen_comment_cursors.add(comments_after)
                    comments_payload = self._request(
                        "POST",
                        "/graphql",
                        json_body={
                            "query": _REVIEW_THREAD_COMMENTS_QUERY,
                            "variables": {
                                "threadId": thread_id,
                                "after": comments_after,
                            },
                        },
                    )
                    comments_data = self._graphql_data(
                        comments_payload,
                        "ReviewThreadComments",
                    )
                    comments_thread = comments_data.get("node")
                    if not isinstance(comments_thread, dict):
                        raise RuntimeError(
                            "GitHub review thread was missing while paginating comments"
                        )
                    if comments_thread.get("id") != thread_id:
                        raise RuntimeError(
                            "GitHub returned comments for a different review thread"
                        )
                    has_more_comments, comments_after = (
                        self._map_review_comments_page(
                            comments_thread.get("comments"),
                            thread_id,
                            target_node_ids,
                            thread_ids,
                        )
                    )

            page_info = connection.get("pageInfo")
            if not isinstance(page_info, dict):
                raise RuntimeError("GitHub reviewThreads pageInfo is missing")
            has_next_page = page_info.get("hasNextPage")
            if type(has_next_page) is not bool:
                raise RuntimeError("GitHub reviewThreads pagination is invalid")
            if not has_next_page:
                break
            after = page_info.get("endCursor")
            if not isinstance(after, str) or not after:
                raise RuntimeError(
                    "GitHub reviewThreads has no next-page cursor"
                )
            if after in seen_thread_cursors:
                raise RuntimeError("GitHub repeated a review-thread page cursor")
            seen_thread_cursors.add(after)

        missing_node_ids = target_node_ids.difference(thread_ids)
        if missing_node_ids:
            raise RuntimeError(
                "GitHub review comments could not be mapped to review threads"
            )
        return thread_ids

    @staticmethod
    def _validate_address_feedback_inputs(
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
        head_sha: str,
    ) -> None:
        repository_parts = (
            github_repository.split("/")
            if isinstance(github_repository, str)
            else []
        )
        if (
            len(repository_parts) != 2
            or not repository_parts[0]
            or not repository_parts[1]
            or github_repository.strip() != github_repository
        ):
            raise ValueError("github_repository must be in owner/name form")
        if type(pull_number) is not int or pull_number <= 0:
            raise ValueError("pull_number must be a positive integer")
        if not isinstance(feedback, GitHubFeedback):
            raise TypeError("feedback must be a GitHubFeedback")
        if feedback.kind not in {"inline", "review"}:
            raise ValueError("feedback kind is not addressable")
        external_id_prefix = f"{feedback.kind}:"
        if (
            not isinstance(feedback.external_id, str)
            or not feedback.external_id.startswith(external_id_prefix)
        ):
            raise ValueError("feedback external_id does not match its kind")
        numeric_id = feedback.external_id.removeprefix(external_id_prefix)
        if (
            not numeric_id
            or not numeric_id.isascii()
            or not numeric_id.isdigit()
            or int(numeric_id) <= 0
        ):
            raise ValueError("feedback external_id is invalid")
        if (
            not isinstance(head_sha, str)
            or len(head_sha) != 40
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in head_sha
            )
        ):
            raise ValueError("head_sha must be a full hexadecimal commit SHA")

        if feedback.kind == "inline":
            if (
                not isinstance(feedback.review_thread_id, str)
                or not feedback.review_thread_id
            ):
                raise ValueError("inline feedback requires a review thread id")
            if (
                type(feedback.top_level_comment_id) is not int
                or feedback.top_level_comment_id <= 0
            ):
                raise ValueError(
                    "inline feedback requires a top-level review comment id"
                )
        elif (
            feedback.review_thread_id is not None
            or feedback.top_level_comment_id is not None
        ):
            raise ValueError("non-thread feedback cannot carry thread identity")

    def address_feedback(
        self,
        github_repository: str,
        pull_number: int,
        feedback: GitHubFeedback,
        head_sha: str,
    ) -> FeedbackAddress:
        self._validate_address_feedback_inputs(
            github_repository,
            pull_number,
            feedback,
            head_sha,
        )
        marker = f"{_FEEDBACK_MARKER_PREFIX}{feedback.external_id} -->"
        acknowledgement = (
            f"Addressed in validated commit `{head_sha}`.\n\n{marker}"
        )
        if feedback.kind == "inline":
            comments_path = (
                f"/repos/{github_repository}/pulls/{pull_number}/comments"
            )
        else:
            comments_path = (
                f"/repos/{github_repository}/issues/{pull_number}/comments"
            )

        response_url = None
        page = 1
        while True:
            page_comments = self._request(
                "GET",
                comments_path,
                query={"per_page": 100, "page": page},
            )
            if not isinstance(page_comments, list):
                raise RuntimeError("GitHub returned an invalid comment collection")
            for comment in page_comments:
                if not isinstance(comment, dict):
                    raise RuntimeError("GitHub returned an invalid comment")
                body = comment.get("body") or ""
                if not isinstance(body, str):
                    raise RuntimeError("GitHub returned an invalid comment body")
                if marker not in body:
                    continue
                if body != acknowledgement:
                    raise RuntimeError(
                        "GitHub acknowledgement does not match "
                        "the current commit"
                    )
                if response_url is not None:
                    continue
                candidate_url = comment.get("html_url")
                if not isinstance(candidate_url, str) or not candidate_url:
                    raise RuntimeError(
                        "GitHub acknowledgement is missing its response URL"
                    )
                response_url = candidate_url
            if len(page_comments) < 100:
                break
            page += 1

        if response_url is None:
            post_path = comments_path
            if feedback.kind == "inline":
                post_path = (
                    f"{comments_path}/{feedback.top_level_comment_id}/replies"
                )
            created = self._request(
                "POST",
                post_path,
                json_body={"body": acknowledgement},
            )
            if not isinstance(created, dict):
                raise RuntimeError("GitHub returned no acknowledgement")
            if created.get("body") != acknowledgement:
                raise RuntimeError(
                    "GitHub returned a mismatched acknowledgement"
                )
            candidate_url = created.get("html_url")
            if not isinstance(candidate_url, str) or not candidate_url:
                raise RuntimeError(
                    "GitHub acknowledgement is missing its response URL"
                )
            response_url = candidate_url

        if feedback.kind != "inline":
            return FeedbackAddress(
                status="ACKNOWLEDGED",
                response_url=response_url,
            )

        thread_id = feedback.review_thread_id
        state_payload = self._request(
            "POST",
            "/graphql",
            json_body={
                "query": _REVIEW_THREAD_QUERY,
                "variables": {"threadId": thread_id},
            },
        )
        state_data = self._graphql_data(state_payload, "ReviewThread")
        thread = state_data.get("node")
        if not isinstance(thread, dict):
            raise RuntimeError("GitHub review thread state is missing")
        if thread.get("id") != thread_id:
            raise RuntimeError("GitHub returned a different review thread")
        is_resolved = thread.get("isResolved")
        viewer_can_resolve = thread.get("viewerCanResolve")
        if type(is_resolved) is not bool:
            raise RuntimeError("GitHub review thread has invalid resolution state")
        if type(viewer_can_resolve) is not bool:
            raise RuntimeError(
                "GitHub review thread has invalid resolution capability"
            )

        if not is_resolved:
            if not viewer_can_resolve:
                raise RuntimeError(
                    "GitHub viewer cannot resolve the review thread"
                )
            resolution_payload = self._request(
                "POST",
                "/graphql",
                json_body={
                    "query": _RESOLVE_THREAD_MUTATION,
                    "variables": {"input": {"threadId": thread_id}},
                },
            )
            resolution_data = self._graphql_data(
                resolution_payload,
                "ResolveThread",
            )
            result = resolution_data.get("resolveReviewThread")
            if not isinstance(result, dict):
                raise RuntimeError(
                    "GitHub returned no review-thread resolution result"
                )
            resolved_thread = result.get("thread")
            if not isinstance(resolved_thread, dict):
                raise RuntimeError(
                    "GitHub returned no resolved review thread"
                )
            if resolved_thread.get("id") != thread_id:
                raise RuntimeError(
                    "GitHub resolved a different review thread"
                )
            if resolved_thread.get("isResolved") is not True:
                raise RuntimeError(
                    "GitHub did not confirm review-thread resolution"
                )

        return FeedbackAddress(
            status="RESOLVED",
            response_url=response_url,
        )
