#!/usr/bin/env python3
"""Generate ``fair/indicators.yaml`` from the vendored RDA FAIR Data Maturity Model.

Issue #356. The generic FAIR indicator *definitions* (dimension, priority, text) are
sourced from the canonical RDA FAIR Data Maturity Model spreadsheet
(``fair/rda_fdmm.xlsx``, DOI 10.15497/rda00050, CC-BY-4.0) rather than hand-authored,
so they cannot silently drift. The *local* decision — which indicators this tool can
assess intrinsically from a single RO-Crate, and with which check function — lives in
:data:`LOCAL_SCOPE` below; that is inherently repo-specific and stays here.

FAIR *scoring* stays local and offline: every automated FAIR evaluator (F-UJI, the
FAIR Evaluator, FAIROS) needs a published, resolvable URL and cannot score a local,
pre-publication crate. Only the indicator definitions are externalised.

The FAIRplus DSM ladder (``fair/dsm_indicators.yaml``) is generated from its own
vendored assessment workbook by ``scripts/gen_dsm_indicators.py`` — not touched here.

Regenerate with::

    uv run python scripts/gen_fair_indicators.py

``tests/test_fair_indicators_source.py`` asserts the committed file equals this
generator's output and that every indicator matches its RDA row.
"""

from __future__ import annotations

import pathlib
from typing import Any

import openpyxl
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
RDA_XLSX = REPO / "fair" / "rda_fdmm.xlsx"
OUT = REPO / "fair" / "indicators.yaml"

# RDA indicator sheet: header row 0; columns PRINCIPLE(3), INDICATOR_ID(4),
# INDICATORS/text(5), PRIORITY(6).
_SHEET = "FAIR Indicators_v0.05"

# The crate-intrinsic subset this tool assesses, in report order: indicator id ->
# check-function name (see builder/tools/fair_assessment.py FAIR_CHECKS), or None to
# mark it out-of-scope (assessable only with repository/protocol context, e.g. most
# Accessibility indicators — reported as out-of-scope, not failed).
LOCAL_SCOPE: list[tuple[str, str | None]] = [
    # Findable
    ("RDA-F1-02M", "root_global_id"),
    ("RDA-F1-02D", "every_entity_has_id"),
    ("RDA-F1-01M", "pid_form"),
    ("RDA-F2-01M", "rich_metadata"),
    ("RDA-F3-01M", "metadata_refs_data"),
    # Accessible (intrinsically out-of-scope: protocol / repository level)
    ("RDA-A1-02M", None),
    ("RDA-A1-04D", None),
    ("RDA-A2-01M", None),
    # Interoperable
    ("RDA-I1-01M", "jsonld_context"),
    ("RDA-I1-02M", "jsonld_context"),
    ("RDA-I2-01M", "fair_vocabularies"),
    ("RDA-I3-01M", "qualified_refs"),
    ("RDA-I3-03M", "qualified_refs"),
    # Reusable
    ("RDA-R1-01M", "reuse_attributes"),
    ("RDA-R1.1-01M", "license_present"),
    ("RDA-R1.1-02M", "license_standard"),
    ("RDA-R1.1-03M", "license_machine"),
    ("RDA-R1.2-01M", "provenance"),
    ("RDA-R1.3-01M", "conforms_to_profile"),
    ("RDA-R1.3-02M", "conforms_to_profile"),
    # R1.3-01D delegates to OECD MIT in-vitro coverage (the community reporting
    # standard); see builder/tools/mit_assessment.py and issue #313.
    ("RDA-R1.3-01D", "mit_coverage"),
]

SOURCES: dict[str, Any] = {
    "rda": {
        "name": "RDA FAIR Data Maturity Model",
        "citation": "Bahim et al. 2020, Data Science Journal 19(1):41",
        "doi": "10.15497/rda00050",
        "distribution": (
            "FAIR_evaluation_levels_v0.02.xlsx (sheet 'FAIR Indicators_v0.05'), "
            "Zenodo record 3909563 — vendored as fair/rda_fdmm.xlsx"
        ),
        "license": "CC-BY-4.0",
        "retrieved": "2026-07-25",
    },
    "dsm": {
        "name": "FAIRplus Dataset Maturity Model (DSM)",
        "url": "https://fairplus.github.io/Data-Maturity/",
        "note": (
            "Generated from the vendored assessment workbook by "
            "scripts/gen_dsm_indicators.py into fair/dsm_indicators.yaml, and scored "
            "by builder/tools/fair_assessment.py."
        ),
    },
    "nsdra": {
        "name": "Nanosafety data reusability (NSDRA)",
        "citation": "Ammar, Evelo & Willighagen 2024, Scientific Data 11:503",
        "doi": "10.1038/s41597-024-03324-x",
    },
}

_HEADER = """\
# GENERATED FILE — do not edit by hand.
#
# Regenerate with:  uv run python scripts/gen_fair_indicators.py
#
# The FAIR indicator definitions (dimension / priority / text) are derived from the
# vendored RDA FAIR Data Maturity Model spreadsheet (fair/rda_fdmm.xlsx,
# doi:10.15497/rda00050, CC-BY-4.0). The local scope/check mapping (which indicators
# are assessed intrinsically from one RO-Crate, and with which check) lives in
# scripts/gen_fair_indicators.py. Accessibility indicators are protocol/repository
# level and reported out-of-scope, not failed. R1.3-01D delegates to OECD MIT
# in-vitro coverage. The FAIRplus DSM ladder is generated separately into
# fair/dsm_indicators.yaml by scripts/gen_dsm_indicators.py.
"""


def _load_rda() -> dict[str, dict[str, str]]:
    """Canonical RDA indicator definitions keyed by indicator id."""
    wb = openpyxl.load_workbook(RDA_XLSX, read_only=True, data_only=True)
    ws = wb[_SHEET]
    out: dict[str, dict[str, str]] = {}
    for row in list(ws.iter_rows(values_only=True))[1:]:
        iid = row[4]
        if iid and str(iid).startswith("RDA-"):
            out[str(iid)] = {
                "dimension": str(row[3])[0],
                "priority": str(row[6]).strip().lower(),
                "text": str(row[5]).strip(),
            }
    return out


def build_data() -> dict[str, Any]:
    """The full ``indicators.yaml`` payload: sources + RDA-sourced indicators."""
    rda = _load_rda()
    indicators: list[dict[str, Any]] = []
    for iid, check in LOCAL_SCOPE:
        if iid not in rda:
            raise KeyError(f"{iid} is not an RDA FAIR Data Maturity Model indicator")
        d = rda[iid]
        entry: dict[str, Any] = {
            "id": iid,
            "dimension": d["dimension"],
            "priority": d["priority"],
        }
        if check is None:
            entry["scope"] = "out_of_scope"
        else:
            entry["check"] = check
        entry["text"] = d["text"]
        indicators.append(entry)
    return {"sources": SOURCES, "indicators": indicators}


def main() -> None:
    body = yaml.safe_dump(
        build_data(), sort_keys=False, allow_unicode=True, width=100
    )
    OUT.write_text(_HEADER + "\n" + body)
    print(f"Wrote {OUT.relative_to(REPO)} ({len(LOCAL_SCOPE)} indicators from RDA).")


if __name__ == "__main__":
    main()
