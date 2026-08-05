"""Deterministic, bounded discovery of scientific documentation.

Discovery is deliberately independent of either agent architecture.  It uses
the scanner inventory, reads only small previews inside approved roots, and
returns ranked evidence for ReAct and the deterministic pipeline to consume.
Filenames are signals, never requirements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from builder.state import FileClassification

_DOCUMENT_SUFFIXES = {
    ".csv", ".doc", ".docx", ".html", ".json", ".md", ".pdf", ".rst",
    ".txt", ".xml", ".xls", ".xlsx", ".yaml", ".yml",
}
_TEXT_MIMES = ("text/", "application/json", "application/xml", "application/pdf")
_MAX_PREVIEW_CHARS = 3_000
_MAX_CONTEXT_CHARS = 12_000
_ROLE_TERMS: dict[str, tuple[str, ...]] = {
    "publication": ("doi", "pmid", "journal", "abstract", "references", "supplementary"),
    "assay_protocol": ("assay", "test method", "experimental procedure", "endpoint", "readout"),
    "sop": ("sop", "standard operating procedure", "work instruction", "laboratory procedure"),
    "study_plan": ("study design", "study plan", "objective", "investigation", "experimental design"),
    "metadata": ("metadata", "data dictionary", "sample sheet", "plate map", "condition table"),
    "analysis_method": ("data analysis", "analysis method", "statistics", "normalization", "pipeline"),
}
_FILENAME_TERMS = {
    "publication": ("publication", "paper", "article", "doi", "pmid"),
    "assay_protocol": ("assay", "protocol", "method"),
    "sop": ("sop", "standard_operating", "work_instruction"),
    "study_plan": ("study", "investigation", "experiment", "design"),
    "metadata": ("metadata", "sample", "plate", "dictionary", "condition"),
    "analysis_method": ("analysis", "pipeline", "statistics"),
}


@dataclass
class DocumentationCandidate:
    """A compact, ranked documentation candidate."""

    path: str
    filename: str
    relative_path: str
    role: str
    score: float
    reasons: list[str] = field(default_factory=list)
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "relative_path": self.relative_path,
            "role": self.role,
            "score": self.score,
            "reasons": list(self.reasons),
            "preview": self.preview,
        }


def _relative(path: str, root: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return Path(path).name


def _is_candidate(file: FileClassification) -> bool:
    suffix = Path(file.filename).suffix.lower()
    return suffix in _DOCUMENT_SUFFIXES or file.mime_type.startswith(_TEXT_MIMES)


def _role_and_signals(filename: str, preview: str) -> tuple[str, list[str], float]:
    name = filename.casefold().replace("-", "_")
    text = f"{name}\n{preview.casefold()}"
    matches: dict[str, int] = {
        role: sum(term in text for term in terms)
        for role, terms in _ROLE_TERMS.items()
    }
    role, count = max(matches.items(), key=lambda item: (item[1], item[0]))
    reasons: list[str] = []
    score = 0.0
    if count:
        score += min(0.45, count * 0.09)
        reasons.append(f"content signals: {count} {role.replace('_', ' ')} term(s)")
    filename_matches = sum(term in name for term in _FILENAME_TERMS[role])
    if filename_matches:
        score += min(0.2, filename_matches * 0.08)
        reasons.append("filename supports role")
    if len(re.findall(r"[.!?]\s+", preview)) >= 2:
        score += 0.12
        reasons.append("prose-like preview")
    if preview.count("\n") >= 2:
        score += 0.08
        reasons.append("structured preview")
    if not count:
        role = "other_document"
    return role, reasons, score


def _safe_preview(path: str, approved_roots: set[str], limit: int) -> str:
    """Read a small preview only after approved-root containment succeeds."""
    from builder.tools.scanner import _contain, read_file_sample

    contained = _contain(path, approved_roots)
    if contained is None or not contained.is_file():
        return ""
    try:
        value = read_file_sample(str(contained), lines=40, mode="content")
    except (OSError, RuntimeError, ValueError):
        return ""
    if not value:
        return ""
    return str(value)[:limit]


def discover_documents(
    files: list[FileClassification],
    *,
    input_root: str,
    approved_roots: set[str],
    max_candidates: int = 20,
    max_context_chars: int = _MAX_CONTEXT_CHARS,
) -> list[DocumentationCandidate]:
    """Screen and rank readable scientific documentation deterministically.

    Root and immediate-child files receive a location bonus, but deeper files
    remain eligible: assay SOPs and publications are often nested.  The result
    is stable for equal scores and contains only bounded previews.
    """
    ranked: list[DocumentationCandidate] = []
    for index, file in enumerate(files):
        if not _is_candidate(file):
            continue
        relative = _relative(file.path, input_root)
        depth = len(Path(relative).parts) - 1
        preview = _safe_preview(file.path, approved_roots, _MAX_PREVIEW_CHARS)
        role, reasons, score = _role_and_signals(file.filename, preview)
        location_bonus = 0.24 if depth == 0 else 0.16 if depth == 1 else max(0.0, 0.08 - depth * 0.01)
        score += location_bonus
        reasons.append(f"directory depth {depth}")
        if preview:
            score += 0.12
        ranked.append(
            DocumentationCandidate(
                path=file.path,
                filename=file.filename,
                relative_path=relative,
                role=role,
                score=round(score, 4),
                reasons=reasons,
                preview=preview,
            )
        )
        if index >= len(files):  # pragma: no cover - defensive, keeps loop explicit
            break
    ranked.sort(key=lambda item: (-item.score, item.relative_path.casefold()))
    return ranked[:max_candidates]


def format_document_context(
    candidates: list[DocumentationCandidate], *, max_chars: int = _MAX_CONTEXT_CHARS
) -> str:
    """Format ranked candidates as bounded, role-labelled agent context."""
    parts: list[str] = []
    used = 0
    for candidate in candidates:
        header = f"[{candidate.role}] {candidate.relative_path}\n"
        body = candidate.preview.strip() or "(preview unavailable; read this file if relevant)"
        block = header + body
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = block[:remaining].rstrip() + " […]"
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts)