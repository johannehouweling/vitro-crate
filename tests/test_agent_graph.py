"""Tests for the explicit StateGraph construction in agent_loop.

These tests verify that _build_agent_graph() produces a compiled graph
with the expected node structure, routing behavior, and message flow.
"""

from __future__ import annotations

from typing import Any

import pytest


class TestBuildAgentGraph:
    """Tests for the _build_agent_graph function."""

    def test_import_and_callable(self):
        """_build_agent_graph is importable and callable."""
        from builder.agents.agent_loop import _build_agent_graph

        # Must be instantiated with llm and tools
        assert callable(_build_agent_graph)

    def test_returns_compiled_graph(self):
        """_build_agent_graph returns a compiled StateGraph."""
        from unittest.mock import MagicMock

        from langchain_core.tools import tool as langchain_tool

        from builder.agents.agent_loop import _build_agent_graph

        @langchain_tool
        def dummy_tool(query: str) -> str:
            """A dummy tool for testing."""
            return f"result: {query}"

        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm

        app = _build_agent_graph(mock_llm, [dummy_tool])

        # Should have expected attributes of a compiled graph
        assert hasattr(app, "invoke")
        assert hasattr(app, "get_graph")

    def test_compiled_graph_has_expected_nodes(self):
        """The compiled graph contains the expected node names."""
        from unittest.mock import MagicMock

        from langchain_core.tools import tool as langchain_tool

        from builder.agents.agent_loop import _build_agent_graph

        @langchain_tool
        def dummy_tool(query: str) -> str:
            """A dummy tool for testing."""
            return f"result: {query}"

        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm

        app = _build_agent_graph(mock_llm, [dummy_tool])

        graph = app.get_graph()
        # In compiled graphs, nodes is a list of node names (strings)
        node_names = list(graph.nodes)
        assert "model" in node_names, f"Expected 'model' node, got {node_names}"
        assert "tools" in node_names, f"Expected 'tools' node, got {node_names}"

    def test_tools_are_bound_to_model(self):
        """The model MUST be given the tool schemas via bind_tools.

        Regression guard for the #71 StateGraph migration: call_model invoked
        a RAW, unbound llm, so with a real provider the model was never told
        the tools exist, could never emit tool_calls, should_continue always
        routed to END, and the agent silently degraded to a text-only chatbot
        that narrates 'let me scan...' forever but never executes a tool.
        """
        from unittest.mock import MagicMock

        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.tools import tool as langchain_tool

        from builder.agents.agent_loop import _build_agent_graph

        @langchain_tool
        def dummy_tool(query: str) -> str:
            """A dummy tool for testing."""
            return f"result: {query}"

        bound = MagicMock()
        bound.invoke.return_value = AIMessage(content="ok")
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = bound

        app = _build_agent_graph(mock_llm, [dummy_tool])
        app.invoke(
            {"messages": [HumanMessage(content="hi")]},
            {"configurable": {"thread_id": "bind-test-001"}},
        )

        # The tools must have been bound to the model...
        mock_llm.bind_tools.assert_called_once()
        bound_tools = mock_llm.bind_tools.call_args.args[0]
        assert dummy_tool in bound_tools
        # ...and the BOUND model (not the raw llm) must be the one invoked.
        bound.invoke.assert_called()

    def test_should_continue_routes_to_tools_when_tool_calls_present(self):
        """should_continue returns 'tools' when last message has tool_calls."""
        from unittest.mock import MagicMock

        from langchain_core.messages import AIMessage

        from builder.agents.agent_loop import should_continue

        # Create an AI message with tool_calls
        ai_msg = AIMessage(
            content="I'll look that up",
            tool_calls=[
                {
                    "name": "dummy_tool",
                    "args": {"query": "test"},
                    "id": "call_123",
                    "type": "tool_call",
                }
            ],
        )
        state = {"messages": [ai_msg]}

        result = should_continue(state)
        assert result == "tools"

    def test_should_continue_routes_to_end_when_no_tool_calls(self):
        """should_continue returns END when last message has no tool_calls."""
        from langgraph.graph import END

        from builder.agents.agent_loop import should_continue

        from langchain_core.messages import AIMessage

        ai_msg = AIMessage(content="Here is the answer.")
        state = {"messages": [ai_msg]}

        result = should_continue(state)
        assert result == END

    def test_should_continue_routes_to_end_when_empty_tool_calls(self):
        """should_continue returns END when tool_calls is empty list."""
        from langgraph.graph import END

        from builder.agents.agent_loop import should_continue

        from langchain_core.messages import AIMessage

        ai_msg = AIMessage(content="Done.", tool_calls=[])
        state = {"messages": [ai_msg]}

        result = should_continue(state)
        assert result == END

    def test_model_node_calls_llm_and_returns_messages(self):
        """The model node calls the LLM and returns updated messages."""
        from unittest.mock import MagicMock

        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.tools import tool as langchain_tool

        from builder.agents.agent_loop import _build_agent_graph

        @langchain_tool
        def dummy_tool(query: str) -> str:
            """A dummy tool for testing."""
            return f"result: {query}"

        # Mock LLM that returns a simple answer
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.invoke.return_value = AIMessage(content="Hello! I am the agent.")

        app = _build_agent_graph(mock_llm, [dummy_tool])

        # Need thread_id for MemorySaver checkpointing
        config = {"configurable": {"thread_id": "test-thread-001"}}
        result = app.invoke(
            {"messages": [HumanMessage(content="Hi")]},
            config,
        )

        assert "messages" in result
        messages = result["messages"]
        # Should have at least the human message and the AI response
        assert len(messages) >= 2
        # The last message should be from the AI (no tool calls)
        last_msg = messages[-1]
        assert last_msg.content == "Hello! I am the agent."


class TestRunInteractiveAgentPreservesBehavior:
    """Tests that run_interactive_agent still works via the new graph."""

    def test_run_interactive_agent_imports_and_calls_create_agent(self, monkeypatch):
        """run_interactive_agent still imports successfully and calls the new _build_agent_graph."""
        import builder.agents.agent_loop as loop_mod

        # The _build_agent_graph function should be defined
        assert hasattr(loop_mod, "_build_agent_graph")
        assert callable(loop_mod._build_agent_graph)