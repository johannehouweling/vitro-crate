"""What KIND of value a field takes — the single vocabulary the toolbox shares (#375).

Three questions recur across the toolbox, and each was previously answered by a
private copy that could drift from the build's actual behaviour:

* **Is this field identifier-bearing?** (D5: its value comes from a lookup, never
  from the model's or the user's prose.) Two byte-identical definitions existed,
  one kept local only to spare an offline path a langchain import. A
  ``builder/tools/`` module solves that properly: it is dependency-light and
  importable from anywhere.
* **Is this field reference-only?** A property that must hold an entity reference,
  never a literal. :data:`builder.tools._crate_mapping._REF_FIELDS` is the source
  of truth; this module is its single reader.
* **Would this value actually resolve as a reference?** Mirrors
  :func:`builder.tools._crate_mapping._wire_reference`'s own acceptance rule, so
  a caller cannot believe it committed a reference the build will discard.

The third question is the one that made #375 a *correctness* bug rather than a
tidiness one: a caller stored free text on a reference-only property, reported
success, and the builder then silently dropped it —
``_scalar_props`` strips every ``_REF_FIELDS`` key, and ``_wire_reference``
(``keep_literal=False``) emits nothing for a non-resolvable literal. Asking the
same question here and in the build keeps the two sides from disagreeing.
"""

from __future__ import annotations

from typing import Any

from builder.state import CrateState
from builder.tools._crate_mapping import _REF_FIELDS, TERM_FIELDS, draft_hints_schema

__all__ = [
    "CITATION_FIELDS",
    "IDENTIFIER_FIELDS",
    "PERSON_FIELDS",
    "drafter_visible_fields",
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
# does not match the snake_case entry (``inchikey``). :func:`is_identifier_field`
# normalises both sides, so ask it rather than test membership here.
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
        "compoundwiki_id",
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
        # the looked-up term of a stated organ, tissue or cell type
        *TERM_FIELDS,
    }
)

# Fields kept out of what a model is offered to draft (D5): identifiers it must
# never invent, plus entity references, which are wired deterministically by
# ``link`` / the resolver rather than extracted as free text.
_EXCLUDED_DRAFT_FIELDS: frozenset[str] = IDENTIFIER_FIELDS | _REF_FIELDS


def normalise_field_name(field: str | None) -> str:
    """Reduce a property name to a spelling-insensitive comparison key.

    A field is spelled differently in three places: ``CrateState`` fields and
    the draft-hint schemas are snake_case (``inchikey``, ``molecular_formula``);
    a SHACL finding carries a schema.org
    property IRI whose local name is camelCase
    (``http://schema.org/inChIKey``); and MIT ``crate_slot`` values are plain
    field names. Without normalising, the D5 guard matched ``identifier`` and
    ``smiles`` but silently missed ``inChIKey`` and ``molecularFormula``, and the
    user's prose was committed onto the exported node (#377).

    The key is the IRI's local part, lowercased, with separators removed —
    **not** snake_case. A camelCase→snake_case conversion cannot reconcile these
    vocabularies: ``inChIKey`` would become ``in_ch_ikey``, never the
    ``inchikey`` the field set is written in, because the acronym boundaries are
    not recoverable. Dropping separators on both sides sidesteps that entirely
    and is why :data:`_IDENTIFIER_KEYS` is normalised the same way.
    """
    if not field:
        return ""
    local = str(field).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return "".join(ch for ch in local if ch.isalnum()).lower()


# The identifier vocabulary in comparison-key form, so membership is spelling-
# insensitive in both directions (`inchikey` and `inChIKey` both hit).
_IDENTIFIER_KEYS: frozenset[str] = frozenset(
    normalise_field_name(f) for f in IDENTIFIER_FIELDS
)


def is_identifier_field(field: str) -> bool:
    """Whether ``field`` is identifier-bearing (D5), however it is spelled.

    Accepts a snake_case field name, a camelCase property, or a full property
    IRI — see :func:`normalise_field_name`.
    """
    return normalise_field_name(field) in _IDENTIFIER_KEYS


def is_reference_field(field: str) -> bool:
    """Whether ``field`` must hold an entity reference rather than a literal.

    The single reader of :data:`builder.tools._crate_mapping._REF_FIELDS`, which
    the build uses to strip such keys out of an entity's scalar properties.
    """
    return field in _REF_FIELDS


def drafter_visible_fields(entity_type: str) -> frozenset[str]:
    """The fields a model may be offered to draft for ``entity_type``.

    :func:`builder.tools._crate_mapping.draft_hints_schema` minus every
    identifier-bearing and reference field (D5 — the model is never *asked* for
    an identifier).

    Note the schema stays open (``additionalProperties: true``), so a model MAY
    return a field outside this set. This is what the model is *offered*, not a
    bound on what it returns.
    """
    props = draft_hints_schema(entity_type).get("properties", {})
    return frozenset(key for key in props if key not in _EXCLUDED_DRAFT_FIELDS)


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
