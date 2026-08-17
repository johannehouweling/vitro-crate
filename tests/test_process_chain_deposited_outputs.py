"""A deposited data file is the step's result, not a synthesized empty one (#589).

``draft_process_chain`` closes the §14.3 "no output fallback" trap by
synthesizing a placeholder File for EndpointReadout / DataAnalysis. It did so
**unconditionally** — never asking whether the deposit already contained the
file the step produced. On a real submission that inverted the crate's metadata
against its evidence: 21 processes each pointed at a header-only stub carrying a
``csvw:Table`` type, a schema and typed columns, while the deposit's own
1048-row measurement table sat beside them as a bare ``File`` that no process
referenced (`output/svhps22_real_input_crate_v6`: 0 of 56 payload files wired).

The rule these tests pin, in the depositor's own vocabulary: **raw files are
what the EndpointReadout produced; processed files are what the DataAnalysis
produced.**

The hard case is generality, and it is not hypothetical — the three real
fixtures do not share one convention:

* ``svhps22`` separates ``raw data/`` from ``assay1_processeddata/``;
* ``svhps21`` and ``svhps26`` put both tiers in ONE directory
  (``Raw data + individual processed data/``).

A folder-name rule that only reads "raw" would hand every processed file in
those two deposits to the EndpointReadout. So a directory naming both tiers
resolves to neither, on the ``_populate_condition_table_from_plan`` precedent
(#408) of refusing to guess between ambiguous candidates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.state import CrateState, FileClassification
from builder.tools.composites import draft_process_chain, scaffold_isa_backbone

_DEPOSIT = "/deposit"


def _scanned(rel: str) -> FileClassification:
    """A scanned-file record for a deposit-relative path."""
    return FileClassification(
        path=f"{_DEPOSIT}/{rel}",
        filename=Path(rel).name,
        size=4096,
        mime_type="text/csv",
        first_rows=None,
    )


def _scaffold(paths: list[str], *, assays: int = 1) -> tuple[CrateState, list[str]]:
    """A backbone with ``paths`` scanned; returns (state, assay_ids)."""
    state = CrateState()
    state.metadata.title = "Deposited-output crate"
    state.metadata.input_path = _DEPOSIT
    ids = scaffold_isa_backbone(
        state,
        investigation={"name": "Inv", "description": "d", "identifier": "INV-1"},
        study={"name": "Study", "description": "d"},
        assay={"name": "Assay 1", "description": "d"},
    )
    assay_ids = [ids["assay_id"]]
    for n in range(2, assays + 1):
        from builder.tools.drafters import draft_assay

        extra = draft_assay(
            state, ids["study_id"], {"name": f"Assay {n}", "description": "d"}
        )
        assay_ids.append(extra.entity_id)
    state.scanned_files = [_scanned(p) for p in paths]
    return state, assay_ids


_CHAIN = [
    {
        "process_type": "EndpointReadout",
        "hints": {"name": "Read", "detection_instrument": "Gamma counter"},
    },
    {"process_type": "DataAnalysis", "hints": {"name": "Analyse", "data_processing": "AUC"}},
]


def _result_entities(state: CrateState, process_type: str) -> list:
    """The entities a step's ``result`` points at."""
    proc = next(
        p
        for p in state.list_entities("LabProcess")
        if p.fields.get("process_type") == process_type
    )
    value = proc.fields.get("result")
    ids = value if isinstance(value, list) else [value]
    out = []
    for i in ids:
        key = i.get("@id") if isinstance(i, dict) else i
        if key:
            entity = state.get_entity(str(key).lstrip("#"))
            if entity is not None:
                out.append(entity)
    return out


def _sources(entities: list) -> set[str]:
    """The deposit-relative paths behind a set of File entities.

    Read from ``dest_path`` — the crate-relative destination, which mirrors the
    file's place in the deposit — rather than from ``_file_source``, which
    resolves only for files that exist on disk and would report every entity here
    as unsourced.
    """
    return {str(e.fields.get("dest_path") or e.entity_id) for e in entities}


# --- separated tiers: the svhps22 shape ------------------------------------

_SEPARATED = [
    "assay_01/raw data/004668.csv",
    "assay_01/characterisation/assay1_rawdata/004043.csv",
    "assay_01/characterisation/assay1_processeddata/combined 0-60 min.csv",
    # Filed WITH the measurements, so only the format family keeps it out — the
    # tier alone would promote a protocol to a measurement result.
    "assay_01/raw data/bench notes.docx",
]


