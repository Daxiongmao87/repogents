from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path


_MAX_READ_BYTES = 256_000
_MAX_RESULTS = 500
_SKIP_DIRECTORIES = {".git", "node_modules", "target", "dist", "build", ".venv", "venv"}


def _integer_argument(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _safe_path(relative: object, *, must_exist: bool = False) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("path must be a nonempty relative string")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ValueError("path must be relative to the isolated checkout")
    root = Path.cwd().resolve()
    resolved = (root / candidate).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("path escapes the isolated checkout")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(resolved)
    return resolved


def _list(action: dict[str, object]) -> object:
    path = _safe_path(action.get("path", "."), must_exist=True)
    if not path.is_dir():
        raise ValueError("list path is not a directory")
    values = []
    for child in sorted(path.iterdir(), key=lambda value: value.name.lower())[:_MAX_RESULTS]:
        stat = child.stat()
        values.append(
            {
                "name": child.name,
                "path": child.relative_to(Path.cwd()).as_posix(),
                "type": "directory" if child.is_dir() else "file",
                "size": stat.st_size,
            }
        )
    return values


def _read(action: dict[str, object]) -> object:
    path = _safe_path(action.get("path"), must_exist=True)
    if not path.is_file():
        raise ValueError("read path is not a file")
    data = path.read_bytes()
    if len(data) > _MAX_READ_BYTES:
        raise ValueError(f"file exceeds {_MAX_READ_BYTES} byte read limit")
    if b"\x00" in data:
        raise ValueError("binary file cannot be submitted to the model")
    text = data.decode("utf-8")
    lines = text.splitlines()
    start = _integer_argument(action.get("start", 1), "start")
    end = _integer_argument(
        action.get("end", min(len(lines), 1000)), "end"
    )
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    selected = lines[start - 1 : end]
    return {
        "path": path.relative_to(Path.cwd()).as_posix(),
        "start": start,
        "end": start + len(selected) - 1,
        "total_lines": len(lines),
        "content": "\n".join(f"{number}:{line}" for number, line in enumerate(selected, start=start)),
    }


def _search(action: dict[str, object]) -> object:
    root = _safe_path(action.get("path", "."), must_exist=True)
    pattern_value = action.get("pattern")
    if not isinstance(pattern_value, str) or not pattern_value:
        raise ValueError("search pattern must be a nonempty string")
    pattern = re.compile(pattern_value)
    matches: list[dict[str, object]] = []
    paths: list[Path]
    if root.is_file():
        paths = [root]
    else:
        paths = []
        for directory, names, files in os.walk(root):
            names[:] = sorted(name for name in names if name not in _SKIP_DIRECTORIES)
            paths.extend(Path(directory) / name for name in sorted(files))
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > _MAX_READ_BYTES or b"\x00" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                matches.append(
                    {
                        "path": path.relative_to(Path.cwd()).as_posix(),
                        "line": number,
                        "content": line,
                    }
                )
                if len(matches) >= _MAX_RESULTS:
                    return matches
    return matches


def _write(action: dict[str, object]) -> object:
    path = _safe_path(action.get("path"))
    content = action.get("content")
    if not isinstance(content, str):
        raise ValueError("write content must be a string")
    encoded = content.encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded:
        raise ValueError("write content is unchanged")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return {
        "path": path.relative_to(Path.cwd()).as_posix(),
        "bytes": len(encoded),
        "changed": True,
    }


def _replace(action: dict[str, object]) -> object:
    path = _safe_path(action.get("path"), must_exist=True)
    old = action.get("old")
    new = action.get("new")
    count = _integer_argument(action.get("count", 1), "count")
    if (
        not isinstance(old, str)
        or not old
        or not isinstance(new, str)
        or old == new
        or count < 1
    ):
        raise ValueError(
            "replace requires distinct nonempty old text, string new text, and positive count"
        )
    text = path.read_text(encoding="utf-8")
    occurrences = text.count(old)
    if occurrences != count:
        raise ValueError(f"replace expected {count} occurrence(s), found {occurrences}")
    path.write_text(text.replace(old, new, count), encoding="utf-8")
    return {
        "path": path.relative_to(Path.cwd()).as_posix(),
        "replacements": count,
        "changed": True,
    }


def execute(action: dict[str, object]) -> object:
    name = action.get("action")
    if name == "list":
        return _list(action)
    if name == "read":
        return _read(action)
    if name == "search":
        return _search(action)
    if name == "write":
        return _write(action)
    if name == "replace":
        return _replace(action)
    raise ValueError(f"unsupported repository tool action: {name}")


def main() -> int:
    if len(sys.argv) != 2:
        print("repository_tools.py expects one base64url JSON action", file=sys.stderr)
        return 2
    try:
        encoded = sys.argv[1].encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        action = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if not isinstance(action, dict):
            raise ValueError("repository tool action must be an object")
        result = execute(action)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as error:
        print(f"repository tool failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
