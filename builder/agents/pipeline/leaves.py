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

from builder.agents.llm import (
    _build_chat_model,
    _extract_model_name,
    _extract_token_usage,
)
from builder.tools._crate_mapping import _REF_FIELDS, draft_hints_schema
from builder.tools.field_kinds import IDENTIFIER_FIELDS

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
    (:func:`builder.agents.react.agent_loop._extract_token_usage`), reports them, and
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
# D5: identifier-bearing fields the model is never even asked for, and which are
# stripped from its output defensively. The set now has ONE definition, in the
# shared :mod:`builder.tools.field_kinds` (#375) — it previously existed here and
# byte-identically in ``guidance``, free to drift apart. Aliased rather than
# renamed so the many references below (and any external caller) are unaffected.
_IDENTIFIER_SCALAR_FIELDS: frozenset[str] = IDENTIFIER_FIELDS

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
    schema["properties"] = {key: spec for key, spec in props.items() if key not in _EXCLUDED_FIELDS}
    # The function name langchain derives for the structured-output tool. Must be
    # a valid identifier; ``entity_type`` is always a CamelCase type name.
    schema["title"] = f"{entity_type}Fields"
    return schema


def _strip_identifiers(fields: dict[str, Any]) -> dict[str, Any]:
    """Defensively drop any identifier/reference field from model output (D5)."""
    return {key: value for key, value in fields.items() if key not in _EXCLUDED_FIELDS}


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
    "confirm in 'notes'. "
    # #258: the test/control compounds are very often named ONLY in the data
    # FILENAMES, not in prose — direct the model to mine them from the
    # scanned-files inventory (the legacy ReAct path inferred them this way).
    "IMPORTANT — find the test/control compounds by reading the DATA FILENAMES "
    "in the scanned-files inventory as well as any prose, JSON, or README "
    "bodies. Data files are routinely named after the chemical(s) they hold, "
    "e.g. 'S-VHPS26_P5_Silychristin+Verapamil.xlsx' or 'Diclofenac+BSP.xlsx' — "
    "propose each chemical token (split filename stems on '+', '_', '-', and "
    "spaces; drop plate/well/replicate/date codes and the study accession) as a "
    "separate compound NAME. Propose the chemical names you recognise even when "
    "they appear only in a filename. "
    "Refer to compounds, cell lines, people and "
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

    The compounds in particular are mined from the **data FILENAMES** in the
    scanned-files inventory as well as from prose/JSON/README bodies (#258): data
    files are routinely named after the chemical(s) they hold (e.g.
    ``…_Silychristin+Verapamil.xlsx``), and the legacy ReAct path recovered the
    test articles exactly that way, so the prompt directs the model to propose
    each chemical token in a filename stem as a candidate compound NAME (D5: name
    only — the CAS/CID come later from ``resolve_compound``).

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
                "Remember the test/control compounds are often named only in the "
                "data FILENAMES above — read the chemical names out of those "
                "filenames too, not just the prose. Names only — no identifiers."
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


# ---------------------------------------------------------------------------
# Guidance leaves (Issue #244): the §14.6 HITL tail's per-gap LLM exchange.
#
# These two leaves let the guidance loop turn a cryptic gap into a real
# conversation instead of an ask-and-set loop that stores raw prose:
#   - ``phrase_gap_question`` rephrases the gap (property / entity_type / tier /
#     MIT-FAIR rationale / suggestion) into ONE clear human question with a
#     concrete example — never the raw SHACL / FAIR-indicator text.
#   - ``interpret_gap_reply`` parses the user's free-text reply into a STRUCTURED
#     decision (commit / skip / clarify / from_file) so a musing like "no idea
#     which file you mean" can NEVER become a field value.
#
# Both are pure bounded leaves (a single structured-output call on the drafter
# tier); they do not mutate state and do not orchestrate. The guidance loop owns
# control flow and the commit. D5: the interpret leaf refuses to commit a value
# for an identifier-bearing field — identifiers come from lookups, never the user.
# ---------------------------------------------------------------------------

# The structured decision actions ``interpret_gap_reply`` may return. ``commit``
# carries a clean value; ``clarify`` carries one follow-up question; ``from_file``
# carries an optional filename hint (NEVER a value — D5); ``skip`` covers "I don't
# know" / empty / unusable replies.
_INTERPRET_ACTIONS: frozenset[str] = frozenset({"commit", "skip", "clarify", "from_file"})

_PHRASE_SYSTEM_PROMPT = (
    "You are the conversational guidance assistant for an ISA-Tox RO-Crate "
    "builder. You are given a metadata GAP a validator found (a field, the entity "
    "type it belongs to, why it matters, and a hint). Rephrase it as ONE short, "
    "clear question a non-expert researcher can answer, with a concrete example of "
    "a good answer. NEVER show raw SHACL shapes, FAIR indicator codes, property "
    "IRIs, or validator jargon. Ask only for what the field needs. CRITICAL: when "
    "an entity name is given, the question MUST name that specific entity (e.g. "
    "'What is the CAS Registry Number for Silychristin A?'), never a vague 'this "
    "chemical', 'this protocol', or 'this cell line' — the user must know WHICH "
    "entity you mean. When NO entity name is given, ask GENERICALLY about 'the "
    "<entity type>' (e.g. 'the cell line', 'the compound') and you are EXPLICITLY "
    "FORBIDDEN from inventing a specific name, identifier, accession, or example "
    "value to fill the blank — never make up a concrete cell-line / compound name "
    "or a code the data does not provide. D5: never fabricate a specific name, "
    "identifier, or value the data does not provide."
)

_INTERPRET_SYSTEM_PROMPT = (
    "You interpret a researcher's free-text reply to a metadata question for an "
    "ISA-Tox RO-Crate. Return a STRUCTURED decision, never prose to store. Choose:\n"
    "- 'commit' with a clean, concise 'value' ONLY when the reply clearly supplies "
    "the requested information; rewrite it into a proper field value (do not store "
    "the raw musing).\n"
    "- 'skip' when the reply is 'I don't know', empty, off-topic, or a complaint "
    "(e.g. 'no idea which file you mean'). A skip carries NO value.\n"
    "- 'clarify' with one short follow-up 'question' when the reply is on-topic but "
    "too vague to commit.\n"
    "- 'from_file' with an optional 'filename' hint when the reply says the answer "
    "lives in a file ('it's in README.txt'). Do NOT put the prose in a value.\n"
    "NEVER fabricate identifiers (CAS, InChIKey, SMILES, PubChem CID, ORCID, ROR, "
    "DOI, accessions, ontology codes): for an identifier field, prefer 'skip' — "
    "those are resolved by lookup services, not from the user's text."
)


def _phrase_schema() -> dict[str, Any]:
    """Structured-output schema for the phrasing leaf: one question string."""
    return {
        "title": "GapQuestion",
        "type": "object",
        "description": "One clear human question rephrasing a metadata gap.",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "A single clear question for the user, with a concrete example. "
                    "No SHACL/FAIR/IRI jargon."
                ),
            }
        },
        "required": ["question"],
        "additionalProperties": False,
    }


