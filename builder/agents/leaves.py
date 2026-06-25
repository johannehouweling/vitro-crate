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

from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from builder.agents.agent_loop import (
    _build_chat_model,
    _extract_model_name,
    _extract_token_usage,
)
from builder.tools._crate_mapping import _REF_FIELDS, draft_hints_schema

# A usage sink is notified of one leaf call's token usage:
# ``(input_tokens, output_tokens, model_name)``. Any element may be ``None`` when
# the provider/fake reported no usage. The deterministic pipeline passes a sink
# that accumulates usage and logs it to the engine profiler so the eval harness
# records real per-case token counts for the ``--arch pipeline`` arm (Issue #221).
UsageSink = Callable[[int | None, int | None, str | None], None]


def _invoke_structured_with_usage(
    llm: Any,
    schema: dict[str, Any],
    messages: list[Any],
    usage_sink: UsageSink | None,
) -> Any:
    """Invoke a structured-output call, reporting token usage when a sink is set.

    With no ``usage_sink`` this is the legacy path: bind ``with_structured_output``
    and return the bare parsed object. With a sink, it binds with
    ``include_raw=True`` so the raw ``AIMessage`` is available, mines
    ``(input_tokens, output_tokens, model_name)`` off it via the SAME
    provider-agnostic helpers the ReAct model node uses
    (:func:`builder.agents.agent_loop._extract_token_usage`), reports them, and
    returns the parsed object — so callers are unaffected by the capture.
    """
    if usage_sink is None:
        return llm.with_structured_output(schema).invoke(messages)

    raw_result = llm.with_structured_output(schema, include_raw=True).invoke(messages)
    if isinstance(raw_result, dict) and "parsed" in raw_result:
        raw_msg = raw_result.get("raw")
        input_tokens, output_tokens = _extract_token_usage(raw_msg)
        usage_sink(input_tokens, output_tokens, _extract_model_name(raw_msg))
        return raw_result.get("parsed")
    # Defensive: a model/runnable that ignored include_raw still yields a result.
    return raw_result

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


# ---------------------------------------------------------------------------
# Stage A: the bounded candidate-plan extractor (Issue #179).
#
# ``extract_plan`` is the *whole-document* sibling of ``draft_entity_fields``: a
# SINGLE bounded structured-output call on the drafter tier that reads scanned
# research docs and proposes a CANDIDATE PLAN of the ISA-Tox entities the docs
# support — the study, the test/control compounds, cell lines, the
# CellCulture→Exposure→EndpointReadout→DataAnalysis process chain, AOPs, people,
# publications, files, and free-text notes for the user to confirm.
#
# It is a *proposal*, not committed truth: every field is optional and the model
# is told to fill only what the context supports and to record ambiguity in
# ``notes`` rather than invent. Crucially (D5) the plan proposes WHAT EXISTS *by
# name only* — NO CAS / CID / InChIKey / SMILES / Cellosaurus accession / ORCID /
# DOI / @id. Those identifiers are resolved later by deterministic lookups, never
# guessed by this leaf. The schema the model sees carries no identifier field
# (the strongest guard) and any identifier an adversarial model slips into the
# output is defensively stripped (:func:`_strip_plan_identifiers`).
# ---------------------------------------------------------------------------

# D5: identifier-bearing keys an adversarial model might attach to any plan item.
# Pruned from the schema the model sees AND stripped from the result. A superset
# of :data:`_IDENTIFIER_SCALAR_FIELDS` with the plan-specific aliases the docs
# might tempt the model toward (``cid``, ``cellosaurus``, ``@id``, ``id``).
_PLAN_IDENTIFIER_FIELDS: frozenset[str] = _IDENTIFIER_SCALAR_FIELDS | frozenset(
    {
        "cid",
        "inchi",
        "cellosaurus",
        "cellosaurus_accession",
        "aop_url",
        "@id",
        "id",
    }
)

