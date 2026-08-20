"""The evaluation corpus — a small fixed set of crate-build cases as data.

Each :class:`EvalCase` declares:

* a stable ``case_id`` and human ``description``;
* a ``kind`` — one of the three input tiers from AGENTS.md §9
  (``"minimal"`` / ``"structured"`` / ``"unstructured"``);
* a ``prompt`` — the natural-language request handed to a *conversational* agent
  (the ReAct engine / tail agent) to drive the build;
* an optional ``input_path`` — an in-repo, offline directory the agent scans
  (the structured-metadata case);
* an optional ``build_state`` — a zero-arg factory returning a finished
  :class:`CrateState`. The **mock** agent factory uses it to stand in for a real
  build so the harness logic is unit-testable offline; live agents ignore it.

The success predicate is shared and deliberately strict: a case succeeds when its
crate reaches ``{base, isa, tox}`` REQUIRED conformance through ``build_and_validate``
(see :func:`reaches_isa_tox_conformance`).

A case may *additionally* declare ``min_entities`` — a minimum count of domain
entities (by ``@type``) its build must produce. That gives the A/B a second,
additive **content-quality** signal (:func:`meets_entity_quota`): conformance
measures whether the agent *acted*; the quota measures whether what it drafted is
actually there. ``min_entities`` never changes the success predicate; it is a
separate, optional metric so cases that do not declare it are simply not assessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from builder.state import CrateState

CaseKind = Literal["minimal", "structured", "unstructured"]

# The structured-metadata fixture is an in-repo, offline research folder (no real
# experimental data) — the same one the #59 end-to-end quality test uses.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_STRUCTURED_INPUT = _REPO_ROOT / "tests" / "fixtures" / "svhps21_input"
# A richer structured fixture (Issue #179): a clear compound + cell line + a
# protocol + a couple of data files + a README, so a build must draft several
# distinct domain entities rather than just a backbone.
_DRAFTING_INPUT = _REPO_ROOT / "tests" / "fixtures" / "svhps22_input"
# A realistic *arbitrary* research folder (Issue #179, decision-gate task 6): the
# raw documents a researcher actually keeps — a study description, a methods /
# protocol write-up, a compound list, and nested measurement + analysis CSVs, with
# NO metadata file. It exercises the full scan -> extract -> materialize -> assess
# path for both archs, and a *good* build must draft a COMPLETE in-vitro tox study
# (the four-step process chain), not just a backbone. Lives under eval/ so the
# A/B owns its own fixture independently of the tests/ end-to-end fixtures.
_ARBITRARY_TOX_INPUT = _REPO_ROOT / "eval" / "fixtures" / "arbitrary_tox_folder"


@dataclass(frozen=True)
class EvalCase:
    """One crate-build case in the corpus.

    Attributes:
        case_id: Stable identifier, unique within the corpus.
        description: Human-readable summary of what the case exercises.
        kind: The input tier — ``"minimal"`` / ``"structured"`` / ``"unstructured"``.
        prompt: NL request driving a conversational agent's build.
        input_path: Optional in-repo directory the agent scans (offline).
        build_state: Optional factory of a finished state, used by the mock agent.
        min_entities: Optional minimum domain-entity quota by ``@type`` (e.g.
            ``{"MolecularEntity": 1, "CellLineSample": 1, "File": 2}``). When set,
            the harness records an additive content-quality signal — whether the
            build drafted at least that many of each type — *on top of* the strict
            conformance success predicate. ``None`` ⇒ quality is not assessed.
    """

    case_id: str
    description: str
    kind: CaseKind
    prompt: str = ""
    input_path: str | None = None
    build_state: Callable[[], CrateState] | None = None
    min_entities: dict[str, int] | None = None


def reaches_isa_tox_conformance(state: CrateState) -> dict[str, Any]:
    """Success predicate: does *state* reach ``{base, isa, tox}`` conformance?

    Runs ``build_and_validate`` at REQUIRED severity over all three layers and
    returns ``{"success": bool, "conformance": {layer: bool}, "issues": [...]}``.
    ``success`` is true only when every layer passes REQUIRED.

    Args:
        state: The crate state produced by an agent build.

    Returns:
        A dict with the boolean verdict, the per-layer conformance map, and the
        list of routable issues (empty on success).
    """
    from eval.metrics import evaluate_success

    result = evaluate_success(state, profile="all", severity="required")
    conformance = result.get("conformance", {})
    layers = ("base", "isa", "tox")
    success = bool(conformance) and all(conformance.get(layer) for layer in layers)
    return {
        "success": success,
        "conformance": conformance,
        "issues": result.get("issues", []),
    }


def meets_entity_quota(state: CrateState, min_entities: dict[str, int] | None) -> dict[str, Any]:
    """Content-quality check: did *state* draft at least ``min_entities`` per type?

    This is the second, *additive* signal the A/B uses alongside
    :func:`reaches_isa_tox_conformance`. Conformance answers "did the agent act?";
    the quota answers "is the drafted domain content actually there?" — a crate can
    reach ``{base, isa, tox}`` with an almost-empty backbone, so a draft-quality
    case demands a minimum set of domain entities (a compound, a cell line, files…).

    Args:
        state: The crate state produced by an agent build.
        min_entities: Required minimum count per entity ``@type``. ``None`` means
            the case does not assess content quality.

    Returns:
        A dict with:

        * ``meets_quota`` — ``True``/``False`` when a quota is declared, else
          ``None`` (quality not assessed);
        * ``entity_counts`` — actual count of each *demanded* type in the state
          (empty when no quota);
        * ``missing`` — ``{type: shortfall}`` for every type below its minimum
          (empty when the quota is met or undeclared).
    """
    if not min_entities:
        return {"meets_quota": None, "entity_counts": {}, "missing": {}}

    entity_counts: dict[str, int] = {}
    missing: dict[str, int] = {}
    for entity_type, required in min_entities.items():
        count = len(state.list_entities(entity_type=entity_type))
        entity_counts[entity_type] = count
        if count < required:
            missing[entity_type] = required - count

    return {
        "meets_quota": not missing,
        "entity_counts": entity_counts,
        "missing": missing,
    }


# --- mock-build state factories (offline stand-ins for a real agent build) -----
#
# These let the harness's runner / report / determinism logic be exercised end to
# end without a live model. A live agent never calls them; they exist so the
# *mock* agent factory has a finished, REQUIRED-clean state to return per case.


def _minimal_state() -> CrateState:
    """A REQUIRED-clean S-VHPS21 backbone — the simplest passing crate."""
    from tests.fixtures.vhps_golden_crates import vhps_fixture_state

    return vhps_fixture_state("S-VHPS21")


def _structured_state() -> CrateState:
    """Stand-in for building from the structured svhps21 input folder."""
    from tests.fixtures.vhps_golden_crates import vhps_fixture_state

    return vhps_fixture_state("S-VHPS21")


def _unstructured_state() -> CrateState:
    """Stand-in for a conversation-driven build with no metadata files."""
    from tests.fixtures.vhps_golden_crates import vhps_fixture_state

    return vhps_fixture_state("S-VHPS21")


def _drafting_state() -> CrateState:
    """A *richly* drafted S-VHPS22 crate — the offline stand-in for a good build.

    Unlike the backbone-only stand-ins above, this drafts the full domain set the
    ``structured-svhps22`` case is designed to elicit: the ISA backbone, a
    compound, a cell line, contributors, a lab protocol, the Exposure process, the
    two attached data files, and the Adverse Outcome Pathway it investigates
    (AOP-Wiki 42, Issue #180). It is REQUIRED-clean across base/ISA/ISA-Tox *and*
    satisfies that case's ``min_entities`` quota, so the content-quality signal is
    exercisable offline with a mock agent.
    """
    from builder.state import CrateState, Entity, EntityProvenance, EntityType

    def _ent(entity_id: str, type_: EntityType, **fields: object) -> Entity:
        return Entity(
            entity_id=entity_id,
            type=type_,
            fields=fields,
            _provenance=EntityProvenance(created_by="llm"),
        )

    title = "Inhibition of thyroid peroxidase (TPO) activity dose-response screen"
    description = (
        "A cell-based in vitro assay screening a reference chemical for its "
        "capacity to inhibit thyroid peroxidase (TPO) activity in a "
        "TPO-overexpressing FRTL-5 follicular cell model."
    )

    state = CrateState()
    state.metadata.title = title
    state.metadata.description = description
    state.metadata.accession = "S-VHPS22"

    # ISA backbone.
    state.add_entity(
        _ent("inv", "Investigation", name=title, description=description, identifier="S-VHPS22")
    )
    state.add_entity(
        _ent(
            "study",
            "Study",
            name=title,
            description=description,
            identifier="S-VHPS22",
            investigation_id="inv",
            datePublished="2025-11-10",
            # The Adverse Outcome Pathway this TPO screen investigates
            # (schema:mentions), referencing the AdverseOutcomePathway entity below.
            aop=[{"@id": "https://aopwiki.org/aops/42"}],
        )
    )
    state.add_entity(
        _ent(
            "assay",
            "Assay",
            name="TPO inhibition dose-response assay",
            identifier="S-VHPS22-assay",
            study_id="study",
        )
    )

    # Contributors.
    state.add_entity(
        _ent(
            "author",
            "Person",
            name="Marije Vonk",
            givenName="Marije",
            familyName="Vonk",
            orcid="0000-0002-1825-0097",
        )
    )
    state.add_entity(_ent("org", "Organization", name="Universiteit Utrecht"))

    # Domain entities: compound, cell line, protocol, and the Exposure process.
    state.add_entity(_ent("cell", "CellLineSample", name="FRTL-5 TPO-overexpressing cells"))
    state.add_entity(_ent("compound", "MolecularEntity", name="Methimazole"))
    state.add_entity(
        _ent("protocol", "LabProtocol", name="Amplex Red fluorometric TPO activity readout")
    )
    state.add_entity(
        _ent(
            "exposure",
            "LabProcess",
            name="Methimazole TPO inhibition exposure",
            process_type="Exposure",
            assay_id="assay",
            samples="cell",
            chemicals="compound",
            protocol_id="protocol",
            # The tox profile requires each of Exposure / EndpointReadout /
            # DataAnalysis to carry at least one schema:additionalProperty, and
            # `_pv` no longer publishes "unknown" to satisfy it. A corpus case
            # that stands in for a GOOD agent therefore has to state real values.
            duration="24 hours",
        )
    )

    # The two attached data files from the fixture folder.
    state.add_entity(
        _ent("raw", "File", name="dose_response_raw.csv", path="raw_data/dose_response_raw.csv")
    )
    state.add_entity(
        _ent("proc", "File", name="ic50_results.csv", path="processed_data/ic50_results.csv")
    )

    # The Adverse Outcome Pathway this TPO screen investigates (AOP-Wiki 42),
    # keyed by its resolvable IRI exactly as materialize_aop_subgraph would
    # (Issue #180). AOP linking is a tox SHOULD, so it does not gate REQUIRED
    # conformance; the case's min_entities quota is what makes the A/B measure it.
    state.add_entity(
        _ent(
            "https://aopwiki.org/aops/42",
            "AdverseOutcomePathway",
            name=(
                "Inhibition of thyroid peroxidase and subsequent adverse "
                "neurodevelopmental outcomes in mammals"
            ),
            identifier="42",
            url="https://aopwiki.org/aops/42",
        )
    )
    return state


def _arbitrary_tox_folder_state() -> CrateState:
    """A *complete* in-vitro tox study — the offline stand-in for a good build.

    This is what a strong agent would materialize from the
    ``arbitrary_tox_folder`` fixture: not just a backbone + one Exposure, but the
    full ISA-Tox study described in ``profiles/docs/isa_tox.md`` — the ISA
    backbone, the contributors, a cell line, a compound, a protocol, the two
    attached data files, and the **four-step derivation chain** (CellCulture →
    Exposure → EndpointReadout → DataAnalysis). It is REQUIRED-clean across
    base/ISA/ISA-Tox *and* satisfies that case's complete-study ``min_entities``
    quota, so the content-quality signal is exercisable offline with a mock agent.

    The process I/O uses the builder's interchangeable I/O aliases (``samples`` /
    ``object`` / ``input`` for consumed inputs, ``result`` / ``output`` for
    produced outputs; see :mod:`builder.tools._crate_mapping`) so the readout's
    raw File and the analysis's processed File wire onto ``schema:result`` /
    ``schema:object`` — the MUSTs those two steps would otherwise miss.
    """
    from builder.state import CrateState, Entity, EntityProvenance, EntityType

    def _ent(entity_id: str, type_: EntityType, **fields: object) -> Entity:
        return Entity(
            entity_id=entity_id,
            type=type_,
            fields=fields,
            _provenance=EntityProvenance(created_by="llm"),
        )

    title = "TPO inhibition dose-response screen"
    description = (
        "A cell-based in vitro assay screening Methimazole for its capacity to "
        "inhibit thyroid peroxidase (TPO) activity in a TPO-overexpressing FRTL-5 "
        "rat thyroid follicular cell model, reported as a dose-response IC50."
    )

    state = CrateState()
    state.metadata.title = title
    state.metadata.description = description
    state.metadata.accession = "ARB-TOX-01"

    # ISA backbone.
    state.add_entity(
        _ent("inv", "Investigation", name=title, description=description, identifier="ARB-TOX-01")
    )
    state.add_entity(
        _ent(
            "study",
            "Study",
            name=title,
            description=description,
            identifier="ARB-TOX-01",
            investigation_id="inv",
            datePublished="2025-11-10",
        )
    )
    state.add_entity(
        _ent(
            "assay",
            "Assay",
            name="TPO inhibition dose-response assay",
            identifier="ARB-TOX-01-assay",
            study_id="study",
        )
    )

    # Contributors.
    state.add_entity(
        _ent(
            "author",
            "Person",
            name="Marije Vonk",
            givenName="Marije",
            familyName="Vonk",
            orcid="0000-0002-1825-0097",
        )
    )
    state.add_entity(_ent("org", "Organization", name="Universiteit Utrecht"))

    # Domain entities: cell line, compound, protocol.
    state.add_entity(_ent("cell", "CellLineSample", name="FRTL-5 TPO-overexpressing cells"))
    state.add_entity(_ent("compound", "MolecularEntity", name="Methimazole"))
    state.add_entity(
        _ent("protocol", "LabProtocol", name="Amplex Red fluorometric TPO activity readout")
    )

    # The raw + processed data files from the fixture folder.
    state.add_entity(
        _ent(
            "raw",
            "File",
            name="dose_response_raw.csv",
            path="measurements/dose_response_raw.csv",
        )
    )
    state.add_entity(
        _ent("proc", "File", name="ic50_results.csv", path="analysis/ic50_results.csv")
    )

    # The full four-step derivation chain: CellCulture → Exposure →
    # EndpointReadout → DataAnalysis.
    state.add_entity(
        _ent(
            "culture",
            "LabProcess",
            name="FRTL-5 cell culture",
            process_type="CellCulture",
            assay_id="assay",
            samples="cell",
            protocol_id="protocol",
        )
    )
    state.add_entity(
        _ent(
            "exposure",
            "LabProcess",
            name="Methimazole TPO inhibition exposure",
            process_type="Exposure",
            assay_id="assay",
            samples="cell",
            chemicals="compound",
            protocol_id="protocol",
            # The tox profile requires each of Exposure / EndpointReadout /
            # DataAnalysis to carry at least one schema:additionalProperty, and
            # `_pv` no longer publishes "unknown" to satisfy it. A corpus case
            # that stands in for a GOOD agent therefore has to state real values.
            duration="24 hours",
        )
    )
    state.add_entity(
        _ent(
            "readout",
            "LabProcess",
            name="Amplex Red TPO activity readout",
            process_type="EndpointReadout",
            assay_id="assay",
            samples="cell",
            output="raw",  # the raw measurement File (schema:result MUST)
            protocol_id="protocol",
            detection_instrument="Amplex Red fluorescence plate reader",
        )
    )
    state.add_entity(
        _ent(
            "analysis",
            "LabProcess",
            name="Dose-response IC50 analysis",
            process_type="DataAnalysis",
            assay_id="assay",
            input="raw",  # raw data consumed (schema:object MUST)
            output="proc",  # processed-data File produced (schema:result MUST)
            protocol_id="protocol",
            data_processing="Four-parameter logistic dose-response fit (IC50)",
        )
    )
    return state


DEFAULT_CORPUS: tuple[EvalCase, ...] = (
    EvalCase(
        case_id="minimal-backbone",
        description=(
            "Minimal case: build a conformant ISA-Tox backbone "
            "(Investigation -> Study -> Assay + one Exposure) from a short brief."
        ),
        kind="minimal",
        prompt=(
            "Build a minimal ISA-Tox RO-Crate for an in vitro assay named "
            "'MCT8-MDCK1 cellular uptake assay' studying inhibition of MCT8-mediated "
            "uptake of triiodothyronine. Use accession S-VHPS21. Scaffold the "
            "Investigation/Study/Assay backbone and add the cell line, the compound "
            "Triiodothyronine, and an Exposure process, then build and validate "
            "until base, ISA, and ISA-Tox all pass."
        ),
        build_state=_minimal_state,
    ),
    EvalCase(
        case_id="structured-svhps21",
        description=(
            "Structured-metadata directory: scan the in-repo S-VHPS21 research "
            "folder (README + raw/processed CSVs) and build a conformant crate."
        ),
        kind="structured",
        prompt=(
            "Scan the provided input folder, read its README and data files, draft "
            "the ISA-Tox entities for the S-VHPS21 MCT8 uptake study, attach the "
            "data files, then build and validate until base, ISA, and ISA-Tox pass."
        ),
        input_path=str(_STRUCTURED_INPUT),
        build_state=_structured_state,
    ),
    EvalCase(
        case_id="structured-svhps22",
        description=(
            "Entity-drafting case: scan a richer in-repo S-VHPS22 research folder "
            "(README naming a compound + cell line + protocol, plus raw/processed "
            "CSVs) and draft the full ISA-Tox domain set. Success is the strict "
            "{base, isa, tox} conformance gate; the additive min_entities quota "
            "measures whether the build actually drafted the domain content "
            "(compound, cell line, the two files, and the AOP-Wiki pathway the "
            "TPO screen investigates) — so the A/B can compare draft QUALITY, not "
            "just that the agent acted (Issues #179, #180)."
        ),
        kind="structured",
        prompt=(
            "Scan the provided input folder. Read its README and both data files, "
            "then draft the ISA-Tox entities for this TPO-inhibition dose-response "
            "study (S-VHPS22): the Investigation/Study/Assay backbone, the test "
            "compound Methimazole, the FRTL-5 TPO-overexpressing cell line, the "
            "Amplex Red protocol, an Exposure process, the contributors, and attach "
            "the raw and processed data files. Then build and validate until base, "
            "ISA, and ISA-Tox all pass."
        ),
        input_path=str(_DRAFTING_INPUT),
        build_state=_drafting_state,
        # Content-quality quota: a real draft must carry the compound, the cell
        # line, and both attached data files — not merely a conformant backbone.
        min_entities={
            "MolecularEntity": 1,
            "CellLineSample": 1,
            "File": 2,
            # AOP-Wiki linking (Issue #180): TPO inhibition is AOP 42, so a good
            # build must draft the pathway — this makes the A/B score AOP, not
            # just backbone + compound + cell line.
            "AdverseOutcomePathway": 1,
        },
    ),
    EvalCase(
        case_id="unstructured-conversation",
        description=(
            "Unstructured / conversational: no metadata files; the whole crate is "
            "elicited from the prompt and built from scratch."
        ),
        kind="unstructured",
        prompt=(
            "I ran an in vitro screen measuring whether test chemicals inhibit "
            "triiodothyronine uptake via MCT8 in overexpressing MDCK1 cells "
            "(study S-VHPS21, by Fabian Wagenaars at Universiteit Utrecht). There "
            "are no metadata files. Please build a conformant ISA-Tox RO-Crate from "
            "this description, validating until base, ISA, and ISA-Tox all pass."
        ),
        build_state=_unstructured_state,
    ),
    EvalCase(
        case_id="arbitrary-tox-folder",
        description=(
            "Realistic arbitrary research folder (Issue #179, decision-gate task "
            "6): scan an in-repo folder of raw documents a researcher actually "
            "keeps — a study description, a methods/protocol write-up, a compound "
            "list, and nested measurement + analysis CSVs, with NO metadata file — "
            "and build the COMPLETE in-vitro tox study. This is the case that "
            "exercises the full scan -> extract -> materialize -> assess path for "
            "BOTH archs (not a pre-seeded backbone). Success is the strict "
            "{base, isa, tox} conformance gate; the additive min_entities quota is "
            "set to a complete-study floor (the four-step process chain, a cell "
            "line, a compound, a protocol, the data files) so the A/B compares "
            "whether the build drafted a WHOLE study, not just that it acted."
        ),
        # An arbitrary folder with no metadata file is the unstructured tier — the
        # whole crate must be elicited from the raw documents the agent scans.
        kind="unstructured",
        prompt=(
            "Scan the provided input folder. Read the study description, the "
            "methods/protocol write-up, the compound list, and both data files, "
            "then build the full ISA-Tox RO-Crate for this in-vitro toxicology "
            "study: the Investigation/Study/Assay backbone; the contributors; the "
            "test compound and the cell line; the lab protocol; the complete "
            "four-step process chain (Cell Culture -> Exposure -> Endpoint "
            "Readout -> Data Analysis) wired to its inputs and outputs; and attach "
            "the raw measurement and processed-results files. Then build and "
            "validate until base, ISA, and ISA-Tox all pass."
        ),
        input_path=str(_ARBITRARY_TOX_INPUT),
        build_state=_arbitrary_tox_folder_state,
        # Complete-study quota (counted from profiles/docs/isa_tox.md): the ISA
        # backbone is 3 entities (Investigation + Study + Assay); a complete study
        # adds >= 1 CellLine Sample, >= 1 MolecularEntity (the test chemical), >= 1
        # LabProtocol, the full four-step LabProcess chain (CellCulture, Exposure,
        # EndpointReadout, DataAnalysis = 4), and the raw + processed data Files
        # (>= 2). That is a conservative floor: it demands the WHOLE derivation
        # chain, so an agent that reaches conformance with only a backbone + one
        # Exposure (which the strict predicate alone accepts) still misses the bar.
        min_entities={
            "Investigation": 1,
            "Study": 1,
            "Assay": 1,
            "CellLineSample": 1,
            "MolecularEntity": 1,
            "LabProtocol": 1,
            "LabProcess": 4,
            "File": 2,
        },
    ),
)