def _interpret_schema() -> dict[str, Any]:
    """Structured-output schema for the interpret leaf: a typed decision.

    By construction the schema offers no identifier field — ``value`` is a clean
    descriptive value only, ``filename`` is a plain name hint, and a ``commit``
    for an identifier-bearing field is refused downstream (D5).
    """
    return {
        "title": "GapReplyDecision",
        "type": "object",
        "description": (
            "A structured decision interpreting the user's reply. Free-text musings "
            "must never become field values."
        ),
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_INTERPRET_ACTIONS),
                "description": "What to do with the reply.",
            },
            "value": {
                "type": "string",
                "description": (
                    "Only for action='commit': the clean field value rewritten "
                    "from the reply. Never an identifier."
                ),
            },
            "question": {
                "type": "string",
                "description": "Only for action='clarify': one short follow-up.",
            },
            "filename": {
                "type": "string",
                "description": ("Only for action='from_file': an optional file name/path hint."),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }


def _known_fields_block(known_fields: Any) -> str | None:
    """Render the gap's KNOWN fields into a compact ``k=v`` line, or ``None``.

    The guidance loop threads the resolved entity's already-known descriptive
    fields (``known_fields``) so the leaf can ground the question in what is
    already recorded ("…for Silychristin A (a test compound)…") instead of asking
    about a nameless entity. Values are truncated so the block stays bounded.
    """
    if not isinstance(known_fields, dict) or not known_fields:
        return None
    parts: list[str] = []
    for key, value in known_fields.items():
        text = str(value).strip()
        if not text:
            continue
        if len(text) > 80:
            text = text[:80].rstrip() + "…"
        parts.append(f"{key}={text}")
    return "; ".join(parts) if parts else None


