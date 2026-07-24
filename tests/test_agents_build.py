"""Tests for builder/agents/build.py — the interactive hybrid build path (#179).

``run_interactive_build(engine)`` completes the §14 hybrid loop: it runs the
**automated** deterministic pipeline (:func:`builder.agents.pipeline.pipeline.run_pipeline`)
and then — *only* for a REAL interactive user — runs the HITL guidance tail
(:func:`builder.agents.pipeline.guidance.run_guidance`). The non-interactive/simulated path
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


class TestHeadlessGapSummary:
    """#179 (Lane 5) — the HEADLESS build surfaces a one-shot gap summary.

    On the non-interactive path ``run_guidance`` is never called (there is no
    human to answer), but the user still deserves to SEE the MUST/conformance
    posture. ``run_interactive_build`` emits a single, non-blocking summary line
    on the headless path. It must NOT prompt and must NOT call ``run_guidance``.

    **Perf (#296).** The summary is derived from the validation result the
    pipeline ALREADY computed (``pipeline_result["issues"]`` / ``["conformance"]``)
    — it does NOT re-run a fresh ``assess_gaps`` (which sweeps the heaviest
    ``severity="optional"`` SHACL pass + MIT + FAIR, the #115 tox-pass bottleneck).
    So the summary adds negligible time to a build that already validated.
    """

    # A pipeline result with two open REQUIRED (MUST) issues and a failing tox
    # layer — what the spine's required-severity fix loop leaves behind.
    _PIPELINE_WITH_GAPS: dict[str, Any] = {
        "ok": False,
        "conformance": {"base": True, "isa": True, "tox": False},
        "issues": [
            {"severity": "required", "entity_id": "p1", "property": "result"},
            {"severity": "required", "entity_id": "p2", "property": "output"},
        ],
        "scaffold": {},
        "materialized": {},
        "drafted": {},
        "fix_rounds": 1,
    }

    def test_headless_emits_summary_with_must_count_and_conformance(self) -> None:
        from builder.agents.build import run_interactive_build

        guidance_calls: list[Any] = []
        engine = _engine(SimulatedHumanInterface())

        lines: list[str] = []
        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(self._PIPELINE_WITH_GAPS),
            guidance_runner=lambda eng, human, **kw: guidance_calls.append(human)
            or dict(_GUIDANCE_RESULT),
            output=lines.append,
        )

        # run_guidance was NOT called on this headless path.
        assert guidance_calls == []

        joined = "\n".join(lines)
        lower = joined.lower()
        # The MUST count derived from the pipeline's REQUIRED issues is surfaced.
        assert "must" in lower
        assert "2" in joined  # two open REQUIRED issues
        # Per-layer conformance from the pipeline result is surfaced.
        assert "base" in lower
        assert "isa" in lower
        assert "tox" in lower
        # No guidance ("resolved"/"asked") wording — guidance never ran.
        assert "resolved" not in lower
        assert "asked" not in lower

    def test_headless_summary_does_not_run_fresh_assess_gaps(self, monkeypatch) -> None:
        """#296 — the headless summary must NOT re-validate (no fresh assess_gaps).

        Re-running ``assess_gaps`` on the headless path re-ran the heaviest
        ``severity="optional"`` SHACL sweep, blowing the CI per-test budget. The
        summary must derive purely from the already-computed pipeline result. We
        assert ``build_and_validate`` (which a fresh ``assess_gaps`` would call) is
        never invoked from the build after the pipeline returns.
        """
        import builder.tools.validation as validation_mod
        from builder.agents.build import run_interactive_build

        validate_calls: list[Any] = []
        real_bav = validation_mod.build_and_validate

        def spy_bav(*a: Any, **k: Any) -> dict[str, Any]:
            validate_calls.append((a, k))
            return real_bav(*a, **k)

        # Spy on the canonical entry point a fresh assess_gaps would call. The
        # injected pipeline runner does NOT call build_and_validate, so any call
        # observed here would come from the headless summary path.
        monkeypatch.setattr(validation_mod, "build_and_validate", spy_bav)

        engine = _engine(SimulatedHumanInterface())
        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(self._PIPELINE_WITH_GAPS),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=[].append,
        )

        assert validate_calls == [], (
            "the headless summary must reuse the pipeline result, not re-validate"
        )

    def test_headless_gap_summary_is_one_shot_and_nonblocking(self) -> None:
        """The headless summary never prompts the human (no present/request_input)."""
        from builder.agents.build import run_interactive_build

        prompts: list[str] = []

        class _RecordingHuman:
            is_interactive = False

            def present(self, context, options=None, purpose=None):
                prompts.append("present")
                return {"action": "approved", "comments": None, "edits": None}

            def request_input(self, prompt, field_type="text"):
                prompts.append("request_input")
                return {"value": None, "skipped": True}

        engine = _engine(_RecordingHuman())

        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(self._PIPELINE_WITH_GAPS),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=[].append,
        )

        assert prompts == [], "the headless gap summary must not prompt the human"

    def test_interactive_path_does_not_emit_headless_gap_summary(self) -> None:
        """The interactive path runs guidance, NOT the one-shot headless summary."""
        from builder.agents.build import run_interactive_build

        engine = _engine(_InteractiveHuman())

        lines: list[str] = []
        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            output=lines.append,
        )

        joined = "\n".join(lines).lower()
        # The interactive guidance summary is what shows ("resolved"/"asked").
        assert "resolved" in joined
        # The headless-only "no interactive guidance" wording does NOT appear.
        assert "no interactive guidance" not in joined

    def test_format_gap_summary_from_pipeline_result(self) -> None:
        """The summary formatter derives MUST count + conformance from the result."""
        from builder.agents.build import format_gap_summary

        text = format_gap_summary(self._PIPELINE_WITH_GAPS)
        assert isinstance(text, str)
        lower = text.lower()
        assert "2" in text  # two REQUIRED issues -> MUST count
        assert "must" in lower
        assert "base" in lower and "isa" in lower and "tox" in lower

    def test_format_gap_summary_handles_missing_fields(self) -> None:
        """A result lacking issues/conformance yields a short, non-crashing line."""
        from builder.agents.build import format_gap_summary

        text = format_gap_summary({})
        assert isinstance(text, str)
        assert text  # non-empty, no crash


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

    # This test runs the REAL deterministic pipeline TWICE (headless + interactive),
    # each doing a full base→ISA→ISA-Tox SHACL sweep in the fix loop — genuinely
    # heavy, near the default 30s CI per-test budget under -n2 contention. Bump just
    # this test (the #278 precedent; do NOT blanket-mark the fast wiring tests).
    @pytest.mark.timeout(120)
    def test_spinner_path_does_not_perturb_graph_hash(self, tmp_path) -> None:
        """Determinism: the spinner path yields the same built @graph hash as the
        headless path (the spinner is pure UI — it never touches the crate).

        Both paths run the REAL deterministic pipeline (so the comparison is
        meaningful); only the interactive path drives the spinner. The HITL
        guidance tail is stubbed with a graph-neutral no-op so the test isolates
        the spinner's effect on the built ``@graph`` WITHOUT paying for a second
        full SHACL guidance run — which made the test run three heavy real builds
        and blow the CI per-test ``--timeout`` (#266).
        """
        from builder.agents.build import run_interactive_build
        from builder.state import CrateState
        from eval.metrics import crate_graph_hash

        # A guidance runner that touches NOTHING (the interactive _InteractiveHuman
        # approves/skips every gap, so real guidance is a graph no-op here anyway).
        noop_guidance = lambda eng, human, **kw: {}  # noqa: E731

        # Headless path (no spinner) — runs the real pipeline; the reference hash.
        e1 = _engine(SimulatedHumanInterface())
        e1.state.metadata.output_path = str(tmp_path / "a-ro-crate")
        run_interactive_build(e1, output=[].append)
        h1 = crate_graph_hash(e1.state)

        # Interactive path (spinner ACTIVE) — same real pipeline, must match.
        e2 = _engine(_InteractiveHuman())
        e2.state.metadata.output_path = str(tmp_path / "b-ro-crate")
        run_interactive_build(e2, guidance_runner=noop_guidance, output=[].append)
        h2 = crate_graph_hash(e2.state)

        assert h1 == h2
        # Sanity: the real pipeline produced a non-trivial crate (not the empty
        # default), so the equality above is a real signal, not two empty hashes.
        assert h1 != crate_graph_hash(CrateState())


def _stub_export(state: CrateState) -> dict[str, Any]:
    """An exporter double that reports success without touching disk."""
    return {"success": True, "crate_path": "/tmp/vitro-crate-test/ro-crate-metadata.json"}


def _rec_console() -> Any:
    """A recording Rich console that captures printed chrome as plain text."""
    import io

    from rich.console import Console

    return Console(file=io.StringIO(), record=True, color_system=None, width=100)


class TestSharedChrome:
    """The interactive pipeline renders the shared UI chrome; headless does not (#344).

    Both arms render through ``builder.agents.ui``; here we drive the real
    ``run_interactive_build`` (pipeline/guidance/exporter stubbed — no SHACL, LLM,
    or disk) and capture what lands on the shared console.
    """

    def test_interactive_build_renders_final_status_bar(self, monkeypatch) -> None:
        from builder.agents import build, ui

        rec = _rec_console()
        monkeypatch.setattr(ui, "get_console", lambda: rec)
        engine = _engine(_InteractiveHuman())

        build.run_interactive_build(
            engine,
            pipeline_runner=lambda eng, **kw: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            exporter=_stub_export,
        )

        out = rec.export_text()
        # The shared one-line status bar: session id, counts, validation labels.
        assert engine.state.session_id in out
        assert "entities" in out
        assert "base" in out and "ISA" in out and "Tox" in out

    def test_headless_build_renders_no_chrome(self, monkeypatch) -> None:
        from builder.agents import build, ui

        rec = _rec_console()
        monkeypatch.setattr(ui, "get_console", lambda: rec)
        engine = _engine(SimulatedHumanInterface())

        build.run_interactive_build(
            engine,
            pipeline_runner=lambda eng, **kw: dict(_PIPELINE_RESULT),
            exporter=_stub_export,
        )

        # Headless / eval path prints no banner or status bar — the A/B path is
        # left byte-identical (the chrome is strictly interactive-only).
        assert rec.export_text() == ""

    def test_interactive_resume_renders_banner(self, monkeypatch) -> None:
        from builder.agents import build, ui
        from builder.state import Entity, EntityProvenance

        rec = _rec_console()
        monkeypatch.setattr(ui, "get_console", lambda: rec)
        engine = _engine(_InteractiveHuman())
        # A pre-existing entity marks this as a resume → the summary panel shows.
        engine.state.add_entity(
            Entity(
                entity_id="inv_001",
                type="Investigation",
                fields={"title": "I"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )

        build.run_interactive_build(
            engine,
            pipeline_runner=lambda eng, **kw: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            exporter=_stub_export,
        )

        assert "Resumed Session" in rec.export_text()

    def test_interactive_build_renders_goodbye(self, monkeypatch) -> None:
        from builder.agents import build, ui

        rec = _rec_console()
        monkeypatch.setattr(ui, "get_console", lambda: rec)
        engine = _engine(_InteractiveHuman())

        build.run_interactive_build(
            engine,
            pipeline_runner=lambda eng, **kw: dict(_PIPELINE_RESULT),
            guidance_runner=lambda eng, human, **kw: dict(_GUIDANCE_RESULT),
            exporter=_stub_export,
        )

        out = rec.export_text()
        # The shared goodbye panel: title + session id (the --resume hint's anchor).
        assert "Goodbye" in out
        assert engine.state.session_id in out
