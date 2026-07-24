"""Tests for builder/tools/builder.py — build_crate tool."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from builder.state import CrateState, Entity, EntityProvenance, FileClassification
from builder.tools.builder import assemble_crate, build_crate, export_crate


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="user"),
    )


def _file_ids(crate):
    """@id of every File node in the generated crate graph."""
    graph = crate.metadata.generate()["@graph"]
    ids = []
    for node in graph:
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "File" in types:
            ids.append(node["@id"])
    return ids


class TestAutoIncludeScanned:
    """Issue #175: assemble_crate packages every scanned file by default (option B)."""

    def _state_with_files(self, tmp_path):
        (tmp_path / "raw").mkdir()
        (tmp_path / "raw" / "a.mzML").write_text("x")
        (tmp_path / "raw" / "b.csv").write_text("x")
        (tmp_path / "notes.txt").write_text("x")
        state = CrateState()
        state.metadata.title = "T"
        state.metadata.description = "d"
        state.metadata.accession = "ACC-1"
        state.metadata.input_path = str(tmp_path)
        state.scanned_files = [
            FileClassification(
                str(tmp_path / "raw" / "a.mzML"), "a.mzML", 1, "application/x-mzml"
            ),
            FileClassification(str(tmp_path / "raw" / "b.csv"), "b.csv", 1, "text/csv"),
            FileClassification(str(tmp_path / "notes.txt"), "notes.txt", 1, "text/plain"),
        ]
        # The agent drafted b.csv (with a role) — its entity must take precedence.
        state.add_entity(
            _ent("file_b", "File", name="b.csv", path="raw/b.csv", role="raw_data")
        )
        return state

    def test_auto_includes_all_scanned_files(self, tmp_path):
        state = self._state_with_files(tmp_path)
        crate = assemble_crate(state, output_dir=tmp_path, materialize_payload=True)
        ids = _file_ids(crate)
        # Every scanned file is in the crate, mirroring its path under input_path.
        assert any(i.endswith("raw/a.mzML") for i in ids), ids
        assert any(i.endswith("notes.txt") for i in ids), ids
        # The drafted b.csv appears exactly once (no auto-duplicate).
        assert sum(1 for i in ids if i.endswith("b.csv")) == 1, ids

    def test_include_all_scanned_false_keeps_only_drafted(self, tmp_path):
        state = self._state_with_files(tmp_path)
        crate = assemble_crate(
            state,
            output_dir=None,
            materialize_payload=False,
            include_all_scanned=False,
        )
        ids = _file_ids(crate)
        assert not any(i.endswith("a.mzML") for i in ids), ids
        assert not any(i.endswith("notes.txt") for i in ids), ids
        assert any(i.endswith("b.csv") for i in ids), ids