def _gap_context_block(gap_context: dict[str, Any]) -> str:
    """Render a gap-context dict into a compact, jargon-light block for a leaf.

    The guidance loop assembles ``gap_context`` (property, entity_type, tier,
    message, suggestion, plus optional crate title/description and — when the gap
    is about a CONCRETE entity — that entity's ``entity_name`` and ``known_fields``,
    #257). We surface the human-meaningful parts (crucially the entity NAME, so the
    leaf phrases a question about *that* entity, never a bare "this chemical"); the
    leaf is told to translate the rest, never to echo raw validator text.
    """
    field = gap_context.get("property") or "this field"
    parts: list[str] = [f"Field: {field}"]
    entity_type = gap_context.get("entity_type")
    if entity_type:
        parts.append(f"Belongs to: {entity_type}")
    # Name the specific entity (#257) so the question can never be a contextless
    # "this chemical / this protocol / this cell line".
    entity_name = gap_context.get("entity_name")
    if entity_name:
        parts.append(f"Entity name: {entity_name}")
    known_block = _known_fields_block(gap_context.get("known_fields"))
    if known_block:
        parts.append(f"Already known: {known_block}")
    tier = gap_context.get("tier")
    if tier:
        parts.append(f"Importance: {tier}")
    message = gap_context.get("message")
    if message:
        parts.append(f"Why it matters: {message}")
    suggestion = gap_context.get("suggestion")
    if suggestion:
        parts.append(f"Hint: {suggestion}")
    return "\n".join(parts)


def phrase_gap_question(
    gap_context: dict[str, Any],
    *,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
) -> str:
    """Rephrase a metadata gap into ONE clear human question (#244).

    A pure bounded leaf: a SINGLE structured-output call on the drafter tier
    (``_build_chat_model(role="drafter")``) that turns ``gap_context`` (property,
    entity_type, tier, MIT/FAIR rationale, suggestion) into one short question a
    non-expert can answer, with a concrete example. It never echoes raw SHACL
    shapes / FAIR indicator codes / property IRIs.

    Args:
        gap_context: The guidance loop's per-gap context dict (``property``,
            ``entity_type``, ``tier``, ``message``, ``suggestion``, ...).
        model: Optional explicit model override; ``None`` resolves the drafter tier.
        usage_sink: Optional token-usage callback (see :func:`draft_entity_fields`).

    Returns:
        The phrased question string, or ``""`` when the model returns nothing
        usable (the caller then falls back to its deterministic prompt).
    """
    llm = _build_chat_model(model=model, role="drafter")
    messages = [
        SystemMessage(content=_PHRASE_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                "Rephrase this metadata gap as one clear question for the user, "
                "with a concrete example of a good answer:\n\n"
                f"{_gap_context_block(gap_context)}"
            )
        ),
    ]
    result = _invoke_structured_with_usage(llm, _phrase_schema(), messages, usage_sink)
    if isinstance(result, dict):
        question = result.get("question")
        if isinstance(question, str) and question.strip():
            return question.strip()
    return ""


