from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


_CREATE_RULESET = 444
_ADD_RULE = 445
_RESTRICT_SELF = 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13
_TRUNCATE = 1 << 14
_IOCTL_DEV = 1 << 15
_READ_ACCESS = _EXECUTE | _READ_FILE | _READ_DIR
_BASE_WRITE_ACCESS = (
    _READ_ACCESS
    | _WRITE_FILE
    | _REMOVE_DIR
    | _REMOVE_FILE
    | _MAKE_CHAR
    | _MAKE_DIR
    | _MAKE_REG
    | _MAKE_SOCK
    | _MAKE_FIFO
    | _MAKE_BLOCK
    | _MAKE_SYM
)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _checked_syscall(libc, number: int, *args) -> int:
    result = libc.syscall(number, *args)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result)


def _handled_access(abi: int) -> int:
    access = _BASE_WRITE_ACCESS
    if abi >= 2:
        access |= _REFER
    if abi >= 3:
        access |= _TRUNCATE
    if abi >= 5:
        access |= _IOCTL_DEV
    return access


def _restrict_filesystem(read_paths: tuple[Path, ...], write_paths: tuple[Path, ...]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    abi = _checked_syscall(
        libc,
        _CREATE_RULESET,
        ctypes.c_void_p(),
        ctypes.c_size_t(0),
        ctypes.c_uint(_CREATE_RULESET_VERSION),
    )
    handled = _handled_access(abi)
    ruleset_attr = _RulesetAttr(handled_access_fs=handled)
    ruleset_fd = _checked_syscall(
        libc,
        _CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        ctypes.c_uint(0),
    )
    try:
        for path, access in (
            *((path, _READ_ACCESS) for path in read_paths),
            *((path, handled) for path in write_paths),
        ):
            if not path.exists():
                continue
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                attr = _PathBeneathAttr(access & handled, path_fd)
                _checked_syscall(
                    libc,
                    _ADD_RULE,
                    ruleset_fd,
                    _RULE_PATH_BENEATH,
                    ctypes.byref(attr),
                    ctypes.c_uint(0),
                )
            finally:
                os.close(path_fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        _checked_syscall(libc, _RESTRICT_SELF, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)


def main(argv: list[str]) -> None:
    try:
        separator = argv.index("--")
        options = argv[:separator]
        command = argv[separator + 1 :]
        values = dict(zip(options[::2], options[1::2], strict=True))
        workspace = Path(values["--workspace"]).resolve()
        temp = Path(values["--temp"]).resolve()
        command_cwd = Path(values["--cwd"]).resolve()
    except (ValueError, KeyError) as exc:
        raise SystemExit(f"invalid Landlock sandbox invocation: {exc}") from exc
    if not command:
        raise SystemExit("invalid Landlock sandbox invocation: command is required")
    if command_cwd != workspace and workspace not in command_cwd.parents:
        raise SystemExit("invalid Landlock sandbox invocation: cwd escapes workspace")

    read_paths = tuple(
        path.resolve()
        for path in map(Path, ("/usr", "/bin", "/lib", "/lib64", "/etc", "/dev", "/proc"))
        if path.exists()
    )
    _restrict_filesystem(read_paths, (workspace, temp))
    os.chdir(command_cwd)
    os.execv(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
