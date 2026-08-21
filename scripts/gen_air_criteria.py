#!/usr/bin/env python3
"""Generate ``air/criteria.yaml`` from the vendored Bridge2AI sources.

The NIH Bridge2AI *AI-readiness Criteria for Biomedical Data* is published in two
distributions under two different licences, and this generator reads both rather than
restating either:

* ``air/bridge2ai_ai_readiness_v6.jats.xml`` — the article (bioRxiv v6,
  doi:10.1101/2024.10.23.619844), **CC BY-ND 4.0**. Table 1's *Practice* sentences are
  the assessable statements and are carried **verbatim**. ND permits copying and
  redistributing an unadapted work; what it forbids is precisely the paraphrase one
  would otherwise write to feel safe. Verbatim is both the honest and the legal choice.
* ``air/bridge2ai_worksheet_v1.0.0.xlsx`` — the authors' *AI-Readiness Self-Evaluation
  Worksheet* (doi:10.5281/zenodo.13961091), **CC BY 4.0**. It supplies the criterion
  ids, the short labels and the scoring arithmetic, and it is adaptable.

The *local* decision — which criteria this tool can assess from a single RO-Crate, with
which check, and how a failure would be remedied — lives in :data:`LOCAL_SCOPE`. This
mirrors ``scripts/gen_dsm_indicators.py``.

**All 28 published criteria are carried**, including the ones no crate can evidence. A
criterion we do not assess is reported ``na`` — excluded from the denominator, never
failed. Research ethics, repository governance and hosting are ``na`` by construction:
a crate on disk cannot evidence IRB approval, a data-access committee, a retention
policy or an API, and auto-passing "consent obtained" would be the single most
embarrassing output this axis could produce.

There is deliberately **no aggregate score**. The authors state it outright — *"We do
not score it pass/fail overall"* — so the axis is seven per-dimension percentages and
nothing else. A single "AI-readiness = 0.42" would be our invention again, which is the
one thing this instrument was adopted to stop.

Regenerate with::

    uv run python -m scripts.gen_air_criteria

``tests/test_air_criteria_source.py`` asserts the committed file equals this
generator's output and that every practice appears verbatim in the vendored article.
"""

from __future__ import annotations

import pathlib
import re
import xml.etree.ElementTree as ET
from typing import Any, NamedTuple

import openpyxl
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
JATS_XML = REPO / "air" / "bridge2ai_ai_readiness_v6.jats.xml"
WORKSHEET = REPO / "air" / "bridge2ai_worksheet_v1.0.0.xlsx"
OUT = REPO / "air" / "criteria.yaml"

_TABLE_CAPTION = "AI-readiness Criteria and Practices"
_WORKSHEET_SHEET = "Blank"
# The worksheet's "AI-Readiness Current Rating" block: one row per criterion.
_WORKSHEET_ROWS = range(4, 32)


class Remedy(NamedTuple):
    """How a failing criterion would be fixed, if it could be.

    ``entity_type`` is the crate entity the field lives on (``None`` = crate-level,
    i.e. the Root Data Entity); ``property`` is a **real writable field name**;
    ``route`` is one of ``auto`` / ``ask-user`` / ``draft`` / ``report-only``.

    This is the whole mechanism by which the axis stops being a tile and starts
    driving the guidance loop. It states *intent* only — ``gap_analysis._is_committable``
    has the final veto, so the axis can never claim actionability the loop does not
    have.
    """

    entity_type: str | None
    property: str | None
    route: str


REPORT_ONLY_REMEDY = Remedy(None, None, "report-only")

