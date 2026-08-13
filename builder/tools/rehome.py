"""Move a field written on the wrong entity to the one that consumes it.

A build reported this at exit, twelve times over three assays::

    · Dropped 'detection_instrument' from assay_deiodinase_activity_assay
      (not a term in the crate's JSON-LD context)
    · Dropped 'instrument_manufacturer' from assay_deiodinase_activity_assay …
    · Dropped 'measured_entity' from assay_deiodinase_activity_assay …
    · Dropped 'technical_replicate' from assay_deiodinase_activity_assay …

The drop rule itself is sound: a JSON-LD key the context does not define fails
BASE conformance in a way nobody can repair by editing the crate, because the
invalid key is regenerated from state on every build. What was wrong is that it
treated two different things as one — a key the model INVENTED, and a field this
codebase asks for by name.

All four above are documented inputs. ``ENTITY_DRAFT_SCHEMA`` declares them on
``LabProcess`` and ``_build_process`` consumes them into
``LabProcessEndpointReadout``; ``tools_spec`` instructs the agent to fill them
from the assay metadata workbook. They were written on the **Assay** instead,
which declares only ``name`` / ``description`` / ``identifier``, so nothing read
them and they were deleted.

They were then reported as missing. "Say which measurement technique was used
for Deiodinase activity assay" is a finding about information the crate had been
given and had thrown away.

**Nothing here decides which entity owns a field.** ``ENTITY_DRAFT_SCHEMA`` has
always declared that, per type, and this inverts it. A field added to a schema
teaches this module about it for free, and the two cannot drift apart, which they
would if the ownership were restated here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from builder.state import CrateState, Entity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rehomed:
    """One field moved, recorded so the move can be reported rather than guessed at.

    A silent move is a worse failure than the silent drop it replaces: a value
    that quietly changes entity is a value the reader cannot account for.
    """

    field: str
    value: Any
    from_id: str
    from_type: str
    to_id: str
    to_type: str


def _field_owners() -> dict[str, set[str]]:
    """``field -> {entity types that declare it}``, inverted from the schema.

    Read fresh rather than cached at import: the schema is a module-level dict
    that tests legitimately patch, and a cache would serve them the wrong answer.
    """
    from builder.tools._crate_mapping import ENTITY_DRAFT_SCHEMA

    owners: dict[str, set[str]] = {}
    for entity_type, schema in ENTITY_DRAFT_SCHEMA.items():
        for field in getattr(schema, "scalar_fields", {}) or {}:
            owners.setdefault(field, set()).add(entity_type)
    return owners


def _declares(entity_type: str, field: str) -> bool:
    """Whether *entity_type*'s own schema declares *field*."""
    from builder.tools._crate_mapping import ENTITY_DRAFT_SCHEMA

    schema = ENTITY_DRAFT_SCHEMA.get(entity_type)
    return bool(schema and field in (getattr(schema, "scalar_fields", {}) or {}))


def _would_be_dropped(field: str) -> bool:
    """Whether the build would delete *field* rather than emit it.

    Delegates to `_crate_mapping.field_would_be_dropped`, which lives beside the
    code that does the dropping. A rescuer working from its own copy of the rule
    would either move fields that were never in danger or miss the ones that
    were — so there is exactly one copy, and this is not it.
    """
    from builder.tools._crate_mapping import field_would_be_dropped

    return field_would_be_dropped(field)


