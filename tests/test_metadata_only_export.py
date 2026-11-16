"""A crate whose data is not public is written without its files.

The deposit of an embargoed study is built like any other: its files are what
the metadata is read from, and the session keeps them. ``metadata_only`` changes
what the EXPORT writes — the study, its assays, steps, compounds, cells, people
and protocol records, and no file. The verdict and the report inside the written
crate describe that crate, so a readout without its result file says so.
"""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path

import pytest

import main
from builder.state import CrateMetadata, CrateState, Entity, EntityProvenance
from builder.tools.builder import export_crate
from builder.tools.data_content import populate_condition_table

pytestmark = pytest.mark.timeout(600)

GENERATED = {"ro-crate-metadata.json", "ro-crate-preview.html", "ro-crate-metadata-maturity.html"}
DEPOSIT_FILES = ("raw/readout.csv", "processed/ic50.csv", "scripts/analysis.R")


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _state(deposit: Path) -> CrateState:
    for rel in DEPOSIT_FILES:
        path = deposit / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("well,value\nA1,1\n", encoding="utf-8")
    s = CrateState()
    s.metadata.input_path = str(deposit)
    s.add_entity(
        _ent("inv_1", "Investigation", name="Thyroid NAM study", description="D3 inhibition")
    )
    s.add_entity(_ent("study_1", "Study", name="Deiodinase study"))
    s.add_entity(_ent("assay_1", "Assay", name="Deiodinase assay", study_id="study_1"))
    s.add_entity(_ent("chem_1", "MolecularEntity", name="Iopanoic acid", identifier="96-83-3"))
    s.add_entity(_ent("cell_a", "CellLineSample", name="SK-N-AS", accession="CVCL_1700"))
    s.add_entity(
        _ent(
            "proc_cult",
            "LabProcess",
            name="Culture neural cells",
            process_type="TestSystemPreparation",
            assay_id="assay_1",
            cell_line=["cell_a"],
            culture_medium="CT medium",
        )
    )
    s.add_entity(
        _ent(
            "proc_exp",
            "LabProcess",
            name="2-hour exposure",
            process_type="Exposure",
            assay_id="assay_1",
            chemicals="chem_1",
            duration="2 hours",
        )
    )
    s.add_entity(_ent("raw_1", "File", name="readout.csv", path="raw/readout.csv"))
    s.add_entity(
        _ent(
            "proc_read",
            "LabProcess",
            name="Luminescence readout",
            process_type="EndpointReadout",
            assay_id="assay_1",
            detection_instrument="GloMax",
            endpoint="deiodinase activity",
            result="raw_1",
        )
    )
    s.add_entity(_ent("script_1", "File", name="analysis.R", path="scripts/analysis.R"))
    s.add_entity(_ent("processed_1", "File", name="ic50.csv", path="processed/ic50.csv"))
    s.add_entity(
        _ent(
            "proc_ana",
            "LabProcess",
            name="IC50 fitting",
            process_type="DataAnalysis",
            assay_id="assay_1",
            software="R drc",
            object="raw_1",
            result="processed_1",
            labprotocol="script_1",
        )
    )
    return s


def _graph(crate_dir: Path) -> list[dict]:
    return json.loads((crate_dir / "ro-crate-metadata.json").read_text(encoding="utf-8"))["@graph"]


def _types(node: dict) -> list[str]:
    found = node.get("@type") or []
    return found if isinstance(found, list) else [found]


def _references(value):
    if isinstance(value, dict):
        if set(value) == {"@id"}:
            yield value["@id"]
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    """One deposit, exported once without its files and once with them."""
    root = tmp_path_factory.mktemp("metadata_only")
    state = _state(root / "deposit")
    state.metadata.metadata_only = True
    before = copy.deepcopy(state.validation)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = export_crate(state, str(root / "public"))
        full = export_crate(_state(root / "deposit_full"), str(root / "full"), validate=False)
    assert result["success"] is True, result
    assert full["success"] is True, full
    return {
        "state": state,
        "validation_before": before,
        "result": result,
        "public": root / "public",
        "full": root / "full",
    }


