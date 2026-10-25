"""A log record at INFO or above is user copy in an interactive session.

``main.py --interactive`` routes every record at INFO or above to the console
(``ui.install_notice_handler(level=logging.INFO)``), where it prints as a dim ``·``
line. Its text is read by the person running the build, not by a developer:
plain English, no issue numbers, no hedged plurals, no module tags. What only a
developer needs is logged at DEBUG, which ``-v`` shows (#813).

This is a static check over every ``logger.<level>(...)`` call whose message is a
string literal. The arguments filled in at run time are not checked.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ROOT / "main.py",
    *sorted((ROOT / "builder").rglob("*.py")),
    *sorted((ROOT / "profiles").rglob("*.py")),
]
USER_LEVELS = {"info", "warning", "error", "exception", "critical"}

# Each pattern names a way developer shorthand reaches the user; the reason is the message.
SHORTHAND = {
    r"#\d+": "an issue number means nothing to the person running the build",
    r"\w\((?:s|es|ies)\)": "a hedged plural; put the count after a label instead",
    r"^[a-z_]+: ": "a module or function tag",
}


def _user_messages() -> list[tuple[str, int, str]]:
    """Every literal message logged at INFO or above, as (file, line, text)."""
    found: list[tuple[str, int, str]] = []
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in USER_LEVELS
                and isinstance(node.func.value, ast.Name)
                and "log" in node.func.value.id.lower()
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.append((str(path.relative_to(ROOT)), node.lineno, node.args[0].value))
    return found


def test_the_scan_sees_the_logging_calls() -> None:
    """Guards the guard: a scan that matched nothing would pass vacuously."""
    assert len(_user_messages()) > 200


def test_every_user_visible_log_line_is_plain_english() -> None:
    offences = [
        f"{path}:{line}: {reason}\n    {text[:120]!r}"
        for path, line, text in _user_messages()
        for pattern, reason in SHORTHAND.items()
        if re.search(pattern, text)
    ]
    assert not offences, "\n".join(offences)