def _target_for(
    state: CrateState, source: Entity, owners: set[str], wanted_step: str | None
) -> Entity | None:
    """The entity that should hold this field, or ``None`` if there isn't one.

    Only relationships the graph already records are followed — this invents no
    entity to receive a value. An assay's processes are found by the ``assay_id``
    the drafter stamps on every ``LabProcess``; the ``process_type`` is not
    guessed, it comes from the schema description, which names the step it
    belongs to ("EndpointReadout: detection instrument.").

    Returns ``None`` when the owning type has no instance related to *source*,
    which is the honest answer: the value stays where it is and the caller
    decides what to do about it, rather than being attached to an unrelated
    entity of the right class.
    """
    if "LabProcess" not in owners:
        return None
    if source.type != "Assay":
        return None
    candidates = [
        e
        for e in state.list_entities("LabProcess")
        if str(e.fields.get("assay_id") or "") == source.entity_id
    ]
    if wanted_step is not None:
        # Select BY step rather than taking the first process and checking it
        # afterwards: the chain is CellCulture -> Exposure -> EndpointReadout ->
        # DataAnalysis, so "the first one" is almost never the one that consumes
        # an EndpointReadout field, and checking after selecting would reject the
        # move on a chain that has a perfectly good home for it.
        candidates = [
            e for e in candidates if str(e.fields.get("process_type") or "") == wanted_step
        ]
    if not candidates:
        return None
    return candidates[0]


def _process_type_for(field: str) -> str | None:
    """The process step a LabProcess field belongs to, per its schema description.

    ``ENTITY_DRAFT_SCHEMA["LabProcess"]`` documents each field as
    ``"EndpointReadout: detection instrument."`` — the step is already written
    down beside the field, so it is read rather than restated here.
    """
    from builder.tools._crate_mapping import ENTITY_DRAFT_SCHEMA
    from builder.tools.drafters import VALID_PROCESS_TYPES

    schema = ENTITY_DRAFT_SCHEMA.get("LabProcess")
    description = str((getattr(schema, "scalar_fields", {}) or {}).get(field) or "")
    head = description.split(":", 1)[0].strip()
    return head if head in VALID_PROCESS_TYPES else None


def rehome_misplaced_fields(state: CrateState) -> list[Rehomed]:
    """Move every field written on an entity that cannot hold it to one that can.

    Runs before the build composes nodes, so the graph is already correct by the
    time ``_scalar_props`` decides what to emit — rather than trying to rescue a
    value from inside a function that only ever holds one entity and has nowhere
    to put it.

    Conservative on every axis. A field is moved only when ALL of these hold:

    * the build would otherwise DELETE it (``_would_be_dropped``) — a field that
      round-trips fine is never touched, whatever schema it appears in;
    * the entity holding it does NOT declare it, and exactly one other type does
      — an ambiguous field is left alone rather than sent somewhere arguable;
    * a related entity of that type already exists, found through a link the
      graph records;
    * the target does not already have a value for that field, which would make
      this a silent overwrite of something written deliberately.

    Returns what it moved, so the caller can say so. Never raises: a rescue that
    breaks the build is worse than the deletion it was preventing.
    """
    moves: list[Rehomed] = []
    owners = _field_owners()

    for source in list(state.list_entities()):
        for field in list(source.fields):
            value = source.fields.get(field)
            if value in (None, "", [], {}):
                continue
            if _declares(source.type, field) or not _would_be_dropped(field):
                continue
            holders = owners.get(field) or set()
            if len(holders) != 1:
                continue
            # The step the field belongs to is resolved BEFORE the target is
            # picked, so the search is for the process that consumes this field
            # rather than for any process at all.
            target = _target_for(state, source, holders, _process_type_for(field))
            if target is None:
                continue
            if target.fields.get(field) not in (None, "", [], {}):
                continue

            # Carry the ORIGINAL provenance across. Moving a value does not
            # change who supplied it — stamping it "rehomed" would erase the fact
            # that a model wrote it, which is exactly what a D5 audit reads
            # `_completion` to find out. Falls back to "llm" only when the source
            # entity recorded nothing, which is the conservative answer: an
            # unattributed value is treated as model-supplied, not as verified.
            existing = source.get_field_status(field)
            target.fields[field] = value
            target.set_field_status(
                field, "filled", existing.source if existing else "llm"
            )
            del source.fields[field]
            moves.append(
                Rehomed(
                    field=field,
                    value=value,
                    from_id=source.entity_id,
                    from_type=source.type,
                    to_id=target.entity_id,
                    to_type=target.type,
                )
            )
            logger.info(
                "Moved %r from %s (%s) to %s (%s) — the type that consumes it",
                field,
                source.entity_id,
                source.type,
                target.entity_id,
                target.type,
            )

    return moves
