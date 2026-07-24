"""Tests for builder/agents/progress_spinner.py — the shared build spinner (#266, #344).

The DEFAULT ``--interactive`` (deterministic pipeline) build only printed static
phase lines (#253), so the ~tens-of-seconds spine looked frozen. :class:`ProgressSpinner`
gives it a live Rich spinner: an animated dots spinner with a rotating funny
toxicology-themed phrase, the currently running tool/phase, and elapsed seconds,
updating in place. Both build arms drive this one spinner (#344) — the pipeline from
``engine.on_tool_event``, the ReAct loop from LangChain tool-event callbacks.

The spinner registers itself as the active console animation (``register_console_animation``)
so a guidance HITL prompt (which calls ``suspend_console_animation``) can pause it and
own the terminal. These tests use a fake console/status (no real terminal).
"""

from __future__ import annotations

from typing import Any


class _FakeStatus:
    """A fake Rich ``console.status`` recording start/stop/update calls."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.renders: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")

    def update(self, renderable: Any = None, **_k: Any) -> None:
        if renderable is not None:
            self.renders.append(str(renderable))

    def __enter__(self) -> "_FakeStatus":
        self.start()
        return self

    def __exit__(self, *_a: Any) -> None:
        self.stop()


class _FakeConsole:
    """A fake Rich ``Console`` that hands back a fixed ``_FakeStatus``.

    Reports ``is_terminal=True`` (the animated path) so the spinner behaves as
    it does on a real TTY. The CI / piped (non-terminal) case is modelled by
    :class:`_NonTTYConsole`.
    """

    is_terminal = True

    def __init__(self, status: _FakeStatus) -> None:
        self._status = status

    def status(self, *_a: Any, **_k: Any) -> _FakeStatus:
        return self._status


class _NonTTYConsole:
    """A fake Rich ``Console`` that reports a NON-terminal output (CI / piped).

    Models the CI / non-interactive case: ``console.is_terminal`` is ``False``.
    Records whether :meth:`status` was ever called — on a non-TTY the spinner
    must NOT create a Live status at all (no Rich ``Live`` region, no Rich
    refresh thread), so ``status_calls`` must stay ``0``.
    """

    is_terminal = False

    def __init__(self) -> None:
        self.status_calls = 0

    def status(self, *_a: Any, **_k: Any) -> _FakeStatus:
        self.status_calls += 1
        return _FakeStatus()


class TestPhraseList:
    """A FRESH toxicology-themed phrase list lives in this module (not imported)."""

    def test_phrase_list_is_nonempty(self) -> None:
        from builder.agents.progress_spinner import TOX_SPINNER_PHRASES

        assert isinstance(TOX_SPINNER_PHRASES, list)
        assert len(TOX_SPINNER_PHRASES) >= 5
        assert all(isinstance(p, str) and p for p in TOX_SPINNER_PHRASES)

    def test_phrase_list_is_module_level(self) -> None:
        """The list is defined fresh in THIS module (not imported from agent_loop)."""
        from builder.agents import progress_spinner

        assert hasattr(progress_spinner, "TOX_SPINNER_PHRASES")


class TestRender:
    """The spinner renders the phrase, elapsed seconds, and the current op."""

    def test_render_includes_phrase_and_elapsed(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner

        sp = ProgressSpinner(console=_FakeConsole(_FakeStatus()), phrase="intoxicating")
        text = sp._render()
        assert "intoxicating" in text
        assert "s)" in text  # elapsed seconds, e.g. "(0s)"

    def test_render_includes_current_op_when_set(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner

        sp = ProgressSpinner(console=_FakeConsole(_FakeStatus()), phrase="vortexing")
        sp.set_current("scaffold_isa_backbone")
        assert "scaffold_isa_backbone" in sp._render()

    def test_set_current_updates_the_live_region(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="titrating")
        sp.set_current("Materializing 12 entities…")
        assert any("Materializing 12 entities" in r for r in st.renders)


class TestRegistration:
    """The spinner registers with the hitl registry on enter, unregisters on exit."""

    def test_enter_registers_and_exit_unregisters(self) -> None:
        import builder.tools.hitl as hitl
        from builder.agents.progress_spinner import ProgressSpinner

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="culturing")
        with sp:
            assert hitl._active_animation is sp
        assert hitl._active_animation is None

    def test_enter_starts_status_and_exit_stops_it(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="centrifuging")
        with sp:
            assert "start" in st.events
        assert "stop" in st.events


class TestPause:
    """suspend_console_animation must pause the spinner so a HITL prompt is clean."""

    def test_pause_sets_flag_and_stops_status(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="x")
        sp.pause()
        assert sp._paused.is_set() is True
        assert "stop" in st.events

    def test_resume_clears_flag_and_starts_status(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="x")
        sp.pause()
        sp.resume()
        assert sp._paused.is_set() is False
        assert "start" in st.events

    def test_suspend_console_animation_pauses_then_resumes(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner
        from builder.tools.hitl import (
            register_console_animation,
            suspend_console_animation,
            unregister_console_animation,
        )

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="x")
        register_console_animation(sp)
        try:
            with suspend_console_animation():
                paused_in_body = sp._paused.is_set()
        finally:
            unregister_console_animation(sp)

        assert paused_in_body is True  # paused for the duration of the prompt
        assert sp._paused.is_set() is False  # resumed after

    def test_set_current_while_paused_does_not_repaint(self) -> None:
        """A set_current while paused records the op but does NOT touch the status."""
        from builder.agents.progress_spinner import ProgressSpinner

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="x")
        sp.pause()
        st.renders.clear()
        sp.set_current("draft_person")
        assert st.renders == []  # no repaint over the prompt
        assert "draft_person" in sp._render()  # but the op is remembered


class TestTickThread:
    """The daemon tick thread refreshes elapsed time and skips while paused."""

    def test_thread_is_daemon_and_joins_on_exit(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="x", tick_interval=0.01)
        with sp:
            assert sp._thread is not None
            assert sp._thread.daemon is True
            assert sp._thread.is_alive() is True
        assert sp._thread.is_alive() is False  # joined cleanly on exit

    def test_tick_repaints_while_running(self) -> None:
        import time

        from builder.agents.progress_spinner import ProgressSpinner

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="x", tick_interval=0.01)
        with sp:
            time.sleep(0.05)
        assert len(st.renders) >= 1  # the daemon thread repainted at least once

    def test_tick_skips_while_paused(self) -> None:
        import time

        from builder.agents.progress_spinner import ProgressSpinner

        st = _FakeStatus()
        sp = ProgressSpinner(console=_FakeConsole(st), phrase="x", tick_interval=0.01)
        with sp:
            sp.pause()
            time.sleep(0.03)
            renders_while_paused = len(st.renders)
            time.sleep(0.03)
            # No new repaints accumulated while paused.
            assert len(st.renders) == renders_while_paused
            sp.resume()


class TestNonTTY:
    """On a NON-terminal console (CI / piped) the spinner is a cheap no-op (#266).

    Rich's ``console.status`` opens a ``Live`` region backed by a background
    refresh thread; on a non-TTY that animation is invisible noise that, under
    CI's ``--timeout`` thread-dumper, only adds threads to start, stop and join.
    So when ``console.is_terminal`` is ``False`` the spinner must NOT open the
    Live region or start its own daemon tick thread, and every public method
    (``set_current`` / ``pause`` / ``resume`` / ``__enter__`` / ``__exit__``)
    must be a fast, safe no-op. On a real TTY it animates exactly as before.
    """

    def test_non_tty_never_opens_a_status(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner

        console = _NonTTYConsole()
        ProgressSpinner(console=console, phrase="x")
        assert console.status_calls == 0  # no Live region opened on a non-TTY

    def test_non_tty_starts_no_tick_thread(self) -> None:
        from builder.agents.progress_spinner import ProgressSpinner

        sp = ProgressSpinner(console=_NonTTYConsole(), phrase="x", tick_interval=0.01)
        with sp:
            # No daemon tick thread on a non-TTY (nothing to repaint).
            assert sp._thread is None or sp._thread.is_alive() is False

    def test_non_tty_methods_are_safe_noops(self) -> None:
        """All public methods complete instantly and never raise on a non-TTY."""
        from builder.agents.progress_spinner import ProgressSpinner

        sp = ProgressSpinner(console=_NonTTYConsole(), phrase="x", tick_interval=0.01)
        with sp:
            sp.set_current("scaffold_isa_backbone")  # no-op, no status to touch
            sp.pause()
            sp.resume()
        # Still rendarable / queryable after exit (pure UI helper).
        assert "x" in sp._render()

    def test_non_tty_registers_with_hitl_registry(self) -> None:
        """Even silenced, it registers so suspend_console_animation stays valid."""
        import builder.tools.hitl as hitl
        from builder.agents.progress_spinner import ProgressSpinner

        sp = ProgressSpinner(console=_NonTTYConsole(), phrase="x")
        with sp:
            assert hitl._active_animation is sp
        assert hitl._active_animation is None

    def test_non_tty_exit_does_not_hang(self) -> None:
        """A non-TTY enter/exit completes well within a tight bound (CI guard)."""
        import time

        from builder.agents.progress_spinner import ProgressSpinner

        sp = ProgressSpinner(console=_NonTTYConsole(), phrase="x", tick_interval=0.01)
        t0 = time.monotonic()
        with sp:
            sp.set_current("phase")
        assert time.monotonic() - t0 < 1.0  # instant: no Live, no thread to join
