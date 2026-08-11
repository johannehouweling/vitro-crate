"""Vocabulary the crate cites is not vocabulary the crate must describe.

roc-validator already holds this position: every base shape carries a SPARQL
target commented "Exclude entities with non-IRI identifiers or those from
specific namespaces", filtering schema.org, w3.org, purl.org, bioschemas.org,
w3id.org/ro/crate and urn: — 34 FILTER clauses across seven files, at SHOULD and
MUST severity alike. It does not want a self-contained graph.

That list is the one a WORKFLOW crate needs. It never grew the ontology hosts a
life-science crate cites, so a real crate collected 87 findings from ~20 IRIs
maintained by other people. `_patch_cited_vocabulary_exemption` adds ours to the
list already there, by wrapping the shape loader — nothing on disk is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from profiles.validator import (
    _CITED_VOCABULARY_NAMESPACES,
    _VOCABULARY_FILTER_ANCHOR,
    DEFAULT_PROFILES_PATH,
    _exempt_cited_vocabulary,
    _patch_cited_vocabulary_exemption,
)


def _shapes_with_the_exemption() -> list[Path]:
    base = Path(DEFAULT_PROFILES_PATH) / "ro-crate"
    return [
        p
        for p in sorted(base.rglob("*.ttl"))
        if _VOCABULARY_FILTER_ANCHOR in p.read_text(encoding="utf-8")
    ]


class TestTheInjection:
    def test_it_found_the_upstream_exemption_blocks(self):
        """If upstream restructures these shapes, this is what notices."""
        assert _shapes_with_the_exemption(), (
            "no shape carries the vocabulary exemption anchor any more — "
            "roc-validator changed shape and the patch needs revisiting"
        )

    @pytest.mark.parametrize("namespace", _CITED_VOCABULARY_NAMESPACES)
    def test_every_cited_namespace_is_added(self, namespace):
        for shape in _shapes_with_the_exemption():
            patched = _exempt_cited_vocabulary(shape.read_text(encoding="utf-8"))
            assert namespace in patched

    def test_the_upstream_filters_survive(self):
        """We extend their list; we never replace it."""
        for shape in _shapes_with_the_exemption():
            patched = _exempt_cited_vocabulary(shape.read_text(encoding="utf-8"))
            for original in (
                "http://schema.org/",
                "https://bioschemas.org/",
                "http://purl.org/",
                "http://www.w3.org/",
            ):
                assert original in patched

    def test_it_is_idempotent(self):
        once = _exempt_cited_vocabulary(_shapes_with_the_exemption()[0].read_text())
        assert _exempt_cited_vocabulary(once) == once

    def test_the_result_is_still_valid_turtle(self):
        """A patched shape has to parse, or every check in that file vanishes."""
        rdflib = pytest.importorskip("rdflib")
        for shape in _shapes_with_the_exemption():
            patched = _exempt_cited_vocabulary(shape.read_text(encoding="utf-8"))
            rdflib.Graph().parse(data=patched, format="turtle")


class TestNothingOnDiskIsTouched:
    """The first version rewrote the .ttl files, and that was worse than it sounds.

    uv hardlinks packages from a shared cache — `st_nlink` was 11 here — so
    writing a shape edited the copy every environment on the machine shares,
    including the cache uv installs from. A patch meant for one venv silently
    became a patch to the machine, and reinstalling could not undo it because the
    cache was what got edited.
    """

    def test_the_shapes_are_unmodified(self):
        for shape in _shapes_with_the_exemption():
            text = shape.read_text(encoding="utf-8")
            for namespace in _CITED_VOCABULARY_NAMESPACES:
                assert namespace not in text, (
                    f"{shape.name} was written to on disk — the patch belongs in memory"
                )

    def test_the_shapes_are_still_shared_with_other_environments(self):
        """Proof we did not privatise a file: the hardlinks are intact."""
        assert any(shape.stat().st_nlink > 1 for shape in _shapes_with_the_exemption()), (
            "expected uv's hardlinked install; if this fails the environment was "
            "built differently and the on-disk assertions above are weaker"
        )


class TestTheLoaderWrapper:
    def test_the_loader_is_wrapped(self):
        from rocrate_validator.requirements.shacl import utils

        assert getattr(utils.load_shapes_from_file, "_vitro_vocabulary_patch", False)

    def test_re_running_does_not_stack_wrappers(self):
        from rocrate_validator.requirements.shacl import utils

        before = utils.load_shapes_from_file
        _patch_cited_vocabulary_exemption()
        _patch_cited_vocabulary_exemption()
        assert utils.load_shapes_from_file is before


class TestTheBoundary:
    def test_described_aop_entities_are_not_exempted(self):
        """The events we DO describe must keep answering the same checks.

        `materialize_aop_subgraph` fetches https://aopwiki.org/events/2266, names
        it and puts it in the graph — it is ours, and #512 gave it a schema.org
        type precisely because the check was right to ask. Exempting the whole
        aopwiki.org host would have silenced that instead of fixing it.
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
