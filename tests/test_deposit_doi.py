"""The depositor's declared DOI is read, not minted (#682).

Nothing read it. The crate's own `identifier` was a slug composed from the title —
`inv_neural_cell_screening_models_for_...` — which identifies the deposit to nobody,
so `RDA-F1-01M`, `RDA-F1-02D` and `DSM-1-C0` were False on every crate this tool has
ever built, and DSM-1-C0 gates Level 1 of the FAIRplus ladder.

It was there the whole time. A BioStudies descriptor states the DOI as an attribute in
the same list whose `License` sibling `_read_declared_licence` already walks:

    {"name": "DOI", "value": "10.6019/S-VHPS22"}

Reading it is not inference. Deriving a repository from an accession pattern WOULD be —
these descriptors never name BioStudies, and a detector for that shape was deliberately
removed from `document_discovery` for special-casing one repository's dialect. A DOI
needs no such guess, which is exactly why it is the fact worth reading.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from builder.engine import AgentEngine
from builder.tools.file_readers import extract_deposit_doi

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _descriptor(attributes: list[dict]) -> str:
    return json.dumps({"accno": "S-TEST1", "type": "submission", "attributes": attributes})


class TestExtractDepositDoi:
    def test_reads_the_attribute_the_descriptor_declares(self) -> None:
        text = _descriptor([{"name": "DOI", "value": "10.6019/S-VHPS22"}])
        assert extract_deposit_doi(text) == "https://doi.org/10.6019/S-VHPS22"

    def test_matches_the_attribute_name_case_insensitively(self) -> None:
        for name in ("DOI", "doi", "Doi"):
            text = _descriptor([{"name": name, "value": "10.1234/abc"}])
            assert extract_deposit_doi(text) == "https://doi.org/10.1234/abc", name

    def test_reads_the_field_convention_too(self) -> None:
        """An RO-Crate, CodeMeta or DataCite record states it as a field, not an
        attribute — the same split the licence reader handles."""
        assert extract_deposit_doi('{"doi": "10.5281/zenodo.7464523"}') == (
            "https://doi.org/10.5281/zenodo.7464523"
        )

    def test_an_already_resolvable_doi_is_not_double_prefixed(self) -> None:
        text = _descriptor([{"name": "DOI", "value": "https://doi.org/10.1234/abc"}])
        assert extract_deposit_doi(text) == "https://doi.org/10.1234/abc"

    def test_a_descriptor_without_a_doi_yields_nothing(self) -> None:
        assert extract_deposit_doi(_descriptor([{"name": "Title", "value": "A study"}])) is None
        assert extract_deposit_doi("not json at all") is None

    def test_a_version_string_is_not_a_doi(self) -> None:
        """`10.2` is a version. A DOI's registrant is four digits or more, and the
        suffix is required — a pattern loose enough to match a version number would
        mint an identifier out of a release note."""
        assert extract_deposit_doi('{"version": "10.2"}') is None
        assert extract_deposit_doi('{"doi": "10.123/x"}') is None, "registrant too short"

    def test_every_deposit_on_hand_declares_one(self) -> None:
        """3 of 3. The claim in #682 that this is worth reading rests on this number."""
        found = {}
        for name in ("svhps21_real_input", "svhps22_real_input", "svhps26_real_input"):
            for path in (FIXTURES / name).glob("*.json"):
                if doi := extract_deposit_doi(path.read_text()):
                    found[name] = doi
        assert found == {
            "svhps21_real_input": "https://doi.org/10.6019/S-VHPS21",
            "svhps22_real_input": "https://doi.org/10.6019/S-VHPS22",
            "svhps26_real_input": "https://doi.org/10.6019/S-VHPS26",
        }


class TestTheDoiReachesTheCrate:
    def _initialised(self, tmp_path: Path) -> AgentEngine:
        deposit = tmp_path / "deposit"
        shutil.copytree(FIXTURES / "svhps22_real_input", deposit)
        engine = AgentEngine()
        engine.initialize(str(deposit))
        return engine

    def test_it_is_read_at_initialize_beside_the_licence(self, tmp_path: Path) -> None:
        state = self._initialised(tmp_path).state
        assert state.metadata.doi == "https://doi.org/10.6019/S-VHPS22"
        assert state.metadata.license, "the licence reader still runs beside it"

    def test_it_becomes_the_crate_identifier(self, tmp_path: Path) -> None:
        """Outranking the accession: both identify the deposit, but only the DOI still
        does so outside the repository it came from."""
        from builder.tools.builder import assemble_crate

        engine = self._initialised(tmp_path)
        engine.state.metadata.accession = "S-VHPS22"
        crate = assemble_crate(engine.state, output_dir=None, materialize_payload=False)
        root = next(n for n in crate.metadata.generate()["@graph"] if n.get("@id") == "./")
        assert root["identifier"] == "https://doi.org/10.6019/S-VHPS22"

    def test_it_flips_the_three_indicators_it_was_filed_for(self, tmp_path: Path) -> None:
        from builder.tools import fair_assessment as fa
        from builder.tools.assessment_graph import as_verdict
        from builder.tools.builder import assemble_crate

        engine = self._initialised(tmp_path)
        graph = assemble_crate(
            engine.state, output_dir=None, materialize_payload=False
        ).metadata.generate()["@graph"]
        assert fa._root_pid(graph) == "https://doi.org/10.6019/S-VHPS22"
        assert fa.FAIR_CHECKS["pid_form"](engine.state, graph) is True, "RDA-F1-01M"
        assert fa.FAIR_CHECKS["every_entity_has_id"](engine.state, graph) is True, "RDA-F1-02D"
        unique_id = as_verdict(fa.DSM_CHECKS["unique_id"](engine.state, graph))
        assert unique_id.value is True, "DSM-1-C0"

    def test_a_doi_already_set_is_left_alone(self, tmp_path: Path) -> None:
        """A resumed session, or a value the model supplied, is not overwritten — the
        same rule the licence reader follows."""
        from builder.engine import _read_declared_doi

        engine = self._initialised(tmp_path)
        engine.state.metadata.doi = "https://doi.org/10.9999/mine"
        _read_declared_doi(engine)
        assert engine.state.metadata.doi == "https://doi.org/10.9999/mine"

    def test_it_survives_a_session_round_trip(self, tmp_path: Path) -> None:
        from builder.state import CrateState

        state = self._initialised(tmp_path).state
        assert CrateState.from_json(state.to_json()).metadata.doi == state.metadata.doi
