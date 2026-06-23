"""Tests for the bundled RO-Crate preview (#86).

We ship the standard `ro-crate-preview.html` that ro-crate-py generates — a
human-readable summary (a CreativeWork `about` the Root Data Entity) — on every
on-disk export, so a written crate is browsable without tooling.
"""

from __future__ import annotations

import json
from pathlib import Path

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate


def _state(tmp_path: Path) -> CrateState:
    state = CrateState()
    state.session_id = "sess-prev"
    state.metadata.title = "My Tox Study"
    state.metadata.description = "An in vitro assay crate."
    state.metadata.output_path = str(tmp_path / "out")
    state.add_entity(
        Entity(
            entity_id="inv1",
            type="Investigation",
            fields={"name": "Investigation One"},
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


class TestPreviewWritten:
    """export_crate writes ro-crate-preview.html into the crate."""

    def test_export_writes_preview_html(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        res = build_crate(state)
        assert res["success"], res["error"]

        preview = Path(res["crate_path"]) / "ro-crate-preview.html"
        assert preview.is_file()
        page = preview.read_text(encoding="utf-8")
        assert "<html" in page.lower()
        assert page.strip() != ""

    def test_preview_referenced_in_metadata(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        res = build_crate(state)
        meta = json.loads(
            (Path(res["crate_path"]) / "ro-crate-metadata.json").read_text(encoding="utf-8")
        )
        graph = meta["@graph"]
        prev = next((e for e in graph if e.get("@id") == "ro-crate-preview.html"), None)
        assert prev is not None, "preview not referenced in ro-crate-metadata.json"
        assert prev.get("@type") == "CreativeWork"
        assert prev.get("about") == {"@id": "./"}
