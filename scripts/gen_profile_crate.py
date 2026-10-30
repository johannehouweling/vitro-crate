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
import html as html_mod
import json
import re
import tempfile
from pathlib import Path

import markdown
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


_MERMAID_FENCE = re.compile(
    r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.DOTALL
)

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ max-width: 54rem; margin: 2rem auto; padding: 0 1rem;
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #8884; padding: .35rem .6rem; text-align: left;
  vertical-align: top; font-size: .92em; }}
code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
pre {{ overflow-x: auto; padding: .75rem; background: #8881; border-radius: 4px; }}
</style>
</head>
<body>
{body}
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs";
mermaid.initialize({{ startOnLoad: true }});
</script>
</body>
</html>
"""


def _render_spec() -> str:
    """Render ``isa_tox.md`` to a standalone page.

    python-markdown emits Mermaid fences as ``<pre><code>``; Mermaid needs
    ``<div class="mermaid">`` holding the unescaped source, so the fences are
    rewritten. GitHub Pages serves the result as-is (``.nojekyll``).
    """
    body = markdown.markdown(
        SPEC.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "toc", "attr_list"],
    )
    body = _MERMAID_FENCE.sub(
        lambda m: f'<div class="mermaid">{html_mod.unescape(m.group(1))}</div>', body
    )
    return _PAGE.format(title=NAME, body=body)


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

    # Staged outside ``out``: ``crate.write()`` copies every file entity to its
    # destination, and copying a file onto itself raises ``shutil.SameFileError``.
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "index.html"
        staged.write_text(_render_spec(), encoding="utf-8")
        spec = crate.add_file(
            staged,
            dest_path="index.html",
            properties={
                "name": f"{NAME} — specification",
                "encodingFormat": "text/html",
                "about": {"@id": "./"},
                "license": {"@id": CC_BY},
            },
        )
        crate.add(
            ContextEntity(
                crate,
                "#spec",
                properties={
                    "@type": "ResourceDescriptor",
                    "name": "Profile specification",
                    "hasRole": {"@id": f"{ROLE}specification"},
                    "hasArtifact": {"@id": spec.id},
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
                "#shapes",
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
                "#vocabulary",
                properties={
                    "@type": "ResourceDescriptor",
                    "name": "Extension vocabulary",
                    "hasRole": {"@id": f"{ROLE}vocabulary"},
                    "hasArtifact": {"@id": "shapes/ontology.ttl"},
                },
            )
        )
        root["hasResource"] = [
            {"@id": "#spec"},
            {"@id": "#shapes"},
            {"@id": "#vocabulary"},
        ]

        out.mkdir(parents=True, exist_ok=True)
        crate.write(out)

    # After the write: ro-crate-py walks ``out`` while writing, so nothing
    # unlisted goes in before it.
    (out / ".nojekyll").write_text("", encoding="utf-8")
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
