"""Deterministic pipeline build mode — the code-orchestrated variant.

The ``--interactive`` default. Pipeline-unique modules live here: the build spine
(:mod:`~builder.agents.pipeline.pipeline`), the HITL gap-guidance tail
(:mod:`~builder.agents.pipeline.guidance`), and the bounded-extraction leaves
(:mod:`~builder.agents.pipeline.leaves`).

Both build modes share the toolbox (``builder.tools``), the engine
(``builder.engine``), and the LLM layer (``builder.agents.llm``); only the
*orchestration* differs (Issue #309).
"""

from __future__ import annotations