class TestTheWrittenCrate:
    def test_the_folder_holds_no_data_file(self, exported):
        folder = exported["public"]
        written = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
        assert written == GENERATED

    def test_the_full_export_still_carries_the_files(self, exported):
        for rel in DEPOSIT_FILES:
            assert (exported["full"] / rel).is_file()

    def test_no_deposit_file_is_described(self, exported):
        graph = _graph(exported["public"])
        assert not {node["@id"] for node in graph} & set(DEPOSIT_FILES)
        assert {node["@id"] for node in graph if "File" in _types(node)} <= GENERATED

    def test_no_condition_table_is_described(self, exported):
        graph = _graph(exported["public"])
        assert not [node["@id"] for node in graph if "condition_table" in node["@id"]]
        assert not [n["@id"] for n in graph if any(t.startswith("csvw:") for t in _types(n))]

    def test_nothing_points_at_a_file_that_was_left_out(self, exported):
        graph = _graph(exported["public"])
        ids = {node["@id"] for node in graph}
        dangling = {
            ref
            for node in graph
            for ref in _references(node)
            if not ref.startswith(("http://", "https://")) and ref not in ids
        }
        assert dangling == set()

    def test_the_study_its_steps_and_their_records_stay(self, exported):
        def kept(crate_dir):
            return sorted(
                node["@id"]
                for node in _graph(crate_dir)
                if {"LabProcess", "Sample", "MolecularEntity", "Dataset"} & set(_types(node))
                or _types(node)[:1] == ["LabProtocol"]
            )

        assert kept(exported["public"]) == kept(exported["full"])


class TestTheVerdict:
    def test_it_is_the_verdict_of_the_written_crate(self, exported):
        """A readout whose result file is left out has no result, and the crate says so."""
        validation = exported["result"]["validation"]
        assert validation["ran"] is True
        assert validation["ok"] is False
        page = exported["public"] / "ro-crate-metadata-maturity.html"
        assert "EndpointReadout process MUST have at least one schema:result" in page.read_text(
            encoding="utf-8"
        )

    def test_the_result_says_what_was_left_out(self, exported):
        assert exported["result"]["metadata_only"] is True
        assert exported["result"]["files_left_out"] == len(DEPOSIT_FILES)


class TestTheSession:
    def test_it_keeps_its_files(self, exported):
        state = exported["state"]
        assert len(state.list_entities("File")) == len(DEPOSIT_FILES)
        assert state.get_entity("proc_read").fields["result"] == "raw_1"

    def test_it_keeps_its_own_verdict(self, exported):
        assert exported["state"].validation.to_dict() == exported["validation_before"].to_dict()


def test_a_folder_that_holds_other_files_is_refused(tmp_path):
    """Embargoed files left by an earlier export must not end up beside the metadata."""
    state = _state(tmp_path / "deposit")
    state.metadata.metadata_only = True
    out = tmp_path / "crate"
    (out / "raw").mkdir(parents=True)
    (out / "raw" / "readout.csv").write_text("well,value\n", encoding="utf-8")
    result = export_crate(state, str(out), validate=False)
    assert result["success"] is False
    assert "raw/readout.csv" in result["error"]
    assert not (out / "ro-crate-metadata.json").exists()


def test_no_condition_table_is_written_for_a_metadata_only_session(tmp_path):
    state = _state(tmp_path / "deposit")
    state.metadata.metadata_only = True
    out = tmp_path / "crate"
    outcome = populate_condition_table(state, "proc_exp", [{"well_id": "A1"}], output_dir=str(out))
    assert outcome["ok"] is False
    assert "metadata" in outcome["error"]
    assert not out.exists() or not [p for p in out.rglob("*") if p.is_file()]


class TestTheSetting:
    def test_it_is_off_unless_asked_for(self):
        assert CrateMetadata().metadata_only is False
        assert "metadata_only" not in CrateMetadata().to_dict()

    def test_it_is_kept_with_the_session(self):
        kept = CrateMetadata.from_dict(CrateMetadata(metadata_only=True).to_dict())
        assert kept.metadata_only is True

    def test_the_command_line_sets_it_either_way_or_leaves_it(self):
        assert main.parse_args(["--metadata-only"]).metadata_only is True
        assert main.parse_args(["--no-metadata-only"]).metadata_only is False
        assert main.parse_args([]).metadata_only is None
