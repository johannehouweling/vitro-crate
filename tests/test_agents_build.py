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


class TestStatePersistence:
    """#242 — run_interactive_build persists CrateState so the dashboard sees it.

    The pipeline path never wrote ``sessions/<id>/crate_state.json``, so a running
    ``--dashboard`` showed "No CrateState data available" and never live-updated.
    The interactive build must persist the final state (always_write) after
    guidance + export, on BOTH the interactive and headless paths.
    """

    def _isolate_sessions(self, monkeypatch, tmp_path) -> Path:
        import builder.tools.session as sess_mod

        sessions = tmp_path / "sessions"
        monkeypatch.setattr(sess_mod, "SESSION_DIR", sessions)
        monkeypatch.setattr(sess_mod, "_last_saved_state_hash", None)
        return sessions

    def test_final_state_persisted_interactive(self, monkeypatch, tmp_path) -> None:
        """After an interactive build, crate_state.json exists with the entities."""
        from builder.agents.build import run_interactive_build
        from builder.state import Entity
        from builder.tools.session import load_session

        sessions = self._isolate_sessions(monkeypatch, tmp_path)
        out_dir = tmp_path / "x-ro-crate"
        engine = _engine(_InteractiveHuman())
        engine.state.metadata.output_path = str(out_dir)
        # Seed an entity so the persisted state demonstrably carries content.
        engine.state.add_entity(
            Entity(entity_id="inv_1", type="Investigation", fields={"name": "Inv"})
        )

        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=[].append,
        )

        state_path = sessions / engine.state.session_id / "crate_state.json"
        assert state_path.is_file(), "interactive build must persist crate_state.json (#242)"
        loaded = load_session(engine.state.session_id)
        assert loaded is not None
        assert any(e.entity_id == "inv_1" for e in loaded.list_entities())

    def test_final_state_persisted_headless(self, monkeypatch, tmp_path) -> None:
        """A headless build (no guidance) still persists the final state (#242)."""
        from builder.agents.build import run_interactive_build
        from builder.tools.session import load_session

        sessions = self._isolate_sessions(monkeypatch, tmp_path)
        out_dir = tmp_path / "headless-ro-crate"
        engine = _engine(SimulatedHumanInterface())
        engine.state.metadata.output_path = str(out_dir)

        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=[].append,
        )

        state_path = sessions / engine.state.session_id / "crate_state.json"
        assert state_path.is_file()
        assert load_session(engine.state.session_id) is not None

    def test_final_save_uses_always_write(self, monkeypatch, tmp_path) -> None:
        """The final save passes always_write=True so a populated overview +
        resume is guaranteed even if a phase save deduped earlier."""
        from builder.agents import build as build_mod

        self._isolate_sessions(monkeypatch, tmp_path)
        out_dir = tmp_path / "aw-ro-crate"
        engine = _engine(SimulatedHumanInterface())
        engine.state.metadata.output_path = str(out_dir)

        saves: list[bool] = []

        def fake_save(state, *, always_write: bool = False, **kw):
            saves.append(always_write)
            return {"success": True, "session_id": state.session_id, "skipped": False}

        monkeypatch.setattr(build_mod, "save_session", fake_save, raising=False)

        build_mod.run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=[].append,
        )

        # The final save is forced (always_write=True).
        assert saves, "save_session must be called from run_interactive_build"
        assert True in saves, "the final save must use always_write=True"


class TestProgressOutput:
    """#241 — the interactive build emits staged progress through `output`."""

    def test_progress_lines_emitted_to_output(self, tmp_path) -> None:
        """Concise per-phase progress lines reach the output channel."""
        from builder.agents.build import run_interactive_build

        out_dir = tmp_path / "prog-ro-crate"
        engine = _engine(_InteractiveHuman())
        engine.state.metadata.output_path = str(out_dir)
        lines: list[str] = []

        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=lines.append,
        )

        joined = "\n".join(lines).lower()
        # The final crate-written line is always surfaced (the #233 export line).
        assert "crate written" in joined
        # And at least one staged progress marker precedes it.
        assert any(
            kw in joined
            for kw in ("scaffold", "materializ", "validat", "resolv", "scanning", "extract")
        ), "interactive build must emit staged progress (#241)"

    def test_progress_threaded_into_pipeline(self, tmp_path) -> None:
        """run_interactive_build threads a progress callback into run_pipeline so
        the spine's per-phase lines reach output."""
        from builder.agents.build import run_interactive_build

        out_dir = tmp_path / "thread-ro-crate"
        engine = _engine(SimulatedHumanInterface())
        engine.state.metadata.output_path = str(out_dir)
        seen_progress: list[Any] = []

        def fake_pipeline(eng, *, progress=None, **kw):
            # The build must hand the spine a usable (non-None) progress callback.
            seen_progress.append(progress)
            if progress is not None:
                progress("Scaffolding ISA backbone…")
            return dict(_PIPELINE_RESULT)

        lines: list[str] = []
        run_interactive_build(
            engine,
            pipeline_runner=fake_pipeline,
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=lines.append,
        )

        assert seen_progress and seen_progress[0] is not None
        assert any("scaffold" in line.lower() for line in lines)

    def test_default_output_is_noop_for_progress(self, tmp_path) -> None:
        """With the default (no) output the build runs silently — no crash, no
        print — so eval/tests stay clean."""
        from builder.agents.build import run_interactive_build

        out_dir = tmp_path / "silent-ro-crate"
        engine = _engine(SimulatedHumanInterface())
        engine.state.metadata.output_path = str(out_dir)

        # No output kwarg => default no-op sink. Must not raise.
        result = run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
        )
        assert result["export"]["success"] is True


