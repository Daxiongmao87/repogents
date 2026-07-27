from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass


_ESLINT_FINDING = re.compile(
    r"^\s*\d+:\d+\s+(error|warning)\s+(.+?)\s{2,}([^\s]+)\s*$",
    re.IGNORECASE,
)
_COLON_FINDING = re.compile(
    r"^(.+?):\d+:\d+:\s*(error|warning):\s*(.*?)"
    r"(?:\s+\[([^\]]+)\])?\s*$",
    re.IGNORECASE,
)
_COMPILER_FINDING = re.compile(
    r"^(.+?)\(\d+,\d+\):\s*(error|warning)\s+([^:]+):\s*(.+)$",
    re.IGNORECASE,
)
_UNITTEST_FINDING = re.compile(r"^(FAIL|ERROR):\s+(.+?)\s*$")
_PYTEST_FINDING = re.compile(r"^(FAILED|ERROR)\s+([^\s]+)(?:\s+-\s+.*)?$")
_GO_TEST_FINDING = re.compile(r"^---\s+(FAIL):\s+([^\s(]+)")
_JEST_FINDING = re.compile(r"^\s*●\s+(.+?)\s*$")
_PRETTIER_FINDING = re.compile(
    r"^\[warn\]\s+(.+\.[^./\s]+)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FindingDelta:
    new: tuple[str, ...]
    resolved: tuple[str, ...]
    unchanged: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.new


def compare_findings(
    baseline: tuple[str, ...],
    candidate: tuple[str, ...],
) -> FindingDelta:
    """Compare normalized finding multisets without losing duplicates."""

    baseline_counts = Counter(baseline)
    candidate_counts = Counter(candidate)
    return FindingDelta(
        new=tuple((candidate_counts - baseline_counts).elements()),
        resolved=tuple((baseline_counts - candidate_counts).elements()),
        unchanged=tuple((baseline_counts & candidate_counts).elements()),
    )


def extract_findings(stdout: str, stderr: str) -> tuple[str, ...]:
    """Extract stable diagnostic identities from common validation output."""

    findings: list[str] = []
    current_path: str | None = None
    for raw_line in (*stdout.splitlines(), *stderr.splitlines()):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        prettier = _PRETTIER_FINDING.match(stripped)
        if prettier is not None:
            findings.append(
                _identity(
                    prettier.group(1),
                    "warning",
                    "prettier",
                    "File is not formatted",
                )
            )
            continue

        eslint = _ESLINT_FINDING.match(line)
        if eslint is not None and current_path is not None:
            severity, message, rule = eslint.groups()
            findings.append(
                _identity(current_path, severity, rule, message)
            )
            continue

        colon = _COLON_FINDING.match(stripped)
        if colon is not None:
            path, severity, message, rule = colon.groups()
            findings.append(_identity(path, severity, rule or "", message))
            continue

        compiler = _COMPILER_FINDING.match(stripped)
        if compiler is not None:
            path, severity, rule, message = compiler.groups()
            findings.append(_identity(path, severity, rule, message))
            continue

        unittest = _UNITTEST_FINDING.match(stripped)
        if unittest is not None:
            kind, name = unittest.groups()
            findings.append(f"unittest|{kind.lower()}|{_space(name)}")
            continue

        pytest = _PYTEST_FINDING.match(stripped)
        if pytest is not None:
            kind, name = pytest.groups()
            if ".py" in name or "::" in name:
                findings.append(f"pytest|{kind.lower()}|{_space(name)}")
            continue

        go_test = _GO_TEST_FINDING.match(stripped)
        if go_test is not None:
            kind, name = go_test.groups()
            findings.append(f"go-test|{kind.lower()}|{_space(name)}")
            continue

        jest = _JEST_FINDING.match(line)
        if jest is not None:
            findings.append(f"jest|fail|{_space(jest.group(1))}")
            continue

        if _looks_like_path(stripped):
            current_path = stripped

    return tuple(findings)


def _identity(path: str, severity: str, rule: str, message: str) -> str:
    return "|".join(
        (
            _normalize_path(path),
            severity.lower(),
            _space(rule),
            _space(message),
        )
    )


def _normalize_path(value: str) -> str:
    path = value.strip().replace("\\", "/")
    for marker in ("/checkout/", "/workspace/"):
        if marker in path:
            path = path.split(marker, 1)[1]
            break
    return path.removeprefix("./")


def _space(value: str) -> str:
    return " ".join(value.strip().split())


def _looks_like_path(value: str) -> bool:
    if value.startswith(("✖", "×")):
        return False
    normalized = value.replace("\\", "/")
    return (
        "/" in normalized
        and not normalized.startswith(("http://", "https://"))
        and not any(character.isspace() for character in normalized)
    )
