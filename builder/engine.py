"""Agent orchestration engine for the ISA-Tox RO-Crate Builder.

The AgentEngine manages the lifecycle of a crate-building session.
It coordinates tool calls, validation, HITL checkpoints, and session persistence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import builder.config as _config
from builder.state import CrateState, ValidationReport
from builder.tools.profiler import ProfilingLogger

if TYPE_CHECKING:
    from builder.tools.hitl import HumanInterface
    from builder.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _compact_tool_kwargs(tool_name: str, kwargs: dict[str, Any]) -> str:
    """Build a compact, human-readable string of a tool's arguments.

    Used by the progress spinner so the terminal shows e.g.
    ``resolve_compound(Silychristin A)`` rather than just the bare tool name,
    giving the user visibility into what is happening during long operations.

    Long string values (>60 chars) are truncated; ``name`` / ``query`` / ``path``
    / ``entity_type`` / ``id`` fields are prioritised for display.  Also
    reaches into ``hints`` / ``fields`` dicts to find those keys.
    """
    if not kwargs:
        return ""

    # Priority keys: pick the single most informative argument per tool.
    priority_keys = ["name", "query", "path", "entity_type", "id", "entity_id",
                     "doi", "aop_id", "title", "process_type", "accession"]

    # Crawl kwargs AND any hints/fields dict values for the first priority key.
    display_value: str | None = None
    for key in priority_keys:
        # Direct kwarg hit.
        val = kwargs.get(key)
        if val is not None:
            display_value = str(val)
            break
        # Nested inside ``hints`` / ``fields`` dict.
        for container in ("hints", "fields"):
            inner = kwargs.get(container)
            if isinstance(inner, dict):
                val = inner.get(key)
                if val is not None:
                    display_value = str(val)
                    break
        if display_value is not None:
            break

    if display_value is not None:
        if len(display_value) > 60:
            display_value = display_value[:57] + "..."
        return display_value

    # Fall back to showing compact key=value pairs.
    parts: list[str] = []
    for i, (k, v) in enumerate(kwargs.items()):
        if i >= 3:
            parts.append("...")
            break
        vs = str(v)
        if len(vs) > 40:
            vs = vs[:37] + "..."
        parts.append(f"{k}={vs}")

    return ", ".join(parts)


def _directory_to_approve(scanned_path: str) -> str | None:
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

    Returns ``None`` when the resulting directory is a forbidden root (the
    filesystem root, the user's home directory, or an OS/system tree). A
    forbidden directory can never become an approved scan root (#197).
    """
    from builder.tools.scanner import _is_archive, _is_forbidden_root

    p = Path(scanned_path).resolve()
    if p.is_dir():
        candidate = p
    elif _is_archive(p):
        # Mirror unzip_file's extraction layout: <parent>/<stem>_extracted
        candidate = p.parent / f"{p.stem}_extracted"
    else:
        candidate = p.parent

    if _is_forbidden_root(candidate):
        logger.warning("Refusing to approve forbidden scan root: %s", candidate)
        return None
    return str(candidate)


def _scan_refusal(path: str, reason: str) -> dict[str, Any]:
    """Return the result a *refused* ``scan_files`` call surfaces to the agent.

    The historical refusal was a bare ``[]`` (#197 fail-closed) with only a log
    line — the scan was denied **silently**, so the agent re-scanned in a loop
    and the user never learned *why* or *how* to grant access. A refusal must
    instead carry a human-readable reason.

    The shape is a ``dict`` (NOT a ``list``) on purpose, so it bypasses both
    list-keyed paths that handle a *successful* scan: the engine's
    ``isinstance(result, list)`` store-back (the inventory is never clobbered and
    no root is auto-approved) and the agent-loop wrapper's
    ``summarize_scan_result`` (which would render an empty list as a misleading
    "0 files" success). The ``error``/``message`` keys mirror the convention the
    other refusing file tools already use, so the LLM reads it as an actionable
    tool result and the user sees how to grant access.
    """
    return {"error": reason, "message": reason, "files": []}


# Validation layers stack as a pyramid (BASE -> ISA -> TOX); ordering REQUIRED
# issues by layer puts the next *unblocking* fix first.
_VALIDATION_LAYER_ORDER = {"base": 0, "isa": 1, "tox": 2}


