#!/usr/bin/env python3
"""Generate ``fair/dsm_indicators.yaml`` from the vendored FAIRplus DSM assessment sheet.

The FAIRplus **Dataset** Maturity Model (DSM) indicator *definitions* — id, level,
category, verbatim text, granularity, and the model's own cross-references to RDA and
FAIRsFAIR indicators — are sourced from the canonical assessment workbook
(``fair/fairplus_dsm_v1.2.xlsx``, FAIRplus/Data-Maturity, model text CC BY 4.0) rather than
hand-authored, so they cannot silently drift. This mirrors how
``scripts/gen_fair_indicators.py`` sources the RDA indicators from ``fair/rda_fdmm.xlsx``.

The *local* decision — which indicators this tool can assess intrinsically from a single
RO-Crate, and with which check function — lives in :data:`LOCAL_SCOPE` below; that is
inherently repo-specific and stays here.

**All 83 published indicators are carried**, including the ones we cannot assess. An
indicator we do not assess is reported ``na`` (not assessable), never failed — the
published model is reproduced in full and our coverage of it is visible rather than
implied. Three groups are ``na`` by construction:

* **Hosting Env. Capabilities** — the model's own ``Granularity`` column marks these
  *Storage / Retrieval / Search Capability*, i.e. properties of the environment serving
  the dataset, not of the dataset. A crate on disk cannot evidence them.
* **Level 0** — these are *negative* statements describing the pre-FAIRification state
  ("Dataset(s) are NOT Identifiable via Unique Identifiers"). They are a starting
  condition, not a target to satisfy.
* Content/Representation indicators needing file-content or external context we do not
  inspect.

Regenerate with::

    uv run python scripts/gen_dsm_indicators.py

``tests/test_dsm_indicators_source.py`` asserts the committed file equals this
generator's output and that every indicator matches its row in the workbook.
"""

from __future__ import annotations

import pathlib
from typing import Any

import openpyxl
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
DSM_XLSX = REPO / "fair" / "fairplus_dsm_v1.2.xlsx"
OUT = REPO / "fair" / "dsm_indicators.yaml"

# The workbook's authoritative indicator listing. Columns: Level(0), Category(1),
# ID(2), v1 Identifier(3), Indicator Description(4), REF RDA indicator(s)(5),
# REF FAIRsFAIR indicator(s)(6), Granurality(7)  [sic — the sheet's own spelling].
_SHEET = "MASTER (Levels View)"

# The workbook's category names -> the single-letter codes the indicator ids use.
_CATEGORY_CODE: dict[str, str] = {
    "Content & Context": "C",
    "Representation & Format": "R",
    "Hosting Env. Capabilities": "H",
}

# Level names, verbatim from the workbook's "Levels definition" sheet ("State of
# Data"), with the scope each level describes. These name the state of the DATA, not
# an organisation's capability — the FAIR Cookbook is explicit that the model defines
# "maturity levels which can used to describe a dataset".
_LEVELS: dict[int, dict[str, str]] = {
    0: {"name": "Single-use data", "scope": "No re-use beyond the project lifetime"},
    1: {"name": "Identifiable Data", "scope": "Data Object level"},
    2: {"name": "Described Data", "scope": "Project level"},
    3: {"name": "Standardised Data", "scope": "Community level"},
    4: {"name": "Semantically Typed Data", "scope": "Cross-community level"},
    5: {"name": "Managed Data Assets", "scope": "Enterprise level"},
}