class TestEmbeddedGraph:
    """export_crate writes the entity-graph diagram into the crate (#130)."""

    def _state(self):
        state = CrateState()
        state.metadata.title = "T"
        state.metadata.description = "d"
        state.metadata.accession = "ACC-1"
        state.add_entity(_ent("study_1", "Study", name="S"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        return state

    def test_export_writes_and_registers_graph(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "crate"
            assert export_crate(self._state(), str(out))["success"] is True

            mmd = out / "ro-crate-graph.mmd"
            assert mmd.is_file()
            assert mmd.read_text(encoding="utf-8").startswith("flowchart")

            graph = json.loads((out / "ro-crate-metadata.json").read_text())["@graph"]
            by_id = {e["@id"]: e for e in graph}

            # Registered as a File + CreativeWork about the Root Data Entity …
            node = by_id["ro-crate-graph.mmd"]
            types = node["@type"] if isinstance(node["@type"], list) else [node["@type"]]
            assert "File" in types and "CreativeWork" in types
            assert node.get("encodingFormat") == "text/vnd.mermaid"
            assert node.get("about") == {"@id": "./"}

            # … and referenced from the Root Data Entity's hasPart.
            parts = by_id["./"].get("hasPart") or []
            part_ids = [r.get("@id") for r in parts if isinstance(r, dict)]
            assert "ro-crate-graph.mmd" in part_ids

    def test_embedded_graph_does_not_depict_itself(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "crate"
            export_crate(self._state(), str(out))
            content = (out / "ro-crate-graph.mmd").read_text(encoding="utf-8")
            # The graph is generated before its own File node is added.
            assert "RO-Crate entity graph" not in content

    def test_embedding_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "crate"
            export_crate(self._state(), str(out), embed_graph=False)
            assert not (out / "ro-crate-graph.mmd").exists()
            graph = json.loads((out / "ro-crate-metadata.json").read_text())["@graph"]
            assert "ro-crate-graph.mmd" not in {e["@id"] for e in graph}


class TestRootName:
    """assemble_crate sets a meaningful root name so ro-crate-py's preview header
    isn't 'Untitled Investigation' (#272).

    The root dataset (``./``) name/description is derived from the Investigation
    (fallback: Study; final fallback: a sensible default) when the session-level
    title/description are not set.
    """

    _INV_NAME = "Inhibition of OATP1C1-mediated cellular uptake of thyroxine"

    def _root(self, crate):
        graph = crate.metadata.generate()["@graph"]
        return next(e for e in graph if e.get("@id") == "./")

    def test_root_name_from_investigation(self):
        state = CrateState()
        state.add_entity(_ent("inv_1", "Investigation", name=self._INV_NAME))
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)

        assert crate.root_dataset["name"] == self._INV_NAME
        root = self._root(crate)
        assert root["name"] == self._INV_NAME
        assert root.get("description"), "root description must be set"
        assert root["name"] != "Untitled Investigation"

    def test_investigation_description_used_when_present(self):
        state = CrateState()
        state.add_entity(
            _ent(
                "inv_1",
                "Investigation",
                name=self._INV_NAME,
                description="Thyroxine uptake inhibition assay.",
            )
        )
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
        root = self._root(crate)
        assert root["description"] == "Thyroxine uptake inhibition assay."

    def test_root_name_falls_back_to_study(self):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="OATP1C1 uptake study"))
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)

        assert crate.root_dataset["name"] == "OATP1C1 uptake study"
        assert self._root(crate)["name"] != "Untitled Investigation"

    def test_root_name_default_when_neither(self):
        state = CrateState()
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)

        name = crate.root_dataset["name"]
        assert name, "root name must be non-empty"
        assert name != "Untitled Investigation"

    def test_explicit_metadata_title_wins(self):
        """A session-level title still takes precedence over the Investigation."""
        state = CrateState()
        state.metadata.title = "Explicit session title"
        state.add_entity(_ent("inv_1", "Investigation", name=self._INV_NAME))
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
        assert crate.root_dataset["name"] == "Explicit session title"

    def test_written_preview_title_is_not_untitled(self):
        """The on-disk ro-crate-preview.html <title> reflects the root name (#272)."""
        state = CrateState()
        state.add_entity(_ent("inv_1", "Investigation", name=self._INV_NAME))
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "crate"
            assert export_crate(state, str(out))["success"] is True
            page = (out / "ro-crate-preview.html").read_text(encoding="utf-8")
            assert "Untitled" not in page
            assert self._INV_NAME in page


class TestExportCrate:
    """export_crate is the explicit disk-writer (the only step that touches disk)."""

    def test_export_crate_writes_to_disk(self):
        state = CrateState()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "crate")
            result = export_crate(state, output_path)

            assert result["success"] is True
            assert result["crate_path"] == output_path
            metadata_path = Path(output_path) / "ro-crate-metadata.json"
            assert metadata_path.exists()
            with open(metadata_path) as f:
                metadata = json.load(f)
            assert "@context" in metadata
            assert "@graph" in metadata

    def test_build_crate_is_export_crate_alias(self):
        """build_crate stays as a back-compat alias for export_crate."""
        state = CrateState()
        state.metadata.title = "Alias check"
        with tempfile.TemporaryDirectory() as tmpdir:
            via_build = build_crate(state, str(Path(tmpdir) / "a"))
            via_export = export_crate(state, str(Path(tmpdir) / "b"))

            assert via_build["success"] is via_export["success"] is True
            graph_a = json.loads(
                (Path(via_build["crate_path"]) / "ro-crate-metadata.json").read_text()
            )["@graph"]
            graph_b = json.loads(
                (Path(via_export["crate_path"]) / "ro-crate-metadata.json").read_text()
            )["@graph"]
            roots_a = [e for e in graph_a if e.get("@type") == "Dataset"][0]
            roots_b = [e for e in graph_b if e.get("@type") == "Dataset"][0]
            assert roots_a.get("name") == roots_b.get("name") == "Alias check"


