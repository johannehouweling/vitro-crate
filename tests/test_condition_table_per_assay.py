"""A design table belongs to ONE assay, and must not be written to another (#669).

The search for a design table is crate-wide, but the write targets a single
Exposure taken from ``chain_by_type`` — a dict keyed by process type, so a crate
with four exposures collapses to one and the last wins. On S-VHPS22 the only
table that qualifies is assay 1's tidy export; nothing stopped its 1048 rows
being written into the deiodinase or TR-activation exposure instead. A table
that looks populated and attributes the wrong experiment is worse than the empty
one it replaces.

The crate already says which files belong to which assay — an Assay ``hasPart``
its own files and is ``about`` its own processes — so the scope is read from the
crate rather than from a folder-naming convention that only this deposit has.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState, Entity, EntityProvenance, FileClassification

pytestmark = pytest.mark.timeout(180)

_DESIGN = "well_id,compound\nA1,Amiodarone\nA2,Cisplatin\nA3,Thyroxine\n"


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


@pytest.fixture
def two_assays(tmp_path: Path):
    """Two assays, each with its own exposure and its own design table on disk."""
    one = tmp_path / "assay_one"
    two = tmp_path / "assay_two"
    one.mkdir()
    two.mkdir()
    table_one = one / "design_one.csv"
    table_two = two / "design_two.csv"
    table_one.write_text(_DESIGN, encoding="utf-8")
    table_two.write_text(_DESIGN.replace("Amiodarone", "Silychristin"), encoding="utf-8")

    state = CrateState()
    state.metadata.output_path = str(tmp_path / "crate")
    state.add_entity(_ent("inv1", "Investigation", name="Inv", description="d"))
    state.add_entity(_ent("st1", "Study", name="St", description="d", investigation_id="inv1"))
    # `attach_files` appends the FILE ENTITY id to an Assay's hasPart, and a File
    # entity carries the on-disk source in `path` — so the scope has to resolve
    # through the entity, not compare strings. Built that way here on purpose: a
    # fixture holding raw paths would let an implementation pass that the
    # pipeline's own shape would defeat.
    state.add_entity(
        _ent("file1", "File", name="design_one.csv", dest_path="assay_one/design_one.csv")
    )
    state.add_entity(
        _ent("file2", "File", name="design_two.csv", dest_path="assay_two/design_two.csv")
    )
    state.add_entity(
        _ent("as1", "Assay", name="Assay one", description="d", study_id="st1", hasPart=["file1"])
    )
    state.add_entity(
        _ent("as2", "Assay", name="Assay two", description="d", study_id="st1", hasPart=["file2"])
    )
    state.add_entity(
        _ent("exp1", "LabProcess", process_type="Exposure", name="Exposure one", assay_id="as1")
    )
    state.add_entity(
        _ent("exp2", "LabProcess", process_type="Exposure", name="Exposure two", assay_id="as2")
    )
    for path in (table_one, table_two):
        state.scanned_files.append(
            FileClassification(
                path=str(path),
                filename=path.name,
                size=path.stat().st_size,
                mime_type="text/csv",
            )
        )
    state.approved_scan_roots = {str(tmp_path)}
    # `dest_path` is what an Assay's File entity records, and `_scanned_dest`
    # mirrors the scan under `input_path` to produce it — so the fixture sets the
    # input root the same way a build does, or the two would never meet.
    state.metadata.input_path = str(tmp_path)
    return AgentEngine(state=state), table_one, table_two


class TestAnAssaySeesOnlyItsOwnTables:
    def test_the_scope_is_the_assay_the_exposure_belongs_to(self, two_assays):
        from builder.agents.pipeline.pipeline import _assay_table_paths

        engine, table_one, table_two = two_assays
        assert _assay_table_paths(engine, "exp1") == {str(table_one)}
        assert _assay_table_paths(engine, "exp2") == {str(table_two)}

    def test_candidates_are_restricted_to_that_scope(self, two_assays):
        from builder.agents.pipeline.pipeline import (
            _assay_table_paths,
            _design_table_candidates,
        )

        engine, table_one, _ = two_assays
        found, _unreadable = _design_table_candidates(
            engine, allowed=_assay_table_paths(engine, "exp1")
        )
        assert [p for _w, _m, p in found] == [str(table_one)]

    def test_without_a_scope_both_are_candidates(self, two_assays):
        """The guard is doing work: unscoped, this is the ambiguity that made the
        spine refuse and ship a header-only table."""
        from builder.agents.pipeline.pipeline import _design_table_candidates

        engine, _one, _two = two_assays
        found, _unreadable = _design_table_candidates(engine)
        assert len(found) == 2, found

    def test_an_exposure_with_no_assay_scopes_to_nothing(self, two_assays):
        """Fail closed. An unattributed exposure must not inherit the crate."""
        from builder.agents.pipeline.pipeline import _assay_table_paths

        engine, _one, _two = two_assays
        engine.state.add_entity(_ent("exp3", "LabProcess", process_type="Exposure", name="Orphan"))
        assert _assay_table_paths(engine, "exp3") == set()


class TestEachExposureIsPopulatedFromItsOwnAssay:
    def test_both_exposures_get_their_own_table(self, two_assays):
        from builder.agents.pipeline.pipeline import _populate_condition_tables

        engine, _one, _two = two_assays
        outcome = _populate_condition_tables(engine)
        per = {e["exposure_id"]: e for e in outcome["per_exposure"]}
        assert set(per) == {"exp1", "exp2"}, sorted(per)
        assert all(e["populated"] for e in per.values()), per

    def test_the_rows_written_are_the_assay_s_own(self, two_assays):
        """The failure this exists to stop: assay one's compounds landing in
        assay two's table."""
        from builder.tools._crate_mapping import _condition_table_rel, _mint_id
        from builder.agents.pipeline.pipeline import _populate_condition_tables

        engine, _one, _two = two_assays
        _populate_condition_tables(engine)
        out = Path(engine.state.metadata.output_path)
        by_id = {e.entity_id: e for e in engine.state.list_entities()}
        first = (out / _condition_table_rel(_mint_id(by_id["exp1"]))).read_text()
        second = (out / _condition_table_rel(_mint_id(by_id["exp2"]))).read_text()
        assert "Amiodarone" in first and "Silychristin" not in first
        assert "Silychristin" in second and "Amiodarone" not in second

    def test_the_summary_stays_a_dict_for_the_build_report(self, two_assays):
        """`build.py` reads `populated` / `reason` / `rows` off one dict."""
        from builder.agents.pipeline.pipeline import _populate_condition_tables

        engine, _one, _two = two_assays
        outcome = _populate_condition_tables(engine)
        assert outcome["populated"] is True
        assert outcome["rows"] == 6, outcome
