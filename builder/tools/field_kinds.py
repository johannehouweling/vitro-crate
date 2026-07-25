"""What KIND of value a field takes — the single vocabulary both arms share (#375).

Three questions recur across the toolbox and both build arms, and each was
previously answered by a private copy that could drift from the build's actual
behaviour:

* **Is this field identifier-bearing?** (D5: its value comes from a lookup, never
  from the model's or the user's prose.) Two byte-identical definitions existed —
  ``guidance._IDENTIFIER_FIELDS`` and ``leaves._IDENTIFIER_SCALAR_FIELDS`` — the
  first kept local only to spare the offline path an import of the
  langchain-importing ``leaves`` module. A ``builder/tools/`` module solves that
  properly: it is dependency-light and importable from anywhere.
* **Is this field reference-only?** A property that must hold an entity reference,
  never a literal. :data:`builder.tools._crate_mapping._REF_FIELDS` is the source
  of truth; this module is its single reader.
* **Would this value actually resolve as a reference?** Mirrors
  :func:`builder.tools._crate_mapping._wire_reference`'s own acceptance rule, so
  a caller cannot believe it committed a reference the build will discard.

The third question is the one that made #375 a *correctness* bug rather than a
tidiness one: the guidance loop stored free text on a reference-only property,
reported success, and the builder then silently dropped it —
``_scalar_props`` strips every ``_REF_FIELDS`` key, and ``_wire_reference``
(``keep_literal=False``) emits nothing for a non-resolvable literal. Asking the
same question here and in the build keeps the two sides from disagreeing.
"""

from __future__ import annotations

from typing import Any

from builder.state import CrateState
from builder.tools._crate_mapping import _REF_FIELDS

__all__ = [
    "CITATION_FIELDS",
    "IDENTIFIER_FIELDS",
    "PERSON_FIELDS",
    "is_citation_field",
    "is_identifier_field",
    "is_person_field",
    "is_reference_field",
    "is_resolvable_reference",
]


# (#275) Person/agent-typed fields whose ISA value MUST be a Person (or
# Organization) ENTITY reference, never a literal string. They have their own
# commit route (``draft_person`` + link by ``@id``), so they are reference-shaped
# without being members of ``_REF_FIELDS``.
PERSON_FIELDS: frozenset[str] = frozenset(
    {"creator", "author", "publisher", "editor", "contributor"}
)

# (#179) Citation-typed fields whose value MUST be a ``ScholarlyArticle``
# reference with an absolute-URI ``@id``, resolved through the publication
# composites rather than stored as prose.
CITATION_FIELDS: frozenset[str] = frozenset({"citation"})


def is_person_field(field: str) -> bool:
    """Whether ``field`` (a local property name) is person/agent-typed."""
    return field in PERSON_FIELDS


def is_citation_field(field: str) -> bool:
    """Whether ``field`` (a local property name) is citation-typed."""
    return field in CITATION_FIELDS


# D5 ("Verify, don't trust"): identifier-bearing field names whose value must come
# from a lookup and must NEVER be taken from prose — the model's or the user's.
#
# NB (#377): membership is exact, so a camelCase property local name (``inChIKey``)
# does not match the snake_case entry (``inchikey``). That mismatch is a real
# defect, but fixing it changes which gaps are refused and is therefore #377's
# scope, not this module's.
IDENTIFIER_FIELDS: frozenset[str] = frozenset(
    {
        # generic identifier slot (CAS / accession / DOI, per entity type)
        "identifier",
        "accession",
        # MolecularEntity structure/registry identifiers
        "inchikey",
        "smiles",
        "molecular_formula",
        "pubchem_cid",
        "cas",
        "casrn",
        "cas_number",
        # Person / Organization / Publication external ids
        "orcid",
        "ror",
        "doi",
        # DefinedTerm / PropertyValue ontology identifiers + dereferenceable IRIs
        "term_code",
        "in_defined_term_set",
        "property_id",
        "unit_code",
        "url",
    }
)


def is_identifier_field(field: str) -> bool:
    """Whether ``field`` (a local property name) is identifier-bearing (D5)."""
    return field in IDENTIFIER_FIELDS


def is_reference_field(field: str) -> bool:
    """Whether ``field`` must hold an entity reference rather than a literal.

    The single reader of :data:`builder.tools._crate_mapping._REF_FIELDS`, which
    the build uses to strip such keys out of an entity's scalar properties.
    """
    return field in _REF_FIELDS


def is_resolvable_reference(state: CrateState, value: Any) -> bool:
    """Whether ``value`` would actually be emitted as a reference at build time.

    Mirrors :func:`builder.tools._crate_mapping._wire_reference`'s acceptance
    rule (with its default ``keep_literal=False``) so the two cannot drift:

    * an inline ``{"@id": …}`` object;
    * a string that is already an IRI (contains ``"://"``) or a ``#``-fragment;
    * a value naming an entity that exists in ``state`` (bare ``entity_id``, or
      the type-qualified storage key that shared collections use — ``get_entity``
      accepts both, per Issue #57).

    Anything else — notably free text — is **not** resolvable: the build would
    drop it, so a caller must not report it as committed.
    """
    if value is None or value == "":
        return False
    if isinstance(value, dict):
        return bool(value.get("@id"))
    if isinstance(value, (list, tuple)):
        return bool(value) and all(is_resolvable_reference(state, v) for v in value)
    if not isinstance(value, str):
        return False
    if "://" in value or value.startswith("#"):
        return True
    return state.get_entity(value) is not None
