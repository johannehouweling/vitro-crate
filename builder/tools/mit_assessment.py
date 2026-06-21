"""Tool that assesses MIT coverage by comparing entity field completion against
mit/invitro_tox.yaml.

For each module in the MIT YAML, maps crate_slot patterns to entity fields
and computes completion scores.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from builder.state import CrateState, MITReport

logger = logging.getLogger(__name__)

# Path to the MIT YAML file
MIT_YAML_PATH = Path(__file__).resolve().parent.parent.parent / "mit" / "invitro_tox.yaml"


def _load_mit_yaml() -> dict[str, Any] | None:
    """Load and parse the MIT YAML file.

    Returns:
        Parsed YAML content as a dict, or None if loading fails.
    """
    try:
        import yaml

        with open(MIT_YAML_PATH) as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning("Failed to load MIT YAML from %s: %s", MIT_YAML_PATH, e)
        return None


def _parse_crate_slots(slot_str: str) -> list[tuple[str, str]]:
    """Parse a crate_slot string into a list of (EntityType, field) tuples.

    Crate slots are formatted like "Investigation:name;Study:name;Assay:name"
    or "MolecularEntity:formula;MolecularEntity:smiles".

    Args:
        slot_str: The crate_slot string from the MIT YAML.

    Returns:
        A list of (entity_type, field_name) tuples.
    """
    slots: list[tuple[str, str]] = []
    parts = [p.strip() for p in slot_str.split(";") if p.strip()]
    for part in parts:
        if ":" in part:
            entity_type, field = part.split(":", 1)
            slots.append((entity_type.strip(), field.strip()))
    return slots


def _count_filled_fields(state: CrateState) -> dict[tuple[str, str], bool]:
    """Count filled/verified fields across all entities in state.

    Returns a dict keyed by (entity_type, field_name) -> True if filled/verified.
    """
    filled: dict[tuple[str, str], bool] = {}
    for entity in state.list_entities():
        for field_name in entity.fields:
            fc = entity.get_field_status(field_name)
            if fc is not None and fc.status in ("filled", "verified"):
                filled[(entity.type, field_name)] = True
            else:
                # Field exists but status is missing or unset
                pass
    return filled


def assess_mit_coverage(state: CrateState) -> MITReport:
    """Assess MIT coverage by comparing entity field completion against MIT YAML.

    For each module in the MIT YAML, maps crate_slot patterns to entity fields
    and computes completion scores per module.

    Args:
        state: The current CrateState to assess.

    Returns:
        An MITReport with per-module scores and overall score.
    """
    mit_data = _load_mit_yaml()

    if mit_data is None:
        return MITReport(module_scores={}, overall_score=0.0)

    filled_fields = _count_filled_fields(state)
    modules = mit_data.get("modules", [])

    module_scores: dict[str, dict[str, int]] = {}
    total_completed = 0
    total_required = 0

    for module in modules:
        module_name = module.get("name", module.get("id", "unknown"))
        sections = module.get("sections", [])
        module_completed = 0
        module_total = 0

        # A module may have parameters directly or within sections
        all_params: list[dict[str, Any]] = []
        for section in sections:
            all_params.extend(section.get("parameters", []))
        # Also include top-level parameters (sections with id=None name=None)
        all_params.extend(module.get("parameters", []))

        # Deduplicate by parameter id
        seen_param_ids: set[str] = set()
        unique_params: list[dict[str, Any]] = []
        for param in all_params:
            pid = param.get("id", "")
            if pid and pid not in seen_param_ids:
                seen_param_ids.add(pid)
                unique_params.append(param)

        for param in unique_params:
            crate_slot = param.get("crate_slot", "")
            if not crate_slot:
                continue

            slots = _parse_crate_slots(crate_slot)
            module_total += 1

            # Check if any of the slots has a filled field
            is_filled = False
            for entity_type, field_name in slots:
                if (entity_type, field_name) in filled_fields:
                    is_filled = True
                    break

            if is_filled:
                module_completed += 1

        if module_total > 0:
            module_scores[module_name] = {
                "completed": module_completed,
                "total": module_total,
            }
            total_completed += module_completed
            total_required += module_total

    overall_score = total_completed / total_required if total_required > 0 else 0.0

    return MITReport(
        module_scores=module_scores,
        overall_score=overall_score,
    )


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("assess_mit_coverage", assess_mit_coverage, takes_state=True)
