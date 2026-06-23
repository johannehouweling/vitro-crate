"""LabProcess derivation-chain tools (Issue #88).

The paper's core value proposition is that a receiving lab can trace how an
output was produced:

    Sample →[CellCulture]→ Sample →[Exposure]→ condition_table
           →[EndpointReadout]→ raw_measurements →[DataAnalysis]→ figures

The crate mapping resolves a process's ``object``/``result``/``input``/``output``
references, but those reference keys live behind the schema-less ``hints`` param,
invisible to a weak model — so the chain is never wired and the front half of the
provenance graph dangles. These tools give the agent explicit verbs:

- :func:`draft_file` — create a File data entity (the mapping renders File nodes
  but nothing created one before).
- :func:`link` — add a single provenance edge (``from --relation--> to``), with
  the relation drawn from :data:`PROVENANCE_RELATIONS`.
- :func:`check_provenance` — a **report-only** connectivity lint returning issues
  in #87's routable shape. It never auto-chains (branching assays make a fixed
  process order wrong); it only surfaces what the agent must wire.
"""

from __future__ import annotations

import logging
from typing import Any

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import PROVENANCE_RELATIONS
from builder.tools.drafters import _make_entity_id

logger = logging.getLogger(__name__)

# Reference fields that carry a process's consumed (input) and produced (output)
# edges. Mirrors how _build_process reads them; kept here so the lint and the
# mapping agree on what an input/output edge is.
_INPUT_FIELDS: tuple[str, ...] = ("object", "input", "samples", "cell_line")
_OUTPUT_FIELDS: tuple[str, ...] = ("result", "output")

# Domain process types whose build mapping has NO synthesized output fallback
# (_build_process synthesizes a result for CellCulture and Exposure, but takes
# EndpointReadout/DataAnalysis results only from explicit fields). A missing
# output on these therefore leaves the derivation chain genuinely dangling.
_OUTPUT_REQUIRED_TYPES = frozenset({"EndpointReadout", "DataAnalysis"})


