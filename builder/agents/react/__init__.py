"""The ReAct build — the LLM-orchestrated LangGraph agent loop.

Reached through :func:`builder.agents.build.run_build`. The loop's own modules
live here: the explicit ``StateGraph`` loop (:mod:`~builder.agents.react.agent_loop`),
the system prompt (:mod:`~builder.agents.react.system_prompt`), and the tool specs
advertised to the model (:mod:`~builder.agents.react.tools_spec`).

The toolbox (``builder.tools``), the engine (``builder.engine``) and the LLM layer
(``builder.agents.llm``) live outside it; this package is the *orchestration*
(Issue #309).
"""

from __future__ import annotations
