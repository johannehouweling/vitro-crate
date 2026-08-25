"""The exposure's conditions, read from the worksheet that states them (#697).

Three of the four assays ship a header-only condition table because no file in
them carries per-well rows. But every experiment workbook in this lab's deposits
opens its ``layout`` sheet with a block of label/value pairs stating the
conditions the whole run shared — incubation volume, buffer, substrate, dose,
duration — and those are exactly the columns the condition table is empty in.

Two things this must not do:

* **Read past the block.** Below it sits the design matrix, whose cells are
  adjacent pairs too: a naive scan turns ``D | D`` into a condition called "D".
* **Merge two runs.** An assay holds several experiment workbooks, and their
  blocks disagree — one run used ``H4 + SKNAS`` and another ``MO3.13``. Only what
  every run of an assay AGREES on is a property of the assay; the rest is
  per-run and belongs to #654.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(180)


def _workbook(tmp_path: Path, name: str, sheet: str, rows: list) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    path = tmp_path / name
    wb.save(path)
    return path


# The real shape: a leading blank, the pairs, a blank, then the design matrix.
_UPTAKE = [
    [None, None],
    ["Name", "Nathalie"],
    ["Incubation buffer", "DPBS + 0.1% glucose"],
    ["Substrate", "1 nM T3 or T4"],
    ["Incubation volume (ul)", 375],
    ["cpm per sample", 50000],
    [None, None],
    ["Per cellijn", None],
    ["T3", "DMSO 0.5%", "Sily", "XN  1"],
    ["T4", "DMSO 0.5%", "Sily", "XN  1"],
]

_METABOLISM = [
    [None, None],
    ["Name", "Nathalie"],
    ["Cell type", "H4  + SKNAS"],
    ["Incubation volume", "0.5 ml"],
    ["cpm per sample", 2000000],
    [None, None],
    ["D", "D", "IOP", "IOP"],
    ["CP", "CP", "NH3", "NH3"],
]


class TestTheBlockIsReadAndBounded:
    def test_it_reads_the_label_value_pairs(self, tmp_path):
        from builder.tools.file_readers import read_layout_conditions

        path = _workbook(tmp_path, "run.xlsx", "layout 27-03", _UPTAKE)
        found = read_layout_conditions(path)
        assert found["Substrate"] == "1 nM T3 or T4"
        assert found["Incubation volume (ul)"] == "375"
        assert found["cpm per sample"] == "50000"

    def test_it_stops_before_the_design_matrix(self, tmp_path):
        """`D | D` is two cells of a plate grid, not a condition called D."""
        from builder.tools.file_readers import read_layout_conditions

        path = _workbook(tmp_path, "metab.xlsx", "Layout", _METABOLISM)
        found = read_layout_conditions(path)
        assert set(found) == {"Name", "Cell type", "Incubation volume", "cpm per sample"}, found

    def test_a_leading_blank_row_does_not_end_the_block(self, tmp_path):
        """Every real sheet starts with one."""
        from builder.tools.file_readers import read_layout_conditions

        path = _workbook(tmp_path, "run.xlsx", "layout 01-03", _UPTAKE)
        assert read_layout_conditions(path)

    def test_the_sheet_is_matched_case_insensitively(self, tmp_path):
        """Observed as `layout 27-03`, `layout 01-03` and `Layout`."""
        from builder.tools.file_readers import read_layout_conditions

        for sheet in ("layout 27-03", "Layout", "LAYOUT"):
            path = _workbook(tmp_path, f"{sheet}.xlsx", sheet, _UPTAKE)
            assert read_layout_conditions(path), sheet

    def test_a_workbook_with_no_layout_sheet_yields_nothing(self, tmp_path):
        from builder.tools.file_readers import read_layout_conditions

        path = _workbook(tmp_path, "raw.xlsx", "Raw data", [["Run ID", 4669]])
        assert read_layout_conditions(path) == {}

    def test_an_unreadable_file_yields_nothing_rather_than_raising(self, tmp_path):
        """A deposit holds workbooks no reader can open, and one must not stop a
        build."""
        from builder.tools.file_readers import read_layout_conditions

        broken = tmp_path / "broken.xlsx"
        broken.write_bytes(b"not a workbook")
        assert read_layout_conditions(broken) == {}


class TestOnlyWhatEveryRunAgreesOn:
    """An assay holds several runs, and their blocks disagree."""

    def test_a_value_two_runs_share_is_kept(self, tmp_path):
        from builder.tools.file_readers import shared_layout_conditions

        one = _workbook(tmp_path, "one.xlsx", "Layout", _UPTAKE)
        two = _workbook(
            tmp_path,
            "two.xlsx",
            "Layout",
            [
                r if r[0] != "Incubation buffer" else ["Incubation buffer", "something else"]
                for r in _UPTAKE
            ],
        )
        shared = shared_layout_conditions([one, two])
        assert shared["Substrate"] == "1 nM T3 or T4"
        assert shared["Incubation volume (ul)"] == "375"

    def test_a_value_they_disagree_on_is_dropped(self, tmp_path):
        """`24hour` and `24 hours` mean the same thing and do not say so.
        Normalising them would be a guess; dropping is honest."""
        from builder.tools.file_readers import shared_layout_conditions

        one = _workbook(tmp_path, "one.xlsx", "Layout", [["incubation time(s)", "24hour"]])
        two = _workbook(tmp_path, "two.xlsx", "Layout", [["incubation time(s)", "24 hours"]])
        assert shared_layout_conditions([one, two]) == {}

    def test_a_label_only_one_run_states_is_dropped(self, tmp_path):
        """Present in one run and absent in another says nothing about the assay."""
        from builder.tools.file_readers import shared_layout_conditions

        one = _workbook(tmp_path, "one.xlsx", "Layout", [["Substrate", "T3"], ["Extra", "x"]])
        two = _workbook(tmp_path, "two.xlsx", "Layout", [["Substrate", "T3"]])
        assert shared_layout_conditions([one, two]) == {"Substrate": "T3"}

    def test_a_single_run_keeps_everything_it_states(self, tmp_path):
        """One run cannot disagree with itself."""
        from builder.tools.file_readers import shared_layout_conditions

        one = _workbook(tmp_path, "one.xlsx", "Layout", _UPTAKE)
        assert shared_layout_conditions([one])["Substrate"] == "1 nM T3 or T4"

    def test_no_workbooks_yields_nothing(self, tmp_path):
        from builder.tools.file_readers import shared_layout_conditions

        assert shared_layout_conditions([]) == {}


class TestTheyReachTheExposure:
    """The conditions land on the Exposure whose assay holds the workbook."""

    @pytest.fixture
    def two_assays(self, tmp_path: Path):
        from builder.engine import AgentEngine
        from builder.state import CrateState, Entity, EntityProvenance, FileClassification

        def ent(entity_id, type_, **fields):
            return Entity(
                entity_id=entity_id,
                type=type_,
                fields=fields,
                _provenance=EntityProvenance(created_by="llm"),
            )

        one_dir = tmp_path / "assay_one"
        two_dir = tmp_path / "assay_two"
        one_dir.mkdir()
        two_dir.mkdir()
        book_one = _workbook(one_dir, "run.xlsx", "Layout", _UPTAKE)
        book_two = _workbook(two_dir, "run.xlsx", "Layout", _METABOLISM)

        state = CrateState()
        state.metadata.input_path = str(tmp_path)
        state.metadata.output_path = str(tmp_path / "crate")
        state.add_entity(ent("inv1", "Investigation", name="Inv", description="d"))
        state.add_entity(ent("st1", "Study", name="St", description="d", investigation_id="inv1"))
        for n, (assay, book) in enumerate(((("as1"), book_one), (("as2"), book_two)), start=1):
            state.add_entity(
                ent(
                    assay,
                    "Assay",
                    name=f"Assay {n}",
                    description="d",
                    study_id="st1",
                    hasPart=[f"file{n}"],
                )
            )
            state.add_entity(
                ent(f"file{n}", "File", name="run.xlsx", dest_path=str(book.relative_to(tmp_path)))
            )
            state.add_entity(
                ent(
                    f"exp{n}",
                    "LabProcess",
                    process_type="Exposure",
                    name=f"Exposure {n}",
                    assay_id=assay,
                )
            )
            state.scanned_files.append(
                FileClassification(
                    path=str(book),
                    filename=book.name,
                    size=book.stat().st_size,
                    mime_type="application/vnd.ms-excel",
                )
            )
        state.approved_scan_roots = {str(tmp_path)}
        return AgentEngine(state=state)

    def _params(self, engine, exposure_id):
        entity = next(
            e for e in engine.state.list_entities("LabProcess") if e.entity_id == exposure_id
        )
        refs = entity.fields.get("additionalProperty") or []
        refs = refs if isinstance(refs, list) else [refs]
        by_id = {e.entity_id: e for e in engine.state.list_entities("PropertyValue")}
        return {
            by_id[r].fields.get("name"): by_id[r].fields.get("value") for r in refs if r in by_id
        }

    def test_each_exposure_gets_its_own_assay_s_conditions(self, two_assays):
        from builder.agents.pipeline.pipeline import _apply_layout_conditions

        outcome = _apply_layout_conditions(two_assays)
        assert outcome["exposures"] == 2, outcome
        first = self._params(two_assays, "exp1")
        second = self._params(two_assays, "exp2")
        assert first.get("Substrate") == "1 nM T3 or T4"
        assert "Substrate" not in second, second
        assert second.get("Cell type") == "H4  + SKNAS"

    def test_nothing_from_another_assay_leaks_in(self, two_assays):
        from builder.agents.pipeline.pipeline import _apply_layout_conditions

        _apply_layout_conditions(two_assays)
        assert "Cell type" not in self._params(two_assays, "exp1")

    def test_running_twice_mints_no_duplicates(self, two_assays):
        """The spine re-runs; a parameter must not double."""
        from builder.agents.pipeline.pipeline import _apply_layout_conditions

        _apply_layout_conditions(two_assays)
        before = self._params(two_assays, "exp1")
        _apply_layout_conditions(two_assays)
        assert self._params(two_assays, "exp1") == before
        entity = next(
            e for e in two_assays.state.list_entities("LabProcess") if e.entity_id == "exp1"
        )
        refs = entity.fields.get("additionalProperty") or []
        assert len(refs) == len(set(refs)), refs
