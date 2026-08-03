"""Tests for Exposure condition-table population + Frictionless bridge (Issue #144).

``populate_condition_table`` writes per-well rows into the Exposure condition
table CSV (replacing the header-only placeholder from #94). ``csvw_to_frictionless``
converts the CSVW column descriptors into the Frictionless ``{fields: [...]}``
shape so ``validate_table`` needs no hand-authored schema.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import _CONDITION_TABLE_COLUMNS
from builder.tools.data_content import (
    csvw_to_frictionless,
    populate_condition_table,
    project_condition_rows,
    validate_table,
)

# The committed per-well fixture the pipeline actually meets in the corpus. Anchored
# to this file, not the CWD — a test below deliberately chdirs. The fixture is owned
# by the eval corpus, so ``test_the_corpus_fixture_still_requires_aliasing`` guards
# against it drifting to canonical column names and quietly making the end-to-end
# case below vacuous.
_FIXTURE_CSV = (
    Path(__file__).parent / "fixtures" / "svhps22_input" / "raw_data" / "dose_response_raw.csv"
)


def _exposure_state() -> CrateState:
    state = CrateState()
    state.metadata.title = "Exposure crate"
    state.add_entity(
        Entity(
            entity_id="proc_exp",
            type="LabProcess",
            fields={"process_type": "Exposure", "name": "Exposure step"},
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


def test_csvw_to_frictionless_maps_columns():
    schema = csvw_to_frictionless(_CONDITION_TABLE_COLUMNS)
    assert "fields" in schema
    names = [f["name"] for f in schema["fields"]]
    # The full 10-column condition-table schema (Issue #180, Lane D).
    assert names == [
        "well_id",
        "assay",
        "cell_line",
        "compound",
        "concentration_value",
        "concentration_unit",
        "exposure_duration",
        "experiment",
        "technical_replicate",
        "control",
    ]
    by_name = {f["name"]: f for f in schema["fields"]}
    # double -> number, string -> string (Frictionless types)
    assert by_name["concentration_value"]["type"] == "number"
    assert by_name["cell_line"]["type"] == "string"


def test_populate_condition_table_writes_rows(tmp_path):
    state = _exposure_state()
    # Population fills whatever columns the data provides; the schema describes
    # all 10, missing columns are written empty (extrasaction="ignore").
    rows = [
        {"well_id": "A1", "cell_line": "HepG2", "compound": "Aspirin",
         "concentration_value": "10", "concentration_unit": "uM",
         "exposure_duration": "24h"},
        {"well_id": "A2", "cell_line": "HepG2", "compound": "Aspirin",
         "concentration_value": "100", "concentration_unit": "uM",
         "exposure_duration": "24h"},
    ]
    result = populate_condition_table(state, "proc_exp", rows, output_dir=str(tmp_path))
    assert result["ok"] is True
    csv_path = result["path"]
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = list(csv.DictReader(fh))
    assert len(reader) == 2
    assert reader[0]["concentration_value"] == "10"
    assert reader[1]["concentration_value"] == "100"
    assert reader[0]["well_id"] == "A1"


def test_populated_table_validates_with_inferred_schema(tmp_path):
    state = _exposure_state()
    rows = [
        {"well_id": "A1", "cell_line": "HepG2", "compound": "Aspirin",
         "concentration_value": "10", "concentration_unit": "uM",
         "exposure_duration": "24h"},
    ]
    result = populate_condition_table(state, "proc_exp", rows, output_dir=str(tmp_path))
    schema = csvw_to_frictionless(_CONDITION_TABLE_COLUMNS)
    report = validate_table(result["path"], schema)
    assert report["ok"] is True, report


def test_populated_table_rejects_non_numeric_concentration(tmp_path):
    """The inferred CSVW schema TYPES ``concentration_value`` as a number, so a row
    whose value is valid-as-string but not-a-number must FAIL content validation with
    an error routed to that column.

    The happy-path test above would still pass even if the column were typed as a
    plain string — this negative case is what makes the number-typing meaningful.
    """
    state = _exposure_state()
    rows = [
        {"well_id": "A1", "cell_line": "HepG2", "compound": "Aspirin",
         "concentration_value": "abc",  # not a number — must be rejected by the type
         "concentration_unit": "uM", "exposure_duration": "24h"},
    ]
    result = populate_condition_table(state, "proc_exp", rows, output_dir=str(tmp_path))
    schema = csvw_to_frictionless(_CONDITION_TABLE_COLUMNS)
    report = validate_table(result["path"], schema)

    assert report["ok"] is False, report
    assert any(
        "concentration_value" in str(issue.get("property", ""))
        or "concentration_value" in str(issue.get("message", ""))
        for issue in report["issues"]
    ), report["issues"]


class TestProjectConditionRows:
    """Source headers are aliased onto the ten canonical titles (#381a).

    ``populate_condition_table`` writes through ``csv.DictWriter(...,
    extrasaction="ignore")``, so any source column not named *exactly* like a
    canonical title was silently dropped and its canonical slot written empty.
    Real plate maps do not use the canonical names.
    """

    def test_projects_the_committed_fixture_header(self) -> None:
        # The real corpus fixture: well,compound,cell_line,concentration_uM,
        # tpo_activity_rfu. Only 2 of 10 titles matched before aliasing, so the
        # dose axis — the entire point of the table — landed blank.
        rows = [
            {
                "well": "A1",
                "compound": "Methimazole",
                "cell_line": "FRTL-5",
                "concentration_uM": "0.3",
                "tpo_activity_rfu": "15110",
            }
        ]
        result = project_condition_rows(rows)
        got = result["rows"][0]
        assert got["well_id"] == "A1"
        assert got["compound"] == "Methimazole"
        assert got["cell_line"] == "FRTL-5"
        assert got["concentration_value"] == "0.3"
        assert got["concentration_unit"] == "uM"

    def test_reports_measurement_columns_as_unmapped_never_swallowed(self) -> None:
        rows = [{"well": "A1", "tpo_activity_rfu": "15110"}]
        result = project_condition_rows(rows)
        assert "tpo_activity_rfu" in result["unmapped_source_columns"]

    def test_suffix_unit_rule_splits_value_and_unit(self) -> None:
        result = project_condition_rows([{"well": "A1", "dose_mM": "2.5"}])
        assert result["rows"][0]["concentration_value"] == "2.5"
        assert result["rows"][0]["concentration_unit"] == "mM"

    def test_unit_stays_a_literal_string_never_an_ontology_iri(self) -> None:
        # D5: normalising a unit to a UO IRI needs an authoritative lookup. The
        # suffix is prose from a filename — it is carried verbatim, never lifted.
        unit = project_condition_rows([{"well": "A1", "conc_uM": "1"}])["rows"][0][
            "concentration_unit"
        ]
        assert unit == "uM"
        assert "://" not in unit and ":" not in unit

    def test_canonical_name_wins_over_an_alias(self) -> None:
        # Both present: the canonical column is authoritative, the alias is ignored.
        rows = [{"well_id": "CANON", "well": "ALIAS"}]
        assert project_condition_rows(rows)["rows"][0]["well_id"] == "CANON"

    def test_alias_does_not_overwrite_a_populated_canonical_value(self) -> None:
        rows = [{"concentration_value": "10", "concentration_unit": "nM", "dose_uM": "99"}]
        got = project_condition_rows(rows)["rows"][0]
        assert got["concentration_value"] == "10"
        assert got["concentration_unit"] == "nM"

    def test_reports_which_canonical_columns_were_mapped(self) -> None:
        result = project_condition_rows([{"well": "A1", "chemical": "Aspirin"}])
        assert set(result["mapped_columns"]) == {"well_id", "compound"}

    def test_the_stale_react_tool_column_names_now_land(self) -> None:
        # react/tools_spec.py advertised the pre-#180 names concentration / unit /
        # duration. All three were dropped by extrasaction="ignore", so a model
        # obeying the tool description verbatim wrote an empty table.
        rows = [
            {
                "well": "A1",
                "cell_line": "HepG2",
                "compound": "Aspirin",
                "concentration": "10",
                "unit": "uM",
                "duration": "24h",
            }
        ]
        got = project_condition_rows(rows)["rows"][0]
        assert got["concentration_value"] == "10"
        assert got["concentration_unit"] == "uM"
        assert got["exposure_duration"] == "24h"


class TestPopulateRefusesRatherThanBlanking:
    """Writing a table of blank cells is worse than the honest header (#381a)."""

    def _dest(self, tmp_path: Path) -> Path:
        return tmp_path / "data" / "proc_exp_condition_table.csv"

    def test_refuses_when_no_canonical_column_maps(self, tmp_path) -> None:
        state = _exposure_state()
        rows = [{"instrument": "plate reader", "operator": "JH"}]
        result = populate_condition_table(state, "proc_exp", rows, output_dir=str(tmp_path))
        assert result["ok"] is False
        assert result["unmapped_source_columns"]

    def test_refusal_writes_nothing_at_all(self, tmp_path) -> None:
        # The critical half: a refusal must not leave a blanked file behind, and
        # must not clobber a header the build already wrote.
        state = _exposure_state()
        populate_condition_table(
            state, "proc_exp", [{"instrument": "plate reader"}], output_dir=str(tmp_path)
        )
        assert not self._dest(tmp_path).exists()

    def test_refuses_when_no_row_resolves_a_well_id(self, tmp_path) -> None:
        # A per-well design table without a well key is not a design table.
        state = _exposure_state()
        rows = [{"compound": "Aspirin", "cell_line": "HepG2"}]
        result = populate_condition_table(state, "proc_exp", rows, output_dir=str(tmp_path))
        assert result["ok"] is False
        assert not self._dest(tmp_path).exists()

    def test_success_still_reports_the_skipped_columns(self, tmp_path) -> None:
        state = _exposure_state()
        rows = [{"well": "A1", "compound": "Aspirin", "tpo_activity_rfu": "18420"}]
        result = populate_condition_table(state, "proc_exp", rows, output_dir=str(tmp_path))
        assert result["ok"] is True
        assert "tpo_activity_rfu" in result["unmapped_source_columns"]

    def test_the_corpus_fixture_still_requires_aliasing(self) -> None:
        """Anti-drift guard for the end-to-end case below.

        The fixture belongs to the eval corpus, not to this test. If its header
        ever became canonical, the end-to-end assertion would still pass while
        exercising no aliasing at all — a tautology. Pin the two properties the
        end-to-end case actually depends on: the header is non-canonical, and it
        carries a suffix-unit dose column.
        """
        with open(_FIXTURE_CSV, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        canonical = {c["titles"] for c in _CONDITION_TABLE_COLUMNS}
        assert set(header) - canonical, (
            f"fixture header {header} is now fully canonical — the end-to-end test "
            "below no longer exercises aliasing and must be rewritten"
        )
        assert "well" in header and "well_id" not in header
        assert any(c.startswith(("concentration_", "conc_", "dose_")) for c in header)

    def test_aliased_plate_map_lands_real_values_end_to_end(self, tmp_path) -> None:
        # The whole point: the committed fixture, read from disk, produces a table
        # whose dose axis is populated rather than blank.
        state = _exposure_state()
        result = populate_condition_table(
            state, "proc_exp", str(_FIXTURE_CSV), output_dir=str(tmp_path)
        )
        assert result["ok"] is True, result
        with open(result["path"], newline="", encoding="utf-8") as fh:
            written = list(csv.DictReader(fh))
        assert len(written) > 1
        assert [r["concentration_value"] for r in written] != [""] * len(written)
        assert all(r["well_id"] for r in written)
        assert {r["concentration_unit"] for r in written} == {"uM"}


class TestPopulatePathResolution:
    def test_falls_back_to_the_session_crate_path_not_cwd(self, tmp_path, monkeypatch) -> None:
        # data_content resolved a missing output_dir to Path.cwd() while
        # export_crate resolves it to _default_crate_path(state). Rows written to
        # the wrong root are silently lost at export.
        from builder.tools.builder import _default_crate_path

        state = _exposure_state()
        state.session_id = "sess-381"
        monkeypatch.chdir(tmp_path)
        result = populate_condition_table(
            state, "proc_exp", [{"well": "A1", "compound": "Aspirin"}]
        )
        assert result["ok"] is True, result
        assert Path(result["path"]).is_relative_to(Path(_default_crate_path(state)))


class TestReactToolDescriptionMatchesTheCode:
    """The advertised columns must be the real ones (#381a).

    The ReAct description named the pre-#180 five-column set long after those
    columns stopped existing, so a model that obeyed it verbatim had every value
    discarded by ``extrasaction="ignore"``. Nothing tested the description, which
    is precisely how it drifted. These pin it to the code.
    """

    def _spec(self) -> dict:
        from builder.agents.react.tools_spec import TOOL_SPECS

        return next(t for t in TOOL_SPECS if t["name"] == "populate_condition_table")

    def test_every_canonical_column_is_advertised(self) -> None:
        text = str(self._spec())
        for column in (c["titles"] for c in _CONDITION_TABLE_COLUMNS):
            assert column in text, f"tool description does not mention {column!r}"

    def test_retired_column_names_are_not_advertised_as_keys(self) -> None:
        # "concentration"/"unit"/"duration" survive only as substrings of the
        # canonical names, so assert on the delimited forms a model would copy.
        text = str(self._spec())
        for retired in ("concentration/unit/duration", "concentration/unit", "unit/duration"):
            assert retired not in text, f"stale column list {retired!r} still advertised"

    def test_the_refusal_contract_is_documented(self) -> None:
        # A tool that can refuse must say so, or the caller reads a failure as a bug.
        description = self._spec()["description"].lower()
        assert "refus" in description
        assert "unmapped_source_columns" in description


@pytest.mark.parametrize(
    "source,canonical",
    [
        ("well", "well_id"),
        ("well_position", "well_id"),
        ("conc", "concentration_value"),
        ("units", "concentration_unit"),
        ("exposure_time", "exposure_duration"),
        ("cell", "cell_line"),
        ("substance", "compound"),
        ("test_item", "compound"),
        ("replicate", "technical_replicate"),
    ],
)
def test_alias_table_covers_the_documented_synonyms(source: str, canonical: str) -> None:
    got = project_condition_rows([{source: "X"}])["rows"][0]
    assert got[canonical] == "X"


class TestMultivaluedColumns:
    """#408 (c) — a populated column may no longer carry a column-wide ``valueUrl``.

    ``_build_condition_table_schema`` asserts ``cells[0]`` / ``chems[0]`` for the
    WHOLE cell_line / compound column. At zero rows that claim is vacuous. Once
    rows exist it says every value in the column resolves to that one entity — so
    populating a multi-compound plate converts unverified prose into false
    entity-resolved claims. D5 says refuse the claim, not inherit it.
    """

    def _write(self, tmp_path: Path, rows: list[dict[str, str]]) -> Path:
        csv_path = tmp_path / "condition_table.csv"
        titles = [c["titles"] for c in _CONDITION_TABLE_COLUMNS]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=titles, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return csv_path

    def test_a_single_valued_column_is_not_multivalued(self, tmp_path: Path) -> None:
        from builder.tools.data_content import condition_table_multivalued_columns

        csv_path = self._write(
            tmp_path,
            [
                {"well_id": "A1", "cell_line": "CHO-K1", "compound": "T4"},
                {"well_id": "A2", "cell_line": "CHO-K1", "compound": "T4"},
            ],
        )
        got = condition_table_multivalued_columns(str(csv_path))
        assert "compound" not in got
        assert "cell_line" not in got
        # Every well differs by design, so well_id IS multivalued — the function
        # reports any canonical column, and the caller decides which ones matter.
        assert got == {"well_id"}

    def test_a_multi_compound_plate_reports_the_compound_column(self, tmp_path: Path) -> None:
        from builder.tools.data_content import condition_table_multivalued_columns

        csv_path = self._write(
            tmp_path,
            [
                {"well_id": "A1", "cell_line": "CHO-K1", "compound": "T4"},
                {"well_id": "A2", "cell_line": "CHO-K1", "compound": "T3"},
            ],
        )
        got = condition_table_multivalued_columns(str(csv_path))
        assert "compound" in got
        assert "cell_line" not in got, "one cell line across both wells — still single-valued"

    def test_non_canonical_source_columns_are_ignored(self, tmp_path: Path) -> None:
        """A verbatim-copied plate map's extra headers have no CSVW column to guard."""
        from builder.tools.data_content import condition_table_multivalued_columns

        csv_path = tmp_path / "verbatim.csv"
        csv_path.write_text(
            "well_id,compound,tpo_activity_rfu\nA1,T4,101\nA2,T4,202\n", encoding="utf-8"
        )
        assert "tpo_activity_rfu" not in condition_table_multivalued_columns(str(csv_path))

    def test_blanks_do_not_count_as_a_distinct_value(self, tmp_path: Path) -> None:
        """An empty cell is absence, not a second compound."""
        from builder.tools.data_content import condition_table_multivalued_columns

        csv_path = self._write(
            tmp_path,
            [
                {"well_id": "A1", "compound": "T4"},
                {"well_id": "A2", "compound": ""},
                {"well_id": "A3", "compound": "  "},
            ],
        )
        assert "compound" not in condition_table_multivalued_columns(str(csv_path))

    def test_a_header_only_table_is_not_multivalued(self, tmp_path: Path) -> None:
        """The pre-#408 state: no rows, so nothing to contradict."""
        from builder.tools.data_content import condition_table_multivalued_columns

        assert condition_table_multivalued_columns(str(self._write(tmp_path, []))) == set()

    def test_a_missing_file_is_not_multivalued(self, tmp_path: Path) -> None:
        """The in-memory validate path has no CSV on disk — must not raise."""
        from builder.tools.data_content import condition_table_multivalued_columns

        assert condition_table_multivalued_columns(str(tmp_path / "nope.csv")) == set()


class TestValueUrlDropsOnMultivaluedColumn:
    """#408 (c), through the real crate mapping + export — not the helper alone.

    The Exposure's ``chemicals`` ref field and its cultured-sample ``object`` are
    what feed ``cells``/``chems`` into ``_build_condition_table_schema``, so these
    build the same wiring ``_materialize_plan`` produces.
    """

    _REL = Path("data") / "LabProcess_proc_exp_condition_table.csv"

    def _state(self, compound_ids: list[str]) -> CrateState:
        state = CrateState()
        state.metadata.title = "Exposure crate"

        def add(eid: str, type_: str, **fields: object) -> None:
            state.add_entity(
                Entity(
                    entity_id=eid,
                    type=type_,  # ty: ignore[invalid-argument-type]
                    fields=fields,
                    _provenance=EntityProvenance(created_by="llm"),
                )
            )

        add("samp_c", "Sample", name="Cultured CHO-K1")
        for eid in compound_ids:
            add(eid, "MolecularEntity", name=eid)
        add(
            "proc_exp",
            "LabProcess",
            process_type="Exposure",
            name="Exposure step",
            chemicals=compound_ids,
            object=["samp_c"],
        )
        return state

    def _export_value_urls(
        self, out_dir: Path, csv_body: str, compound_ids: list[str]
    ) -> dict[str, str | None]:
        """Pre-populate the condition CSV, export, and read back column valueUrls."""
        import json

        from builder.tools.builder import export_crate

        dest = out_dir / self._REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(csv_body, encoding="utf-8")

        state = self._state(compound_ids)
        state.metadata.output_path = str(out_dir)
        export_crate(state)

        doc = json.loads((out_dir / "ro-crate-metadata.json").read_text(encoding="utf-8"))
        urls: dict[str, str | None] = {}
        for node in doc["@graph"]:
            if "csvw:Column" not in str(node.get("@type", "")):
                continue
            raw = node.get("valueUrl")
            urls[str(node.get("titles") or "")] = (
                raw.get("@id") if isinstance(raw, dict) else raw
            )
        return urls

    _HEADER = "well_id,assay,cell_line,compound\n"

    def test_single_valued_compound_column_keeps_its_valueurl(self, tmp_path: Path) -> None:
        """The control: existing #94/#180 behaviour must survive the change."""
        urls = self._export_value_urls(
            tmp_path / "crate",
            self._HEADER + "A1,uptake,CHO-K1,Thyroxine\n",
            ["chem_t4"],
        )
        assert urls.get("compound"), "a single-compound plate must keep its column valueUrl"
        assert urls.get("cell_line"), "a single-cell-line plate must keep its column valueUrl"

    def test_multi_compound_column_drops_its_valueurl(self, tmp_path: Path) -> None:
        urls = self._export_value_urls(
            tmp_path / "crate",
            self._HEADER + "A1,uptake,CHO-K1,Thyroxine\nA2,uptake,CHO-K1,Triiodothyronine\n",
            ["chem_t4", "chem_t3"],
        )
        assert urls.get("compound") is None, (
            "two compounds in the column — a column-wide valueUrl would be a false claim"
        )
        # cell_line is still single-valued, so that claim stands: the guard is
        # per-column, not a blanket retreat.
        assert urls.get("cell_line"), "single-valued cell_line must keep its valueUrl"

    def test_header_only_table_keeps_its_valueurl(self, tmp_path: Path) -> None:
        """The pre-#408 vacuous case is unchanged — no rows contradict the claim."""
        urls = self._export_value_urls(tmp_path / "crate", self._HEADER, ["chem_t4"])
        assert urls.get("compound"), "a header-only table asserts nothing to contradict"

    def test_populated_rows_survive_the_export(self, tmp_path: Path) -> None:
        """`if not dest.exists()` + ro-crate-py's samefile guard must not clobber rows."""
        out = tmp_path / "crate"
        body = self._HEADER + "A1,uptake,CHO-K1,Thyroxine\nA2,uptake,CHO-K1,Triiodothyronine\n"
        self._export_value_urls(out, body, ["chem_t4", "chem_t3"])
        assert (out / self._REL).read_text(encoding="utf-8") == body
