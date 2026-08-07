"""Entity management tools for the ISA-Tox RO-Crate Builder.

Provides CRUD operations on entities within a CrateState, including
field-level completion tracking.
"""

from __future__ import annotations

import logging
from typing import Any

from builder.state import (
    CompletionSource,
    CrateState,
    Entity,
    FileClassification,
)
from builder.tools._crate_mapping import _REF_FIELDS
from builder.tools.field_kinds import is_reference_field, is_resolvable_reference

logger = logging.getLogger(__name__)


# Reference properties the build resolves as COLLECTIONS (via ``_resolve_many`` /
# ``_wire_references``). For these a bare value and a one-item list say the same
# thing, so both are stored as a list — otherwise "assay_y" and ["assay_y"] are
# two different states and a model alternating between them looks like progress
# forever (observed: a string/list flip every ~6s for a full minute).
#
# Deliberately an allow-list: the single-valued references (``labprotocol``,
# ``cell_line``, ``study_id``, ``measurementMethod``, …) go through
# ``_wire_reference``, which resolves ONE value — wrapping those in a list would
# silently break them. An unrecognised reference field keeps its shape.
_COLLECTION_REF_FIELDS: frozenset[str] = frozenset(
    {
        "hasPart",
        "has_part",
        "studies",
        "assays",
        "resources",
        "dataFiles",
        "author",
        "mentions",
        "chemicals",
        "cell_lines",
        "biological_models",
        "biologicalModels",
        "samples",
        "result",
        "output",
        "input",
        "object",
        "about",
        "funder",
        "additionalProperty",
    }
)


def _normalize_reference_one(state: CrateState, value: Any) -> str | None:
    """One reference value reduced to its bare entity id (or a kept-verbatim IRI).

    Accepts every encoding a caller might reach for — ``"assay_x"``,
    ``{"@id": "assay_x"}``, ``"#assay_x"``, ``"./#Assay_assay_x"`` — because the
    build resolves all of them. An external IRI is kept as-is; an unrecognised
    string is kept verbatim rather than dropped, so nothing is silently lost.
    """
    key = value.get("@id") if isinstance(value, dict) else value
    if not isinstance(key, str):
        return None
    key = key.strip()
    if not key:
        return None
    if "://" in key:
        return key  # external identifier — already canonical
    bare = key.lstrip("./").lstrip("#")
    if state.get_entity(bare) is not None:
        return bare
    # Crate-style type-qualified id, e.g. "Assay_assay_x" -> "assay_x".
    if "_" in bare:
        candidate = bare.split("_", 1)[1]
        if state.get_entity(candidate) is not None:
            return candidate
    return key


def _normalize_reference_value(
    state: CrateState, entity_id: str, value: Any, field: str = ""
) -> Any:
    """Canonicalise a reference-valued field before it is stored.

    The same fact can be written six ways — bare id, ``{"@id": …}``, a
    ``./#Type_id`` crate id, each of those alone or in a list — and every one of
    them used to be stored verbatim. A model that cannot tell which encoding the
    validator wants then cycles through them, and because each write genuinely
    changes the stored value, nothing downstream recognises it as going in
    circles: one observed session wrote the same Study→Assay link 29 times in
    six encodings, ~1.7M input tokens.

    Collapsing to one canonical form makes the second write of the same fact a
    true no-op, which the loop's no-op guard already catches. Self-references
    and duplicates are dropped — a Study listing itself under ``hasPart`` is
    never meaningful. List-ness is preserved: some reference properties are
    single-valued in the build and wrapping them in a list would break them.
    """
    if value is None or value == "":
        return value
    is_list = isinstance(value, (list, tuple))
    items = list(value) if is_list else [value]
    out: list[str] = []
    for item in items:
        norm = _normalize_reference_one(state, item)
        if norm is None or norm == entity_id or norm in out:
            continue
        out.append(norm)
    if field in _COLLECTION_REF_FIELDS:
        return out  # a collection property is always stored as a list
    if is_list:
        return out
    return out[0] if out else None


# Reference fields whose members are PropertyValues, so an inline
# ``{"name": …, "value": …}`` can be turned into a real entity instead of dropped.
_PROPERTY_VALUE_REF_FIELDS: frozenset[str] = frozenset({"additionalProperty"})


