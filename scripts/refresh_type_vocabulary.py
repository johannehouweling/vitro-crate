#!/usr/bin/env python
"""Refresh the vendored type→schema.org-supertype map from the authoritative specs.

The crate types domain entities with Bioschemas terms (``LabProcess``,
``Sample``, ``LabProtocol``) and schema.org's own extension terms
(``MolecularEntity``). RO-Crate RECOMMENDS every entity also carry a type in the
schema.org namespace, so the build needs to know each domain type's schema.org
supertype.

That relation is *published* — ``LabProcess rdfs:subClassOf schema:Action`` is
stated in the Bioschemas specification — so it must never be typed out by hand.
Guessing produced two wrong answers on the first attempt: ``CreateAction``
instead of ``Action`` for LabProcess, and ``BioChemEntity`` instead of ``Thing``
for Sample. Both look plausible and neither is what the spec says.

This script fetches the definitions, extracts ``rdfs:subClassOf``, and writes
``profiles/vocabulary/type_supertypes.json`` with the source URL and the fetch
date next to every entry, so the provenance of each mapping is inspectable. The
build reads that file; it does not fall back to a built-in guess, because a
wrong supertype is worse than an absent one.

Run it when the specs move::

    uv run python scripts/refresh_type_vocabulary.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

# The Bioschemas types this project uses, and where each definition lives. The
# repository is the source the bioschemas.org pages are generated from — the
# website has no machine-readable endpoint for a single type.
_BIOSCHEMAS_REPO = "https://api.github.com/repos/BioSchemas/specifications/contents"
_BIOSCHEMAS_RAW = "https://raw.githubusercontent.com/BioSchemas/specifications/master"
_BIOSCHEMAS_TYPES = ("LabProcess", "LabProtocol", "Sample")

# schema.org publishes everything, including the pending layer where
# MolecularEntity lives, in one document.
_SCHEMA_ORG_DUMP = "https://schema.org/version/latest/schemaorg-current-https.jsonld"
_SCHEMA_ORG_TYPES = ("MolecularEntity", "BioChemEntity")

_OUTPUT = (
    Path(__file__).resolve().parent.parent / "profiles" / "vocabulary" / "type_supertypes.json"
)


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 — fixed hosts
        return response.read()


def _bioschemas_supertype(type_name: str) -> tuple[str, str] | None:
    """Return ``(schema.org supertype, source url)`` for one Bioschemas type.

    Picks the highest version directory available, so a refresh follows the spec
    forward rather than pinning whatever was current when this was written.
    """
    listing = json.loads(_get(f"{_BIOSCHEMAS_REPO}/{type_name}/jsonld/type"))
    files = sorted(
        entry["name"] for entry in listing if entry["name"].endswith((".json", ".jsonld"))
    )
    if not files:
        return None
    url = f"{_BIOSCHEMAS_RAW}/{type_name}/jsonld/type/{files[-1]}"
    document = json.loads(_get(url))
    # The document is an @graph holding the CLASS alongside the properties it
    # introduces. Take the subClassOf from the rdfs:Class node whose label is the
    # type — parsed, not pattern-matched, so a reordered or reformatted spec file
    # still resolves instead of silently yielding nothing.
    for node in document.get("@graph", []):
        if node.get("@type") != "rdfs:Class":
            continue
        if str(node.get("@id", "")).split(":")[-1] != type_name:
            continue
        parent = node.get("rdfs:subClassOf")
        parent_id = parent.get("@id") if isinstance(parent, dict) else None
        if isinstance(parent_id, str) and parent_id.startswith("schema:"):
            return parent_id, url
    return None


def _schema_org_supertypes() -> dict[str, tuple[str, str]]:
    """Return ``{type: (supertype, source url)}`` from the schema.org dump."""
    graph = json.loads(_get(_SCHEMA_ORG_DUMP)).get("@graph", [])
    found: dict[str, tuple[str, str]] = {}
    for node in graph:
        name = str(node.get("@id", "")).split(":")[-1]
        if name not in _SCHEMA_ORG_TYPES:
            continue
        parent = node.get("rdfs:subClassOf")
        parent_id = parent.get("@id") if isinstance(parent, dict) else None
        if parent_id:
            found[name] = (str(parent_id), _SCHEMA_ORG_DUMP)
    return found


def main() -> int:
    entries: dict[str, Any] = {}
    for type_name in _BIOSCHEMAS_TYPES:
        result = _bioschemas_supertype(type_name)
        if result is None:
            print(f"  ! no subClassOf found for {type_name} — skipping", file=sys.stderr)
            continue
        supertype, source = result
        entries[type_name] = {"supertype": supertype, "source": source}
        print(f"  {type_name:16} -> {supertype}")
    for type_name, (supertype, source) in _schema_org_supertypes().items():
        entries[type_name] = {"supertype": supertype, "source": source}
        print(f"  {type_name:16} -> {supertype}")

    if not entries:
        print("nothing fetched; leaving the existing file alone", file=sys.stderr)
        return 1

    # Carry the `decisions` section through untouched. It holds the mappings with
    # no published alignment, chosen deliberately and justified in place; a
    # refresh reports what the specs say and has no business deleting a decision
    # nobody revisited.
    decisions: dict[str, Any] = {}
    if _OUTPUT.exists():
        try:
            decisions = json.loads(_OUTPUT.read_text()).get("decisions") or {}
        except ValueError:
            print("  ! existing file unreadable — decisions not preserved", file=sys.stderr)

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"fetched": date.today().isoformat(), "types": entries}
    if decisions:
        payload["decisions"] = decisions
        print(f"  (kept {len(decisions)} project decision(s) unchanged)")
    _OUTPUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nwrote {_OUTPUT.relative_to(Path.cwd())} ({len(entries)} types)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
