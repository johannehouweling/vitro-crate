# D15: The ReAct Loop Is the Build

There is one build: the ReAct loop (D1, [the agent graph](../design/react-loop.md#agent-graph-langgraph--stategraph))
over [the toolbox](../design/toolbox.md). The LLM orders the tool calls and reads the deposit on
demand; code owns only initialization ([architecture](../design/architecture.md#architecture-overview)),
one read of the [deposit structure](../design/initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy),
and the guard rails. `run_build` ([entrypoint](../design/react-loop.md#the-build-entrypoint-builderagentsbuildpy))
serves `main.py --interactive` and the eval alike, and the crate records
`generator.architecture = "react"` (#772). The eval (`eval/`) measures profile conformance
(base + isa + tox) and an entity-count quota, **not** scientific accuracy.
