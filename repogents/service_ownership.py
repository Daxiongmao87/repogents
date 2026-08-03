"""Cross-platform exclusive ownership of a Repogents data directory.

The service listener is bound before this primitive is acquired. Holding the lock
therefore grants the process authority to run startup recovery and mutate durable
state for the data directory. The lock is released by :meth:`close` and by the
operating system if the process terminates while its file handle is open.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":  # pragma: win32 cover
    import msvcrt
else:  # pragma: posix cover
    import fcntl


class ServiceOwnershipUnavailableError(RuntimeError):
    """Another process already owns the data-directory service boundary."""


class ServiceOwnership:
    """Hold one non-blocking, process-exclusive lock file until closed."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        if self._file is not None:
            return
        ownership_file = self.path.open("a+b")
        try:
            self._lock(ownership_file)
        except OSError as error:
            ownership_file.close()
            if error.errno in (
                errno.EACCES,
                errno.EAGAIN,
                getattr(errno, "EDEADLK", errno.EACCES),
            ):
                raise ServiceOwnershipUnavailableError(
                    f"service ownership is already held for {self.path.parent}"
                ) from error
            raise
        self._file = ownership_file

    def close(self) -> None:
        ownership_file = self._file
        self._file = None
        if ownership_file is None:
            return
        try:
            self._unlock(ownership_file)
        finally:
            ownership_file.close()

    @staticmethod
    def _lock(ownership_file: BinaryIO) -> None:
        if os.name == "nt":  # pragma: win32 cover
            # msvcrt.locking locks a byte range from the current position. Ensure
            # the shared lock file owns one byte, then lock that byte without waiting.
            ownership_file.seek(0, os.SEEK_END)
            if ownership_file.tell() == 0:
                ownership_file.write(b"\0")
                ownership_file.flush()
            ownership_file.seek(0)
            msvcrt.locking(ownership_file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        fcntl.flock(ownership_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(ownership_file: BinaryIO) -> None:
        if os.name == "nt":  # pragma: win32 cover
            ownership_file.seek(0)
            msvcrt.locking(ownership_file.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(ownership_file.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> "ServiceOwnership":
        self.acquire()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()
