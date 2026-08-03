from __future__ import annotations

import builtins
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from repogents.service_ownership import (
    ServiceOwnership,
    ServiceOwnershipUnavailableError,
)


def test_service_ownership_is_exclusive_and_reacquirable(tmp_path):
    path = tmp_path / ".repogents-service.lock"
    owner = ServiceOwnership(path)
    competitor = ServiceOwnership(path)

    owner.acquire()
    assert owner.acquired is True
    with pytest.raises(ServiceOwnershipUnavailableError):
        competitor.acquire()
    assert competitor.acquired is False

    owner.close()
    assert owner.acquired is False
    competitor.acquire()
    assert competitor.acquired is True
    competitor.close()



def test_service_ownership_excludes_competing_process_and_releases(tmp_path):
    """The boundary is process-exclusive and a later process can reacquire it."""
    path = tmp_path / ".repogents-service.lock"
    owner = ServiceOwnership(path)
    owner.acquire()
    program = """
import sys
from repogents.service_ownership import ServiceOwnership, ServiceOwnershipUnavailableError

ownership = ServiceOwnership(sys.argv[1])
try:
    ownership.acquire()
except ServiceOwnershipUnavailableError:
    print("unavailable")
else:
    print("acquired")
    ownership.close()
"""

    blocked = subprocess.run(
        [sys.executable, "-c", program, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert blocked.stdout.strip() == "unavailable"

    owner.close()
    reacquired = subprocess.run(
        [sys.executable, "-c", program, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert reacquired.stdout.strip() == "acquired"

def test_service_ownership_module_loads_without_fcntl_on_windows(tmp_path):
    """The Windows branch must never import the Unix-only locking module."""
    application_source = (
        Path(__file__).resolve().parents[1] / "repogents" / "application.py"
    ).read_text(encoding="utf-8")
    assert "import fcntl" not in application_source
    assert "from fcntl" not in application_source

    source = (
        Path(__file__).resolve().parents[1]
        / "repogents"
        / "service_ownership.py"
    ).read_text(encoding="utf-8")
    real_import = builtins.__import__
    locking_calls = []
    fake_os = types.SimpleNamespace(name="nt", SEEK_END=os.SEEK_END)
    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=1,
        LK_UNLCK=2,
        locking=lambda fd, mode, length: locking_calls.append((fd, mode, length)),
    )

    def platform_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "os":
            return fake_os
        if name == "msvcrt":
            return fake_msvcrt
        if name == "fcntl":
            raise AssertionError("the Windows ownership branch imported fcntl")
        return real_import(name, globals, locals, fromlist, level)

    namespace = {
        "__name__": "portable_service_ownership_test_module",
        "__builtins__": {**vars(builtins), "__import__": platform_import},
    }
    exec(compile(source, "service_ownership.py", "exec"), namespace)

    ownership = namespace["ServiceOwnership"](tmp_path / "windows.lock")
    ownership.acquire()
    ownership.close()
    assert [mode for _, mode, _ in locking_calls] == [
        fake_msvcrt.LK_NBLCK,
        fake_msvcrt.LK_UNLCK,
    ]
    assert all(length == 1 for _, _, length in locking_calls)
