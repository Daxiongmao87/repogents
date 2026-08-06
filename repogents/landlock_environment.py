from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from minisweagent.exceptions import Submitted
from minisweagent.utils.serialize import recursive_merge


class LandlockEnvironment:
    """mini-swe-agent environment with unprivileged filesystem isolation."""

    def __init__(
        self, *, cwd: str, timeout: int = 30, internet_access: bool = False
    ):
        self.cwd = Path(cwd).resolve()
        self.timeout = timeout
        self.internet_access = internet_access
        self.working_dir = Path(tempfile.gettempdir()) / f"repogents-{uuid.uuid4().hex[:8]}"
        self.working_dir.mkdir(parents=True, exist_ok=True)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        command_cwd = Path(cwd).resolve() if cwd else self.cwd
        if command_cwd != self.cwd and self.cwd not in command_cwd.parents:
            return {
                "output": "",
                "returncode": -1,
                "exception_info": "command cwd must be inside the workspace",
            }
        env = {
            "PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/bin",
            "HOME": str(self.working_dir),
            "TMPDIR": str(self.working_dir),
            "XDG_CACHE_HOME": str(self.working_dir / ".cache"),
            "XDG_CONFIG_HOME": str(self.working_dir / ".config"),
            "XDG_DATA_HOME": str(self.working_dir / ".local" / "share"),
            "XDG_RUNTIME_DIR": str(self.working_dir / ".runtime"),
        }

        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("landlock_runner.py").resolve()),
                    "--workspace",
                    str(self.cwd),
                    "--temp",
                    str(self.working_dir),
                    "--cwd",
                    str(command_cwd),
                    "--internet",
                    "enabled" if self.internet_access else "disabled",
                    "--",
                    "/bin/bash",
                    "-c",
                    command,
                ],
                cwd=command_cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                output, _ = process.communicate(timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate()
                raise subprocess.TimeoutExpired(command, timeout or self.timeout, output=output)
            result = {"output": output, "returncode": process.returncode, "exception_info": ""}
        except Exception as exc:
            raw_output = getattr(exc, "output", None)
            result = {
                "output": raw_output or "",
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {exc}",
            }
        self._check_finished(result, command)
        return result

    @staticmethod
    def _check_finished(output: dict, command: str) -> None:
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if (
            command.strip() == "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
            and lines
            and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
            and output["returncode"] == 0
        ):
            submission = "".join(lines[1:])
            raise Submitted(
                {
                    "role": "exit",
                    "content": submission,
                    "extra": {"exit_status": "Submitted", "submission": submission},
                }
            )

    def cleanup(self) -> None:
        if self.working_dir.exists():
            shutil.rmtree(self.working_dir)

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(
            {"cwd": str(self.cwd), "timeout": self.timeout},
            platform.uname()._asdict(),
            kwargs,
        )

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "environment": {
                        "cwd": str(self.cwd),
                        "timeout": self.timeout,
                        "internet_access": self.internet_access,
                    },
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }
