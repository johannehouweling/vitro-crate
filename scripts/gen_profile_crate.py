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
import tempfile
from pathlib import Path

from rocrate.model import ContextEntity, Person
from rocrate.rocrate import ROCrate

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "profiles" / "docs" / "isa_tox.md"
FIGURE = REPO / "profiles" / "docs" / "figures" / "isa-tox-model.svg"
SHAPES_DIR = REPO / "profiles" / "shapes" / "tox"

PROFILE_IRI = "https://w3id.org/ro/crate/isa-tox/1.0"
VERSION = "1.0.0"
DATE_PUBLISHED = "2026-10-30"
PARENT_IRI = "https://github.com/nfdi4plants/isa-ro-crate-profile"
RO_CRATE = "https://w3id.org/ro/crate/1.2"
PAGES = "https://vitro-crate.github.io/isa-tox-ro-crate-profile/1.0/"
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
# The RO-Crate Profile Portal facets its listing on these, and keeps a
# `scripts/gaps.ttl` of hand-written keywords for the profiles that ship none.
KEYWORDS = [
    "RO-Crate",
    "in vitro toxicology",
    "New Approach Methodology",
    "ISA",
    "SHACL",
    "FAIR",
    "Adverse Outcome Pathway",
    "Bioschemas",
    "toxicology",
]


FRONT_MATTER = f"---\ntitle: {NAME}\n---\n\n"


def _jekyll_page() -> str:
    """The specification Markdown, front-matter'd for GitHub Pages.

    The published profiles serve their specification this way — Markdown in
    the repository, rendered by the Jekyll build GitHub Pages runs, with a
    supported theme and no stylesheet of their own (the RO-Crate team's own
    Workflow Run Crate profiles and our parent ISA profile both use
    `jekyll-theme-slate`, set in the profile repository's `_config.yml`).
    The front matter names the page: GitHub Pages bundles
    `jekyll-optional-front-matter`, so the file renders without it, but then
    the theme titles the page from its filename.

    The spec's one figure is referenced relatively and resolves both on GitHub
    and under the rendered page, where :func:`build` writes it alongside.
    """
    return FRONT_MATTER + SPEC.read_text(encoding="utf-8")


def build(out: Path) -> dict:
    """Write the Profile Crate to ``out`` and return its metadata document."""
    crate = ROCrate(gen_preview=False)
    root = crate.root_dataset
    root["name"] = NAME
    root["description"] = DESCRIPTION
    root["version"] = VERSION
    root["datePublished"] = DATE_PUBLISHED
    root["license"] = {"@id": CC_BY}
    root["keywords"] = KEYWORDS
    # Most specific first. ISA-Tox layers on ISA, which layers on RO-Crate;
    # peer Profile Crates all name the base specification, and the portal's
    # listing roots its hierarchy there.
    root["isProfileOf"] = [{"@id": PARENT_IRI}, {"@id": RO_CRATE}]
    root["identifier"] = PROFILE_IRI
    # `identifier` is the permalink the profile is cited by; `url` is the
    # address that serves it, which is what a reader can click today.
    root["url"] = PAGES
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
    for iri, label in ((PARENT_IRI, "ISA RO-Crate Profile"), (RO_CRATE, "RO-Crate 1.2")):
        crate.add(
            ContextEntity(
                crate,
                iri,
                properties={"@type": ["CreativeWork", "Profile"], "name": label},
            )
        )

    # Staged outside ``out``: ``crate.write()`` copies every file entity to its
    # destination, and copying a file onto itself raises ``shutil.SameFileError``.
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "index.md"
        staged.write_text(_jekyll_page(), encoding="utf-8")
        crate.add_file(
            staged,
            dest_path="index.md",
            properties={
                "name": f"{NAME} — specification",
                "encodingFormat": "text/markdown",
                "about": {"@id": "./"},
                "license": {"@id": CC_BY},
            },
        )
        # The readable specification is the page Jekyll renders from that
        # Markdown, which exists only once published — so it is named by its
        # absolute URI. A relative ``index.html`` would be a data entity no
        # copy of this crate, the Zenodo archive included, actually contains.
        page = crate.add_file(
            PAGES,
            fetch_remote=False,
            validate_url=False,
            properties={
                "name": f"{NAME} — specification (rendered)",
                "encodingFormat": "text/html",
                "about": {"@id": "./"},
                "license": {"@id": CC_BY},
            },
        )
        crate.add(
            ContextEntity(
                crate,
                "#hasSpecification",
                properties={
                    "@type": "ResourceDescriptor",
                    "name": "Profile specification",
                    "hasRole": {"@id": f"{ROLE}specification"},
                    "hasArtifact": {"@id": page.id},
                },
            )
        )

        shape_ids = []
        for path in sorted(SHAPES_DIR.glob("*.ttl")):
            entity = crate.add_file(
                path,
                dest_path=f"shapes/{path.name}",
                properties={
                    "name": path.stem.replace("_", " "),
                    "encodingFormat": "text/turtle",
                    "license": {"@id": MIT},
                },
            )
            shape_ids.append({"@id": entity.id})
        crate.add(
            ContextEntity(
                crate,
                "#hasSHACL",
                properties={
                    "@type": "ResourceDescriptor",
                    "name": "SHACL shapes",
                    "hasRole": {"@id": f"{ROLE}validation"},
                    "hasArtifact": shape_ids,
                },
            )
        )
        crate.add(
            ContextEntity(
                crate,
                "#hasVocabulary",
                properties={
                    "@type": "ResourceDescriptor",
                    "name": "Extension vocabulary",
                    "hasRole": {"@id": f"{ROLE}vocabulary"},
                    "hasArtifact": {"@id": "shapes/ontology.ttl"},
                },
            )
        )
        crate.add_file(
            FIGURE,
            dest_path=f"figures/{FIGURE.name}",
            properties={
                "name": "The ISA-Tox model of an in vitro NAM study",
                "encodingFormat": "image/svg+xml",
                "license": {"@id": CC_BY},
            },
        )
        root["hasResource"] = [
            {"@id": "#hasSpecification"},
            {"@id": "#hasSHACL"},
            {"@id": "#hasVocabulary"},
        ]

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
