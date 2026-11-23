"""Read a deposit's studies and assays from its folder tree (#852).

One bounded drafter call labels the folder tree that
:func:`builder.tools.deposit_structure.tree_summary` summarises; the labelling is
the raw answer, and :func:`builder.tools.deposit_structure.check_structure`
decides whether it is accepted. The call runs on the **drafter tier**
(``_build_chat_model(role="drafter")``) and reports its token usage like the
ReAct model node does. Nothing here reads a file or touches state.

The module imports ``langchain_core`` at load, so :mod:`builder.agents.build`
imports it lazily and stays importable without it.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from builder.agents.llm import (
    ModelOverrides,
    UsageSink,
    _build_chat_model,
    _extract_model_name,
    _extract_system_fingerprint,
    _extract_token_usage,
)


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
    ``(input_tokens, output_tokens, model_name, system_fingerprint)`` off it via
    the SAME provider-agnostic helpers the ReAct model node uses
    (:func:`builder.agents.llm._extract_token_usage`), reports them, and
    returns the parsed object — so callers are unaffected by the capture.
    """
    if usage_sink is None:
        return llm.with_structured_output(schema).invoke(messages)

    raw_result = llm.with_structured_output(schema, include_raw=True).invoke(messages)
    if isinstance(raw_result, dict) and "parsed" in raw_result:
        raw_msg = raw_result.get("raw")
        input_tokens, output_tokens = _extract_token_usage(raw_msg)
        usage_sink(
            input_tokens,
            output_tokens,
            _extract_model_name(raw_msg),
            _extract_system_fingerprint(raw_msg),
        )
        return raw_result.get("parsed")
    # Defensive: a model/runnable that ignored include_raw still yields a result.
    return raw_result


_STRUCTURE_SYSTEM_PROMPT = (
    "You label the folder tree of a research data deposit as its studies and "
    "assays. An assay is one experiment type that produced data; its folder "
    "usually holds raw or processed data, a protocol or a metadata record, and "
    "sometimes only the documents with the data still to come. A study is the "
    "assays run on one test system or research question; its folder contains "
    "its assays' folders. Run folders, dated folders, compound-series folders "
    "and raw/processed folders belong to the assay above them and are never "
    "assays themselves. Literature, resources and protocol folders are neither, "
    "and a folder with no assay below it is not a study: leave it out. "
    "A flat deposit is one study at '.' with one assay at '.'. Use only folder "
    "paths that appear in the tree, spelled exactly as written ('.' is the "
    "root). Name each study and assay from its folder name or README; never "
    "invent a name."
)


def _structure_schema() -> dict[str, Any]:
    """Studies of assays, a folder and a name each — nothing else (#852)."""
    labelled = {
        "type": "object",
        "properties": {"folder": {"type": "string"}, "name": {"type": "string"}},
        "required": ["folder", "name"],
        "additionalProperties": False,
    }
    study = {
        "type": "object",
        "properties": {
            "folder": {"type": "string"},
            "name": {"type": "string"},
            "assays": {"type": "array", "items": labelled},
        },
        "required": ["folder", "name", "assays"],
        "additionalProperties": False,
    }
    return {
        "title": "DepositStructure",
        "description": "The studies and assays a deposit's folder tree states.",
        "type": "object",
        "properties": {"studies": {"type": "array", "items": study}},
        "required": ["studies"],
        "additionalProperties": False,
    }


def derive_deposit_structure(
    tree_summary: str,
    *,
    overrides: ModelOverrides | None = None,
    usage_sink: UsageSink | None = None,
) -> dict[str, Any]:
    """Label a deposit's folder tree as studies and assays, in one drafter call (#852).

    *tree_summary* is :func:`builder.tools.deposit_structure.tree_summary`. The
    answer is the raw labelling; :func:`builder.tools.deposit_structure.check_structure`
    decides whether it is accepted, and the caller falls back when it is not.
    Nothing here reads a file or touches state. An answer that is not an object
    is an empty labelling, which the checks reject.
    """
    llm = _build_chat_model(role="drafter", **(overrides or ModelOverrides()).as_kwargs())
    messages = [
        SystemMessage(content=_STRUCTURE_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                "Folder tree (one folder per line: its own files by class, some of "
                "their names, what lies below, and its README where one was read):\n"
                f"{tree_summary}\n\n"
                "Label the studies and the assays under each study."
            )
        ),
    ]
    result = _invoke_structured_with_usage(llm, _structure_schema(), messages, usage_sink)
    return dict(result) if isinstance(result, dict) else {}
