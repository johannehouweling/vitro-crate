"""Deterministic build -> validate -> on-disk round-trip harness over a golden crate (Issue #59).

This does NOT exercise the agent/producer path — it hand-scripts the exact
``engine.run_tool(...)`` sequence for the S-VHPS21 golden fixture (#97) to assemble a
known-good ``CrateState``, then exercises the **deterministic downstream** that consumes
it. The scripted state is legitimate *input* to that downstream (the writer, mapper and
validator take a ``CrateState``); it is NOT a stand-in for extraction:

    scan_files          -> inventory the input directory (via the real guard)
    draft_* (scripted)  -> assemble the golden ISA backbone + contributors + domain
                           entities from the #97 spec (no LLM, no inference)
    build_and_validate  -> in-memory 3-pass SHACL gate
    build_crate         -> materialise the on-disk RO-Crate
    validate            -> re-validate the crate read back from disk
    assess_mit_coverage / assess_fair_maturity -> score floors

The assertions are on that downstream: crate completeness & wiring on the built
``ro-crate-metadata.json`` (required entity types, ``hasPart``/``about`` containment,
``conformsTo`` placement) plus FAIR/MIT floors — so a regression in the
build/mapping/validation path fails the suite.

The genuine producer coverage — does the real pipeline extract these entities from real
scanned input? — lives in ``tests/test_pipeline_real_input.py`` (#342). This harness
deliberately fixes the input so the downstream stays deterministic and offline; it does
not, and must not be read as, a check that the agent drafts the right entities.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

from builder.engine import AgentEngine
from tests.fixtures.vhps_golden_crates import VHPS_STUDIES

FIXTURE_INPUT_DIR = Path(__file__).parent / "fixtures" / "svhps21_input"

# This module is a heavy integration harness: each test drives the full scripted
# tool sequence plus real SHACL validation (build_and_validate + an on-disk
# round-trip validate). Under RO-Crate 1.2 the validator is slower (larger
# ontology / pyshacl ontology-mixing), so the global CI --timeout=30 is too tight
# for these on shared runners. Raise the per-test ceiling for this file only.
pytestmark = pytest.mark.timeout(120)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Run the whole scripted e2e with the live network disabled (#117).

    #59 requires this harness to run "deterministically without network/LLM
    access", but the SHACL validator used to dereference the remote RO-Crate
    ``@context`` (``https://w3id.org/ro/crate/1.2/context``); a transient fetch
    failure once produced a spurious REQUIRED issue and flaked CI (#116). The
    validator now serves a bundled local copy of the context, so we hard-block
    the HTTP transport here to *prove* the path is offline-safe: any real
    outbound request raises, turning a regression (a re-introduced network
    dependency) into a deterministic failure rather than a flake.
    """
    import requests
    from requests.adapters import HTTPAdapter

    def _blocked_send(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise requests.exceptions.ConnectionError(
            f"network disabled in offline e2e test (attempted {request.url})"
        )

    monkeypatch.setattr(HTTPAdapter, "send", _blocked_send)


def _scripted_build(engine: AgentEngine) -> dict:
    """Drive the deterministic scripted tool sequence for S-VHPS21.

    Every value an agent would have inferred from the folder is hard-coded from
    the S-VHPS21 golden spec so the run is fully deterministic and offline.
    Returns the ``build_and_validate`` result for the caller to assert on.
    """
    spec = VHPS_STUDIES["S-VHPS21"]
    given, _, family = spec.author_name.partition(" ")

    # --- 0. crate-level metadata the session establishes (root name/identifier).
    # This is CrateState.metadata, not a per-entity draft — the agent/session
    # sets it from the study accession (mirrors the golden fixture + the
    # validator-wiring test). The root data entity needs a non-empty identifier.
    engine.state.metadata.title = spec.title
    engine.state.metadata.description = spec.description
    engine.state.metadata.accession = spec.accession
    engine.state.metadata.input_path = str(FIXTURE_INPUT_DIR)

    # --- 1. scan: inventory the input dir through the real approved-roots guard
    scanned = engine.run_tool("scan_files", path=str(FIXTURE_INPUT_DIR))
    assert scanned, "scanner returned no files for the fixture input dir"

    # --- 2. draft the ISA backbone, wired by entity_id references
    inv = engine.run_tool(
        "draft_investigation",
        hints={
            "name": spec.title,
            "description": spec.description,
            "identifier": spec.accession,
        },
    )
    study = engine.run_tool(
        "draft_study",
        investigation_id=inv.entity_id,
        hints={
            "name": spec.title,
            "description": spec.description,
            "identifier": spec.accession,
            "datePublished": spec.release_date,
        },
    )
    assay = engine.run_tool(
        "draft_assay",
        study_id=study.entity_id,
        hints={"name": spec.assay_name, "identifier": f"{spec.accession}-assay"},
    )

    # --- 3. contributors
    engine.run_tool(
        "draft_person",
        name=spec.author_name,
        hints={
            "givenName": given,
            "familyName": family or given,
            "orcid": spec.author_orcid,
        },
    )
    engine.run_tool("draft_organization", name=spec.organization, hints={})

    # --- 4. domain entities
    cell = engine.run_tool("draft_cell_line_sample", name=spec.cell_line, hints={})
    compound = engine.run_tool("draft_molecular_entity", name=spec.compound, hints={})

    # --- 5. the Exposure LabProcess anchoring the assay's derivation chain
    engine.run_tool(
        "draft_process",
        assay_id=assay.entity_id,
        process_type="Exposure",
        hints={
            "name": f"{spec.compound} uptake exposure",
            "samples": cell.entity_id,
            "chemicals": compound.entity_id,
        },
    )

    # --- 6. in-memory 3-pass SHACL gate (no disk)
    return engine.run_tool("build_and_validate", severity="required")


@dataclass
class ScriptedRun:
    """The artefacts of one scripted scan->draft->build->validate run."""

    engine: AgentEngine
    build_and_validate: dict


@pytest.fixture
def scripted_run() -> ScriptedRun:
    """A fresh engine that has run the full scripted scan->draft sequence."""
    engine = AgentEngine()
    # initialize() with the fixture dir seeds approved_scan_roots the legitimate
    # way (a user-provided input path), so the later run_tool("scan_files", ...)
    # is allowed by the now fail-closed guard (#197). We re-scan via run_tool
    # below to exercise the real approved-roots path.
    engine.initialize(input_path=str(FIXTURE_INPUT_DIR))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = _scripted_build(engine)
    return ScriptedRun(engine=engine, build_and_validate=result)


class TestScriptedAgentSequenceConformance:
    """The scripted sequence yields a crate that passes all three SHACL passes."""

    def test_build_and_validate_passes_all_three_at_required(self, scripted_run):
        result = scripted_run.build_and_validate
        assert result["ok"] is True, result["issues"]
        assert result["conformance"] == {"base": True, "isa": True, "tox": True}
        assert result["issues"] == []


class TestBuiltCrateCompletenessAndWiring:
    """Assert completeness & wiring on the on-disk ro-crate-metadata.json."""

    @pytest.fixture
    def graph(self, scripted_run, tmp_path) -> list[dict]:
        """Materialise the crate to disk via build_crate, re-validate, return @graph."""
        out = tmp_path / "crate"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            build_res = scripted_run.engine.run_tool("build_crate", output_path=str(out))
            assert build_res["success"] is True, build_res["error"]

            # Re-validate the crate read back from disk (the round-trip path).
            report = scripted_run.engine.run_tool("validate", crate_path=build_res["crate_path"])
        assert report.base_passed, report.required_issues
        assert report.isa_passed, report.required_issues
        assert report.tox_passed, report.required_issues

        metadata = json.loads(
            (out / "ro-crate-metadata.json").read_text(encoding="utf-8")
        )
        assert "@context" in metadata
        return metadata["@graph"]

    def _by_id(self, graph: list[dict]) -> dict[str, dict]:
        return {e["@id"]: e for e in graph if "@id" in e}

    def _additional_types(self, graph: list[dict]) -> set[str]:
        types: set[str] = set()
        for e in graph:
            at = e.get("additionalType")
            if isinstance(at, str):
                types.add(at)
            elif isinstance(at, list):
                types.update(t for t in at if isinstance(t, str))
        return types

    def test_required_entity_types_present_in_graph(self, graph):
        """Investigation + Study + Assay are present as typed Datasets."""
        addl = self._additional_types(graph)
        assert {"Investigation", "Study", "Assay"} <= addl, addl
        # LabProcess present (typed via @type, e.g. LabProcess/Exposure)
        type_blob = json.dumps([e.get("@type") for e in graph])
        assert "LabProcess" in type_blob, type_blob

    def test_haspart_about_wiring(self, graph):
        """Root --hasPart--> Investigation/Study; Study --hasPart--> Assay;
        Assay --about--> LabProcess."""
        by_id = self._by_id(graph)

        def ids(node: dict, key: str) -> set[str]:
            val = node.get(key, [])
            if isinstance(val, dict):
                val = [val]
            return {v["@id"] for v in val if isinstance(v, dict) and "@id" in v}

        def additional_type(node: dict) -> set[str]:
            at = node.get("additionalType")
            if isinstance(at, list):
                return {t for t in at if isinstance(t, str)}
            return {at} if isinstance(at, str) else set()

        def types_of(node_ids: set[str]) -> set[str]:
            types: set[str] = set()
            for i in node_ids:
                if i in by_id:
                    types |= additional_type(by_id[i])
            return types

        # Root Data Entity is the "./" Dataset — and IS the Investigation
        # (folded onto the root, not emitted as a separate node).
        root = by_id["./"]
        assert root.get("@type") == "Dataset"
        assert "Investigation" in additional_type(root), root.get("additionalType")

        # Root hasPart references the Study (the Investigation is the root itself,
        # so it is not in its own hasPart).
        root_part_types = types_of(ids(root, "hasPart"))
        assert "Study" in root_part_types, root_part_types
        assert "Investigation" not in root_part_types, root_part_types

        # Study hasPart references the Assay.
        study = next(n for n in graph if "Study" in additional_type(n))
        assert "Assay" in types_of(ids(study, "hasPart")), study.get("hasPart")

        # Assay about references the LabProcess (the experimental process).
        assay = next(n for n in graph if "Assay" in additional_type(n))
        about_ids = ids(assay, "about")
        assert about_ids, "Assay has no `about` link to its LabProcess"
        about_types = json.dumps([by_id[i].get("@type") for i in about_ids if i in by_id])
        assert "LabProcess" in about_types, about_types

    def test_conformsto_isa_and_isatox_on_root_descriptor_base_only(self, graph):
        """Profile conformsTo placement follows RO-Crate 1.2 (#91): the ISA +
        ISA-Tox profiles the crate targets are declared on the Root Data Entity
        (``./``), while the metadata descriptor's conformsTo is the single
        base-spec URI only (no profile URIs)."""

        def conforms_ids(node):
            conforms = node.get("conformsTo")
            if isinstance(conforms, dict):
                conforms = [conforms]
            return [c.get("@id", "") for c in conforms or []]

        root = next(e for e in graph if e.get("@id") == "./")
        descriptor = next(
            e for e in graph if e.get("@id") == "ro-crate-metadata.json"
        )

        root_ids = conforms_ids(root)
        desc_ids = conforms_ids(descriptor)

        # Root Data Entity declares both targeted profiles (ISA + ISA-Tox).
        assert sum("isa" in i.lower() for i in root_ids) >= 2, root_ids

        # Descriptor conformsTo is the base spec only — no profile URIs there.
        assert any("w3id.org/ro/crate" in i for i in desc_ids), desc_ids
        assert not any("isa" in i.lower() for i in desc_ids), desc_ids


class TestScoreFloors:
    """A FAIR/MIT score floor on the built crate — guards against silent drops."""

    def test_mit_coverage_floor(self, scripted_run):
        report = scripted_run.engine.run_tool("assess_mit_coverage")
        assert report.module_scores, "MIT report has no module scores"
        # The scripted crate fills the ISA backbone + key domain fields. Assert a
        # conservative non-zero floor so a regression that stops drafting key
        # entities (dropping coverage to 0) fails here. The General Information
        # module specifically must have completions from the drafted entities.
        assert report.overall_score > 0.0, report.overall_score
        general = report.module_scores.get("General Information", {})
        assert general.get("completed", 0) >= 2, report.module_scores

    def test_fair_maturity_floor(self, scripted_run):
        report = scripted_run.engine.run_tool("assess_fair_maturity")
        passed = [r for r in report.indicator_results if r.get("passed") is True]
        assert passed, "no FAIR indicators passed"
        # DSM level is cumulative; the scripted crate has title+description+
        # entities+ids, clearing the lowest tiers.
        assert report.dsm_level >= 1, report.dsm_level
