"""Bounded drafter leaves for the deterministic pipeline (Issue #179, task 2).

This module holds the cheap-model "bounded extraction" primitive the §14
deterministic pipeline calls at its *leaves*. A leaf is the smallest unit of LLM
work: free-text/context in -> a structured dict of one entity's fields out, in a
SINGLE bounded model call. It does **not** mutate :class:`CrateState` and does
**not** orchestrate — the spine (a separate PR) imports it and feeds the result
to the deterministic ``draft_*`` state mutators.

Design (AGENTS.md §4.4 Model Tiering, §14.2 "Leaves = cheap model"):

- The call runs on the **drafter tier** — ``_build_chat_model(role="drafter")``
  — so a cheap model does the extraction while a stronger model (if configured)
  drives orchestration elsewhere. With no drafter model configured this resolves
  to the primary model, a strict no-op.
- The output is constrained by the entity's typed hint schema
  (``_crate_mapping.draft_hints_schema(entity_type)``) via the model's
  **structured-output / function-calling**, so it validates against that schema.
- **D5 (Verify, Don't Trust).** Identifiers come from *lookups*, never
  invention. Identifier-bearing fields (CAS / InChIKey / SMILES / PubChem CID /
  ORCID / ROR / DOI / Cellosaurus accession / ontology codes / ...) and all
  entity-reference fields are **removed from the schema the model sees** so it is
  never even asked to produce one, and are defensively **stripped from the
  output**. The leaf leaves those fields empty for a downstream lookup to fill.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from builder.agents.agent_loop import _build_chat_model
from builder.tools._crate_mapping import _REF_FIELDS, draft_hints_schema

# ---------------------------------------------------------------------------
# D5: identifier-bearing scalar fields the leaf must NEVER let the model fill.
#
# These are the keys across ENTITY_DRAFT_SCHEMA whose values are identifiers /
# accessions / ontology codes that MUST be resolved by a lookup service, never
# guessed by the extractor. Entity-reference fields (``_REF_FIELDS``) are also
# excluded — they are wired deterministically by ``link`` / the resolver, not
# extracted as free text. Both sets are pruned from the structured-output schema
# *and* stripped from the result.
# ---------------------------------------------------------------------------
_IDENTIFIER_SCALAR_FIELDS: frozenset[str] = frozenset(
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

# Fields removed from the schema the model sees AND stripped from its output (D5).
_EXCLUDED_FIELDS: frozenset[str] = _IDENTIFIER_SCALAR_FIELDS | _REF_FIELDS

_SYSTEM_PROMPT = (
    "You are a bounded metadata extractor for ISA-Tox RO-Crates. Given an entity "
    "type and some free-text context, return ONLY the entity's descriptive fields "
    "(names, free-text descriptions) that are explicitly supported by the context. "
    "Do NOT invent or guess. Critically, NEVER fabricate identifiers: CAS numbers, "
    "InChIKeys, SMILES, PubChem CIDs, ORCIDs, RORs, DOIs, accessions, ontology "
    "codes, or IRIs come from dedicated lookup services, never from you. Leave any "
    "field you are unsure about empty rather than guessing."
)


def _structured_output_schema(entity_type: str) -> dict[str, Any]:
    """Build the structured-output schema for the model, D5-pruned.

    Starts from :func:`draft_hints_schema` and removes every identifier-bearing
    and entity-reference field so the model is never asked to produce an
    identifier (the strongest D5 guard). The schema stays open
    (``additionalProperties: true``) so the long tail of descriptive fields is
    still permitted. A top-level ``title`` is added so langchain can use the
    schema directly as a structured-output function (a raw JSON-schema dict
    without a ``title`` is rejected as a function spec).
    """
    schema = draft_hints_schema(entity_type)
    props = schema.get("properties", {})
    schema["properties"] = {
        key: spec for key, spec in props.items() if key not in _EXCLUDED_FIELDS
    }
    # The function name langchain derives for the structured-output tool. Must be
    # a valid identifier; ``entity_type`` is always a CamelCase type name.
    schema["title"] = f"{entity_type}Fields"
    return schema


def _strip_identifiers(fields: dict[str, Any]) -> dict[str, Any]:
    """Defensively drop any identifier/reference field from model output (D5)."""
    return {
        key: value for key, value in fields.items() if key not in _EXCLUDED_FIELDS
    }


def draft_entity_fields(
    entity_type: str,
    context: str,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Extract one entity's descriptive fields from free-text ``context``.

    A pure leaf: a SINGLE bounded LLM call on the drafter tier
    (``_build_chat_model(role="drafter")``) whose output is constrained by the
    entity type's typed hint schema (``draft_hints_schema(entity_type)``) via the
    model's structured-output / function-calling. It does not mutate state and
    does not orchestrate — context in, structured fields out.

    D5: identifier-bearing fields and entity references are removed from the
    schema the model sees and stripped from the result, so an identifier is never
    fabricated — it is left empty for a downstream lookup to fill.

    Args:
        entity_type: The ISA-Tox entity type (e.g. ``"MolecularEntity"``,
            ``"Study"``) keying :data:`ENTITY_DRAFT_SCHEMA`.
        context: Free-text/context to extract from — a file excerpt, a brief,
            or a conversation snippet.
        model: Optional explicit model name override; when ``None`` the drafter
            tier resolves the configured drafter (or primary) model.

    Returns:
        A dict of the entity's descriptive fields, validating against
        ``draft_hints_schema(entity_type)`` and free of fabricated identifiers.
    """
    llm = _build_chat_model(model=model, role="drafter")
    schema = _structured_output_schema(entity_type)
    structured = llm.with_structured_output(schema)

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Entity type: {entity_type}\n\n"
                f"Context:\n{context}\n\n"
                "Extract the supported descriptive fields for this entity. "
                "Leave identifier fields empty — they are filled by lookups."
            )
        ),
    ]
    result = structured.invoke(messages)

    fields = dict(result) if isinstance(result, dict) else {}
    return _strip_identifiers(fields)


__all__ = ["draft_entity_fields"]
