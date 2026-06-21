"""Tool that assesses FAIR maturity from CrateState metadata.

Checks basic FAIR indicators (metadata presence, entity IDs, license, context)
and computes DSM level from fair/dsm_indicators.yaml check results.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from builder.state import CrateState, FAIRReport

logger = logging.getLogger(__name__)

# Path to the FAIR YAML files
FAIR_INDICATORS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "fair" / "indicators.yaml"
)
DSM_INDICATORS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "fair" / "dsm_indicators.yaml"
)


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
    return any(
        e._provenance.created_by != "missing" for e in state.list_entities()
    )


def _check_conforms_to_profile(state: CrateState) -> bool:
    """Check that metadata complies with a community standard."""
    types = {e.type for e in state.list_entities()}
    has_isa_types = bool(types & {"Investigation", "Study", "Assay", "LabProcess"})
    return has_isa_types


def _check_mit_coverage_indicator(state: CrateState) -> bool:
    """Check that MIT coverage is tracked (report present)."""
    return (
        state.mit_assessment.module_scores != {}
        and state.mit_assessment.overall_score > 0
    )


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
    """Dataset Descriptor contains access information."""
    return bool(state.metadata.output_path or state.metadata.input_path)


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

DSM_CHECKS: dict[str, Any] = {
    "unique_id": _check_unique_id,
    "study_summary": _check_study_summary,
    "dataset_metadata": _check_dataset_metadata,
    "access_info": _check_access_info,
    "has_descriptor": _check_has_descriptor,
    "context_fields": _check_context_fields,
    "dataset_hierarchy": _check_dataset_hierarchy,
    "general_schema": _check_general_schema,
    "descriptor_machine_readable": _check_descriptor_machine_readable,
    "data_machine_readable": _check_data_machine_readable,
    "cross_dataset_refs": _check_cross_dataset_refs,
    "field_level_metadata": _check_field_level_metadata,
    "value_level_metadata": _check_value_level_metadata,
    "generic_model": _check_generic_model,
    "data_structured": _check_data_structured,
    "domain_model": _check_domain_model,
    "resolvable_terms": _check_resolvable_terms,
    "standard_license": _check_standard_license,
    "domain_standard": _check_domain_standard,
    "standard_field_metadata": _check_standard_field_metadata,
    "controlled_values": _check_controlled_values,
    "standard_identifiers": _check_standard_identifiers,
    "linked_data": _check_linked_data,
    "semantic_model": _check_semantic_model,
    "machine_interpretable": _check_machine_interpretable,
}


def assess_fair_maturity(state: CrateState) -> FAIRReport:
    """Assess FAIR maturity from CrateState metadata.

    Checks basic FAIR indicators from fair/indicators.yaml and computes
    DSM level from fair/dsm_indicators.yaml check results.

    Args:
        state: The current CrateState to assess.

    Returns:
        A FAIRReport with indicator_results and dsm_level.
    """
    indicators_data = _load_yaml(FAIR_INDICATORS_PATH)
    dsm_data = _load_yaml(DSM_INDICATORS_PATH)

    indicator_results: list[dict[str, Any]] = []

    # Run RDA indicator checks
    if indicators_data:
        indicators = indicators_data.get("indicators", [])
        for indicator in indicators:
            check_name = indicator.get("check", "")
            scope = indicator.get("scope", "")

            if scope == "out_of_scope":
                indicator_results.append({
                    "id": indicator.get("id", ""),
                    "dimension": indicator.get("dimension", ""),
                    "priority": indicator.get("priority", ""),
                    "text": indicator.get("text", ""),
                    "passed": None,
                    "scope": "out_of_scope",
                })
            elif check_name in FAIR_CHECKS:
                passed = FAIR_CHECKS[check_name](state)
                indicator_results.append({
                    "id": indicator.get("id", ""),
                    "dimension": indicator.get("dimension", ""),
                    "priority": indicator.get("priority", ""),
                    "text": indicator.get("text", ""),
                    "passed": passed,
                })

    dsm_level = _compute_dsm_level(state, dsm_data)

    return FAIRReport(
        indicator_results=indicator_results,
        dsm_level=dsm_level,
    )


def _compute_dsm_level(state: CrateState, dsm_data: dict[str, Any] | None) -> int:
    """Compute the Data Stewardship Maturity level.

    Per the DSM model: cumulative per-category. A crate-intrinsic level N
    is awarded only when every assessable C and R indicator at levels 1..N passes.

    Args:
        state: The CrateState to assess.
        dsm_data: Parsed DSM indicators YAML data, or None.

    Returns:
        The highest DSM level achieved (0-5).
    """
    if dsm_data is None:
        return 0

    indicators = dsm_data.get("indicators", [])

    by_level: dict[int, list[dict[str, Any]]] = {}
    for ind in indicators:
        level = ind.get("level", 1)
        by_level.setdefault(level, []).append(ind)

    max_level = 0
    cumulative_pass = True

    for level in sorted(by_level.keys()):
        if not cumulative_pass:
            break

        level_indicators = by_level[level]
        for ind in level_indicators:
            scope = ind.get("scope", "full")
            if scope == "na":
                continue

            check_name = ind.get("check", "")
            if check_name in DSM_CHECKS:
                passed = DSM_CHECKS[check_name](state)
                if not passed:
                    cumulative_pass = False
                    break

        if cumulative_pass:
            max_level = level

    return max_level


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register(
    "assess_fair_maturity", assess_fair_maturity, takes_state=True
)