from __future__ import annotations

from pathlib import Path
import re


_DESIGN_SYSTEM = Path(__file__).resolve().parents[1] / "docs" / "design-system.md"

_OBSOLETE_BOUNDARY_CLAIMS = (
    "this foundation does not implement state normalization",
    "assigned to the downstream repository-dashboard",
    "pending behavior is specified here but belongs to the repository-form implementation work",
    "downstream graph work should add explicit sequence text",
    "downstream state mapping can migrate",
    "downstream rendering must supply normalized labels",
)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _implemented_boundary_section(document: str) -> str:
    match = re.search(
        r"^## Implemented behavior and follow-up boundaries\s*$\n(.*?)(?=^## |\Z)",
        document,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "design system must retain an explicit implemented/follow-up boundary"
    return _normalized(match.group(1))


def _assert_current_design_system_boundary(document: str) -> None:
    normalized_document = _normalized(document)
    for obsolete_claim in _OBSOLETE_BOUNDARY_CLAIMS:
        assert obsolete_claim not in normalized_document, (
            f"obsolete downstream-ownership claim returned: {obsolete_claim!r}"
        )

    boundary = _implemented_boundary_section(document)

    # Each behavior called out by the review is inventoried as implemented, rather
    # than being implicitly bundled into a vague statement that the UI is complete.
    implemented_concepts = (
        ("normalized", "lifecycle statuses"),
        ("ordered and numbered", "agent graph"),
        ("destructive confirmation",),
        ("pending add and remove mutations",),
        ("targeted status and alert regions",),
        ("retains the last valid state",),
        ("preserves relevant focus",),
    )
    for concept in implemented_concepts:
        assert all(term in boundary for term in concept), (
            "implemented-boundary inventory lost required concept: "
            + " + ".join(concept)
        )

    # Ownership points contributors to the actual source and regression suite and
    # explicitly prevents parallel reimplementation of delivered behavior.
    for ownership_term in (
        "repogents/http_api.py",
        "tests/test_http_api.py",
        "refine them in place",
        "rather than recreate parallel components",
    ):
        assert ownership_term in boundary, (
            f"implementation ownership guidance is missing {ownership_term!r}"
        )

    # Genuine follow-up work is described as extension/hardening beyond the baseline,
    # with concrete examples and the constraints those extensions must preserve.
    for extension_term in (
        "beyond that baseline",
        "concrete defect",
        "scale-driven filtering or disclosure",
        "optional manual refresh control",
        "complete user-selectable light theme",
        "focused announcement regions",
        "retained-content refresh model",
        "locally served dependency-free architecture",
        "does not reserve already implemented interaction behavior",
    ):
        assert extension_term in boundary, (
            f"remaining-extension boundary is missing {extension_term!r}"
        )


def test_design_system_documents_current_implementation_boundary() -> None:
    _assert_current_design_system_boundary(_DESIGN_SYSTEM.read_text(encoding="utf-8"))


def test_design_system_boundary_contract_rejects_obsolete_follow_up_claim() -> None:
    current = _DESIGN_SYSTEM.read_text(encoding="utf-8")
    regressed = current.replace(
        "The embedded client now applies this system to the complete dashboard interaction baseline.",
        "This foundation does not implement state normalization.",
        1,
    )
    try:
        _assert_current_design_system_boundary(regressed)
    except AssertionError as error:
        assert "obsolete downstream-ownership claim returned" in str(error)
    else:
        raise AssertionError("documentation contract accepted the obsolete boundary claim")
