"""Agent orchestration engine for the ISA-Tox RO-Crate Builder.

The AgentEngine manages the lifecycle of a crate-building session.
It coordinates tool calls, validation, HITL checkpoints, and session persistence.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from builder.state import CrateState

logger = logging.getLogger(__name__)


class AgentEngine:
    """Orchestrator for the LLM agent toolbox loop.

    The AgentEngine manages the lifecycle of a crate-building session.
    It coordinates tool calls, validation, HITL checkpoints, and
    session persistence.
    """

    _registry: dict[str, Any] = {}

    def __init__(self, state: CrateState | None = None):
        """Initialize the engine with an optional existing state."""
        self.state = state or CrateState()
        self._running = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(self, input_path: str | None = None) -> CrateState:
        """Run initialization: scan files, create initial state."""
        from builder.tools.scanner import scan_files

        if input_path:
            scanned = scan_files(input_path)
            self.state.scanned_files = scanned
            self.state.metadata.input_path = input_path
            resolved = Path(input_path).resolve()
            self.state.approved_scan_roots.add(str(resolved))

        if not self.state.session_id:
            self.state.session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if not self.state.created_at:
            self.state.created_at = datetime.now(timezone.utc).isoformat()
        self.state.updated_at = datetime.now(timezone.utc).isoformat()

        self.state.log_reasoning(
            "initialize", "scan_files",
            f"Scanned {len(self.state.scanned_files)} files",
        )
        self.state.checkpoint.completed_checkpoints.append("files_scanned")
        return self.state

    # ------------------------------------------------------------------
    # Tool registry
    # ------------------------------------------------------------------

    @classmethod
    def _build_registry(cls) -> dict[str, Any]:
        """Build the tool registry by importing all tool functions."""
        if cls._registry:
            return cls._registry

        import builder.tools.management as mgmt
        import builder.tools.drafters as draft
        import builder.tools.lookups as lookups
        import builder.tools.verification as verify
        import builder.tools.builder as builder_mod
        import builder.tools.validation as val_mod
        import builder.tools.mit_assessment as mit
        import builder.tools.fair_assessment as fair
        import builder.tools.session as session_mod

        registry: dict[str, Any] = {}
        for module in [mgmt, draft, lookups, verify, builder_mod, val_mod, mit, fair, session_mod]:
            for name in dir(module):
                obj = getattr(module, name)
                if callable(obj) and not name.startswith("_"):
                    registry[name] = obj
        cls._registry = registry
        return registry

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def run_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute a tool by name with kwargs.

        Looks up the tool function from the registry and calls it.
        Records the call in the reasoning log.

        Args:
            tool_name: Name of the tool to execute.
            **kwargs: Arguments to pass to the tool function.

        Returns:
            The result of the tool function.

        Raises:
            ValueError: If the tool name is not recognised.
        """
        scanner_tools: dict[str, Any] = {}
        try:
            from builder.tools.scanner import scan_files as sf, read_file_sample as rfs, read_multiple_files as rmf, unzip_file as uzf
            scanner_tools = {"scan_files": sf, "read_file_sample": rfs, "read_multiple_files": rmf, "unzip_file": uzf}
        except ImportError:
            pass

        if tool_name in scanner_tools:
            tool_fn = scanner_tools[tool_name]
            # Inject approved roots for scan_files
            tool_kwargs = dict(kwargs)
            if tool_name == "scan_files":
                tool_kwargs["approved_roots"] = (
                    self.state.approved_scan_roots.copy()
                    if self.state.approved_scan_roots
                    else None
                )
            result = tool_fn(**tool_kwargs)
            # Auto-store scan results in state, and register the scanned
            # path as an approved root if none were set yet.
            if tool_name == "scan_files" and isinstance(result, list):
                self.state.scanned_files = result
                if not self.state.approved_scan_roots:
                    resolved = Path(kwargs.get("path", "")).resolve()
                    self.state.approved_scan_roots.add(str(resolved))
                self.state.log_reasoning(
                    "store_scan_results", "scan_files",
                    f"Stored {len(result)} files in state",
                )
        else:
            registry = self._build_registry()
            if tool_name not in registry:
                raise ValueError(f"Unknown tool: {tool_name!r}")
            tool_fn = registry[tool_name]

            sig = inspect.signature(tool_fn)
            params = list(sig.parameters.keys())
            if params and params[0] == "state":
                result = tool_fn(self.state, **kwargs)
            else:
                result = tool_fn(**kwargs)

        self.state.iteration_count += 1
        self.state.log_reasoning(
            f"run_tool: {tool_name}",
            tool_name,
            str(result)[:300] if result is not None else "None",
        )
        return result

    # ------------------------------------------------------------------
    # Status & introspection
    # ------------------------------------------------------------------

    def get_available_tools(self) -> list[str]:
        """Return the list of available tool names.

        Returns:
            Sorted list of all registered tool names.
        """
        registry = self._build_registry()
        return sorted(set(list(registry.keys()) + ["scan_files", "read_file_sample"]))

    @property
    def is_stuck(self) -> bool:
        """Detect if the agent is stuck.

        Returns True if the stuck flag is set or if the iteration count
        has reached the maximum allowed.
        """
        return self.state.stuck or self.state.iteration_count >= self.state.max_iterations

    def mark_stuck(self, reason: str) -> None:
        """Mark the current session as stuck with a reason."""
        self.state.stuck = True
        self.state.log_reasoning("mark_stuck", "system", reason)
        logger.warning("Agent stuck: %s", reason)

    def get_status(self) -> dict:
        """Return current engine/state status."""
        from builder.tools.session import get_status as gs
        return gs(self.state)