def draft_file(
    state: CrateState,
    name: str,
    path: str | None = None,
    role: str | None = None,
    encoding_format: str | None = None,
) -> Entity:
    """Create a File data entity in the state.

    Args:
        state: The crate state to add the entity to.
        name: Human-readable file name (also used to mint the entity_id).
        path: Crate-relative destination path for the file (``dest_path``). When
            omitted the mapping derives ``data/<name>``.
        role: Optional role label for the file (e.g. "raw_data", "figure").
        encoding_format: Optional IANA media type (schema:encodingFormat).

    Returns:
        The newly created File Entity.
    """
    fields: dict[str, Any] = {"name": name}
    if path:
        fields["dest_path"] = path
    if role:
        fields["role"] = role
    if encoding_format:
        fields["encodingFormat"] = encoding_format
    entity = Entity(
        entity_id=_make_entity_id("file", name, {}),
        type="File",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(fields, source="llm")
    state.add_entity(entity)
    return entity


def link(state: CrateState, from_id: str, relation: str, to_id: str) -> dict[str, str]:
    """Add a single provenance edge ``from_id --relation--> to_id``.

    The ``relation`` MUST be one of :data:`PROVENANCE_RELATIONS`. Both endpoints
    MUST already exist in the state. If the relation already holds a value the
    new target is appended (the edge becomes a list), so a process can take
    several inputs/outputs.

    Args:
        state: The crate state to operate on.
        from_id: entity_id of the source entity (the process or sample).
        relation: One of :data:`PROVENANCE_RELATIONS` (object/result/input/...).
        to_id: entity_id of the target entity.

    Returns:
        A small confirmation dict ``{"from_id", "relation", "to_id"}``.

    Raises:
        ValueError: If ``relation`` is unknown or either endpoint is missing —
            with an actionable, model-readable message.
    """
    if relation not in PROVENANCE_RELATIONS:
        valid = ", ".join(sorted(PROVENANCE_RELATIONS))
        raise ValueError(
            f"Unknown provenance relation {relation!r}. "
            f"Valid relations are: {valid}."
        )
    src = state.get_entity(from_id)
    if src is None:
        raise ValueError(f"link source entity not found: {from_id!r}.")
    if state.get_entity(to_id) is None:
        raise ValueError(f"link target entity not found: {to_id!r}.")

    existing = src.fields.get(relation)
    if existing is None:
        src.fields[relation] = to_id
    elif isinstance(existing, list):
        if to_id not in existing:
            existing.append(to_id)
    elif existing != to_id:
        src.fields[relation] = [existing, to_id]
    src.set_field_status(relation, "filled", "llm")
    logger.debug("Linked %s --%s--> %s", from_id, relation, to_id)
    return {"from_id": from_id, "relation": relation, "to_id": to_id}


def _ref_ids(value: Any) -> set[str]:
    """Normalize a reference value (id, {@id}, or list thereof) to bare ids."""
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    out: set[str] = set()
    for v in items:
        key = v.get("@id") if isinstance(v, dict) else v
        if key:
            out.add(str(key).lstrip("#"))
    return out


def _issue(entity_id: str, prop: str, message: str, fix: str) -> dict[str, Any]:
    """A routable issue in #87's shape (REQUIRED, ISA layer)."""
    return {
        "entity_id": entity_id,
        "property": prop,
        "message": message,
        "fix": fix,
        "severity": "required",
        "profile": "isa",
    }


def check_provenance(state: CrateState) -> dict[str, Any]:
    """Report-only connectivity lint over the derivation chain.

    Surfaces (without modifying state) two classes of break:

    1. A domain LabProcess that produces no output where the build has no
       fallback (EndpointReadout / DataAnalysis) — the chain dangles there.
    2. A File referenced by no process input/output and not part of any
       ``hasPart`` — an orphan data entity with no producer.

    Args:
        state: The crate state to lint.

    Returns:
        ``{"ok": bool, "issues": [issue, ...]}`` where each issue is the #87
        routable shape ``{entity_id, property, message, fix, severity, profile}``
        keyed to the state ``entity_id`` (the id the agent passes to ``link`` /
        the management tools).
    """
    issues: list[dict[str, Any]] = []
    processes = state.list_entities("LabProcess")

    # Collect every entity_id consumed/produced by a process or held in a
    # hasPart, so a File can be checked for a producer / parent.
    referenced: set[str] = set()
    for proc in processes:
        for fld in (*_INPUT_FIELDS, *_OUTPUT_FIELDS):
            referenced |= _ref_ids(proc.fields.get(fld))
    for entity in state.list_entities():
        for fld in ("hasPart", "has_part"):
            referenced |= _ref_ids(entity.fields.get(fld))

    # Rule 1: dangling process output (no build-time fallback for these types).
    for proc in processes:
        ptype = proc.fields.get("process_type") or proc.fields.get("additionalType") or ""
        if ptype in _OUTPUT_REQUIRED_TYPES and not any(
            proc.fields.get(f) for f in _OUTPUT_FIELDS
        ):
            issues.append(
                _issue(
                    proc.entity_id,
                    "result",
                    f"{ptype} '{proc.entity_id}' has no output (result); the "
                    f"derivation chain dangles here.",
                    f"Produce an output and wire it, e.g. draft_file(...) then "
                    f"link('{proc.entity_id}', 'result', '<file_id>').",
                )
            )

    # Rule 2: orphan File (no producing process, not in any hasPart).
    for fe in state.list_entities("File"):
        if fe.entity_id not in referenced:
            issues.append(
                _issue(
                    fe.entity_id,
                    "hasPart",
                    f"File '{fe.entity_id}' is not produced by any process and "
                    f"not part of any dataset (orphan).",
                    f"Wire it as a process output "
                    f"(link('<process_id>', 'result', '{fe.entity_id}')) or add "
                    f"it to a dataset's hasPart.",
                )
            )

    return {"ok": not issues, "issues": issues}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("draft_file", draft_file, takes_state=True)
TOOL_REGISTRY.register("link", link, takes_state=True)
TOOL_REGISTRY.register("check_provenance", check_provenance, takes_state=True)