# The crate-intrinsic subset this tool assesses: published indicator id -> check
# function name (see builder/tools/fair_assessment.py DSM_CHECKS). Every id here is
# verified against the workbook at generation time, so a typo or an indicator the
# model drops fails loudly rather than silently scoring nothing.
#
# `scope: full`    — directly decidable from the assembled @graph.
# `scope: partial` — a presence/structure heuristic; true semantic compliance would
#                    need content or external checks we do not perform.
# Anything absent here is `scope: na`.
LOCAL_SCOPE: dict[str, tuple[str, str]] = {
    # ---- Level 1 — identifiable, access info, machine-readable descriptor --------
    "DSM-1-C0": ("full", "unique_id"),
    "DSM-1-C1": ("full", "study_summary"),
    "DSM-1-C2": ("full", "dataset_metadata"),
    "DSM-1-C3": ("full", "access_info"),
    "DSM-1-R0": ("full", "has_descriptor"),
    "DSM-1-R1": ("full", "context_fields"),
    "DSM-1-R2": ("full", "dataset_hierarchy"),
    "DSM-1-R3": ("full", "general_schema"),
    "DSM-1-R4": ("full", "descriptor_machine_readable"),
    "DSM-1-R5": ("full", "data_machine_readable"),
    # ---- Level 2 — field/value metadata, local domain + dataset model ------------
    "DSM-2-C1": ("partial", "domain_model"),
    "DSM-2-C2": ("partial", "tidy_dataset"),
    "DSM-2-C3": ("full", "reference_fields"),
    "DSM-2-C4": ("partial", "local_data_dictionary"),
    "DSM-2-C5": ("full", "cross_dataset_refs"),
    "DSM-2-C6": ("full", "field_level_metadata"),
    "DSM-2-C7": ("full", "value_level_metadata"),
    "DSM-2-R1": ("partial", "domain_model"),
    "DSM-2-R2": ("full", "local_dataset_model"),
    "DSM-2-R3": ("full", "generic_model"),
    "DSM-2-R4": ("partial", "model_documentation_human"),
    "DSM-2-R5": ("full", "data_structured"),
    # ---- Level 3 — community standards, controlled terms, standard licence -------
    "DSM-3-C1": ("full", "min_info_guidelines"),
    "DSM-3-C2": ("partial", "community_domain_model"),
    "DSM-3-C3": ("full", "standard_field_names"),
    "DSM-3-C4": ("partial", "controlled_values"),
    "DSM-3-C5": ("full", "resolvable_terms"),
    "DSM-3-C6": ("partial", "standard_field_metadata"),
    "DSM-3-C7": ("full", "standard_license"),
    "DSM-3-R1": ("partial", "community_domain_model"),
    "DSM-3-R2": ("full", "local_dataset_model"),
    "DSM-3-R3": ("full", "domain_standard"),
    "DSM-3-R4": ("full", "community_domain_model"),
    "DSM-3-R5": ("full", "non_proprietary_format"),
    # ---- Level 4 — semantic model, linked data, machine-readable licence ---------
    "DSM-4-C1": ("partial", "semantic_study_design"),
    "DSM-4-C2": ("partial", "common_data_elements"),
    "DSM-4-C3": ("full", "common_data_elements"),
    "DSM-4-C4": ("partial", "standard_identifiers"),
    "DSM-4-C5": ("partial", "cde_relationships"),
    "DSM-4-R1": ("full", "semantic_contextual_metadata"),
    "DSM-4-R2": ("partial", "linked_data"),
    "DSM-4-R3": ("partial", "semantic_model"),
    "DSM-4-R4": ("partial", "machine_interpretable"),
    "DSM-4-R5": ("partial", "machine_interpretable_graph"),
    "DSM-4-R6": ("full", "license_machine"),
}