class TestDepositedFilesAreTheResult:
    def test_endpoint_readout_result_is_the_deposited_raw_files(self):
        state, (assay_id,) = _scaffold(_SEPARATED)

        draft_process_chain(state, assay_id, chain=_CHAIN)

        assert _sources(_result_entities(state, "EndpointReadout")) == {
            "assay_01/raw data/004668.csv",
            "assay_01/characterisation/assay1_rawdata/004043.csv",
        }

    def test_data_analysis_result_is_the_deposited_processed_file(self):
        state, (assay_id,) = _scaffold(_SEPARATED)

        draft_process_chain(state, assay_id, chain=_CHAIN)

        assert _sources(_result_entities(state, "DataAnalysis")) == {
            "assay_01/characterisation/assay1_processeddata/combined 0-60 min.csv"
        }

    def test_no_empty_placeholder_is_synthesized_when_the_deposit_has_the_file(self):
        """The stub is what made the crate 39% column definitions over zero rows."""
        state, (assay_id,) = _scaffold(_SEPARATED)

        result = draft_process_chain(state, assay_id, chain=_CHAIN)

        assert result["synthesized"] == []
        provisional = [
            e.entity_id
            for e in state.list_entities("File")
            if e.fields.get("provisional")
        ]
        assert provisional == []

    def test_rewiring_the_same_chain_changes_nothing(self):
        """The composite documents itself idempotent; wiring real files must keep that."""
        state, (assay_id,) = _scaffold(_SEPARATED)

        seen = []
        for _ in range(3):
            draft_process_chain(state, assay_id, chain=_CHAIN)
            seen.append(
                (
                    len(state.list_entities("File")),
                    tuple(sorted(_sources(_result_entities(state, "EndpointReadout")))),
                    tuple(sorted(_sources(_result_entities(state, "DataAnalysis")))),
                )
            )
        assert seen[0] == seen[1] == seen[2], seen

    def test_a_document_filed_with_the_measurements_is_not_a_measurement(self):
        """`bench notes.docx` sits in `raw data/` — the tier matches, the format does not."""
        state, (assay_id,) = _scaffold(_SEPARATED)

        draft_process_chain(state, assay_id, chain=_CHAIN)

        wired = _sources(
            _result_entities(state, "EndpointReadout")
            + _result_entities(state, "DataAnalysis")
        )
        assert "assay_01/raw data/bench notes.docx" not in wired


# --- one directory naming BOTH tiers: the svhps21 / svhps26 shape ----------

_COMBINED = [
    "Assay_MCT8/Raw data + individual processed data/220517_P1.xls",
    "Assay_MCT8/Raw data + individual processed data/220517_P1.prism",
]


class TestTheTierRuleItself:
    """Directory names, against the conventions the three real deposits use.

    Anchored at a token boundary rather than matched as a substring: bare `raw`
    also matches "drawings", and bare `process` matches "unprocessed" — which
    means the *opposite* tier and would have been filed as processed.
    """

    @pytest.mark.parametrize(
        ("path", "tier"),
        [
            ("a/raw data/x.csv", "raw"),
            ("a/assay1_rawdata/x.csv", "raw"),
            ("a/Raw/x.csv", "raw"),
            ("a/RAW DATA/x.csv", "raw"),
            ("a/processed data/x.csv", "processed"),
            ("a/assay1_processeddata/x.csv", "processed"),
            ("a/assay4_EDCs_processed data/x.csv", "processed"),
            ("a/data processing/x.csv", "processed"),
            # svhps21 / svhps26: one directory, both tiers named
            ("a/Raw data + individual processed data/x.csv", None),
            ("a/raw data+individual processed data/x.csv", None),
            # neither tier named
            ("a/EDCs/x.csv", None),
            ("a/characterisation/x.csv", None),
            ("x.csv", None),
            # substring traps
            ("a/drawings/x.csv", None),
            ("a/unprocessed/x.csv", None),
            ("a/preprocessed/x.csv", None),
        ],
    )
    def test_the_directory_decides_the_tier(self, path, tier):
        from builder.tools.composites import _path_tier

        assert _path_tier(path) == tier


class TestAmbiguousTierIsNotGuessed:
    def test_a_directory_naming_both_tiers_wires_no_deposited_file(self):
        """Reading only 'raw' would hand svhps21/26's processed files to the readout."""
        state, (assay_id,) = _scaffold(_COMBINED)

        draft_process_chain(state, assay_id, chain=_CHAIN)

        deposited = {p for p in _COMBINED}
        for process_type in ("EndpointReadout", "DataAnalysis"):
            wired = _sources(_result_entities(state, process_type))
            assert not (wired & deposited), f"{process_type} guessed: {wired}"

    def test_the_step_still_gets_an_output_so_the_chain_does_not_dangle(self):
        """EndpointReadout MUST have a schema:result (tox sh:Violation)."""
        state, (assay_id,) = _scaffold(_COMBINED)

        draft_process_chain(state, assay_id, chain=_CHAIN)

        for process_type in ("EndpointReadout", "DataAnalysis"):
            assert _result_entities(state, process_type), process_type


class TestUnchangedWhenTheDepositHasNothing:
    def test_a_deposit_with_no_data_files_still_synthesizes_a_placeholder(self):
        state, (assay_id,) = _scaffold(["README.txt", "protocol.docx"])

        result = draft_process_chain(state, assay_id, chain=_CHAIN)

        assert len(result["synthesized"]) == 2