_PLAN_SYSTEM_PROMPT = (
    "You are a bounded planning extractor for ISA-Tox RO-Crates. Read the "
    "provided research documents and propose a CANDIDATE PLAN of the entities "
    "and connections the documents support: the study, test/control compounds, "
    "cell lines, the CellCulture -> Exposure -> EndpointReadout -> DataAnalysis "
    "process chain, AOPs (only if an AOP id is explicitly stated), people, "
    "publications, and files. This is a PROPOSAL for the user to confirm, not "
    "committed truth. Propose ONLY what the documents support; leave any field "
    "you cannot support empty and record ambiguities or things the user should "
    "confirm in 'notes'. Refer to compounds, cell lines, people and "
    "publications BY NAME ONLY. NEVER include identifiers of any kind: no CAS, "
    "PubChem CID, InChIKey, SMILES, InChI, Cellosaurus accession, ORCID, DOI, "
    "or @id. Those are resolved later by dedicated lookup services, never by you."
)


def _plan_schema() -> dict[str, Any]:
    """Build the structured-output schema for the candidate plan (D5-clean).

    A hand-built JSON Schema describing the Plan shape. Every field is optional —
    the model fills only what the docs support. By construction it contains NO
    identifier field (no CAS/CID/InChIKey/SMILES/accession/ORCID/DOI/@id), so the
    model is never even asked to produce one (the strongest D5 guard). Sections
    stay open (``additionalProperties: true``) for descriptive long-tail fields,
    but :func:`_strip_plan_identifiers` still scrubs the result as defense in
    depth. A top-level ``title`` is added so langchain can use the schema as a
    structured-output function spec.
    """
    str_field = {"type": "string"}

    def _array_of(item_props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "type": "object",
            "properties": item_props,
            "additionalProperties": True,
        }
        if required:
            item["required"] = required
        return {"type": "array", "items": item}

    return {
        "title": "CandidatePlan",
        "type": "object",
        "description": (
            "A candidate plan of the ISA-Tox entities the documents support. "
            "All fields optional; propose only what the docs support and leave "
            "the rest empty. Names only — no identifiers (D5)."
        ),
        "properties": {
            "study": {
                "type": "object",
                "description": "The study the documents describe.",
                "properties": {
                    "name": {**str_field, "description": "Study name."},
                    "description": {**str_field, "description": "Free-text study description."},
                },
                "additionalProperties": True,
            },
            "compounds": _array_of(
                {
                    "name": {**str_field, "description": "Compound name only (no identifiers)."},
                    "role": {
                        "type": "string",
                        "enum": ["test", "control"],
                        "description": "Whether the compound is the test article or a control.",
                    },
                },
                required=["name"],
            ),
            "cell_lines": _array_of(
                {"name": {**str_field, "description": "Cell-line name only (no accession)."}},
                required=["name"],
            ),
            "protocols": _array_of(
                {
                    "name": {**str_field, "description": "Protocol name only (no identifiers)."},
                    "description": {
                        **str_field,
                        "description": "Free-text description of the protocol.",
                    },
                    "process_hint": {
                        **str_field,
                        "description": (
                            "Free-text hint of which process step this protocol "
                            "governs, e.g. the process_type ('EndpointReadout') or "
                            "a step name. Optional."
                        ),
                    },
                },
                required=["name"],
            ),
            "process_chain": _array_of(
                {
                    "process_type": {
                        "type": "string",
                        "enum": ["CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"],
                        "description": "Which step of the ISA-Tox LabProcess chain this is.",
                    },
                    "name": {**str_field, "description": "Step name."},
                    "object_hint": {**str_field, "description": "Free-text hint of the input."},
                    "result_hint": {**str_field, "description": "Free-text hint of the output."},
                },
                required=["process_type"],
            ),
            "aops": _array_of(
                {"aop_id": {**str_field, "description": "AOP-Wiki id, only if explicitly stated."}},
                required=["aop_id"],
            ),
            "people": _array_of(
                {
                    "name": {**str_field, "description": "Person's name only (no ORCID)."},
                    "affiliation_name": {**str_field, "description": "Affiliation org name."},
                },
                required=["name"],
            ),
            "publications": _array_of(
                {"title": {**str_field, "description": "Publication title only (no DOI)."}},
                required=["title"],
            ),
            "files": _array_of(
                {
                    "path": {**str_field, "description": "File path or name."},
                    "role": {
                        "type": "string",
                        "enum": ["raw", "processed", "condition_table", "other"],
                        "description": "What kind of data file this is.",
                    },
                },
                required=["path"],
            ),
            "notes": {
                **str_field,
                "description": (
                    "Free-text: ambiguities, gaps, and anything the user should "
                    "confirm. Use this instead of guessing."
                ),
            },
        },
        "additionalProperties": False,
    }


