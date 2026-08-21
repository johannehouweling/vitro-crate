"""Primitives shared by every crate-maturity instrument.

Two published instruments are scored against one assembled ``@graph`` — the FAIRplus
Dataset Maturity Model (``fair_assessment``) and the NIH Bridge2AI AI-readiness
criteria (``air_assessment``) — and a third, the RDA FAIR Data Maturity Model, sits
beside the first. They ask different questions and aggregate differently, but they
read the same graph and they answer in the same shape: a tri-state verdict carrying
the evidence behind it.

Keeping that shape here rather than in one instrument's module is what stops the two
from drifting apart on the questions they happen to share — "is there a licence", "is
the descriptor machine-readable" — where two implementations of one question is
exactly how the axes come to disagree with each other about one crate.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# Either an assembled ``@graph`` list or the whole crate document that wraps one —
# callers hold whichever they were given, and normalising here beats making every
# call site unwrap (the same tolerance `mit_assessment.graph_nodes` provides).
Graph = list[dict[str, Any]] | dict[str, Any] | None


class Verdict(NamedTuple):
    """One indicator's answer, plus the evidence behind it.

    A bare boolean cannot be audited: "DSM-3-C3 is false" is a verdict a reader has to
    take on trust, and both instruments carried here are human assessment instruments
    whose whole point is that an assessor can say *why*. ``evidence`` records what was
    looked for and what was found, so every cell in the report is inspectable.

    ``value`` is tri-state: ``True`` / ``False`` / ``None`` for "not assessed" — the
    workbook's blank cell, which leaves the denominator rather than counting against
    the dataset.
    """

    value: bool | None
    evidence: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - guard, not behaviour
        raise TypeError(
            "Verdict is tri-state; test `.value is True` / `is False` / `is None` "
            "explicitly rather than relying on truthiness."
        )


def as_verdict(result: Any) -> Verdict:
    """Coerce a check's return to a :class:`Verdict`.

    Checks may return a bare ``bool``/``None`` or a full ``Verdict``, so evidence can
    be enriched check by check without a flag-day change to all forty.
    """
    if isinstance(result, Verdict):
        return result
    return Verdict(result, "")


# Media types that are open, documented and readable without licensed software.
# The tox corpus is dominated by the opposite (GraphPad .prism/.pzf, .xls), which
# is exactly what DSM-3-R5 and Bridge2AI 6.c are asking about, so this genuinely
# discriminates.
OPEN_MEDIA_TYPES = frozenset(
    {
        "text/csv", "text/tab-separated-values", "text/plain", "text/markdown",
        "application/json", "application/ld+json", "application/xml", "text/xml",
        "application/x-hdf5", "application/x-netcdf", "image/png", "image/svg+xml",
        "text/html", "application/pdf",
    }
)


def nodes(graph: Graph) -> list[dict[str, Any]]:
    """The node list, whether *graph* is the list itself or the document holding it."""
    if isinstance(graph, dict):
        graph = graph.get("@graph")
    return [n for n in (graph or []) if isinstance(n, dict)]


def needs_graph(graph: Graph) -> bool:
    """True when there is no graph to read, so a graph-aware check cannot answer.

    Returning ``False`` in that case would be a lie of exactly the kind this module
    exists to prevent: it reads as "the crate fails this indicator" when the truth is
    "nothing was assessed". Callers turn ``None`` into an *unanswered cell* — excluded
    from the denominator, never counted against the dataset.
    """
    return not nodes(graph)


def node_types(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).split(":")[-1] for v in values if v}


def ref_id(value: Any) -> str:
    """The ``@id`` a property points at, whether wrapped or bare."""
    if isinstance(value, dict):
        return str(value.get("@id") or "")
    return str(value or "")


def columns(graph: Graph) -> list[dict[str, Any]]:
    """Every csvw:Column node — the crate's field-level metadata."""
    return [n for n in nodes(graph) if "Column" in node_types(n)]


def is_external_iri(value: Any) -> bool:
    """An absolute http(s) IRI — a *shared* term, not a crate-local anchor."""
    return ref_id(value).startswith(("http://", "https://"))