def _materialize_property_values(state: CrateState, field: str, value: Any) -> Any:
    """Turn inline ``{"name": …, "value": …}`` members into PropertyValue entities.

    A PropertyValue written inline is the most natural encoding there is — it is
    exactly how the thing appears in the finished JSON-LD, and it is what the tox
    profile's own message ("MUST have at least one schema:additionalProperty
    (e.g. Computational Tool …)") suggests. But ``additionalProperty`` is a
    reference-only field, so an inline dict resolved to nothing and the whole
    list was stored as ``[]``: the value vanished, the field was marked filled,
    and the caller was told the write succeeded. Creating the entity the caller
    described is faithful to what they supplied — the name and the value are
    both theirs, nothing is invented — and it leaves a reference that the build
    can actually wire.
    """
    if field not in _PROPERTY_VALUE_REF_FIELDS:
        return value
    is_list = isinstance(value, (list, tuple))
    items = list(value) if is_list else [value]
    out: list[Any] = []
    for item in items:
        if isinstance(item, dict) and item.get("name") and not item.get("@id"):
            from builder.tools.drafters import draft_property_value

            hints = {k: v for k, v in item.items() if k != "name"}
            out.append(draft_property_value(state, str(item["name"]), hints).entity_id)
        else:
            out.append(item)
    return out if is_list else out[0]


def _reject_unresolvable_property_values(
    state: CrateState, entity_id: str, field: str, value: Any
) -> None:
    """Raise when an ``additionalProperty`` member names no entity.

    Narrower than the general reference check on purpose. Most reference fields
    tolerate a forward reference — the agent may wire a result before drafting
    the file — but a PropertyValue is never external and never arrives later:
    either it exists or the member is a display string that the build silently
    discards. ``"Computational Tool: GraphPad Prism"`` survived normalisation
    verbatim, looked stored, and then vanished at build time with the process
    still failing its MUST. Refuse it while the caller can still act.
    """
    if field not in _PROPERTY_VALUE_REF_FIELDS:
        return
    for item in value if isinstance(value, (list, tuple)) else [value]:
        key = item.get("@id") if isinstance(item, dict) else item
        if not isinstance(key, str) or "://" in key:
            continue
        if state.get_entity(key.lstrip("./").lstrip("#")) is None:
            raise ValueError(
                f"{key!r} is not an entity, so {field!r} on {entity_id!r} would be "
                f"dropped when the crate is built. {field!r} holds references to "
                f"PropertyValue entities: call draft_property_value(name=…, "
                f"hints={{'value': …}}) first and pass the entity_id it returns, or "
                f"pass the name/value inline as {{'name': …, 'value': …}} and it "
                f"will be created for you."
            )


def _reject_fully_dropped_references(
    entity_id: str, supplied: dict[str, Any], normalized: dict[str, Any]
) -> None:
    """Raise when a reference field was given values and kept none of them.

    Normalisation drops what it cannot resolve, which is right — the build would
    drop it anyway. Storing the empty remainder as if it were the caller's intent
    is not: it overwrites whatever was there, records the field as ``filled``,
    and hands back an entity that looks successfully updated. A model reading
    that result has no way to learn its encoding was wrong, so it tries another
    one, and another. Failing loudly turns three silent rounds into one clear
    correction.
    """
    for field, value in supplied.items():
        if not is_reference_field(field) or not value:
            continue
        kept = normalized.get(field)
        if kept:
            continue
        raise ValueError(
            f"None of the values given for {field!r} on {entity_id!r} resolve to an "
            f"entity, so nothing would be stored: {value!r}. This property holds "
            f"REFERENCES to entities that already exist. Create the entity first "
            f"(e.g. draft_property_value for an additionalProperty) and pass its "
            f"entity_id, or pass an existing id — not a display string."
        )


