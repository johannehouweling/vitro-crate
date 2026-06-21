"""Tests for builder/agents/tools_spec.py — TOOL_SPECS definitions.

These tests ensure tool descriptions are self-consistent and don't reference
tools the LLM cannot call (Issue #70).
"""

from __future__ import annotations

import re

from builder.agents.tools_spec import TOOL_SPECS


def _tool_names() -> set[str]:
    """Return the set of all tool names registered in TOOL_SPECS."""
    return {spec["name"] for spec in TOOL_SPECS}


# Tool functions that are exported by builder.tools but deliberately omitted
# from TOOL_SPECS (they are internal helpers, not LLM-facing tools).
# Computed from builder/tools/__init__.py __all__ minus TOOL_SPECS names.
_INTERNAL_TOOL_NAMES: set[str] | None = None


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

__all__: list[str] = []
