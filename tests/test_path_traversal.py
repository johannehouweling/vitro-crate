"""Security sandbox for the agent file tools (#167).

These tests pin the three confirmed prompt-injection escape vectors and the
fail-closed contract that closes them, completing the #198/#197 approved-roots
foundation:

1. **Arbitrary local file read.** Every file-reading tool dispatched through
   ``AgentEngine.run_tool`` (``read_file``, ``read_excel``, ``read_docx``,
   ``read_file_sample``, ``read_multiple_files``, ``extract_pdf_text``,
   ``preview_archive``, ``unzip_file``) is gated against
   ``state.approved_scan_roots``. A path outside the approved roots — or any
   read at all when no root is approved (fail-closed) — is refused without
   touching the file.
2. **Path-traversal write on export.** A ``File`` entity whose ``dest_path``
   climbs out of the crate output dir (``../../../escaped``) is contained: no
   bytes are written outside ``output_dir``.
3. **Symlink escape.** A symlink inside an approved/input tree whose realpath
   escapes the tree is neither read nor copied into the payload.

Regression: a legitimate read inside an approved root, and a normal export of an
in-tree file, still succeed.
"""

from __future__ import annotations

import sys

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate

# Export/build_and_validate touch SHACL/disk; keep the slow modules bounded.
pytestmark = pytest.mark.timeout(120)


def _engine_with_root(root) -> AgentEngine:
    """An engine whose only approved scan root is *root* (a directory)."""
    engine = AgentEngine()
    engine.state.approved_scan_roots.add(str(root.resolve()))
    return engine