def set_fields(
    state: CrateState,
    entity_id: str,
    fields: dict[str, Any],
    source: CompletionSource = "llm",
) -> Entity:
    """Set one or more fields on an entity, with completion tracking.

    This is the single consolidated mutation tool (Issue #90, sub-task 2). It
    replaces the byte-identical ``update_entity`` / ``bulk_set_fields`` pair and
    the single-field ``set_entity_field`` (the one-key-dict case): pass a dict of
    field names to values — one key or many.

    Args:
        state: The crate state to operate on.
        entity_id: The ID of the entity to update.
        fields: Dictionary of field names to new values.
        source: The provenance source recorded for every field set.

    Returns:
        The updated Entity.

    Raises:
        ValueError: If no entity with the given ID exists.
    """
    entity = state.get_entity(entity_id)
    if entity is None:
        raise ValueError(f"Entity not found: {entity_id}")

    supplied = dict(fields)
    fields = {
        name: (_normalize_reference_value(
                   state, entity_id, _materialize_property_values(state, name, value), name)
               if is_reference_field(name) else value)
        for name, value in fields.items()
    }
    _reject_fully_dropped_references(entity_id, supplied, fields)
    for field, value in fields.items():
        _reject_unresolvable_property_values(state, entity_id, field, value)

    for field, value in fields.items():
        # (#375) A reference-only property whose value is not a resolvable
        # reference is silently DROPPED at build time (`_scalar_props` strips
        # every `_REF_FIELDS` key, and `_wire_reference` emits nothing for a
        # non-resolvable literal), so the caller believes it stored something the
        # crate will never carry. Warn rather than raise: this is a hot shared
        # tool the ReAct LLM calls directly, and refusing here would be a
        # behaviour change to every existing caller.
        if is_reference_field(field) and not is_resolvable_reference(state, value):
            logger.warning(
                "set_fields: %r on %s is a reference-only property but %r does not "
                "resolve to an entity — the build will drop it",
                field,
                entity_id,
                value,
            )
        entity.fields[field] = value
        entity.set_field_status(field, "filled", source)

    return entity


def update_entity(state: CrateState, entity_id: str, patch: dict) -> Entity:
    """Deprecated alias of :func:`set_fields` (kept for library callers/tests)."""
    return set_fields(state, entity_id, patch)


def _ref_key(value: Any) -> str | None:
    """The bare entity_id a single reference value points at (``#`` stripped)."""
    key = value.get("@id") if isinstance(value, dict) else value
    return key.lstrip("#") if isinstance(key, str) else None


def find_referrers(state: CrateState, entity_id: str) -> list[tuple[Entity, str]]:
    """Find every entity that references ``entity_id`` through a reference field.

    Scans each entity's reference-bearing fields (``_REF_FIELDS`` — the same set
    the crate mapping resolves) for a pointer to ``entity_id``, handling scalar
    values, ``{"@id": ...}`` objects, and lists thereof.

    Args:
        state: The crate state to scan.
        entity_id: The entity_id to find referrers of.

    Returns:
        ``(referrer_entity, field_name)`` tuples — one per field that points at
        ``entity_id``. The entity itself is never reported as its own referrer.
    """
    referrers: list[tuple[Entity, str]] = []
    for ent in state.list_entities():
        if ent.entity_id == entity_id:
            continue
        for field in _REF_FIELDS:
            value = ent.fields.get(field)
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            if any(_ref_key(item) == entity_id for item in items):
                referrers.append((ent, field))
    return referrers


def _drop_reference(fields: dict[str, Any], field: str, entity_id: str) -> None:
    """Remove every pointer to ``entity_id`` from ``fields[field]`` in place."""
    value = fields.get(field)
    if isinstance(value, list):
        pruned = [item for item in value if _ref_key(item) != entity_id]
        if pruned:
            fields[field] = pruned
        else:
            del fields[field]
    elif _ref_key(value) == entity_id:
        del fields[field]


def remove_entity(state: CrateState, entity_id: str, cascade: bool = False) -> bool:
    """Remove an entity by id, preserving referential integrity.

    The builder rebuilds the crate from state on every iteration, so a dangling
    reference left in state surfaces as a dangling ``{"@id": ...}`` in the built
    ``ro-crate-metadata.json``. To prevent that, removal first finds every
    referrer:

    - ``cascade=False`` (default): if any entity still references the target,
      refuse with an actionable ``ValueError`` naming the referrers (and the
      ``cascade=True`` escape hatch). The target is left in place.
    - ``cascade=True``: clear the target's id out of every referrer's field
      first, then remove it — no dangling references survive.

    Args:
        state: The crate state to operate on.
        entity_id: The ID of the entity to remove.
        cascade: When True, clear referrers instead of refusing.

    Returns:
        True if the entity was found and removed, False otherwise.

    Raises:
        ValueError: If the entity is still referenced and ``cascade`` is False.
    """
    referrers = find_referrers(state, entity_id)
    if referrers and not cascade:
        named = ", ".join(sorted({f"{ent.entity_id} (via {field})" for ent, field in referrers}))
        raise ValueError(
            f"Cannot remove '{entity_id}': still referenced by {named}. "
            f"Repoint or remove those references first, or pass cascade=True to "
            f"clear them."
        )
    if cascade:
        for ent, field in referrers:
            _drop_reference(ent.fields, field, entity_id)
    return state.remove_entity(entity_id)


