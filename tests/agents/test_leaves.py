"""Tests for the bounded drafter-leaf (Issue #179, task 2).

`builder.agents.leaves.draft_entity_fields` is the cheap-model "bounded
extraction" primitive the §14 deterministic pipeline calls at its leaves:
free-text context in -> a structured dict of one entity's fields out, in a
SINGLE LLM call on the drafter tier (`_build_chat_model(role="drafter")`),
constrained by `_crate_mapping.draft_hints_schema(entity_type)` via the model's
structured-output / function-calling.

These tests are OFFLINE: the chat model is mocked with a fake that records the
build call and returns canned structured output. No real model, no network.

Contracts pinned here:
  1. The leaf builds the chat model on the *drafter* tier (`role="drafter"`).
  2. It calls structured output bound to the entity's hints schema, once.
  3. The returned dict validates against `draft_hints_schema(entity_type)`.
  4. It works for several entity types (MolecularEntity, Study, ...).
  5. D5: identifier fields are never fabricated — a model that hallucinates a
     CAS number / ORCID / DOI has those stripped from the result.
"""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest

from builder.agents import leaves
from builder.tools._crate_mapping import draft_hints_schema


class _FakeStructuredRunnable:
    """The runnable returned by `model.with_structured_output(schema, ...)`.

    Records the messages it was invoked with and returns canned output. When the
    leaf binds with ``include_raw=True`` (the usage-capture path) it returns the
    langchain-shaped ``{"raw": AIMessage, "parsed": ..., "parsing_error": None}``
    so the leaf can mine ``usage_metadata`` off the raw message; otherwise it
    returns the bare parsed dict (the legacy contract).
    """

    def __init__(self, parent: "FakeChatModel", *, include_raw: bool = False) -> None:
        self._parent = parent
        self._include_raw = include_raw

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._parent.invoke_calls.append(messages)
        if self._include_raw:
            return {
                "raw": self._parent.raw_message(),
                "parsed": self._parent.canned_output,
                "parsing_error": None,
            }
        return self._parent.canned_output


class FakeChatModel:
    """A fake LangChain chat model recording structured-output usage.

    `with_structured_output(schema, include_raw=...)` records the schema (and the
    ``include_raw`` flag) and returns a runnable whose `.invoke(...)` yields
    `canned_output`. Optionally carries a ``usage_metadata`` payload so the
    usage-capture path can be exercised entirely offline.
    """

    def __init__(
        self,
        canned_output: dict[str, Any],
        *,
        usage_metadata: dict[str, Any] | None = None,
        model_name: str | None = None,
    ) -> None:
        self.canned_output = canned_output
        self._usage_metadata = usage_metadata
        self._model_name = model_name
        self.structured_schemas: list[Any] = []
        self.invoke_calls: list[Any] = []
        self.include_raw_flags: list[bool] = []

    def raw_message(self) -> Any:
        """Build the AIMessage the structured-output runnable returns as ``raw``."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(content="", usage_metadata=self._usage_metadata)
        if self._model_name is not None:
            msg.response_metadata = {"model_name": self._model_name}
        return msg

    def with_structured_output(
        self, schema: Any, *, include_raw: bool = False, **kwargs: Any
    ) -> _FakeStructuredRunnable:
        self.structured_schemas.append(schema)
        self.include_raw_flags.append(include_raw)
        return _FakeStructuredRunnable(self, include_raw=include_raw)


@pytest.fixture(autouse=True)
def _patch_build_chat_model(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch `_build_chat_model` in the leaves module to capture its kwargs.

    Returns a mutable record dict the test can read (the role requested, the
    model passed) and write (`record["model"]` -> the FakeChatModel to return).
    """
    record: dict[str, Any] = {"calls": [], "model": FakeChatModel({})}

    def _fake_build(*args: Any, **kwargs: Any) -> FakeChatModel:
        record["calls"].append(kwargs)
        return record["model"]

    monkeypatch.setattr(leaves, "_build_chat_model", _fake_build)
    return record


