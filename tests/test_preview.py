"""Tests for the bundled RO-Crate preview (#86).

We ship the standard `ro-crate-preview.html` that ro-crate-py generates — a
human-readable summary (a CreativeWork `about` the Root Data Entity) — on every
on-disk export, so a written crate is browsable without tooling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate

# Every test here exports a crate, and each export now runs the uncached,
# owlrl-heavy validator over all three profiles at the full severity gate (#446)
# — ~10s per export locally, and the 2-vCPU CI runner is ~2-3x slower, which puts
# the whole module against the CI-wide `--timeout=30`. Same headroom, for the
# same reason, that the other export-heavy modules already take
# (test_export_smoke, test_readers, test_path_traversal, test_html_xss).
# Headroom, not a licence to grow: no test in this module is changed.
pytestmark = pytest.mark.timeout(120)

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


class TestHtmlSafeCopy:
    """The community generator escapes nothing (#169), so it is handed a copy of
    the metadata whose strings cannot act as markup (#849)."""

    def test_strings_are_escaped_and_everything_else_is_left_alone(self) -> None:
        from builder.tools.builder import _html_safe

        doc = {
            "@graph": [
                {
                    "@id": "#a",
                    "name": '<b>x</b> & "q"',
                    "n": 3,
                    "ok": True,
                    "tags": ["a<b", {"@id": "#c", "description": "it's"}],
                }
            ]
        }
        safe = _html_safe(doc)
        node = safe["@graph"][0]
        assert node["name"] == "&lt;b&gt;x&lt;/b&gt; &amp; &quot;q&quot;"
        assert node["tags"][0] == "a&lt;b"
        assert node["tags"][1]["description"] == "it&#x27;s"
        assert node["n"] == 3 and node["ok"] is True
        assert list(node) == list(doc["@graph"][0])
        assert doc["@graph"][0]["name"] == '<b>x</b> & "q"', "the original was mutated"

    def test_a_script_scheme_cannot_become_a_link(self) -> None:
        from builder.tools.builder import _html_safe

        schemes = ("javascript:alert(1)", " JavaScript:alert(1)", "vbscript:x", "data:text/html,x")
        for value in schemes:
            safe = _html_safe({"@id": value})["@id"]
            assert ":" not in safe.split("-", 1)[0], safe


class TestCommunityPreview:
    """ro-crate-preview.html comes from the RO-Crate community generator when one
    is on the machine, and from ro-crate-py's escaped template otherwise (#849)."""

    @pytest.fixture(autouse=True)
    def _fresh_memo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.tools.builder as mod

        monkeypatch.setattr(mod, "_preview_generator_memo", None)

    @staticmethod
    def _fake_generator(tmp_path: Path, *, exit_code: int = 0) -> list[str]:
        """A stand-in for ``rochtml``: renders the root name, logs each call."""
        script = tmp_path / "fake_rochtml.py"
        script.write_text(
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(tmp_path / 'calls.log')!r}).open('a').write('run\\n')\n"
            "src = pathlib.Path(sys.argv[1])\n"
            "doc = json.loads(src.read_text())\n"
            "name = next(e['name'] for e in doc['@graph'] if e['@id'] == './')\n"
            "out = src.with_name('ro-crate-preview.html')\n"
            "out.write_text('<html>GENERATED ' + name + '</html>')\n"
            f"sys.exit({exit_code})\n"
        )
        return [sys.executable, str(script)]

    def test_the_generator_renders_an_escaped_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.tools.builder as mod

        monkeypatch.setattr(mod, "_find_preview_generator", lambda: self._fake_generator(tmp_path))
        state = _state(tmp_path)
        state.metadata.title = "<b>Bold</b> study"

        res = build_crate(state)
        assert res["success"], res["error"]

        page = (Path(res["crate_path"]) / "ro-crate-preview.html").read_text(encoding="utf-8")
        # The base profile requires the doctype; the generator's page starts at <html>.
        assert page.startswith("<!DOCTYPE html>\n<html>GENERATED")
        assert "&lt;b&gt;Bold&lt;/b&gt;" in page and "<b>" not in page
        meta = json.loads(
            (Path(res["crate_path"]) / "ro-crate-metadata.json").read_text(encoding="utf-8")
        )
        root = next(e for e in meta["@graph"] if e["@id"] == "./")
        assert root["name"] == "<b>Bold</b> study", "the crate itself must keep the raw value"

    def test_without_a_generator_ro_crate_py_renders_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.tools.builder as mod

        monkeypatch.setattr(mod, "_find_preview_generator", lambda: None)

        res = build_crate(_state(tmp_path))
        assert res["success"], res["error"]

        page = (Path(res["crate_path"]) / "ro-crate-preview.html").read_text(encoding="utf-8")
        assert "GENERATED" not in page and "<html" in page.lower()

    def test_the_real_generator_escapes_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a machine with ro-crate-html, the page it renders carries the crate's
        strings as inert text (#169) and the doctype the base profile requires."""
        import builder.tools.builder as mod

        if mod._find_preview_generator() is None:
            pytest.skip("no ro-crate-html generator on this machine")
        monkeypatch.setattr(mod, "_preview_generator_memo", None)
        state = _state(tmp_path)
        state.metadata.title = "<script>alert(1)</script> study"

        res = build_crate(state)
        assert res["success"], res["error"]
        if mod._preview_generator_memo is False:
            pytest.skip("the generator could not run here (offline, or npx without its cache)")

        page = (Path(res["crate_path"]) / "ro-crate-preview.html").read_text(encoding="utf-8")
        assert page.startswith("<!DOCTYPE html>")
        assert "ro-crate-dynamic" in page, "not the community page"
        assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page

    def test_a_failing_generator_is_tried_once_per_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.tools.builder as mod

        cmd = self._fake_generator(tmp_path, exit_code=1)
        monkeypatch.setattr(mod, "_find_preview_generator", lambda: cmd)

        first = build_crate(_state(tmp_path / "one"))
        second = build_crate(_state(tmp_path / "two"))
        assert first["success"] and second["success"]

        for res in (first, second):
            page = (Path(res["crate_path"]) / "ro-crate-preview.html").read_text(encoding="utf-8")
            assert "<html" in page.lower() and "GENERATED" not in page, "a failed render was kept"
        assert (tmp_path / "calls.log").read_text().count("run") == 1, "the generator was retried"

