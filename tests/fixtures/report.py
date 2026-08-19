"""Readers for the rendered maturity report, shared by the test modules that
assert on its verdict."""

from __future__ import annotations

import re


def profile_verdict(page: str) -> str:
    """The Required-tier state the Profile adherence KPI tile shows: ``"ok"``
    (every required profile layer passes), ``"no"`` (one or more fail) or
    ``"na"`` (not validated, or validated against an older crate). This is the
    report's headline verdict now that the header carries no pill."""
    m = re.search(
        r'<span class="eyebrow">Profile adherence</span><span class="mk (ok|no|na)"', page
    )
    assert m, "no Profile adherence tile rendered"
    return m.group(1)
