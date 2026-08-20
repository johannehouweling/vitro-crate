"""LLM build modes for the ISA-Tox RO-Crate Builder.

Two build variants share one toolbox (``builder.tools``), engine
(``builder.engine``), and LLM layer (:mod:`builder.agents.llm`); only the
orchestration differs (Issue #309):

- :mod:`builder.agents.pipeline` — the deterministic, code-orchestrated spine
  (``--interactive`` default): ``pipeline``, ``guidance``, ``leaves``.
- :mod:`builder.agents.react` — the LLM-orchestrated ReAct StateGraph
  (``--react``): ``agent_loop``, ``system_prompt``, ``tools_spec``.

:mod:`builder.agents.build` dispatches to the selected mode.
"""

from __future__ import annotations
