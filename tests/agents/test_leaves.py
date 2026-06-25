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
    """The runnable returned by `model.with_structured_output(schema)`.

    Records the messages it was invoked with and returns canned output.
    """

    def __init__(self, parent: "FakeChatModel") -> None:
        self._parent = parent

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._parent.invoke_calls.append(messages)
        return self._parent.canned_output


class FakeChatModel:
    """A fake LangChain chat model recording structured-output usage.

    `with_structured_output(schema)` records the schema and returns a runnable
    whose `.invoke(...)` yields `canned_output`. This lets the tests assert the
    leaf goes through structured output exactly once without any network call.
    """

    def __init__(self, canned_output: dict[str, Any]) -> None:
        self.canned_output = canned_output
        self.structured_schemas: list[Any] = []
        self.invoke_calls: list[Any] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeStructuredRunnable:
        self.structured_schemas.append(schema)
        return _FakeStructuredRunnable(self)


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