def _order_issues(issues: list[dict[str, Any]], severity: str) -> list[str]:
    """Return one severity tier as stable, layer-ordered display strings."""
    selected = [i for i in issues if i.get("severity") == severity]
    selected.sort(key=lambda i: _VALIDATION_LAYER_ORDER.get(i.get("profile") or "", 99))
    return [
        f"[{i.get('profile') or '?'}] {i.get('entity_id') or '?'}: {i.get('message') or ''}".rstrip()
        for i in selected
    ]


def _order_required_issues(issues: list[dict[str, Any]]) -> list[str]:
    """Return REQUIRED-severity issues as strings, ordered base -> isa -> tox."""
    return _order_issues(issues, "required")


# Upper bound on the per-engine build_and_validate result cache (#155). The
# distinct keys a session produces ~= the number of materially different crate
# states it validates; the cap is a safety net against unbounded growth.
_VALIDATION_CACHE_MAX = 64


def _validation_input_hash(state: CrateState) -> str:
    """Hash the inputs ``build_and_validate`` consumes: entities + crate metadata.

    Thin delegation to :meth:`builder.state.CrateState.validation_fingerprint`,
    which is where the definition lives so both build arms can reach it without
    importing the engine (#380). Kept as a module-level name because the debounce
    key below and ``tests/test_validation_debounce.py`` both import it.
    """
    return state.validation_fingerprint()


# File-reading tools that take a model-supplied path and must be sandboxed to
# ``approved_scan_roots`` before they touch disk (#167). ``scan_files`` already
# receives its own approved-roots injection and is handled separately; the rest
# are gated by :meth:`AgentEngine._gate_file_read`. The value is the kwarg that
# names the path(s): a single string for the per-file readers, ``"paths"`` (a
# list) for ``read_multiple_files``.
_FILE_READ_TOOLS: dict[str, str] = {
    "read_file": "path",
    "read_excel": "path",
    "read_docx": "path",
    "read_file_sample": "path",
    "extract_pdf_text": "path",
    "preview_archive": "path",
    "unzip_file": "path",
    "read_multiple_files": "paths",
    "scan_files": "path",  # gated via its own injection, listed for completeness
}

# Sentinel meaning "the gate allows this call to proceed unchanged". A distinct
# object (never a tool's own return value) so ``is`` comparison is unambiguous.
_GATE_OK = object()

# Max length of the compact call-args repr embedded in each reasoning_log entry
# (#240). Bounded so a big ``hints`` blob or a long path can't bloat the log.
_REASONING_ARGS_MAX = 240


def _compact_args_repr(kwargs: dict[str, Any], *, max_len: int = _REASONING_ARGS_MAX) -> str:
    """Render tool call kwargs as a compact, truncated string for the log (#240).

    The reasoning_log used to record only the tool RESULT, so you couldn't tell
    which path ``read_file`` was called with. This embeds a bounded repr of the
    arguments in each entry. Values that fail to repr are rendered defensively so
    logging never raises.
    """
    if not kwargs:
        return ""
    try:
        rendered = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    except Exception:  # never let logging crash on an exotic value
        rendered = str(list(kwargs.keys()))
    if len(rendered) > max_len:
        rendered = rendered[: max_len - 1] + "…"
    return rendered


