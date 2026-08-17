"""A step whose output was never deposited says so, instead of shipping a fake one (#592).

Two faults produced the same symptom — an empty CSV in the crate's ``data/``
that reads as data and is not.

**Ordering.** ``_deposited_outputs`` scopes to files already attached to the
assay, and on a real run (session ``20260817_190026``) the agent drafted all four
chains at 6.7 min and attached the files at 7.0 min. Every ``hasPart`` was empty
at the moment the question was asked, so the lookup answered "nothing deposited"
when the truth was "I looked too early" — and 5 of 8 steps fell back to a stub
whose real output was sitting in the deposit. Attachment is when the evidence
arrives, so attachment is where the answer is revisited.

**The fallback itself.** When the deposit holds no output for a step, the build
used to write a 0-byte CSV so ``schema:result`` was satisfied and the crate did
not reference a file it had not shipped (#438). That trades a visible gap for an
invisible one: the tox shape passes, the maturity report counts a data file, and
nothing tells the depositor what is missing.

Nothing is manufactured now. Two questions, deliberately separate:

* **Is the step real?** Data of its tier, or a document describing the
  procedure. Either is evidence; a deposit holding neither leaves nothing to
  model, and drafting a step there would invent it exactly as an empty output
  file used to invent its result — so it is skipped, and ``skipped`` says why.

  A protocol counts on its own, because the step carries more than its output:
  on svhps26 the SOP yields ``Detection Instrument = "gamma counter"``, which
  reaches the crate only because an EndpointReadout exists to hold it. Gating on
  data alone deleted that.

* **Is its output present?** Separate, and often no. The step keeps no
  ``result`` it cannot show, and the tox Violation reports the gap — including
  when the deposit does hold the file but nothing has placed it yet (svhps21 and
  svhps26 file both tiers in one directory; see #591).

The first question is asked of the WHOLE deposit and the second per assay,
because only the second depends on attachment order.
"""

from __future__ import annotations

from pathlib import Path

from builder.state import CrateState, FileClassification
from builder.tools.composites import draft_process_chain, scaffold_isa_backbone
from builder.tools.drafters import draft_assay
from builder.tools.provenance import attach_files

_DEPOSIT = "/deposit"

_CHAIN = [
    {
        "process_type": "EndpointReadout",
        "hints": {"name": "Read", "detection_instrument": "Gamma counter"},
    },
    {"process_type": "DataAnalysis", "hints": {"name": "Analyse", "data_processing": "AUC"}},
]


def _scanned(rel: str) -> FileClassification:
    return FileClassification(
        path=f"{_DEPOSIT}/{rel}",
        filename=Path(rel).name,
        size=4096,
        mime_type="text/csv",
        first_rows=None,
    )


def _scaffold(paths: list[str], *, assays: int = 1) -> tuple[CrateState, list[str]]:
    state = CrateState()
    state.metadata.title = "Missing-output crate"
    state.metadata.input_path = _DEPOSIT
    ids = scaffold_isa_backbone(
        state,
        investigation={"name": "Inv", "description": "d", "identifier": "INV-1"},
        study={"name": "Study", "description": "d"},
        assay={"name": "Assay 1", "description": "d"},
    )
    assay_ids = [ids["assay_id"]]
    for n in range(2, assays + 1):
        assay_ids.append(
            draft_assay(state, ids["study_id"], {"name": f"Assay {n}", "description": "d"}).entity_id
        )
    state.scanned_files = [_scanned(p) for p in paths]
    return state, assay_ids


def _process(state: CrateState, process_type: str):
    return next(
        p
        for p in state.list_entities("LabProcess")
        if p.fields.get("process_type") == process_type
    )


def _result_paths(state: CrateState, process_type: str) -> set[str]:
    value = _process(state, process_type).fields.get("result")
    ids = value if isinstance(value, list) else [value]
    out = set()
    for i in ids:
        key = i.get("@id") if isinstance(i, dict) else i
        if not key:
            continue
        entity = state.get_entity(str(key).lstrip("#"))
        if entity is not None:
            out.add(str(entity.fields.get("dest_path") or entity.entity_id))
    return out


_A1 = [
    "assay_01/raw data/004668.csv",
    "assay_01/processed data/combined.csv",
]


class TestAttachmentCompletesTheWiring:
    """The real-run ordering: chain first, files attached after."""

    def test_a_chain_drafted_before_attachment_is_still_wired(self):
        state, assay_ids = _scaffold(_A1, assays=2)
        target = assay_ids[1]
        draft_process_chain(state, target, chain=_CHAIN)
        assert _result_paths(state, "EndpointReadout") == set(), "precondition: nothing wired yet"

        attach_files(state, to=target, name_contains="assay_01")

        assert _result_paths(state, "EndpointReadout") == {"assay_01/raw data/004668.csv"}
        assert _result_paths(state, "DataAnalysis") == {"assay_01/processed data/combined.csv"}

    def test_attaching_to_one_assay_does_not_wire_anothers_chain(self):
        state, assay_ids = _scaffold(_A1, assays=2)
        draft_process_chain(state, assay_ids[0], chain=_CHAIN)

        attach_files(state, to=assay_ids[1], name_contains="assay_01")

        assert _result_paths(state, "EndpointReadout") == set()

    def test_attachment_does_not_disturb_an_already_wired_step(self):
        state, (assay_id,) = _scaffold(_A1)
        draft_process_chain(state, assay_id, chain=_CHAIN)
        before = _result_paths(state, "EndpointReadout")

        attach_files(state, to=assay_id, name_contains="assay_01")

        assert _result_paths(state, "EndpointReadout") == before

    def test_an_explicit_result_is_never_overwritten_by_attachment(self):
        """A curator's own wiring outranks anything derived from the deposit."""
        from builder.tools.provenance import draft_file

        state, assay_ids = _scaffold(_A1, assays=2)
        target = assay_ids[1]
        chosen = draft_file(state, name="mine.csv", path="assay_01/mine.csv")
        draft_process_chain(
            state, target, chain=[{**_CHAIN[0], "result": chosen.entity_id}, _CHAIN[1]]
        )

        attach_files(state, to=target, name_contains="assay_01")

        assert _result_paths(state, "EndpointReadout") == {"assay_01/mine.csv"}


