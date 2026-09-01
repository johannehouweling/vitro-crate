"""Tests for the ``--graph`` CLI mode (the entity explorer front-end).

``--graph`` writes the crate's interactive entity explorer as a self-contained
HTML page and opens it in the browser. It is the same section the maturity
report embeds, in the report's own shell — one explorer rendered in two places
(#618). Unlike ``--interactive`` / ``--dashboard`` it needs no LLM config: it
resolves a crate from either an explicit ``--input`` (a crate dir or
``ro-crate-metadata.json``) or a session (``--resume`` / latest).

Before #618 this mode emitted Mermaid — ``--format mermaid`` to stdout, or an
HTML page that fetched mermaid.js from a CDN to draw it. Nothing rendered the
Mermaid the crate shipped, and the CDN made this the one output in the repo that
needed the network to display.
"""

from __future__ import annotations

import json

import builder.tools.session as session_mod
import main as main_mod
from builder.state import CrateState, Entity, EntityProvenance
from main import main, parse_args


def test_parse_args_graph_flag() -> None:
    assert parse_args(["--graph"]).graph is True
    assert parse_args(["-g"]).graph is True
    assert parse_args([]).graph is False


def test_parse_args_graph_view_defaults_to_the_whole_crate() -> None:
    """The explorer's own opening view: everything the crate describes."""
    assert parse_args(["--graph"]).view == "crate"


def test_parse_args_keeps_the_view_names_it_shipped_with() -> None:
    """`provenance` was renamed `labprocesses` and both stayed accepted; `crate`
    names the whole-graph view; `researcher` named a view the explorer no longer
    has. Renaming a CLI value silently breaks every script that passes it, so all
    four still resolve."""
    for view in ("crate", "labprocesses", "provenance", "researcher"):
        assert parse_args(["--graph", "--view", view]).view == view


