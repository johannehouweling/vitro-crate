"""Vendored vocabulary facts, fetched from the specs rather than written by hand.

The crate types its domain entities with Bioschemas terms, and RO-Crate
RECOMMENDS that every entity also carry a type in the schema.org namespace. The
relation between the two — ``LabProcess rdfs:subClassOf schema:Action`` — is
published by Bioschemas, so this package reads it instead of asserting it.

``scripts/refresh_type_vocabulary.py`` writes ``type_supertypes.json`` from the
Bioschemas specification repository and the schema.org dump, recording the source
URL for each entry. Nothing here falls back to a built-in guess: an absent
mapping leaves a node with only its domain type, which costs a RECOMMENDED
finding, while a wrong one puts a false claim in a scientific record. The first
hand-written attempt got two of three wrong (``CreateAction`` for LabProcess,
which is ``Action``; ``BioChemEntity`` for Sample, which is ``Thing``) — both
plausible, both wrong, and neither detectable by reading the code.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_SUPERTYPES_FILE = Path(__file__).with_name("type_supertypes.json")


@lru_cache(maxsize=1)
def type_supertypes() -> dict[str, str]:
    """Map a domain type name to its schema.org supertype CURIE.

    Merges two sections of the vendored file, and the split is the point:

    ``types``
        Fetched from the specs. Facts, refreshable, not ours to argue with.
    ``decisions``
        Types with NO published alignment, where the project has chosen one
        anyway and said why in a ``rationale``. ``csvw:Column`` is the only one:
        CSVW declares no superclass and no schema.org mapping, so the choice is
        either to make one or to accept the findings.

    Keeping them apart means a reader can always tell which mappings are
    reported and which are asserted — the distinction that matters when someone
    asks where a claim in a published crate came from.

    Returns an empty mapping when the file is missing or unreadable; the build
    then adds no companion types at all, which is the safe direction.
    """
    try:
        payload = json.loads(_SUPERTYPES_FILE.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("No type-supertype vocabulary available (%s); none will be added", exc)
        return {}
    merged: dict[str, str] = {}
    for section in ("types", "decisions"):
        for name, entry in (payload.get(section) or {}).items():
            if isinstance(entry, dict) and entry.get("supertype"):
                merged[name] = str(entry["supertype"])
    return merged


def supertype_provenance() -> str:
    """One line naming where the vocabulary came from and when, for logs/reports."""
    try:
        payload = json.loads(_SUPERTYPES_FILE.read_text())
    except (OSError, ValueError):
        return "type-supertype vocabulary: unavailable"
    return (
        f"type-supertype vocabulary: {len(payload.get('types') or {})} types, "
        f"fetched {payload.get('fetched', 'unknown')}"
    )