class TestNothingIsManufactured:
    """A deposit with no data file for a step gets no stand-in, at any layer."""

    def test_no_file_entity_is_created_for_an_undeposited_output(self):
        state, (assay_id,) = _scaffold(["protocol.docx"])

        draft_process_chain(state, assay_id, chain=_CHAIN)

        assert state.list_entities("File") == []

    def test_a_deposit_with_nothing_at_all_drafts_no_data_producer(self):
        """With neither data nor a procedure document, the step itself would be invented."""
        state, (assay_id,) = _scaffold([])

        draft_process_chain(state, assay_id, chain=_CHAIN)

        assert state.list_entities("LabProcess") == []

    def test_a_protocol_alone_is_enough_to_record_the_step(self):
        """The readout is real, and carries the instrument read out of that SOP —
        on svhps26 `Detection Instrument = "gamma counter"` reaches the crate only
        because the step exists to hold it. Its OUTPUT is what is missing."""
        state, (assay_id,) = _scaffold(["Assay/OATP1C1 SOP TH 250425.docx"])

        draft_process_chain(state, assay_id, chain=_CHAIN)

        readout = _process(state, "EndpointReadout")
        assert readout.fields.get("detection_instrument") == "Gamma counter"
        assert not readout.fields.get("result")

    def test_the_skipped_steps_are_reported_not_dropped_in_silence(self):
        """A chain that quietly loses half its steps looks like one only asked for half."""
        state, (assay_id,) = _scaffold([])

        result = draft_process_chain(state, assay_id, chain=_CHAIN)

        assert {s["process_type"] for s in result["skipped"]} == {
            "EndpointReadout",
            "DataAnalysis",
        }
        assert all("evidences this" in s["reason"] for s in result["skipped"]), result["skipped"]

    def test_an_explicit_result_records_the_step_anyway(self):
        """The evidence rule is a default, not a veto over the curator."""
        from builder.tools.provenance import draft_file

        state, (assay_id,) = _scaffold([])
        chosen = draft_file(state, name="mine.csv", path="mine.csv")

        draft_process_chain(
            state, assay_id, chain=[{**_CHAIN[0], "result": chosen.entity_id}]
        )

        assert _process(state, "EndpointReadout").fields.get("result") == chosen.entity_id

    def test_the_composite_reports_that_it_synthesized_nothing(self):
        state, (assay_id,) = _scaffold(["protocol.docx"])

        result = draft_process_chain(state, assay_id, chain=_CHAIN)

        assert result["synthesized"] == []

    def test_a_cell_culture_still_gets_its_output_sample(self):
        """Only FILE stand-ins go; a material producer's Sample is a real modelling act."""
        state, (assay_id,) = _scaffold(["protocol.docx"])

        result = draft_process_chain(
            state, assay_id, chain=[{"process_type": "CellCulture", "hints": {"name": "Seed"}}]
        )

        assert len(result["synthesized"]) == 1
        assert state.list_entities("Sample")

    def test_nothing_is_written_to_disk_for_a_missing_output(self, tmp_path):
        from builder.tools.builder import assemble_crate

        state, (assay_id,) = _scaffold(["protocol.docx"])
        draft_process_chain(state, assay_id, chain=_CHAIN)

        assemble_crate(state, output_dir=tmp_path, materialize_payload=True)

        assert not (tmp_path / "data").exists() or list((tmp_path / "data").iterdir()) == []


class TestTheGapIsReported:
    """The deposit HAS data of the tier, so the step is real — but none of it
    reached this assay. That gap is the one worth reporting, and it is what a
    0-byte CSV used to hide."""

    def test_a_step_whose_files_never_arrive_fires_its_required_issue(self):
        from builder.tools.validation import build_and_validate

        state, assay_ids = _scaffold(_A1, assays=2)
        draft_process_chain(state, assay_ids[1], chain=_CHAIN)

        report = build_and_validate(state, severity="required")

        assert report["ok"] is False
        assert any(
            "result" in str(issue.get("message", "")).lower() for issue in report["issues"]
        ), report["issues"]

    def test_no_file_is_invented_to_silence_it(self):
        state, assay_ids = _scaffold(_A1, assays=2)

        draft_process_chain(state, assay_ids[1], chain=_CHAIN)

        assert state.list_entities("File") == []

    def test_a_deposit_that_has_the_files_still_passes(self):
        from builder.tools.validation import build_and_validate

        state, (assay_id,) = _scaffold(_A1)
        draft_process_chain(state, assay_id, chain=_CHAIN)

        report = build_and_validate(state, severity="required")

        assert not any(
            "result" in str(issue.get("message", "")).lower() for issue in report["issues"]
        ), report["issues"]
