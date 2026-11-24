# D1: Toolbox over Graph

The agent decides what to call rather than following a predefined workflow graph; validation and
HITL feedback can send it back to any earlier stage. Max iterations and HITL escalation bound it.

LangGraph's `StateGraph` is only the execution runtime: the `"model"` and `"tools"` nodes and the
`should_continue` edge call the LLM and run its tools, with message-passing and checkpointing. The
*workflow* (which entities to draft, when to validate, what to look up) is not in the graph; it
emerges from the LLM's tool choices.
