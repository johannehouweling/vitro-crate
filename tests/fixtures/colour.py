"""Colour arithmetic shared by the palette tests.

The two palettes the report draws — the entity-category ring
(``provenance_dag.CATEGORY_STYLES``) and the MIT module colours
(``maturity_report.MIT_MODULE_STYLES``) — are pinned by the same measures, so
the measures live once, here, rather than one test module importing another.
"""

from __future__ import annotations

import math


def srgb_to_lab(colour: str) -> tuple[float, float, float]:
    """``#rrggbb`` to CIE L*a*b* (D65), so colours can be compared the way an eye
    compares them rather than by how far apart their hex digits are."""
    r, g, b = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede(a: str, b: str) -> float:
    """CIE76 colour difference. ~2.3 is the just-noticeable step; 20 is "clearly
    a different colour" when the two are side by side rather than adjacent."""
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(srgb_to_lab(a), srgb_to_lab(b))))


def contrast_on_white(colour: str) -> float:
    """WCAG contrast ratio against the report's page background."""
    r, g, b = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b))
    return 1.05 / (0.2126 * r + 0.7152 * g + 0.0722 * b + 0.05)
