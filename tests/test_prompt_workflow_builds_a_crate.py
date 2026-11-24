"""Following the prompt's steps, in order, through the real graph builds a crate.

No live model is involved: a scripted one calls the tools the prompt names, step by
step, on a fixture deposit. What is real is everything the agent depends on: the
prompt's tool list, the per-turn tool binding, the ToolNode and the tools. The
test fails if a step's tool is not bound when the step runs, if the script has to
leave the prompt's order, or if the crate those steps make does not pass base.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from builder.agents.react.agent_loop import _build_agent_graph, _build_langchain_tools
from builder.agents.react.system_prompt import SYSTEM_PROMPT
from builder.engine import AgentEngine

FIXTURE = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "arbitrary_tox_folder"


def _step_of() -> dict[str, int]:
    found = re.search(r"## Your Tools\n(.*?)\n## ", SYSTEM_PROMPT, re.S)
    assert found, "the prompt has no Your Tools section"
    section = found.group(1)
    steps, current = {}, 0
    for line in section.splitlines():
        if m := re.match(r"^(\d+)\. ", line):
            current = int(m.group(1))
        elif m := re.match(r"^- ([a-z_]+):", line):
            steps[m.group(1)] = current
    return steps


def _last_result(messages) -> dict:
    tool = [m for m in messages if isinstance(m, ToolMessage)][-1]
    return json.loads(tool.content)


def test_the_prompts_steps_build_a_base_passing_crate(tmp_path) -> None:
    deposit = shutil.copytree(FIXTURE, tmp_path / "deposit")
    engine = AgentEngine()
    engine.initialize(str(deposit))

    script = [
        lambda m: ("list_scanned_files", {}),
        lambda m: ("read_file", {"path": str(deposit / "study_description.md")}),
        lambda m: ("scaffold_isa_backbone", {"investigation": {"name": "Dose response"}}),
        lambda m: ("draft_process_chain", {
            "assay_id": _last_result(m)["assay_id"],
            "chain": [{"process_type": "Exposure"}, {"process_type": "EndpointReadout"},
                      {"process_type": "DataAnalysis"}]}),
        lambda m: ("build_and_validate", {}),
    ]
    calls: list[tuple[str, set[str]]] = []

    class _ScriptedModel:
        bound: set[str] = set()

        def bind_tools(self, tools):
            model = _ScriptedModel()
            model.bound = {t.name for t in tools}
            return model

        def invoke(self, messages):
            if not script:
                return AIMessage(content="done")
            name, args = script.pop(0)(messages)
            calls.append((name, self.bound))
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])

    app = _build_agent_graph(_ScriptedModel(), _build_langchain_tools(engine), engine=engine)
    out = app.invoke({"messages": [HumanMessage(content="build")]},
                     {"configurable": {"thread_id": "t"}, "recursion_limit": 40})

    step_of = _step_of()
    for name, bound in calls:
        assert name in bound, f"{name} was not offered when its step ran"
    order = [step_of[name] for name, _ in calls]
    assert order == sorted(order), f"the steps ran out of the prompt's order: {order}"
    verdict = _last_result(out["messages"])
    assert verdict["conformance"]["base"] is True, verdict.get("issues", [])[:3]
