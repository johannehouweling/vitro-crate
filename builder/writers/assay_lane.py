"""The maturity report's assay-lane section (#686).

One assay, drawn as the chain it is: cell line, culture, cultured sample,
exposure, exposed sample, readout, raw files, analysis, processed files — nine
fixed columns, left to right, with the protocols a step executes and the
compounds a protocol lists hanging in a band underneath.

The lane began as a layout the entity explorer could switch to, and that was the
wrong home for it twice over. A lane has a fixed order and nine named columns,
so it wants a flat drawing a reader scans rather than a pan-and-zoom viewport
that has to be framed first; and combining it with any other view handed the
lane's geometry a graph it had no place for, which is how the section came to
draw nothing at all.

So it is a section of its own. What it is *not* is a second copy of the crate:
it reads the explorer's data island, which already carries the nodes, the edges,
the vocabulary, the palette and the crate document, and it draws the same legend
from the same registry. The report emits the explorer first for that reason.

Three files:

- :func:`~builder.writers.entity_explorer.build_assay_lanes` — which lanes exist
  and what each draws, minted from the crate's own assays.
- ``assay_lane_view.js`` — pure geometry: which column a node belongs to and
  where its box goes. No DOM, so the tests run the shipped module itself.
- ``assay_lane_app.js`` — the drawing and the reading of it: SVG, assay chips,
  the two folds, the inspector.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from builder.writers.entity_explorer import build_assay_lanes

_ASSET_DIR = Path(__file__).resolve().parent
_VIEW_PATH = _ASSET_DIR / "assay_lane_view.js"
_APP_PATH = _ASSET_DIR / "assay_lane_app.js"

LANE_SCRIPT_COUNT = 2
"""The geometry module and the app. Named so a test can state the count without
recounting the implementation, and so an accidental extra ``<script>`` is a
failure rather than a habit."""

_SVG_ID = "lane-svg"


@lru_cache(maxsize=1)
def _view_js() -> str:
    """Where each box goes: pure geometry, no DOM, no payload.

    Its own file and its own ``<script>`` so a test can run the code the page
    runs over a real crate's graph rather than over a second copy of it kept in
    the test.
    """
    return _VIEW_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _app_js() -> str:
    """The drawing, the chips, the folds and the inspector.

    Loaded after :func:`_view_js` — it calls that module at render time — and
    after the explorer's, because it reads the island the explorer writes.
    """
    return _APP_PATH.read_text(encoding="utf-8")


def render_assay_lane_section(metadata: dict[str, Any] | list[dict[str, Any]]) -> str:
    """The section, or the empty string for a crate with no assay to draw.

    A deposit has as many assays as it has. Four lanes, one lane, none: the chips
    are minted from the crate, and a crate whose assays state no steps leaves the
    section out altogether rather than rendering a heading over an empty box.

    The frame is markup rather than something the app builds, so every id the app
    reaches for is declared in one place and a reader with no script gets the
    ``<noscript>`` note rather than a blank.

    No lede: the drawing is captioned by its own column headings, and prose
    explaining a picture is the first thing a reader skips.

    Args:
        metadata: A parsed ``ro-crate-metadata.json``, the ``@graph`` list, or a
            ``crate.metadata.generate()`` document.

    Returns:
        One ``<section>``, or ``""``.
    """
    if not build_assay_lanes(metadata):
        return ""
    scripts = f"<script>{_view_js()}</script><script>{_app_js()}</script>"
    return (
        '<section class="lanes" id="assay-lanes">\n'
        '  <div class="sec-h"><h2>Assay lanes</h2></div>\n'
        '  <div class="lane-app" id="lane-app">\n'
        '    <div class="lane-bar">\n'
        '      <div class="ex-views" id="lane-chips" role="group" aria-label="Assay"></div>\n'
        "    </div>\n"
        '    <div class="lane-stage">\n'
        '      <div class="lane-left">\n'
        '        <div class="lane-viewer">\n'
        '          <div class="lane-overlay">\n'
        '            <button type="button" class="lane-sw" id="lane-band" aria-pressed="true">'
        '<span class="lane-tr"><span class="lane-kn"></span></span>Protocols</button>\n'
        '            <button type="button" class="lane-sw" id="lane-unfold" '
        'aria-pressed="false">'
        '<span class="lane-tr"><span class="lane-kn"></span></span>Unfold files</button>\n'
        '            <button type="button" class="ex-icon" id="lane-fit" aria-pressed="false" '
        'aria-label="Fit the whole chain" '
        'title="Scale the chain down until all nine columns fit; off, it is drawn '
        'at reading size and scrolls">'
        '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" fill="none" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        'stroke-linejoin="round"><path d="M6 2H2v4M10 2h4v4M10 14h4v-4M6 14H2v-4"/></svg>'
        "</button>\n"
        "          </div>\n"
        '          <div class="lane-canvas">\n'
        f'            <svg id="{_SVG_ID}" role="img"></svg>\n'
        '            <p class="lane-note ex-hint" id="lane-note" hidden>This assay records no '
        "step the ISA-Tox chain names, so there is no lane to draw. The entity "
        "explorer above still shows what the crate holds.</p>\n"
        "          </div>\n"
        "        </div>\n"
        '        <div class="ex-footer">\n'
        '          <div class="ex-legend" id="lane-legend"></div>\n'
        '          <span class="ex-count" id="lane-count"></span>\n'
        "        </div>\n"
        "      </div>\n"
        '      <aside class="ex-side ex-side-empty" id="lane-panel"></aside>\n'
        "    </div>\n"
        "  </div>\n"
        '  <p class="ex-print-note">The assay lanes are interactive: open this report '
        "in a browser to step through each assay, fold its files and read any "
        "entity&rsquo;s JSON-LD.</p>\n"
        '  <noscript><p class="ex-noscript">The assay lanes need JavaScript. The same '
        "steps and materials are in the crate&rsquo;s "
        "<code>ro-crate-metadata.json</code>.</p></noscript>\n"
        f"  {scripts}\n"
        "</section>\n"
    )
