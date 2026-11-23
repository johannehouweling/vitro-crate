#!/usr/bin/env python
"""Refresh the vendored identifier-namespace records from the Bioregistry.

The ISA-Tox shapes check the form of an identifier: an ORCID iD, a ROR ID, a
Cellosaurus accession, a CAS number, an ontology term IRI. What a well-formed one
looks like is *published*: the Bioregistry (https://bioregistry.io) records, per
prefix, the pattern of the local identifier and the URI format it expands to. So
the patterns in the shapes are never typed from memory. This script reads the
records and writes ``profiles/vocabulary/identifier_namespaces.json``;
``profiles/identifier_namespaces.py`` derives the patterns from that file, and
``tests/test_identifier_namespace_shapes.py`` holds every ``sh:pattern`` in the
shapes to them.

The records come from the ``bioregistry`` package rather than the website, for
two reasons. A release is a version that can be named, where the site changes
daily. And a bare record often states no pattern of its own: the Bioregistry
takes it from a registry it maps to (MIRIAM, for ORCID and ROR), and only the
package's own resolvers apply that order.

The package is not a dependency of this project. Like
``scripts/refresh_type_vocabulary.py`` this is run by hand, and nothing it
touches runs at build or validation time (#117)::

    uv run --with bioregistry==0.15.15 python scripts/refresh_identifier_namespaces.py

Pass another version to move the pin; the version read is recorded in the file.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

# The prefixes the shapes name. Adding one here is the first step of checking a
# new namespace; the shape and its `rdfs:seeAlso` are the second.
PREFIXES: tuple[str, ...] = (
    # people and organisations
    "orcid",
    "ror",
    # adverse outcome pathways
    "aop",
    "aop.events",
    # cell lines
    "cellosaurus",
    "rrid",
    # chemicals
    "cas",
    "pubchem.compound",
    "comptox",
    "inchikey",
    # publications
    "doi",
    "pubmed",
    # ontologies whose terms the profile and the builder use
    "bao",
    "chebi",
    "cl",
    "efo",
    "go",
    "iao",
    "msio",
    "ncbitaxon",
    "ncit",
    "obi",
    "pato",
    "uberon",
    "uo",
)

SOURCE: dict[str, str] = {
    "name": "Bioregistry",
    "url": "https://bioregistry.io",
    "package": "bioregistry",
    "license": (
        "CC0-1.0 for the records the Bioregistry curates; records it aggregates "
        "from other registries keep the licence of the registry they came from"
    ),
    "citation": "https://doi.org/10.1038/s41597-022-01807-3",
}

_OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "profiles"
    / "vocabulary"
    / "identifier_namespaces.json"
)

# SHACL's sh:pattern is the SPARQL REGEX dialect, which has no lookaround, no
# inline flags and no non-capturing groups. A pattern that uses one would mean
# something else, or nothing, in a conforming engine.
_OUTSIDE_SHACL_REGEX = "(?"


def _record(prefix: str) -> dict[str, str]:
    import bioregistry  # ty: ignore[unresolved-import]

    resource = bioregistry.get_resource(prefix)
    if resource is None:
        raise SystemExit(f"the Bioregistry has no record for {prefix!r}")
    pattern = resource.get_pattern()
    uri_format = resource.get_uri_format()
    if not pattern:
        raise SystemExit(f"{prefix}: the record states no pattern")
    if not uri_format or uri_format.count("$1") != 1:
        raise SystemExit(f"{prefix}: no URI format with one $1, got {uri_format!r}")
    if _OUTSIDE_SHACL_REGEX in pattern:
        raise SystemExit(f"{prefix}: pattern {pattern!r} is outside the SHACL regex dialect")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise SystemExit(f"{prefix}: pattern {pattern!r} does not compile ({exc})") from exc
    # The registry's own example, which a message or a fix hint can show in place
    # of a description somebody typed. One that its own pattern refuses would send
    # a reader to an identifier the shapes then report.
    example = resource.get_example()
    if not example or not re.search(pattern, example):
        raise SystemExit(f"{prefix}: no example its pattern accepts, got {example!r}")
    record = {
        "name": resource.get_name() or prefix,
        "pattern": pattern,
        "uri_format": uri_format,
        "example": example,
        "record": f"https://bioregistry.io/registry/{prefix}",
    }
    # A "banana" is a prefix some sources embed in the identifier itself: a
    # Cellosaurus accession is written CVCL_0440, an RRID is written RRID:CVCL_0440.
    if banana := resource.get_banana():
        record["banana"] = banana
        record["banana_peel"] = resource.get_banana_peel() or ":"
    return record


def main() -> int:
    from importlib.metadata import version

    records = {prefix: _record(prefix) for prefix in PREFIXES}
    for prefix, record in records.items():
        print(f"  {prefix:18} {record['pattern']:42} {record['uri_format']}")

    # Carry the `decisions` section through untouched. It holds the namespaces the
    # Bioregistry has no record for, written here deliberately and justified in
    # place; a refresh reports what the registry says and has no business deleting
    # a decision nobody revisited.
    decisions: dict[str, Any] = {}
    if _OUTPUT.exists():
        try:
            decisions = json.loads(_OUTPUT.read_text(encoding="utf-8")).get("decisions") or {}
        except ValueError:
            print("  ! existing file unreadable: decisions not preserved", file=sys.stderr)

    payload: dict[str, Any] = {
        "source": {
            **SOURCE,
            # The package's own version; `bioregistry.get_version` is a resource's.
            "version": version("bioregistry"),
            "retrieved": date.today().isoformat(),
        },
        "prefixes": records,
    }
    if decisions:
        payload["decisions"] = decisions
        print(f"  (kept {len(decisions)} project decision(s) unchanged)")
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nwrote {_OUTPUT.relative_to(Path.cwd())} ({len(records)} prefixes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