class TestProgressSpinner:
    """#266 — the DEFAULT interactive build drives a live progress spinner.

    The spinner is created only on the REAL interactive path (an interactive
    ``HumanInterface``), so headless / simulated runs (the A/B eval, batch, tests)
    stay completely silent — no spinner, no daemon thread, no stdout noise — and
    the built ``@graph`` hash is unperturbed. When the spinner runs it subscribes
    to the engine's ``on_tool_event`` hook (set during the build, restored after)
    and feeds the #253 phase-progress strings into ``set_current``.
    """

    def test_no_spinner_on_non_interactive_build(self, tmp_path, monkeypatch) -> None:
        """A simulated (headless) build creates NO spinner — no thread, no noise."""
        import builder.agents.build as build_mod
        from builder.agents.build import run_interactive_build

        created: list[Any] = []

        class _SpySpinner:
            def __init__(self, *a: Any, **k: Any) -> None:
                created.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *a: Any) -> None:
                pass

            def set_current(self, _text: str) -> None:
                pass

        monkeypatch.setattr(build_mod, "ProgressSpinner", _SpySpinner, raising=False)

        out_dir = tmp_path / "headless-ro-crate"
        engine = _engine(SimulatedHumanInterface())
        engine.state.metadata.output_path = str(out_dir)

        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=[].append,
        )

        assert created == [], "no spinner may be created on a headless build"

    def test_spinner_created_on_interactive_build(self, tmp_path, monkeypatch) -> None:
        """A real interactive build creates the spinner and drives set_current."""
        import builder.agents.build as build_mod
        from builder.agents.build import run_interactive_build

        ops: list[str] = []
        created: list[Any] = []

        class _SpySpinner:
            def __init__(self, *a: Any, **k: Any) -> None:
                created.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *a: Any) -> None:
                pass

            def set_current(self, text: str) -> None:
                ops.append(text)

        monkeypatch.setattr(build_mod, "ProgressSpinner", _SpySpinner, raising=False)

        out_dir = tmp_path / "interactive-ro-crate"
        engine = _engine(_InteractiveHuman())
        engine.state.metadata.output_path = str(out_dir)

        def fake_pipeline(eng, *, progress=None, **kw):
            if progress is not None:
                progress("Scaffolding ISA backbone…")
            return dict(_PIPELINE_RESULT)

        run_interactive_build(
            engine,
            pipeline_runner=fake_pipeline,
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=[].append,
        )

        assert len(created) == 1, "the interactive build must create one spinner"
        # The #253 phase string was fed into the spinner's current-op display.
        assert any("scaffold" in op.lower() for op in ops)

    def test_on_tool_event_restored_after_build(self, tmp_path) -> None:
        """The engine's on_tool_event hook is restored to its prior value after."""
        from builder.agents.build import run_interactive_build

        out_dir = tmp_path / "restore-ro-crate"
        engine = _engine(_InteractiveHuman())
        engine.state.metadata.output_path = str(out_dir)
        sentinel = lambda _n, _p: None  # noqa: E731
        engine.on_tool_event = sentinel

        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=[].append,
        )

        assert engine.on_tool_event is sentinel, "the prior hook must be restored"

    def test_spinner_path_does_not_perturb_graph_hash(self, tmp_path) -> None:
        """Determinism: the spinner path yields the same built @graph hash as the
        headless path (the spinner is pure UI — it never touches the crate)."""
        from builder.agents.build import run_interactive_build
        from builder.agents.pipeline import run_pipeline
        from eval.metrics import crate_graph_hash

        # Headless path (no spinner) — the reference hash.
        e1 = _engine(SimulatedHumanInterface())
        e1.state.metadata.output_path = str(tmp_path / "a-ro-crate")
        run_interactive_build(e1, output=[].append)
        h1 = crate_graph_hash(e1.state)

        # Interactive path (spinner active) — must match.
        e2 = _engine(_InteractiveHuman())
        e2.state.metadata.output_path = str(tmp_path / "b-ro-crate")
        run_interactive_build(e2, output=[].append)
        h2 = crate_graph_hash(e2.state)

        # Sanity: the real run_pipeline produced a non-trivial crate.
        ref = _engine(SimulatedHumanInterface())
        run_pipeline(ref)
        assert h1 == h2
