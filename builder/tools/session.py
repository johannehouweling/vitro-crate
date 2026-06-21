"""Session persistence and HITL interaction tools.

Provides functions to save, load, and manage CrateState sessions, as well
as interaction primitives for Human-in-the-Loop workflows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from builder.state import CrateState

logger = logging.getLogger(__name__)

SESSION_DIR = Path("sessions")


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
                    entries.append({
                        "session_id": child.name,
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "entity_count": entity_count,
                    })
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
        "validation_status": {
            "base": state.validation.base_passed,
            "isa": state.validation.isa_passed,
            "tox": state.validation.tox_passed,
        },
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
    if not state.validation.base_passed or not state.validation.isa_passed or not state.validation.tox_passed:
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


def save_session(state: CrateState, label: str = "") -> dict:
    """Save CrateState to sessions/<session_id>/ directory.

    Creates directory structure:
    sessions/<session_id>/
    ├── crate_state.json    # Serialized CrateState
    └── session.log         # Agent reasoning trace

    Returns {"success": bool, "session_id": str, "path": str, "error": str | None}
    """
    # Assign session_id if not already set
    if not state.session_id:
        state.session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if not state.created_at:
        state.created_at = datetime.now(timezone.utc).isoformat()
    state.updated_at = datetime.now(timezone.utc).isoformat()

    session_id = state.session_id
    session_path = SESSION_DIR / session_id
    session_path.mkdir(parents=True, exist_ok=True)

    # Write crate_state.json
    state_path = session_path / "crate_state.json"
    with open(state_path, "w") as f:
        f.write(state.to_json())

    # Write session.log
    log_path = session_path / "session.log"
    with open(log_path, "a") as f:
        timestamp = datetime.now(timezone.utc).isoformat()
        action = f"save_session: saved state for {session_id}"
        if label:
            action += f" (label: {label})"
        f.write(f"[{timestamp}] {action}\n")

    return {
        "success": True,
        "session_id": session_id,
        "path": str(session_path),
        "error": None,
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