def list_entities(state: CrateState, entity_type: str | None = None) -> list[Entity]:
    """List entities, optionally filtered by type.

    Args:
        state: The crate state to query.
        entity_type: Optional entity type to filter by (e.g. "Investigation").
            If None, all entities are returned.

    Returns:
        A list of matching Entity objects.
    """
    return state.list_entities(entity_type=entity_type)


def list_scanned_files(
    state: CrateState,
    name_contains: str | None = None,
    mime_contains: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Return the raw scanned-file inventory from ``CrateState`` (paginated).

    ``scan_files`` only surfaces a small sample, and its tool output is later
    pruned from history to save tokens — so the agent uses this to retrieve the
    full list of files it must bind to ``File``/process entities. Returns compact
    records (``path``/``filename``/``size``/``mime_type``) only — never the heavy
    ``first_rows`` preview — so the result stays token-bounded.

    Args:
        state: The crate state to query.
        name_contains: Keep only files whose ``filename`` or ``path`` contains
            this substring (case-insensitive).
        mime_contains: Keep only files whose ``mime_type`` contains this
            substring (e.g. ``"csv"``, ``"image"``).
        offset: Pagination start index into the (filtered) list.
        limit: Maximum number of records to return.

    Returns:
        ``{"total_scanned", "matched", "offset", "limit", "returned", "files": [...]}``
        where ``files`` is the requested page of compact records.
    """
    name_q = name_contains.lower() if name_contains else None
    mime_q = mime_contains.lower() if mime_contains else None

    def _match(fc: FileClassification) -> bool:
        if name_q is not None and (
            name_q not in (fc.filename or "").lower() and name_q not in (fc.path or "").lower()
        ):
            return False
        if mime_q is not None and mime_q not in (fc.mime_type or "").lower():
            return False
        return True

    matched = [fc for fc in state.scanned_files if _match(fc)]
    start = max(0, offset)
    page = matched[start : start + max(0, limit)]
    return {
        "total_scanned": len(state.scanned_files),
        "matched": len(matched),
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "files": [
            {
                "path": fc.path,
                "filename": fc.filename,
                "size": fc.size,
                "mime_type": fc.mime_type,
            }
            for fc in page
        ],
    }


def set_entity_field(
    state: CrateState,
    entity_id: str,
    field: str,
    value: Any,
    source: CompletionSource = "llm",
) -> None:
    """Deprecated single-field alias of :func:`set_fields` (the one-key case)."""
    set_fields(state, entity_id, {field: value}, source)


def bulk_set_fields(
    state: CrateState,
    entity_id: str,
    fields: dict[str, Any],
    source: CompletionSource = "llm",
) -> None:
    """Deprecated alias of :func:`set_fields` (kept for library callers/tests)."""
    set_fields(state, entity_id, fields, source)


def set_crate_metadata(
    state: CrateState,
    title: str | None = None,
    description: str | None = None,
    accession: str | None = None,
    release_date: str | None = None,
    date_modified: str | None = None,
    publisher: str | None = None,
    creator: str | None = None,
    contact: str | None = None,
    license: str | None = None,
) -> dict[str, Any]:
    """Set top-level crate metadata on ``state.metadata`` (the Root Data Entity).

    The single setter for crate-level (root dataset) scalar metadata. ``title`` /
    ``description`` / ``accession`` map onto the root's ``name`` / ``description``
    / ``identifier``; ``release_date`` / ``date_modified`` map onto the root's
    ``schema:releaseDate`` / ``schema:dateModified`` (#180). Pass ISO-8601
    date/datetime strings for the dates (e.g. ``"2025-11-10"`` or
    ``"2026-06-14T19:37:30Z"``).

    Only the arguments actually supplied (non-``None``, non-empty) are written —
    omitting a field leaves the current value untouched and never fabricates one
    (D5). ro-crate-py auto-sets the root's ``datePublished`` at build time; this
    tool never touches it.

    Args:
        state: The crate state to mutate.
        title: Human-readable crate title (root ``name``).
        description: Free-text crate description (root ``description``).
        accession: Accession/identifier (root ``identifier``).
        release_date: ISO-8601 release date (root ``schema:releaseDate``).
        date_modified: ISO-8601 last-modified date/datetime
            (root ``schema:dateModified``).

    Returns:
        A token-bounded summary of the metadata values now in effect for the
        fields this tool manages, or an ``{"error": ...}`` dict when the call
        supplies no value to write.

    A call with every field empty is REFUSED rather than treated as a harmless
    read. It cannot write anything, so it is always a mistake — and the
    successful-looking summary it used to return read as progress to the caller,
    which is how one session issued this call 33 times in a row (~990k input
    tokens) without anything stopping it. Use ``get_status`` to read the current
    metadata.
    """
    supplied = (
        title, description, accession, release_date, date_modified,
        publisher, creator, contact, license,
    )
    if all(value in (None, "") for value in supplied):
        return {
            "error": (
                "set_crate_metadata needs at least one value to write. Pass the "
                "field(s) you want to set — title, description, accession, "
                "release_date or date_modified — or use get_status to read the "
                "current crate metadata."
            ),
            "tool": "set_crate_metadata",
        }

    m = state.metadata
    if title not in (None, ""):
        m.title = title
    if description not in (None, ""):
        m.description = description
    if accession not in (None, ""):
        m.accession = accession
    if release_date not in (None, ""):
        m.release_date = release_date
    if date_modified not in (None, ""):
        m.date_modified = date_modified
    # Attribution: an entity id or a resolvable IRI, never free text — a name
    # string credits nobody a registry can resolve (D5).
    for attr, value in (
        ("publisher", publisher), ("creator", creator), ("contact", contact)
    ):
        if value in (None, ""):
            continue
        if not is_resolvable_reference(state, value):
            return {
                "error": (
                    f"{attr}={value!r} does not resolve to anyone. Draft the Person or "
                    "Organization first (draft_person / draft_organization), or pass a "
                    "verified ORCID/ROR IRI. A bare name is not attribution."
                ),
                "tool": "set_crate_metadata",
            }
        setattr(m, attr, value)
    if license not in (None, ""):
        m.license = license
    return {
        "title": m.title,
        "description": m.description,
        "accession": m.accession,
        "release_date": m.release_date,
        "date_modified": m.date_modified,
        "publisher": m.publisher,
        "creator": m.creator,
        "contact": m.contact,
        "license": m.license,
    }


def set_validation_preference(
    state: CrateState,
    recommended: bool | None = None,
    optional: bool | None = None,
) -> dict[str, Any]:
    """Record whether the user wants the RECOMMENDED / OPTIONAL validation tiers.

    The interactive loop offers each broader tier once and then honours the
    answer silently. Call this when the user changes their mind mid-session —
    "stop running the recommended checks", "let's look at the optional findings
    after all" — so the loop stops asking, or starts running, accordingly.

    The tiers are a hierarchy, and this enforces it: OPTIONAL findings are only
    meaningful once the SHOULD-tier gaps are being worked, so turning
    ``recommended`` off turns ``optional`` off with it. Turning ``optional`` on
    turns ``recommended`` on for the same reason.

    Args:
        state: The crate state whose preferences are updated.
        recommended: Run the RECOMMENDED tier from now on. ``None`` leaves it.
        optional: Run the OPTIONAL tier from now on. ``None`` leaves it.

    Returns:
        The preferences now in effect, plus the tiers that will run.
    """
    prefs = state.validation_preferences
    if recommended is not None:
        prefs["recommended"] = bool(recommended)
        if not recommended:
            # Optional sits above recommended: keeping it on while its
            # foundation is off would go back to asking about MAY-level
            # findings the user has just said they do not want.
            prefs["optional"] = False
    if optional is not None:
        prefs["optional"] = bool(optional)
        if optional:
            prefs["recommended"] = True
    return {
        "validation_preferences": dict(prefs),
        "tiers_that_will_run": ["required"]
        + [tier for tier in ("recommended", "optional") if prefs.get(tier)],
    }
# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("list_entities", list_entities, takes_state=True)
TOOL_REGISTRY.register("list_scanned_files", list_scanned_files, takes_state=True)
TOOL_REGISTRY.register("remove_entity", remove_entity, takes_state=True)
# The single consolidated mutation tool (Issue #90, sub-task 2). update_entity,
# bulk_set_fields and set_entity_field were redundant (the first two byte-
# identical, the third the single-field case) and are no longer exposed to the
# LLM — set_fields covers all three.
TOOL_REGISTRY.register("set_fields", set_fields, takes_state=True)
# Crate-level (Root Data Entity) metadata setter — title/description/accession
# plus the root dates releaseDate/dateModified (Issue #180).
TOOL_REGISTRY.register("set_crate_metadata", set_crate_metadata, takes_state=True)
# Standing answer to "run the broader validation tiers?" — so the user can
# revoke it mid-session instead of being asked again (or never again).
TOOL_REGISTRY.register("set_validation_preference", set_validation_preference, takes_state=True)
