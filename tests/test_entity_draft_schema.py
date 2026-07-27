"""Tests for the typed entity-draft schema (Issue #90, sub-task 1).

The ``draft_*`` tools used to advertise a schema-less ``hints: {type: object}``
parameter, which a weak model never fills with the right keys. They now advertise
per-entity-type typed parameter schemas sourced from a single source of truth
(``builder.tools._crate_mapping.ENTITY_DRAFT_SCHEMA``) shared with ``_REF_FIELDS``
so the advertised reference keys and the crate-mapping resolver cannot drift.
"""

from __future__ import annotations

from typing import Any, cast

from builder.tools._crate_mapping import (
    ENTITY_DRAFT_SCHEMA,
    _REF_FIELDS,
    EntityDraftSchema,
)


def _spec(name: str) -> dict[str, Any]:
    """Return the TOOL_SPECS entry for ``name`` as a plain dict (typed for ty)."""
    from builder.agents.react.tools_spec import TOOL_SPECS

    return next(cast(dict[str, Any], s) for s in TOOL_SPECS if s["name"] == name)


def test_schema_exists_for_every_drafted_entity_type():
    """Every entity type a ``draft_*`` tool can create has a draft schema."""
    expected = {
        "Investigation",
        "Study",
        "Assay",
        "MolecularEntity",
        "CellLineSample",
        "LabProcess",
        "LabProtocol",
        "Sample",
        "Person",
        "Organization",
        "Publication",
    }
    assert expected <= set(ENTITY_DRAFT_SCHEMA)


def test_entries_are_entity_draft_schema_instances():
    for entity_type, schema in ENTITY_DRAFT_SCHEMA.items():
        assert isinstance(schema, EntityDraftSchema), entity_type


def test_reference_fields_are_subset_of_ref_fields():
    """Advertised reference keys must be a strict subset of ``_REF_FIELDS``.

    This is the anti-drift guarantee: the keys the LLM is told carry entity
    references are exactly the keys the crate-mapping resolver treats as
    references. If someone adds a ref key to a draft schema that the mapping
    does not resolve, this fails.
    """
    for entity_type, schema in ENTITY_DRAFT_SCHEMA.items():
        unknown = set(schema.ref_fields) - _REF_FIELDS
        assert not unknown, (
            f"{entity_type} advertises reference keys not in _REF_FIELDS: {unknown}"
        )


def test_scalar_and_reference_fields_do_not_overlap():
    for entity_type, schema in ENTITY_DRAFT_SCHEMA.items():
        overlap = set(schema.scalar_fields) & set(schema.ref_fields)
        assert not overlap, f"{entity_type} field both scalar and reference: {overlap}"


def test_every_field_has_a_nonempty_description():
    for entity_type, schema in ENTITY_DRAFT_SCHEMA.items():
        for field, desc in {**schema.scalar_fields, **schema.ref_fields}.items():
            assert desc and isinstance(desc, str), f"{entity_type}.{field} missing desc"


def test_name_is_advertised_for_named_entity_types():
    """Entity types whose drafter mints an id from a ``name`` advertise it."""
    for entity_type in ("Investigation", "Study", "Assay", "Sample", "LabProtocol"):
        assert "name" in ENTITY_DRAFT_SCHEMA[entity_type].scalar_fields


def test_draft_specs_no_longer_use_bare_object_hints():
    """No ``draft_*`` tool may advertise a schema-less ``hints: {type: object}``.

    The whole point of sub-task 1: a weak model must see typed keys, not an
    opaque object it never fills correctly.
    """
    from builder.agents.react.tools_spec import TOOL_SPECS

    for raw in TOOL_SPECS:
        spec = cast(dict[str, Any], raw)
        if not spec["name"].startswith("draft_"):
            continue
        hints = spec["parameters"]["properties"].get("hints")
        if hints is None:
            continue  # draft_file has no hints param
        assert hints.get("properties"), (
            f"{spec['name']} still has a schema-less hints param: {hints}"
        )


def test_draft_study_hints_advertise_ref_keys():
    """A representative draft spec exposes its reference keys as typed properties."""
    spec = _spec("draft_study")
    props = spec["parameters"]["properties"]["hints"]["properties"]
    assert "name" in props
    assert "aop" in props  # a reference key from the Study schema


# ---------------------------------------------------------------------------
# LabProcess experimental parameters (#379)
# ---------------------------------------------------------------------------


def test_technical_replicate_is_advertised():
    """`_build_process` reads it, so the vocabulary must offer it.

    `_build_process` does `f.get("technical_replicate", "1")`, but the key was
    absent from the advertised scalars — so `Technical replicate = "1"` was
    unfillable through the advertised vocabulary on BOTH arms.
    """
    assert "technical_replicate" in ENTITY_DRAFT_SCHEMA["LabProcess"].scalar_fields


def test_labprocess_parameter_fields_are_all_advertised():
    """The parameter constant must be DERIVED from the shared vocabulary.

    Non-tautological: both sides are read from the module, so this can only fail
    if someone hand-writes the constant instead of deriving it — which is exactly
    how the two arms would drift apart again.
    """
    from builder.tools._crate_mapping import LABPROCESS_PARAMETER_FIELDS

    advertised = set(ENTITY_DRAFT_SCHEMA["LabProcess"].scalar_fields)
    assert LABPROCESS_PARAMETER_FIELDS <= advertised
    assert LABPROCESS_PARAMETER_FIELDS, "the parameter vocabulary must not be empty"


def test_units_is_not_a_parameter_field():
    """`units` is advertised as a string but the code requires a dict.

    `profiles/models/tox.py` does `u = units or {}` then `u.get("Exposure
    Duration")` — a display-name-keyed map. `draft_hints_schema` flattens every
    scalar to `{"type": "string"}`, so a model following the advertised type
    passes a bare string and the build dies with `'str' object has no attribute
    'get'`, which `build_and_validate` reports as an error with ZERO routable
    issues. Excluded here until the type is fixed; see #379 Out of scope.
    """
    from builder.tools._crate_mapping import LABPROCESS_PARAMETER_FIELDS

    assert "units" not in LABPROCESS_PARAMETER_FIELDS


def test_name_and_description_are_not_parameter_fields():
    """HONESTY CONTROL — the constant is a filter, not the whole scalar set.

    `name` drives the process `@id` via `_make_entity_id` and is merged
    separately; `description` is never emitted by `_build_process`, so overlaying
    it would create a dead state field.
    """
    from builder.tools._crate_mapping import LABPROCESS_PARAMETER_FIELDS

    assert "name" not in LABPROCESS_PARAMETER_FIELDS
    assert "description" not in LABPROCESS_PARAMETER_FIELDS
