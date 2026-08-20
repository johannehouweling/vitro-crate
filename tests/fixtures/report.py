"""Readers for the rendered maturity report, shared by the test modules that
assert on its verdict."""

from __future__ import annotations

import re


def profile_verdict(page: str) -> str:
    """The report's headline conformance state, read from the Profile
    conformance matrix's Required column: ``"ok"`` when every profile's
    required cell is a pass, ``"no"`` when any fails, ``"na"`` when none was
    assessed (not validated, or validated against an older crate)."""
    cells = re.findall(
        r'data-cell="(?:base|isa|tox)-required"[^>]*><span class="mk (ok|no|na)"', page
    )
    assert len(cells) == 3, f"expected the three required cells, found {len(cells)}"
    if "no" in cells:
        return "no"
    if all(c == "ok" for c in cells):
        return "ok"
    return "na"