SOURCE: dict[str, Any] = {
    "name": "FAIRplus Dataset Maturity Model (DSM)",
    "url": "https://fairplus.github.io/Data-Maturity/",
    "repository": "https://github.com/FAIRplus/Data-Maturity",
    "distribution": (
        "docs/assessment/FAIR-DSM-Assessment-Sheet-v1.2.xlsx "
        "(sheet 'MASTER (Levels View)') — vendored as fair/fairplus_dsm_v1.2.xlsx"
    ),
    "version": "1.2",
    # The MODEL TEXT is CC BY 4.0 and requires attribution. The repository's
    # LICENSE.txt is only the MIT licence of its Just-the-Docs Jekyll theme
    # ((c) Patrick Marsceill) and does NOT license the model — a trap worth naming,
    # because taking the repo's headline licence at face value would under-attribute
    # a work this paper cites.
    "license": "CC-BY-4.0",
    "specification": {
        "name": "FAIRplus D2.6 FAIR Data Set Maturity model",
        "doi": "10.5281/zenodo.7464523",
        "url": "https://doi.org/10.5281/zenodo.7464523",
        "year": 2022,
        "authors": (
            "Emam I, Rocca-Serra P, Sansone S-A, Portell-Silva L, Gadiya Y, "
            "Welter D, Juty N, Abbassi-Daloii T"
        ),
    },
    "peer_reviewed": {
        "citation": (
            "Welter D, Juty N, Rocca-Serra P, et al. FAIR in action - a flexible "
            "framework to guide FAIRification. Sci Data 10:291 (2023)"
        ),
        "doi": "10.1038/s41597-023-02167-2",
    },
    "assessment_tool": "https://fairdsm.biospeak.solutions/",
    # The DSM is built on the RDA FAIR Data Maturity Model: the workbook ships an
    # "RDA indicators" sheet and 27 of the 83 indicators carry an explicit
    # `REF RDA indicator(s)` cross-reference, surfaced per indicator as `rda_ref`.
    "derived_from": {
        "name": "RDA FAIR Data Maturity Model",
        "doi": "10.15497/rda00050",
        "url": "https://doi.org/10.15497/rda00050",
    },
    "retrieved": "2026-08-21",
    "note": (
        "The model assesses a DATASET, not an organisation: the FAIR Cookbook states it "
        "defines 'maturity levels which can used to describe a dataset'. Level names are "
        "the workbook's own 'State of Data'. Indicators this tool cannot assess from a "
        "crate alone are reported na, never failed."
    ),
}

_HEADER = """\
# GENERATED FILE — do not edit by hand.
#
# Regenerate with:  uv run python scripts/gen_dsm_indicators.py
#
# All 83 published FAIRplus Dataset Maturity Model (DSM) indicators, sourced verbatim
# from the vendored assessment workbook (fair/fairplus_dsm_v1.2.xlsx; model text
# CC BY 4.0, FAIRplus D2.6, doi:10.5281/zenodo.7464523).
# `text` is the model's own wording; `granularity`, `rda_ref` and `fairsfair_ref` are
# the model's own columns. The local scope/check mapping — which indicators this tool
# assesses from one RO-Crate, and with which check — lives in
# scripts/gen_dsm_indicators.py. Hosting-environment indicators and the Level 0
# pre-FAIRification states are reported na (not assessable), never failed.
"""


def _load_workbook_rows() -> list[dict[str, Any]]:
    """Every DSM indicator in the workbook, in sheet order.

    The Level column is only filled on the first row of each level block, so it is
    carried down. Rows without a ``DSM-`` id are spacers and are skipped.
    """
    wb = openpyxl.load_workbook(DSM_XLSX, read_only=True, data_only=True)
    ws = wb[_SHEET]
    rows: list[dict[str, Any]] = []
    level: int | None = None
    for row in list(ws.iter_rows(values_only=True))[1:]:
        if row[0] not in (None, ""):
            try:
                level = int(float(row[0]))
            except (TypeError, ValueError):  # a stray note in the Level column
                pass
        ident = str(row[2]).strip() if row[2] else ""
        if not ident.startswith("DSM-"):
            continue
        category = str(row[1]).strip() if row[1] else ""
        if category not in _CATEGORY_CODE:
            raise KeyError(f"{ident}: unknown DSM category {category!r}")
        if level is None:
            raise ValueError(f"{ident}: no level in scope — the sheet layout changed")
        rows.append(
            {
                "id": ident,
                "level": level,
                "category": _CATEGORY_CODE[category],
                "text": str(row[4]).strip() if row[4] else "",
                "rda_ref": str(row[5]).strip() if row[5] else "",
                "fairsfair_ref": str(row[6]).strip() if row[6] else "",
                "granularity": str(row[7]).strip() if row[7] else "",
            }
        )
    return rows


