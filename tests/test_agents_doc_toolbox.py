"""Guard test for the toolbox design doc, ``docs/design/toolbox.md``.

Ensures the hand-written tool list in the design doc cannot drift away from the
real, dispatchable toolbox — i.e. it can never reintroduce a phantom tool like
a tool that never existed (Issue #145). Every tool name documented there
must be a real, LLM-callable tool: either specced in ``TOOL_SPECS`` or one of the
engine's special-cased / session-init tools.
"""

from __future__ import annotations

import re
from pathlib import Path

from builder.agents.react.tools_spec import TOOL_SPECS

_TOOLBOX_MD = Path(__file__).resolve().parent.parent / "docs" / "design" / "toolbox.md"

# Tool names that are real and callable but are intentionally NOT in TOOL_SPECS
# (engine-routed HITL/scanner helpers and the session-init scanner). These mirror
# the special-cased set asserted in tests/test_tools_spec.py.
_ENGINE_SPECIAL_CASED: set[str] = {
    "present_to_human",
    "request_input",
    "scan_files",
    "read_file_sample",
    "read_multiple_files",
    "unzip_file",
    "preview_archive",
}

# Matches a tool invocation at the start of a code-fence line, e.g.
#   draft_study(investigation_id: str, hints: dict) → Entity
#   /verify_all_identifiers() → [VerificationResult]
_TOOL_CALL_RE = re.compile(r"^/?([a-z_][a-z0-9_]*)\s*\(")


def _spec_names() -> set[str]:
    return {str(spec["name"]) for spec in TOOL_SPECS}


def _section_5_text() -> str:
    """Return the raw text of the toolbox doc."""
    return _TOOLBOX_MD.read_text(encoding="utf-8")


def _documented_tool_names() -> set[str]:
    """Tool names that appear as ``name(...)`` lines inside the doc's code fences."""
    names: set[str] = set()
    in_fence = False
    for line in _section_5_text().splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        match = _TOOL_CALL_RE.match(line.strip())
        if match:
            names.add(match.group(1))
    return names


def test_section_5_documents_only_real_tools() -> None:
    """No phantom tools: documented names ⊆ (TOOL_SPECS ∪ engine special-cased)."""
    documented = _documented_tool_names()
    callable_tools = _spec_names() | _ENGINE_SPECIAL_CASED
    phantom = documented - callable_tools
    assert not phantom, (
        f"docs/design/toolbox.md documents tool(s) that do not exist in TOOL_SPECS or the "
        f"engine special-cased set: {sorted(phantom)}"
    )


def test_section_5_documents_every_specced_tool() -> None:
    """No silent omissions: every specced tool is documented."""
    documented = _documented_tool_names()
    missing = _spec_names() - documented
    assert not missing, (
        f"docs/design/toolbox.md is missing dispatchable tool(s) that are in TOOL_SPECS: "
        f"{sorted(missing)}"
    )
