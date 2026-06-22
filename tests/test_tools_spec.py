"""Tests for builder/agents/tools_spec.py — TOOL_SPECS definitions.

These tests ensure tool descriptions are self-consistent and don't reference
tools the LLM cannot call (Issue #70).
"""

from __future__ import annotations

import re

from builder.agents.tools_spec import TOOL_SPECS


def _tool_names() -> set[str]:
    """Return the set of all tool names registered in TOOL_SPECS."""
    return {spec["name"] for spec in TOOL_SPECS}  # ty: ignore


# Tool functions that are exported by builder.tools but deliberately omitted
# from TOOL_SPECS (they are internal helpers, not LLM-facing tools).
# Computed from builder/tools/__init__.py __all__ minus TOOL_SPECS names.
_INTERNAL_TOOL_NAMES: set[str] | None = None


def _get_registry_tool_names() -> set[str]:
    """Return tool names registered in the shared TOOL_REGISTRY.

    Imports all tool modules (triggering registration calls) and returns
    the full set of registered tool names intended for LLM use.
    """
    import builder.tools.builder
    import builder.tools.drafters
    import builder.tools.fair_assessment
    import builder.tools.management
    import builder.tools.mit_assessment
    import builder.tools.scanner
    import builder.tools.session
    import builder.tools.validation
    import builder.tools.verification 
    from builder.tools.registry import TOOL_REGISTRY

    return set(TOOL_REGISTRY.list())


def _get_engine_routable_llm_tools() -> set[str]:
    """Return tool names that the engine routes specially (not via registry)
    and that are intended to be callable by the LLM.

    The engine has special routing for HITL tools and scanner tools.
    Scanner tools like scan_files, read_file_sample, read_multiple_files,
    unzip_file, and preview_archive are engine-routed and should be in
    TOOL_SPECS (some already are). HITL tools present_to_human and
    request_input must also be in TOOL_SPECS for the LLM to call them.
    """
    return {
        "present_to_human",
        "request_input",
        "scan_files",
        "read_file_sample",
        "read_multiple_files",
        "unzip_file",
        "preview_archive",
    }


def _get_tool_names_from_system_prompt() -> set[str]:
    """Extract tool names mentioned in the SYSTEM_PROMPT text.

    Looks for backtick-quoted identifiers and dash-list items.
    """
    from builder.agents.system_prompt import SYSTEM_PROMPT

    names: set[str] = set()
    # Find backtick-quoted identifiers like `build_crate`
    for m in re.finditer(r"`([a-z_]+)`", SYSTEM_PROMPT):
        names.add(m.group(1))
    return names


def _get_internal_tool_names() -> set[str]:
    """Return tool-function names exported by builder.tools but NOT in TOOL_SPECS."""
    global _INTERNAL_TOOL_NAMES
    if _INTERNAL_TOOL_NAMES is not None:
        return _INTERNAL_TOOL_NAMES

    import builder.tools as bt

    spec_names = _tool_names()
    _INTERNAL_TOOL_NAMES = set(bt.__all__) - spec_names
    return _INTERNAL_TOOL_NAMES


# Patterns that indicate a reference to another tool by name.
# We look for quoted or backtick-wrapped references like "read_file_sample"
# or ``read_file_sample`` in description strings.
_REF_PATTERN = re.compile(r"`([a-z_]+)`|`{2}([a-z_]+)`{2}")


def _collect_referenced_tools(desc: str) -> set[str]:
    """Return set of tool-name-like identifiers found in *desc*.

    Looks for:
    - Backtick-quoted identifiers (the most explicit form).
    - Bare mentions of tool names that exist in the builder.tools module
      but NOT in TOOL_SPECS. This catches the case where a description
      says "use read_file_sample" or "instead of calling read_file_sample"
      without backticks.
    """
    refs: set[str] = set()

    # Backtick-quoted identifiers (single or double backticks)
    for match in _REF_PATTERN.finditer(desc):
        ref = match.group(1) or match.group(2)
        if ref:
            refs.add(ref)

    # Bare mentions of known internal (non-TOOL_SPECS) tool names
    for internal_name in _get_internal_tool_names():
        if internal_name in desc:
            refs.add(internal_name)

    return refs


