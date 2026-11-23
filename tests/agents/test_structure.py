"""The drafter call that labels a deposit's folder tree (#852).

`builder.agents.structure.derive_deposit_structure` labels the tree summary as
studies and assays in ONE structured-output call on the drafter tier
(`_build_chat_model(role="drafter")`), reporting its token usage when asked.

These tests are OFFLINE: the chat model is mocked with a fake that records the
build call and returns canned structured output. No real model, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from builder.agents import structure
from builder.agents.llm import ModelOverrides


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
        system_fingerprint: str | None = None,
    ) -> None:
        self.canned_output = canned_output
        self._usage_metadata = usage_metadata
        self._model_name = model_name
        self._system_fingerprint = system_fingerprint
        self.structured_schemas: list[Any] = []
        self.invoke_calls: list[Any] = []
        self.include_raw_flags: list[bool] = []

    def raw_message(self) -> Any:
        """Build the AIMessage the structured-output runnable returns as ``raw``."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(content="", usage_metadata=self._usage_metadata)
        meta: dict[str, Any] = {}
        if self._model_name is not None:
            meta["model_name"] = self._model_name
        if self._system_fingerprint is not None:
            meta["system_fingerprint"] = self._system_fingerprint
        if meta:
            msg.response_metadata = meta
        return msg

    def with_structured_output(
        self, schema: Any, *, include_raw: bool = False, **kwargs: Any
    ) -> _FakeStructuredRunnable:
        self.structured_schemas.append(schema)
        self.include_raw_flags.append(include_raw)
        return _FakeStructuredRunnable(self, include_raw=include_raw)


@pytest.fixture(autouse=True)
def _patch_build_chat_model(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch `_build_chat_model` in the structure module to capture its kwargs.

    Returns a mutable record dict the test can read (the role requested, the
    model passed) and write (`record["model"]` -> the FakeChatModel to return).
    """
    record: dict[str, Any] = {"calls": [], "model": FakeChatModel({})}

    def _fake_build(*args: Any, **kwargs: Any) -> FakeChatModel:
        record["calls"].append(kwargs)
        return record["model"]

    monkeypatch.setattr(structure, "_build_chat_model", _fake_build)
    return record


class TestDeriveDepositStructure:
    """One drafter-tier call labels the folder tree as studies and assays (#852)."""

    SUMMARY = (
        "./ — 7 files: 4 metadata, 3 protocol | docs: README.txt, S-VHPS16.json\n"
        "Exposure scenarios/Assays/LDH analysis/ — 4 files: 3 metadata, 1 protocol\n"
    )
    LABELLING = {
        "studies": [
            {
                "folder": "Exposure scenarios",
                "name": "Exposure scenarios",
                "assays": [
                    {"folder": "Exposure scenarios/Assays/LDH analysis", "name": "LDH analysis"}
                ],
            }
        ]
    }

    def test_runs_on_the_drafter_tier_with_a_studies_schema(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        rec = _patch_build_chat_model
        rec["model"] = FakeChatModel(self.LABELLING)

        result = structure.derive_deposit_structure(self.SUMMARY)

        assert rec["calls"][0]["role"] == "drafter"
        assert len(rec["model"].structured_schemas) == 1
        schema = rec["model"].structured_schemas[0]
        assays = schema["properties"]["studies"]["items"]["properties"]["assays"]
        assert set(assays["items"]["properties"]) == {"folder", "name"}
        assert result == self.LABELLING

    def test_the_tree_summary_is_the_prompt(self, _patch_build_chat_model: dict[str, Any]) -> None:
        rec = _patch_build_chat_model
        rec["model"] = FakeChatModel(self.LABELLING)

        structure.derive_deposit_structure(self.SUMMARY)

        prompt = "\n".join(str(getattr(m, "content", m)) for m in rec["model"].invoke_calls[0])
        assert self.SUMMARY in prompt
        assert "assay" in prompt.lower() and "study" in prompt.lower()

    def test_overrides_and_usage_reach_the_call(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        rec = _patch_build_chat_model
        rec["model"] = FakeChatModel(
            self.LABELLING,
            usage_metadata={"input_tokens": 50, "output_tokens": 9, "total_tokens": 59},
            model_name="m",
        )
        seen: list[tuple[Any, ...]] = []

        structure.derive_deposit_structure(
            self.SUMMARY,
            overrides=ModelOverrides(model="drafter-x"),
            usage_sink=lambda *a: seen.append(a),
        )

        assert rec["calls"][0]["model"] == "drafter-x"
        assert seen == [(50, 9, "m", None)]

    def test_a_non_dict_answer_is_an_empty_labelling(
        self, _patch_build_chat_model: dict[str, Any]
    ) -> None:
        rec = _patch_build_chat_model
        not_a_dict: Any = None
        rec["model"] = FakeChatModel(not_a_dict)

        assert structure.derive_deposit_structure(self.SUMMARY) == {}
