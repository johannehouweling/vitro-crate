"""Tool that assembles an RO-Crate directory from CrateState entity data.

The crate is assembled with `ro-crate-py` (the official RO-Crate SDK): a
`ROCrate` is created, the ISA-Tox JSON-LD context is attached, and the entity
graph is built by `builder/tools/_crate_mapping.py` (which maps each CrateState
entity onto its ISA-Tox domain-model class, resolves cross-entity references, and
wires the Investigation → Study → Assay → LabProcess graph). `crate.write()` then
serialises a valid `ro-crate-metadata.json`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rocrate.rocrate import ROCrate

from builder.state import CrateState
from builder.tools._crate_mapping import populate_crate
from profiles.context import ISA_TOX_CONTEXT

logger = logging.getLogger(__name__)


def _default_crate_path(state: CrateState) -> str:
    """Return a session-derived default crate path.

    The convention is ``sessions/<session_id>/working_crate/``, which matches
    the session persistence layout described in AGENTS.md.
    """
    session_id = state.session_id or "unknown"
    return str(Path("sessions") / session_id / "working_crate")


def build_crate(state: CrateState, output_path: str | None = None) -> dict[str, Any]:
    """Build an RO-Crate from CrateState using ro-crate-py.

    Creates the output directory, assembles a `ROCrate` from the state, and
    writes `ro-crate-metadata.json` plus any payload.

    Args:
        state: The current CrateState to build from.
        output_path: Path where the crate directory should be created.
            When omitted, defaults to ``sessions/<session_id>/working_crate/``.

    Returns:
        A dict with keys:
            success (bool): Whether the crate was built successfully.
            crate_path (str): The output path used (can be passed directly
                to :func:`validate`).
            error (str | None): Error message if success is False.
    """
    try:
        if not output_path:
            output_path = _default_crate_path(state)

        output_dir = Path(output_path)
        output_dir.mkdir(parents=True, exist_ok=True)

        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(state, crate, output_dir)
        crate.write(str(output_dir))

        logger.info("Crate built at %s", output_path)
        return {"success": True, "crate_path": output_path, "error": None}

    except OSError as e:
        logger.error("Failed to create crate at %s: %s", output_path, e)
        return {"success": False, "crate_path": output_path, "error": str(e)}
    except Exception as e:
        logger.error("Unexpected error building crate: %s", e)
        return {"success": False, "crate_path": output_path, "error": str(e)}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("build_crate", build_crate, takes_state=True)
