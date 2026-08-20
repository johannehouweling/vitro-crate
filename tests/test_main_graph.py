"""Tests for the ``--graph`` CLI mode (provenance DAG front-end).

``--graph`` renders the LabProcess derivation DAG. By default it writes a
self-contained **HTML** page (mermaid.js renders it) and opens it in the
browser; ``--format mermaid`` prints the raw Mermaid source to stdout instead.
Unlike ``--interactive`` / ``--dashboard`` it needs no LLM config: it resolves a
crate from either an explicit ``--input`` (a crate dir or
``ro-crate-metadata.json``) or a session (``--resume`` / latest).
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


def test_parse_args_graph_format_defaults_to_html() -> None:
    assert parse_args(["--graph"]).format == "html"
    assert parse_args(["--graph", "--format", "mermaid"]).format == "mermaid"


def test_parse_args_graph_view_and_layer_defaults() -> None:
    a = parse_args(["--graph"])
    assert a.view == "crate"
    assert a.layer == "all"
    assert a.all_edges is False
    assert parse_args(["--graph", "--view", "provenance"]).view == "provenance"
    assert parse_args(["--graph", "--view", "labprocesses"]).view == "labprocesses"
    assert parse_args(["--graph", "--layer", "isa"]).layer == "isa"


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


# --- raw mermaid source (--format mermaid) ----------------------------------


def test_graph_mermaid_from_metadata_file(tmp_path, capsys) -> None:
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    rc = main(["--graph", "--format", "mermaid", "--input", str(meta)])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("flowchart")  # crate view (default) renders TD
    assert "Exposure" in out and "Condition table" in out


def test_graph_mermaid_from_crate_dir(tmp_path, capsys) -> None:
    _write_metadata(tmp_path / "ro-crate-metadata.json")
    rc = main(["--graph", "--format", "mermaid", "--input", str(tmp_path)])
    assert rc == 0
    assert "Exposure" in capsys.readouterr().out


def test_graph_crate_view_has_layer_subgraphs(tmp_path, capsys) -> None:
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    rc = main(["--graph", "--format", "mermaid", "--input", str(meta)])
    assert rc == 0
    out = capsys.readouterr().out
    # The full crate view groups by paper layer and ships a legend.
    assert "Domain" in out and "Legend" in out


def test_graph_layer_filter_drops_domain(tmp_path, capsys) -> None:
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    rc = main(["--graph", "--format", "mermaid", "--layer", "isa", "--input", str(meta)])
    assert rc == 0
    out = capsys.readouterr().out
    # The Exposure process is domain (layer 3) → hidden when filtering to isa.
    assert "Exposure" not in out


def test_graph_labprocesses_view(tmp_path, capsys) -> None:
    """The view is 'labprocesses' now; 'provenance' is the name it shipped
    under and renders the same thing — renaming a CLI value without keeping
    the old one breaks every script that passes it."""
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    rendered = []
    for view in ("labprocesses", "provenance"):
        rc = main(["--graph", "--view", view, "--format", "mermaid", "--input", str(meta)])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.startswith("flowchart LR")  # the chain is LR, no legend
        assert "Legend" not in out
        rendered.append(out)
    assert rendered[0] == rendered[1]


# --- rendered HTML (default) ------------------------------------------------


def test_graph_html_writes_file_and_opens(tmp_path, monkeypatch, capsys) -> None:
    opened: list[str] = []
    monkeypatch.setattr(main_mod.webbrowser, "open", lambda uri: opened.append(uri) or True)
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    out_html = tmp_path / "dag.html"
    rc = main(["--graph", "--input", str(meta), "--graph-out", str(out_html)])
    assert rc == 0
    assert out_html.is_file()
    html = out_html.read_text(encoding="utf-8")
    assert "mermaid" in html and "Exposure" in html
    # The file was opened in the browser (as a file:// URI).
    assert opened and opened[0].startswith("file://")
    # The path is reported on stderr (stdout stays clean for piping).
    assert str(out_html) in capsys.readouterr().err


def test_graph_html_no_browser_suppresses_open(tmp_path, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(main_mod.webbrowser, "open", lambda uri: opened.append(uri) or True)
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    out_html = tmp_path / "dag.html"
    rc = main(
        ["--graph", "--input", str(meta), "--graph-out", str(out_html), "--no-browser"]
    )
    assert rc == 0
    assert out_html.is_file()
    assert opened == []


def test_graph_html_default_path_when_no_out(tmp_path, monkeypatch, capsys) -> None:
    """Without --graph-out a temp HTML file is written and its path reported."""
    monkeypatch.setattr(main_mod.webbrowser, "open", lambda uri: True)
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    rc = main(["--graph", "--input", str(meta)])
    assert rc == 0
    err = capsys.readouterr().err
    assert ".html" in err


# --- source resolution & errors ---------------------------------------------


def test_graph_malformed_json_errors_gracefully(tmp_path, capsys) -> None:
    bad = tmp_path / "ro-crate-metadata.json"
    bad.write_text('{"invalid": json}', encoding="utf-8")
    rc = main(["--graph", "--format", "mermaid", "--input", str(bad)])
    assert rc == 1  # graceful "no crate" exit, not a traceback
    assert capsys.readouterr().err


def test_graph_html_browser_raise_is_handled(tmp_path, monkeypatch) -> None:
    def _boom(_uri):
        raise OSError("no browser")

    monkeypatch.setattr(main_mod.webbrowser, "open", _boom)
    meta = tmp_path / "ro-crate-metadata.json"
    _write_metadata(meta)
    out_html = tmp_path / "dag.html"
    rc = main(["--graph", "--input", str(meta), "--graph-out", str(out_html)])
    assert rc == 0  # the file is written; a browser failure does not crash
    assert out_html.is_file()


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


def test_graph_from_latest_session(monkeypatch, capsys) -> None:
    """With no --input/--resume, --graph assembles the latest session in memory."""
    monkeypatch.setattr(
        session_mod, "list_sessions", lambda: [{"session_id": "20260101_000000"}]
    )
    monkeypatch.setattr(session_mod, "load_session", lambda _sid: _exposure_state())
    rc = main(["--graph", "--format", "mermaid"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("flowchart")
    assert "Exposure" in out and "Condition table" in out


def test_graph_from_resumed_session(monkeypatch, capsys) -> None:
    captured: dict[str, str] = {}

    def _load(sid: str) -> CrateState:
        captured["sid"] = sid
        return _exposure_state()

    monkeypatch.setattr(session_mod, "load_session", _load)
    rc = main(["--graph", "--format", "mermaid", "--resume", "20251231_235959"])
    assert rc == 0
    assert captured["sid"] == "20251231_235959"
    assert "Exposure" in capsys.readouterr().out


def test_graph_missing_session_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(session_mod, "load_session", lambda _sid: None)
    rc = main(["--graph", "--resume", "nope"])
    assert rc == 1
    assert capsys.readouterr().err