# The crate-intrinsic subset this tool assesses: published criterion id ->
# (scope, check name, remedy). Every check name is verified against
# builder/tools/air_assessment.AIR_CHECKS by the source test.
#
# `scope: full`    — directly decidable from the assembled @graph or CrateState.
# `scope: partial` — a presence/structure heuristic; the criterion asks for more
#                    than a crate can show, and the evidence string says which part.
# Anything absent here is `scope: na` — not assessable, never failed.
LOCAL_SCOPE: dict[str, tuple[str, str, Remedy]] = {
    # ---- 0 FAIRness -------------------------------------------------------------
    # 0.a: a crate cannot show which repository holds it; a persistent identifier on
    # the root is the only trace of a deposit that survives into the crate.
    "0.a": ("partial", "repository_deposit", REPORT_ONLY_REMEDY),
    "0.b": ("full", "metadata_standalone", REPORT_ONLY_REMEDY),
    "0.c": ("full", "formal_specification", REPORT_ONLY_REMEDY),
    "0.d": ("full", "usage_license", REPORT_ONLY_REMEDY),
    # ---- 1 Provenance -----------------------------------------------------------
    "1.a": ("partial", "data_sources_identified", REPORT_ONLY_REMEDY),
    "1.b": ("full", "transformation_steps_wired", REPORT_ONLY_REMEDY),
    "1.c": ("full", "software_in_repository", REPORT_ONLY_REMEDY),
    # 1.d wants ORCID and ROR — identifiers, which D5 forbids taking from a human
    # answer, so there is no ask-user route however much we would like one.
    "1.d": ("full", "key_actors_identified", REPORT_ONLY_REMEDY),
    # ---- 2 Characterization -----------------------------------------------------
    "2.a": ("full", "descriptive_metadata_rich", Remedy(None, "description", "ask-user")),
    "2.c": ("partial", "machine_readable_schema", REPORT_ONLY_REMEDY),
    # ---- 3 Pre-model Explainability ---------------------------------------------
    "3.a": ("partial", "documentation_template", Remedy("LabProtocol", "description", "ask-user")),
    "3.b": ("partial", "linked_publications", REPORT_ONLY_REMEDY),
    "3.c": ("full", "payload_checksums", REPORT_ONLY_REMEDY),
    # ---- 4 Ethics ---------------------------------------------------------------
    # Only 4.d is visible in a crate: schema.org conditionsOfAccess says, in the
    # criterion's own words, "public" or "controlled access only".
    "4.d": ("full", "access_conditions", REPORT_ONLY_REMEDY),
    # ---- 5 Sustainability -------------------------------------------------------
    # 5.d names RO-Crate as its own suggested resource.
    "5.d": ("full", "project_level_links", REPORT_ONLY_REMEDY),
    # ---- 6 Computability --------------------------------------------------------
    "6.a": ("full", "validatable_standard", REPORT_ONLY_REMEDY),
    "6.c": ("full", "portable_formats", REPORT_ONLY_REMEDY),
}

# Where an AIR criterion asks a question one of the other two instruments already
# asks, the overlap is recorded here — because the manuscript cannot then present the
# three axes as independent evidence, and a reviewer who works it out for themselves
# is a worse outcome than saying so.
#
# `shared-check`  the SAME function answers both. Two implementations of one question
#                 is how two axes come to disagree about one crate, so this is the
#                 default wherever the existing check is a real measurement.
# `same-question` the criterion asks the same thing but is answered independently,
#                 because the existing check is a `len(entities) > 0` presence
#                 tautology (an audit found 18 of the 40 RDA checks are). Reusing one
#                 would import a known-bad measurement into a new axis.
OVERLAPS: dict[str, tuple[list[str], str]] = {
    "0.c": (["RDA-I1-01M", "RDA-I1-02M"], "same-question"),
    "0.d": (["RDA-R1.1-01M"], "shared-check"),
    "2.c": (["DSM-2-C4"], "shared-check"),
    "4.d": (["DSM-1-C3"], "same-question"),
    "6.a": (["RDA-R1.3-01M", "RDA-R1.3-02M"], "same-question"),
    "6.c": (["DSM-3-R5"], "shared-check"),
}

# Worksheet v1.0.0 (Oct 2024) predates paper v6 (Apr 2026) and numbers dimension 3
# as 3.a / 3.c / 3.d. Same three criteria, different labels. The paper is canonical;
# the worksheet id is carried so our figures can be lined up against the authors'
# own spreadsheet, which is the one artefact a reader can actually run.
_PAPER_TO_WORKSHEET_ID: dict[str, str] = {"3.b": "3.c", "3.c": "3.d"}

