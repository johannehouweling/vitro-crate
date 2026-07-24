"""Unit tests for the shared interactive-UI layer (``builder/agents/ui.py``).

These exercise the arm-neutral render functions offline — no TTY, no live
engine — by rendering each Rich renderable into a recording console and
asserting on the exported plain text. ``flatten_message_content`` is the
#341 leak-fix helper and is tested directly as a pure string function.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

from rich.console import Console, RenderableType

from builder.agents import ui
from builder.state import ValidationReport


def _render(renderable: RenderableType, width: int = 100) -> str:
    """Render a Rich renderable to plain text for assertions."""
    console = Console(width=width, file=io.StringIO(), record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def _fake_engine(
    *,
    session_id: str = "sess-1",
    files: int = 3,
    entities: list[Any] | None = None,
    validation: ValidationReport | None = None,
    mit_score: float | None = 0.75,
) -> SimpleNamespace:
    if entities is None:
        entities = [
            SimpleNamespace(type="Investigation"),
            SimpleNamespace(type="Study"),
            SimpleNamespace(type="Study"),
        ]
    if validation is None:
        validation = ValidationReport(
            base_passed=True, isa_passed=False, tox_passed=False, required_issues=["x"]
        )
    state = SimpleNamespace(
        session_id=session_id,
        scanned_files=list(range(files)),
        validation=validation,
        mit_assessment=SimpleNamespace(overall_score=mit_score),
        list_entities=lambda entity_type=None: list(entities),
    )
    return SimpleNamespace(state=state)


# ---------------------------------------------------------------------------
# flatten_message_content — the #341 raw-message-leak fix
# ---------------------------------------------------------------------------


def test_flatten_plain_string_passthrough() -> None:
    assert ui.flatten_message_content("hello world") == "hello world"


def test_flatten_none_is_empty() -> None:
    assert ui.flatten_message_content(None) == ""


def test_flatten_list_of_text_blocks_joins_text_only() -> None:
    # The exact #341 shape: a list of content-block dicts. Only the `text`
    # survives; annotations/id/phase must NOT leak.
    content = [
        {
            "type": "text",
            "text": "Welcome back. The session has 3 scanned files.",
            "annotations": [],
            "id": "msg_0e84aa37",
            "phase": "final_answer",
        }
    ]
    out = ui.flatten_message_content(content)
    assert out == "Welcome back. The session has 3 scanned files."
    assert "annotations" not in out
    assert "msg_0e84aa37" not in out
    assert "phase" not in out


def test_flatten_drops_non_text_blocks() -> None:
    content = [
        {"type": "text", "text": "before "},
        {"type": "tool_use", "name": "scan", "id": "toolu_1"},
        {"type": "text", "text": "after"},
    ]
    assert ui.flatten_message_content(content) == "before after"


def test_flatten_langchain_block_objects_with_text_attr() -> None:
    content = [SimpleNamespace(type="text", text="obj-text")]
    assert ui.flatten_message_content(content) == "obj-text"


def test_flatten_list_of_plain_strings() -> None:
    assert ui.flatten_message_content(["a", "b"]) == "ab"


# ---------------------------------------------------------------------------
# UiSnapshot / snapshot_from_engine
# ---------------------------------------------------------------------------


def test_snapshot_from_engine_reads_state() -> None:
    snap = ui.snapshot_from_engine(_fake_engine())
    assert snap.session_id == "sess-1"
    assert snap.entity_count == 3
    assert snap.file_count == 3
    assert snap.base_passed is True
    assert snap.isa_passed is False
    assert snap.tox_passed is False
    assert snap.required_issue_count == 1
    assert snap.entity_counts == {"Investigation": 1, "Study": 2}
    assert snap.mit_score == 0.75
    # No profile.ndjson for this fake session → token totals default to zero.
    assert snap.tokens_in == 0
    assert snap.tokens_out == 0
    assert snap.cost_usd is None


def test_snapshot_handles_missing_mit_assessment() -> None:
    engine = _fake_engine()
    del engine.state.mit_assessment
    snap = ui.snapshot_from_engine(engine)
    assert snap.mit_score is None


# ---------------------------------------------------------------------------
# render_status_bar
# ---------------------------------------------------------------------------


def test_render_status_bar_shows_counts_and_validation() -> None:
    snap = ui.snapshot_from_engine(_fake_engine())
    text = _render(ui.render_status_bar(snap))
    assert "sess-1" in text
    assert "3 entities" in text
    assert "3 files" in text
    assert "base" in text
    assert "ISA" in text
    assert "Tox" in text


def test_render_status_bar_shows_tokens_when_present() -> None:
    snap = ui.UiSnapshot(
        session_id="s",
        entity_count=0,
        file_count=0,
        base_passed=False,
        isa_passed=False,
        tox_passed=False,
        required_issue_count=0,
        entity_counts={},
        mit_score=None,
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.0012,
    )
    text = _render(ui.render_status_bar(snap))
    assert "100" in text and "50" in text


# ---------------------------------------------------------------------------
# render_reply
# ---------------------------------------------------------------------------


def test_render_reply_shows_text_and_marker() -> None:
    text = _render(ui.render_reply("Hello **world**"))
    assert "Hello" in text
    assert "world" in text
    assert "●" in text


# ---------------------------------------------------------------------------
# render_resume_summary
# ---------------------------------------------------------------------------


def test_render_resume_summary_shows_session_and_counts() -> None:
    snap = ui.snapshot_from_engine(_fake_engine())
    text = _render(ui.render_resume_summary(snap))
    assert "Resumed Session" in text
    assert "sess-1" in text
    assert "Investigation" in text


# ---------------------------------------------------------------------------
# render_goodbye
# ---------------------------------------------------------------------------


def test_render_goodbye_shows_session_and_entities() -> None:
    text = _render(
        ui.render_goodbye("sess-9", {"Study": 2}, resumable=True)
    )
    assert "Goodbye" in text
    assert "sess-9" in text
    assert "Study=2" in text


# ---------------------------------------------------------------------------
# get_console
# ---------------------------------------------------------------------------


def test_get_console_is_memoized() -> None:
    assert ui.get_console() is ui.get_console()
    assert isinstance(ui.get_console(), Console)