def _strip_plan_identifiers(value: Any) -> Any:
    """Recursively drop any identifier field from a plan (D5, defense in depth).

    Walks the plan's nested dicts/lists and removes every key in
    :data:`_PLAN_IDENTIFIER_FIELDS` wherever it appears, so an identifier an
    adversarial model slipped into any section (a compound's CAS, a person's
    ORCID, a ``@id`` on any item) never propagates downstream.
    """
    if isinstance(value, dict):
        return {
            key: _strip_plan_identifiers(val)
            for key, val in value.items()
            if key not in _PLAN_IDENTIFIER_FIELDS
        }
    if isinstance(value, list):
        return [_strip_plan_identifiers(item) for item in value]
    return value


def extract_plan(
    context: str,
    *,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
) -> dict[str, Any]:
    """Propose a candidate plan of ISA-Tox entities from research docs.

    Stage A of the §14 hybrid build loop: a pure leaf making a SINGLE bounded
    structured-output call on the drafter tier
    (``_build_chat_model(role="drafter")``) over the scanned-document ``context``.
    It returns a CANDIDATE PLAN — the study, test/control compounds, cell lines,
    the CellCulture→Exposure→EndpointReadout→DataAnalysis process chain, AOPs,
    people, publications, files, and free-text ``notes`` — for the user to
    confirm. It does not mutate state and does not orchestrate.

    Every field is optional: the model proposes only what the context supports and
    records ambiguity in ``notes`` rather than inventing. D5: the plan names WHAT
    EXISTS — no identifiers (CAS/CID/InChIKey/SMILES/Cellosaurus accession/ORCID/
    DOI/@id). The schema the model sees carries no identifier field, and any that
    an adversarial model slips into the output is stripped recursively. Real
    identifiers are resolved later by deterministic lookups, never by this leaf.

    Args:
        context: The scanned research documents (or an excerpt) to plan from.
        model: Optional explicit model name override; when ``None`` the drafter
            tier resolves the configured drafter (or primary) model.
        usage_sink: Optional callback notified of this call's token usage as
            ``(input_tokens, output_tokens, model_name)``. When given, the call
            binds structured output with ``include_raw=True`` so usage can be
            mined off the raw response (Issue #221). Default ``None`` leaves the
            call (and its return) unchanged.

    Returns:
        A candidate-plan dict free of fabricated identifiers. An empty/
        uninformative context yields an empty-but-valid plan rather than
        fabricated entities.
    """
    llm = _build_chat_model(model=model, role="drafter")
    schema = _plan_schema()

    messages = [
        SystemMessage(content=_PLAN_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                "Documents:\n"
                f"{context}\n\n"
                "Propose the candidate plan. Fill only what the documents "
                "support; leave the rest empty and note ambiguities in 'notes'. "
                "Names only — no identifiers."
            )
        ),
    ]
    result = _invoke_structured_with_usage(llm, schema, messages, usage_sink)

    plan = dict(result) if isinstance(result, dict) else {}
    return _strip_plan_identifiers(plan)


def draft_entity_fields(
    entity_type: str,
    context: str,
    *,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
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
        usage_sink: Optional callback notified of this call's token usage as
            ``(input_tokens, output_tokens, model_name)``. When given, the call
            binds structured output with ``include_raw=True`` so usage can be
            mined off the raw response (Issue #221). Default ``None`` leaves the
            call (and its return) unchanged.

    Returns:
        A dict of the entity's descriptive fields, validating against
        ``draft_hints_schema(entity_type)`` and free of fabricated identifiers.
    """
    llm = _build_chat_model(model=model, role="drafter")
    schema = _structured_output_schema(entity_type)

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
    result = _invoke_structured_with_usage(llm, schema, messages, usage_sink)

    fields = dict(result) if isinstance(result, dict) else {}
    return _strip_identifiers(fields)


__all__ = ["draft_entity_fields", "extract_plan"]