class TestDrafterTier:
    """The leaf must run on the cheap drafter tier."""

    def test_requests_drafter_role(self, _patch_build_chat_model: dict[str, Any]) -> None:
        rec = _patch_build_chat_model
        rec["model"] = FakeChatModel({"name": "Acetaminophen"})

        leaves.draft_entity_fields("MolecularEntity", "A study of acetaminophen.")

        assert rec["calls"], "the leaf must build a chat model"
        assert all(c.get("role") == "drafter" for c in rec["calls"]), (
            "the leaf must build the chat model on the drafter tier (role='drafter')"
        )

    def test_explicit_model_is_forwarded(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        rec = _patch_build_chat_model
        rec["model"] = FakeChatModel({"name": "Acetaminophen"})

        leaves.draft_entity_fields(
            "MolecularEntity", "context", model="gpt-4o-mini"
        )

        assert rec["calls"][0].get("model") == "gpt-4o-mini"


class TestStructuredOutput:
    """The leaf must use structured output bound once to the hints schema."""

    def test_uses_structured_output_once(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({"name": "Acetaminophen"})
        _patch_build_chat_model["model"] = fake

        leaves.draft_entity_fields("MolecularEntity", "context")

        assert len(fake.structured_schemas) == 1, "exactly one structured-output bind"
        assert len(fake.invoke_calls) == 1, "exactly one model invocation (a leaf)"

    def test_single_model_invocation_for_study(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({"name": "Toxicity study"})
        _patch_build_chat_model["model"] = fake

        leaves.draft_entity_fields("Study", "A study of liver toxicity.")

        assert len(fake.invoke_calls) == 1


class TestOutputValidatesAgainstSchema:
    """The returned dict must validate against draft_hints_schema(entity_type)."""

    def test_molecular_entity_output_validates(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"name": "Acetaminophen", "molecular_formula": "C8H9NO2"}
        )

        out = leaves.draft_entity_fields("MolecularEntity", "context")

        assert isinstance(out, dict)
        jsonschema.validate(out, draft_hints_schema("MolecularEntity"))
        assert out["name"] == "Acetaminophen"

    def test_study_output_validates(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"name": "Liver tox study", "description": "Hepatotoxicity assays."}
        )

        out = leaves.draft_entity_fields("Study", "context")

        jsonschema.validate(out, draft_hints_schema("Study"))
        assert out["name"] == "Liver tox study"


class TestD5NoFabricatedIdentifiers:
    """D5: identifiers come from lookups, never invention.

    A model that hallucinates an identifier (CAS, ORCID, DOI, accession, ...)
    must have those fields dropped — the leaf leaves them empty rather than
    propagating a guessed id downstream.
    """

    def test_molecular_entity_strips_fabricated_identifier(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        # The model fabricates a CAS number, InChIKey, SMILES and PubChem CID.
        _patch_build_chat_model["model"] = FakeChatModel(
            {
                "name": "Acetaminophen",
                "identifier": "103-90-2",
                "inchikey": "RZVAJINKPMORJF-UHFFFAOYSA-N",
                "smiles": "CC(=O)Nc1ccc(O)cc1",
                "pubchem_cid": "1983",
            }
        )

        out = leaves.draft_entity_fields("MolecularEntity", "context")

        assert out.get("name") == "Acetaminophen"
        for ident in ("identifier", "inchikey", "smiles", "pubchem_cid"):
            assert ident not in out or not out[ident], (
                f"D5 violated: fabricated identifier field {ident!r} leaked through"
            )

    def test_person_strips_fabricated_orcid(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"name": "Jane Doe", "orcid": "0000-0002-1825-0097"}
        )

        out = leaves.draft_entity_fields("Person", "context")

        assert out.get("name") == "Jane Doe"
        assert not out.get("orcid"), "D5 violated: fabricated ORCID leaked through"

    def test_publication_strips_fabricated_doi(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"name": "A paper", "doi": "10.1234/fake", "identifier": "10.1234/fake"}
        )

        out = leaves.draft_entity_fields("Publication", "context")

        assert out.get("name") == "A paper"
        assert not out.get("doi")
        assert not out.get("identifier")


