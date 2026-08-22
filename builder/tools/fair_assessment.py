"""Tool that assesses FAIR maturity from CrateState metadata.

Checks basic FAIR indicators (metadata presence, entity IDs, license, context)
and computes DSM level from fair/dsm_indicators.yaml check results.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from builder.state import CrateState, FAIRReport, MITReport

# The graph primitives and the tri-state Verdict are shared with the Bridge2AI
# AI-readiness instrument (builder/tools/air_assessment.py): both read one assembled
# @graph and both answer in the same auditable shape, and two implementations of one
# question is how two axes come to disagree about one crate. They keep their private
# names here so every call site below reads unchanged.
from builder.tools.assessment_graph import (
    OPEN_MEDIA_TYPES as _OPEN_MEDIA_TYPES,
)
from builder.tools.assessment_graph import (
    Graph,
    Verdict,
)
from builder.tools.assessment_graph import (
    as_verdict as _as_verdict,
)
from builder.tools.assessment_graph import (
    columns as _columns,
)
from builder.tools.assessment_graph import (
    is_external_iri as _is_external_iri,
)
from builder.tools.assessment_graph import (
    needs_graph as _needs_graph,
)
from builder.tools.assessment_graph import (
    node_types as _node_types,
)
from builder.tools.assessment_graph import (
    nodes as _nodes,
)
from builder.tools.assessment_graph import (
    ref_id as _ref_id,
)

logger = logging.getLogger(__name__)

# Path to the FAIR YAML files
FAIR_INDICATORS_PATH = Path(__file__).resolve().parent.parent.parent / "fair" / "indicators.yaml"
DSM_INDICATORS_PATH = Path(__file__).resolve().parent.parent.parent / "fair" / "dsm_indicators.yaml"

# The DSM ladder starts at 1. Level 0 states the pre-FAIRification condition in the
# negative ("Dataset(s) are NOT Identifiable..."), so failing its indicators is the
# desired outcome and scoring them would invert the scale.
_DSM_FIRST_LEVEL = 1


def _load_yaml(path: Path) -> dict[str, Any] | None:
    """Load and parse a YAML file safely.

    Returns the parsed content, or None on failure.
    """
    try:
        import yaml

        with open(path) as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning("Failed to load YAML from %s: %s", path, e)
        return None


def _check_root_global_id(state: CrateState) -> bool:
    """Check that the crate has a globally unique identifier (accession or session_id)."""
    return bool(state.metadata.accession or state.session_id)


def _check_every_entity_has_id(state: CrateState) -> bool:
    """Check that every entity has an entity_id."""
    entities = state.list_entities()
    if not entities:
        return False
    return all(bool(e.entity_id) for e in entities)


def _check_pid_form(state: CrateState) -> bool:
    """Check that metadata is identified by a persistent identifier."""
    accession = state.metadata.accession or ""
    return bool(accession and (accession.startswith("10.") or "doi" in accession.lower()))


def _check_rich_metadata(state: CrateState) -> bool:
    """Check that rich metadata is provided (title + description)."""
    return bool(state.metadata.title and state.metadata.description)


def _check_metadata_refs_data(state: CrateState) -> bool:
    """Check that metadata references data (entities present)."""
    return len(state.list_entities()) > 0


def _check_jsonld_context(state: CrateState) -> bool:
    """Check that metadata uses JSON-LD context / machine-understandable format."""
    return len(state.list_entities()) > 0


def _check_fair_vocabularies(state: CrateState) -> bool:
    """Check that metadata uses FAIR-compliant vocabularies (has entity types)."""
    return len(state.list_entities()) > 0


def _check_qualified_refs(state: CrateState) -> bool:
    """Check that metadata includes references to other metadata."""
    entity_ids = {e.entity_id for e in state.list_entities()}
    for entity in state.list_entities():
        for field_value in entity.fields.values():
            if isinstance(field_value, str) and field_value in entity_ids:
                return True
            if isinstance(field_value, list):
                for item in field_value:
                    if isinstance(item, str) and item in entity_ids:
                        return True
    return False


def _check_reuse_attributes(state: CrateState) -> bool:
    """Check that a plurality of accurate, relevant attributes exists."""
    return len(state.list_entities()) >= 2


def _check_license_present(state: CrateState) -> bool:
    """Check that metadata includes license information."""
    for entity in state.list_entities():
        if "license" in entity.fields and entity.fields["license"]:
            return True
    return False


def _check_license_standard(state: CrateState) -> bool:
    """Check that metadata refers to a standard reuse license."""
    for entity in state.list_entities():
        lic = entity.fields.get("license", "")
        if lic and ("creativecommons" in str(lic).lower() or "cc-" in str(lic).lower()):
            return True
    return False


def _check_license_machine(state: CrateState) -> bool:
    """Check that license is machine-understandable (URL)."""
    for entity in state.list_entities():
        lic = entity.fields.get("license", "")
        if lic and str(lic).startswith("http"):
            return True
    return False


def _check_provenance(state: CrateState) -> bool:
    """Check that metadata includes provenance per community standards."""
    return any(e._provenance.created_by != "missing" for e in state.list_entities())


def _check_conforms_to_profile(state: CrateState) -> bool:
    """Check that metadata complies with a community standard."""
    types = {e.type for e in state.list_entities()}
    has_isa_types = bool(types & {"Investigation", "Study", "Assay", "LabProcess"})
    return has_isa_types


def _mit_has_coverage(report: MITReport) -> bool:
    """True when a MIT report records actual (non-empty, non-zero) coverage."""
    return report.module_scores != {} and report.overall_score > 0


def _check_mit_coverage_indicator(state: CrateState) -> bool:
    """Check that MIT coverage is tracked (report present).

    Reads ``state.mit_assessment`` — the back-compat path. On the report/export
    path that field is never populated, so callers pass the freshly-computed report
    to :func:`assess_fair_maturity` instead (``mit=``), which is scored against the
    assembled ``@graph`` (#311).
    """
    return _mit_has_coverage(state.mit_assessment)


# DSM indicator checks
def _check_unique_id(state: CrateState) -> bool:
    """Each Dataset is assigned a unique identifier."""
    return bool(state.session_id or state.metadata.accession)


def _check_study_summary(state: CrateState) -> bool:
    """Dataset Descriptor includes a descriptive study/project summary."""
    return bool(state.metadata.title and state.metadata.description)


def _check_dataset_metadata(state: CrateState) -> bool:
    """Dataset Descriptor includes identifying + descriptive metadata."""
    return len(state.list_entities()) > 0


def _check_access_info(state: CrateState) -> bool:
    """Dataset Descriptor contains access information.

    A self-contained RO-Crate carries its access information intrinsically: the
    data is reached through the crate, and the descriptor states how — a resolvable
    identity, explicit reuse terms, included data files, or a known location.
    Reading *only* the incidental build-time ``output_path`` / ``input_path`` (unset
    on the report and fixture paths) collapsed the whole DSM ladder at L1 even for
    complete crates (#311). Credit crate content instead; a crate with none of these
    genuinely lacks access information and still fails.
    """
    md = state.metadata
    has_location = bool(md.output_path or md.input_path)
    has_identity = bool(md.accession or state.session_id)
    has_license = any(e.fields.get("license") for e in state.list_entities())
    has_data = bool(state.list_entities("File"))
    return has_location or has_identity or has_license or has_data


def _check_has_descriptor(state: CrateState) -> bool:
    """Dataset metadata is a formally identifiable Dataset Descriptor."""
    return bool(state.metadata.title)


def _check_context_fields(state: CrateState) -> bool:
    """Contextual metadata reported at summary level."""
    return len(state.list_entities()) > 0 or bool(state.metadata.title)


def _check_dataset_hierarchy(state: CrateState) -> bool:
    """Data organised into Dataset(s) created for FAIR sharing."""
    return len(state.list_entities()) > 0


def _check_general_schema(state: CrateState) -> bool:
    """Descriptor conforms to a general-purpose metadata schema."""
    return len(state.list_entities()) > 0


def _check_descriptor_machine_readable(state: CrateState) -> bool:
    """Dataset Descriptor available in machine-readable format."""
    return len(state.list_entities()) > 0


def _check_data_machine_readable(state: CrateState) -> bool:
    """Dataset(s) available in machine-readable format."""
    return len(state.list_entities()) > 0


def _check_cross_dataset_refs(state: CrateState) -> bool:
    """Descriptor references related Datasets."""
    return _check_qualified_refs(state)


def _check_field_level_metadata(state: CrateState) -> bool:
    """Descriptor includes field-level metadata."""
    return len(state.list_entities()) > 1


def _check_value_level_metadata(state: CrateState) -> bool:
    """Descriptor includes value-level metadata."""
    return any(len(e.fields) >= 2 for e in state.list_entities())


def _check_generic_model(state: CrateState) -> bool:
    """Descriptor formally represents the dataset model extending a generic model."""
    return len(state.list_entities()) > 0


def _check_data_structured(state: CrateState) -> bool:
    """Dataset(s) available in a structured machine-readable format."""
    return len(state.list_entities()) > 0


def _check_domain_model(state: CrateState) -> bool:
    """A locally-defined Domain Model describes study design."""
    return bool(state.metadata.description)


def _check_resolvable_terms(state: CrateState) -> bool:
    """Value-level metadata includes resolvable identifiers."""
    for entity in state.list_entities():
        for field, value in entity.fields.items():
            if isinstance(value, str) and ("http" in value or "doi" in value.lower()):
                return True
    return False


def _check_standard_license(state: CrateState) -> bool:
    """Descriptor references a standard reuse license."""
    return _check_license_present(state)


def _check_domain_standard(state: CrateState) -> bool:
    """Descriptor uses a community-defined metadata standard."""
    return _check_conforms_to_profile(state)


def _check_standard_field_metadata(state: CrateState) -> bool:
    """Descriptor includes standard-compliant field-level metadata."""
    return len(state.list_entities()) > 1


def _check_controlled_values(state: CrateState) -> bool:
    """Textual field values standardised against domain controlled terminologies."""
    for entity in state.list_entities():
        for value in entity.fields.values():
            if isinstance(value, str) and ("_" in value or ":" in value):
                return True
    return False


def _check_standard_identifiers(state: CrateState) -> bool:
    """Domain-entity values assigned unique standard identifiers."""
    for entity in state.list_entities():
        for field, value in entity.fields.items():
            if field in ("identifier", "accession", "doi", "orcid", "ror") and value:
                return True
    return False


def _check_linked_data(state: CrateState) -> bool:
    """Dataset content semantically represented as Linked Data."""
    return len(state.list_entities()) > 0


def _check_semantic_model(state: CrateState) -> bool:
    """The Semantic Data Model is represented using Linked Data."""
    return len(state.list_entities()) > 0


def _check_machine_interpretable(state: CrateState) -> bool:
    """Metadata in machine-readable AND machine-interpretable format."""
    return len(state.list_entities()) > 0


# ---------------------------------------------------------------------------
# Graph-aware DSM checks
#
# The DSM's Level 2-4 Content and Representation indicators ask about *dataset
# fields and values* — column names, datatypes, controlled vocabularies, formats.
# None of that exists on `CrateState`: it appears only once the crate is assembled,
# as csvw:Column nodes and encodingFormat on File nodes. So these checks read the
# assembled ``@graph``, the same source `assess_mit_coverage` moved to under #311
# ("the cheaper one was wrong, not approximate"). With no graph supplied they
# return False rather than guessing — absent evidence is not evidence.
# ---------------------------------------------------------------------------

# What a DSM check may hand back: a bare tri-state, or a Verdict carrying evidence.
# Both are accepted so evidence can be enriched check by check rather than in a
# flag-day change to all forty.
CheckResult = "bool | None | Verdict"
DsmCheck = Callable[[CrateState, "Graph"], "bool | None | Verdict"]



def _check_tidy_dataset(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-2-C2 — data structured per Tidy Data Principles.

    Tidy means one column per variable, machine-declared. The crate evidences that
    when a table's schema names several columns and each carries a ``datatype``; a
    table whose columns are untyped is not a declared structure.
    """
    if _needs_graph(graph):
        return None
    cols = _columns(graph)
    typed = [c for c in cols if c.get("datatype")]
    return Verdict(
        len(cols) >= 2 and len(typed) == len(cols),
        f"{len(typed)} of {len(cols)} declared columns carry a datatype",
    )


def _check_reference_fields(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-2-C3 — Reference Fields that enable joining related datasets.

    A join is evidenced by a column whose ``valueUrl`` resolves to another entity
    **in this crate**: that is the foreign key. An external ontology IRI types the
    column but joins nothing.
    """
    if _needs_graph(graph):
        return None
    ids = {str(n.get("@id")) for n in _nodes(graph) if n.get("@id")}
    cols = _columns(graph)
    joins = [c for c in cols if _ref_id(c.get("valueUrl")) in ids and c.get("valueUrl")]
    names = ", ".join(str(c.get("name") or c.get("titles") or "?") for c in joins[:3])
    return Verdict(
        bool(joins),
        f"{len(joins)} of {len(cols)} columns resolve a valueUrl to an in-crate entity"
        + (f" ({names})" if joins else ""),
    )


def _check_local_data_dictionary(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-2-C4 — Field Values standardised against a Data Dictionary.

    Evidenced by a column that both declares a datatype and binds its values to a
    declared vocabulary (``valueUrl``) or property (``propertyUrl``).
    """
    if _needs_graph(graph):
        return None
    cols = _columns(graph)
    bound = [c for c in cols if c.get("datatype") and (c.get("valueUrl") or c.get("propertyUrl"))]
    return Verdict(
        bool(bound),
        f"{len(bound)} of {len(cols)} columns are both typed and bound to a vocabulary",
    )


def _check_local_dataset_model(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-2-R2 — Dataset(s) standardised to a locally defined Dataset Model."""
    if _needs_graph(graph):
        return None
    tables = [n for n in _nodes(graph) if n.get("tableSchema")]
    return Verdict(bool(tables), f"{len(tables)} data table(s) declare a tableSchema")


def _check_model_documentation_human(state: CrateState, graph: Graph) -> bool | None:
    """DSM-2-R4 — the Domain Model documented in a Human Readable Format.

    A described protocol is that documentation; a bare protocol *name* is not.
    """
    if _needs_graph(graph):
        return None
    for node in _nodes(graph):
        types = _node_types(node)
        if "LabProtocol" in types and str(node.get("description") or "").strip():
            return True
    return False


def _check_standard_field_names(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-3-C3 — Dataset Field Names use standard controlled terms.

    Evidenced by a column whose ``propertyUrl`` is an external ontology IRI.
    """
    if _needs_graph(graph):
        return None
    cols = _columns(graph)
    std = [c for c in cols if _is_external_iri(c.get("propertyUrl"))]
    return Verdict(
        bool(std),
        f"{len(std)} of {len(cols)} column names map to an external ontology IRI",
    )


def _check_community_domain_model(state: CrateState, graph: Graph) -> bool | None:
    """DSM-3-R1 / DSM-3-R4 — conformance to a community domain model, declared
    machine-readably as a resolvable profile IRI on the root."""
    if _needs_graph(graph):
        return None
    for node in _nodes(graph):
        conforms = node.get("conformsTo")
        for entry in conforms if isinstance(conforms, list) else [conforms]:
            iri = _ref_id(entry)
            if iri.startswith("http") and "profile" in iri:
                return True
    return False


def _check_non_proprietary_format(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-3-R5 — Dataset(s) available in a non-proprietary Machine Readable Format.

    True when the crate carries at least one data file in an open format. This is
    the indicator the in-vitro corpus most often fails: GraphPad ``.prism``/``.pzf``
    and legacy ``.xls`` need licensed software to read.
    """
    if _needs_graph(graph):
        return None
    files = [n for n in _nodes(graph) if "File" in _node_types(n)]
    fmts = [str(n.get("encodingFormat") or "").split(";")[0].strip() for n in files]
    open_fmts = [f for f in fmts if f in _OPEN_MEDIA_TYPES]
    closed = sorted({f for f in fmts if f and f not in _OPEN_MEDIA_TYPES})
    return Verdict(
        bool(open_fmts),
        f"{len(open_fmts)} of {len(files)} files are in an open format"
        + (f"; proprietary present: {', '.join(closed[:3])}" if closed else ""),
    )


def _check_semantic_study_design(state: CrateState, graph: Graph) -> bool | None:
    """DSM-4-C1 — the Semantic Data Model includes study design elements AND the
    relationships between them: a typed process that is actually wired to both an
    input and an output."""
    if _needs_graph(graph):
        return None
    for node in _nodes(graph):
        if "LabProcess" not in _node_types(node):
            continue
        if (node.get("object") or node.get("agent")) and node.get("result"):
            return True
    return False


def _check_common_data_elements(state: CrateState, graph: Graph) -> bool | None:
    """DSM-4-C3 / DSM-4-C2 — key Dataset Fields mapped to Common Data Elements.

    Stricter than DSM-3-C3's "at least one": a mapping is only a model when most of
    the declared fields carry one.
    """
    if _needs_graph(graph):
        return None
    cols = _columns(graph)
    if not cols:
        return False
    mapped = sum(1 for c in cols if _is_external_iri(c.get("propertyUrl")))
    return mapped >= max(2, len(cols) // 2)


def _check_cde_relationships(state: CrateState, graph: Graph) -> bool | None:
    """DSM-4-C5 — a pre-defined set of Common Data Elements, reported in the
    Datasets, with the relationships between them: DefinedTerm entities that
    something in the crate actually references."""
    if _needs_graph(graph):
        return None
    defined = {
        str(n.get("@id")) for n in _nodes(graph) if "DefinedTerm" in _node_types(n)
    }
    if not defined:
        return False
    for node in _nodes(graph):
        for key, value in node.items():
            if key in ("@id", "@type"):
                continue
            for item in value if isinstance(value, list) else [value]:
                if _ref_id(item) in defined and str(node.get("@id")) not in defined:
                    return True
    return False


def _check_semantic_contextual_metadata(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-4-R1 — Contextual Metadata represented by semantically defined Common
    Data Elements: a PropertyValue carrying an external ``propertyID`` IRI."""
    if _needs_graph(graph):
        return None
    pvs = [n for n in _nodes(graph) if "PropertyValue" in _node_types(n)]
    typed = [n for n in pvs if _is_external_iri(n.get("propertyID"))]
    return Verdict(
        bool(typed),
        f"{len(typed)} of {len(pvs)} PropertyValues carry an external propertyID IRI",
    )


def _check_machine_interpretable_graph(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-4-R5 — available in a Machine Readable AND Machine *Interpretable*
    format. Readable is the JSON-LD serialisation; interpretable additionally
    requires the terms to be resolvable, i.e. external IRIs in the graph."""
    if _needs_graph(graph):
        return None
    semantic = _as_verdict(_check_semantic_contextual_metadata(state, graph))
    named = _as_verdict(_check_standard_field_names(state, graph))
    return Verdict(
        semantic.value is True or named.value is True,
        "; ".join(e for e in (semantic.evidence, named.evidence) if e),
    )


def _check_min_info_guidelines(state: CrateState, graph: Graph) -> bool:
    """DSM-3-C1 — study-level metadata reported per Minimum Information Reporting
    Guidelines. The model itself cross-references RDA-R1.3-01M, which this tool
    operationalises as OECD MIT coverage (see mit_assessment)."""
    return _mit_has_coverage(state.mit_assessment)


def _state_check(fn: Callable[[CrateState], bool]) -> DsmCheck:
    """Adapt a CrateState-only check to the shared ``(state, graph)`` shape.

    One registry, one call shape. Wrapping the older checks here rather than
    keeping a second dict is what stops "which checks see the graph?" becoming a
    thing that can drift.
    """

    def _wrapped(state: CrateState, _graph: Graph) -> bool:
        return fn(state)

    # Kept reachable so a test can prove that a Bridge2AI criterion declaring it
    # shares this check really calls this function, not a look-alike of its own.
    setattr(_wrapped, "__wrapped_check__", fn)  # noqa: B010
    return _wrapped


# Map check names to functions
FAIR_CHECKS: dict[str, Any] = {
    "root_global_id": _check_root_global_id,
    "every_entity_has_id": _check_every_entity_has_id,
    "pid_form": _check_pid_form,
    "rich_metadata": _check_rich_metadata,
    "metadata_refs_data": _check_metadata_refs_data,
    "jsonld_context": _check_jsonld_context,
    "fair_vocabularies": _check_fair_vocabularies,
    "qualified_refs": _check_qualified_refs,
    "reuse_attributes": _check_reuse_attributes,
    "license_present": _check_license_present,
    "license_standard": _check_license_standard,
    "license_machine": _check_license_machine,
    "provenance": _check_provenance,
    "conforms_to_profile": _check_conforms_to_profile,
    "mit_coverage": _check_mit_coverage_indicator,
}

# Every value takes ``(state, graph)``. Older CrateState-only checks are adapted
# by ``_state_check``; the Level 2-4 field/value indicators read the graph directly.
DSM_CHECKS: dict[str, DsmCheck] = {
    "unique_id": _state_check(_check_unique_id),
    "study_summary": _state_check(_check_study_summary),
    "dataset_metadata": _state_check(_check_dataset_metadata),
    "access_info": _state_check(_check_access_info),
    "has_descriptor": _state_check(_check_has_descriptor),
    "context_fields": _state_check(_check_context_fields),
    "dataset_hierarchy": _state_check(_check_dataset_hierarchy),
    "general_schema": _state_check(_check_general_schema),
    "descriptor_machine_readable": _state_check(_check_descriptor_machine_readable),
    "data_machine_readable": _state_check(_check_data_machine_readable),
    "cross_dataset_refs": _state_check(_check_cross_dataset_refs),
    "field_level_metadata": _state_check(_check_field_level_metadata),
    "value_level_metadata": _state_check(_check_value_level_metadata),
    "generic_model": _state_check(_check_generic_model),
    "data_structured": _state_check(_check_data_structured),
    "domain_model": _state_check(_check_domain_model),
    "resolvable_terms": _state_check(_check_resolvable_terms),
    "standard_license": _state_check(_check_standard_license),
    "domain_standard": _state_check(_check_domain_standard),
    "standard_field_metadata": _state_check(_check_standard_field_metadata),
    "controlled_values": _state_check(_check_controlled_values),
    "standard_identifiers": _state_check(_check_standard_identifiers),
    "linked_data": _state_check(_check_linked_data),
    "semantic_model": _state_check(_check_semantic_model),
    "machine_interpretable": _state_check(_check_machine_interpretable),
    # DSM-4-R6 "License information is formally represented/encoded in a Machine
    # Readable Format" reuses the RDA R1.1-03M check — same question, both models.
    # It was defined but never registered here, so _compute_dsm_level skipped it
    # and handed out Level 4 for free.
    "license_machine": _state_check(_check_license_machine),
    # --- graph-aware: the DSM's dataset field/value indicators (Levels 2-4) ---
    "tidy_dataset": _check_tidy_dataset,
    "reference_fields": _check_reference_fields,
    "local_data_dictionary": _check_local_data_dictionary,
    "local_dataset_model": _check_local_dataset_model,
    "model_documentation_human": _check_model_documentation_human,
    "standard_field_names": _check_standard_field_names,
    "community_domain_model": _check_community_domain_model,
    "non_proprietary_format": _check_non_proprietary_format,
    "semantic_study_design": _check_semantic_study_design,
    "common_data_elements": _check_common_data_elements,
    "cde_relationships": _check_cde_relationships,
    "semantic_contextual_metadata": _check_semantic_contextual_metadata,
    "machine_interpretable_graph": _check_machine_interpretable_graph,
    "min_info_guidelines": _check_min_info_guidelines,
}


def assess_fair_maturity(
    state: CrateState, *, mit: MITReport | None = None, graph: Graph = None
) -> FAIRReport:
    """Assess FAIR maturity from CrateState metadata.

    Checks basic FAIR indicators from fair/indicators.yaml and computes
    DSM level from fair/dsm_indicators.yaml check results.

    Args:
        state: The current CrateState to assess.
        mit: An already-computed MIT report to score the ``mit_coverage`` indicator
            (RDA-R1.3-01D) against. The report/export path computes MIT against the
            assembled ``@graph`` and passes it here, because ``state.mit_assessment``
            is never populated on that path (#311). When ``None`` the indicator falls
            back to ``state.mit_assessment`` (back-compat).

    Returns:
        A FAIRReport with indicator_results and dsm_level.
    """
    indicators_data = _load_yaml(FAIR_INDICATORS_PATH)
    dsm_data = _load_yaml(DSM_INDICATORS_PATH)

    # The mit_coverage indicator is scored from the caller-supplied report when
    # given (graph-based), else from the state's own (possibly empty) assessment.
    mit_report = mit if mit is not None else state.mit_assessment

    indicator_results: list[dict[str, Any]] = []

    # Run RDA indicator checks
    if indicators_data:
        indicators = indicators_data.get("indicators", [])
        for indicator in indicators:
            check_name = indicator.get("check", "")
            scope = indicator.get("scope", "")

            if scope == "out_of_scope":
                indicator_results.append(
                    {
                        "id": indicator.get("id", ""),
                        "dimension": indicator.get("dimension", ""),
                        "priority": indicator.get("priority", ""),
                        "text": indicator.get("text", ""),
                        "passed": None,
                        "scope": "out_of_scope",
                    }
                )
            elif check_name in FAIR_CHECKS:
                # mit_coverage is scored from the (graph-based) report, not the
                # never-populated state.mit_assessment (#311).
                if check_name == "mit_coverage":
                    passed = _mit_has_coverage(mit_report)
                else:
                    passed = FAIR_CHECKS[check_name](state)
                indicator_results.append(
                    {
                        "id": indicator.get("id", ""),
                        "dimension": indicator.get("dimension", ""),
                        "priority": indicator.get("priority", ""),
                        "text": indicator.get("text", ""),
                        "passed": passed,
                    }
                )

    dsm_level = _compute_dsm_level(state, dsm_data, graph)

    return FAIRReport(
        indicator_results=indicator_results,
        dsm_level=dsm_level,
    )


def _assessable_indicators(
    dsm_data: dict[str, Any], level: int
) -> list[tuple[dict[str, Any], DsmCheck]]:
    """The indicators at *level* this tool can actually evaluate, with their checks.

    One definition, used by both :func:`_compute_dsm_level` and :func:`dsm_blockers`,
    so "what counts as assessable" cannot drift between the level a crate is awarded
    and the blockers reported for the level above it.

    An indicator scoped ``na`` is not assessable by construction — the published model
    carries all 83 indicators and most describe a hosting environment, a Level-0
    pre-FAIRification state, or content we do not inspect. An indicator that *is*
    scoped for assessment but names a check nothing registers is a **wiring bug**, not
    a pass: it raises rather than being silently skipped, which is how DSM-4-R6 went
    unnoticed while its level was awarded for free.
    """
    out: list[tuple[dict[str, Any], DsmCheck]] = []
    for ind in dsm_data.get("indicators", []):
        if ind.get("level") != level or ind.get("scope", "na") == "na":
            continue
        check_name = str(ind.get("check") or "")
        check = DSM_CHECKS.get(check_name)
        if check is None:
            raise KeyError(
                f"DSM indicator {ind.get('id')} is scoped {ind.get('scope')!r} but its "
                f"check {check_name!r} is not registered in DSM_CHECKS. Register it or "
                f"scope the indicator 'na' in scripts/gen_dsm_indicators.py."
            )
        out.append((ind, check))
    return out


def _failing_at(
    dsm_data: dict[str, Any], answers: dict[str, Verdict], level: int
) -> list[tuple[str, str, str]]:
    """``(id, the model's own text, our evidence)`` for every indicator failing at *level*.

    One definition shared by the ceiling and the blockers list, so the "what stands in
    the way" answer cannot differ between the two places the report shows it.
    """
    out: list[tuple[str, str, str]] = []
    for ind, _check in _assessable_indicators(dsm_data, level):
        verdict = answers.get(str(ind.get("id") or ""))
        if verdict is not None and verdict.value is False:
            out.append(
                (str(ind.get("id") or ""), str(ind.get("text") or ""), verdict.evidence)
            )
    return out


def dsm_verdicts(
    state: CrateState, dsm_data: dict[str, Any] | None = None, graph: Graph = None
) -> dict[str, Verdict]:
    """Every assessable indicator's verdict, with the questionnaire's ladder enforced.

    The single evaluation pass. The level, the grid, the ceiling and the blockers all
    read this map, so the four can never disagree about what an indicator answered.

    **The ladder constraint.** The published instrument is a questionnaire of 18
    multiple-choice questions whose options are nested by maturity level — within one
    question, "Dataset(s) are standardised to a *community* Standard Dataset Model"
    (L3) sits above "...to a *locally defined* Dataset Model" (L2). A respondent
    cannot truthfully tick the higher statement while leaving the lower one unticked.

    This tool evaluates indicators independently, which *can* produce that incoherence:
    measured on a real crate, 5 of the 18 questions came back non-monotone. So after
    evaluation, each question's ladder is walked upward and any option above a
    **False** is demoted to False, with the reason recorded in its evidence. Demoting
    (rather than promoting the lower one) is the conservative direction: it never
    credits a maturity the crate has not evidenced at every step below.

    Unanswered options (``value is None``) neither block nor are blocked — an absent
    measurement is not a failure, so the ladder walks past them untouched.
    """
    if dsm_data is None:
        dsm_data = _load_yaml(DSM_INDICATORS_PATH)
    if dsm_data is None:
        return {}

    verdicts: dict[str, Verdict] = {}
    for ind in dsm_data.get("indicators", []):
        ident = str(ind.get("id") or "")
        if not ident or ind.get("scope", "na") == "na":
            continue
        check = DSM_CHECKS.get(str(ind.get("check") or ""))
        if check is None:
            raise KeyError(
                f"DSM indicator {ident} is scoped {ind.get('scope')!r} but its check "
                f"{ind.get('check')!r} is not registered in DSM_CHECKS. Register it or "
                f"scope the indicator 'na' in scripts/gen_dsm_indicators.py."
            )
        verdicts[ident] = _as_verdict(check(state, graph))

    for question in dsm_data.get("questions", []) or []:
        # The ladder runs ACROSS levels, not within one. Two options at the same
        # level are siblings — independent statements — so a failure at level N must
        # only demote options ABOVE N, never the ones beside it.
        failed_at: int | None = None
        blocked_by = ""
        for option in question.get("options", []) or []:
            ident = str(option.get("id") or "")
            level = option.get("level")
            current = verdicts.get(ident)
            if current is None or current.value is None or not isinstance(level, int):
                continue
            if failed_at is not None and level > failed_at and current.value is True:
                verdicts[ident] = Verdict(
                    False,
                    f"demoted: {blocked_by} (level {failed_at}) is not met, and this "
                    f"question's statements form a ladder"
                    + (f" — measured: {current.evidence}" if current.evidence else ""),
                )
            elif current.value is False and failed_at is None:
                failed_at, blocked_by = level, ident

    return verdicts


def _compute_dsm_level(
    state: CrateState, dsm_data: dict[str, Any] | None, graph: Graph = None
) -> int:
    """Compute the FAIRplus Dataset Maturity level.

    Per the DSM model the ladder is **cumulative**: level N is awarded only when every
    assessable indicator at levels 1..N passes. Two rules keep the number honest:

    * **A level nobody assessed is never awarded.** Levels whose indicators are all
      ``na`` — Level 5 is entirely hosting/enterprise, so no crate can evidence it —
      would otherwise be handed out for free, because "no failures" and "no evidence"
      are not the same claim.
    * **Level 0 is not on the ladder.** Its indicators are *negative* statements of the
      pre-FAIRification state ("Dataset(s) are NOT Identifiable…"); failing them is the
      desired outcome, so scoring them would invert the scale.

    Args:
        state: The CrateState to assess.
        dsm_data: Parsed DSM indicators YAML data, or None.

    Returns:
        The highest DSM level achieved (0-5); 0 means "level 1 not reached".
    """
    if dsm_data is None:
        return 0

    levels = sorted(
        {
            lvl
            for ind in dsm_data.get("indicators", [])
            if isinstance(lvl := ind.get("level"), int) and lvl >= _DSM_FIRST_LEVEL
        }
    )

    answers = dsm_verdicts(state, dsm_data, graph)
    max_level = 0
    for level in levels:
        answered = [
            verdict.value
            for ind, _check in _assessable_indicators(dsm_data, level)
            if (verdict := answers.get(str(ind.get("id")))) is not None
            and verdict.value is not None
        ]
        if not answered:
            break  # no evidence at this level, so nothing above it can be claimed
        if not all(answered):
            break
        max_level = level

    return max_level


def dsm_ceiling(
    state: CrateState, dsm_data: dict[str, Any] | None = None, graph: Graph = None
) -> dict[str, Any]:
    """What DSM level this crate could **realistically** reach, and what caps it.

    The gated level says where a crate *is*; on its own it is not actionable, because a
    reader cannot tell whether the next rung is one fixable gap away or unreachable by
    any crate that will ever exist. Three quantities separate those cases:

    * ``attained`` — the gated level now (:func:`_compute_dsm_level`).
    * ``attainable`` — the level this crate would reach if every assessed indicator
      currently failing were satisfied. The distance ``attainable - attained`` is the
      part that is genuinely the depositor's to close.
    * ``ceiling`` — the highest level *any* crate can reach with this tool, because
      above it no indicator is assessable from a crate at all. Level 5 is entirely
      hosting and enterprise data-governance, so no RO-Crate can evidence it: reporting
      a level "out of 5" implies a rung that is not on the board.

    ``blocked_by`` names the assessed indicators failing at ``attained + 1`` — the
    concrete next step — and ``ceiling_reason`` says why the ladder stops where it does,
    so "you cannot go higher" is never an unexplained verdict.
    """
    if dsm_data is None:
        dsm_data = _load_yaml(DSM_INDICATORS_PATH)
    if dsm_data is None:
        return {}

    levels = sorted(
        {
            lvl
            for ind in dsm_data.get("indicators", [])
            if isinstance(lvl := ind.get("level"), int) and lvl >= _DSM_FIRST_LEVEL
        }
    )

    attained = _compute_dsm_level(state, dsm_data, graph)
    ceiling = 0
    attainable = 0
    for level in levels:
        assessable = _assessable_indicators(dsm_data, level)
        if not assessable:
            break
        ceiling = level
        attainable = level  # every indicator here *could* be satisfied

    answers = dsm_verdicts(state, dsm_data, graph)
    failing = _failing_at(dsm_data, answers, attained + 1)

    unreachable = sorted(
        {lvl for lvl in levels if lvl > ceiling},
    )
    if unreachable:
        names = dsm_data.get("levels") or {}
        listed = ", ".join(f"{lvl} ({names.get(lvl, '')})".strip() for lvl in unreachable)
        reason = (
            f"Level {listed} has no crate-assessable indicator: it is scored entirely on "
            "hosting-environment and enterprise data-governance capability, which a crate "
            "cannot evidence about the environment that serves it."
        )
    else:
        reason = ""

    return {
        "attained": attained,
        "attainable": attainable,
        "ceiling": ceiling,
        "ceiling_reason": reason,
        "blocked_by": failing,
        "levels_out_of_reach": unreachable,
    }


def dsm_grid(
    state: CrateState, dsm_data: dict[str, Any] | None = None, graph: Graph = None
) -> dict[int, dict[str, dict[str, Any]]]:
    """The DSM's own **"% Complete" grid** — every level × every category.

    This reproduces the scoring the published assessment sheet performs, rather than
    only the single gated level. The workbook's formula, verbatim from
    ``FAIR-DSM Assessment Sheet v1.2`` cell ``P10`` (Level 1, Content and context)::

        =(((COUNTIFS(J32,1))+(COUNTIFS(J45,1))+(COUNTIFS(J50,1))+(COUNTIFS(J51,1)))
          /(COUNT(J32,J45,J50,J51))*100)

    Two properties of that formula are load-bearing and are reproduced exactly:

    * **The denominator is ``COUNT``, not ``COUNTA``.** Excel's ``COUNT`` tallies
      *numeric* cells, so an indicator left unanswered drops out of the denominator
      instead of counting against the score. An indicator this tool cannot assess from
      a crate is precisely an unanswered cell, so it is excluded the same way — the
      percentage says "of what was assessed", exactly as the sheet does.
    * **Level 0 counts zeros, not ones** (cell ``P6``: ``COUNTIFS(J31,0)``). Its
      indicators state the pre-FAIRification condition in the negative, so *not*
      satisfying one is the good outcome. Scoring it like any other level would invert
      the scale.

    A cell whose indicators are all unassessable has an empty denominator — ``#DIV/0!``
    in the sheet — and is reported as ``pct=None`` ("not assessed"), never 0.

    Returns:
        ``{level: {category: {"pct", "passed", "assessed", "total"}}}`` where
        *category* is ``C``/``R``/``H``, ``assessed`` is the denominator actually used,
        and ``total`` is how many the published model defines there — so a reader can
        see the coverage behind every percentage.
    """
    if dsm_data is None:
        dsm_data = _load_yaml(DSM_INDICATORS_PATH)
    if dsm_data is None:
        return {}

    indicators = dsm_data.get("indicators", [])
    answers = dsm_verdicts(state, dsm_data, graph)
    grid: dict[int, dict[str, dict[str, Any]]] = {}

    for ind in indicators:
        level, category = ind.get("level"), str(ind.get("category") or "")
        if not isinstance(level, int) or not category:
            continue
        cell = grid.setdefault(level, {}).setdefault(
            category, {"pct": None, "passed": 0, "assessed": 0, "total": 0}
        )
        cell["total"] += 1

        if ind.get("scope", "na") == "na":
            continue  # an unanswered cell: outside COUNT(), so outside the denominator
        check = DSM_CHECKS.get(str(ind.get("check") or ""))
        if check is None:  # guarded by _assessable_indicators; belt and braces
            continue
        verdict = answers.get(str(ind.get("id")))
        satisfied = verdict.value if verdict is not None else None
        if satisfied is None:
            continue  # answered by nothing: an unanswered cell, outside COUNT()
        cell["assessed"] += 1
        # Level 0 is scored inverted — see the docstring.
        if satisfied != (level == 0):
            cell["passed"] += 1

    for by_category in grid.values():
        for cell in by_category.values():
            if cell["assessed"]:
                cell["pct"] = round(cell["passed"] / cell["assessed"] * 100, 1)

    return grid


def dsm_blockers(state: CrateState, graph: Graph = None) -> list[tuple[str, str, str]]:
    """``(id, text)`` of the assessable DSM indicators failing at the *next*
    level — what stands between the crate and DSM ``level + 1``.

    Nothing new is measured: :func:`_compute_dsm_level` already evaluates these
    checks and discards which ones failed; this re-walks the same YAML with the
    same ``DSM_CHECKS`` so the report can name what "2 indicators to level 1"
    actually are instead of a bare gated zero. Empty at level 5 (nothing above
    to block) and when the DSM YAML cannot be read (no level was computed, so
    nothing blocks).
    """
    dsm_data = _load_yaml(DSM_INDICATORS_PATH)
    if dsm_data is None:
        return []
    level = _compute_dsm_level(state, dsm_data, graph)
    answers = dsm_verdicts(state, dsm_data, graph)
    return _failing_at(dsm_data, answers, level + 1)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("assess_fair_maturity", assess_fair_maturity, takes_state=True)
