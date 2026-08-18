"""Real-input producer regression: drive ``run_pipeline`` over a genuine study crate.

Where the golden-crate fixtures (``tests/fixtures/vhps_golden_crates.py``) and the
scripted e2e harness (``tests/test_e2e_agent_eval.py``) hand-build a finished
``CrateState`` — or hard-code every value from the golden spec and merely *assert it
back* — this test SCANS a real VHP4Safety study folder off disk and drives the FULL
deterministic pipeline over it:

    scaffold backbone → _materialize_plan → _draft_entities → build_and_validate → fix

The fixture (``tests/fixtures/svhps26_real_input/``) is the genuine S-VHPS26 OATP1C1
deposit — every file is committed verbatim from the EBI BioStudies archive, in its
real nested layout:

    S-VHPS26.json                                  the BioStudies submission descriptor
    Assay_OATP1C1/
      Assay-metadata-CHO-K1_OATP1C1-v1.1.xlsx      the assay-metadata spreadsheet
      OATP1C1 SOP TH 250425.docx                   the real Standard Operating Procedure
      raw data+individual processed data/220825_RA_CHO-K1_hOATP1C1/
        …_P1_Timecourse.xlsx                       real RAW measurement data
        …_P1_Timecourse.pzfx                       real PROCESSED (GraphPad) data

So the scan exercises real directory recursion (paths with spaces / ``+``), the
metadata/SOP body reads, the assay spreadsheet preview, and the raw-vs-
processed file-role split — none of which a flat, hand-built fixture touches.

Only the two genuinely non-deterministic seams are stubbed, both offline:

* the bounded LLM leaves (``extract_plan`` / ``draft_entity_fields``). Crucially the
  ``extract_plan`` stub is **context-driven**: it proposes the study/compound/cell
  line only when the substrate token is present in the gathered context, and the
  protocol only when a method token from the SOP body is present. So if scanning or
  ``_gather_context`` regresses and the real document content stops reaching the
  leaf, the plan empties and the materialization assertions redden. The stub is never
  fed a hand-built plan.
* the composites' network lookups (``lookup_compound`` / ``verify_identifier`` /
  ``search_works_by_title``), replaced by deterministic canned data.

Everything between — scanning, ``_gather_context`` body reads, plan materialization,
``resolve_compound``'s minting + D5 identifier handling, JSON-LD mapping, and SHACL
validation — runs for real. ``test_no_compound_without_the_real_document`` is the
explicit control proving the compound is caused by the real input, not by the stub.

Fully offline: no network, no live LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.hitl import SimulatedHumanInterface

# run_pipeline drives build_and_validate / the fix loop, which run the (uncached,
# owlrl-heavy) SHACL validator several times — heavier than the CI --timeout=30
# default. Mirror tests/test_pipeline_e2e.py and give this module headroom.
pytestmark = pytest.mark.timeout(120)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "svhps26_real_input"
_METADATA_XLSX_NAME = "Assay-metadata-CHO-K1_OATP1C1-v1.1.xlsx"
_DESCRIPTOR = FIXTURE_DIR / "S-VHPS26.json"
_SOP = FIXTURE_DIR / "Assay_OATP1C1" / "OATP1C1 SOP TH 250425.docx"
_RAW_DATA = (
    FIXTURE_DIR
    / "Assay_OATP1C1"
    / "raw data+individual processed data"
    / "220825_RA_CHO-K1_hOATP1C1"
    / "220825_RA_CHO-K1_hOATP1C1_P1_Timecourse.xlsx"
)
_PROCESSED_DATA = _RAW_DATA.with_suffix(".pzfx")

# Honesty anchor: the assay substrate, carried only in real document CONTENT (the
# descriptor/SOP/spreadsheet), NEVER in a filename — so a total content regression
# (scanner delivering filenames only) empties the plan. "oatp1c1"/"cho-k1"/"th"/"sop"
# all occur in filenames and are deliberately NOT used as gates.
_TOKEN_SUBSTRATE = "thyroxine"
# A method token carried in document CONTENT and no filename. This used to be
# "sandell" (the Sandell-Kolthoff readout), which lived only in
# `Assay_OATP1C1/README.txt` — a README copy-pasted from the unrelated MCT8-MDCK1
# assay and removed in 6abf72c. This deposit reads T4 uptake out radiometrically,
# so the gate is now the readout wording from the SOP .docx body itself (offset
# 1871, inside the 2,000-char slice that tier gets, same sentence as
# `_TOKEN_SOP_INSTRUMENT`). It occurs in no filename and in no other fixture file.
_TOKEN_SOP_BODY = "cell lysate"
# A token that really IS reachable only through the .docx body read (python-docx):
# the gene symbol for the transporter, written out in the SOP's Definition section
# and in no other file, filename or preview (the rest of the deposit says OATP1C1,
# the protein). Without this the pipeline had NO test proving the Word-document
# read works, because the token documented as covering it does not occur there.
_TOKEN_DOCX_BODY = "slco1c1"
# Signal the priority-0 metadata workbook holds past its first three preview rows —
# each was absent from the leaf's context before the #378 weighted budget.
_TOKEN_CELL_LINE_RRID = "cvcl_0214"
_TOKEN_CHEMICAL_2 = "lesinurad"
_TOKEN_CHEMICAL_5 = "diclofenac"
_TOKEN_PERSON = "wagenaars"
# Experimental parameters stated in the SOP .docx body prose — "After exposure to
# 10 nM of 125I-T4 for 30 minutes …  quantified by measuring the radioactivity of
# the cell lysate in a gamma counter." Neither occurs in any filename (#379).
_TOKEN_SOP_DURATION = "30 minutes"
_TOKEN_SOP_INSTRUMENT = "gamma counter"

# The first five test chemicals in sheet order. Chemicals 2-5 sit past 2,600
# compacted chars, so the leaf saw none of them before #378 — the crate carried the
# substrate alone. These five drive the plan-stub gating below; the sheet holds many
# more (see `_NAMED_CHEMICALS`), and gating the stub on all of them would be unsound
# because several names are substrings of each other ("bisphenol-A" occurs inside
# "tetrabromobisphenol-A").
_TEST_CHEMICALS: tuple[tuple[str, str], ...] = (
    ("silychristin", "Silychristin"),
    (_TOKEN_CHEMICAL_2, "Lesinurad"),
    ("indocyanine", "Indocyanine green"),
    ("verapamil", "Verapamil"),
    (_TOKEN_CHEMICAL_5, "Diclofenac"),
)

# EVERY chemical the workbook actually names, by sheet row number (#419). The sheet
# has 20 `Chemical_N` slots; row 6 has no `_Name` cell at all — "Probenecid" was
# typed into `Chemical_6_CAS` instead — so 19 are named and slot 6 is unreachable at
# any budget. Keyed by row number and asserted against the row label rather than the
# bare name, because the names collide as substrings.
_NAMED_CHEMICALS: tuple[tuple[int, str], ...] = (
    (1, "Silychristin"),
    (2, "Lesinurad"),
    (3, "Indocyanine green"),
    (4, "Verapamil"),
    (5, "diclofenac"),
    (7, "bromosulfophthalein"),
    (8, "bisphenol-S"),
    (9, "bisphenol-Z"),
    (10, "bisphenol-AF"),
    (11, "bisphenol-F"),
    (12, "Sulforhodamine 101"),
    (13, "pentachlorophenol"),
    (14, "tetrabromobisphenol-A"),
    (15, "bisphenol-A"),
    (16, "perfluorooctanesulfonic acid (PFOA)"),
    (17, "Perfluorooctanoic acid (PFOA)"),
    (18, "Quercetin"),
    (19, "Rifampicin"),
    (20, "Triclosan"),
)


def _scanning_engine(input_dir: Path) -> AgentEngine:
    """A headless engine that has SCANNED ``input_dir`` off disk via the real guard."""
    engine = AgentEngine(state=CrateState(), human_interface=SimulatedHumanInterface())
    engine.initialize(input_path=str(input_dir))
    return engine


def _install_offline_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ONLY the two non-deterministic seams (LLM leaves + network lookups).

    Returns a capture dict; ``capture["context"]`` records the exact free-text context
    the extraction leaf received, so a test can assert the real document content
    reached it.
    """
    import builder.agents.pipeline.pipeline as pipeline_mod
    import builder.tools.composites as composites_mod
    from builder.tools._resolve_cache import compound_cache

    # The compound-resolution cache is process-global; a prior test can pre-cache these
    # names and short-circuit the lookup stub. Clear it (xdist-safe).
    compound_cache.clear()

    capture: dict[str, Any] = {"context": ""}

    # Provider gate: configured (no real model) so the leaves run rather than no-op.
    monkeypatch.setattr(pipeline_mod, "get_provider", lambda: "openai")

    def fake_extract_plan(
        context: str, *, overrides: Any = None, usage_sink: Any = None
    ) -> dict[str, Any]:
        """Context-driven plan: propose entities ONLY when their real token is present.

        This is what keeps the test honest — the stub reads the REAL scanned context
        and cannot invent what the documents don't mention. A scan/body-read regression
        empties the context, empties the plan, and reddens the test.
        """
        capture["context"] = context
        low = context.lower()
        plan: dict[str, Any] = {}
        if _TOKEN_SUBSTRATE in low:
            plan["study"] = {
                "name": "Inhibition of OATP1C1-mediated cellular uptake of thyroxine",
                "description": (
                    "A cell-based in vitro assay screening chemicals for their "
                    "capacity to inhibit thyroxine (T4) uptake by OATP1C1."
                ),
            }
            plan["compounds"] = [{"name": "Thyroxine", "role": "substrate"}]
            plan["cell_lines"] = [{"name": "CHO-K1 OATP1C1-overexpressing cells"}]
            # Each test chemical is proposed ONLY when its own name reached the
            # leaf. All five sit in the metadata workbook past the three preview
            # rows it used to emit, so before #378 this list stayed at one entry
            # and the crate shipped one MolecularEntity instead of six.
            plan["compounds"] += [
                {"name": label, "role": "test chemical"}
                for token, label in _TEST_CHEMICALS
                if token in low
            ]
        # Process parameters, each gated on its own token from the SOP .docx BODY —
        # "30 minutes" at char 1730, "gamma counter" at 1887, both inside the 2,000
        # char slice that tier gets. Neither string occurs in any filename in the
        # fixture, so they can only come from the real Word-document read (#379).
        chain_params: dict[str, dict[str, str]] = {}
        if _TOKEN_SOP_DURATION in low:
            chain_params["Exposure"] = {"duration": "30 minutes"}
        if _TOKEN_SOP_INSTRUMENT in low:
            chain_params["EndpointReadout"] = {"detection_instrument": "gamma counter"}
        if chain_params:
            plan["process_chain"] = [
                {"process_type": ptype, "parameters": params}
                for ptype, params in chain_params.items()
            ]

        if _TOKEN_SOP_BODY in low:
            # The SOP body (read from the .docx) drives a LabProtocol governing the
            # exposure — proposed only because the procedure text reached the leaf.
            plan["protocols"] = [
                {
                    "name": "OATP1C1 thyroxine-uptake SOP",
                    "description": (
                        "Standard operating procedure: cellular thyroxine uptake read "
                        "out as radioactivity of the cell lysate in a gamma counter."
                    ),
                    "process_hint": "Exposure",
                }
            ]
        return plan

    monkeypatch.setattr(pipeline_mod, "extract_plan", fake_extract_plan)

    def fake_draft_entity_fields(
        entity_type: str,
        context: str,
        *,
        model: str | None = None,
        usage_sink: Any = None,
    ) -> dict[str, Any]:
        return {"description": f"Drafted {entity_type} description."}

    monkeypatch.setattr(pipeline_mod, "draft_entity_fields", fake_draft_entity_fields)

    # resolve_compound → lookup_compound: canned but REAL identifiers for thyroxine
    # (T4: PubChem CID 5819, CAS 51-48-9). resolve_compound's own minting / D5 handling
    # runs for real over this.
    # Name-keyed real identifiers. Before #378 only one compound ever reached the
    # plan, so a single canned answer was harmless; now all five do, and a
    # name-blind stub would stamp every one of them with thyroxine's CAS. An
    # unknown name returns found=False rather than falling through to the network —
    # a live PubChem/ChEBI call from this module is a release blocker.
    _CANNED_COMPOUNDS = {
        "thyroxine": ("51-48-9", "5819"),
        "silychristin": ("33889-69-9", "441764"),
        "lesinurad": ("878672-00-5", "44543017"),
        "indocyanine": ("3599-32-4", "5282412"),
        "verapamil": ("52-53-9", "2520"),
        "diclofenac": ("15307-86-5", "3033"),
    }

    def fake_lookup_compound(name: str) -> dict[str, Any]:
        key = next((k for k in _CANNED_COMPOUNDS if k in (name or "").lower()), None)
        if key is None:
            return {"found": False, "data": None, "error": "no offline candidate"}
        cas, cid = _CANNED_COMPOUNDS[key]
        return {
            "found": True,
            "data": {"cas": cas, "pubchem_cid": cid, "source": "pubchem"},
            "error": None,
        }

    def fake_verify_identifier(state: CrateState, entity_id: str, field: str) -> dict[str, Any]:
        ent = state.get_entity(entity_id)
        if ent is not None:
            ent.set_field_status(field, "verified", "lookup")
        return {"verified": True, "entity_id": entity_id, "field": field, "message": "ok"}

    # No offline publication candidates → the descriptor's publication stays deferred
    # (D5), keeping the build deterministic.
    def fake_search_works_by_title(title: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(composites_mod, "lookup_compound", fake_lookup_compound)
    monkeypatch.setattr(composites_mod, "verify_identifier", fake_verify_identifier)
    monkeypatch.setattr(composites_mod, "search_works_by_title", fake_search_works_by_title)
    return capture


# ---------------------------------------------------------------------------
# The fixture IS the real crate (guards it silently becoming synthetic / deleted).
# ---------------------------------------------------------------------------


class TestRealInputFixture:
    def test_fixture_is_the_real_svhps26_crate(self) -> None:
        assert FIXTURE_DIR.is_dir(), f"missing real-input fixture: {FIXTURE_DIR}"
        # The genuine EBI BioStudies submission (real accession), not a synthetic stub.
        descriptor = json.loads(_DESCRIPTOR.read_text())
        assert descriptor["accno"] == "S-VHPS26"
        # A real crate carries more than metadata: a real SOP + real raw + processed
        # measurement files, in the archive's nested layout.
        assert _SOP.is_file(), "the real SOP document must be committed"
        assert _RAW_DATA.is_file(), "a real RAW data file must be committed"
        assert _PROCESSED_DATA.is_file(), "a real PROCESSED data file must be committed"

    def test_scan_recurses_and_classifies_raw_vs_processed(self) -> None:
        """The scanner recurses the real nested layout (paths with spaces / ``+``),
        inventories the metadata + SOP + raw + processed files, splits raw vs
        processed, and never escapes the root (no repo pollution)."""
        from builder.tools.document_discovery import (
            CLASS_PROCESSED_DATA,
            CLASS_RAW_DATA,
            classification_of,
        )

        engine = _scanning_engine(FIXTURE_DIR)
        scanned = {Path(f.path).name: f for f in engine.state.scanned_files}

        assert "S-VHPS26.json" in scanned
        assert _SOP.name in scanned
        assert _RAW_DATA.name in scanned  # recursion into the spaced/"+" subdir worked
        assert _PROCESSED_DATA.name in scanned

        classes = {
            classification_of(f, input_root=str(FIXTURE_DIR)) for f in scanned.values()
        }
        assert {CLASS_RAW_DATA, CLASS_PROCESSED_DATA} <= classes
        assert not any(n.endswith((".py", ".pyc")) for n in scanned)


# ---------------------------------------------------------------------------
# The producer path, driven end to end from the real scanned crate.
# ---------------------------------------------------------------------------


class TestRealInputPipeline:
    def test_real_document_content_reaches_the_extraction_leaf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Context fidelity: the leaf's context carries the substrate token AND a
        method token from the SOP .docx BODY — proving scan + ``_gather_context``
        delivered document CONTENT (spreadsheet preview + Word-document body read),
        not just filenames."""
        from builder.agents.pipeline.pipeline import run_pipeline

        capture = _install_offline_seams(monkeypatch)
        run_pipeline(_scanning_engine(FIXTURE_DIR))

        context = capture["context"].lower()
        assert _TOKEN_SUBSTRATE in context, capture["context"][:600]
        # Document CONTENT, never a filename (SOP .docx body).
        assert _TOKEN_SOP_BODY in context, capture["context"][:600]
        # The Word-document body read specifically (python-docx).
        assert _TOKEN_DOCX_BODY in context, capture["context"][:600]

        # #378 — the priority-0 workbook's signal, all of it past the three preview
        # rows the leaf used to receive. Red before the weighted budget, and red
        # under the naive "count previewed files in n_to_read" fix too.
        assert _TOKEN_CELL_LINE_RRID in context, "cell-line RRID starved"
        assert _TOKEN_CHEMICAL_2 in context, "test chemical 2 starved"
        assert _TOKEN_CHEMICAL_5 in context, "test chemical 5 starved"
        assert _TOKEN_PERSON in context, "corresponding person starved"

    def test_every_test_chemical_becomes_a_molecular_entity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The payoff: all five test chemicals + the substrate reach the crate (#378).

        `_materialize_plan` mints compounds only from `plan["compounds"]`, and the
        plan can only name what reached the leaf. Before the weighted budget the
        crate carried ONE `MolecularEntity`; the workbook naming the other five was
        emitting 298 characters.

        This also exercises the name-keyed offline compound seam — with the old
        name-blind stub all six would carry thyroxine's CAS.
        """
        from builder.agents.pipeline.pipeline import run_pipeline

        _install_offline_seams(monkeypatch)
        engine = _scanning_engine(FIXTURE_DIR)
        run_pipeline(engine)

        compounds = engine.state.list_entities("MolecularEntity")
        names = {(e.fields.get("name") or "").lower() for e in compounds}
        for _token, label in _TEST_CHEMICALS:
            assert any(label.lower() in n for n in names), f"{label} never minted: {names}"

        cas_values = {e.fields.get("cas") for e in compounds if e.fields.get("cas")}
        assert len(cas_values) > 1, f"every compound got the same CAS: {cas_values}"

    def test_every_named_chemical_reaches_the_extraction_leaf(self) -> None:
        """The whole compound table must survive the context budget (#419).

        The leaf can only propose what it was shown, so a chemical missing here is
        unreachable at any temperature, prompt or model — silently. Three separate
        truncations used to cut 19 named chemicals down to 5: `read_file`'s
        `max_lines=100` row cap, the uncompacted concentration series, and the
        tier-0 char share.

        Asserted on the `Chemical_N_Name` row rather than the bare name because the
        names collide as substrings — matching "bisphenol-A" alone would pass on
        "tetrabromobisphenol-A" and hide a genuine miss.
        """
        from builder.agents.pipeline.pipeline import _gather_context

        context = _gather_context(_scanning_engine(FIXTURE_DIR))

        missing = [
            f"Chemical_{row}_Name ({name})"
            for row, name in _NAMED_CHEMICALS
            if f"Chemical_{row}_Name".lower() not in context.lower()
            or name.lower() not in context.lower()
        ]
        assert not missing, f"{len(missing)} of {len(_NAMED_CHEMICALS)} starved: {missing}"

    def test_bulk_data_does_not_consume_the_metadata_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """STARVATION CONTROL — metadata-first must hold in CHARS (#378).

        Without this the fix could turn the tokens above green while regressing
        #179's guarantee, by simply handing every file a bigger slice. The
        priority-0 workbook must out-weigh the priority-3 GraphPad export.
        """
        from builder.agents.pipeline.pipeline import run_pipeline

        capture = _install_offline_seams(monkeypatch)
        run_pipeline(_scanning_engine(FIXTURE_DIR))

        def _emitted_for(stem: str) -> int:
            for block in capture["context"].split("\n- "):
                if block.lstrip("- ").lower().startswith(stem.lower()):
                    return len(block)
            return 0

        metadata = _emitted_for("Assay-metadata-CHO-K1_OATP1C1")
        bulk = _emitted_for("220825_RA_CHO-K1_hOATP1C1_P1_Timecourse.pzfx")
        assert metadata > bulk, f"metadata {metadata} chars vs bulk {bulk} chars"

    def test_a_second_metadata_file_does_not_starve_the_tiers_below(self, tmp_path: Path) -> None:
        """STARVATION CONTROL for the tier-0 raise (#419).

        A tier's share is granted PER FILE, so raising tier 0 to 9,000 let two
        `*metadata*` workbooks claim the whole 16,000 ceiling between them and the
        BioStudies descriptor and the SOP emitted nothing at all —
        the same silent starvation #419 exists to remove, aimed at a different
        document. A versioned or two-plate deposit is an ordinary shape and no
        other test builds one.
        """
        import shutil

        from builder.agents.pipeline.pipeline import _gather_context

        deposit = tmp_path / "two_metadata"
        shutil.copytree(FIXTURE_DIR, deposit)
        workbook = deposit / "Assay_OATP1C1" / _METADATA_XLSX_NAME
        shutil.copy(workbook, workbook.with_name("Assay-metadata-second-plate-v1.2.xlsx"))

        context = _gather_context(_scanning_engine(deposit))

        def _emitted_for(stem: str) -> int:
            for block in context.split("\n- "):
                if block.lstrip("- ").lower().startswith(stem.lower()):
                    return len(block)
            return 0

        assert _emitted_for("Assay-metadata-CHO-K1_OATP1C1") > 0, "first workbook starved"
        assert _emitted_for("Assay-metadata-second-plate") > 0, "second workbook starved"
        # The descriptor is the deposit's only structured identity record; losing
        # it to a duplicated workbook is a strictly worse trade than truncating
        # either workbook.
        assert _emitted_for("S-VHPS26.json") > 0, "the BioStudies descriptor was starved"
        assert _TOKEN_SOP_BODY in context.lower(), "the tier-2 documents were starved"

    def test_pipeline_builds_conformant_crate_from_real_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deterministic spine, over the real scanned crate, reaches BASE and
        ISA REQUIRED conformance, and states every tox parameter it can source.

        Exposure and EndpointReadout carry real values read out of the SOP body.
        DataAnalysis does not, and cannot: the SOP describes its statistics
        (GraphPad/Prism, regression) only past offset ~17,000, far outside the
        2,000-char slice that tier receives — so the value never reaches the
        extraction leaf, and `_pv` will not invent one (D5).

        TWO issue classes are outstanding, and both are honest reports rather
        than regressions:

        * that DataAnalysis `additionalProperty`, as above;
        * `schema:result` on the data-producing steps. This deposit DOES ship its
          measurements, but it files both tiers in one
          `raw data+individual processed data/` directory, so nothing can yet say
          which step produced which file (#591). Rather than invent an empty CSV
          to satisfy the shape, the steps keep no result and the Violation says
          so (#592). When #591 lands and the files classify, these clear and the
          only remaining issue is the DataAnalysis one.

        Any OTHER issue here is a real regression.
        """
        from builder.agents.pipeline.pipeline import run_pipeline

        _install_offline_seams(monkeypatch)
        result = run_pipeline(_scanning_engine(FIXTURE_DIR))

        assert result["conformance"]["base"] is True, result["issues"]
        assert result["conformance"]["isa"] is True, result["issues"]
        outstanding = result.get("issues") or []
        assert all(
            (
                str(i.get("property", "")).endswith("additionalProperty")
                and "DataAnalysis" in str(i.get("message", ""))
            )
            or str(i.get("property", "")).endswith("result")
            for i in outstanding
        ), outstanding
        # The result gaps are reported, not papered over with a manufactured file.
        assert not any(
            str(i.get("entity_id", "")).startswith("data/") for i in outstanding
        ), outstanding

    def test_backbone_compound_and_protocol_materialized_from_real_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The produced crate carries the ISA backbone, the substrate compound the
        pipeline proposed FROM the real context (resolved to its looked-up CAS, D5),
        the four-step LabProcess chain, and a LabProtocol minted from the real SOP and
        linked to a process — none of it hand-built in the fixture."""
        from builder.agents.pipeline.pipeline import run_pipeline

        _install_offline_seams(monkeypatch)
        engine = _scanning_engine(FIXTURE_DIR)
        run_pipeline(engine)
        state = engine.state

        assert {"Investigation", "Study", "Assay"} <= {e.type for e in state.list_entities()}

        chems = state.list_entities("MolecularEntity")
        t4 = next((c for c in chems if c.fields.get("name") == "Thyroxine"), None)
        assert t4 is not None, "the pipeline must materialize the compound it proposed"
        # D5: the CAS is the LOOKED-UP value, never fabricated by the plan/leaf.
        assert t4.fields.get("cas") == "51-48-9"

        # #372, the honest seam: this plan carries the documents' descriptive
        # phrase and no short catalogue name, so the exact-match gate finds
        # nothing — and the cell line must survive that anyway. A miss is not a
        # failure: returning {ok: False} would delete the Sample and with it the
        # CellCulture's `cell_line` input. No canned CVCL_0214 is handed over,
        # because "CHO-K1 OATP1C1-overexpressing cells" is not a name Cellosaurus
        # can resolve to the parent line.
        cells = state.list_entities("CellLineSample")
        assert [c.fields.get("name") for c in cells] == [
            "CHO-K1 OATP1C1-overexpressing cells"
        ]
        assert "accession" not in cells[0].fields

        procs = state.list_entities("LabProcess")
        assert {"CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"} <= {
            p.fields.get("process_type") for p in procs
        }

        # The real SOP drives a LabProtocol, linked to a process it governs.
        protocols = state.list_entities("LabProtocol")
        assert protocols, "the real SOP must materialize a LabProtocol"
        assert any(p.fields.get("labprotocol") for p in procs), (
            "the LabProtocol must be linked to a LabProcess"
        )

    def test_a_deposit_with_no_design_table_still_yields_a_populated_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#422/#438 acceptance on the genuine fixture: S-VHPS26 ships no plate map.

        Its only workbook is a Parameter|Value assay-metadata template — no
        per-well rows — so nothing in the deposit reads as a design table and the
        spine must PROPOSE one from the entities the same run materialized,
        landing real rows rather than a header-only export.

        This used to inject a plan entry labelling that workbook
        ``condition_table``, reproducing the leaf's real mislabel, because the
        label was what selected the file. Since #594 nothing reads it — the rows
        decide — so the injection is gone rather than kept as inert scaffolding
        that would make this pass for a reason that no longer exists.
        """
        from builder.agents.pipeline.pipeline import run_pipeline

        _install_offline_seams(monkeypatch)
        engine = _scanning_engine(FIXTURE_DIR)
        result = run_pipeline(engine)

        table = (result.get("materialized") or {}).get("condition_table") or {}
        assert table.get("populated") is True, f"header-only table shipped: {table}"
        assert table.get("proposed") is True
        assert table.get("fallback_from"), "the original refusal must be recorded"
        path = table.get("path")
        assert path and Path(path).is_file()
        body = Path(path).read_text(encoding="utf-8")
        assert len(body.strip().splitlines()) > 1, "the table is header-only"
        assert "Thyroxine" in body, "the proposed rows must carry the real compound"

    def test_raw_and_processed_data_files_attached_to_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real raw (.xlsx) and processed (.pzfx) measurement files are attached to
        the crate as File entities, each stamped with its classification — the
        data-file fidelity a metadata-only fixture cannot exercise.

        Both sit in one ``raw data+individual processed data/``, so the folder
        names both tiers and can say nothing about either; they split on their own
        content, the instrument's column headers against a Prism project's XML
        root (#591)."""
        from builder.agents.pipeline.pipeline import run_pipeline
        from builder.tools.document_discovery import CLASS_PROCESSED_DATA, CLASS_RAW_DATA

        _install_offline_seams(monkeypatch)
        engine = _scanning_engine(FIXTURE_DIR)
        run_pipeline(engine)

        files = engine.state.list_entities("File")
        by_name = {f.fields.get("name"): f for f in files}
        assert _RAW_DATA.name in by_name, "the raw data file must be attached"
        assert _PROCESSED_DATA.name in by_name, "the processed data file must be attached"
        assert by_name[_RAW_DATA.name].fields.get("role") == CLASS_RAW_DATA
        assert by_name[_PROCESSED_DATA.name].fields.get("role") == CLASS_PROCESSED_DATA

    def test_no_compound_without_the_real_document(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Honesty control: the SAME seams over input that lacks the study tokens
        propose no compound — proving the compound above is caused by the real
        S-VHPS26 documents, not hard-coded in the stub."""
        from builder.agents.pipeline.pipeline import run_pipeline

        (tmp_path / "unrelated.txt").write_text(
            "A note with no study, compound, or protocol tokens.", encoding="utf-8"
        )

        _install_offline_seams(monkeypatch)
        engine = _scanning_engine(tmp_path)
        run_pipeline(engine)

        chems = engine.state.list_entities("MolecularEntity")
        assert not any(c.fields.get("name") == "Thyroxine" for c in chems)


class TestRealSopParametersReachTheCrate:
    """The SOP's stated conditions must replace the fabricated placeholders (#379).

    Every default-arm crate published 11 ontology-typed PropertyValues asserting
    conditions nobody stated — `Exposure Duration = "unknown"`, `Detection
    Instrument = "unknown"`, `Technical replicate = "1"` — each carrying a real
    BAO `propertyID`, with the tox SHACL pass reporting conformant. The real SOP
    states the duration and the instrument in its body; before this the plan had
    nowhere to put them.
    """

    @staticmethod
    def _parameter_values(state) -> dict[str, str]:
        from builder.tools.builder import assemble_crate

        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
        graph = crate.metadata.generate().get("@graph", [])
        out: dict[str, str] = {}
        for node in graph:
            if not isinstance(node, dict) or "PropertyValue" not in str(node.get("@type")):
                continue
            name, value = node.get("name"), node.get("value")
            if isinstance(name, str) and isinstance(value, str):
                out[name] = value
        return out

    def test_sop_body_tokens_reach_the_extraction_leaf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard the input side first, so a budget regression fails legibly.

        Without this, a `_gather_context` change that drops the SOP slice would
        redden the crate assertions below opaquely.
        """
        from builder.agents.pipeline.pipeline import run_pipeline

        capture = _install_offline_seams(monkeypatch)
        run_pipeline(_scanning_engine(FIXTURE_DIR))

        context = capture["context"].lower()
        assert _TOKEN_SOP_DURATION in context
        assert _TOKEN_SOP_INSTRUMENT in context

    def test_process_parameters_from_the_real_sop_reach_the_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from builder.agents.pipeline.pipeline import run_pipeline

        _install_offline_seams(monkeypatch)
        engine = _scanning_engine(FIXTURE_DIR)
        run_pipeline(engine)

        values = self._parameter_values(engine.state)
        assert values["Exposure Duration"] == "30 minutes"
        assert values["Detection Instrument"] == "gamma counter"

    def test_no_process_parameter_without_the_real_sop_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """HONESTY CONTROL — the values came from the scanned BODY, not the stub.

        Mirrors `test_no_compound_without_the_real_document`: a folder holding an
        empty file with the SOP's own FILENAME. The name is present, the body is
        not, and the placeholders must return.
        """
        from builder.agents.pipeline.pipeline import run_pipeline

        (tmp_path / _SOP.name).write_text("", encoding="utf-8")

        _install_offline_seams(monkeypatch)
        engine = _scanning_engine(tmp_path)
        run_pipeline(engine)

        values = self._parameter_values(engine.state)
        # The parameters are now OMITTED rather than published as "unknown":
        # `_pv` will not emit a placeholder a reader cannot tell from a real
        # answer (D5). Absence alone would also pass if the whole chain vanished,
        # so the #262 never-hollow guarantee is asserted directly instead of
        # being inferred from the placeholder's presence.
        assert "Exposure Duration" not in values
        assert "Detection Instrument" not in values
        chain = {p.fields.get("process_type") for p in engine.state.list_entities("LabProcess")}
        assert {"Exposure", "EndpointReadout"} <= chain, chain
