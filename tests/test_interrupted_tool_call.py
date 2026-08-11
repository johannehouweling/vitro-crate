"""A Ctrl+C must not poison the rest of the session.

Interrupting a turn kills the tool node between the model's ``tool_calls`` and
their ``ToolMessage`` replies. The checkpoint then holds a function call nobody
answered, and every later turn replays it:

    BadRequestError: 400 — No tool output found for function call call_YfkY…

`_rotate_checkpoint` was written for exactly this, but never ran for an
interrupt: ``KeyboardInterrupt`` is not an ``Exception``, so it propagated past
the call site. And rotation alone does not repair a session whose saved history
already carries the orphan, so the history is sanitised as well.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from builder.agents.react.agent_loop import _drop_unanswered_tool_calls, _trim_history


def _ai(*calls):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {}, "id": cid} for cid, name in calls],
    )


class TestDropUnansweredToolCalls:
    def test_an_answered_call_is_untouched(self):
        msgs = [
            HumanMessage(content="go"),
            _ai(("call_1", "list_entities")),
            ToolMessage(content="[]", tool_call_id="call_1"),
        ]
        assert _drop_unanswered_tool_calls(msgs) == msgs

    def test_a_trailing_unanswered_call_is_removed(self):
        """The Ctrl+C shape: the model asked, the tool never replied."""
        msgs = [
            HumanMessage(content="go"),
            _ai(("call_1", "list_entities")),
            ToolMessage(content="[]", tool_call_id="call_1"),
            _ai(("call_2", "resolve_compound")),
        ]
        kept = _drop_unanswered_tool_calls(msgs)
        assert len(kept) == 3
        assert all("call_2" not in str(getattr(m, "tool_calls", "")) for m in kept)

    def test_a_partly_answered_message_takes_its_answers_with_it(self):
        """Half a parallel fan-out is the same violation from the other side.

        Dropping the AIMessage but keeping the ToolMessage that DID land would
        leave a result whose call no longer exists, which the provider rejects
        just as firmly.
        """
        msgs = [
            HumanMessage(content="go"),
            _ai(("call_a", "lookup_orcid"), ("call_b", "lookup_ror")),
            ToolMessage(content="{}", tool_call_id="call_a"),
        ]
        kept = _drop_unanswered_tool_calls(msgs)
        assert kept == [msgs[0]]

    def test_an_orphan_mid_history_is_removed_without_touching_later_turns(self):
        msgs = [
            HumanMessage(content="one"),
            _ai(("call_x", "read_file")),
            HumanMessage(content="two"),
            _ai(("call_y", "list_entities")),
            ToolMessage(content="[]", tool_call_id="call_y"),
        ]
        kept = _drop_unanswered_tool_calls(msgs)
        assert kept == [msgs[0], msgs[2], msgs[3], msgs[4]]

    def test_plain_conversation_is_returned_unchanged(self):
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
        assert _drop_unanswered_tool_calls(msgs) is msgs

    def test_empty_history(self):
        assert _drop_unanswered_tool_calls([]) == []


class TestTrimHistoryRefusesToEmitOrphans:
    def test_trim_strips_an_interrupted_call(self):
        """The sanitiser runs inside the path every model turn goes through."""
        msgs = [
            HumanMessage(content="go"),
            _ai(("call_1", "list_entities")),
            ToolMessage(content="[]", tool_call_id="call_1"),
            _ai(("call_dead", "export_crate")),
        ]
        out = _trim_history(msgs, max_tokens=100_000)
        dangling = [
            m
            for m in out
            for c in (getattr(m, "tool_calls", None) or [])
            if (c.get("id") if isinstance(c, dict) else None) == "call_dead"
        ]
        assert dangling == []
