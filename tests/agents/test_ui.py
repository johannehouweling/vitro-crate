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

import pytest
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
    mit_assessed=True,
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
    """The middle slot carries findings once drafting has started, not the scan.

    ``3 files`` is settled before the first entity exists and never moves again,
    so once ``entity_count`` is non-zero the slot is spent on what is still open
    instead. This snapshot has entities and no assessed tiers, so the honest
    answer there is "locked" — not a zero for checks nobody ran. The slot's own
    cases live in ``tests/test_status_footer.py``.
    """
    text = _render(ui.render_status_bar(_snapshot()))
    assert "sess-1" in text
    assert "3 entities" in text
    assert "3 files" not in text
    assert "req/rec/opt locked" in text
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
    text = _render(ui.render_resume_summary(_snapshot(), resumed=True))
    assert "Resumed Session" in text
    assert "sess-1" in text
    assert "Investigation" in text
    assert "MIT score" in text
    # Coverage renders as a whole percent (0.75 -> "75%"), not a raw fraction.
    assert "75%" in text


def test_render_resume_summary_hides_mit_when_unassessed() -> None:
    """An unassessed crate omits the MIT row rather than showing a misleading 0%."""
    text = _render(
        ui.render_resume_summary(_snapshot(mit_assessed=False, mit_score=0.0), resumed=True)
    )
    assert "MIT score" not in text


def test_render_resume_summary_does_not_claim_resume_on_a_fresh_session() -> None:
    """A never-resumed session must not be titled "Resumed" (#410).

    The panel is still worth showing on a fresh ``--input`` run — session id and
    scanned-file count are a useful preflight — but the *same populated snapshot*
    must read as a plain session when the run did not come from ``--resume``.
    """
    snap = _snapshot()
    assert "Resumed" in _render(ui.render_resume_summary(snap, resumed=True))
    text = _render(ui.render_resume_summary(snap, resumed=False))
    assert "Resumed" not in text
    # The body still carries the useful preflight facts.
    assert "sess-1" in text
    assert "Investigation" in text


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
# print_* helpers — the shared "snapshot → render → print" both arms call
# ---------------------------------------------------------------------------


def _rec() -> Console:
    """A recording console captured in place of the shared one."""
    return Console(width=100, file=io.StringIO(), record=True, color_system=None)


def test_print_status_bar_prints_for_engine(monkeypatch) -> None:
    rec = _rec()
    monkeypatch.setattr(ui, "get_console", lambda: rec)
    ui.print_status_bar(_real_engine())
    out = rec.export_text()
    assert "sess-1" in out
    assert "entities" in out


def test_print_resume_summary_prints_when_populated(monkeypatch) -> None:
    rec = _rec()
    monkeypatch.setattr(ui, "get_console", lambda: rec)
    ui.print_resume_summary(_real_engine(), resumed=True)
    assert "Resumed Session" in rec.export_text()


def test_print_resume_summary_states_the_model_on_a_fresh_session(monkeypatch) -> None:
    """An empty session prints the model line — and nothing more (#494).

    There is no panel to draw, but the model about to spend the user's money is
    worth stating up front, so ``print_resume_summary`` deliberately falls back to
    a one-line header. This test used to pin the older "strict no-op" contract and
    was the thing making main red; the production branch is the intended
    behaviour, so the expectation moves rather than the code.

    The assertion is over CONTENT, not emptiness: the session id and the resolved
    model must both appear, and the panel must NOT — an assertion that only
    checked for non-empty output would pass on a full panel too.
    """
    import builder.config as config_mod

    rec = _rec()
    monkeypatch.setattr(ui, "get_console", lambda: rec)
    # Pin the model through the REAL snapshot producer: `snapshot_from_engine`
    # falls back to `get_active_model()` when the profile has no model event yet,
    # which is exactly a fresh session. Stubbing the snapshot itself would bypass
    # the producer under test.
    monkeypatch.setattr(config_mod, "get_active_model", lambda: "gpt-4o-mini")

    ui.print_resume_summary(_real_engine(populated=False), resumed=False)

    out = rec.export_text()
    assert "sess-1" in out
    assert "gpt-4o-mini" in out
    assert "Resumed Session" not in out


def test_print_resume_summary_is_silent_when_no_model_is_resolved(monkeypatch) -> None:
    """With nothing built AND no model known there is nothing to say (#494).

    The honesty control for the test above: it proves the model line is caused by
    a RESOLVED model rather than by "the function was called on an empty
    session". Without it, the pair could not tell the new branch from an
    unconditional header.
    """
    import builder.config as config_mod

    rec = _rec()
    monkeypatch.setattr(ui, "get_console", lambda: rec)
    monkeypatch.setattr(config_mod, "get_active_model", lambda: "")

    ui.print_resume_summary(_real_engine(populated=False), resumed=False)

    assert rec.export_text() == ""


def test_print_resume_summary_does_not_claim_resume_after_a_scan(monkeypatch) -> None:
    """Scanned files alone must not read as a resume (#410).

    ``engine.initialize(--input)`` fills ``scanned_files`` before either arm prints
    its banner, so the old content-based gate (``entity_count or file_count``) made
    a brand-new session indistinguishable from a resumed one. Provenance is a
    caller fact, so the adapter must be *told*, never left to infer.
    """
    rec = _rec()
    monkeypatch.setattr(ui, "get_console", lambda: rec)
    ui.print_resume_summary(_real_engine(), resumed=False)
    assert "Resumed" not in rec.export_text()


def test_print_resume_summary_requires_explicit_provenance() -> None:
    """``resumed`` is keyword-only and mandatory — no silently-wrong default (#410)."""
    with pytest.raises(TypeError):
        ui.print_resume_summary(_real_engine())  # ty: ignore[missing-argument]


def test_print_goodbye_prints_for_engine(monkeypatch) -> None:
    rec = _rec()
    monkeypatch.setattr(ui, "get_console", lambda: rec)
    ui.print_goodbye(_real_engine(), resumable=False)
    out = rec.export_text()
    assert "Goodbye" in out
    assert "sess-1" in out


# ---------------------------------------------------------------------------
# get_console
# ---------------------------------------------------------------------------


def test_get_console_is_memoized() -> None:
    assert ui.get_console() is ui.get_console()
    assert isinstance(ui.get_console(), Console)