def _write_metadata(path) -> None:
    doc = {
        "@graph": [
            {"@id": "./", "@type": "Dataset"},
            {
                "@id": "#exp",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
                "object": {"@id": "#cells"},
                "result": {"@id": "#table"},
            },
            {"@id": "#cells", "@type": "Sample", "name": "Cultured cells"},
            {"@id": "#table", "@type": ["File", "csvw:Table"], "name": "Condition table"},
        ]
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def _render(tmp_path, *args: str) -> str:
    """Run ``--graph`` over a written crate and return the page it wrote."""
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    out_html = tmp_path / "graph.html"
    rc = main(["--graph", "--input", str(meta), "--graph-out", str(out_html), "--no-browser", *args])
    assert rc == 0
    return out_html.read_text(encoding="utf-8")


# --- what it writes ---------------------------------------------------------


def test_graph_writes_the_entity_explorer(tmp_path) -> None:
    page = _render(tmp_path)

    assert page.startswith("<!DOCTYPE html>")
    assert 'id="entity-explorer"' in page
    assert 'id="ex-data"' in page
    assert "Exposure" in page and "Condition table" in page


def test_the_page_it_writes_needs_no_network(tmp_path) -> None:
    """The reason this mode changed: its HTML used to fetch mermaid.js from a
    CDN, so the one artifact meant for looking at was the one that failed
    offline."""
    import re

    page = _render(tmp_path)

    markup = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    assert "src=" not in markup
    assert "cdn." not in markup and "mermaid" not in markup.lower()


def test_graph_reads_a_crate_directory(tmp_path) -> None:
    _write_metadata(tmp_path / "ro-crate-metadata.json")
    out_html = tmp_path / "graph.html"

    rc = main(["--graph", "--input", str(tmp_path), "--graph-out", str(out_html), "--no-browser"])

    assert rc == 0
    assert "Exposure" in out_html.read_text(encoding="utf-8")


def test_the_view_flag_chooses_which_toggle_opens(tmp_path) -> None:
    """The Mermaid-era views are now toggles inside the page, so `--view` picks
    the one it opens on rather than a different renderer."""
    opening = {}
    for flag, key in (
        # `researcher` drew the science and hid the packaging; the assay lanes
        # serve that reader better, so the flag opens on the whole crate.
        ("researcher", "all"),
        ("crate", "all"),
        ("labprocesses", "processes"),
        ("provenance", "processes"),
    ):
        page = _render(tmp_path, "--view", flag)
        payload = json.loads(
            page.split('id="ex-data" type="application/json">', 1)[1].split("</script>", 1)[0]
        )
        opening[flag] = [v["key"] for v in payload["views"] if v["default"]]
        assert opening[flag] == [key], (flag, opening[flag])
    assert opening["labprocesses"] == opening["provenance"]


# --- browser handling -------------------------------------------------------


def test_graph_writes_file_and_opens(tmp_path, monkeypatch, capsys) -> None:
    opened: list[str] = []
    monkeypatch.setattr(main_mod.webbrowser, "open", lambda uri: opened.append(uri) or True)
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    out_html = tmp_path / "graph.html"

    rc = main(["--graph", "--input", str(meta), "--graph-out", str(out_html)])

    assert rc == 0
    assert out_html.is_file()
    assert opened and opened[0].startswith("file://")
    # The path is reported on stderr (stdout stays clean for piping).
    assert str(out_html) in capsys.readouterr().err


def test_graph_no_browser_suppresses_open(tmp_path, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(main_mod.webbrowser, "open", lambda uri: opened.append(uri) or True)
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    out_html = tmp_path / "graph.html"

    rc = main(["--graph", "--input", str(meta), "--graph-out", str(out_html), "--no-browser"])

    assert rc == 0
    assert out_html.is_file()
    assert opened == []


def test_graph_default_path_when_no_out(tmp_path, monkeypatch, capsys) -> None:
    """Without --graph-out a temp HTML file is written and its path reported."""
    monkeypatch.setattr(main_mod.webbrowser, "open", lambda uri: True)
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)

    rc = main(["--graph", "--input", str(meta)])

    assert rc == 0
    assert ".html" in capsys.readouterr().err


def test_graph_browser_raise_is_handled(tmp_path, monkeypatch) -> None:
    def _boom(_uri):
        raise OSError("no browser")

    monkeypatch.setattr(main_mod.webbrowser, "open", _boom)
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    out_html = tmp_path / "graph.html"

    rc = main(["--graph", "--input", str(meta), "--graph-out", str(out_html)])

    assert rc == 0  # the file is written; a browser failure does not crash
    assert out_html.is_file()


# --- source resolution & errors ---------------------------------------------


def test_graph_malformed_json_errors_gracefully(tmp_path, capsys) -> None:
    bad = tmp_path / "ro-crate-metadata.json"
    bad.write_text('{"invalid": json}', encoding="utf-8")

    rc = main(["--graph", "--input", str(bad), "--no-browser"])

    assert rc == 1  # graceful "no crate" exit, not a traceback
    assert capsys.readouterr().err


def test_graph_no_source_errors(tmp_path, monkeypatch, capsys) -> None:
    # Empty cwd → no sessions, no input → graceful error.
    monkeypatch.chdir(tmp_path)

    rc = main(["--graph"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "no crate" in err.lower() or "--input" in err


def _exposure_state() -> CrateState:
    state = CrateState()
    state.metadata.title = "Exposure crate"
    state.add_entity(
        Entity(
            entity_id="proc_exp",
            type="LabProcess",
            fields={"process_type": "Exposure", "name": "Exposure step"},
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


def test_graph_from_latest_session(tmp_path, monkeypatch, capsys) -> None:
    """With no --input/--resume, --graph assembles the latest session in memory."""
    monkeypatch.setattr(
        session_mod, "list_sessions", lambda: [{"session_id": "20260101_000000"}]
    )
    monkeypatch.setattr(session_mod, "load_session", lambda _sid: _exposure_state())
    out_html = tmp_path / "graph.html"

    rc = main(["--graph", "--graph-out", str(out_html), "--no-browser"])

    assert rc == 0
    assert "Exposure step" in out_html.read_text(encoding="utf-8")


def test_graph_from_resumed_session(tmp_path, monkeypatch) -> None:
    captured: dict[str, str] = {}

    def _load(sid: str) -> CrateState:
        captured["sid"] = sid
        return _exposure_state()

    monkeypatch.setattr(session_mod, "load_session", _load)
    out_html = tmp_path / "graph.html"

    rc = main(["--graph", "--resume", "20251231_235959", "--graph-out", str(out_html), "--no-browser"])

    assert rc == 0
    assert captured["sid"] == "20251231_235959"
    assert "Exposure step" in out_html.read_text(encoding="utf-8")


def test_graph_missing_session_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(session_mod, "load_session", lambda _sid: None)

    rc = main(["--graph", "--resume", "nope"])

    assert rc == 1
    assert capsys.readouterr().err