SOURCE: dict[str, Any] = {
    "name": "AI-readiness Criteria for Biomedical Data",
    "programme": "NIH Bridge2AI",
    "doi": "10.1101/2024.10.23.619844",
    "url": "https://doi.org/10.1101/2024.10.23.619844",
    "pmid": 39484409,
    "pmcid": "PMC11526931",
    "version": 6,
    "posted": "2026-04-24",
    # Still a preprint: the JATS carries <article-version article-version-type=
    # "status">preprint</article-version>, Crossref records no is-preprint-of
    # relation, and PubMed links no journal article. Implying a peer-reviewed
    # version would misrepresent the instrument to the people who wrote it.
    "status": "preprint",
    "peer_reviewed": None,
    "authors": "Clark T, Caufield H, Parker JA, et al. (32 authors)",
    "license": "CC-BY-ND-4.0",
    "license_url": "https://creativecommons.org/licenses/by-nd/4.0/",
    "license_note": (
        "The practice text is reproduced verbatim and unmodified. CC BY-ND permits "
        "copying and distributing the work in unadapted form only; a paraphrase would "
        "be the derivative it forbids."
    ),
    "distribution": (
        "Europe PMC JATS full text for PMC11526931 (Table 1) — vendored as "
        "air/bridge2ai_ai_readiness_v6.jats.xml"
    ),
    "table": "Table 1 — AI-readiness Criteria and Practices",
    "table_note": (
        "Practices may impact multiple criteria; the most relevant relationship is "
        "shown for brevity"
    ),
    "worksheet": {
        "name": "AI-Readiness Self-Evaluation Worksheet",
        "authors": "Parker J, Clark T",
        "doi": "10.5281/zenodo.13961091",
        "concept_doi": "10.5281/zenodo.13961090",
        "url": "https://doi.org/10.5281/zenodo.13961091",
        "version": "1.0.0",
        "released": "2024-10-21",
        "license": "CC-BY-4.0",
        "distribution": (
            "AI-Ready Self-Evaluation Worksheet_Blank.xlsx — vendored as "
            "air/bridge2ai_worksheet_v1.0.0.xlsx"
        ),
    },
    "unit_of_assessment": (
        "a dataset — 'AI-readiness is a dynamic, context-dependent developmental "
        "property of specific data sets'. One RO-Crate is one dataset."
    ),
    "retrieved": "2026-08-22",
    "note": (
        "This is a SELF-EVALUATION instrument and the paper calls its own evaluation "
        "report 'subjective'. Automating it is defensible; claiming the scoring is "
        "objective is not. Criteria this tool cannot assess from a crate alone are "
        "reported na, never failed."
    ),
}

SCORING: dict[str, Any] = {
    "per_dimension": "met / total * 100",
    "formula": '=(SUM(D4:D7)/COUNTIF(C4:C7, "*"))*100',
    "input": "Criterion met? (Y=1; N=0)",
    "aggregate": None,
    "note": (
        "Verbatim: 'We do not score it pass/fail overall, but along multiple "
        "dimensions based on readiness scores for major components, yielding a "
        "characteristic readiness profile.' Sub-criteria are unweighted and the "
        "denominators are deliberately unequal (Characterization 5, Pre-model "
        "Explainability 3)."
    ),
    "published_denominator": "all criteria in the dimension",
    "local_denominator": "criteria assessed (a declared deviation)",
    "deviation_note": (
        "The published denominator is COUNTIF over the label cells, which are always "
        "all present — the instrument has no 'not assessed' state. Reporting only our "
        "figure would quietly restate it, so both are computed per dimension: "
        "`published_pct` is theirs, `pct` is ours with unassessed criteria excluded."
    ),
}

_HEADER = """\
# GENERATED FILE — do not edit by hand.
#
# Regenerate with:  uv run python -m scripts.gen_air_criteria
#
# All 28 published NIH Bridge2AI AI-readiness criteria, sourced from the two vendored
# distributions: the practice text verbatim from the article's Table 1
# (air/bridge2ai_ai_readiness_v6.jats.xml; CC BY-ND 4.0, bioRxiv v6,
# doi:10.1101/2024.10.23.619844) and the ids, labels and scoring arithmetic from the
# authors' worksheet (air/bridge2ai_worksheet_v1.0.0.xlsx; CC BY 4.0,
# doi:10.5281/zenodo.13961091).
# `text` is the authors' own wording, unmodified — CC BY-ND permits copying but not
# adaptation, so a paraphrase would be the one prohibited act. The local scope, check
# and remedy mapping lives in scripts/gen_air_criteria.py.
# There is deliberately NO aggregate score: seven per-dimension percentages, as the
# authors specify. Criteria concerning ethics, governance and hosting are reported na
# (not assessable from a crate), never failed.
"""


