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


def assemble_crate(
    state: CrateState,
    output_dir: Path | None = None,
    *,
    materialize_payload: bool = True,
) -> ROCrate:
    """Assemble an in-memory `ROCrate` from CrateState — no disk write.

    This is the shared assembly step behind both :func:`export_crate` (which
    then writes the crate to disk) and ``build_and_validate`` (which generates
    the metadata document and validates it in memory). Splitting it out keeps
    disk writes confined to :func:`export_crate`.

    Args:
        state: The CrateState to build from.
        output_dir: Crate root, used only when payload is materialised. ``None``
            for the pure in-memory path.
        materialize_payload: When False, no payload file is written (see
            :func:`builder.tools._crate_mapping.populate_crate`).

    Returns:
        A populated :class:`ROCrate`. Nothing is written unless the caller
        invokes ``crate.write()``.
    """
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, output_dir, materialize_payload=materialize_payload)
    return crate


def export_crate(state: CrateState, output_path: str | None = None) -> dict[str, Any]:
    """Assemble an RO-Crate from CrateState and write it to disk (ro-crate-py).

    This is the only step that touches disk: it creates the output directory,
    assembles a `ROCrate` from the state, and writes `ro-crate-metadata.json`
    plus any payload. For a fast, zero-disk conformance check during the agent
    loop, use ``build_and_validate`` instead.

    Args:
        state: The current CrateState to build from.
        output_path: Path where the crate directory should be created.
            When omitted, falls back to ``state.metadata.output_path`` (the
            user-configured destination), then to
            ``sessions/<session_id>/working_crate/``.

    Returns:
        A dict with keys:
            success (bool): Whether the crate was built successfully.
            crate_path (str): The output path used (can be passed directly
                to :func:`validate`).
            error (str | None): Error message if success is False.
    """
    try:
        if not output_path:
            # Honor the user-configured destination (set from the CLI --output /
            # state.metadata.output_path) before falling back to the session dir.
            output_path = state.metadata.output_path or _default_crate_path(state)

        output_dir = Path(output_path)
        output_dir.mkdir(parents=True, exist_ok=True)

        crate = assemble_crate(state, output_dir, materialize_payload=True)
        crate.write(str(output_dir))

        logger.info("Crate exported to %s", output_path)
        return {"success": True, "crate_path": output_path, "error": None}

    except OSError as e:
        logger.error("Failed to create crate at %s: %s", output_path, e)
        return {"success": False, "crate_path": output_path, "error": str(e)}
    except Exception as e:
        logger.error("Unexpected error building crate: %s", e)
        return {"success": False, "crate_path": output_path, "error": str(e)}


def build_crate(state: CrateState, output_path: str | None = None) -> dict[str, Any]:
    """Back-compat alias for :func:`export_crate` (the on-disk writer)."""
    return export_crate(state, output_path)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("export_crate", export_crate, takes_state=True)
TOOL_REGISTRY.register("build_crate", build_crate, takes_state=True)
