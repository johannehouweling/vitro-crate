# D1: Toolbox over Graph

The agent decides what to call rather than following a predefined workflow graph. Validation and HITL feedback can send the process to any earlier stage. Mitigated by max iterations and HITL escalation.

> **Clarification:** The agent loop uses LangGraph's `StateGraph` as its execution runtime — the `"model"` and `"tools"` nodes and `should_continue` conditional edge form the LLM-calling machinery. The *workflow* (which entities to draft, when to validate, what to look up) is **not** encoded in the graph; it emerges from the LLM's tool choices. The StateGraph provides structured message-passing, checkpointing, and looping — not a predefined pipeline.
