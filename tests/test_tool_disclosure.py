"""Tests for progressive tool disclosure (Issue #156).

The agent advertises only a state-relevant subset of the toolbox each turn so a
weak model (DeepSeek-flash) picks from a smaller menu. Pruning is conservative:
only tools that provably cannot act yet are dropped (file readers with no
scanned files; entity-dependent tools with no entities). Uncategorised tools are
always advertised, and the ToolNode keeps the full set — advertise narrow,
execute wide.
"""

from __future__ import annotations

from builder.agents.agent_loop import _tools_for_state


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _names(tools: list) -> set[str]:
    return {t.name for t in tools}


class TestToolsForState:
    def test_empty_state_prunes_file_and_entity_tools(self):
        tools = [
            _FakeTool(n)
            for n in (
                "scan_files",
                "read_file_sample",
                "extract_pdf_text",
                "draft_investigation",
                "scaffold_isa_backbone",
                "set_fields",
                "link",
                "list_entities",
                "export_crate",
                "lookup_compound",
                "build_and_validate",
            )
        ]
        out = _names(_tools_for_state(tools, has_files=False, has_entities=False))
        # file readers and entity-dependent tools are pruned...
        assert "read_file_sample" not in out
        assert "extract_pdf_text" not in out
        assert {"set_fields", "link", "list_entities", "export_crate"}.isdisjoint(out)
        # ...but scanning, drafters, lookups and the build loop stay.
        assert {
            "scan_files",
            "draft_investigation",
            "scaffold_isa_backbone",
            "lookup_compound",
            "build_and_validate",
        } <= out

    def test_files_present_includes_readers(self):
        tools = [_FakeTool("read_file_sample"), _FakeTool("extract_pdf_text")]
        out = _names(_tools_for_state(tools, has_files=True, has_entities=False))
        assert {"read_file_sample", "extract_pdf_text"} <= out

    def test_entities_present_includes_entity_tools(self):
        tools = [_FakeTool("set_fields"), _FakeTool("link"), _FakeTool("export_crate")]
        out = _names(_tools_for_state(tools, has_files=False, has_entities=True))
        assert {"set_fields", "link", "export_crate"} <= out

    def test_uncategorised_tool_always_advertised(self):
        tools = [_FakeTool("some_future_tool")]
        out = _names(_tools_for_state(tools, has_files=False, has_entities=False))
        assert "some_future_tool" in out

    def test_full_state_keeps_everything(self):
        names = ["scan_files", "read_file_sample", "set_fields", "draft_assay", "lookup_aop"]
        tools = [_FakeTool(n) for n in names]
        out = _names(_tools_for_state(tools, has_files=True, has_entities=True))
        assert out == set(names)


class TestCallModelBindsSubset:
    def test_empty_state_binds_pruned_subset(self):
        """The graph's model node must bind only the state-relevant subset."""
        from unittest.mock import MagicMock

        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.tools import StructuredTool

        from builder.agents.agent_loop import _build_agent_graph
        from builder.engine import AgentEngine

        def _mk(name: str) -> StructuredTool:
            return StructuredTool.from_function(func=lambda: "ok", name=name, description=name)

        engine = AgentEngine()
        engine.initialize()  # empty: no files, no entities

        tools = [_mk("read_file_sample"), _mk("set_fields"), _mk("draft_investigation")]
        bound = MagicMock()
        bound.invoke.return_value = AIMessage(content="ok")
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = bound

        app = _build_agent_graph(mock_llm, tools, engine=engine)
        app.invoke(
            {"messages": [HumanMessage(content="hi")]},
            {"configurable": {"thread_id": "disclosure-empty-001"}},
        )

        mock_llm.bind_tools.assert_called_once()
        advertised = {t.name for t in mock_llm.bind_tools.call_args.args[0]}
        assert "draft_investigation" in advertised
        assert "read_file_sample" not in advertised  # pruned: no scanned files
        assert "set_fields" not in advertised  # pruned: no entities
