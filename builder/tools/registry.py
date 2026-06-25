"""Explicit tool registry for the ISA-Tox RO-Crate Builder.

Replaces ``dir()``-based auto-discovery (which registered every public
callable in a tool module — including imported classes and internal helpers)
with explicit registration. Each tool module imports the shared
:data:`TOOL_REGISTRY` and registers its public tools at the bottom of the file
via :meth:`ToolRegistry.register`, declaring ``takes_state`` rather than
relying on parameter-name introspection.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool.

    Attributes:
        name: The name the agent uses to call the tool.
        fn: The tool callable.
        description: Optional human-readable description.
        takes_state: Whether the engine must pass ``CrateState`` as the first
            positional argument when invoking ``fn``.
        takes_human: Whether the engine must inject the active
            :class:`~builder.tools.hitl.HumanInterface` as a ``human_interface``
            keyword argument so the tool can escalate to HITL on genuine
            ambiguity (e.g. ``draft_publication_with_authors``, #180).
    """

    name: str
    fn: Callable[..., Any]
    description: str = ""
    takes_state: bool = False
    takes_human: bool = False


class ToolRegistry:
    """Maps tool names to :class:`ToolSpec` entries."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        description: str = "",
        takes_state: bool = False,
        takes_human: bool = False,
    ) -> None:
        """Register ``fn`` under ``name`` (overwrites any existing entry).

        Set ``takes_human=True`` for a tool that needs to escalate to HITL: the
        engine then injects the active ``HumanInterface`` as a ``human_interface``
        keyword when it invokes the tool.

        Hint for developers: after adding a new tool here, also add an entry
        in ``builder.tools.dashboard._TOOL_CATEGORIES`` so it appears under
        the right category in the profiler dashboard.  Uncategorised tools
        show up in a highlighted "Other" row with a logger warning.
        """
        self._tools[name] = ToolSpec(
            name=name,
            fn=fn,
            description=description,
            takes_state=takes_state,
            takes_human=takes_human,
        )

    def get(self, name: str) -> Callable[..., Any]:
        """Return the callable registered under ``name`` (raises KeyError)."""
        return self._tools[name].fn

    def get_spec(self, name: str) -> ToolSpec | None:
        """Return the full :class:`ToolSpec` for ``name``, or None if absent."""
        return self._tools.get(name)

    def list(self) -> builtins.list[str]:
        """Return registered tool names, sorted."""
        return sorted(self._tools)

    def all(self) -> dict[str, ToolSpec]:
        """Return a copy of the full name → :class:`ToolSpec` mapping."""
        return dict(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


# Shared registry populated by each tool module's bottom-of-file registration.
TOOL_REGISTRY = ToolRegistry()
