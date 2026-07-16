"""ReAct build mode — the LLM-orchestrated LangGraph agent variant.

Reached via ``--legacy-react``. Agent-mode-unique modules live here: the explicit
``StateGraph`` loop (:mod:`~builder.agents.react.agent_loop`), the system prompt
(:mod:`~builder.agents.react.system_prompt`), and the tool specs advertised to the
model (:mod:`~builder.agents.react.tools_spec`).

Both build modes share the toolbox (``builder.tools``), the engine
(``builder.engine``), and the LLM layer (``builder.agents.llm``); only the
*orchestration* differs (Issue #309).
"""

from __future__ import annotations
