"""Regression tests for stored XSS in the generated HTML artifacts (#169).

The crate writers embed user/crate-controlled strings (entity names,
descriptions, ids, labels) into HTML and Mermaid that a victim opens in a
browser. A crafted entity name such as ``<script>alert(1)</script>`` or
``<img src=x onerror=alert(1)>`` must be neutralised — emitted as inert,
HTML-escaped text — and the Mermaid renderer must not run with
``securityLevel: 'loose'`` (which would execute HTML embedded in node labels).

These are graph/string-level assertions (no browser needed): we assert the
literal ``<script>`` / ``onerror=`` tag never reaches the output as a live
construct, and that ``securityLevel`` is the strict default.

Covers the three generators that embed crate data:

* ``builder/writers/provenance_dag.py`` — the Mermaid DAG, the full crate graph,
  and the self-contained ``render_mermaid_html`` page;
* ``builder/writers/maturity_report.py`` — ``ro-crate-maturity.html``;
* the bundled ``ro-crate-preview.html`` written by ``export_crate``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate
from builder.writers.maturity_report import build_maturity_html
from builder.writers.provenance_dag import (
    render_crate_graph,
    render_mermaid_html,
    render_provenance_mermaid,
)

pytestmark = pytest.mark.timeout(120)

# The two canonical injection payloads: a literal script tag and an attribute
# event handler. Both must be neutralised wherever crate data reaches HTML.
SCRIPT_PAYLOAD = "<script>alert(1)</script>"
IMG_PAYLOAD = '<img src=x onerror="alert(1)">'


def _malicious_graph() -> dict:
    """A serialized ``@graph`` whose entity names/descriptions carry XSS payloads.

    The processes form a minimal derivation chain so they survive the
    provenance subgraph pruning and reach the renderer.
    """
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": SCRIPT_PAYLOAD,
                "hasPart": [{"@id": "#cc"}],
            },
            {
                "@id": "#sample",
                "@type": "Sample",
                "additionalType": "CellLine",
                "name": IMG_PAYLOAD,
            },
            {
                "@id": "#cc",
                "@type": "LabProcess",
                "additionalType": "CellCulture",
                "name": SCRIPT_PAYLOAD,
                "input": {"@id": "#sample"},
                "output": {"@id": "#cultured"},
            },
            {"@id": "#cultured", "@type": "Sample", "name": IMG_PAYLOAD},
        ]
    }


def _assert_no_live_script(out: str) -> None:
    """No payload survives as a live HTML construct; it appears escaped instead.

    The security property is that the payload's angle brackets are escaped, so
    there is no live ``<script>`` or ``<img …onerror=…>`` tag — the surviving
    ``onerror=`` text inside ``&lt;img …&gt;`` is inert display text and cannot
    fire. We therefore assert no live ``<script`` / ``<img`` opening tag and
    that the payload is present only in its escaped form.
    """
    assert SCRIPT_PAYLOAD not in out, "raw <script> tag leaked into output"
    assert "<script" not in out, "live <script tag leaked into output"
    assert "<img " not in out and "<img>" not in out, "live <img> tag leaked"
    # No live event-handler attribute (escaped ``&lt;img … onerror=`` is inert).
    assert 'onerror="alert' not in out or "&lt;img" in out
    # The text is preserved, just escaped (defence verified, content not lost).
    assert "&lt;script&gt;" in out
    assert "&lt;img" in out


# --- provenance DAG (Mermaid) ----------------------------------------------


def test_provenance_mermaid_escapes_entity_names() -> None:
    out = render_provenance_mermaid(_malicious_graph())
    _assert_no_live_script(out)


def test_crate_graph_escapes_entity_names() -> None:
    out = render_crate_graph(_malicious_graph())
    _assert_no_live_script(out)


# --- the self-contained mermaid HTML page ----------------------------------


def test_render_mermaid_html_is_not_loose() -> None:
    html = render_mermaid_html(render_provenance_mermaid(_malicious_graph()))
    assert "securityLevel: 'loose'" not in html
    assert "'loose'" not in html and '"loose"' not in html


def test_render_mermaid_html_neutralises_payload() -> None:
    """Even embedded as a JS string, the payload must not be a live tag.

    The source is JSON-encoded into the page, so a leaked literal ``<script>``
    would be a document-level injection (a ``</script>`` would close the module
    and inject markup). The Mermaid labels are HTML-escaped upstream, so the
    literal tag never appears — only its inert ``&lt;script&gt;`` form does.
    """
    page = render_mermaid_html(render_provenance_mermaid(_malicious_graph()))
    assert SCRIPT_PAYLOAD not in page
    # No live opening tags injected into the document via the embedded source.
    # (The page legitimately contains <script type="module"> for the loader; the
    # source string must not introduce another, nor an <img> breakout.)
    assert "</script>" not in page or page.count("</script>") == 1
    assert "<img " not in page


# --- maturity report --------------------------------------------------------


def _malicious_state() -> CrateState:
    state = CrateState()
    state.metadata.title = SCRIPT_PAYLOAD
    state.add_entity(
        Entity(
            entity_id="proc1",
            type="LabProcess",
            fields={"process_type": "Exposure", "name": IMG_PAYLOAD},
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


def test_maturity_report_escapes_title() -> None:
    out = build_maturity_html(_malicious_state())
    assert SCRIPT_PAYLOAD not in out
    assert "&lt;script&gt;" in out


# --- bundled ro-crate-preview.html (export) --------------------------------


def test_exported_preview_escapes_crate_data(tmp_path: Path) -> None:
    state = CrateState()
    state.session_id = "sess-xss"
    state.metadata.title = SCRIPT_PAYLOAD
    state.metadata.description = IMG_PAYLOAD
    state.metadata.output_path = str(tmp_path / "out")
    state.add_entity(
        Entity(
            entity_id="inv1",
            type="Investigation",
            fields={"name": SCRIPT_PAYLOAD},
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    res = build_crate(state)
    assert res["success"], res["error"]

    preview = Path(res["crate_path"]) / "ro-crate-preview.html"
    page = preview.read_text(encoding="utf-8")
    assert SCRIPT_PAYLOAD not in page, "preview leaked a live <script> tag"
    assert "<script>alert" not in page, "preview leaked a live <script> tag"
    assert "<img " not in page, "preview leaked a live <img> tag"
    # The escaped forms are present (content preserved, just inert) — proving the
    # payload reached the page only as text, not as a live tag/attribute.
    assert "&lt;script&gt;" in page
    assert "&lt;img" in page


def test_exported_metadata_json_still_holds_raw_values(tmp_path: Path) -> None:
    """The fix is presentation-layer only: the JSON-LD keeps the literal value.

    Escaping must happen when rendering HTML, not by mutating the crate graph —
    so the metadata document still carries the original (unescaped) string.
    """
    state = CrateState()
    state.session_id = "sess-xss2"
    state.metadata.title = SCRIPT_PAYLOAD
    state.metadata.output_path = str(tmp_path / "out2")
    res = build_crate(state)
    assert res["success"], res["error"]
    meta = json.loads(
        (Path(res["crate_path"]) / "ro-crate-metadata.json").read_text(encoding="utf-8")
    )
    blob = json.dumps(meta)
    assert SCRIPT_PAYLOAD in blob, "raw value must be preserved in the JSON-LD"
