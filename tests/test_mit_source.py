"""The vendored MIT checklist is pinned, and what it drops is counted.

`mit/invitro_tox.yaml` is the only one of the four vendored instruments with no
generator: it is a hand-placed copy of `tox-maturity-indicators`' own
`invitro_tox.yaml`, and until this file nothing asserted anything about it. A
re-vendoring could change the checklist's size, its module split or its
`crate_slot` coverage — every denominator this tool publishes — and no test would
notice (#714). #313 tracks replacing the copy with the package's loader; this pins
the copy meanwhile, and the numbers below are what that swap has to reproduce.

The per-module table is the finding, not the total: **Analysis and Statistics is 7
scorable of 41**. Its bar on the maturity report is drawn over a sixth of the
module the checklist defines, next to five bars drawn over nearly all of theirs.
"""

from __future__ import annotations

import hashlib
import pathlib

import yaml

from builder.tools.mit_assessment import (
    iter_scorable_params,
    load_mit_yaml,
    parse_crate_slots,
    unique_module_params,
)

REPO = pathlib.Path(__file__).resolve().parents[1]
MIT_YAML = REPO / "mit" / "invitro_tox.yaml"

# The vendored bytes. Change this only with the counts below, in the same commit.
MIT_YAML_MD5 = "e7d649b1792e979ab1fb0ce99a8b4aa3"

# module name -> (parameters carrying a parseable crate_slot, parameters defined)
MODULE_SHAPE = {
    "General Information": (21, 28),
    "Chemical Information": (17, 18),
    "Biological Model Information": (53, 53),
    "Exposure Information": (21, 22),
    "Endpoint Read Out Information": (57, 58),
    "Analysis and Statistics": (7, 41),
}


def test_the_vendored_bytes_are_the_ones_these_counts_were_measured_on() -> None:
    assert hashlib.md5(MIT_YAML.read_bytes()).hexdigest() == MIT_YAML_MD5, (
        "the checklist changed; re-measure every count in this file and in the "
        "report's own declaration before updating the pin"
    )


def test_the_checklist_is_220_parameters_in_six_modules() -> None:
    data = load_mit_yaml()
    assert data is not None
    modules = data["modules"]
    assert len(modules) == 6
    published = [p for m in modules for p in unique_module_params(m)]
    assert len(published) == 220


def test_44_parameters_carry_no_crate_slot_and_leave_every_denominator() -> None:
    """Not a defect of the checklist — a limit of this tool's mapping. It has to be
    stated, because the page prints a percentage of the 176 under a heading naming
    the 220."""
    data = load_mit_yaml()
    assert data is not None
    published = [p for m in data["modules"] for p in unique_module_params(m)]
    scorable = list(iter_scorable_params(data))
    assert len(scorable) == 176
    assert len(published) - len(scorable) == 44


def test_the_module_split_is_uneven_and_analysis_is_the_outlier() -> None:
    data = load_mit_yaml()
    assert data is not None
    measured = {}
    for module in data["modules"]:
        params = unique_module_params(module)
        slotted = [p for p in params if parse_crate_slots(p.get("crate_slot", ""))]
        measured[module["name"]] = (len(slotted), len(params))
    assert measured == MODULE_SHAPE
    scorable, published = measured["Analysis and Statistics"]
    assert scorable * 5 < published, "the one module whose bar is mostly unmeasurable"


def test_every_standards_flag_is_a_boolean() -> None:
    """#705 rests on this: a guidance document flags a parameter or does not, with no
    weight and no threshold, so "in compliance with the guideline" has no predicate
    the checklist could answer."""
    data = yaml.safe_load(MIT_YAML.read_text())
    flags = [
        value
        for module in data["modules"]
        for param in unique_module_params(module)
        for value in (param.get("standards") or {}).values()
    ]
    assert flags, "the checklist flags parameters against guidance documents"
    assert all(isinstance(v, bool) for v in flags)


class TestTheReportSaysWhatItLeftOut:
    """A percentage of 176 printed under a heading that names the 220.

    The skip is honest and documented at the traversal (`iter_scorable_params`),
    but it stopped there: `MITReport` carried only what was scored, so the page
    could not say how much of the checklist it was scoring. Same shape as AIR's
    `published_pct`/`pct` pair and the RDA `scoring` block — the instrument's
    denominator and ours, both stated.
    """

    def _report(self):
        from builder.state import CrateState
        from builder.tools.mit_assessment import assess_mit_coverage

        return assess_mit_coverage(CrateState(), graph={"@graph": []})

    def test_the_report_carries_the_checklists_own_total(self) -> None:
        assert self._report().published_total == 220

    def test_the_report_carries_the_published_total_per_module(self) -> None:
        published = self._report().published_module_totals
        assert published == {name: total for name, (_, total) in MODULE_SHAPE.items()}

    def test_a_deserialised_report_keeps_them(self) -> None:
        from builder.state import MITReport

        report = self._report()
        assert MITReport.from_dict(report.to_dict()).published_total == 220

    def test_the_page_states_both_denominators(self) -> None:
        from builder.writers.maturity_report import _render_mit_section

        html = _render_mit_section(self._report())
        assert "176" in html and "220" in html, "both denominators on the page"
        assert "41" in html, "the module whose bar is mostly unmeasurable"
