from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


class UpdateRefused(RuntimeError):
    """Raised when updating would overwrite or diverge from local work."""


class UpdateCommandFailed(RuntimeError):
    """Raised when a Git command fails without exposing captured output."""

    def __init__(self, operation: str, returncode: int) -> None:
        super().__init__(f"{operation} failed with exit code {returncode}")
        self.operation = operation
        self.returncode = returncode


@dataclass(frozen=True)
class UpdateOutcome:
    commit_sha: str
    updated: bool
    restarted: bool


class ServiceController(Protocol):
    def restart_and_verify(self, service_name: str) -> None: ...


class SystemdServiceController:
    def __init__(self, executable: str = "/usr/bin/systemctl") -> None:
        self.executable = executable

    def restart_and_verify(self, service_name: str) -> None:
        if not service_name or service_name.startswith("-"):
            raise ValueError(
                "service name must not be empty or begin with '-'"
            )
        subprocess.run(
            [self.executable, "--user", "restart", service_name],
            check=True,
        )
        subprocess.run(
            [self.executable, "--user", "is-active", "--quiet", service_name],
            check=True,
        )


class MainBranchUpdater:
    def __init__(
        self,
        *,
        repository: Path,
        state_file: Path,
        service_name: str,
        remote: str = "origin",
        branch: str = "main",
        service_controller: ServiceController | None = None,
    ) -> None:
        self.repository = repository.resolve()
        self.state_file = state_file.expanduser().resolve()
        self.service_name = service_name
        self.remote = remote
        self.branch = branch
        self.service_controller = (
            service_controller or SystemdServiceController()
        )

    def check_once(self) -> UpdateOutcome:
        self._require_safe_checkout()
        remote_ref = f"refs/remotes/{self.remote}/{self.branch}"
        self._git(
            "fetch",
            "--quiet",
            "--no-tags",
            self.remote,
            f"refs/heads/{self.branch}:{remote_ref}",
        )
        self._require_safe_checkout()

        local_sha = self._revision("HEAD")
        remote_sha = self._revision(remote_ref)
        updated = False
        if local_sha != remote_sha:
            ancestry = self._git_result(
                "merge-base",
                "--is-ancestor",
                local_sha,
                remote_sha,
            )
            if ancestry.returncode == 1:
                raise UpdateRefused(
                    f"{self.remote}/{self.branch} is not a fast-forward "
                    f"from local {self.branch}"
                )
            if ancestry.returncode != 0:
                raise UpdateCommandFailed(
                    "git merge-base", ancestry.returncode
                )
            self._git("merge", "--ff-only", remote_sha)
            local_sha = self._revision("HEAD")
            if local_sha != remote_sha:
                raise UpdateCommandFailed("verify fast-forward", 1)
            updated = True

        if self._read_restarted_commit() == local_sha:
            print(
                f"repogents-updater: {self.branch} is current at {local_sha}; "
                "restart already recorded",
                flush=True,
            )
            return UpdateOutcome(
                commit_sha=local_sha,
                updated=updated,
                restarted=False,
            )

        self.service_controller.restart_and_verify(self.service_name)
        self._write_restarted_commit(local_sha)
        print(
            f"repogents-updater: restarted {self.service_name} at {local_sha}",
            flush=True,
        )
        return UpdateOutcome(
            commit_sha=local_sha,
            updated=updated,
            restarted=True,
        )

    def _require_safe_checkout(self) -> None:
        branch = self._git_result(
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        )
        if branch.returncode != 0:
            raise UpdateRefused(
                f"checkout is detached; expected branch '{self.branch}'"
            )
        current_branch = branch.stdout.strip()
        if current_branch != self.branch:
            raise UpdateRefused(
                f"checked out branch is '{current_branch}'; "
                f"expected '{self.branch}'"
            )
        status = self._git(
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        )
        if status.stdout:
            raise UpdateRefused("checkout has tracked changes")

    def _revision(self, revision: str) -> str:
        return self._git("rev-parse", "--verify", revision).stdout.strip()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = self._git_result(*arguments)
        if result.returncode != 0:
            operation = f"git {arguments[0]}" if arguments else "git"
            raise UpdateCommandFailed(operation, result.returncode)
        return result

    def _git_result(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=False,
            capture_output=True,
            text=True,
        )

    def _read_restarted_commit(self) -> str | None:
        try:
            value = self.state_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _write_restarted_commit(self, commit_sha: str) -> None:
        parent = self.state_file.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent,
            prefix=f".{self.state_file.name}.",
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{commit_sha}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_file)
            directory_descriptor = os.open(
                parent, os.O_RDONLY | os.O_DIRECTORY
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fast-forward Repogents main and restart its user service."
    )
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--service", default="repogents.service")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    updater = MainBranchUpdater(
        repository=arguments.repository,
        state_file=arguments.state_file,
        remote=arguments.remote,
        branch=arguments.branch,
        service_name=arguments.service,
    )
    try:
        updater.check_once()
    except UpdateRefused as error:
        print(f"repogents-updater: refused: {error}", file=sys.stderr)
        return 2
    except UpdateCommandFailed as error:
        print(f"repogents-updater: failed: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print(
            "repogents-updater: service restart or health check failed",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(
            f"repogents-updater: failed with {error.__class__.__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
