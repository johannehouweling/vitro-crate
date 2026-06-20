"""ARC folder structure scaffolding tool.

scaffold_arc creates the ARC folder tree from the template and sorts
scanned files into the correct ARC buckets. Called after scan_files
and before the agent loop starts.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from builder.state import FileClassification

logger = logging.getLogger(__name__)

# Path to the ARC template directory
ARC_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "arc" / "arc-template"


def scaffold_arc(
    scanned_files: list[FileClassification],
    output_path: str,
) -> dict:
    """Create the ARC folder tree from the template and sort scanned files.

    Copies the ARC template structure to the output directory, then
    places scanned files into the appropriate ARC buckets based on
    their MIME type and filename patterns.

    Args:
        scanned_files: List of FileClassification records from scan_files.
        output_path: Path where the ARC directory should be created.

    Returns:
        Dict with keys:
            success (bool): Whether scaffolding succeeded.
            path (str): The output path used.
            files_sorted (int): Number of files sorted into ARC buckets.
            error (str | None): Error message if success is False.
    """
    output_dir = Path(output_path)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)

        # Copy template structure if available
        if ARC_TEMPLATE_DIR.is_dir():
            _copy_template(ARC_TEMPLATE_DIR, output_dir)
            logger.info("Copied ARC template to %s", output_path)
        else:
            _create_default_arc_structure(output_dir)
            logger.info("Created default ARC structure at %s", output_path)

        # Sort scanned files into ARC buckets
        files_sorted = _sort_files(scanned_files, output_dir)

        logger.info(
            "ARC scaffolded at %s — %d files sorted",
            output_path,
            files_sorted,
        )
        return {
            "success": True,
            "path": output_path,
            "files_sorted": files_sorted,
            "error": None,
        }

    except OSError as e:
        logger.error("Failed to scaffold ARC at %s: %s", output_path, e)
        return {"success": False, "path": output_path, "files_sorted": 0, "error": str(e)}


def _copy_template(template_dir: Path, output_dir: Path) -> None:
    """Copy template contents to output directory, preserving structure."""
    shutil.copytree(
        template_dir,
        output_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("README.md"),
    )

def _create_default_arc_structure(output_dir: Path) -> None:
    """Create a minimal default ARC directory structure."""
    (output_dir / "studies").mkdir(exist_ok=True)
    (output_dir / "assays").mkdir(exist_ok=True)
    (output_dir / "workflows").mkdir(exist_ok=True)
    (output_dir / "runs").mkdir(exist_ok=True)


def _sort_files(
    scanned_files: list[FileClassification],
    output_dir: Path,
) -> int:
    """Sort scanned files into ARC buckets based on MIME type and filename.

    For this initial implementation, files are placed by category:
    - CSV/TSV → assays/<name>/dataset/raw_data/
    - JSON/YAML → assays/<name>/ (metadata)
    - TXT/MD → protocols/
    - Other → assays/<name>/dataset/raw_data/

    Returns the number of files successfully sorted.
    """
    count = 0
    for fc in scanned_files:
        dest = _classify_file_path(fc, output_dir)
        if dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                src_path = Path(fc.path)
                if src_path.exists():
                    shutil.copy2(str(src_path), str(dest))
                    count += 1
            except OSError:
                logger.warning("Could not copy %s to %s", fc.path, dest)
    return count


def _classify_file_path(
    fc: FileClassification,
    output_dir: Path,
) -> Path | None:
    """Determine the destination path for a file based on its MIME type."""
    mime = fc.mime_type
    name = fc.filename.lower()

    if mime in ("text/csv", "text/tab-separated-values"):
        return output_dir / "assays" / "assay_1" / "dataset" / "raw_data" / fc.filename
    elif mime in ("application/json", "application/x-yaml", "text/yaml"):
        return output_dir / "assays" / "assay_1" / fc.filename
    elif mime in ("text/markdown", "text/plain") and (
        "protocol" in name or "readme" in name
    ):
        return output_dir / "protocols" / fc.filename
    elif mime == "text/plain":
        return output_dir / "studies" / "study_1" / "resources" / fc.filename
    else:
        return output_dir / "assays" / "assay_1" / "dataset" / "raw_data" / fc.filename


__all__ = ["scaffold_arc"]