# The questionnaire sheet. Each row is one ANSWER OPTION; rows sharing a QUESTION are
# that question's options, ordered as an increasing ladder (one statement per maturity
# level). Columns: CATEGORY(1), SUBCATEGORY(2), QUESTION_NUM(5), QUESTION(6),
# INDICATORID(8), INDICATOR(9), LEVEL(10), ISMULTI(11), HAS_NOA(12).
_QUESTION_SHEET = "Assessment Tool Data v1.2 "


def _load_questions(levels_by_id: dict[str, int]) -> list[dict[str, Any]]:
    """The assessment questionnaire: 18 questions, each a ladder of indicator options.

    This is what makes the ladder constraint expressible. Within one question the
    options are nested by level — "standardised to a *community* model" sits above
    "standardised to a *local* model" — so an option cannot honestly be true while a
    lower one on the same ladder is false. Scoring indicators independently, as this
    tool does, can produce exactly that incoherence; carrying the question structure
    is what lets the scorer refuse it.
    """
    wb = openpyxl.load_workbook(DSM_XLSX, read_only=True, data_only=True)
    ws = wb[_QUESTION_SHEET]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(c).strip() if c else "" for c in rows[0]]
    col = {name: i for i, name in enumerate(header) if name}

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows[1:]:
        question = str(row[col["QUESTION"]]).strip() if row[col["QUESTION"]] else ""
        ident = str(row[col["INDICATORID"]]).strip() if row[col["INDICATORID"]] else ""
        if not question or not ident.startswith("DSM-"):
            continue
        # The questionnaire's own LEVEL cell is multi-valued ("Level 1, Level 2" —
        # an option that satisfies several rungs), so it cannot order a ladder.
        # The MASTER sheet is the authoritative statement of an indicator's level.
        level = levels_by_id.get(ident)
        entry = grouped.setdefault(
            question,
            {
                "question": question,
                "multi_select": bool(row[col["ISMULTI"]]),
                "has_none_of_the_above": bool(row[col["HAS_NOA"]]),
                "options": [],
            },
        )
        if ident not in [o["id"] for o in entry["options"]]:
            entry["options"].append({"id": ident, "level": level})

    questions = list(grouped.values())
    for entry in questions:
        # Ladder order is by level; an option with no level sorts first (it is the
        # "none of the above" / level-0 state).
        entry["options"].sort(key=lambda o: (o["level"] is None, o["level"] or 0))
    return questions


def build_data() -> dict[str, Any]:
    """The full ``dsm_indicators.yaml`` payload: source + levels + all indicators."""
    rows = _load_workbook_rows()
    known = {r["id"] for r in rows}
    unknown = sorted(set(LOCAL_SCOPE) - known)
    if unknown:
        raise KeyError(
            f"LOCAL_SCOPE names indicators absent from the workbook: {unknown}. "
            "Fix the mapping rather than the workbook."
        )

    indicators: list[dict[str, Any]] = []
    for row in rows:
        scope, check = LOCAL_SCOPE.get(row["id"], ("na", ""))
        entry: dict[str, Any] = {
            "id": row["id"],
            "level": row["level"],
            "category": row["category"],
            "scope": scope,
        }
        if check:
            entry["check"] = check
        entry["granularity"] = row["granularity"]
        if row["rda_ref"]:
            entry["rda_ref"] = row["rda_ref"]
        if row["fairsfair_ref"]:
            entry["fairsfair_ref"] = row["fairsfair_ref"]
        entry["text"] = row["text"]
        indicators.append(entry)

    return {
        "source": SOURCE,
        "license": SOURCE["license"],
        "levels": {lvl: meta["name"] for lvl, meta in _LEVELS.items()},
        "level_scope": {lvl: meta["scope"] for lvl, meta in _LEVELS.items()},
        "questions": _load_questions({r["id"]: r["level"] for r in rows}),
        "indicators": indicators,
    }


def format_report(data: dict[str, Any]) -> str:
    """A per-level summary of what the model holds and how much of it we assess."""
    indicators = data["indicators"]
    levels = data["levels"]
    scope_of = {i["id"]: i["scope"] for i in indicators}

    lines: list[str] = []
    lines.append("FAIRplus Dataset Maturity Model (DSM) v1.2 — indicator coverage")
    lines.append("=" * 72)
    lines.append(
        f"{'Level':<5} {'Name':<26} {'C':>4} {'R':>4} {'H':>4} "
        f"{'total':>6} {'assessed':>9} {'na':>4}"
    )
    lines.append("-" * 72)

    for lvl in sorted(levels):
        at = [i for i in indicators if i["level"] == lvl]
        if not at:
            continue
        cats = {c: sum(1 for i in at if i["category"] == c) for c in "CRH"}
        assessed = sum(1 for i in at if scope_of[i["id"]] in ("full", "partial"))
        lines.append(
            f"{lvl:<5} {levels[lvl][:26]:<26} {cats['C']:>4} {cats['R']:>4} "
            f"{cats['H']:>4} {len(at):>6} {assessed:>9} {len(at) - assessed:>4}"
        )

    total = len(indicators)
    full = sum(1 for i in indicators if i["scope"] == "full")
    partial = sum(1 for i in indicators if i["scope"] == "partial")
    na = total - full - partial
    lines.append("-" * 72)
    lines.append(
        f"{'ALL':<5} {'':<26} "
        f"{sum(1 for i in indicators if i['category'] == 'C'):>4} "
        f"{sum(1 for i in indicators if i['category'] == 'R'):>4} "
        f"{sum(1 for i in indicators if i['category'] == 'H'):>4} "
        f"{total:>6} {full + partial:>9} {na:>4}"
    )
    lines.append("")
    lines.append(f"  full     {full:>3}  directly decidable from the assembled @graph")
    lines.append(f"  partial  {partial:>3}  presence/structure heuristic")
    lines.append(f"  na       {na:>3}  not assessable from a crate — reported, never failed")

    # A DISJOINT partition of the `na` set — the three groups overlap (Level 0 and
    # Level 5 each contain hosting indicators), so counting them independently would
    # sum past the total and misstate the coverage.
    ladder = [i for i in indicators if i["level"] not in (0, 5)]
    level0 = sum(1 for i in indicators if i["level"] == 0)
    level5 = sum(1 for i in indicators if i["level"] == 5)
    hosting_on_ladder = sum(1 for i in ladder if i["category"] == "H")
    other = sum(
        1 for i in ladder if i["category"] != "H" and i["scope"] == "na"
    )
    linked = sum(1 for i in indicators if i.get("rda_ref"))
    lines.append("")
    lines.append(f"  the {na} na, partitioned:")
    lines.append(f"    {level0:>3}  Level 0 — pre-FAIRification states, not a target")
    lines.append(f"    {level5:>3}  Level 5 — enterprise data governance, no crate can evidence it")
    lines.append(f"    {hosting_on_ladder:>3}  hosting-environment indicators at levels 1-4")
    lines.append(f"    {other:>3}  content/representation at levels 1-4 with no check yet")
    lines.append("")
    lines.append(f"  {linked} indicators carry the model's own RDA cross-reference.")
    lines.append(
        f"  Ceiling for any crate: level {max(lvl for lvl in levels if lvl not in (0, 5))}"
        " — levels 0 and 5 are off the ladder."
    )
    return "\n".join(lines)


def main() -> None:
    data = build_data()
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    OUT.write_text(_HEADER + "\n" + body)
    print(format_report(data))
    print()
    print(f"Wrote {OUT.relative_to(REPO)} ({len(data['indicators'])} indicators).")


if __name__ == "__main__":
    main()