def interpret_gap_reply(
    question: str,
    reply: str,
    gap_context: dict[str, Any],
    *,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
) -> dict[str, Any]:
    """Interpret a free-text reply into a STRUCTURED decision (#244).

    A pure bounded leaf: a SINGLE structured-output call on the drafter tier that
    maps the user's ``reply`` to one of ``{action: "commit", value}`` |
    ``{action: "skip"}`` | ``{action: "clarify", question}`` |
    ``{action: "from_file", filename?}``. Free-text musings (e.g. "no idea which
    file you mean") map to ``skip`` — they are NEVER stored as field values.

    D5: identifiers come from lookups, never the user's prose. A ``commit`` whose
    field is identifier-bearing (:data:`_IDENTIFIER_SCALAR_FIELDS`) is refused and
    coerced to ``skip``. A malformed/unknown action, or a ``commit`` with no usable
    value, is also coerced to ``skip`` — the safe default that commits nothing.

    Args:
        question: The phrased question the user was answering (context for the leaf).
        reply: The user's raw free-text reply.
        gap_context: The per-gap context dict (notably ``property`` for the D5 guard).
        model: Optional explicit model override; ``None`` resolves the drafter tier.
        usage_sink: Optional token-usage callback (see :func:`draft_entity_fields`).

    Returns:
        A normalised decision dict whose ``action`` is one of
        :data:`_INTERPRET_ACTIONS`; ``commit`` carries a non-empty ``value`` and
        ``clarify`` carries a non-empty ``question`` (else both coerce to ``skip``).
    """
    llm = _build_chat_model(model=model, role="drafter")
    messages = [
        SystemMessage(content=_INTERPRET_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Question asked:\n{question}\n\n"
                f"User's reply:\n{reply}\n\n"
                "Gap context:\n"
                f"{_gap_context_block(gap_context)}\n\n"
                "Return the structured decision."
            )
        ),
    ]
    result = _invoke_structured_with_usage(llm, _interpret_schema(), messages, usage_sink)
    return _normalise_interpretation(result, gap_context)


