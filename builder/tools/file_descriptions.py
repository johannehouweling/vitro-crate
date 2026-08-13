"""Describe payload files from their content, for the files that have no description.

"Add a description for metabolism_assay_metadata.xlsx, 20231213_BCA SK uptake
23-11.xlsx, 3.2 Protocol transporter assay radioactive T3 T4_EN.docx and 40
others" is the largest single action in a real report, and it is one a model can
actually do: the files are in the crate, their content is readable, and a
sentence saying what a spreadsheet holds is prose, not an identifier.

**D5 is not in the way, and it is worth being precise about why.** D5 governs
IDENTIFIERS — an accession, a CASRN, an ORCID — which may only come from an
authoritative lookup, because a plausible-looking wrong one is indistinguishable
from a right one and points at the wrong thing forever. A description is the same
kind of value as the study description, the protocol names and the process names
the model already writes today. It is recorded with the same ``source="llm"``
provenance, so the crate's own completion record says which sentences a model
wrote.

**The content is the whole point.** Describing a file from its NAME is guessing,
and a guess written into ``description`` reads exactly like a curator's sentence
once it is in the crate. So this module's central rule is that a file whose
content could not be read is never sent to the model at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from builder.state import CrateState, Entity

logger = logging.getLogger(__name__)

# How much of each file the model is shown. Enough for sheet names, a header row
# and a few data rows — which is what "what does this file contain?" turns on —
# without paying for the whole payload.
PREVIEW_LIMIT = 1200

# How many files one call describes. Bounded because the leaf is length-checked:
# a batch big enough to strain the model's ordering is a batch whose descriptions
# risk landing on the wrong files, and the check would then throw the whole batch
# away.
BATCH_SIZE = 12


def _needs_description(entity: Entity) -> bool:
    return not str(entity.fields.get("description") or "").strip()


def _preview_for(state: CrateState, entity: Entity) -> str:
    """A readable content sample for *entity*, or "" if there isn't one.

    Goes through ``document_discovery._safe_preview``, which contains the path
    against the session's approved roots before reading — so this cannot be
    talked into reading a file outside the scan root by a crafted ``dest_path``.
    """
    from builder.tools._crate_mapping import _file_source
    from builder.tools.document_discovery import _safe_preview

    source = _file_source(entity, state.metadata.input_path)
    if not source:
        return ""
    # The session's approved scan roots, exactly as the ReAct loop reads them
    # (`agent_loop` uses the same attribute), plus the configured input path.
    # Fail-closed: with no approved root, nothing is read at all — a description
    # is enrichment and never a reason to widen filesystem access (#197).
    roots = {
        str(Path(r).resolve())
        for r in (getattr(state, "approved_scan_roots", None) or set())
        if r
    }
    if state.metadata.input_path:
        roots.add(str(Path(state.metadata.input_path).resolve()))
    if not roots:
        return ""
    # Content first — the actual rows of a CSV are the best evidence of what it
    # holds. Then the file-type summary, because `mode="content"` returns NOTHING
    # for a binary format: measured against the real corpus, every .xlsx and
    # .docx came back 0 characters, which is most of a deposit. The summary gives
    # sheet names, column headers and sample paragraphs — still the file's own
    # content, just the part that survives not being plain text.
    preview = _safe_preview(str(source), roots, PREVIEW_LIMIT)
    if preview.strip():
        return preview
    return _safe_preview(str(source), roots, PREVIEW_LIMIT, mode="summary")


def describe_payload_files(
    state: CrateState,
    *,
    describe_fn: Any = None,
    limit: int | None = None,
) -> list[tuple[str, str]]:
    """Fill ``description`` on File entities that have none, from their content.

    Returns ``[(entity_id, description)]`` for what was written.

    Every guard here exists because the failure mode is a plausible sentence
    about the wrong thing:

    * a file whose content could NOT be read is skipped entirely, never described
      from its name;
    * an entity that already has a description is left alone — a curator's
      sentence is not improved by replacing it;
    * an empty string back from the model is a decline, and nothing is written;
    * a batch whose returned count does not match is discarded whole, because
      the descriptions are matched to files by POSITION and a short list would
      silently shift every sentence onto the wrong file.

    Args:
        state: The crate state; File entities are read and updated in place.
        describe_fn: The leaf to call. Injected so tests drive this without a
            provider, and so a caller can supply model overrides.
        limit: Stop after this many files. ``None`` means all of them.

    Returns:
        The ``(entity_id, description)`` pairs written, in the order written.
    """
    if describe_fn is None:
        from builder.agents.pipeline.leaves import describe_files as describe_fn

    candidates: list[tuple[Entity, str]] = []
    for entity in state.list_entities("File"):
        if not _needs_description(entity):
            continue
        preview = _preview_for(state, entity)
        if not preview.strip():
            # No readable content. This is the case the module exists to refuse:
            # the filename alone is not evidence of what is inside.
            logger.debug(
                "No readable preview for %s; leaving it undescribed", entity.entity_id
            )
            continue
        candidates.append((entity, preview))
        if limit is not None and len(candidates) >= limit:
            break

    written: list[tuple[str, str]] = []
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        payload = [
            {"name": str(e.fields.get("name") or e.entity_id), "preview": p} for e, p in batch
        ]
        try:
            descriptions = describe_fn(payload)
        except Exception:  # noqa: BLE001 — enrichment must never sink a build
            logger.warning("File description call failed; skipping batch", exc_info=True)
            continue
        if not isinstance(descriptions, list) or len(descriptions) != len(batch):
            logger.warning(
                "Got %s descriptions for %d files; discarding the batch rather than "
                "risking a description on the wrong file",
                len(descriptions) if isinstance(descriptions, list) else "no",
                len(batch),
            )
            continue
        for (entity, _preview), description in zip(batch, descriptions):
            text = str(description or "").strip()
            if not text:
                # The model declined. That is a correct answer here.
                continue
            entity.set_fields_from_dict({"description": text}, source="llm")
            written.append((entity.entity_id, text))

    if written:
        logger.info("Described %d payload file(s) from their content", len(written))
    return written