def test_no_tool_description_references_absent_tool():
    """Every tool referenced by name in descriptions must exist in TOOL_SPECS.

    The 'read_multiple_files' description previously referenced 'read_file_sample'
    in its description, but 'read_file_sample' was NOT in TOOL_SPECS, causing
    the LLM to be told about a tool it cannot call.

    We detect both backtick-quoted identifiers and bare mentions of tool
    functions that exist in builder.tools but are not registered in TOOL_SPECS.
    """
    available = _tool_names()

    for spec in TOOL_SPECS:
        name = spec["name"]
        desc = spec.get("description", "")

        refs = _collect_referenced_tools(desc)

        for ref in refs:
            # It's ok for a tool to reference itself
            if ref == name:
                continue
            if ref not in available:
                pytest.fail(
                    f"Tool '{name}' description references '{ref}' which is "
                    f"not a registered tool in TOOL_SPECS. Either add '{ref}' "
                    f"to TOOL_SPECS or reword the description."
                )


def _import_module() -> object:
    """Helper to import and return the tools_spec module for introspection."""
    import importlib

    return importlib.import_module("builder.agents.tools_spec")


# We need pytest for the fail helper
import pytest  # noqa: E402


def test_every_registry_tool_is_in_tool_specs():
    """Every tool registered in TOOL_REGISTRY must appear in TOOL_SPECS.

    The registry is the authoritative list of LLM-callable tools. If a tool
    is registered but missing from TOOL_SPECS, the LLM cannot call it.
    """
    spec_names = _tool_names()
    registry_names = _get_registry_tool_names()

    missing = registry_names - spec_names
    assert not missing, (
        f"Tools registered in TOOL_REGISTRY but missing from TOOL_SPECS: "
        f"{sorted(missing)}"
    )


def test_every_engine_routable_llm_tool_is_in_tool_specs():
    """Every engine-routable tool intended for LLM use must appear in TOOL_SPECS.

    The engine has special routing for HITL tools (present_to_human,
    request_input) and scanner tools (scan_files, read_file_sample, etc.).
    These tools are callable by the LLM but not in the registry. They still
    need TOOL_SPECS entries so the LLM knows their schemas.
    """
    spec_names = _tool_names()
    routable = _get_engine_routable_llm_tools()

    missing = routable - spec_names
    assert not missing, (
        f"Engine-routable LLM tools missing from TOOL_SPECS: "
        f"{sorted(missing)}"
    )


def test_every_system_prompt_tool_is_in_tool_specs():
    """Every tool named in SYSTEM_PROMPT must appear in TOOL_SPECS.

    If the system prompt tells the LLM about a tool that doesn't have a
    schema in TOOL_SPECS, the LLM will be confused or unable to call it.
    """
    spec_names = _tool_names()
    prompt_names = _get_tool_names_from_system_prompt()

    missing = prompt_names - spec_names
    assert not missing, (
        f"Tools mentioned in SYSTEM_PROMPT but missing from TOOL_SPECS: "
        f"{sorted(missing)}"
    )


def test_no_duplicate_tool_names_in_tool_specs():
    """TOOL_SPECS must not contain two entries with the same name.

    A duplicate (e.g. an upgraded tool spec added without removing the old one)
    is silently shadowed when specs are collapsed into a name->tool map, so the
    LLM sees one schema while another spec's description is dead. The build then
    disagrees with itself (see the read_multiple_files duplicate from the
    file-readers change).
    """
    from collections import Counter

    counts = Counter(spec["name"] for spec in TOOL_SPECS)  # ty: ignore
    duplicates = {name: n for name, n in counts.items() if n > 1}
    assert not duplicates, f"Duplicate tool names in TOOL_SPECS: {duplicates}"


__all__: list[str] = []
