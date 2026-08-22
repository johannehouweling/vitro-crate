"""Agent orchestration engine for the ISA-Tox RO-Crate Builder.

The AgentEngine manages the lifecycle of a crate-building session.
It coordinates tool calls, validation, HITL checkpoints, and session persistence.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import builder.config as _config
from builder.state import CrateState
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

    # Priority keys: pick the single most informative argument per tool. What is
    # being ACTED ON comes before what is being written: `set_fields(entity_id=…,
    # fields={"name": "x"})` is a fact about that entity, not about the string
    # "x", and rendering the payload made a failed call unattributable.
    priority_keys = ["entity_id", "name", "query", "path", "entity_type", "id",
                     "doi", "aop_id", "title", "process_type", "accession"]

    display_value: str | None = None
    # DIRECT kwargs first, all of them, before reaching into a nested dict. The
    # single-pass version tried `fields["name"]` before it ever looked at the
    # top-level `entity_id`, so the label described the value rather than the
    # target — and every mutation to a differently-named entity looked alike.
    for key in priority_keys:
        val = kwargs.get(key)
        if val is not None:
            display_value = str(val)
            break
    if display_value is None:
        for key in priority_keys:
            for container in ("hints", "fields"):
                inner = kwargs.get(container)
                if isinstance(inner, dict) and inner.get(key) is not None:
                    display_value = str(inner[key])
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
    """Return one severity tier as stable, layer-ordered display strings.

    Thin delegation to :func:`builder.tools.validation.order_issues`, which is
    where the definition lives so the engine write-back and ``export_crate``'s
    own validation produce byte-identical reports. Kept as a module-level name
    because ``tests/test_validation_writeback.py`` imports it from here.
    """
    from builder.tools.validation import order_issues

    return order_issues(issues, severity)


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
# Per-document cap. Sized so ordinary submission documents are stored WHOLE: a
# 22.8k-char SOP and a 32.5k-char study JSON were both cut to 12k, and the guard
# then served those fragments back under the claim that they were "identical to
# re-reading it". The model was missing half of one document and two-thirds of
# the other, was told nothing was missing, and has no offset parameter to ask
# for the rest — so it asked again, and again. A partial copy is no longer
# served at all (see the reader guard), which makes this cap the line between
# "answered from memory" and "re-read from disk" rather than a silent edit.
_DOCUMENT_EVIDENCE_MAX_CHARS = 40000
# The store must hold a whole working set, not most of one. A typical assay
# submission is three documents — workbook, SOP, study JSON — which at full
# length come to 8.7k + 22.8k + 32.5k = 64k chars. At the old 30k ceiling that
# set did not fit, so every read evicted the document the model was about to ask
# for next: one observed session re-read the same three files sixteen times
# across fifteen turns, never once hitting the cache. Sized for four whole
# documents at the per-document cap. This budget is storage only — the
# prompt-side render (`_format_document_evidence`) has its own independent 12k
# cap, so raising it costs session state, not context.
_DOCUMENT_EVIDENCE_MAX_TOTAL_CHARS = 160000

# Readers whose successful output is kept as bounded session evidence, so the
# "already loaded this document" guard can suppress an identical re-read. These
# take a single ``path`` kwarg and return text.
_DOCUMENT_EVIDENCE_TOOLS = frozenset(
    {"read_file", "read_excel", "read_docx", "read_file_sample"}
)

# Reader outputs that can be losslessly squeezed before they reach the model.
_JSON_SUFFIXES = (".json", ".jsonld")


def _compact_reader_text(tool_name: str, path: Any, result: Any) -> Any:
    """Strip formatting-only bulk from a reader result. Never drops content.

    A pretty-printed study record is half indentation: the session's
    ``S-VHPS26.json`` reads as 32,485 characters (~7,981 tokens) and re-serialises
    to 15,350 (~4,372) with the identical object inside. Whitespace is the one
    thing in a document that costs context and carries nothing, so it goes —
    every key, every value and every ordering is preserved, which is what makes
    this safe to do without being asked.

    Compaction is the STARTING point, not a ceiling: the full text is still what
    is stored and served, and anything that will not round-trip is returned
    untouched rather than guessed at.
    """
    if tool_name not in _DOCUMENT_EVIDENCE_TOOLS or not isinstance(result, str):
        return result
    if not isinstance(path, str) or not path.lower().endswith(_JSON_SUFFIXES):
        return result
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return result  # not valid JSON (a sample/slice?) — leave it exactly as read
    try:
        compacted = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover — json.loads output is dumpable
        return result
    return compacted if len(compacted) < len(result) else result


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
        classify_scanned_files,
        discover_documents,
        format_document_context,
    )

    approved = list(engine.state.approved_scan_roots)
    root = approved[0] if approved else engine.state.metadata.input_path or ""
    if not root:
        return

    # Classify EVERY scanned file before ranking any of them (#591). The ranking
    # exists to fill a bounded prompt and shows a capped subset; what the crate is
    # built from must not depend on what fits in a context window. Every file is
    # stamped, but not every file is opened — a folder of interchangeable
    # instrument output is sampled (#598). The previews are handed on so the
    # deposit is read once.
    previews = classify_scanned_files(
        engine.state.scanned_files,
        input_root=root,
        approved_roots=engine.state.approved_scan_roots,
    )
    candidates = discover_documents(
        engine.state.scanned_files,
        input_root=root,
        approved_roots=engine.state.approved_scan_roots,
        previews=previews,
    )
    # Pass the scan size so the context can say what it left out: the ranking
    # decides what the agent sees at all, and a silent cap reads as "this is
    # everything" (#587).
    context = format_document_context(candidates, total_scanned=len(engine.state.scanned_files))
    engine.state.documents = [
        {
            "kind": c.kind,
            "classification": c.classification,
            "filename": c.filename,
            "relative_path": c.relative_path,
            "score": c.score,
            "reasons": list(c.reasons),
            "preview": c.preview,
        }
        for c in candidates
    ]
    if context and engine.state.metadata:
        logger.info(
            "Document discovery: %d candidates, ~%d chars of context",
            len(candidates),
            len(context),
        )


# Formats that carry a NAMED licence field, and the filenames whose whole
# content is one. Prose is deliberately absent: every real deposit's README
# ships the unfilled placeholder "[Default CC-BY 4.0 for data, CC0 for metadata
# unless specified otherwise]", which names two licences and declares neither
# (#535). Any other text is still read for an `SPDX-License-Identifier:`, which
# is a formal declaration rather than prose.
_LICENCE_DOCUMENT_SUFFIXES = (".json", ".jsonld", ".yaml", ".yml", ".cff", ".xml")
_LICENCE_FILE_STEMS = frozenset({"license", "licence", "copying", "copyright"})


def _may_declare_a_licence(path: str) -> bool:
    """Whether *path* is a document a licence can be READ from, by name alone."""
    name = Path(path)
    return name.suffix.lower() in _LICENCE_DOCUMENT_SUFFIXES or (
        name.stem.casefold() in _LICENCE_FILE_STEMS
    )


def _read_declared_licence(engine: AgentEngine) -> None:
    """Read the licence the deposit declares, before anyone drafts one (#535).

    Nothing used to read it: ``set_metadata`` — an LLM-callable tool — was the
    only writer, so the licence on the Root Data Entity was whatever the model
    supplied, and assembly asserted all-rights-reserved when it supplied
    nothing. On a real deposit the guess inverted the depositor's: S-VHPS26
    declares CC-BY-4.0 and its crate claimed all rights reserved — wrong in the
    one direction that suppresses reuse of openly-licensed data.

    Runs beside document discovery, for the same reason: it is deterministic,
    bounded, and independent of which arm is driving, so both get the fact. Only
    a licence nobody has set yet is filled — a resumed session that already
    carries one is left alone — and the value is marked as read from the deposit
    so a later draft cannot overwrite it.

    More than one file can name a licence, so which one is the DEPOSIT's is
    decided rather than left to directory order: the shallowest declaration
    wins, because a file at the deposit root describes the deposit while a
    bundled manifest four directories down describes itself. Depth outranks
    everything — a nested SPDX URI must not beat the root descriptor's own
    label — then a machine-actionable IRI, then the path, so the answer is the
    same on every run.
    """
    from builder.tools.file_readers import extract_deposit_licence, read_file

    metadata = engine.state.metadata
    if metadata.license:
        return

    root = metadata.input_path or ""
    found: list[tuple[int, int, str, str]] = []
    for candidate in engine.state.scanned_files:
        path = str(getattr(candidate, "path", "") or "")
        if not _may_declare_a_licence(path):
            continue
        try:
            text = read_file(path)
        except Exception:  # noqa: BLE001 — an unreadable file is simply not the one
            continue
        licence = extract_deposit_licence(text or "", filename=Path(path).name)
        if not licence:
            continue
        try:
            depth = len(Path(path).resolve().relative_to(Path(root).resolve()).parts)
        except (ValueError, OSError):
            depth = len(Path(path).parts)
        actionable = 0 if licence.startswith(("http://", "https://")) else 1
        found.append((depth, actionable, path, licence))

    if not found:
        return
    depth, _, path, licence = min(found)
    metadata.license = licence
    metadata.license_from_deposit = True
    logger.info("Read the licence the deposit declares from %s: %s", path, licence)


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

    def ensure_profiler(self) -> None:
        """Attach the session profiler without rescanning or resetting state."""
        profiler_session = getattr(self.profiler, "_session_id", None)
        if self.profiler is None or profiler_session != self.state.session_id:
            if self.profiler is not None:
                self.profiler.close()
            self.profiler = ProfilingLogger(self.state.session_id)

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
                    try:
                        _read_declared_licence(self)
                    except Exception as exc:  # noqa: BLE001 — best-effort, like discovery
                        logger.warning("Reading the declared licence failed: %s", exc)
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

        import builder.tools.air_assessment  # noqa: F401
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

    def _store_document_evidence(
        self, tool_name: str, path: str, result: Any, kwargs: dict[str, Any]
    ) -> None:
        """Store bounded successful reader output in serializable session state."""
        if not isinstance(result, str) or not result.strip():
            return
        try:
            resolved = Path(path).resolve()
            root = next(Path(r).resolve() for r in self.state.approved_scan_roots)
            relative = str(resolved.relative_to(root))
        except (OSError, RuntimeError, StopIteration, ValueError):
            return
        content = result[:_DOCUMENT_EVIDENCE_MAX_CHARS]
        evidence = dict(self.state.document_evidence)
        # Re-insert rather than overwrite: assigning to an existing key keeps its
        # original position, and eviction below pops from the front. Deleting
        # first moves the entry to the back, so the front stays "least recently
        # used" (`touch_document_evidence` does the same on a cache hit).
        evidence.pop(relative, None)
        evidence[relative] = {
            "tool": tool_name,
            "path": relative,
            "content": content,
            "truncated": len(result) > len(content),
            "args": {k: v for k, v in kwargs.items() if k != "path"},
        }
        # Never evict down to nothing: a single document larger than the whole
        # budget would otherwise drop itself and cache nothing at all.
        while (
            len(evidence) > 1
            and sum(len(str(item.get("content", ""))) for item in evidence.values())
            > _DOCUMENT_EVIDENCE_MAX_TOTAL_CHARS
        ):
            dropped = next(iter(evidence))
            evidence.pop(dropped)
            # Not silent: an eviction means the next read of that document pays a
            # full re-read, and a run of these is the signature of a working set
            # that does not fit the budget.
            logger.info(
                "Evidence budget full — dropped %s to make room for %s", dropped, relative
            )
        self.state.document_evidence = evidence

    def touch_document_evidence(self, key: str) -> None:
        """Mark stored evidence as most recently used, so eviction skips it.

        Called when the reader guard serves a document from evidence instead of
        re-reading it. Without it the store ages by insertion time alone, and the
        document the model keeps asking for is exactly the one evicted next.
        """
        evidence = self.state.document_evidence
        item = evidence.get(key)
        if item is None:
            return
        reordered = {k: v for k, v in evidence.items() if k != key}
        reordered[key] = item
        self.state.document_evidence = reordered

    def _resolve_within_roots(self, path: Any) -> str | None:
        """Resolve a bare filename to an absolute path inside an approved root.

        Returns ``None`` unless *path* has no directory component and exactly one
        file with that name exists under the approved roots — an ambiguous name
        (the same basename in two subfolders) is left for the caller to refuse,
        because picking one would silently read a file the agent did not ask for.

        Prefers the scanned-file inventory (already built, no disk walk) and falls
        back to a bounded ``rglob`` when the inventory is empty or predates the
        file. Containment is re-checked on the result, so this can only ever widen
        the agent's reach to files the sandbox would already have allowed.
        """
        from builder.tools.scanner import _contain

        if not isinstance(path, str) or not path.strip():
            return None
        raw = path.strip()
        name = Path(raw).name
        if not name:
            return None

        # A path RELATIVE to the input root ("Assay_OATP1C1/Assay-metadata.xlsx")
        # is the other shape a model naturally emits: it is how the file appears
        # in the crate and in the inventory's relative listings. Resolved against
        # the CWD it lands outside every approved root and was refused — one
        # weak-model session lost 11 of 20 failed tool calls this way, retrying
        # the same workbook six times. Joining it to an approved root can only
        # reach files the sandbox already allows, and containment is re-checked.
        if name != raw and not Path(raw).is_absolute():
            for root in self.state.approved_scan_roots:
                try:
                    candidate = Path(root) / raw
                    if not candidate.is_file():
                        continue
                    resolved = str(candidate.resolve())
                except (OSError, RuntimeError, ValueError):
                    continue
                if _contain(resolved, self.state.approved_scan_roots) is not None:
                    return resolved
            # FALL THROUGH to the basename match rather than refusing. A model
            # that gets the folder wrong writes "Assay_OATP1C1/S-VHPS26.json"
            # for a file sitting at the root, and returning None here refused it
            # outright — while the identical mistake in ABSOLUTE form was already
            # being repaired below. One session spent its whole budget retrying
            # that path until the loop-breaker fired. The filename is the part
            # the model got right; containment is re-checked on whatever matches,
            # and an ambiguous name still refuses rather than guessing.
            #
            # NOT for a path that tries to climb out, though. A wrong subfolder is
            # a typo worth helping past; `..` is an escape attempt, and answering
            # one with the contents of a same-named file that happens to sit
            # inside the sandbox is exactly what the traversal guard exists to
            # stop. The observed weak-model mistake never contains `..`.
            if ".." in Path(raw).parts:
                return None

        matches: list[str] = []
        for scanned in self.state.scanned_files:
            candidate = getattr(scanned, "path", None)
            if candidate and Path(candidate).name == name:
                matches.append(str(candidate))

        if not matches:
            for root in self.state.approved_scan_roots:
                try:
                    matches.extend(str(p) for p in Path(root).rglob(name) if p.is_file())
                except (OSError, RuntimeError, ValueError):
                    continue

        unique = sorted({str(Path(m).resolve()) for m in matches})
        if len(unique) != 1:
            if len(unique) > 1:
                logger.info(
                    "Not resolving bare filename %r — %d files share that name",
                    path,
                    len(unique),
                )
            return None
        return unique[0] if _contain(unique[0], self.state.approved_scan_roots) else None

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

        path_kwarg = _FILE_READ_TOOLS[tool_name]
        path = kwargs.get(path_kwarg)
        if path is not None and _contain(path, roots) is not None:
            # Contained, but does it exist? A model that guesses the right root and
            # the wrong subfolder ("<root>/workbook.xlsx" for
            # "<root>/Assay_OATP1C1/workbook.xlsx") produced a path that passes
            # containment, so it skipped the basename rescue below and died as an
            # "unreadable file" — telling the model the workbook was missing or
            # corrupt when it was neither. One session then spent fifteen turns
            # hunting for a file it had named correctly all along. Fall through to
            # the same unambiguous-basename resolution used for outside-root paths.
            try:
                exists = Path(path).is_file()
            except (OSError, RuntimeError, ValueError):
                exists = False
            if exists or tool_name in ("scan_files", "preview_archive", "unzip_file"):
                return _GATE_OK
            relocated = self._resolve_within_roots(path)
            if relocated is None:
                return _GATE_OK  # let the reader report it as unreadable
            kwargs[path_kwarg] = relocated
            logger.info(
                "Path %r is inside an approved root but does not exist — reading the "
                "one file of that name that does: %s",
                path,
                relocated,
            )
            return _GATE_OK

        # A bare filename ("OATP1C1 SOP TH 250425.docx") is what the model
        # naturally emits — it sees filenames in the scanned-file inventory, not
        # absolute paths. Resolving it relative to the CWD puts it outside every
        # approved root, so the read was refused, and because a refusal is never
        # recorded as evidence the model simply tried again: one session spent 226
        # of 235 reader calls re-reading three files it could never open this way.
        # Resolve the basename inside the approved roots instead. Only an
        # UNAMBIGUOUS match is accepted — several files sharing a name give no
        # basis to pick one, so that still refuses rather than guessing.
        resolved_path = self._resolve_within_roots(path)
        if resolved_path is not None:
            kwargs[path_kwarg] = resolved_path
            logger.info(
                "Resolved bare filename %r to %s inside an approved scan root",
                path,
                resolved_path,
            )
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

        # ``--input`` establishes the session's explicit data boundary. A model
        # asking to scan ``.`` or another unrelated directory must not turn that
        # bounded run into a broader filesystem grant. Refuse without prompting;
        # the user already supplied the authoritative input root at startup.
        input_root = self.state.metadata.input_path
        if input_root:
            logger.warning(
                "Refusing scan of %s: session is bounded to --input %s",
                path,
                input_root,
            )
            self.state.log_reasoning(
                "refuse_scan_root",
                "scan_files",
                f"Refused: {path} is outside the --input boundary {input_root}.",
            )
            return _scan_refusal(
                path,
                f"Refused: this session is restricted to the --input path {input_root}. "
                "Use the existing scanned-file inventory instead of scanning another folder.",
            )

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
            logger.debug(
                "on_tool_event(%s, %s, %s) raised",
                tool_name,
                phase,
                kwargs_str,
                exc_info=True,
            )

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
        # Emit a live profiler marker before entering slow tools. The completed
        # ``tool_call`` record is intentionally written after return, but without
        # this start event profile.ndjson looks idle during long validation,
        # network resolution, or HITL waits.
        if self.profiler is not None:
            self.profiler.log_event(
                event="tool_start",
                tool=tool_name,
                iteration=self.state.iteration_count,
                args=kwargs_str or None,
            )
        try:
            return self._run_tool_impl(tool_name, **kwargs)
        except Exception as exc:
            # A raising tool wrote no `tool_call` record, so the profile showed a
            # start with no completion and nothing about what went wrong. The
            # failure is the interesting event — it is what the model reacts to,
            # and what a session analysis needs to attribute time to.
            if self.profiler is not None:
                try:
                    self.profiler.log_event(
                        event="tool_failed",
                        tool=tool_name,
                        iteration=self.state.iteration_count,
                        args=kwargs_str or None,
                        error=f"{type(exc).__name__}: {exc}"[:300],
                    )
                except Exception:  # noqa: BLE001 — logging never masks the error
                    logger.debug("failed-call logging failed", exc_info=True)
            raise
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

        # Compact by DEFAULT, not on request. On the session's own workbook
        # `compact` strips the repeated header row, the authoring-instructions
        # Comments column and the empty cells for a 75% reduction (34,137 ->
        # 8,675 chars; 12,016 -> 3,710 tokens) with the same values in it. Left
        # opt-in, the saving depends on the model remembering a flag, and the one
        # that forgets is exactly the one that can least afford the context. An
        # explicit compact=False still wins — the raw sheet is one argument away.
        #
        # Applied HERE, before the dispatch splits: `read_excel` resolves through
        # the generic registry, not `scanner_tools`, so setting this inside the
        # scanner branch (where it first went) meant it never fired for the very
        # tool it names. The evidence store learned the same lesson below.
        if tool_name == "read_excel":
            kwargs.setdefault("compact", True)

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
            # Persist the answer: it is otherwise only a tool result inside the
            # graph checkpoint, which a rotated thread discards (#user_answers).
            if isinstance(result, dict):
                spoken = result.get("comments") or result.get("action") or ""
                self.state.record_user_answer(kwargs.get("context", ""), str(spoken))
        elif tool_name == "request_input":
            if self.profiler is not None:
                self.profiler.log_event(event="hitl_wait", tool=tool_name)
            result = self.human_interface.request_input(
                kwargs.get("prompt", ""), kwargs.get("field_type", "text")
            )
            if isinstance(result, dict) and not result.get("skipped"):
                self.state.record_user_answer(
                    kwargs.get("prompt", ""), str(result.get("value") or "")
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

        # Record successful reader output as bounded session evidence. This sits
        # AFTER every dispatch branch on purpose: `read_file`/`read_excel`/
        # `read_docx` resolve through the generic registry below, not through
        # `scanner_tools`, so hooking this inside the scanner branch (where it
        # used to live) meant it only ever fired for `read_file_sample` and the
        # evidence store stayed permanently empty — taking the "already loaded
        # this document" de-duplication down with it.
        # Squeeze formatting-only bulk out of reader output. Same placement
        # lesson as the evidence store directly below: this must sit AFTER every
        # dispatch branch, because the readers it targets do not go through
        # `scanner_tools`. Hooked inside that branch it silently did nothing —
        # a session stored its 32,484-char study record whole when 15,335 chars
        # of identical JSON would have done.
        if tool_name in _DOCUMENT_EVIDENCE_TOOLS:
            result = _compact_reader_text(tool_name, kwargs.get("path"), result)
            self._store_document_evidence(
                tool_name, str(kwargs.get("path", "")), result, kwargs
            )

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
        self._writeback_validation(
            tool_name,
            result,
            severity=kwargs.get("severity"),
            profile=kwargs.get("profile"),
        )

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
            # The SAME rendering `tool_start` uses. They rendered differently —
            # one compacted, one a raw dict repr — so a start and its completion
            # could not be paired, and a call that RAISED (which writes no
            # `tool_call` at all) could only be counted, never identified. "16
            # set_fields calls failed, cause unknown" is not a diagnosis.
            _args_str: str | None = None
            try:
                _args_str = _compact_tool_kwargs(tool_name, kwargs)
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

    def _writeback_validation(
        self,
        tool_name: str,
        result: Any,
        *,
        severity: str | None = None,
        profile: str | None = None,
    ) -> None:
        """Fold a validation result into ``state.validation`` (#153).

        ``validate`` returns a fully-formed :class:`ValidationReport` (disk,
        three-pass) — adopt it wholesale. ``build_and_validate`` returns the
        in-memory routable dict (``{"ok", "conformance", "issues"}``); map its
        per-layer ``conformance`` onto the report and record the REQUIRED issues
        ordered base -> isa -> tox. Layers absent from ``conformance`` (a scoped
        ``profile=`` call) keep their prior value, and an errored result is left
        untouched so a transient failure never wipes known issues.
        """
        from builder.tools.validation import apply_validation_result

        apply_validation_result(self.state, tool_name, result, severity=severity)

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
