"""A SHACL message says what the data must satisfy, not what to write (#584).

The licence shape used to read:

    "Investigation MUST specify a schema:license
     (use 'ALL RIGHTS RESERVED BY THE AUTHORS' if none available)"

which is a category error with consequences. A ``sh:message`` is the validator
speaking about the data in front of it; prescribing a literal turns it into
content policy — and the literal prescribed here was the most restrictive
licence available, applied by machine to someone else's data, by a tool whose
purpose is FAIR outputs.

Naming the *kind* of value is still welcome, and the rest of the profile already
does it ("InChIKey, CAS, PubChem CID, …", "e.g. Culture Medium"): those describe
what would satisfy the shape without dictating the answer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SHAPES = sorted(Path("profiles/shapes/tox").glob("*.ttl"))

# `use 'X'` / `use "X"` — the shape handing the author a literal to paste.
_PRESCRIBES_A_LITERAL = re.compile(r"\buse\s+['\"][^'\"]+['\"]", re.IGNORECASE)

_MESSAGE = re.compile(r"sh:message\s+\"([^\"]+)\"")


def _messages(path: Path) -> list[str]:
    return _MESSAGE.findall(path.read_text(encoding="utf-8"))


def test_the_profile_has_messages_to_check() -> None:
    # Guards the guard: a glob that matched nothing would pass every test below.
    assert _SHAPES, "no tox shapes found"
    assert sum(len(_messages(p)) for p in _SHAPES) > 10


@pytest.mark.parametrize("shape", _SHAPES, ids=lambda p: p.name)
def test_no_message_prescribes_a_literal_value(shape: Path) -> None:
    offenders = [m for m in _messages(shape) if _PRESCRIBES_A_LITERAL.search(m)]
    assert offenders == [], (
        f"{shape.name}: a validation message tells the author what to write, "
        f"rather than what the data must satisfy: {offenders}"
    )


def test_no_message_names_an_all_rights_reserved_default() -> None:
    """The specific claim worth never making by machine."""
    offenders = [
        (shape.name, m)
        for shape in _SHAPES
        for m in _messages(shape)
        if "all rights reserved" in m.lower()
    ]
    assert offenders == [], offenders


def test_the_licence_shape_still_requires_a_licence() -> None:
    # The point is the wording, not the requirement: dropping the rule would be
    # a different (and much larger) decision — see #540.
    strict = Path("profiles/shapes/tox/9_investigation_strict.ttl").read_text(encoding="utf-8")
    assert "schema:license" in strict
    licence_message = next(m for m in _messages(Path(
        "profiles/shapes/tox/9_investigation_strict.ttl"
    )) if "license" in m)
    assert "MUST" in licence_message
