# D15: The ReAct Loop Is the Build

There is one build: the ReAct agent loop (D1, [the agent graph](../design/react-loop.md#agent-graph-langgraph--stategraph)), one orchestration over the
toolbox ([the toolbox](../design/toolbox.md)). The LLM decides the order of tool calls and reads the deposit through
tools, on demand; code owns only what runs before the loop — initialization ([architecture overview](../design/architecture.md#architecture-overview)) and
one read of the deposit's structure from its folder tree ([deposit structure](../design/initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy)) — and the guard
rails around it. `run_build` ([the build entrypoint](../design/react-loop.md#the-build-entrypoint-builderagentsbuildpy)) is its one entrypoint, for `main.py
--interactive` and the eval harness alike, and the crate names the build that
produced it on its generator record (`generator.architecture = "react"`, #772). The
eval (`eval/`) measures it by profile conformance (base + isa + tox) plus an
entity-count quota — **not** scientific accuracy.
