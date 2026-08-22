"""Session persistence and HITL interaction tools.

Provides functions to save, load, and manage CrateState sessions, as well
as interaction primitives for Human-in-the-Loop workflows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile

import builder.config as _config
from builder.state import CrateState, conformance_by_layer

logger = logging.getLogger(__name__)

SESSION_DIR = _config.session_root()

# ---------------------------------------------------------------------------
# Change-detection: cached content hash of the last saved state so we can
# skip writes when nothing changed.
# ---------------------------------------------------------------------------
_last_saved_state_hash: str | None = None


def _state_content_hash(state: CrateState) -> str:
    """Return a hash of the state's meaningful content, ignoring metadata timestamps.

    This hash is used for change-detection: two saves with the same entities,
    scanned files, approved roots, etc. (but different ``updated_at`` values)
    will produce the same hash.
    """
    # Build a dict with the fields that represent actual content changes
    content = {
        "entities": state.entities.to_dict()
        if hasattr(state.entities, "to_dict")
        else str(state.entities),
        "scanned_files": [
            f.to_dict() if hasattr(f, "to_dict") else str(f) for f in state.scanned_files
        ],
        "approved_scan_roots": sorted(state.approved_scan_roots),
        "validation": state.validation.to_dict()
        if hasattr(state.validation, "to_dict")
        else str(state.validation),
        "mit_assessment": state.mit_assessment.to_dict()
        if hasattr(state.mit_assessment, "to_dict")
        else str(state.mit_assessment),
        "fair_assessment": state.fair_assessment.to_dict()
        if hasattr(state.fair_assessment, "to_dict")
        else str(state.fair_assessment),
        "checkpoint": state.checkpoint.to_dict()
        if hasattr(state.checkpoint, "to_dict")
        else str(state.checkpoint),
    }
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def load_session(session_id: str) -> CrateState | None:
    """Load a CrateState from sessions/<session_id>/crate_state.json.

    Returns None if session doesn't exist or is corrupt.
    """
    session_path = SESSION_DIR / session_id
    state_path = session_path / "crate_state.json"
    if not state_path.is_file():
        return None
    try:
        with open(state_path) as f:
            return CrateState.from_json(f.read())
    except (json.JSONDecodeError, KeyError, FileNotFoundError):
        logger.warning("Failed to load session %s", session_id)
        return None


def list_sessions() -> list[dict]:
    """List available sessions with metadata.

    Returns [{"session_id": str, "created_at": str, "updated_at": str, "entity_count": int}]
    """
    if not SESSION_DIR.is_dir():
        return []
    entries: list[dict] = []
    for child in sorted(SESSION_DIR.iterdir()):
        if child.is_dir():
            state_path = child / "crate_state.json"
            if state_path.is_file():
                try:
                    with open(state_path) as f:
                        data = json.load(f)
                    created_at = data.get("created_at", "")
                    updated_at = data.get("updated_at", "")
                    entity_count = _count_entities(data)
                    entries.append(
                        {
                            "session_id": child.name,
                            "created_at": created_at,
                            "updated_at": updated_at,
                            "entity_count": entity_count,
                        }
                    )
                except (json.JSONDecodeError, KeyError, FileNotFoundError):
                    continue
    return entries


def _count_entities(data: dict) -> int:
    """Count total entities across all entity collections."""
    entities = data.get("entities", {})
    total = 0
    for coll in entities.values():
        if isinstance(coll, list):
            total += len(coll)
    return total


def get_status(state: CrateState) -> dict:
    """Return current session status for live UIs.

    Returns:
    - session_id, phase (based on checkpoint completions),
      entity_counts (per type), total_entities,
      mit_score, validation_status (pass/fail per layer),
      iteration_count, stuck, last_action
    """
    # Count entities per type
    entity_counts: dict[str, int] = {}
    for entity in state.list_entities():
        t = entity.type
        entity_counts[t] = entity_counts.get(t, 0) + 1

    total_entities = sum(entity_counts.values())

    # Determine phase from checkpoints
    phase = _determine_phase(state, total_entities)

    # Last action from reasoning log
    last_action = ""
    if state.checkpoint.reasoning_log:
        last_action = state.checkpoint.reasoning_log[-1].action

    return {
        "session_id": state.session_id,
        "phase": phase,
        "entity_counts": entity_counts,
        "total_entities": total_entities,
        "mit_score": state.mit_assessment.overall_score,
        # Cumulative: a layer cannot conform where the layer it extends does not.
        "validation_status": conformance_by_layer(
            base=state.validation.base_passed,
            isa=state.validation.isa_passed,
            tox=state.validation.tox_passed,
        ),
        "iteration_count": state.iteration_count,
        "stuck": state.stuck,
        "last_action": last_action,
    }


def _determine_phase(state: CrateState, total_entities: int) -> str:
    """Determine the current build phase based on state."""
    completed = state.checkpoint.completed_checkpoints
    if "crate_built" in completed:
        return "complete"
    if "files_scanned" in completed:
        return "drafting"
    if total_entities > 0:
        return "drafting"
    if state.scanned_files:
        return "scanning"
    return "initial"


def get_hint(state: CrateState) -> str:
    """Return a contextual hint about what the agent should do next.

    Examines state for:
    - No entities → "Start by drafting an Investigation"
    - Entities with missing required fields → "Fill in required fields: ..."
    - Validation failures → "Fix REQUIRED validation issues: ..."
    """
    total_entities = len(state.list_entities())

    if total_entities == 0:
        return "Start by drafting an Investigation"

    # Check for validation failures
    if (
        not state.validation.base_passed
        or not state.validation.isa_passed
        or not state.validation.tox_passed
    ):
        if state.validation.required_issues:
            issues = "; ".join(state.validation.required_issues[:3])
            return f"Fix REQUIRED validation issues: {issues}"

    # Check for missing fields
    missing_fields: list[str] = []
    for entity in state.list_entities():
        for key, fc in entity._completion.items():
            if fc.status == "missing":
                field_name = key.split(":", 1)[-1] if ":" in key else key
                missing_fields.append(f"{entity.entity_id}.{field_name}")
    if missing_fields:
        fields_str = ", ".join(missing_fields[:5])
        return f"Fill in required fields: {fields_str}"

    return "Continue building the RO-Crate"


def save_session(state: CrateState, label: str = "", always_write: bool = False) -> dict:
    """Save CrateState to sessions/<session_id>/ directory.

    Uses an atomic write pattern: write to a temporary file in the same
    directory, call os.fsync() to flush data to disk, then os.replace()
    to atomically rename the temp file over the target.  This ensures
    ``crate_state.json`` is never left in a partially-written state.

    When the state content (entities, scanned files, approved roots,
    validation/assessment/checkpoint data) is identical to the last saved
    version the write is skipped (unless *always_write* is ``True``).
    Metadata timestamps (``updated_at``, ``created_at``) are NOT considered
    part of the "content" for change detection.

    Creates directory structure::

        sessions/<session_id>/
        ├── crate_state.json    # Serialized CrateState
        └── session.log         # Agent reasoning trace

    Returns ``{"success": bool, "session_id": str, "path": str,
    "error": str | None, "skipped": bool}``
    """
    global _last_saved_state_hash

    # Assign session_id if not already set
    if not state.session_id:
        state.session_id = _config.now().strftime("%Y%m%d_%H%M%S")

    # Change detection: hash the meaningful content (not timestamps)
    content_hash = _state_content_hash(state)

    if not always_write and _last_saved_state_hash == content_hash:
        return {
            "success": True,
            "session_id": state.session_id,
            "path": str(SESSION_DIR / state.session_id),
            "error": None,
            "skipped": True,
        }

    # Update timestamps for the write
    if not state.created_at:
        state.created_at = _config.now().isoformat()
    state.updated_at = _config.now().isoformat()

    session_id = state.session_id
    session_path = SESSION_DIR / session_id
    session_path.mkdir(parents=True, exist_ok=True)

    # Full JSON for the actual write (includes updated_at)
    current_json = state.to_json()

    # Atomic write: temp file → fsync → os.replace
    state_path = session_path / "crate_state.json"
    try:
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(session_path),
            prefix=".crate_state_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w") as tmp_file:
                tmp_file.write(current_json)
                tmp_file.flush()
                os.fsync(fd)
        except Exception:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise

        os.replace(tmp_path_str, str(state_path))
    except OSError as exc:
        logger.error("Failed to save session %s: %s", session_id, exc)
        return {
            "success": False,
            "session_id": session_id,
            "path": str(session_path),
            "error": str(exc),
            "skipped": False,
        }

    # Remember the content hash for change detection
    _last_saved_state_hash = content_hash

    # Write session.log (best-effort)
    log_path = session_path / "session.log"
    try:
        with open(log_path, "a") as f:
            timestamp = _config.now().isoformat()
            action = f"save_session: saved state for {session_id}"
            if label:
                action += f" (label: {label})"
            f.write(f"[{timestamp}] {action}\n")
    except OSError:
        pass

    return {
        "success": True,
        "session_id": session_id,
        "path": str(session_path),
        "error": None,
        "skipped": False,
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("get_status", get_status, takes_state=True)
TOOL_REGISTRY.register("get_hint", get_hint, takes_state=True)
TOOL_REGISTRY.register("save_session", save_session, takes_state=True)
TOOL_REGISTRY.register("list_sessions", list_sessions, takes_state=False)
TOOL_REGISTRY.register("load_session", load_session, takes_state=False)
