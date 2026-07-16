"""Tool that assesses OECD MIT coverage against ``mit/invitro_tox.yaml``.

For each module in the MIT YAML, maps ``crate_slot`` patterns to crate entities
and computes per-module completion scores.

The ``crate_slot`` vocabulary (``Investigation:author``, ``LabProcessExposure:param``,
``CellLineSample:sampleType`` …) describes the **assembled RO-Crate** — schema.org /
RO-Crate properties on nodes discriminated by ``@type`` + ``additionalType`` — not the
intermediate :class:`CrateState`. So when the serialized ``@graph`` is available
(``assess_mit_coverage(state, graph=…)``, as the maturity report passes it), coverage is
scored by matching each slot against the graph nodes (#311). Without a graph, it falls
back to the legacy best-effort match against ``CrateState`` fields — all the domain data
is only synthesized at assembly, so the graph path is the accurate one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from builder.state import CrateState, MITReport

logger = logging.getLogger(__name__)

# Path to the MIT YAML file
MIT_YAML_PATH = Path(__file__).resolve().parent.parent.parent / "mit" / "invitro_tox.yaml"


def _load_mit_yaml() -> dict[str, Any] | None:
    """Load and parse the MIT YAML file.

    Returns:
        Parsed YAML content as a dict, or None if loading fails.
    """
    try:
        import yaml

        with open(MIT_YAML_PATH) as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning("Failed to load MIT YAML from %s: %s", MIT_YAML_PATH, e)
        return None


def _parse_crate_slots(slot_str: str) -> list[tuple[str, str]]:
    """Parse a crate_slot string into a list of (EntityType, field) tuples.

    Crate slots are formatted like "Investigation:name;Study:name;Assay:name"
    or "MolecularEntity:formula;MolecularEntity:smiles".

    Args:
        slot_str: The crate_slot string from the MIT YAML.

    Returns:
        A list of (entity_type, field_name) tuples.
    """
    slots: list[tuple[str, str]] = []
    parts = [p.strip() for p in slot_str.split(";") if p.strip()]
    for part in parts:
        if ":" in part:
            entity_type, field = part.split(":", 1)
            slots.append((entity_type.strip(), field.strip()))
    return slots


# ---------------------------------------------------------------------------
# Graph-based matching (#311) — the crate_slot vocabulary against the serialized
# @graph. See builder/writers/provenance_dag.py for the sibling graph model; here
# we only need node typing + property presence.
# ---------------------------------------------------------------------------

# crate_slot EntityType -> (accepted @type local-names, required additionalType|None).
# ISA backbone Datasets share @type "Dataset" and are told apart by additionalType;
# the LabProcess subtypes and the cell line likewise carry an additionalType string.
_SLOT_TYPE_MATCH: dict[str, tuple[frozenset[str], str | None]] = {
    "Investigation": (frozenset({"Dataset"}), "Investigation"),
    "Study": (frozenset({"Dataset"}), "Study"),
    "Assay": (frozenset({"Dataset"}), "Assay"),
    "MolecularEntity": (frozenset({"MolecularEntity"}), None),
    "CellLineSample": (frozenset({"Sample"}), "CellLine"),
    "LabProcessCellCulture": (frozenset({"LabProcess"}), "CellCulture"),
    "LabProcessExposure": (frozenset({"LabProcess"}), "Exposure"),
    "LabProcessEndpointReadout": (frozenset({"LabProcess"}), "EndpointReadout"),
    "LabProcessDataAnalysis": (frozenset({"LabProcess"}), "DataAnalysis"),
    "LabProtocol": (frozenset({"LabProtocol"}), None),
    "File": (frozenset({"File", "MediaObject"}), None),
}

# crate_slot field -> the actual property key on the assembled node.
_SLOT_FIELD_ALIAS: dict[str, str] = {"param": "parameter"}


def _local(token: str) -> str:
    """Local name of a type/IRI token (last path/hash/CURIE segment)."""
    return token.rsplit("/", 1)[-1].rsplit("#", 1)[-1].rsplit(":", 1)[-1]


def _type_localnames(node: dict[str, Any]) -> set[str]:
    t = node.get("@type")
    return {_local(x) for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}


def _additional_type_of(node: dict[str, Any]) -> str | None:
    v = node.get("additionalType")
    if isinstance(v, dict):
        v = v.get("@id")
    return _local(v) if isinstance(v, str) else None


def _nonempty(value: Any) -> bool:
    """A property counts as filled when it carries a non-empty value (a bare
    ``{"@id": …}`` reference or a list of them counts)."""
    if value is None:
        return False
    if isinstance(value, (str, list, dict)):
        return len(value) > 0
    return True


def _node_matches_slot_type(node: dict[str, Any], entity_type: str) -> bool:
    rule = _SLOT_TYPE_MATCH.get(entity_type)
    if rule is None:
        return False
    bases, add = rule
    if not (bases & _type_localnames(node)):
        return False
    return add is None or _additional_type_of(node) == add


def _iter_property_values(node: dict[str, Any], key: str) -> list[Any]:
    v = node.get(key)
    return v if isinstance(v, list) else [] if v is None else [v]


def _has_characteristic(node: dict[str, Any], index: dict[str, dict[str, Any]]) -> bool:
    """True if *node* carries an ISA Characteristic — realized only as an
    ``additionalProperty`` PropertyValue with ``additionalType == CharacteristicValue``
    (built for CellLineSample from organ/tissue/passage/growth)."""
    for item in _iter_property_values(node, "additionalProperty"):
        target = index.get(item["@id"]) if isinstance(item, dict) and "@id" in item else item
        if isinstance(target, dict) and _additional_type_of(target) == "CharacteristicValue":
            return True
    return False


def _graph_slot_filled(
    entity_type: str,
    field: str,
    nodes: list[dict[str, Any]],
    index: dict[str, dict[str, Any]],
) -> bool:
    """True if any assembled node of *entity_type* has *field* filled.

    ``param`` aliases to the ``parameter`` key; ``char`` is a characteristic
    traversal; everything else is a direct property-key presence check.
    ``conditionsOfAccess`` is deliberately *not* aliased to the always-present
    default ``license`` — crediting it would be a trivial false pass.
    """
    matched = [n for n in nodes if _node_matches_slot_type(n, entity_type)]
    if not matched:
        return False
    if field == "char":
        return any(_has_characteristic(n, index) for n in matched)
    key = _SLOT_FIELD_ALIAS.get(field, field)
    return any(_nonempty(n.get(key)) for n in matched)


def _assemble_graph(state: CrateState) -> list[dict[str, Any]]:
    """Serialize *state* into an RO-Crate ``@graph`` in memory (no disk write)."""
    import tempfile

    from rocrate.rocrate import ROCrate

    from builder.tools._crate_mapping import populate_crate
    from profiles.context import ISA_TOX_CONTEXT

    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    with tempfile.TemporaryDirectory() as tmp:
        populate_crate(state, crate, Path(tmp), materialize_payload=False)
        graph = crate.metadata.generate().get("@graph", [])
    return [n for n in graph if isinstance(n, dict) and "@id" in n]


# ---------------------------------------------------------------------------
# Legacy CrateState field matching (fallback when no @graph is available)
# ---------------------------------------------------------------------------
def _count_filled_fields(state: CrateState) -> dict[tuple[str, str], bool]:
    """Count filled/verified fields across all entities in state.

    Returns a dict keyed by (entity_type, field_name) -> True if filled/verified.
    """
    filled: dict[tuple[str, str], bool] = {}
    for entity in state.list_entities():
        for field_name in entity.fields:
            fc = entity.get_field_status(field_name)
            if fc is not None and fc.status in ("filled", "verified"):
                filled[(entity.type, field_name)] = True
    return filled


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _unique_module_params(module: dict[str, Any]) -> list[dict[str, Any]]:
    """The module's parameters (sections + top-level), deduplicated by id."""
    all_params: list[dict[str, Any]] = []
    for section in module.get("sections", []):
        all_params.extend(section.get("parameters", []))
    all_params.extend(module.get("parameters", []))

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for param in all_params:
        pid = param.get("id", "")
        if pid and pid not in seen:
            seen.add(pid)
            unique.append(param)
    return unique


