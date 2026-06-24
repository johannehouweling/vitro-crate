"""Agent orchestration engine for the ISA-Tox RO-Crate Builder.

The AgentEngine manages the lifecycle of a crate-building session.
It coordinates tool calls, validation, HITL checkpoints, and session persistence.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import builder.config as _config
from builder.state import CrateState, ValidationReport
from builder.tools.profiler import ProfilingLogger

if TYPE_CHECKING:
    from builder.tools.hitl import HumanInterface
    from builder.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _directory_to_approve(scanned_path: str) -> str:
    """Return the directory to add to ``approved_scan_roots`` for *scanned_path*.

    The approved root must be a *directory*: the guard treats roots as
    directory prefixes, and nothing can be a sub-path of a file, so approving
    a file path is useless and denies every follow-up scan. The chosen
    directory is the one whose contents are actually inventoried:

    - a directory  -> the directory itself;
    - an archive   -> its extraction directory (``<stem>_extracted``), **not**
      the archive's parent — approving the parent would expose unrelated
      sibling files, weakening the D9 approved-roots guard rail;
    - any other file -> its parent directory.
    """
    from builder.tools.scanner import _is_archive

    p = Path(scanned_path).resolve()
    if p.is_dir():
        return str(p)
    if _is_archive(p):
        # Mirror unzip_file's extraction layout: <parent>/<stem>_extracted
        return str(p.parent / f"{p.stem}_extracted")
    return str(p.parent)


# Validation layers stack as a pyramid (BASE -> ISA -> TOX); ordering REQUIRED
# issues by layer puts the next *unblocking* fix first.
_VALIDATION_LAYER_ORDER = {"base": 0, "isa": 1, "tox": 2}


def _order_required_issues(issues: list[dict[str, Any]]) -> list[str]:
    """Return REQUIRED-severity issues as strings, ordered base -> isa -> tox.

    ``build_and_validate`` reports issues as routable dicts
    (``{entity_id, property, message, severity, profile, fix}``); the
    :class:`ValidationReport` stores them as human-readable strings consumed by
    ``get_hint`` and the maturity report. We keep only REQUIRED-severity issues
    (the blocking ones) and order them by validation layer so the first entry is
    the next fix that unblocks the pyramid. The sort is stable, preserving the
    validator's within-layer order.
    """
    required = [i for i in issues if i.get("severity") == "required"]
    required.sort(key=lambda i: _VALIDATION_LAYER_ORDER.get(i.get("profile") or "", 99))
    lines: list[str] = []
    for issue in required:
        profile = issue.get("profile") or "?"
        entity = issue.get("entity_id") or "?"
        message = issue.get("message") or ""
        lines.append(f"[{profile}] {entity}: {message}".rstrip())
    return lines


class AgentEngine:
    """Orchestrator for the LLM agent toolbox loop.

    The AgentEngine manages the lifecycle of a crate-building session.
    It coordinates tool calls, validation, HITL checkpoints, and
    session persistence.
    """

    _registry: ToolRegistry | None = None

    def __init__(
        self,
        state: CrateState | None = None,
        human_interface: HumanInterface | None = None,
    ):
        """Initialize the engine with optional state and HITL interface.

        Args:
            state: Existing CrateState to resume, or None for a fresh state.
            human_interface: Adapter for human-in-the-loop interaction;
                defaults to a non-interactive ``SimulatedHumanInterface``.
        """
        self.state = state or CrateState()
        if human_interface is None:
            from builder.tools.hitl import SimulatedHumanInterface

            human_interface = SimulatedHumanInterface()
        self.human_interface = human_interface
        self._running = False
        self.profiler: ProfilingLogger | None = None

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
            # Approve the directory whose contents we inventoried (the
            # extraction dir for an archive), not the raw input path — see
            # _directory_to_approve. Locks the guard even on an empty scan.
            self.state.approved_scan_roots.add(_directory_to_approve(input_path))

        if not self.state.session_id:
            self.state.session_id = _config.now().strftime("%Y%m%d_%H%M%S")
        if not self.state.created_at:
            self.state.created_at = _config.now().isoformat()
        self.state.updated_at = _config.now().isoformat()

        # Initialize the profiler now that we have a session_id
        self.profiler = ProfilingLogger(self.state.session_id)

        self.state.log_reasoning(
            "initialize",
            "scan_files",
            f"Scanned {len(self.state.scanned_files)} files",
        )
        self.state.checkpoint.completed_checkpoints.append("files_scanned")
        return self.state

    # ------------------------------------------------------------------
    # Tool registry
    # ------------------------------------------------------------------

    @classmethod
    def _build_registry(cls) -> ToolRegistry:
        """Return the shared ToolRegistry of explicitly registered tools.

        Importing each tool module triggers its bottom-of-file
        ``TOOL_REGISTRY.register(...)`` calls; imports are idempotent so this
        is cached after the first call.
        """
        if cls._registry is not None:
            return cls._registry

        import builder.tools.builder  # noqa: F401
        import builder.tools.composites  # noqa: F401
        import builder.tools.data_content  # noqa: F401
        import builder.tools.drafters  # noqa: F401
        import builder.tools.fair_assessment  # noqa: F401
        import builder.tools.lookups  # noqa: F401
        import builder.tools.management  # noqa: F401
        import builder.tools.mit_assessment  # noqa: F401
        import builder.tools.session  # noqa: F401
        import builder.tools.validation  # noqa: F401
        import builder.tools.verification  # noqa: F401
        from builder.tools.registry import TOOL_REGISTRY

        cls._registry = TOOL_REGISTRY
        return TOOL_REGISTRY

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def run_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute a tool by name with kwargs.

        Looks up the tool function from the registry and calls it.
        Records the call in the reasoning log and the profiling log
        (if a profiler is active).

        Args:
            tool_name: Name of the tool to execute.
            **kwargs: Arguments to pass to the tool function.

        Returns:
            The result of the tool function.

        Raises:
            ValueError: If the tool name is not recognised.
        """
        import time as _time

        _start = _time.perf_counter()
        scanner_tools: dict[str, Any] = {}
        try:
            from builder.tools.scanner import extract_pdf_text as ept
            from builder.tools.scanner import preview_archive as pa
            from builder.tools.scanner import read_file_sample as rfs
            from builder.tools.scanner import read_multiple_files as rmf
            from builder.tools.scanner import scan_files as sf
            from builder.tools.scanner import unzip_file as uzf

            scanner_tools = {
                "extract_pdf_text": ept,
                "scan_files": sf,
                "read_file_sample": rfs,
                "read_multiple_files": rmf,
                "unzip_file": uzf,
                "preview_archive": pa,
            }
        except ImportError:
            pass

        if tool_name == "present_to_human":
            result = self.human_interface.present(kwargs.get("context", ""), kwargs.get("options"))
        elif tool_name == "request_input":
            result = self.human_interface.request_input(
                kwargs.get("prompt", ""), kwargs.get("field_type", "text")
            )
        elif tool_name in scanner_tools:
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
                # Lock the guard to the scanned area on the first scan, even if
                # it returned nothing — otherwise approved_scan_roots stays
                # empty, run_tool keeps passing approved_roots=None, and the
                # guard is effectively disabled (any path becomes scannable).
                if not self.state.approved_scan_roots:
                    self.state.approved_scan_roots.add(
                        _directory_to_approve(kwargs.get("path", ""))
                    )
                if result:
                    self.state.scanned_files = result
                    self.state.log_reasoning(
                        "store_scan_results",
                        "scan_files",
                        f"Stored {len(result)} files in state",
                    )
                else:
                    # Empty = denied root or empty/failed scan. Do NOT clobber
                    # an existing inventory with zero — that tricked the agent
                    # into endless re-scanning.
                    self.state.log_reasoning(
                        "scan_no_results",
                        "scan_files",
                        "Scan returned no files; keeping existing inventory",
                    )
        else:
            registry = self._build_registry()
            spec = registry.get_spec(tool_name)
            if spec is None:
                raise ValueError(f"Unknown tool: {tool_name!r}")
            if spec.takes_state:
                result = spec.fn(self.state, **kwargs)
            else:
                result = spec.fn(**kwargs)

        # Fold a validation verdict back into state.validation so get_hint, the
        # interactive header, and the maturity report (#150 renders *from*
        # state.validation) reflect the latest result instead of stale defaults
        # (#153). Orchestration-layer side effect, mirroring the scan_files
        # write-back above; the validation tools themselves stay pure.
        self._writeback_validation(tool_name, result)

        self.state.iteration_count += 1
        self.state.log_reasoning(
            f"run_tool: {tool_name}",
            tool_name,
            str(result)[:300] if result is not None else "None",
        )

        # Record timing in the profiling log
        if self.profiler is not None:
            _duration = (_time.perf_counter() - _start) * 1000.0
            _args_str: str | None = None
            try:
                _args_str = str(kwargs)[:500]
            except Exception:
                pass
            # Truncate result string to avoid bloating profile
            _result_str: str | None = None
            try:
                res_text = str(result)
                if len(res_text) > 500:
                    res_text = res_text[:497] + "..."
                _result_str = res_text if result is not None else "None"
            except Exception:
                _result_str = "<unprintable>"
            self.profiler.log_tool_call(
                tool_name=tool_name,
                duration_ms=_duration,
                iteration=self.state.iteration_count,
                args=_args_str,
                result=_result_str,
            )

        return result

    def _writeback_validation(self, tool_name: str, result: Any) -> None:
        """Fold a validation result into ``state.validation`` (#153).

        ``validate`` returns a fully-formed :class:`ValidationReport` (disk,
        three-pass) — adopt it wholesale. ``build_and_validate`` returns the
        in-memory routable dict (``{"ok", "conformance", "issues"}``); map its
        per-layer ``conformance`` onto the report and record the REQUIRED issues
        ordered base -> isa -> tox. Layers absent from ``conformance`` (a scoped
        ``profile=`` call) keep their prior value, and an errored result is left
        untouched so a transient failure never wipes known issues.
        """
        if tool_name == "validate" and isinstance(result, ValidationReport):
            self.state.validation = result
            return
        if tool_name == "build_and_validate" and isinstance(result, dict):
            if "error" in result:
                return
            conformance = result.get("conformance") or {}
            if not conformance:
                return
            report = self.state.validation
            if "base" in conformance:
                report.base_passed = bool(conformance["base"])
            if "isa" in conformance:
                report.isa_passed = bool(conformance["isa"])
            if "tox" in conformance:
                report.tox_passed = bool(conformance["tox"])
            report.required_issues = _order_required_issues(result.get("issues") or [])

    def close_profiler(self) -> None:
        """Close the profiling log file, if open.

        This method is idempotent — calling it multiple times or when
        no profiler was ever created is safe.
        """
        if self.profiler is not None:
            self.profiler.close()
            self.profiler = None

    # ------------------------------------------------------------------
    # Status & introspection
    # ------------------------------------------------------------------

    def get_available_tools(self) -> list[str]:
        """Return the list of available tool names.

        Returns:
            Sorted list of all registered tool names.
        """
        registry = self._build_registry()
        extra = ["scan_files", "read_file_sample", "preview_archive"]
        return sorted(set(registry.list() + extra))

    @property
    def is_stuck(self) -> bool:
        """Detect if the agent is stuck.

        Returns True if the stuck flag is set or if the iteration count
        has reached the maximum allowed.
        """
        return self.state.is_stuck()

    def mark_stuck(self, reason: str) -> None:
        """Mark the current session as stuck with a reason."""
        self.state.mark_stuck(reason)
        logger.warning("Agent stuck: %s", reason)

    def get_status(self) -> dict:
        """Return current engine/state status."""
        from builder.tools.session import get_status as gs

        return gs(self.state)