class TestBuildCrate:
    """Tests for build_crate — assembles a crate directory from CrateState."""

    def test_creates_output_directory(self):
        """build_crate creates the output directory if it doesn't exist."""
        state = CrateState()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "my_crate")
            result = build_crate(state, output_path)

            assert result["success"] is True
            assert result["crate_path"] == output_path
            assert result["error"] is None
            assert Path(output_path).is_dir()

    def test_writes_minimal_ro_crate_metadata_json(self):
        """build_crate writes a ro-crate-metadata.json with basic structure."""
        state = CrateState()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "my_crate")
            result = build_crate(state, output_path)

            assert result["success"] is True
            metadata_path = Path(output_path) / "ro-crate-metadata.json"
            assert metadata_path.exists()

            with open(metadata_path) as f:
                metadata = json.load(f)

            # Must have @context and @graph
            assert "@context" in metadata
            assert "@graph" in metadata
            # Should have a root dataset
            roots = [e for e in metadata["@graph"] if e.get("@type") == "Dataset"]
            assert len(roots) >= 1

    def test_includes_state_metadata_in_crate(self):
        """build_crate includes session metadata from CrateState."""
        state = CrateState()
        state.metadata.title = "My Test Crate"
        state.metadata.description = "A description"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "crate")
            result = build_crate(state, output_path)

            assert result["success"] is True
            metadata_path = Path(output_path) / "ro-crate-metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            # The root dataset should have the name and description
            root = [e for e in metadata["@graph"] if e.get("@type") == "Dataset"][0]
            assert root.get("name") == "My Test Crate"
            assert root.get("description") == "A description"

    def test_returns_error_on_invalid_path(self):
        """build_crate returns error dict on failure (e.g. non-writable path)."""
        state = CrateState()
        state.session_id = "test_invalid_path"
        # An empty string now uses the default, so use a path that will fail
        # (root-owned dir without write permission)
        result = build_crate(state, "/proc/0/crate")

        assert result["success"] is False
        assert result["error"] is not None

    def test_uses_rocrate_py_metadata_descriptor(self):
        """build_crate assembles via ro-crate-py, not a hand-rolled dict.

        ro-crate-py always emits a self-describing RO-Crate Metadata Descriptor
        entity (@id ro-crate-metadata.json, @type CreativeWork) whose conformsTo
        names the RO-Crate 1.1 spec and whose `about` points at the root. A
        hand-rolled graph does not produce this, so its presence proves the
        library is doing the assembly.
        """
        state = CrateState()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "crate")
            result = build_crate(state, output_path)

            assert result["success"] is True
            with open(Path(output_path) / "ro-crate-metadata.json") as f:
                metadata = json.load(f)

            descriptor = next(
                e for e in metadata["@graph"] if e.get("@id") == "ro-crate-metadata.json"
            )
            assert descriptor.get("@type") == "CreativeWork"
            assert descriptor.get("about") == {"@id": "./"}
            conforms = descriptor.get("conformsTo")
            conforms_ids = (
                [conforms.get("@id")]
                if isinstance(conforms, dict)
                else [c.get("@id") for c in conforms or []]
            )
            # ro-crate-py stamps the descriptor with the RO-Crate spec it conforms
            # to; the exact minor version is pinned explicitly in Step 2.
            assert any("w3id.org/ro/crate" in cid for cid in conforms_ids)

    def test_build_crate_path_round_trips_into_validate(self):
        """build_crate's returned crate_path is a REAL on-disk crate dir that feeds
        straight back into validate() — the build->validate round-trip the name claims.

        (That ``crate_path == output_path`` is already covered by the echo assertions
        earlier in this file; this exercises the actual round-trip instead of re-asserting
        the input argument.)
        """
        from builder.state import ValidationReport
        from builder.tools.validation import validate

        state = CrateState()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "my_crate")
            result = build_crate(state, output_path)
            assert result["success"] is True

            # The returned path pointed at a real crate dir, so validate ran SHACL over
            # it (not the missing-crate guard) and produced a report.
            report = validate(state, result["crate_path"])
            assert isinstance(report, ValidationReport)
            assert (Path(result["crate_path"]) / "ro-crate-metadata.json").is_file()

    def test_build_crate_generates_default_crate_path_to_sessions(self, monkeypatch):
        """build_crate uses a session-derived default when output_path is not given.

        The default should be: sessions/<session_id>/working_crate/
        """
        state = CrateState()
        state.session_id = "test_default_path_001"

        # Call without output_path — the function should supply a default.
        # Assert the default session root convention (relative "sessions/"), so
        # clear the test harness's VITRO_SESSION_DIR isolation override.
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.chdir(tmpdir)
            monkeypatch.delenv("VITRO_SESSION_DIR", raising=False)
            result = build_crate(state)

            assert result["success"] is True
            expected = f"sessions/{state.session_id}/working_crate"
            assert result["crate_path"] == expected
            assert Path(expected).is_dir()