def _normalise_interpretation(result: Any, gap_context: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw interpret result into a safe, well-formed decision (D5).

    Guards (the model output is never trusted as-is):
      * an unknown/absent action -> ``skip``;
      * ``commit`` with no usable (non-whitespace) value -> ``skip``;
      * ``commit`` for an identifier-bearing field -> ``skip`` (identifiers come
        from lookups, never the user — D5);
      * ``clarify`` with no usable question -> ``skip``;
      * ``from_file`` keeps only a clean ``filename`` hint and NEVER a value.
    """
    decision = result if isinstance(result, dict) else {}
    action = decision.get("action")
    if action not in _INTERPRET_ACTIONS:
        return {"action": "skip"}

    if action == "commit":
        value = decision.get("value")
        if not isinstance(value, str) or not value.strip():
            return {"action": "skip"}
        field = _local_property_name(gap_context.get("property"))
        if field in _IDENTIFIER_SCALAR_FIELDS:
            # D5: never let the user's prose become an identifier value.
            return {"action": "skip"}
        return {"action": "commit", "value": value.strip()}

    if action == "clarify":
        follow_up = decision.get("question")
        if not isinstance(follow_up, str) or not follow_up.strip():
            return {"action": "skip"}
        return {"action": "clarify", "question": follow_up.strip()}

    if action == "from_file":
        filename = decision.get("filename")
        out: dict[str, Any] = {"action": "from_file"}
        if isinstance(filename, str) and filename.strip():
            out["filename"] = filename.strip()
        return out

    return {"action": "skip"}


def _local_property_name(iri: str | None) -> str:
    """Local part of a property IRI (after the last ``/`` or ``#``).

    Mirrors the guidance loop's ``_local_name`` so the D5 identifier check sees the
    same field token the loop would commit to (e.g. ``.../cas`` -> ``cas``).
    """
    if not iri:
        return ""
    return iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


# ---------------------------------------------------------------------------
# extract_field_from_file — the file-extraction leaf (Issue #257, fix C)
#
# When the user points the guidance loop at a file ("the CAS number is in
# assay-metadata.xlsx"), the loop READS the file (via file_readers) and calls
# this leaf to extract the requested field value from the file text — instead of
# logging a hint and skipping. It is a single bounded structured-output call on
# the drafter tier; it returns a clean value or an empty string when the file
# does not support the field. D5: an identifier-bearing field is NEVER extracted
# from file text (those come from lookups), so the leaf returns "" for one.
# ---------------------------------------------------------------------------

_EXTRACT_FILE_SYSTEM_PROMPT = (
    "You extract ONE metadata field value from the text of a research file for an "
    "ISA-Tox RO-Crate. You are given the field name, the entity it belongs to, and "
    "the file's text. Return the clean value for that field if — and only if — the "
    "file clearly supplies it; rewrite it into a proper, concise field value. If "
    "the file does not contain the answer, return an EMPTY value (do not guess). "
    "NEVER fabricate or extract identifiers (CAS, InChIKey, SMILES, PubChem CID, "
    "ORCID, ROR, DOI, accessions, ontology codes): those are resolved by lookup "
    "services, so for an identifier field return an empty value."
)


def _extract_file_schema() -> dict[str, Any]:
    """Structured-output schema for the file-extraction leaf: one value string."""
    return {
        "title": "ExtractedFieldValue",
        "type": "object",
        "description": (
            "The value of one metadata field extracted from a file's text, or "
            "empty when the file does not supply it. Never an identifier."
        ),
        "properties": {
            "value": {
                "type": "string",
                "description": (
                    "The clean field value extracted from the file text, or an "
                    "empty string when the file does not contain it."
                ),
            }
        },
        "required": ["value"],
        "additionalProperties": False,
    }


def extract_field_from_file(
    field: str,
    file_text: str,
    gap_context: dict[str, Any],
    *,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
) -> str:
    """Extract the value of ``field`` from ``file_text`` for a gap (#257, fix C).

    A pure bounded leaf: a SINGLE structured-output call on the drafter tier
    (``_build_chat_model(role="drafter")``) that reads the (already-read,
    size-capped) text of a file the user pointed at and returns the clean value
    for the requested ``field``, or ``""`` when the file does not support it. It
    does not mutate state and does not read disk (the guidance loop reads the file
    via ``file_readers`` and hands the text in).

    D5: identifiers come from lookups, never from extracting prose/file text. When
    ``field`` (or the gap's ``property``) is identifier-bearing
    (:data:`_IDENTIFIER_SCALAR_FIELDS`) the leaf short-circuits to ``""`` so an
    identifier is never lifted out of a file — the loop verifies it via a lookup
    instead.

    Args:
        field: The local field name being filled (e.g. ``"description"``).
        file_text: The file's (bounded) text content to extract from.
        gap_context: The per-gap context dict (entity_type, property, ...), used
            for grounding and the D5 identifier guard.
        model: Optional explicit model override; ``None`` resolves the drafter tier.
        usage_sink: Optional token-usage callback (see :func:`draft_entity_fields`).

    Returns:
        The extracted clean value, or ``""`` when the file does not supply it (or
        the field is identifier-bearing — D5).
    """
    # D5: never extract an identifier from file text — those come from lookups.
    target = _local_property_name(field) or _local_property_name(gap_context.get("property"))
    if target in _IDENTIFIER_SCALAR_FIELDS:
        return ""
    if not file_text or not file_text.strip():
        return ""

    llm = _build_chat_model(model=model, role="drafter")
    messages = [
        SystemMessage(content=_EXTRACT_FILE_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Field to extract: {field}\n"
                f"{_gap_context_block(gap_context)}\n\n"
                "File text:\n"
                f"{file_text}\n\n"
                "Return the field's value if the file clearly supplies it, else an "
                "empty value. Never an identifier."
            )
        ),
    ]
    result = _invoke_structured_with_usage(llm, _extract_file_schema(), messages, usage_sink)
    if isinstance(result, dict):
        value = result.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


__all__ = [
    "draft_entity_fields",
    "extract_field_from_file",
    "extract_plan",
    "interpret_gap_reply",
    "phrase_gap_question",
]