def _run_document_discovery(engine: AgentEngine) -> None:
    """Run deterministic document discovery after file scanning and store results in state.

    Uses the shared ``discover_documents`` function which screens scanned files for
    readable scientific documentation (SOPs, protocols, publications, metadata files,
    data dictionaries, sample sheets, assay/process docs) and ranks them by content
    signals, filename clues, and directory depth. The formatted context is stored on
    ``engine.state.documents`` for both arms:

    - **ReAct**: the state brief includes a ``Documents: N`` line and the context is
      appended as a document-context SystemMessage block.
    - **Pipeline**: :func:`builder.agents.pipeline.pipeline._gather_context` folds
      the discovered documents into the drafter/extraction leaf context.

    Discovery is best-effort and bounded (``_MAX_CONTEXT_CHARS`` caps preview text).
    A failure never breaks initialization — it just leaves ``state.documents`` empty.
    """
    from builder.tools.document_discovery import (
        discover_documents,
        format_document_context,
    )

    approved = list(engine.state.approved_scan_roots)
    root = approved[0] if approved else engine.state.metadata.input_path or ""
    if not root:
        return

    candidates = discover_documents(
        engine.state.scanned_files,
        input_root=root,
        approved_roots=engine.state.approved_scan_roots,
    )
    context = format_document_context(candidates)
    engine.state.documents = [
        {
            "role": c.role,
            "filename": c.filename,
            "relative_path": c.relative_path,
            "score": c.score,
            "reasons": list(c.reasons),
        }
        for c in candidates
    ]
    if context and engine.state.metadata:
        logger.info(
            "Document discovery: %d candidates, ~%d chars of context",
            len(candidates),
            len(context),
        )


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
        # Optional tool-event observer (#266): invoked at the start and end of
        # every ``run_tool`` call with ``(tool_name, "start"|"end")``. Default
        # ``None`` is a strict no-op (no behavior change). The deterministic
        # pipeline runs tools via ``run_tool`` (not LangChain), so this is the
        # single hook the interactive build's progress spinner subscribes to in
        # order to show the currently-running tool. A callback that raises must
        # never break ``run_tool`` (it is UI chrome).
        # Optional observer for tool execution lifecycle (Issue #266). Fired with
        # ``(tool_name, phase, kwargs_str)`` — the third argument is a compact
        # representation of the tool's arguments so UI chrome can show e.g.
        # ``resolve_compound(Silychristin A)`` rather than just the tool name.
        # ``None`` means no observer (a strict no-op — the default, so behaviour
        # is unchanged when unset). A raising observer is logged but never
        # propagated (it is UI chrome).
        self.on_tool_event: Callable[[str, str, str], None] | None = None
        # Per-session memo of build_and_validate results keyed on
        # (validation-input hash, profile, severity) so consecutive validations
        # of an unchanged crate skip the ~3.7s SHACL re-run (#155).
        self._validation_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(self, input_path: str | None = None) -> CrateState:
        """Run initialization: scan files, discover documents, create initial state."""
        from builder.tools.scanner import scan_files

        if input_path:
            # Approve the directory whose contents we inventory (the extraction
            # dir for an archive), not the raw input path — see
            # _directory_to_approve. This is a user-provided input path, the
            # only legitimate way (besides a real approval) for a root to enter
            # the allowlist (#197). A forbidden root yields None and is refused.
            self.state.metadata.input_path = input_path
            approved = _directory_to_approve(input_path)
            if approved is not None:
                self.state.approved_scan_roots.add(approved)
                # The scanner fails closed, so it must receive a concrete
                # allowlist. For an archive the literal input path differs from
                # its extraction dir, so approve the input path for this one
                # scan too; the persistent root remains the extraction dir.
                scan_roots = self.state.approved_scan_roots | {str(Path(input_path).resolve())}
                self.state.scanned_files = scan_files(input_path, approved_roots=scan_roots)

                # --- Document discovery (#179) ---
                # After the file inventory is built, run the deterministic,
                # bounded document discovery to rank SOPs, protocols, publications,
                # metadata files, and other scientific documentation. The result
                # is stored in CrateState and consumed by both the ReAct state
                # brief and the pipeline's _gather_context.
                if self.state.scanned_files and approved:
                    try:
                        _run_document_discovery(self)
                    except Exception as exc:  # noqa: BLE001 — discovery is best-effort
                        logger.warning("Document discovery failed (continuing): %s", exc)
            else:
                logger.warning(
                    "Refusing to initialize scan on forbidden input path: %s", input_path
                )

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
        import builder.tools.repair  # noqa: F401
        import builder.tools.session  # noqa: F401
        import builder.tools.validation  # noqa: F401
        import builder.tools.verification  # noqa: F401
        from builder.tools.registry import TOOL_REGISTRY

        cls._registry = TOOL_REGISTRY
        return TOOL_REGISTRY

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _gate_file_read(self, tool_name: str, kwargs: dict[str, Any]) -> Any:
        """Sandbox a file-reading tool to ``approved_scan_roots`` (#167).

        Returns :data:`_GATE_OK` when the call may proceed unchanged. Otherwise
        returns the tool's *refusal* value — ``None`` for the single-path readers
        and ``preview_archive``, an ``error`` dict for ``unzip_file`` — so the
        agent sees a benign "unreadable" result rather than escaping the sandbox.

        For ``read_multiple_files`` the out-of-root paths are filtered out
        in-place (added to ``skipped``) so an in-tree batch still works; only
        when *every* path is refused do we short-circuit with an empty result.

        The check is fail-closed: with no approved roots, every read is refused.
        Containment resolves the realpath, so a symlink escaping the tree fails.
        """
        from builder.tools.scanner import _contain

        roots = self.state.approved_scan_roots

        if tool_name == "read_multiple_files":
            paths = kwargs.get("paths") or []
            allowed = [p for p in paths if _contain(p, roots) is not None]
            refused = [p for p in paths if p not in allowed]
            # Stash the refused paths so the dispatch can fold them back into the
            # tool's own ``skipped`` list (the tool only sees the filtered batch).
            self._read_multiple_refused = refused
            if not allowed:
                return {
                    "files": {},
                    "count": 0,
                    "skipped": list(paths),
                    "message": ("Refused: no path was inside an approved scan root (#167)."),
                }
            if refused:
                kwargs["paths"] = allowed
            return _GATE_OK

        path = kwargs.get(_FILE_READ_TOOLS[tool_name])
        if path is not None and _contain(path, roots) is not None:
            return _GATE_OK

        logger.warning(
            "Refusing %s on %s — not inside an approved scan root (#167)",
            tool_name,
            path,
        )
        if tool_name == "unzip_file":
            return {
                "error": f"Refused: {path} is not inside an approved scan root.",
                "message": "Refused to extract a path outside the approved roots.",
            }
        return None

    def _authorize_scan_root(self, path: str) -> dict[str, Any] | None:
        """Authorise a ``scan_files`` *path* that is not yet an approved root.

        Implements the prompt-once, children-only design for a folder the user
        points the agent at without ``--input``:

        - If *path* already descends from an approved root, returns ``None`` —
          the scan proceeds unchanged with NO prompt (the ``--input`` flow and
          any already-approved subtree are untouched).
        - Otherwise, with a REAL interactive human (:func:`is_interactive`),
          prompt once via ``present(purpose=SCAN_ROOT_PURPOSE)``. On approval the
          SUBMITTED directory only (never its parent; see
          :func:`_directory_to_approve`) is added to ``approved_scan_roots`` and
          ``None`` is returned so the scan runs with the widened allowlist. A
          bare/forbidden root yields ``None`` from ``_directory_to_approve`` and
          is REFUSED even on a "yes" (the denylist stands, #197).
        - With no interactive human (SimulatedHumanInterface / None — eval,
          batch, tests) nothing is auto-approved: fail closed (#197/#198).

        Returns ``None`` to let the scan proceed, or a refusal dict (from
        :func:`_scan_refusal`) the caller must return *instead* of scanning —
        never a silent empty result.
        """
        from builder.tools.hitl import SCAN_ROOT_PURPOSE, is_interactive
        from builder.tools.scanner import _contain

        # Already inside an approved root (e.g. via --input): scan directly.
        if _contain(path, self.state.approved_scan_roots) is not None:
            return None

        if not is_interactive(self.human_interface):
            # Headless/simulated: never widen filesystem access on the agent's
            # say-so. Surface the reason rather than a silent empty result.
            logger.warning(
                "Refusing scan of unapproved path %s — no interactive human to "
                "approve a new scan root (#197/#198).",
                path,
            )
            self.state.log_reasoning(
                "refuse_scan_root",
                "scan_files",
                f"Refused: {path} is not an approved scan root (non-interactive).",
            )
            return _scan_refusal(
                path,
                f"Refused: {path} is not an approved scan root. Re-run with "
                f"--input {path} to grant access.",
            )

        # Real user: ask once whether to approve this folder for scanning.
        decision = self.human_interface.present(
            context=(
                f"The agent wants to scan a folder you did not pass via --input:\n"
                f"  {path}\n"
                "Approving grants read access to this folder and its children "
                "only (never its parent)."
            ),
            purpose=SCAN_ROOT_PURPOSE,
        )
        if decision.get("action") != "approved":
            logger.info("User declined scan-root approval for %s", path)
            self.state.log_reasoning(
                "refuse_scan_root",
                "scan_files",
                f"User declined approval to scan {path}.",
            )
            return _scan_refusal(
                path,
                f"Refused: you declined approval to scan {path}.",
            )

        approved = _directory_to_approve(path)
        if approved is None:
            # A bare/forbidden root is never approvable, even with consent.
            logger.warning("Refusing approved-by-user but forbidden scan root: %s", path)
            self.state.log_reasoning(
                "refuse_scan_root",
                "scan_files",
                f"Refused: {path} is a forbidden root (denylist) and cannot be "
                "approved even with consent.",
            )
            return _scan_refusal(
                path,
                f"Refused: {path} is a filesystem/system root and can never be "
                "approved for scanning.",
            )

        self.state.approved_scan_roots.add(approved)
        logger.info("User approved new scan root: %s", approved)
        self.state.log_reasoning(
            "approve_scan_root",
            "scan_files",
            f"User approved {approved} as a scan root (children only).",
        )
        return None

    def _fire_tool_event(self, tool_name: str, phase: str, kwargs_str: str = "") -> None:
        """Notify the optional ``on_tool_event`` observer (#266).

        Best-effort: a ``None`` observer is a no-op, and an observer that raises
        is logged but never propagated — the callback is UI chrome (the interactive
        build's progress spinner) and must never break a tool call.

        Args:
            tool_name: The name of the tool being executed.
            phase: ``"start"`` or ``"end"``.
            kwargs_str: Compact string representation of the tool arguments,
                e.g. ``'Silychristin A'`` for ``resolve_compound``.
        """
        cb = self.on_tool_event
        if cb is None:
            return
        try:
            cb(tool_name, phase, kwargs_str)
        except Exception:  # noqa: BLE001 — a UI callback must never break run_tool
            logger.debug("on_tool_event(%s, %s, %s) raised", tool_name, phase, kwargs_str, exc_info=True)

    def run_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute a tool by name with kwargs.

        Looks up the tool function from the registry and calls it.
        Records the call in the reasoning log and the profiling log
        (if a profiler is active).

        Fires the optional ``on_tool_event`` observer with
        ``(tool_name, "start", kwargs_str)`` before and ``(tool_name, "end", "")``
        after the call (#266) — the "end" event fires even when the tool raises
        (``finally``-guarded), and a raising observer never breaks the call.
        The observer defaults to ``None`` (a strict no-op), so behaviour is
        unchanged when it is unset.

        Args:
            tool_name: Name of the tool to execute.
            **kwargs: Arguments to pass to the tool function.

        Returns:
            The result of the tool function.

        Raises:
            ValueError: If the tool name is not recognised.
        """
        kwargs_str = _compact_tool_kwargs(tool_name, kwargs)
        self._fire_tool_event(tool_name, "start", kwargs_str)
        try:
            return self._run_tool_impl(tool_name, **kwargs)
        finally:
            self._fire_tool_event(tool_name, "end")

    def _run_tool_impl(self, tool_name: str, **kwargs) -> Any:
        """The actual tool dispatch (wrapped by :meth:`run_tool` for events)."""
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

        # -- Security sandbox: contain every file-reading tool (#167) -----------
        # The read/extract/archive tools take a model-supplied path and would
        # otherwise read ANY local file (e.g. ``read_file('/etc/passwd')``), so a
        # prompt-injected metadata file could exfiltrate secrets. Gate them here
        # against ``approved_scan_roots`` with the SAME fail-closed containment
        # the scanner uses (#198/#197): a path outside an approved root — or any
        # read at all when no root is approved — is refused before the file is
        # touched. Symlink escape is refused because containment resolves the
        # realpath. ``scan_files`` keeps its own dedicated injection above.
        self._read_multiple_refused: list[str] = []
        if tool_name in _FILE_READ_TOOLS and tool_name != "scan_files":
            refused = self._gate_file_read(tool_name, kwargs)
            if refused is not _GATE_OK:
                self.state.iteration_count += 1
                self.state.log_reasoning(
                    "refuse_unsandboxed_read",
                    tool_name,
                    "Refused: path outside approved scan roots (#167)",
                )
                return refused

        # build_and_validate debounce (#155): when the validation inputs
        # (entities + crate metadata) and the requested scope are unchanged since
        # the last call, reuse the cached result and skip the ~3.7s SHACL re-run.
        # The key excludes validation/assessment OUTPUTS, so the #153 write-back
        # does not invalidate it.
        debounce_key: tuple[str, str, str] | None = None
        debounce_hit = False
        if tool_name == "build_and_validate":
            profile = kwargs.get("profile") or "all"
            severity = kwargs.get("severity") or "required"
            debounce_key = (_validation_input_hash(self.state), profile, severity)
            cached = self._validation_cache.get(debounce_key)
            if cached is not None:
                result = dict(cached)
                debounce_hit = True

        if debounce_hit:
            pass  # cached build_and_validate result reused; SHACL skipped
        elif tool_name == "present_to_human":
            # Emit a hitl_wait marker *before* blocking on the human (#193). The
            # tool_call event is only logged after this returns — i.e. after the
            # human responds — so without this marker a pending HITL pause is
            # invisible to the dashboard's ▶/⏸ status inference.
            if self.profiler is not None:
                self.profiler.log_event(event="hitl_wait", tool=tool_name)
            result = self.human_interface.present(kwargs.get("context", ""), kwargs.get("options"))
        elif tool_name == "request_input":
            if self.profiler is not None:
                self.profiler.log_event(event="hitl_wait", tool=tool_name)
            result = self.human_interface.request_input(
                kwargs.get("prompt", ""), kwargs.get("field_type", "text")
            )
        elif tool_name in scanner_tools:
            tool_fn = scanner_tools[tool_name]
            # Prompt-once, children-only approval for a user-submitted folder
            # that is NOT yet an approved root. A REAL interactive human can
            # approve it once (the submitted dir only, never its parent, never a
            # forbidden root); headless runs stay fail-closed. A refusal returns
            # a reason dict instead of scanning — never a silent empty result.
            if tool_name == "scan_files":
                refusal = self._authorize_scan_root(kwargs.get("path", ""))
                if refusal is not None:
                    self.state.iteration_count += 1
                    self.state.log_reasoning(
                        "refuse_scan_unapproved_root",
                        tool_name,
                        str(refusal.get("message", "Scan refused"))[:300],
                    )
                    return refusal
            # Inject approved roots for scan_files. Fail closed (#197): always
            # pass a concrete allowlist — an EMPTY set, never None — so the
            # scanner refuses when nothing has been approved. A new root enters
            # the allowlist only via initialize() (a user-provided input path) or
            # the explicit approval handled by _authorize_scan_root above.
            tool_kwargs = dict(kwargs)
            if tool_name == "scan_files":
                tool_kwargs["approved_roots"] = self.state.approved_scan_roots.copy()
            result = tool_fn(**tool_kwargs)
            # Fold sandbox-refused paths back into read_multiple_files' own
            # ``skipped`` list so the agent sees them as unread (#167).
            if (
                tool_name == "read_multiple_files"
                and self._read_multiple_refused
                and isinstance(result, dict)
            ):
                result["skipped"] = list(result.get("skipped", [])) + [
                    p for p in self._read_multiple_refused if p not in result.get("skipped", [])
                ]
            # Auto-store scan results in state. Do NOT register the scanned path
            # as an approved root here — that fail-open auto-approve (#197) let
            # the agent scan arbitrary locations by simply naming them.
            if tool_name == "scan_files" and isinstance(result, list):
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
            call_kwargs = dict(kwargs)
            # Inject the active HITL adapter for tools that escalate ambiguous
            # decisions to the human (e.g. draft_publication_with_authors, #180).
            if spec.takes_human:
                call_kwargs["human_interface"] = self.human_interface
            if spec.takes_state:
                result = spec.fn(self.state, **call_kwargs)
            else:
                result = spec.fn(**call_kwargs)

        # Memoize a fresh, non-error build_and_validate result for the debounce
        # above (#155). Bounded so a long session cannot grow the cache without
        # limit; errored results are never cached so a retry re-runs.
        if (
            tool_name == "build_and_validate"
            and debounce_key is not None
            and not debounce_hit
            and isinstance(result, dict)
            and "error" not in result
        ):
            if len(self._validation_cache) >= _VALIDATION_CACHE_MAX:
                self._validation_cache.pop(next(iter(self._validation_cache)))
            self._validation_cache[debounce_key] = dict(result)

        # Fold a validation verdict back into state.validation so get_hint, the
        # interactive header, and the maturity report (#150 renders *from*
        # state.validation) reflect the latest result instead of stale defaults
        # (#153). Orchestration-layer side effect, mirroring the scan_files
        # write-back above; the validation tools themselves stay pure.
        self._writeback_validation(tool_name, result)

        self.state.iteration_count += 1
        # Embed a compact, bounded repr of the call args in the action so the log
        # shows WHICH path/hints a tool was called with — not just its result
        # (#240). Previously this was just "run_tool: <name>", hiding the args.
        _args_repr = _compact_args_repr(kwargs)
        _action = f"run_tool: {tool_name}"
        if _args_repr:
            _action = f"{_action}({_args_repr})"
        self.state.log_reasoning(
            _action,
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
            issues = result.get("issues") or []
            severity = str(result.get("severity") or "required")
            if severity == "required":
                report.required_issues = _order_issues(issues, "required")
                report.assessed_tiers.add("required")
            elif severity == "recommended":
                report.should_issues = _order_issues(issues, "recommended")
                report.assessed_tiers.add("recommended")
            elif severity == "optional":
                report.may_issues = _order_issues(issues, "optional")
                report.assessed_tiers.add("optional")

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
