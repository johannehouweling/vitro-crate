"""Tests for archive-extraction hardening + tar support (#147)."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from builder.tools import scanner
from builder.tools.scanner import preview_archive, unzip_file


class TestZipSlip:
    """Members must not escape the destination directory."""

    def test_zip_traversal_member_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../evil.txt", "pwned")
        dest = tmp_path / "out"

        result = unzip_file(str(archive), str(dest))

        assert "error" in result
        assert "unsafe" in result["error"].lower() or "zip-slip" in result["error"].lower()
        # Nothing was written outside the destination.
        assert not (tmp_path / "evil.txt").exists()
        assert not (dest.parent / "evil.txt").exists()

    def test_tar_traversal_member_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "evil.tar"
        data = b"pwned"
        info = tarfile.TarInfo(name="../evil.txt")
        info.size = len(data)
        with tarfile.open(archive, "w") as tf:
            tf.addfile(info, io.BytesIO(data))
        dest = tmp_path / "out"

        result = unzip_file(str(archive), str(dest))

        assert "error" in result
        assert not (tmp_path / "evil.txt").exists()


class TestZipBomb:
    """Total uncompressed size is capped."""

    def test_oversized_archive_refused(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(scanner, "_MAX_UNCOMPRESSED_BYTES", 10)
        archive = tmp_path / "big.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("data/big.bin", b"x" * 1000)  # 1000 uncompressed > 10 cap
        dest = tmp_path / "out"

        result = unzip_file(str(archive), str(dest))

        assert "error" in result
        assert "size" in result["error"].lower() or "cap" in result["error"].lower()
        assert not (dest / "data" / "big.bin").exists()


class TestValidExtraction:
    """Safe archives still extract correctly (zip + tar.gz)."""

    def test_valid_zip_extracts(self, tmp_path: Path) -> None:
        archive = tmp_path / "ok.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("data/x.csv", "a,b\n1,2\n")
        dest = tmp_path / "out"

        result = unzip_file(str(archive), str(dest))

        assert "error" not in result, result
        extracted = Path(result["extracted_to"]) / "data" / "x.csv"
        assert extracted.read_text() == "a,b\n1,2\n"

    def test_targz_previews_and_extracts(self, tmp_path: Path) -> None:
        archive = tmp_path / "dump.tar.gz"
        payload = b"col\n42\n"
        info = tarfile.TarInfo(name="data/y.csv")
        info.size = len(payload)
        with tarfile.open(archive, "w:gz") as tf:
            tf.addfile(info, io.BytesIO(payload))

        # Preview honors the advertised tar.gz support (was zip-only before #147).
        preview = preview_archive(str(archive))
        assert preview.error is None, preview.error
        assert any("y.csv" in e.get("path", "") for e in preview.entries)

        # And it extracts.
        dest = tmp_path / "out"
        result = unzip_file(str(archive), str(dest))
        assert "error" not in result, result
        assert (Path(result["extracted_to"]) / "data" / "y.csv").read_bytes() == payload


def test_corrupt_archive_returns_error_not_raises(tmp_path: Path) -> None:
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not a real archive")
    result = unzip_file(str(archive), str(tmp_path / "out"))
    assert "error" in result  # clean error, no exception


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
