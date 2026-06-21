"""Tests for StateGraph node timing instrumentation (issue #38)."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool as langchain_tool

from builder.agents.agent_loop import (
    _build_agent_graph,
    _tool_names_from_state,
    _wrap_model_node,
    _wrap_tools_node,
)


class _RecordingProfiler:
    """Minimal profiler stand-in that records log_event calls."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_event(self, **kwargs) -> None:
        self.events.append(kwargs)


def _events(profiler, event, node):
    return [e for e in profiler.events if e.get("event") == event and e.get("node") == node]


def _tc(name, cid="1"):
    """Build a LangChain tool_call dict."""
    return {"name": name, "args": {}, "id": cid, "type": "tool_call"}


class TestToolNamesFromState:
    def test_extracts_tool_names_from_last_ai_message(self):
        msg = AIMessage(
            content="",
            tool_calls=[
                _tc("draft_investigation", "1"),
                _tc("validate", "2"),
            ],
        )
        assert _tool_names_from_state({"messages": [msg]}) == [
            "draft_investigation",
            "validate",
        ]

    def test_empty_when_no_messages_or_tool_calls(self):
        assert _tool_names_from_state({"messages": []}) == []
        assert _tool_names_from_state({"messages": [AIMessage(content="hi")]}) == []


class TestWrapModelNode:
    def test_logs_start_and_end_with_metrics(self):
        prof = _RecordingProfiler()

        def call_model(state):
            return {"messages": [AIMessage(content="done")]}

        wrapped = _wrap_model_node(call_model, prof, lambda: 3)
        out = wrapped({"messages": [HumanMessage(content="hi")]})

        assert out["messages"][0].content == "done"
        starts = _events(prof, "node_start", "model")
        ends = _events(prof, "node_end", "model")
        assert len(starts) == 1 and len(ends) == 1
        assert starts[0]["iteration"] == 3
        end = ends[0]
        assert end["iteration"] == 3
        assert isinstance(end["duration_ms"], float)
        assert end["messages_in"] == 1
        assert end["messages_out"] == 1
        assert end["produced_tool_calls"] is False

    def test_flags_produced_tool_calls(self):
        prof = _RecordingProfiler()

        def call_model(state):
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[_tc("validate")],
                    )
                ]
            }

        wrapped = _wrap_model_node(call_model, prof, lambda: 1)
        wrapped({"messages": []})
        assert _events(prof, "node_end", "model")[0]["produced_tool_calls"] is True

    def test_noop_without_profiler_returns_original(self):
        def call_model(state):
            return {"messages": []}

        assert _wrap_model_node(call_model, None, lambda: 0) is call_model


class TestWrapToolsNode:
    def test_logs_tools_called(self):
        prof = _RecordingProfiler()

        class _FakeToolNode:
            def invoke(self, state, *a, **k):
                return {"messages": ["tool result"]}

        wrapped = _wrap_tools_node(_FakeToolNode(), prof, lambda: 5)
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[_tc("lookup_compound")],
                )
            ]
        }
        out = wrapped(state)
        assert out["messages"] == ["tool result"]
        starts = _events(prof, "node_start", "tools")
        ends = _events(prof, "node_end", "tools")
        assert len(starts) == 1 and len(ends) == 1
        assert starts[0]["tools"] == ["lookup_compound"]
        assert ends[0]["tools"] == ["lookup_compound"]
        assert isinstance(ends[0]["duration_ms"], float)

    def test_noop_without_profiler_returns_original(self):
        node = object()
        assert _wrap_tools_node(node, None, lambda: 0) is node


class TestGraphWiringWritesProfile:
    def test_build_agent_graph_with_engine_writes_node_events(self, tmp_path, monkeypatch):
        import builder.tools.profiler as profiler_mod
        from builder.engine import AgentEngine

        monkeypatch.setattr(profiler_mod, "SESSION_DIR", tmp_path)

        engine = AgentEngine()
        engine.initialize()

        @langchain_tool
        def dummy_tool(query: str) -> str:
            """A dummy tool."""
            return f"result: {query}"

        class _FakeLLM:
            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                return AIMessage(content="final answer")

        app = _build_agent_graph(_FakeLLM(), [dummy_tool], engine=engine)
        app.invoke(
            {"messages": [HumanMessage(content="hi")]},
            {"configurable": {"thread_id": engine.state.session_id}},
        )
        engine.close_profiler()

        profile_file = tmp_path / engine.state.session_id / "profile.ndjson"
        assert profile_file.exists()
        records = [json.loads(line) for line in profile_file.read_text().splitlines() if line]

        def _node(ev):
            return [r for r in records if r.get("event") == ev and r.get("node") == "model"]

        model_starts = _node("node_start")
        model_ends = _node("node_end")
        assert model_starts, f"no model node_start in {records}"
        assert model_ends, f"no model node_end in {records}"
        assert "duration_ms" in model_ends[0]