def _cell_text(cell: ET.Element) -> str:
    """A table cell's text with reference markers removed and whitespace collapsed.

    JATS wraps citation superscripts as ``<xref>`` inside the cell, so a naive
    ``itertext`` glues reference numbers onto the prose ("...repository.4344"). The
    numbers are the article's own bibliography markers, not part of the criterion.
    """
    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        if node.tag == "xref":
            if node.tail:
                parts.append(node.tail)
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
        if node.tail:
            parts.append(node.tail)

    if cell.text:
        parts.append(cell.text)
    for child in cell:
        walk(child)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _tidy_resources(raw: str) -> str:
    """The suggested-resources cell, minus the punctuation the removed xrefs left."""
    text = re.sub(r"\s*[–—]\s*", " ", raw)  # en/em dashes joined citation ranges
    text = re.sub(r"\s+([,;])", r"\1", text)
    text = re.sub(r"([,;])(?=\S)", r"\1 ", text)
    return re.sub(r"\s+", " ", text).strip(" ,;")


def _load_table1() -> tuple[dict[int, str], list[dict[str, Any]]]:
    """Table 1 of the article: the seven dimension names and all 28 criteria.

    Dimension rows carry a bare integer id and no practice; criterion rows carry a
    ``<n>.<letter>`` id. Table 1 is authoritative for the numbering — the abstract
    lists the dimensions in a different order, swapping Ethics and Pre-model
    Explainability, and following it would mislabel every figure on both axes.
    """
    root = ET.parse(JATS_XML).getroot()
    table = next(
        (
            t
            for t in root.iter("table-wrap")
            if any(_TABLE_CAPTION in "".join(c.itertext()) for c in t.iter("caption"))
        ),
        None,
    )
    if table is None:
        raise LookupError(f"no table captioned {_TABLE_CAPTION!r} in {JATS_XML.name}")

    dimensions: dict[int, str] = {}
    criteria: list[dict[str, Any]] = []
    dimension: int | None = None
    for row in table.iter("tr"):
        cells = [_cell_text(c) for c in row]
        if len(cells) < 4:
            continue
        ident = cells[0]
        if re.fullmatch(r"\d+", ident):
            dimension = int(ident)
            dimensions[dimension] = cells[1]
        elif re.fullmatch(r"\d+\.[a-z]", ident):
            if dimension is None:
                raise ValueError(f"{ident}: no dimension in scope — the table changed")
            criteria.append(
                {
                    "id": ident,
                    "dimension": dimension,
                    "label": cells[1],
                    "text": cells[2],
                    "suggested_resources": _tidy_resources(cells[3]),
                }
            )
    return dimensions, criteria


def _load_worksheet_labels() -> dict[str, str]:
    """The worksheet's own short label per criterion id, e.g. ``3.d`` -> ``Verifiable``.

    Read to cross-check the paper's ids against the authors' scoring instrument and to
    surface where the two have drifted, rather than trusting one and hoping.
    """
    sheet = openpyxl.load_workbook(WORKSHEET, read_only=True, data_only=True)[_WORKSHEET_SHEET]
    rows = list(sheet.iter_rows(values_only=True))
    labels: dict[str, str] = {}
    for number in _WORKSHEET_ROWS:
        value = rows[number - 1][2]  # column C: "Findable (0.a)"
        if not value:
            continue
        match = re.match(r"^(.*?)\s*\((\d+\.[a-z])\)\s*$", str(value).strip())
        if match:
            labels[match.group(2)] = match.group(1).strip()
    return labels


def build_data() -> dict[str, Any]:
    """The full ``air/criteria.yaml`` payload: source + scoring + all 28 criteria."""
    dimensions, rows = _load_table1()
    worksheet_labels = _load_worksheet_labels()

    known = {row["id"] for row in rows}
    unknown = sorted(set(LOCAL_SCOPE) - known)
    if unknown:
        raise KeyError(
            f"LOCAL_SCOPE names criteria absent from the published table: {unknown}. "
            "Fix the mapping rather than the instrument."
        )

    from builder.tools.field_kinds import is_identifier_field

    criteria: list[dict[str, Any]] = []
    for row in rows:
        scope, check, remedy = LOCAL_SCOPE.get(row["id"], ("na", "", REPORT_ONLY_REMEDY))
        if remedy.route != "report-only":
            if not remedy.property:
                raise ValueError(
                    f"{row['id']}: remedy route {remedy.route!r} names no field to set. "
                    "A gap with nothing to write is report-only, whatever we intend."
                )
            if is_identifier_field(remedy.property):
                raise ValueError(
                    f"{row['id']}: remedy names the identifier field "
                    f"{remedy.property!r}. D5 — identifiers come from lookups, so the "
                    "guidance loop would discard whatever the user typed."
                )

        entry: dict[str, Any] = {
            "id": row["id"],
            "dimension": row["dimension"],
            "label": row["label"],
            "scope": scope,
        }
        if check:
            entry["check"] = check
        worksheet_id = _PAPER_TO_WORKSHEET_ID.get(row["id"])
        if worksheet_id:
            entry["worksheet_id"] = worksheet_id
        entry["worksheet_label"] = worksheet_labels.get(worksheet_id or row["id"], "")
        if row["id"] in OVERLAPS:
            refs, kind = OVERLAPS[row["id"]]
            entry["overlaps"] = list(refs)
            entry["overlap_kind"] = kind
        entry["remedy"] = {
            "entity_type": remedy.entity_type,
            "property": remedy.property,
            "route": remedy.route,
        }
        entry["suggested_resources"] = row["suggested_resources"]
        entry["text"] = row["text"]
        criteria.append(entry)

    return {
        "source": SOURCE,
        "license": SOURCE["license"],
        "dimensions": dimensions,
        "scoring": SCORING,
        "criteria": criteria,
    }


