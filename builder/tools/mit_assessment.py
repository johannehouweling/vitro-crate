"""Tool that assesses OECD MIT coverage against ``mit/invitro_tox.yaml``.

For each module in the MIT YAML, maps ``crate_slot`` patterns to crate entities
and computes per-module completion scores.

The ``crate_slot`` vocabulary (``Investigation:author``, ``LabProcessExposure:param``,
``CellLineSample:sampleType`` …) describes the **assembled RO-Crate** — schema.org /
RO-Crate properties on nodes discriminated by ``@type`` + ``additionalType`` — not the
intermediate :class:`CrateState`. All the domain data is only synthesized at assembly,
so the assembled ``@graph`` is the only document the checklist can honestly be scored
against.

There is therefore exactly ONE scoring path (#311): :func:`assess_mit_coverage` matches
slots against graph nodes, and assembles the graph itself when the caller has none
instead of falling back to a second, weaker matcher. The fallback it replaces scanned
``CrateState`` fields and scored 0.0 for every real crate — the slot names it looked for
(``char``, ``keyEvent``, ``taxonomicRange``) simply do not exist before assembly — and
that 0.0 was indistinguishable from "this crate covers nothing", so the maturity report
rendered "MIT coverage 0%" as a confident false statement about a crate nobody had
measured.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from builder.state import CrateState, MITReport

logger = logging.getLogger(__name__)

# Path to the MIT YAML file. THE one constant (#357): `gap_analysis` imports it
# rather than deriving its own, so the two readers can never point at different
# checklists.
MIT_YAML_PATH = Path(__file__).resolve().parent.parent.parent / "mit" / "invitro_tox.yaml"


def load_mit_yaml() -> dict[str, Any] | None:
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


def parse_crate_slots(slot_str: str) -> list[tuple[str, str]]:
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

# Values the BUILD synthesizes when the user supplied nothing. Crediting one
# would be a false pass — and, since the gap engine shares this matcher (#377),
# would silently stop the loop asking the user for the real value. Same class as
# the deliberate refusal to alias `conditionsOfAccess` to the default `license`.
def _placeholder_values() -> frozenset[str]:
    """The synthesized values, read from the constants the BUILD actually uses.

    Imported rather than duplicated as string literals: a hard-coded copy would
    drift the moment a default is reworded, and silently start crediting it
    again. Lazy + cached because ``builder.tools.builder`` pulls in ro-crate-py.
    """
    global _PLACEHOLDER_CACHE
    if _PLACEHOLDER_CACHE is None:
        from builder.tools.builder import (
            _DEFAULT_ROOT_NAME,
            _PLACEHOLDER_ROOT_DESCRIPTION,
            _PLACEHOLDER_ROOT_NAME,
        )

        _PLACEHOLDER_CACHE = frozenset(
            v.strip().lower()
            for v in (
                _PLACEHOLDER_ROOT_NAME,
                _DEFAULT_ROOT_NAME,
                _PLACEHOLDER_ROOT_DESCRIPTION,
                # _crate_mapping's default root license; there is no constant for
                # it, and the module already refuses to credit `conditionsOfAccess`
                # from it for the same reason.
                "ALL RIGHTS RESERVED BY THE AUTHORS",
            )
        )
    return _PLACEHOLDER_CACHE


_PLACEHOLDER_CACHE: frozenset[str] | None = None


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
    """A property counts as filled when it carries a real, non-placeholder value.

    A bare ``{"@id": …}`` reference or a list of them counts. A value the BUILD
    synthesized in the user's absence does not — see :data:`_PLACEHOLDER_VALUES`.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value) and value.strip().lower() not in _placeholder_values()
    if isinstance(value, (list, dict)):
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
    """Serialize *state* into an RO-Crate ``@graph`` in memory (no disk write).

    Goes through :func:`builder.tools.builder.assemble_crate` — the one assembly
    path — with the same arguments :func:`builder.tools.validation._assemble_and_validate`
    uses. Reaching past it to ``populate_crate`` is not equivalent, and both
    differences changed the score (#377): it skipped ``_apply_root_name`` (#272),
    leaving the root at the literal placeholder name that :func:`_placeholder_values`
    then refuses, and it took ``include_all_scanned``'s default of True, crediting
    ``File:encodingFormat`` off scanned files the gap engine's document omits. One
    matcher over two differently-assembled documents is still two answers.
    """
    from builder.tools.builder import assemble_crate

    crate = assemble_crate(
        state,
        output_dir=None,
        materialize_payload=False,
        include_all_scanned=False,
    )
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
def unique_module_params(module: dict[str, Any]) -> list[dict[str, Any]]:
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