def _score_modules(
    mit_data: dict[str, Any],
    slot_filled: Callable[[str, str], bool],
) -> MITReport:
    """Roll the MIT YAML into per-module scores using *slot_filled* to decide,
    for each ``(EntityType, field)`` crate_slot, whether it is covered."""
    module_scores: dict[str, dict[str, int]] = {}
    total_completed = 0
    total_required = 0

    for module in mit_data.get("modules", []):
        module_name = module.get("name", module.get("id", "unknown"))
        module_completed = 0
        module_total = 0

        for param in _unique_module_params(module):
            slots = _parse_crate_slots(param.get("crate_slot", ""))
            if not slots:
                continue
            module_total += 1
            if any(slot_filled(et, field) for et, field in slots):
                module_completed += 1

        if module_total > 0:
            module_scores[module_name] = {
                "completed": module_completed,
                "total": module_total,
            }
            total_completed += module_completed
            total_required += module_total

    overall_score = total_completed / total_required if total_required > 0 else 0.0
    return MITReport(module_scores=module_scores, overall_score=overall_score)


def assess_mit_coverage(
    state: CrateState,
    *,
    graph: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> MITReport:
    """Assess OECD MIT coverage of *state* against the MIT YAML checklist.

    Args:
        state: The current CrateState.
        graph: The assembled crate ``@graph`` (or the full metadata document).
            When provided, each ``crate_slot`` is matched against the serialized
            nodes — the accurate path, since the MIT vocabulary describes the
            assembled crate. When ``None``, falls back to a best-effort match
            against ``CrateState`` fields (used by callers that don't hold an
            assembled crate, e.g. the in-loop score floor).

    Returns:
        An MITReport with per-module scores and an overall score.
    """
    mit_data = _load_mit_yaml()
    if mit_data is None:
        return MITReport(module_scores={}, overall_score=0.0)

    if graph is not None:
        nodes = graph.get("@graph", []) if isinstance(graph, dict) else graph
        nodes = [n for n in nodes if isinstance(n, dict) and "@id" in n]
        index = {n["@id"]: n for n in nodes}

        def slot_filled(entity_type: str, field: str) -> bool:
            return _graph_slot_filled(entity_type, field, nodes, index)
    else:
        filled_fields = _count_filled_fields(state)

        def slot_filled(entity_type: str, field: str) -> bool:
            return (entity_type, field) in filled_fields

    return _score_modules(mit_data, slot_filled)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("assess_mit_coverage", assess_mit_coverage, takes_state=True)