class TestSchemaExclusion:
    """The structured-output schema must not even offer identifier fields.

    Pushing D5 into the schema (not just post-filtering) means the model is
    never asked to produce an identifier, which is the strongest guard.
    """

    def test_identifier_fields_absent_from_structured_schema(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({"name": "Acetaminophen"})
        _patch_build_chat_model["model"] = fake

        leaves.draft_entity_fields("MolecularEntity", "context")

        schema = fake.structured_schemas[0]
        props = schema.get("properties", {})
        for ident in ("identifier", "inchikey", "smiles", "pubchem_cid"):
            assert ident not in props, (
                f"identifier field {ident!r} must not be offered to the model (D5)"
            )
        assert "name" in props, "non-identifier fields must still be offered"

    def test_structured_schema_is_convertible_to_a_tool(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        # A raw JSON-schema dict needs a top-level `title` to be usable as a
        # function-calling tool name by langchain. Without it, the real model
        # call raises ValueError("...must have a top-level 'title' key...").
        from langchain_core.utils.function_calling import convert_to_openai_tool

        fake = FakeChatModel({"name": "Acetaminophen"})
        _patch_build_chat_model["model"] = fake

        leaves.draft_entity_fields("MolecularEntity", "context")

        schema = fake.structured_schemas[0]
        assert schema.get("title"), "structured-output schema must carry a title"
        tool = convert_to_openai_tool(schema)  # must not raise
        assert tool["function"]["name"]


class TestUnknownEntityType:
    """An unknown entity type still returns a dict (open schema fallback)."""

    def test_unknown_type_returns_dict(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel({"name": "x"})

        out = leaves.draft_entity_fields("NotAType", "context")

        assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# extract_plan — the Stage A candidate-plan extractor (Issue #179)
#
# `extract_plan` is the *whole-document* sibling of `draft_entity_fields`: one
# bounded structured-output call on the drafter tier that reads scanned research
# docs and proposes a CANDIDATE PLAN (study, compounds, cell lines, the
# CellCulture→Exposure→EndpointReadout→DataAnalysis process chain, AOPs, people,
# publications, files, free-text notes). It proposes WHAT EXISTS by name; it must
# never fabricate identifiers (D5) — real CAS/CID/InChIKey/Cellosaurus/ORCID/DOI
# come later from deterministic lookups, not from this leaf.
# ---------------------------------------------------------------------------


# A doc-like context the model could plausibly turn into a populated plan.
_DOC_CONTEXT = """
Investigation: Hepatotoxicity of acetaminophen in renal cells.

Study: We exposed MDCK cells to acetaminophen (test compound) and a DMSO
vehicle control, then read out viability and analysed dose-response.

Cells were cultured in DMEM, exposed for 24h across a concentration series,
viability was measured on a plate reader, and the data were analysed in R.

Authors: Jane Doe (Acme University). Reference: "AAP renal tox", Doe et al.
This relates to AOP 144.

Files: plate_raw.csv (raw), results.xlsx (processed), conditions.csv.
"""


class TestExtractPlanDrafterTier:
    """The plan extractor must run on the cheap drafter tier (mirrors the leaf)."""

    def test_requests_drafter_role(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        rec = _patch_build_chat_model
        rec["model"] = FakeChatModel({"study": {"name": "x"}})

        leaves.extract_plan(_DOC_CONTEXT)

        assert rec["calls"], "the leaf must build a chat model"
        assert all(c.get("role") == "drafter" for c in rec["calls"]), (
            "extract_plan must build the chat model on the drafter tier"
        )

    def test_explicit_model_is_forwarded(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        rec = _patch_build_chat_model
        rec["model"] = FakeChatModel({})

        leaves.extract_plan("context", model="gpt-4o-mini")

        assert rec["calls"][0].get("model") == "gpt-4o-mini"


class TestExtractPlanStructuredOutput:
    """One bounded structured-output bind + one invocation (a leaf)."""

    def test_single_structured_output_call(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({"study": {"name": "x"}})
        _patch_build_chat_model["model"] = fake

        leaves.extract_plan(_DOC_CONTEXT)

        assert len(fake.structured_schemas) == 1, "exactly one structured-output bind"
        assert len(fake.invoke_calls) == 1, "exactly one model invocation (a leaf)"


class TestExtractPlanShape:
    """A doc-like context yields a populated plan with the right shape."""

    def test_populated_plan_round_trips(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {
                "study": {"name": "AAP renal tox", "description": "Hepatotox study."},
                "compounds": [
                    {"name": "Acetaminophen", "role": "test"},
                    {"name": "DMSO", "role": "control"},
                ],
                "cell_lines": [{"name": "MDCK"}],
                "process_chain": [
                    {"process_type": "CellCulture", "name": "Culture"},
                    {"process_type": "Exposure", "name": "Expose"},
                    {"process_type": "EndpointReadout", "name": "Readout"},
                    {"process_type": "DataAnalysis", "name": "Analyse"},
                ],
                "aops": [{"aop_id": "144"}],
                "people": [{"name": "Jane Doe", "affiliation_name": "Acme University"}],
                "publications": [{"title": "AAP renal tox"}],
                "files": [
                    {"path": "plate_raw.csv", "role": "raw"},
                    {"path": "results.xlsx", "role": "processed"},
                    {"path": "conditions.csv", "role": "condition_table"},
                ],
                "notes": "Concentration series not fully specified.",
            }
        )

        plan = leaves.extract_plan(_DOC_CONTEXT)

        assert isinstance(plan, dict)
        assert plan["study"]["name"] == "AAP renal tox"
        assert {c["name"] for c in plan["compounds"]} == {"Acetaminophen", "DMSO"}
        assert plan["cell_lines"][0]["name"] == "MDCK"
        assert [p["process_type"] for p in plan["process_chain"]] == [
            "CellCulture",
            "Exposure",
            "EndpointReadout",
            "DataAnalysis",
        ]
        assert plan["aops"][0]["aop_id"] == "144"
        assert plan["people"][0]["name"] == "Jane Doe"
        assert plan["publications"][0]["title"] == "AAP renal tox"
        assert {f["role"] for f in plan["files"]} == {
            "raw",
            "processed",
            "condition_table",
        }
        assert plan["notes"]


def _prompt_text(fake: FakeChatModel) -> str:
    """Concatenated text of every message the model was invoked with.

    The leaf builds a ``[SystemMessage, HumanMessage]`` list; ``FakeChatModel``
    records that list in ``invoke_calls``. Flattening it to one lowercase string
    lets a test assert WHAT INSTRUCTIONS reach the model (e.g. "mine compound
    names from the data filenames") without coupling to message ordering.
    """
    chunks: list[str] = []
    for messages in fake.invoke_calls:
        for msg in messages:
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                chunks.append(content)
    return "\n".join(chunks).lower()


class TestExtractPlanMinesCompoundsFromFilenames:
    """#258: the bounded plan extractor must propose candidate compound NAMES
    inferred from the DATA FILENAMES (and JSON/README bodies), not only from
    prose — the legacy ReAct path got 22 compounds off filenames like
    ``…_P5_Silychristin+Verapamil.xlsx`` while the bounded leaf got 0 because its
    prompt never told the model to read names out of the filenames inventory.

    These tests pin the PROMPT contract (the model is *instructed* to mine
    compound names from filenames + bodies, names only — D5), offline, so the
    real DeepSeek-flash call is steered the same way without any network.
    """

    # A scanned-files inventory whose ONLY compound signal is in the filenames —
    # the S-VHPS26 shape that produced 0 compounds on the default path.
    _FILENAME_CONTEXT = (
        "Title: S-VHPS26 transporter interaction screen\n\n"
        "Scanned files:\n"
        "- S-VHPS26_P5_Silychristin+Verapamil.xlsx\n"
        "- S-VHPS26_Diclofenac+BSP.xlsx\n"
        "- conditions.csv"
    )

    def test_prompt_instructs_mining_compound_names_from_filenames(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({})
        _patch_build_chat_model["model"] = fake

        leaves.extract_plan(self._FILENAME_CONTEXT)

        text = _prompt_text(fake)
        # The model must be told to infer compound names from the FILENAMES …
        assert "filename" in text, (
            "extract_plan must instruct the model to read candidate compound "
            "names from the data filenames (#258)"
        )
        # … and that the inferred items are COMPOUND candidates specifically.
        assert "compound" in text

    def test_prompt_keeps_names_only_discipline(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        """The filename-mining instruction must not weaken D5: names only."""
        fake = FakeChatModel({})
        _patch_build_chat_model["model"] = fake

        leaves.extract_plan(self._FILENAME_CONTEXT)

        text = _prompt_text(fake)
        # The names-only / no-identifier discipline is still asserted in the prompt.
        assert "name" in text
        assert ("no identifier" in text) or ("never include identifiers" in text) or (
            "identifiers of any kind" in text
        ), "the filename-mining prompt must still forbid identifiers (D5)"

    def test_filename_derived_compound_plan_round_trips(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        """A model that (correctly) returns filename-derived compound NAMES has
        them preserved by the leaf (names only — no fabricated identifiers)."""
        _patch_build_chat_model["model"] = FakeChatModel(
            {
                "compounds": [
                    {"name": "Silychristin", "role": "test"},
                    {"name": "Verapamil", "role": "test"},
                    {"name": "Diclofenac", "role": "test"},
                    {"name": "BSP", "role": "test"},
                ]
            }
        )

        plan = leaves.extract_plan(self._FILENAME_CONTEXT)

        names = {c["name"] for c in plan.get("compounds", [])}
        assert names == {"Silychristin", "Verapamil", "Diclofenac", "BSP"}
        # D5: still no fabricated identifiers on any filename-derived compound.
        for compound in plan["compounds"]:
            for ident in ("cas", "pubchem_cid", "inchikey", "smiles", "@id"):
                assert ident not in compound


class TestExtractPlanEmptyContext:
    """An uninformative context yields an empty-but-valid plan, not fabrication."""

    def test_empty_context_returns_empty_plan(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        # An honest model returns nothing it can support from an empty context.
        _patch_build_chat_model["model"] = FakeChatModel({})

        plan = leaves.extract_plan("")

        assert isinstance(plan, dict)
        # No fabricated entities: every list-valued section is empty/absent.
        for key in (
            "compounds",
            "cell_lines",
            "process_chain",
            "aops",
            "people",
            "publications",
            "files",
        ):
            assert not plan.get(key), f"empty context must not fabricate {key!r}"


class TestExtractPlanD5NoIdentifiersInSchema:
    """D5: the schema the model sees must never offer an identifier field.

    Pushing D5 into the schema (not just post-filtering) is the strongest guard:
    the model is never even asked to produce a CAS/CID/InChIKey/SMILES/
    Cellosaurus accession/ORCID/DOI/@id.
    """

    def test_no_identifier_fields_anywhere_in_schema(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({})
        _patch_build_chat_model["model"] = fake

        leaves.extract_plan(_DOC_CONTEXT)

        schema = fake.structured_schemas[0]
        flat = _flatten_keys(schema)
        for ident in (
            "cas",
            "casrn",
            "cas_number",
            "cid",
            "pubchem_cid",
            "inchikey",
            "smiles",
            "accession",
            "cellosaurus",
            "orcid",
            "doi",
            "identifier",
            "@id",
            "id",
        ):
            assert ident not in flat, (
                f"D5: identifier field {ident!r} must not appear in the plan schema"
            )
        # The schema must be usable as a function-calling tool (carries a title).
        assert schema.get("title"), "plan schema must carry a title"

    def test_schema_is_convertible_to_a_tool(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        fake = FakeChatModel({})
        _patch_build_chat_model["model"] = fake

        leaves.extract_plan(_DOC_CONTEXT)

        tool = convert_to_openai_tool(fake.structured_schemas[0])  # must not raise
        assert tool["function"]["name"]


class TestExtractPlanD5StripsFabricatedIdentifiers:
    """D5 defense-in-depth: an adversarial model that slips identifiers into the
    plan output has them stripped from every section."""

    def test_identifiers_stripped_from_all_sections(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {
                "study": {"name": "S", "identifier": "FAKE-1"},
                "compounds": [
                    {
                        "name": "Acetaminophen",
                        "role": "test",
                        "cas": "103-90-2",
                        "pubchem_cid": "1983",
                        "inchikey": "RZVAJINKPMORJF-UHFFFAOYSA-N",
                        "smiles": "CC(=O)Nc1ccc(O)cc1",
                        "@id": "https://pubchem.ncbi.nlm.nih.gov/compound/1983",
                    }
                ],
                "cell_lines": [{"name": "MDCK", "accession": "CVCL_0027"}],
                "people": [
                    {"name": "Jane Doe", "orcid": "0000-0002-1825-0097"}
                ],
                "publications": [{"title": "P", "doi": "10.1234/fake"}],
            }
        )

        plan = leaves.extract_plan(_DOC_CONTEXT)

        # Descriptive fields survive; identifiers do not.
        assert plan["study"]["name"] == "S"
        assert "identifier" not in plan["study"]

        compound = plan["compounds"][0]
        assert compound["name"] == "Acetaminophen"
        assert compound["role"] == "test"
        for ident in ("cas", "pubchem_cid", "inchikey", "smiles", "@id"):
            assert ident not in compound, f"D5: {ident!r} leaked into a compound"

        assert plan["cell_lines"][0]["name"] == "MDCK"
        assert "accession" not in plan["cell_lines"][0]

        assert plan["people"][0]["name"] == "Jane Doe"
        assert "orcid" not in plan["people"][0]

        assert plan["publications"][0]["title"] == "P"
        assert "doi" not in plan["publications"][0]


# ---------------------------------------------------------------------------
# Token-usage capture (Issue #221)
#
# The deterministic pipeline's leaves make their own chat-model calls; without
# instrumentation their token usage is discarded and the eval `--arch pipeline`
# arm records 0. The leaves accept an optional `usage_sink` callback: when given,
# the leaf binds structured output with `include_raw=True`, mines
# `(input_tokens, output_tokens, model_name)` off the raw AIMessage (the SAME
# provider-agnostic source the ReAct model node uses), and reports it through the
# sink. With no sink the behaviour is unchanged (the legacy bare-parsed contract).
# ---------------------------------------------------------------------------


class TestDraftEntityFieldsUsageCapture:
    def test_usage_sink_receives_token_usage(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"name": "Acetaminophen"},
            usage_metadata={"input_tokens": 120, "output_tokens": 35, "total_tokens": 155},
            model_name="gpt-4o-mini",
        )
        captured: list[tuple[Any, Any, Any]] = []

        out = leaves.draft_entity_fields(
            "MolecularEntity",
            "context",
            usage_sink=lambda i, o, m: captured.append((i, o, m)),
        )

        assert out["name"] == "Acetaminophen"
        assert captured == [(120, 35, "gpt-4o-mini")]

    def test_usage_sink_binds_structured_output_with_include_raw(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel(
            {"name": "x"},
            usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )
        _patch_build_chat_model["model"] = fake

        leaves.draft_entity_fields("Study", "context", usage_sink=lambda *_: None)

        assert fake.include_raw_flags == [True]

    def test_no_usage_sink_keeps_legacy_contract(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        # Without a sink the leaf must NOT request include_raw and must still
        # return the bare parsed dict (backward compatible).
        fake = FakeChatModel({"name": "Acetaminophen"})
        _patch_build_chat_model["model"] = fake

        out = leaves.draft_entity_fields("MolecularEntity", "context")

        assert out["name"] == "Acetaminophen"
        assert fake.include_raw_flags == [False]


class TestExtractPlanUsageCapture:
    def test_usage_sink_receives_token_usage(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"study": {"name": "S"}},
            usage_metadata={"input_tokens": 500, "output_tokens": 80, "total_tokens": 580},
            model_name="gpt-4o-mini",
        )
        captured: list[tuple[Any, Any, Any]] = []

        plan = leaves.extract_plan(
            _DOC_CONTEXT,
            usage_sink=lambda i, o, m: captured.append((i, o, m)),
        )

        assert plan["study"]["name"] == "S"
        assert captured == [(500, 80, "gpt-4o-mini")]

    def test_no_usage_sink_keeps_legacy_contract(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({"study": {"name": "S"}})
        _patch_build_chat_model["model"] = fake

        plan = leaves.extract_plan(_DOC_CONTEXT)

        assert plan["study"]["name"] == "S"
        assert fake.include_raw_flags == [False]


# ---------------------------------------------------------------------------
# phrase_gap_question / interpret_gap_reply — the guidance leaves (Issue #244)
#
# The §14.6 guidance tail's per-gap step is a small bounded LLM exchange:
#   - `phrase_gap_question(gap_context)` turns a cryptic gap (property,
#     entity_type, MIT/FAIR rationale, suggestion) into ONE clear human question
#     with a concrete example — never the raw SHACL/indicator text.
#   - `interpret_gap_reply(question, reply, gap_context)` parses the user's
#     free-text reply into a STRUCTURED decision so musings never become field
#     values. Both are bounded structured-output calls on the drafter tier.
# ---------------------------------------------------------------------------


# A gap-context double the guidance loop assembles for a single gap.
_GAP_CONTEXT = {
    "property": "description",
    "entity_type": "Study",
    "tier": "MUST",
    "message": "Study MUST have a description.",
    "suggestion": "A free-text study description.",
}


class TestPhraseGapQuestionDrafterTier:
    """Phrasing runs on the cheap drafter tier and is a single bounded call."""

    def test_requests_drafter_role(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        rec = _patch_build_chat_model
        rec["model"] = FakeChatModel({"question": "What does this study examine?"})

        leaves.phrase_gap_question(_GAP_CONTEXT)

        assert rec["calls"], "the leaf must build a chat model"
        assert all(c.get("role") == "drafter" for c in rec["calls"]), (
            "phrase_gap_question must build the chat model on the drafter tier"
        )

    def test_single_structured_output_call(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({"question": "What does this study examine?"})
        _patch_build_chat_model["model"] = fake

        leaves.phrase_gap_question(_GAP_CONTEXT)

        assert len(fake.structured_schemas) == 1, "exactly one structured-output bind"
        assert len(fake.invoke_calls) == 1, "exactly one model invocation (a leaf)"


class TestPhraseGapQuestionShape:
    """The leaf returns one human question string."""

    def test_returns_the_question_string(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"question": "In one sentence, what does this study examine?"}
        )

        question = leaves.phrase_gap_question(_GAP_CONTEXT)

        assert isinstance(question, str)
        assert question == "In one sentence, what does this study examine?"

    def test_empty_model_output_returns_empty_string(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        # A model that returns nothing usable -> empty string; the caller falls
        # back to the deterministic prompt rather than asking a blank question.
        _patch_build_chat_model["model"] = FakeChatModel({})

        question = leaves.phrase_gap_question(_GAP_CONTEXT)

        assert question == ""


class TestInterpretGapReplyDrafterTier:
    """Interpretation runs on the cheap drafter tier and is a single call."""

    def test_requests_drafter_role(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        rec = _patch_build_chat_model
        rec["model"] = FakeChatModel({"action": "skip"})

        leaves.interpret_gap_reply("Question?", "I don't know", _GAP_CONTEXT)

        assert rec["calls"], "the leaf must build a chat model"
        assert all(c.get("role") == "drafter" for c in rec["calls"]), (
            "interpret_gap_reply must build the chat model on the drafter tier"
        )

    def test_single_structured_output_call(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({"action": "commit", "value": "x"})
        _patch_build_chat_model["model"] = fake

        leaves.interpret_gap_reply("Question?", "the value is x", _GAP_CONTEXT)

        assert len(fake.structured_schemas) == 1, "exactly one structured-output bind"
        assert len(fake.invoke_calls) == 1, "exactly one model invocation (a leaf)"


class TestInterpretGapReplyDecision:
    """The leaf returns a STRUCTURED decision, not free text."""

    def test_commit_returns_clean_value(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"action": "commit", "value": "A hepatotoxicity dose-response study."}
        )

        decision = leaves.interpret_gap_reply(
            "What does this study examine?",
            "it's a dose-response study of liver toxicity",
            _GAP_CONTEXT,
        )

        assert decision["action"] == "commit"
        assert decision["value"] == "A hepatotoxicity dose-response study."

    def test_idk_reply_returns_skip(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel({"action": "skip"})

        decision = leaves.interpret_gap_reply(
            "What does this study examine?",
            "No idea which file you are talking about",
            _GAP_CONTEXT,
        )

        assert decision["action"] == "skip"
        # A musing must NEVER carry a value.
        assert not decision.get("value")

    def test_clarify_returns_one_follow_up(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"action": "clarify", "question": "Do you mean the in-vitro assay?"}
        )

        decision = leaves.interpret_gap_reply(
            "What does this study examine?", "the assay", _GAP_CONTEXT
        )

        assert decision["action"] == "clarify"
        assert decision["question"] == "Do you mean the in-vitro assay?"

    def test_from_file_returns_filename_hint(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"action": "from_file", "filename": "README.txt"}
        )

        decision = leaves.interpret_gap_reply(
            "What does this study examine?",
            "it's all written up in README.txt",
            _GAP_CONTEXT,
        )

        assert decision["action"] == "from_file"
        assert decision["filename"] == "README.txt"
        # D5: a from-file reply must NEVER smuggle a value into the field.
        assert not decision.get("value")

    def test_unknown_action_normalises_to_skip(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        # An adversarial / malformed action must be coerced to the safe default
        # (skip) so it can never become a field value.
        _patch_build_chat_model["model"] = FakeChatModel(
            {"action": "nonsense", "value": "garbage"}
        )

        decision = leaves.interpret_gap_reply("Q?", "whatever", _GAP_CONTEXT)

        assert decision["action"] == "skip"
        assert not decision.get("value")

    def test_commit_without_value_normalises_to_skip(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        # A "commit" with no usable value is not a commit — coerce to skip so the
        # loop never writes an empty/whitespace value.
        _patch_build_chat_model["model"] = FakeChatModel(
            {"action": "commit", "value": "   "}
        )

        decision = leaves.interpret_gap_reply("Q?", "...", _GAP_CONTEXT)

        assert decision["action"] == "skip"


class TestGuidanceLeavesD5:
    """D5: the interpret leaf must never let the model fabricate an identifier.

    Identifier-bearing gaps are resolved by lookups, never by interpreting the
    user's prose, so a 'commit' the model proposes for an identifier field is
    refused (coerced to skip)."""

    def test_identifier_gap_commit_is_refused(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"action": "commit", "value": "103-90-2"}
        )

        decision = leaves.interpret_gap_reply(
            "What is the CAS number?",
            "103-90-2",
            {"property": "cas", "entity_type": "MolecularEntity"},
        )

        assert decision["action"] == "skip", (
            "D5: an identifier value must come from a lookup, not the user's prose"
        )


# ---------------------------------------------------------------------------
# Entity-aware phrasing (Issue #257, fix A)
#
# When a gap is about a CONCRETE entity, the guidance loop now threads the
# entity's type/name/known-fields into the gap context. The phrase leaf must
# put that NAME into the question — never a bare "this chemical/protocol/cell
# line" — so the user knows WHICH entity is being asked about.
# ---------------------------------------------------------------------------


class TestPhraseGapQuestionNamesEntity:
    """The phrased question must reference the named entity (Issue #257)."""

    def test_entity_name_reaches_the_model_prompt(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel(
            {"question": "What is the CAS Registry Number for Silychristin A?"}
        )
        _patch_build_chat_model["model"] = fake

        leaves.phrase_gap_question(
            {
                "property": "cas",
                "entity_type": "MolecularEntity",
                "entity_name": "Silychristin A",
                "tier": "SHOULD",
                "message": "MolecularEntity SHOULD record a CAS number.",
                "suggestion": "A CAS Registry Number like 103-90-2.",
            }
        )

        # The entity NAME the loop resolved must reach the model in the prompt
        # block, so the model can phrase a question naming the entity.
        human_msg = fake.invoke_calls[0][-1].content
        assert "Silychristin A" in human_msg

    def test_known_fields_reach_the_model_prompt(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({"question": "q"})
        _patch_build_chat_model["model"] = fake

        leaves.phrase_gap_question(
            {
                "property": "description",
                "entity_type": "LabProtocol",
                "entity_name": "OATP1C1 uptake assay",
                "known_fields": {"name": "OATP1C1 uptake assay"},
                "tier": "SHOULD",
                "message": "LabProtocol SHOULD have a description.",
            }
        )

        human_msg = fake.invoke_calls[0][-1].content
        assert "OATP1C1 uptake assay" in human_msg


# ---------------------------------------------------------------------------
# extract_field_from_file — the bounded file-extraction leaf (Issue #257, fix C)
#
# When the user points the guidance loop at a file ("the CAS number is in
# assay-metadata.xlsx"), the loop reads the file and asks this leaf to extract
# the requested field value from the file text. It is a single bounded
# structured-output call on the drafter tier; it returns a clean value or empty.
# D5: an identifier-bearing field is never fabricated — the leaf returns nothing
# for one, so the loop verifies via lookups instead.
# ---------------------------------------------------------------------------


class TestExtractFieldFromFile:
    def test_runs_on_drafter_tier_single_call(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        rec = _patch_build_chat_model
        fake = FakeChatModel({"value": "A viability assay protocol."})
        rec["model"] = fake

        leaves.extract_field_from_file(
            "description",
            "Protocol: the cells were exposed for 24h then read out.",
            {"property": "description", "entity_type": "LabProtocol"},
        )

        assert all(c.get("role") == "drafter" for c in rec["calls"]), (
            "extract_field_from_file must build the chat model on the drafter tier"
        )
        assert len(fake.structured_schemas) == 1, "exactly one structured-output bind"
        assert len(fake.invoke_calls) == 1, "exactly one model invocation (a leaf)"

    def test_extracts_a_clean_value(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        _patch_build_chat_model["model"] = FakeChatModel(
            {"value": "A dose-response viability assay in CHO-K1 cells."}
        )

        value = leaves.extract_field_from_file(
            "description",
            "file body with the description in it",
            {"property": "description", "entity_type": "Study"},
        )

        assert value == "A dose-response viability assay in CHO-K1 cells."

    def test_file_text_reaches_the_model_prompt(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        fake = FakeChatModel({"value": "x"})
        _patch_build_chat_model["model"] = fake

        leaves.extract_field_from_file(
            "description",
            "UNIQUE-FILE-MARKER-12345",
            {"property": "description", "entity_type": "Study"},
        )

        human_msg = fake.invoke_calls[0][-1].content
        assert "UNIQUE-FILE-MARKER-12345" in human_msg

    def test_no_value_returns_empty_string(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        # The file does not contain the field -> the model returns nothing usable.
        _patch_build_chat_model["model"] = FakeChatModel({})

        value = leaves.extract_field_from_file(
            "description",
            "an unrelated file body",
            {"property": "description", "entity_type": "Study"},
        )

        assert value == ""

    def test_identifier_field_extraction_is_refused(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        # D5: even if the model returns a CAS number, an identifier-bearing field
        # must NOT be extracted from the file text — those come from lookups.
        _patch_build_chat_model["model"] = FakeChatModel({"value": "103-90-2"})

        value = leaves.extract_field_from_file(
            "cas",
            "the CAS number is 103-90-2",
            {"property": "cas", "entity_type": "MolecularEntity"},
        )

        assert value == "", (
            "D5: an identifier value must come from a lookup, not file text"
        )


def _flatten_keys(schema: Any) -> set[str]:
    """Every property key appearing anywhere in a (possibly nested) JSON schema."""
    keys: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    keys.update(value.keys())
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    return keys
