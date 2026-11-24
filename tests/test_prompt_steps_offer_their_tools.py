"""Every tool the prompt names at a build step is advertised when that step runs.

The loop advertises a narrowed tool set per turn (`_tools_for_state`): file readers
only once files are scanned, entity tools only once an entity exists. The prompt
lists the tools step by step in build order. If the two disagree, the prompt sends
the agent for a tool it is not being shown.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from builder.agents.react.agent_loop import _tools_for_state
from builder.agents.react.system_prompt import SYSTEM_PROMPT
from builder.agents.react.tools_spec import TOOL_SPECS


def _steps() -> dict[int, set[str]]:
    section = re.search(r"## Your Tools\n(.*?)\n## ", SYSTEM_PROMPT, re.S).group(1)
    steps: dict[int, set[str]] = {}
    current = 0
    for line in section.splitlines():
        if m := re.match(r"^(\d+)\. ", line):
            current = int(m.group(1))
        elif m := re.match(r"^- ([a-z_]+):", line):
            steps.setdefault(current, set()).add(m.group(1))
    return steps


def test_the_prompt_lists_its_tools_in_numbered_steps() -> None:
    steps = _steps()
    assert 0 not in steps, "a tool is listed before the first step"
    assert sorted(steps) == list(range(1, len(steps) + 1))


def test_each_step_is_offered_the_tools_it_names() -> None:
    tools = [SimpleNamespace(name=spec["name"]) for spec in TOOL_SPECS]
    # Step 1 runs on the scanned deposit before anything is drafted; step 2 drafts
    # the backbone, so every later step has entities.
    for step, names in _steps().items():
        offered = {t.name for t in _tools_for_state(tools, has_files=True, has_entities=step > 2)}
        if step == 2:
            offered &= {t.name for t in _tools_for_state(tools, has_files=True, has_entities=False)}
        assert names <= offered, f"step {step} names tools not advertised then: {names - offered}"
