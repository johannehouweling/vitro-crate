"""Unit tests for the shared interactive-UI layer (``builder/agents/ui.py``).

Two seams, tested at their real public entry points:

* the ``render_*`` functions are pure formatters — their real input is a
  :class:`~builder.agents.ui.UiSnapshot`, so the tests build snapshot literals
  (no mock stands in for anything) and assert on the exported plain text;
* ``snapshot_from_engine`` is the one impure adapter — it is driven over a
  **real** ``AgentEngine``/``CrateState`` populated with real ``Entity`` /
  ``FileClassification`` / ``ValidationReport`` objects, so a regression in how
  state exposes entities or validation reddens the test.

``flatten_message_content`` (the #341 leak fix) is tested directly as a pure
string function and again through ``render_reply`` (its real call site).
"""

from __future__ import annotations

import dataclasses
import io
from types import SimpleNamespace

from rich.console import Console, RenderableType

from builder.agents import ui
from builder.engine import AgentEngine
from builder.state import (
    CrateState,
    Entity,
    EntityProvenance,
    FileClassification,
    ValidationReport,
)


def _render(renderable: RenderableType, width: int = 100) -> str:
    """Render a Rich renderable to plain text for assertions."""
    console = Console(width=width, file=io.StringIO(), record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


_BASE_SNAPSHOT = ui.UiSnapshot(
    session_id="sess-1",
    entity_count=3,
    file_count=3,
    base_passed=True,
    isa_passed=False,
    tox_passed=False,
    required_issue_count=1,
    entity_counts={"Investigation": 1, "Study": 2},
    mit_score=0.75,
    tokens_in=0,
    tokens_out=0,
    cost_usd=None,
)


def _snapshot(**overrides: object) -> ui.UiSnapshot:
    """A ``UiSnapshot`` literal — the renderers' real, direct public input."""
    return dataclasses.replace(_BASE_SNAPSHOT, **overrides)


def _real_engine(populated: bool = True) -> AgentEngine:
    """A real ``AgentEngine`` over a real ``CrateState`` (the adapter's real input).

    ``populated=False`` returns a fresh, un-built session (the meaningful empty
    edge). ``human_interface`` is left ``None`` so the engine wires its real
    ``SimulatedHumanInterface`` default rather than a hand-written double.
    """
    state = CrateState(session_id="sess-1")
    if populated:
        state.add_entity(
            Entity(
                entity_id="inv_001",
                type="Investigation",
                fields={"title": "I"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        for i in (1, 2):
            state.add_entity(
                Entity(
                    entity_id=f"stu_00{i}",
                    type="Study",
                    fields={"title": f"S{i}"},
                    _provenance=EntityProvenance(created_by="llm"),
                )
            )
        state.scanned_files = [
            FileClassification(
                path=f"/d/f{i}.csv", filename=f"f{i}.csv", size=10, mime_type="text/csv"
            )
            for i in range(3)
        ]
        state.validation = ValidationReport(
            base_passed=True,
            isa_passed=False,
            tox_passed=False,
            required_issues=["missing publisher"],
        )
        state.mit_assessment.overall_score = 0.75
    return AgentEngine(state=state)


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
# snapshot_from_engine — driven over a real engine/state
# ---------------------------------------------------------------------------


def test_snapshot_from_engine_reads_real_state() -> None:
    snap = ui.snapshot_from_engine(_real_engine())
    assert snap.session_id == "sess-1"
    assert snap.entity_count == 3
    assert snap.file_count == 3
    assert snap.base_passed is True
    assert snap.isa_passed is False
    assert snap.tox_passed is False
    assert snap.required_issue_count == 1
    assert snap.entity_counts == {"Investigation": 1, "Study": 2}
    assert snap.mit_score == 0.75
    # No profile.ndjson for this session → token totals default to zero.
    assert snap.tokens_in == 0
    assert snap.tokens_out == 0
    assert snap.cost_usd is None


def test_snapshot_from_fresh_engine_is_empty() -> None:
    snap = ui.snapshot_from_engine(_real_engine(populated=False))
    assert snap.entity_count == 0
    assert snap.file_count == 0
    assert snap.entity_counts == {}
    assert snap.base_passed is False
    assert snap.required_issue_count == 0
    # A fresh MITReport scores 0.0 (assessed-but-empty), not None.
    assert snap.mit_score == 0.0


# ---------------------------------------------------------------------------
# render_status_bar
# ---------------------------------------------------------------------------


def test_render_status_bar_shows_counts_and_validation() -> None:
    text = _render(ui.render_status_bar(_snapshot()))
    assert "sess-1" in text
    assert "3 entities" in text
    assert "3 files" in text
    assert "base" in text
    assert "ISA" in text
    assert "Tox" in text


def test_render_status_bar_shows_tokens_when_present() -> None:
    text = _render(
        ui.render_status_bar(_snapshot(tokens_in=100, tokens_out=50, cost_usd=0.0012))
    )
    # tok 100→50 (150)@$…
    assert "100" in text
    assert "50" in text
    assert "150" in text


def test_render_status_bar_hides_tokens_when_zero() -> None:
    text = _render(ui.render_status_bar(_snapshot(tokens_in=0, tokens_out=0)))
    assert "tok" not in text


# ---------------------------------------------------------------------------
# render_reply — flattens structured content at its real call site (#341)
# ---------------------------------------------------------------------------


def test_render_reply_shows_text_and_marker() -> None:
    text = _render(ui.render_reply("Hello **world**"))
    assert "Hello" in text
    assert "world" in text
    assert "●" in text


def test_render_reply_flattens_structured_content_no_leak() -> None:
    content = [
        {
            "type": "text",
            "text": "The next step is the ISA backbone.",
            "annotations": [],
            "id": "msg_abc",
            "phase": "final_answer",
        }
    ]
    text = _render(ui.render_reply(content))
    assert "ISA backbone" in text
    assert "annotations" not in text
    assert "msg_abc" not in text


# ---------------------------------------------------------------------------
# render_resume_summary
# ---------------------------------------------------------------------------


def test_render_resume_summary_shows_session_and_breakdown() -> None:
    text = _render(ui.render_resume_summary(_snapshot()))
    assert "Resumed Session" in text
    assert "sess-1" in text
    assert "Investigation" in text
    assert "MIT score" in text


# ---------------------------------------------------------------------------
# render_goodbye
# ---------------------------------------------------------------------------


def test_render_goodbye_shows_session_and_entities() -> None:
    text = _render(ui.render_goodbye("sess-9", {"Study": 2}, resumable=True))
    assert "Goodbye" in text
    assert "sess-9" in text
    assert "Study=2" in text


def test_render_goodbye_zero_entities_when_empty() -> None:
    text = _render(ui.render_goodbye("sess-9", {}, resumable=False))
    assert "0" in text


# ---------------------------------------------------------------------------
# get_console
# ---------------------------------------------------------------------------


def test_get_console_is_memoized() -> None:
    assert ui.get_console() is ui.get_console()
    assert isinstance(ui.get_console(), Console)
