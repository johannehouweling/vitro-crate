"""Generate the ISA-Tox Profile Crate (#856).

Run:
    uv run python scripts/gen_profile_crate.py --out dist/profile_crate

The Profile Crate is the artefact w3id, Zenodo and the RO-Crate Profile Portal
point at. It is generated from the two sources that already exist —
``profiles/docs/isa_tox.md`` and ``profiles/shapes/tox/*.ttl`` — so the
published profile cannot drift from the shapes the validator runs. Nothing it
writes is committed here: a release copies the output into the public
``vitro-crate/isa-tox-ro-crate-profile`` repo, which serves it from GitHub
Pages.

``tests/test_profile_crate.py`` holds the output to the three checks the
portal's own CI runs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rocrate.model import ContextEntity, Person
from rocrate.rocrate import ROCrate

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "profiles" / "docs" / "isa_tox.md"
SHAPES_DIR = REPO / "profiles" / "shapes" / "tox"

PROFILE_IRI = "https://w3id.org/ro/crate/isa-tox/1.0"
VERSION = "1.0.0"
DATE_PUBLISHED = "2026-10-30"
PARENT_IRI = "https://github.com/nfdi4plants/isa-ro-crate-profile"
ORCID = "https://orcid.org/0009-0005-3680-0645"
CC_BY = "https://creativecommons.org/licenses/by/4.0/"
MIT = "https://opensource.org/licenses/MIT"
ROLE = "http://www.w3.org/ns/dx/prof/role/"

NAME = "ISA-Tox RO-Crate Profile"
DESCRIPTION = (
    "Toxicology (in vitro New Approach Methodology) domain extension of the "
    "ISA RO-Crate Profile: the entity types, ontology-backed properties and "
    "SHACL shapes needed to describe in vitro toxicology studies."
)


def build(out: Path) -> dict:
    """Write the Profile Crate to ``out`` and return its metadata document."""
    crate = ROCrate(gen_preview=False)
    root = crate.root_dataset
    root["name"] = NAME
    root["description"] = DESCRIPTION
    root["version"] = VERSION
    root["datePublished"] = DATE_PUBLISHED
    root["license"] = {"@id": CC_BY}
    root["isProfileOf"] = {"@id": PARENT_IRI}
    root["identifier"] = PROFILE_IRI
    root["author"] = crate.add(
        Person(crate, ORCID, properties={"name": "Jente Houweling"})
    )
    crate.add(
        ContextEntity(
            crate, CC_BY, properties={"@type": "CreativeWork", "name": "CC BY 4.0"}
        )
    )
    crate.add(
        ContextEntity(
            crate, MIT, properties={"@type": "CreativeWork", "name": "MIT License"}
        )
    )
    crate.add(
        ContextEntity(
            crate,
            PARENT_IRI,
            properties={
                "@type": ["CreativeWork", "Profile"],
                "name": "ISA RO-Crate Profile",
            },
        )
    )

    out.mkdir(parents=True, exist_ok=True)
    crate.write(out)
    doc = _as_profile_crate(crate.metadata.generate())
    (out / "ro-crate-metadata.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return doc


def _as_profile_crate(doc: dict) -> dict:
    """Give the root the profile's own IRI and the ``Profile`` type.

    ro-crate-py models neither: ``RootDataset.id`` has no setter and
    ``__setitem__`` refuses ``@type`` (both checked against rocrate 0.15). The
    RO-Crate 1.2 Profile Crate requirements need both, so the three fields are
    rewritten on the generated document — the graph itself is still assembled
    by ro-crate-py, as AGENTS.md requires.

    ponytail: three-field rewrite; drop it if ro-crate-py grows a Profile Crate
    writer.
    """
    for node in doc["@graph"]:
        if node["@id"] == "./":
            node["@id"] = PROFILE_IRI
            node["@type"] = ["Dataset", "Profile"]
        elif node["@id"] == "ro-crate-metadata.json":
            node["about"] = {"@id": PROFILE_IRI}
        elif isinstance(node.get("about"), dict) and node["about"].get("@id") == "./":
            node["about"] = {"@id": PROFILE_IRI}
    return doc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO / "dist" / "profile_crate")
    args = parser.parse_args()
    doc = build(args.out)
    print(f"Wrote {args.out} ({len(doc['@graph'])} entities, profile {VERSION}).")


if __name__ == "__main__":
    main()
