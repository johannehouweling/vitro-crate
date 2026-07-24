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
      README.txt                                   the study README
      OATP1C1 SOP TH 250425.docx                   the real Standard Operating Procedure
      raw data+individual processed data/220825_RA_CHO-K1_hOATP1C1/
        …_P1_Timecourse.xlsx                       real RAW measurement data
        …_P1_Timecourse.pzfx                       real PROCESSED (GraphPad) data

So the scan exercises real directory recursion (paths with spaces / ``+``), the
metadata/README/SOP body reads, the assay spreadsheet preview, and the raw-vs-
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
# descriptor/README/spreadsheet), NEVER in a filename — so a total content regression
# (scanner delivering filenames only) empties the plan. "oatp1c1"/"cho-k1"/"th"/"sop"
# all occur in filenames and are deliberately NOT used as gates.
_TOKEN_SUBSTRATE = "thyroxine"
# A method token that lives in the SOP .docx BODY (the Sandell-Kolthoff readout),
# reachable only if the scanner read the Word document body (via python-docx) — it is
# in no filename and no tabular preview. Gates the protocol and proves the SOP body
# reached the leaf.
_TOKEN_SOP_BODY = "sandell"


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
        context: str, *, model: str | None = None, usage_sink: Any = None
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
        if _TOKEN_SOP_BODY in low:
            # The SOP body (read from the .docx) drives a LabProtocol governing the
            # exposure — proposed only because the procedure text reached the leaf.
            plan["protocols"] = [
                {
                    "name": "OATP1C1 thyroxine-uptake SOP",
                    "description": (
                        "Standard operating procedure: cellular thyroxine uptake read "
                        "out via the Sandell-Kolthoff reaction."
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
    def fake_lookup_compound(name: str) -> dict[str, Any]:
        return {
            "found": True,
            "data": {"cas": "51-48-9", "pubchem_cid": "5819", "source": "pubchem"},
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
        processed roles, and never escapes the root (no repo pollution)."""
        from builder.agents.pipeline.pipeline import _file_role

        engine = _scanning_engine(FIXTURE_DIR)
        scanned = {Path(f.path).name: f for f in engine.state.scanned_files}

        assert "S-VHPS26.json" in scanned
        assert _SOP.name in scanned
        assert _RAW_DATA.name in scanned  # recursion into the spaced/"+" subdir worked
        assert _PROCESSED_DATA.name in scanned

        roles = {_file_role(f.filename, f.mime_type or "") for f in scanned.values()}
        assert {"raw_data", "processed_data"} <= roles
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
        # Reachable only via the SOP .docx body read (python-docx), never a filename.
        assert _TOKEN_SOP_BODY in context, capture["context"][:600]

    def test_pipeline_builds_conformant_crate_from_real_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deterministic spine, over the real scanned crate, reaches
        ``{base, isa, tox}`` REQUIRED conformance."""
        from builder.agents.pipeline.pipeline import run_pipeline

        _install_offline_seams(monkeypatch)
        result = run_pipeline(_scanning_engine(FIXTURE_DIR))

        assert result["ok"] is True, result["issues"]
        assert result["conformance"] == {"base": True, "isa": True, "tox": True}

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

    def test_raw_and_processed_data_files_attached_to_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real raw (.xlsx) and processed (.pzfx) measurement files are attached to
        the crate as File entities, each stamped with its raw/processed role — the
        data-file fidelity a metadata-only fixture cannot exercise."""
        from builder.agents.pipeline.pipeline import run_pipeline

        _install_offline_seams(monkeypatch)
        engine = _scanning_engine(FIXTURE_DIR)
        run_pipeline(engine)

        files = engine.state.list_entities("File")
        by_name = {f.fields.get("name"): f for f in files}
        assert _RAW_DATA.name in by_name, "the raw data file must be attached"
        assert _PROCESSED_DATA.name in by_name, "the processed data file must be attached"
        assert by_name[_RAW_DATA.name].fields.get("role") == "raw_data"
        assert by_name[_PROCESSED_DATA.name].fields.get("role") == "processed_data"

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
