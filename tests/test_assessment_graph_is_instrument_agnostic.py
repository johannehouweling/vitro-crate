"""``assessment_graph`` is the shared primitives module; it states no instrument's sums.

Its own docstring says why it exists: the tri-state verdict shape lives here "rather
than in one instrument's module" so the axes cannot drift apart. Prose describing how
*one* spreadsheet aggregates has no business here — it is out of place whether it is
true or false, and a copy left behind here propagates to every scorer that imports it.

That is what this guard checks, and it is a **location** rule, not a truth check: a
general "docstring asserts X, code does Y" checker is not buildable offline (free prose
has no machine-readable projection, and #117 rules out the LLM that would be needed).
Naming the instruments is fine — describing their arithmetic is not.

It has a specific failure to its name: the claim that an unanswered indicator "leaves
the denominator" survived here through #704, which corrected it in the four other
places it appeared, precisely because nobody looks for one instrument's arithmetic in
the module that belongs to none of them (#710).
"""

from __future__ import annotations

import ast
import re
import tokenize
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "builder" / "tools" / "assessment_graph.py"

# Each pattern names a way one instrument counts. Reasons are the assertion message.
ARITHMETIC = {
    r"denominat|numerat": "how an instrument divides is that instrument's business",
    r"\bCOUNTA?\b|COUNTIFS": "a spreadsheet function is one workbook's implementation",
    r"blank cell|workbook": "the shape of one instrument's answer sheet",
    r"\b[HIJPQ]\d{1,3}\b": "a worksheet cell reference",
    r"percentage|% complete": "an aggregate, which every instrument computes its own way",
}


def _prose() -> list[tuple[int, str]]:
    """Every docstring and comment in the module, as (line number, text)."""
    source = MODULE.read_text(encoding="utf-8")
    found = [
        (getattr(node, "lineno", 1), ast.get_docstring(node) or "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
    ]
    with MODULE.open("rb") as handle:
        found += [
            (token.start[0], token.string)
            for token in tokenize.tokenize(handle.readline)
            if token.type == tokenize.COMMENT
        ]
    return found


def test_the_shared_primitives_state_no_instrument_s_arithmetic() -> None:
    offences = [
        f"{MODULE.name}:{line}: {reason}\n    {text.strip()[:200]}"
        for line, text in _prose()
        for pattern, reason in ARITHMETIC.items()
        if re.search(pattern, text, re.IGNORECASE)
    ]
    assert not offences, (
        "Prose in the module every instrument imports describes one instrument's "
        "aggregation. State it in that instrument's own module:\n\n"
        + "\n".join(offences)
    )
