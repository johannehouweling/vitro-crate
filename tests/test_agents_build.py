"""Tests for builder/agents/build.py — the interactive hybrid build path (#179).

``run_interactive_build(engine)`` completes the §14 hybrid loop: it runs the
**automated** deterministic pipeline (:func:`builder.agents.pipeline.run_pipeline`)
and then — *only* for a REAL interactive user — runs the HITL guidance tail
(:func:`builder.agents.guidance.run_guidance`). The non-interactive/simulated path
(the A/B eval, headless/batch) must run the pipeline ALONE so the A/B stays a clean
automated-vs-automated comparison.

These tests stub both the pipeline and the guidance runners (no SHACL, no LLM, no
network) so they assert *only* the wiring: that guidance is invoked iff the human
interface is interactive, and that a concise summary is surfaced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.hitl import HumanInterface, SimulatedHumanInterface


class _InteractiveHuman:
    """A HumanInterface double that declares itself interactive."""

    is_interactive = True

    def present(self, context, options=None, purpose=None):
        return {"action": "approved", "comments": None, "edits": None}

    def request_input(self, prompt, field_type="text"):
        return {"value": None, "skipped": True}


class _SilentHuman:
    """A HumanInterface double with NO is_interactive attribute (defaults off)."""

    def present(self, context, options=None, purpose=None):
        return {"action": "approved", "comments": None, "edits": None}

    def request_input(self, prompt, field_type="text"):
        return {"value": None, "skipped": True}


def _engine(human: HumanInterface) -> AgentEngine:
    engine = AgentEngine(state=CrateState(), human_interface=human)
    engine.initialize()  # assigns a session_id; no input => no scan
    return engine


_PIPELINE_RESULT: dict[str, Any] = {
    "ok": True,
    "conformance": {"base": True, "isa": True, "tox": True},
    "issues": [],
    "scaffold": {},
    "materialized": {},
    "drafted": {},
    "fix_rounds": 0,
}

_GUIDANCE_RESULT: dict[str, Any] = {
    "resolved": [{"tier": "MUST", "property": "name", "via": "ask-user"}],
    "asked": [{"tier": "SHOULD", "property": "description"}],
    "remaining_gaps": {"must_open": 0, "should_open": 1, "may_open": 2},
    "conformance": {"base": True, "isa": True, "tox": False},
    "rounds": 2,
}


class TestInteractiveGating:
    """run_guidance runs IFF the human interface is interactive."""

    def test_interactive_human_invokes_guidance(self) -> None:
        from builder.agents.build import run_interactive_build

        pipeline_calls: list[Any] = []
        guidance_calls: list[Any] = []
        engine = _engine(_InteractiveHuman())

        def fake_pipeline(eng: AgentEngine) -> dict:
            pipeline_calls.append(eng)
            return dict(_PIPELINE_RESULT)

        def fake_guidance(eng: AgentEngine, human: HumanInterface, **kw: Any) -> dict:
            guidance_calls.append((eng, human))
            return dict(_GUIDANCE_RESULT)

        result = run_interactive_build(
            engine, pipeline_runner=fake_pipeline, guidance_runner=fake_guidance
        )

        # Pipeline always runs; guidance runs because the human is interactive.
        assert len(pipeline_calls) == 1
        assert pipeline_calls[0] is engine
        assert len(guidance_calls) == 1
        assert guidance_calls[0][0] is engine
        assert guidance_calls[0][1] is engine.human_interface
        assert result["guidance"] == _GUIDANCE_RESULT

    def test_simulated_human_skips_guidance(self) -> None:
        from builder.agents.build import run_interactive_build

        pipeline_calls: list[Any] = []
        guidance_calls: list[Any] = []
        engine = _engine(SimulatedHumanInterface())

        def fake_pipeline(eng: AgentEngine) -> dict:
            pipeline_calls.append(eng)
            return dict(_PIPELINE_RESULT)

        def fake_guidance(eng: AgentEngine, human: HumanInterface, **kw: Any) -> dict:
            guidance_calls.append((eng, human))
            return dict(_GUIDANCE_RESULT)

        result = run_interactive_build(
            engine, pipeline_runner=fake_pipeline, guidance_runner=fake_guidance
        )

        # Pipeline runs; guidance is SKIPPED (the simulated interface is headless).
        assert len(pipeline_calls) == 1
        assert guidance_calls == []
        assert result["guidance"] is None

    def test_missing_is_interactive_attribute_skips_guidance(self) -> None:
        """A HumanInterface that does not declare is_interactive is non-interactive."""
        from builder.agents.build import run_interactive_build

        guidance_calls: list[Any] = []
        engine = _engine(_SilentHuman())

        result = run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: guidance_calls.append(human)
            or dict(_GUIDANCE_RESULT),
        )

        assert guidance_calls == []
        assert result["guidance"] is None

    def test_pipeline_result_always_returned(self) -> None:
        from builder.agents.build import run_interactive_build

        engine = _engine(SimulatedHumanInterface())
        result = run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
        )
        assert result["pipeline"] == _PIPELINE_RESULT


class TestGuidanceSummary:
    """The guidance results are surfaced as a concise human-readable summary."""

    def test_format_guidance_summary_mentions_counts(self) -> None:
        from builder.agents.build import format_guidance_summary

        text = format_guidance_summary(_GUIDANCE_RESULT)
        assert isinstance(text, str)
        # Resolved / asked / remaining counts are all surfaced.
        assert "1" in text  # 1 resolved
        assert "resolved" in text.lower()
        assert "asked" in text.lower()
        assert "remaining" in text.lower() or "gap" in text.lower()
        # Per-layer conformance is surfaced.
        assert "base" in text.lower()
        assert "isa" in text.lower()
        assert "tox" in text.lower()

    def test_format_guidance_summary_handles_none(self) -> None:
        """No guidance ran (non-interactive) -> a short, non-crashing message."""
        from builder.agents.build import format_guidance_summary

        text = format_guidance_summary(None)
        assert isinstance(text, str)
        assert text  # non-empty

    def test_run_interactive_build_surfaces_summary_via_output(self) -> None:
        """The interactive build prints the guidance summary to the output channel."""
        from builder.agents.build import run_interactive_build

        lines: list[str] = []
        engine = _engine(_InteractiveHuman())
        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=lines.append,
        )
        joined = "\n".join(lines)
        assert "resolved" in joined.lower()
        assert "tox" in joined.lower()

    def test_non_interactive_build_does_not_emit_guidance_summary(self) -> None:
        """A headless build emits no guidance summary (only the pipeline ran)."""
        from builder.agents.build import run_interactive_build

        lines: list[str] = []
        engine = _engine(SimulatedHumanInterface())
        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=lines.append,
        )
        joined = "\n".join(lines).lower()
        # No "asked"/"resolved" guidance wording when guidance never ran.
        assert "resolved" not in joined
        assert "asked" not in joined


class TestDeterministicExport:
    """The interactive build writes the enriched crate to disk (#233).

    Before #233 the pipeline path built + validated in memory and exited without
    calling :func:`builder.tools.builder.export_crate`, so nothing landed on disk
    and ``--output`` had no effect on the default build. These tests assert the
    final deterministic export: ``ro-crate-metadata.json`` is written to the
    resolved path AND that absolute path is surfaced via the ``output`` channel.
    """

    def test_export_writes_metadata_to_resolved_output_path(self, tmp_path) -> None:
        """After the build, ro-crate-metadata.json exists at the resolved path."""
        from builder.agents.build import run_interactive_build

        out_dir = tmp_path / "experiment-ro-crate"
        engine = _engine(_InteractiveHuman())
        engine.state.metadata.output_path = str(out_dir)

        lines: list[str] = []
        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=lines.append,
        )

        # The on-disk writer ran: the crate metadata document exists.
        assert (out_dir / "ro-crate-metadata.json").is_file()
        # The final ABSOLUTE path is surfaced to the user via the output channel.
        joined = "\n".join(lines)
        assert str(out_dir.resolve()) in joined

    def test_export_runs_on_non_interactive_build(self, tmp_path) -> None:
        """Even a headless build (no guidance) still writes the crate (#233)."""
        from builder.agents.build import run_interactive_build

        out_dir = tmp_path / "headless-ro-crate"
        engine = _engine(SimulatedHumanInterface())
        engine.state.metadata.output_path = str(out_dir)

        lines: list[str] = []
        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=lines.append,
        )

        assert (out_dir / "ro-crate-metadata.json").is_file()
        assert str(out_dir.resolve()) in "\n".join(lines)

    def test_export_runs_after_guidance(self, tmp_path) -> None:
        """Export is the FINAL step — it runs after guidance has mutated state."""
        from builder.agents.build import run_interactive_build

        out_dir = tmp_path / "ordered-ro-crate"
        engine = _engine(_InteractiveHuman())
        engine.state.metadata.output_path = str(out_dir)
        order: list[str] = []

        def fake_guidance(eng: AgentEngine, human: HumanInterface, **kw: Any) -> dict:
            order.append("guidance")
            return dict(_GUIDANCE_RESULT)

        def fake_exporter(state: CrateState, **kw: Any) -> dict[str, Any]:
            order.append("export")
            return {"success": True, "crate_path": str(out_dir), "error": None}

        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=fake_guidance,
            exporter=fake_exporter,
            output=[].append,
        )

        assert order == ["guidance", "export"]

    def test_export_failure_is_surfaced_not_swallowed(self, tmp_path) -> None:
        """A failed export is reported via output and raised, never silent (#233)."""
        from builder.agents.build import run_interactive_build

        engine = _engine(SimulatedHumanInterface())
        engine.state.metadata.output_path = str(tmp_path / "x-ro-crate")
        lines: list[str] = []

        def failing_exporter(state: CrateState, **kw: Any) -> dict[str, Any]:
            return {
                "success": False,
                "crate_path": state.metadata.output_path,
                "error": "disk full",
            }

        with pytest.raises(RuntimeError, match="disk full"):
            run_interactive_build(
                engine,
                pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
                guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
                exporter=failing_exporter,
                output=lines.append,
            )

        # The failure was surfaced to the user before raising.
        assert any("disk full" in line.lower() or "fail" in line.lower() for line in lines)

    def test_export_result_in_return_value(self, tmp_path) -> None:
        """run_interactive_build returns the export result under an 'export' key."""
        from builder.agents.build import run_interactive_build

        out_dir = tmp_path / "ret-ro-crate"
        engine = _engine(SimulatedHumanInterface())
        engine.state.metadata.output_path = str(out_dir)

        result = run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=[].append,
        )

        assert result["export"]["success"] is True
        assert Path(result["export"]["crate_path"]).resolve() == out_dir.resolve()