def format_report(data: dict[str, Any]) -> str:
    """A per-dimension summary of what the instrument holds and how much we assess."""
    criteria = data["criteria"]
    dimensions = data["dimensions"]

    lines: list[str] = []
    lines.append("NIH Bridge2AI AI-readiness criteria (bioRxiv v6) — criterion coverage")
    lines.append("=" * 76)
    lines.append(f"{'Dim':<4} {'Name':<26} {'total':>6} {'assessed':>9} {'na':>4}  routes")
    lines.append("-" * 76)

    for dim in sorted(dimensions):
        at = [c for c in criteria if c["dimension"] == dim]
        assessed = sum(1 for c in at if c["scope"] in ("full", "partial"))
        routed = sorted(
            {c["remedy"]["route"] for c in at if c["remedy"]["route"] != "report-only"}
        )
        lines.append(
            f"{dim:<4} {dimensions[dim][:26]:<26} {len(at):>6} {assessed:>9} "
            f"{len(at) - assessed:>4}  {', '.join(routed) or '-'}"
        )

    total = len(criteria)
    full = sum(1 for c in criteria if c["scope"] == "full")
    partial = sum(1 for c in criteria if c["scope"] == "partial")
    na = total - full - partial
    lines.append("-" * 76)
    lines.append(f"{'ALL':<4} {'':<26} {total:>6} {full + partial:>9} {na:>4}")
    lines.append("")
    lines.append(f"  full     {full:>3}  directly decidable from the crate")
    lines.append(f"  partial  {partial:>3}  the criterion asks for more than a crate can show")
    lines.append(f"  na       {na:>3}  not assessable from a crate — reported, never failed")
    lines.append("")

    by_dim: dict[int, list[str]] = {}
    for crit in criteria:
        if crit["scope"] == "na":
            by_dim.setdefault(crit["dimension"], []).append(crit["id"])
    lines.append("  the na, by what a crate cannot show:")
    for dim in sorted(by_dim):
        lines.append(f"    {dimensions[dim][:26]:<26} {', '.join(by_dim[dim])}")

    actionable = [c for c in criteria if c["remedy"]["route"] != "report-only"]
    lines.append("")
    lines.append(
        f"  {len(actionable)} of {total} criteria declare a remedy the guidance loop can act on: "
        + ", ".join(f"{c['id']} -> {c['remedy']['property']}" for c in actionable)
    )
    lines.append(
        "  The rest are report-only because an AI-readiness criterion is a statement "
        "about the\n  dataset, and the loop writes entity fields — not because we "
        "declined to route them."
    )
    lines.append("")
    lines.append(
        "  Scoring: seven per-dimension percentages, no aggregate — "
        "'We do not score it pass/fail overall'."
    )
    shared = [c["id"] for c in criteria if c.get("overlap_kind") == "shared-check"]
    same = [c["id"] for c in criteria if c.get("overlap_kind") == "same-question"]
    lines.append(
        f"  {len(shared)} criteria are answered by the very same DSM/RDA check "
        f"({', '.join(shared)}); {len(same)} ask the same question independently "
        f"({', '.join(same)}). The three axes are not independent evidence."
    )
    return "\n".join(lines)


def main() -> None:
    data = build_data()
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    OUT.write_text(_HEADER + "\n" + body)
    print(format_report(data))
    print()
    print(f"Wrote {OUT.relative_to(REPO)} ({len(data['criteria'])} criteria).")


if __name__ == "__main__":
    main()
