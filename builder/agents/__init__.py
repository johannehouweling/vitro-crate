"""LLM build modes for the ISA-Tox RO-Crate Builder.

The build is the LLM-orchestrated ReAct StateGraph (:mod:`builder.agents.react`:
``agent_loop``, ``system_prompt``, ``tools_spec``) over the toolbox
(``builder.tools``), the engine (``builder.engine``) and the LLM layer
(:mod:`builder.agents.llm`). :mod:`builder.agents.build` is its entrypoint, and
:mod:`builder.agents.structure` reads a deposit's studies and assays from its
folder tree.
"""

from __future__ import annotations
