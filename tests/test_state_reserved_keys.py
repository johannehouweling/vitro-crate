"""Tests for the reserved-key guard in Entity.set_fields_from_dict (#286).

The keys ``entity_id`` / ``@id`` are an entity's internal @id handle (the
``Entity.entity_id`` attribute) and ``type`` / ``@type`` are its @type handle
(the ``Entity.type`` attribute). None of them are schema.org properties, so if
they ever land in ``Entity.fields`` they serialize as bare JSON-LD keys absent
from the RO-Crate @context and fail base conformance. ``set_fields_from_dict``
must therefore refuse to store them as fields.
"""

from __future__ import annotations

from builder.state import Entity


def test_set_fields_from_dict_drops_reserved_internal_keys():
    """entity_id/@id/type/@type passed in a fields dict never become fields."""
    entity = Entity(entity_id="dt_x", type="DefinedTerm")
    entity.set_fields_from_dict(
        {
            "name": "apoptosis",
            "entity_id": "http://example.org/term",
            "@id": "http://example.org/term",
            "type": "Bogus",
            "@type": "Bogus",
        }
    )
    # Legitimate fields are kept.
    assert entity.fields.get("name") == "apoptosis"
    # The internal handles are NOT stored as fields.
    for key in ("entity_id", "@id", "type", "@type"):
        assert key not in entity.fields, f"reserved key {key!r} leaked into fields"
    # The entity's own identity handles are untouched by the dropped keys.
    assert entity.entity_id == "dt_x"
    assert entity.type == "DefinedTerm"


def test_reserved_keys_get_no_completion_status():
    """Dropped reserved keys must not get a phantom 'filled' completion entry."""
    entity = Entity(entity_id="dt_x", type="DefinedTerm")
    entity.set_fields_from_dict({"name": "apoptosis", "entity_id": "http://example.org/x"})
    assert entity.get_field_status("name") is not None
    assert entity.get_field_status("entity_id") is None