def iter_scorable_params(
    mit_data: dict[str, Any],
) -> Iterator[tuple[dict[str, Any], dict[str, Any], list[tuple[str, str]]]]:
    """Yield ``(module, param, slots)`` for every scorable MIT parameter.

    THE single traversal of the checklist (#357): the walk over
    ``sections`` + top-level parameters, the dedup by id, and the skip rule all
    live here, so the scorer (:func:`_score_modules`) and the gap engine
    (``gap_analysis._mit_gaps``) cannot drift into disagreeing about which
    parameters exist or how many there are.

    A parameter is *scorable* when its ``crate_slot`` parses to at least one
    ``(EntityType, field)`` pair. A parameter with no parseable slot has nothing
    the matcher could ever match, so counting it in the denominator would mark it
    permanently uncoverable and silently depress every score. The two callers
    previously disagreed on exactly this: the scorer skipped such a parameter,
    the gap engine counted it. No parameter in the shipped checklist triggers it
    today, which is precisely why it was worth removing before one did.
    """
    for module in mit_data.get("modules", []):
        for param in unique_module_params(module):
            slots = parse_crate_slots(param.get("crate_slot", ""))
            if not slots:
                continue
            yield module, param, slots


def _score_modules(
    mit_data: dict[str, Any],
    slot_filled: Callable[[str, str], bool],
) -> MITReport:
    """Roll the MIT YAML into per-module scores using *slot_filled* to decide,
    for each ``(EntityType, field)`` crate_slot, whether it is covered."""
    module_scores: dict[str, dict[str, int]] = {}
    total_completed = 0
    total_required = 0

    # A module with no scorable parameter never gets a row, as before: it is
    # created on first yield, so an all-unscorable module is simply never keyed.
    for module, _param, slots in iter_scorable_params(mit_data):
        module_name = module.get("name", module.get("id", "unknown"))
        bucket = module_scores.setdefault(module_name, {"completed": 0, "total": 0})
        bucket["total"] += 1
        total_required += 1
        if any(slot_filled(et, field) for et, field in slots):
            bucket["completed"] += 1
            total_completed += 1

    overall_score = total_completed / total_required if total_required > 0 else 0.0
    return MITReport(module_scores=module_scores, overall_score=overall_score)