class TestTwoFilesSharingABasename:
    """File ids are minted from the name, so `raw/README.txt` and
    `processed/README.txt` minted the same one and the second replaced the first.

    Pre-existing in ``draft_file``, but wiring deposited files is what made it
    bite: one surviving entity was claimed as the output of two different steps.
    Both verbs now reach files through ``find_or_create_file``, so ``attach_files``
    is covered by the same fix.
    """

    def test_both_files_survive_with_distinct_entities(self):
        from builder.tools.provenance import file_index_by_source, find_or_create_file

        state = CrateState()
        state.metadata.input_path = _DEPOSIT
        index = file_index_by_source(state)

        first = find_or_create_file(state, _scanned("raw data/README.txt"), index=index)
        second = find_or_create_file(
            state, _scanned("processed data/README.txt"), index=index
        )

        assert first.entity_id != second.entity_id
        assert {f.fields.get("dest_path") for f in state.list_entities("File")} == {
            "raw data/README.txt",
            "processed data/README.txt",
        }

    def test_the_same_file_reached_twice_is_still_one_entity(self):
        """Disambiguation must not defeat the dedupe it sits next to."""
        from builder.tools.provenance import file_index_by_source, find_or_create_file

        state = CrateState()
        state.metadata.input_path = _DEPOSIT
        index = file_index_by_source(state)
        record = _scanned("raw data/004043.csv")

        first = find_or_create_file(state, record, index=index)
        second = find_or_create_file(state, record, index=index)

        assert first is second
        assert len(state.list_entities("File")) == 1

    def test_the_id_is_stable_across_runs(self):
        """A rebuilt crate must not re-key its files."""
        from builder.tools.provenance import find_or_create_file

        ids = []
        for _ in range(2):
            state = CrateState()
            state.metadata.input_path = _DEPOSIT
            find_or_create_file(state, _scanned("raw data/README.txt"))
            ids.append(
                find_or_create_file(state, _scanned("processed data/README.txt")).entity_id
            )
        assert ids[0] == ids[1]


class TestTheRealDeposit:
    """The synthetic paths above pin the rule; this pins the outcome on svhps22.

    `output/svhps22_real_input_crate_v6` wired 0 of 56 payload files into any
    process. Every chain ended at a header-only stub while a 1048-row measurement
    table sat in the crate as a File nothing referenced.
    """

    @staticmethod
    def _real_state():
        from builder.tools.scanner import scan_files

        root = str(Path("tests/fixtures/svhps22_real_input").resolve())
        state = CrateState()
        state.metadata.title = "svhps22"
        state.metadata.input_path = root
        ids = scaffold_isa_backbone(
            state,
            investigation={"name": "Inv", "description": "d", "identifier": "INV-1"},
            study={"name": "Study", "description": "d"},
            assay={"name": "Assay", "description": "d"},
        )
        state.scanned_files = list(scan_files(root, approved_roots={root}))
        return state, ids["assay_id"]

    def test_the_readout_reaches_the_deposits_raw_measurements(self):
        state, assay_id = self._real_state()

        draft_process_chain(state, assay_id, chain=_CHAIN)

        wired = _sources(_result_entities(state, "EndpointReadout"))
        assert wired, "the readout still ends at a stub"
        assert all("raw" in w.lower() for w in wired), wired

    def test_the_analysis_reaches_the_deposits_processed_data(self):
        state, assay_id = self._real_state()

        draft_process_chain(state, assay_id, chain=_CHAIN)

        wired = _sources(_result_entities(state, "DataAnalysis"))
        assert wired, "the analysis still ends at a stub"
        assert all("processed" in w.lower() for w in wired), wired

    def test_nothing_is_manufactured_for_this_deposit(self):
        state, assay_id = self._real_state()

        result = draft_process_chain(state, assay_id, chain=_CHAIN)

        assert result["synthesized"] == []


class TestScopingToTheAssay:
    def test_a_second_assay_does_not_borrow_the_first_assays_files(self):
        """With several Assays and no attachment, which folder is whose is not derivable."""
        state, assay_ids = _scaffold(_SEPARATED, assays=2)

        draft_process_chain(state, assay_ids[1], chain=_CHAIN)

        wired = _sources(_result_entities(state, "EndpointReadout"))
        assert not (wired & set(_SEPARATED)), wired

    def test_attached_files_scope_the_search_when_there_are_several_assays(self):
        from builder.tools.provenance import attach_files

        state, assay_ids = _scaffold(_SEPARATED, assays=2)
        attach_files(state, to=assay_ids[1], name_contains="004668")

        draft_process_chain(state, assay_ids[1], chain=_CHAIN)

        assert _sources(_result_entities(state, "EndpointReadout")) == {
            "assay_01/raw data/004668.csv"
        }
