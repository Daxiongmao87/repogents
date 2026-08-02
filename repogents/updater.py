from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


_OFFLINE_EDITABLE_BACKEND = r"""import json
import zipfile
from pathlib import Path


def get_requires_for_build_editable(config_settings=None):
    return []


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    project = json.loads(Path("refresh.json").read_text(encoding="utf-8"))
    normalized = project["name"].replace("-", "_")
    wheel_version = project["normalized_version"]
    distribution = f"{normalized}-{wheel_version}.dist-info"
    filename = f"{normalized}-{wheel_version}-py3-none-any.whl"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
    ]
    if project.get("requires_python"):
        metadata.append(f"Requires-Python: {project['requires_python']}")
    metadata.extend(f"Requires-Dist: {item}" for item in project["dependencies"])
    entries = ["[console_scripts]"]
    entries.extend(
        f"{name} = {target}" for name, target in project["scripts"].items()
    )
    with zipfile.ZipFile(Path(wheel_directory) / filename, "w") as archive:
        archive.writestr(f"{normalized}-editable.pth", project["source"] + "\n")
        archive.writestr(
            f"{distribution}/METADATA", "\n".join(metadata) + "\n"
        )
        archive.writestr(
            f"{distribution}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: repogents-updater\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{distribution}/entry_points.txt", "\n".join(entries) + "\n"
        )
        archive.writestr(f"{distribution}/RECORD", "")
    return filename
"""


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
        self.refresh_state_file = self.state_file.with_name(
            f"{self.state_file.name}.installation-refreshed"
        )
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

        restarted_commit = self._read_restarted_commit()
        refreshed_commit = self._read_checkpoint(self.refresh_state_file)
        refreshed_now = False

        if refreshed_commit != local_sha:
            # A legacy restart checkpoint does not prove that installation
            # metadata and runtime dependencies were reconciled. Refresh first
            # so a refresh failure preserves the last verified restart, then
            # invalidate matching legacy evidence before recording the refresh.
            # This ordering is failure-safe: a crash before invalidation repeats
            # the refresh, while a crash afterwards leaves the restart retryable.
            self._refresh_installation_metadata()
            if restarted_commit == local_sha:
                self._remove_checkpoint(self.state_file)
                restarted_commit = None
            self._write_checkpoint(self.refresh_state_file, local_sha)
            refreshed_now = True

        if restarted_commit == local_sha and not refreshed_now:
            print(
                f"repogents-updater: {self.branch} is current at {local_sha}; "
                "installation refresh and restart already recorded",
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

    def _refresh_installation_metadata(self) -> None:
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python 3.10 compatibility
            from pip._vendor import tomli as tomllib

        project = tomllib.loads(
            (self.repository / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        from pip._vendor.packaging.version import Version

        metadata = {
            "name": project["name"],
            "version": project["version"],
            "normalized_version": str(Version(project["version"])),
            "requires_python": project.get("requires-python"),
            "dependencies": project.get("dependencies", []),
            "scripts": project.get("scripts", {}),
            "source": str(self.repository),
        }

        # Pip records the editable target in direct_url.json, so keep the
        # backend beside the durable updater checkpoint rather than in a
        # temporary directory. Its generated .pth resolves imports from the
        # fast-forwarded repository checkout recorded in metadata.
        build_root = self.state_file.parent / "editable-refresh"
        build_root.mkdir(parents=True, exist_ok=True)
        (build_root / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = []\n"
            "build-backend = 'refresh_backend'\n"
            "backend-path = ['.']\n",
            encoding="utf-8",
        )
        (build_root / "refresh.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        (build_root / "refresh_backend.py").write_text(
            _OFFLINE_EDITABLE_BACKEND, encoding="utf-8"
        )

        commands = (
            (
                "refresh editable Repogents installation",
                ["--force-reinstall", "--no-deps"],
            ),
            ("reconcile Repogents runtime dependencies", []),
        )
        for operation, options in commands:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    *options,
                    "--editable",
                    str(build_root),
                ],
                cwd=self.repository,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise UpdateCommandFailed(operation, result.returncode)

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
        return self._read_checkpoint(self.state_file)

    @staticmethod
    def _read_checkpoint(checkpoint: Path) -> str | None:
        try:
            value = checkpoint.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _write_restarted_commit(self, commit_sha: str) -> None:
        self._write_checkpoint(self.state_file, commit_sha)

    @staticmethod
    def _remove_checkpoint(checkpoint: Path) -> None:
        try:
            checkpoint.unlink()
        except FileNotFoundError:
            return
        parent = checkpoint.parent
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    @staticmethod
    def _write_checkpoint(checkpoint: Path, commit_sha: str) -> None:
        parent = checkpoint.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent,
            prefix=f".{checkpoint.name}.",
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{commit_sha}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, checkpoint)
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