class TestArbitraryReadRefused:
    """Vector 1: read tools must honour approved_scan_roots (fail-closed)."""

    def test_read_file_outside_approved_roots_is_refused(self, tmp_path):
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")

        engine = _engine_with_root(approved)
        result = engine.run_tool("read_file", path=str(secret))
        assert result is None, "read_file leaked a file outside the approved roots"

    def test_read_file_sample_outside_approved_roots_is_refused(self, tmp_path):
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "creds.env"
        secret.write_text("API_KEY=deadbeef", encoding="utf-8")

        engine = _engine_with_root(approved)
        result = engine.run_tool("read_file_sample", path=str(secret))
        assert result is None

    def test_read_multiple_files_skips_paths_outside_roots(self, tmp_path):
        approved = tmp_path / "approved"
        approved.mkdir()
        inside = approved / "ok.txt"
        inside.write_text("inside content\n", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("nope\n", encoding="utf-8")

        engine = _engine_with_root(approved)
        result = engine.run_tool(
            "read_multiple_files", paths=[str(inside), str(secret)]
        )
        assert result["count"] == 1
        assert str(inside) in result["files"]
        assert str(secret) not in result["files"]
        assert str(secret) in result["skipped"]

    def test_extract_pdf_text_outside_roots_is_refused(self, tmp_path):
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # Not a real PDF — but the guard must refuse before ever opening it.
        bait = outside / "report.pdf"
        bait.write_text("%PDF-1.4 not really", encoding="utf-8")

        engine = _engine_with_root(approved)
        assert engine.run_tool("extract_pdf_text", path=str(bait)) is None

    def test_read_excel_outside_roots_is_refused(self, tmp_path):
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        bait = outside / "book.xlsx"
        bait.write_bytes(b"PK\x03\x04 not really xlsx")

        engine = _engine_with_root(approved)
        assert engine.run_tool("read_excel", path=str(bait)) is None

    def test_read_etc_passwd_is_refused(self, tmp_path):
        """The canonical PoC: read_file('/etc/passwd') must be refused even
        with an unrelated approved root present."""
        approved = tmp_path / "approved"
        approved.mkdir()
        engine = _engine_with_root(approved)
        # /etc/hosts is portable; /etc itself is also a forbidden root.
        assert engine.run_tool("read_file", path="/etc/hosts") is None

    def test_no_approved_roots_refuses_every_read(self, tmp_path):
        """Fail-closed: with an empty approved-roots set, even an in-tree read
        is refused (nothing is approved)."""
        f = tmp_path / "data.txt"
        f.write_text("hello\n", encoding="utf-8")

        engine = AgentEngine()
        assert engine.state.approved_scan_roots == set()
        assert engine.run_tool("read_file", path=str(f)) is None
        assert engine.run_tool("read_file_sample", path=str(f)) is None

    def test_forbidden_root_in_approved_roots_still_refused(self, tmp_path):
        """The hard denylist wins: a forbidden root listed in approved_roots
        cannot authorise a read."""
        engine = AgentEngine()
        engine.state.approved_scan_roots.add("/etc")
        assert engine.run_tool("read_file", path="/etc/hosts") is None

    def test_read_inside_approved_root_still_works(self, tmp_path):
        """Regression: a legitimate read inside an approved root succeeds."""
        approved = tmp_path / "approved"
        approved.mkdir()
        f = approved / "data.csv"
        f.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

        engine = _engine_with_root(approved)
        result = engine.run_tool("read_file", path=str(f))
        assert result is not None
        assert "a,b,c" in result


class TestSymlinkEscapeRefused:
    """Vector 3: a symlink whose realpath escapes the approved tree is refused
    for reads (its resolved target is what gets matched)."""

    def test_symlink_escape_read_is_refused(self, tmp_path):
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("exfil me", encoding="utf-8")

        link = approved / "innocent.txt"  # lives inside the approved root...
        link.symlink_to(secret)  # ...but points outside it.

        engine = _engine_with_root(approved)
        # The link path is lexically inside the root, but its realpath escapes.
        assert engine.run_tool("read_file", path=str(link)) is None


class TestExportTraversalContained:
    """Vector 2: a traversal dest_path on export must not write outside the
    crate output dir."""

    def test_dest_path_traversal_kept_inside_output_dir(self, tmp_path):
        inp = tmp_path / "in"
        (inp / "data").mkdir(parents=True)
        src = inp / "data" / "payload.csv"
        src.write_text("a,b\n1,2\n", encoding="utf-8")
        # Nest the crate output deep under tmp_path so a ``../../../`` escape
        # lands *inside* tmp_path (self-contained; never pollutes shared dirs).
        out = tmp_path / "a" / "b" / "c" / "out"

        state = CrateState()
        state.session_id = "trav"
        state.metadata.input_path = str(inp)
        state.metadata.output_path = str(out)
        state.add_entity(
            Entity(
                entity_id="f1",
                type="File",
                fields={
                    "name": "payload.csv",
                    "path": "data/payload.csv",
                    # Malicious LLM/injection-set destination escaping the crate.
                    "dest_path": "../../../escaped_pwned.csv",
                },
                _provenance=EntityProvenance(created_by="scanner"),
            )
        )

        res = build_crate(state)
        assert res["success"], res["error"]

        # The exact escape target must NOT exist.
        escaped = (out / ".." / ".." / ".." / "escaped_pwned.csv").resolve()
        assert not escaped.exists(), "traversal dest_path escaped the crate output dir"
        # Every written byte stays under output_dir.
        out_resolved = out.resolve()
        for p in out.rglob("*"):
            assert out_resolved in p.resolve().parents or p.resolve() == out_resolved
        # The payload landed at the safe in-crate fallback instead.
        assert (out / "data" / "payload.csv").is_file()

    def test_absolute_dest_path_kept_inside_output_dir(self, tmp_path):
        inp = tmp_path / "in"
        (inp / "data").mkdir(parents=True)
        src = inp / "data" / "payload.csv"
        src.write_text("x,y\n3,4\n", encoding="utf-8")
        out = tmp_path / "out"
        sentinel = tmp_path / "abs_escape.csv"

        state = CrateState()
        state.session_id = "abs"
        state.metadata.input_path = str(inp)
        state.metadata.output_path = str(out)
        state.add_entity(
            Entity(
                entity_id="f1",
                type="File",
                fields={
                    "name": "payload.csv",
                    "path": "data/payload.csv",
                    "dest_path": str(sentinel),
                },
                _provenance=EntityProvenance(created_by="scanner"),
            )
        )

        res = build_crate(state)
        assert res["success"], res["error"]
        assert not sentinel.exists(), "absolute dest_path escaped the crate output dir"

    def test_normal_export_of_in_tree_file_still_succeeds(self, tmp_path):
        """Regression: a normal File export still copies the payload."""
        inp = tmp_path / "in"
        (inp / "data").mkdir(parents=True)
        (inp / "data" / "ok.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        out = tmp_path / "out"

        state = CrateState()
        state.session_id = "normal"
        state.metadata.input_path = str(inp)
        state.metadata.output_path = str(out)
        state.add_entity(
            Entity(
                entity_id="f1",
                type="File",
                fields={"name": "ok.csv", "path": "data/ok.csv"},
                _provenance=EntityProvenance(created_by="scanner"),
            )
        )

        res = build_crate(state)
        assert res["success"], res["error"]
        copied = out / "data" / "ok.csv"
        assert copied.is_file()
        assert copied.read_text(encoding="utf-8") == "a,b\n1,2\n"


class TestExportSourceSymlinkContained:
    """Vector 3 (export side): a File whose source symlinks out of input_path
    must not be copied into the payload."""

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
    def test_symlink_source_escaping_input_is_not_copied(self, tmp_path):
        inp = tmp_path / "in"
        (inp / "data").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("EXFIL", encoding="utf-8")

        link = inp / "data" / "innocent.txt"
        link.symlink_to(secret)
        out = tmp_path / "out"

        state = CrateState()
        state.session_id = "symlink"
        state.metadata.input_path = str(inp)
        state.metadata.output_path = str(out)
        state.add_entity(
            Entity(
                entity_id="f1",
                type="File",
                fields={"name": "innocent.txt", "path": "data/innocent.txt"},
                _provenance=EntityProvenance(created_by="scanner"),
            )
        )

        res = build_crate(state)
        assert res["success"], res["error"]
        # The escaping symlink target's bytes must NOT have been packaged.
        for p in out.rglob("*"):
            if p.is_file():
                try:
                    assert p.read_text(encoding="utf-8", errors="ignore") != "EXFIL"
                except OSError:
                    pass