def assess_mit_coverage(
    state: CrateState,
    *,
    graph: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> MITReport:
    """Assess OECD MIT coverage of *state* against the MIT YAML checklist.

    **THE one scoring owner** (#311): coverage is always measured against an
    assembled ``@graph``, never against ``CrateState`` fields. A caller that
    already holds the document passes it (the export path does, through
    ``build_maturity_html``); a caller that does not gets it assembled here by
    :func:`_assemble_graph`. Two scorers meant two answers for the same crate —
    0.0 from the state path against 0.148 from the graph path for the golden
    fixture — and the cheaper one was wrong, not approximate.

    Args:
        state: The current CrateState.
        graph: The assembled crate ``@graph`` (or the full metadata document),
            when the caller already has one. ``None`` assembles it — same
            document, one extra in-memory assembly (no disk, no network, no
            SHACL).

    Returns:
        An MITReport with per-module scores and an overall score, or an
        *unassessed* report (empty ``module_scores``, see
        :func:`mit_was_assessed`) when coverage could not be measured at all:
        the checklist did not load, or the crate did not assemble. Callers must
        render that as "not assessed" — its 0.0 is the absence of a measurement,
        not a measured zero.
    """
    mit_data = load_mit_yaml()
    if mit_data is None:
        return MITReport()

    graph = scoring_graph(state, graph)
    if graph is None:
        # A state that will not assemble cannot be scored against anything.
        # Returning a 0.0 *score* here would put "MIT coverage 0%" on the report
        # for a crate that was never looked at; an unassessed report makes every
        # consumer say so instead.
        return MITReport()

    return _score_modules(mit_data, slot_matcher(state, graph=graph))


def scoring_graph(state: CrateState, graph: Any | None = None) -> Any | None:
    """The document to score *state* against: the caller's, else one built here.

    The single place either MIT reader turns a "no graph" into something scorable
    (#311), so the scorer and the gap engine cannot answer the same question two
    ways. They used to agree on ``graph=None`` only because both were equally
    wrong — the ``crate_slot`` vocabulary describes assembled nodes, so the
    ``CrateState`` fallback scored 0.0 for every real crate. Teaching only the
    scorer to assemble would have replaced that shared wrong answer with a
    disagreement, which is worse: two numbers for one crate, and no way to tell
    which the report meant.

    Returns ``None`` when the crate will not assemble. Callers decide what that
    means for them — the scorer declines to state a number, the gap engine still
    surfaces what it can from the degraded field match — but neither invents an
    assembly that failed.
    """
    if graph is not None:
        return graph
    try:
        return _assemble_graph(state)
    except Exception as exc:  # noqa: BLE001 — any assembly failure, same answer
        # Never raise: the callers are a pure report writer that must still render
        # its other axes, and a gap engine the guidance loop runs every round.
        logger.warning("MIT coverage not assessed — crate assembly failed: %s", exc)
        return None


def mit_was_assessed(report: MITReport) -> bool:
    """Whether *report* carries a real measurement of a crate.

    A report with no module scores was never scored — the checklist did not load,
    the crate did not assemble, or this is the untouched :class:`MITReport`
    default on a state nobody has assessed. Its ``overall_score`` is 0.0 by
    construction, which is exactly why it must not be rendered as "0% covered":
    that phrasing is a claim about a crate, and no crate was examined. The same
    distinction the dashboard's MIT tile already draws, and the same one the
    maturity report draws for an unevaluated SHACL severity tier (#446).
    """
    return bool(report.module_scores)


def graph_nodes(graph: Any) -> list[dict[str, Any]]:
    """The addressable nodes of an assembled crate document.

    Accepts either the whole ``{"@context":…, "@graph":[…]}`` document or a bare
    node list, so callers can pass whichever they are holding.
    """
    nodes = graph.get("@graph", []) if isinstance(graph, dict) else (graph or [])
    return [n for n in nodes if isinstance(n, dict) and "@id" in n]


def slot_matcher(
    state: CrateState, *, graph: Any | None = None
) -> Callable[[str, str], bool]:
    """The single ``(crate_slot EntityType, field) -> filled?`` predicate.

    **This is the one matcher for the ``crate_slot`` vocabulary** (#377). That
    vocabulary describes the *assembled* crate — schema.org properties on nodes
    discriminated by ``@type`` + ``additionalType`` — not the intermediate
    ``CrateState``, so the graph branch is the accurate one. It resolves three
    things a ``CrateState`` field scan structurally cannot:

    * a slot type that is not an ``EntityType`` at all (``LabProcessExposure``
      and its three siblings are ``LabProcess`` + an ``additionalType``);
    * the ``char`` slot, realized only as an ``additionalProperty``
      ``CharacteristicValue`` at assembly;
    * a field the assembly *promotes* — a ``MolecularEntity``'s ``cas`` becomes
      the node's ``identifier`` PropertyValue, and a ``CellLineSample``'s
      ``accession`` becomes its ``identifier``.

    Exposed publicly so the gap engine consumes it instead of keeping a second,
    un-migrated copy — the two disagreeing is what made the pipeline ask for
    identifiers the crate already carried.

    With ``graph=None`` it degrades to the legacy ``CrateState`` field match.
    That branch is **not a scoring path** (#311): for the three reasons above it
    credits almost nothing on a real crate, so :func:`assess_mit_coverage`
    assembles a graph rather than call it. It survives for the gap engine's one
    genuinely degraded case — a state whose SHACL assembly already failed, where
    re-assembling here would only fail the same way — and what it returns there
    is a best-effort guess at which questions to ask, never a coverage figure
    anyone reports.
    """
    if graph is not None:
        nodes = graph_nodes(graph)
        index = {n["@id"]: n for n in nodes}

        def _graph_match(entity_type: str, field: str) -> bool:
            return _graph_slot_filled(entity_type, field, nodes, index)

        return _graph_match

    filled_fields = _count_filled_fields(state)

    def _state_match(entity_type: str, field: str) -> bool:
        return (entity_type, field) in filled_fields

    return _state_match


def slot_type_present(entity_type: str, nodes: list[dict[str, Any]]) -> bool:
    """Whether any assembled node matches ``entity_type``'s slot-type rule.

    The graph-aware counterpart of "is there an instance of this type?" — the
    check that decides whether an MIT parameter gets a per-field question or the
    #257 creation prompt. A ``CrateState`` type scan answers ``False`` for every
    ``LabProcess*`` subtype (they are not ``EntityType`` members), so a fully
    parameterised Exposure still produced "No LabProcessExposure recorded yet".
    """
    return any(_node_matches_slot_type(n, entity_type) for n in nodes)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

# Registered bare. A wrapper (`_assess_mit_coverage_tool`) used to assemble the
# graph before delegating, because the bare function scored the legacy
# ``CrateState`` path when called with no graph (#377); it also carried an
# ``assemble=False`` opt-out for a cheap in-loop score floor that no caller ever
# used. Now that :func:`assess_mit_coverage` assembles for itself, the wrapper had
# nothing left to add, and removing it means the tool, the maturity report and the
# gap engine cannot report three different numbers for one crate.
TOOL_REGISTRY.register("assess_mit_coverage", assess_mit_coverage, takes_state=True)
