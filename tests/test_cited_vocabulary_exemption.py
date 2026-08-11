"""Vocabulary the crate cites is not vocabulary the crate must describe.

roc-validator already holds this position: its base shapes carry a SPARQL target
commented "Exclude entities with non-IRI identifiers or those from specific
namespaces", filtering schema.org, w3.org, purl.org, bioschemas.org,
w3id.org/ro/crate and urn: — 34 FILTER clauses across eight files, at SHOULD and
MUST severity alike. It does not want a self-contained graph.

That list is the one a WORKFLOW crate needs. It never grew the ontology hosts a
toxicology crate cites, so a real crate collected ~60 findings from ~20 IRIs
maintained by OBO, EFO and AOP-Wiki — none of them ours to describe.
`_patch_cited_vocabulary_exemption` adds ours to the list already there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from profiles.validator import (
    _CITED_VOCABULARY_NAMESPACES,
    _VOCABULARY_FILTER_ANCHOR,
    DEFAULT_PROFILES_PATH,
    _patch_cited_vocabulary_exemption,
)


def _shapes_with_the_exemption() -> list[Path]:
    base = Path(DEFAULT_PROFILES_PATH) / "ro-crate" / "1.2"
    return [
        p
        for p in sorted(base.rglob("*.ttl"))
        if _VOCABULARY_FILTER_ANCHOR in p.read_text(encoding="utf-8")
    ]


class TestThePatch:
    def test_it_found_the_upstream_exemption_blocks(self):
        """If upstream restructures these shapes, this is what notices."""
        assert _shapes_with_the_exemption(), (
            "no shape carries the vocabulary exemption anchor any more — "
            "roc-validator changed shape and the patch needs revisiting"
        )

    @pytest.mark.parametrize("namespace", _CITED_VOCABULARY_NAMESPACES)
    def test_every_cited_namespace_is_exempt(self, namespace):
        for shape in _shapes_with_the_exemption():
            assert namespace in shape.read_text(encoding="utf-8")

    def test_it_is_idempotent(self):
        """Import runs it once; a re-run must not stack duplicate filters."""
        before = {p: p.read_text(encoding="utf-8") for p in _shapes_with_the_exemption()}
        _patch_cited_vocabulary_exemption()
        _patch_cited_vocabulary_exemption()
        for path, text in before.items():
            assert path.read_text(encoding="utf-8") == text

    def test_the_upstream_filters_are_left_intact(self):
        """We extend their list; we never replace it."""
        for shape in _shapes_with_the_exemption():
            text = shape.read_text(encoding="utf-8")
            for original in (
                "http://schema.org/",
                "https://bioschemas.org/",
                "http://purl.org/",
                "http://www.w3.org/",
            ):
                assert original in text


class TestTheBoundary:
    def test_described_aop_entities_are_not_exempted(self):
        """The events we DO describe must keep answering the same checks.

        `materialize_aop_subgraph` fetches https://aopwiki.org/events/2266,
        names it and puts it in the graph — it is ours, and #512 gave it a
        schema.org type precisely because the check was right to ask. Exempting
        the whole aopwiki.org host would have silenced that instead of fixing it,
        so the exemption is scoped to the ontology path.
        """
        assert "https://aopwiki.org/ontology/" in _CITED_VOCABULARY_NAMESPACES
        assert "https://aopwiki.org/" not in _CITED_VOCABULARY_NAMESPACES
        assert "https://aopwiki.org/events/" not in _CITED_VOCABULARY_NAMESPACES

    def test_only_term_paths_are_exempt(self):
        """A bare host would exempt anything that registry ever serves."""
        for namespace in _CITED_VOCABULARY_NAMESPACES:
            tail = namespace.split("://", 1)[1]
            assert "/" in tail.rstrip("/"), f"{namespace} is a bare host, not a term path"
            assert namespace.endswith("/")


class TestItStaysInThisEnvironment:
    def test_a_patched_shape_is_not_shared_with_other_venvs(self):
        """uv hardlinks packages from a shared cache — these shapes had 11 links.

        `write_text` truncates in place, so writing a patched shape edits the
        inode every one of those environments shares, including the cache uv
        installs from. A patch meant for this venv becomes a patch to the
        machine, and a reinstall no longer undoes it because the cache is the
        thing that was edited. `_replace_file` swaps the directory entry instead,
        so this environment gets its own copy and every other one keeps theirs.
        """
        for shape in _shapes_with_the_exemption():
            assert shape.stat().st_nlink == 1, (
                f"{shape.name} is still hardlinked into other environments — "
                "patching it would edit theirs too"
            )
