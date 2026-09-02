"""A crate this tool writes must open in the process that wrote it (#544).

ro-crate-py builds its read-time class map from every imported subclass of
``ContextEntity``, keyed by class NAME, and constructs the match positionally::

    cls = pick_type(entity, type_map, fallback=ContextEntity)
    self.add(cls(self, identifier, entity))          # (crate, identifier, properties)

Our modelling classes are written for the WRITE path, where the third parameter
is what the entity is about — ``Sample(crate, id, name)``. Under the reader the
properties dict bound to that parameter and the entity was built with ``name``
set to a whole JSON-LD dict, dying as ``ValueError: no @id in {...}``.

Importing ``profiles.models`` is enough to arm it, so this affected
any code that opens one with ro-crate-py inside this process. External
consumers were never affected — without those imports ``pick_type`` falls back
to plain ``ContextEntity`` — which is why #532's guard reads a crate in a
SUBPROCESS and this one reads it in-process. Two different claims, two tests.
"""

from __future__ import annotations

import inspect

import pytest
from rocrate.model.contextentity import ContextEntity
from rocrate.rocrate import ROCrate

# Imported for the side effect the bug depends on: this is what puts our classes
# into ro-crate-py's type map.
import profiles.models.isa  # noqa: F401
import profiles.models.tox  # noqa: F401
from builder.tools.builder import export_crate
from tests.fixtures.vhps_golden_crates import vhps_fixture_state


def _our_context_entity_classes() -> list[type]:
    """Every ContextEntity subclass of ours the reader can pick, at any depth."""

    def descend(cls: type):
        for sub in cls.__subclasses__():
            yield sub
            yield from descend(sub)

    return [
        c
        for c in dict.fromkeys(descend(ContextEntity))
        if c.__module__.startswith("profiles.")
    ]


class TestEveryModelTheReaderCanPickIsConstructibleByIt:
    """The rule, asserted against the classes rather than one sample crate.

    A crate-based test only covers the ``@type``s that crate happens to contain,
    so a new model class would reintroduce the bug and stay green until some
    fixture grew an instance of it. This enumerates the map the reader actually
    builds.
    """

    def test_the_reader_can_construct_each_one(self) -> None:
        crate = ROCrate()
        broken: list[str] = []
        for cls in _our_context_entity_classes():
            properties = {"@id": "#probe", "@type": "Thing", "name": "probe"}
            try:
                # Exactly the reader's call: positional, properties third.
                entity = cls(crate, "#probe", properties)
            except Exception as exc:  # noqa: BLE001 — collecting, not handling
                broken.append(f"{cls.__name__}: {type(exc).__name__}: {exc}")
                continue
            if entity.id != "#probe":
                broken.append(f"{cls.__name__}: built with id {entity.id!r}")
        assert broken == [], (
            "these classes cannot be built the way ROCrate(path) builds them, so "
            "any crate containing one is unreadable in this process:\n  "
            + "\n  ".join(broken)
        )

    def test_the_write_path_signatures_are_untouched(self) -> None:
        """The control. The fix must not have quietly turned every model into a
        properties bag — the third parameter is still what the entity is about,
        and every existing construction site still means what it says."""
        from profiles.models.isa import Sample

        third = list(inspect.signature(Sample.__init__).parameters)[3]
        assert third == "name", (
            "Sample's write-path signature changed; the reader fix was supposed to "
            "leave it alone"
        )
        crate = ROCrate()
        sample = Sample(crate, "#s1", "MDCK1 cells")
        assert sample["name"] == "MDCK1 cells"


class TestACrateWeWroteOpensInThisProcess:
    """End to end, on a real exported crate — the symptom the issue reported."""

    @pytest.fixture(scope="class")
    def written_crate(self, tmp_path_factory: pytest.TempPathFactory):
        out = tmp_path_factory.mktemp("readable") / "crate"
        state = vhps_fixture_state("S-VHPS21")
        state.metadata.output_path = str(out)
        result = export_crate(state, str(out))
        assert result["success"], result["error"]
        return out

    def test_it_opens(self, written_crate) -> None:
        ROCrate(str(written_crate))

    def test_the_fixture_actually_exercises_the_bug(self, written_crate) -> None:
        """Without an affected @type in it, the test above proves nothing."""
        import json

        graph = json.loads(
            (written_crate / "ro-crate-metadata.json").read_text(encoding="utf-8")
        )["@graph"]
        names = {c.__name__ for c in _our_context_entity_classes()}
        present = {
            t
            for e in graph
            for t in (e.get("@type") if isinstance(e.get("@type"), list) else [e.get("@type")])
            if t in names
        }
        assert present, (
            "this crate carries none of our modelled @types, so opening it does "
            "not exercise the reader path at all"
        )
