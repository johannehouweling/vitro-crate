"""The design docs name only links, paths, shapes and code that exist.

A doc that cites a deleted function, test or shape is how it goes stale: the
removed pipeline left docs describing a `pre_assessment` capture and a
`PrimaryCellSample` class that no code had. This reads AGENTS.md, docs/design and
docs/adr and fails on any reference that no longer resolves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = [
    ROOT / "AGENTS.md",
    *sorted(ROOT.glob("docs/design/*.md")),
    *sorted(ROOT.glob("docs/adr/*.md")),
]
# Named on purpose though no code of ours states them: upstream ISA shapes and
# LangGraph's own node name; and a package the docs record as removed.
UPSTREAM = {
    "StudyMustBeReferencedFromInvestigation",
    "AssayMustBeReferencedFromInvestigation",
    "__start__",
}
REMOVED = {"builder/readers/"}
PATH_RE = r"`((?:builder|tests|eval|scripts|profiles|lookups|fair|air|mit|docs)/[^`\s*{}<>:]+)`"


def _prose(doc: Path) -> str:
    return re.sub(r"^```.*?^```", "", doc.read_text(encoding="utf-8"), flags=re.S | re.M)


def _corpus() -> str:
    skip = {".venv", ".git", "sessions", "output", "input", "__pycache__"}
    files = [
        p
        for g in ("**/*.py", "**/*.js", "**/*.ttl", "**/*.yaml", "**/*.json")
        for p in ROOT.glob(g)
        if not skip & set(p.relative_to(ROOT).parts) and p != Path(__file__).resolve()
    ]
    return "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in files)


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _exists(path: str) -> bool:
    stem = path.rstrip(".,;)")
    module = stem.rsplit(".", 1)[0] + ".py"  # `profiles/ontology_iris.PREFIXES`
    return stem in REMOVED or any((ROOT / c).exists() for c in (stem, stem + ".py", module))


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: str(d.relative_to(ROOT)))
def test_links_and_paths_resolve(doc: Path) -> None:
    text, broken = _prose(doc), []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path, _, anchor = target.partition("#")
        dest = (doc.parent / path).resolve() if path else doc
        if not dest.exists():
            broken.append(target)
        elif anchor and dest.is_file():
            if anchor not in {_slug(h) for h in re.findall(r"^#+ (.*)$", dest.read_text(), re.M)}:
                broken.append(target)
    broken += [p for p in re.findall(PATH_RE, text) if not _exists(p)]
    assert not broken, f"{doc.name} names what does not exist: {broken}"


def test_code_and_shape_names_exist() -> None:
    corpus, missing = _corpus(), set()
    for doc in DOCS:
        for token in re.findall(r"`([A-Za-z_][\w.:]*)(?:\(\))?`", _prose(doc)):
            name = token.rsplit(".", 1)[-1].removeprefix("tox:")
            code_like = token.startswith("tox:") or "_" in name.strip("_") or "." in token
            if re.match(r"[A-Z][a-z0-9]+[A-Z]", name):
                code_like = True
            if (
                code_like
                and name not in UPSTREAM
                and not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", corpus)
            ):
                missing.add(f"{doc.name}: {token}")
    assert not missing, f"docs name code or shapes nothing defines: {sorted(missing)}"
