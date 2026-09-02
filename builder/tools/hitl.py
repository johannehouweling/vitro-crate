"""Human-in-the-Loop interaction tools for the ISA-Tox RO-Crate Builder.

Provides a :class:`HumanInterface` protocol so frontends (Streamlit, FastAPI,
…) can be injected into :class:`~builder.engine.AgentEngine` without
monkeypatching. The default :class:`SimulatedHumanInterface` reproduces the
previous non-interactive stub behaviour (auto-approve, skip-input). The
module-level :func:`present_to_human` / :func:`request_input` functions remain
as thin wrappers over a shared default simulator for backward compatibility.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

logger = logging.getLogger(__name__)


# --- console animation suspension -------------------------------------------
# A long-running CLI animation (e.g. the ReAct agent loop's "thinking" spinner,
# a Rich ``Live`` region driven by a daemon thread) repaints the terminal
# continuously, which clobbers a blocking ``input()`` prompt — the user cannot
# read or answer a HITL question (scan-root approval, ask-user) while it ticks.
# The animation registers itself here; the console HITL prompts suspend it for
# the duration of ``input()``. Frontend-agnostic: a non-CLI HumanInterface never
# registers anything and these become no-ops.
_active_animation: Any = None
_animation_lock = threading.Lock()


def register_console_animation(animation: Any) -> None:
    """Register the active terminal animation so console prompts can pause it.

    ``animation`` must expose ``pause()`` and ``resume()``. The most recent
    registration wins (CLI animations are not nested).
    """
    global _active_animation
    with _animation_lock:
        _active_animation = animation


def unregister_console_animation(animation: Any) -> None:
    """Clear the registered animation, but only if it is *animation*."""
    global _active_animation
    with _animation_lock:
        if _active_animation is animation:
            _active_animation = None


@contextlib.contextmanager
def suspend_console_animation() -> Iterator[None]:
    """Pause any registered console animation for the duration of the block.

    Best-effort: no registered animation is a no-op, and a ``pause``/``resume``
    that raises is logged but never propagated — UI chrome must not break a HITL
    prompt. ``resume`` always runs, even if the body raises.
    """
    with _animation_lock:
        animation = _active_animation
    if animation is not None:
        try:
            animation.pause()
        except Exception:  # noqa: BLE001 — never let UI chrome break a prompt
            logger.debug("console animation pause failed", exc_info=True)
    try:
        yield
    finally:
        if animation is not None:
            try:
                animation.resume()
            except Exception:  # noqa: BLE001 — never let UI chrome break a prompt
                logger.debug("console animation resume failed", exc_info=True)


class HumanResponse(TypedDict):
    """Response from presenting content to a human for review.

    Attributes:
        action: One of "approved", "edited", "rejected", "skipped".
        comments: Free-text comments from the user, or None.
        edits: Dict of field edits (when action == "edited"), or None.
    """

    action: Literal["approved", "edited", "rejected", "skipped"]
    comments: str | None
    edits: dict | None


class InputResponse(TypedDict):
    """Response from requesting a specific input value from a human.

    Attributes:
        value: The user's input, or None if skipped.
        skipped: True if the user chose to skip.
    """

    value: Any | None
    skipped: bool


class MultiChoiceResponse(TypedDict):
    """Response from asking a human to pick ANY NUMBER of options.

    Distinct from :class:`HumanResponse` because the answer is a set, not a
    decision: "which of these entities should reference this organization?" can
    truthfully be answered with three of them, and a single-choice prompt forces
    the asker to either pick one wrongly or invent a link. ``values`` is empty
    when the user selected nothing.

    Attributes:
        values: The options the user selected, in the order they were offered.
        skipped: True if the user declined to answer at all (distinct from
            selecting nothing, which is a deliberate "none of these").
    """

    values: list[str]
    skipped: bool


# Sentinel ``purpose`` value marking a request to approve a NEW filesystem
# scan root. The default/simulated interface must DENY these — it can never be
# the approver for filesystem access (fail-closed, #197).
SCAN_ROOT_PURPOSE = "scan_root"


@runtime_checkable
class HumanInterface(Protocol):
    """Dependency-injection point for human-in-the-loop interaction.

    Implementations adapt the agent's HITL requests to a concrete frontend
    (CLI prompt, Streamlit widget, FastAPI round-trip, test double, …).

    An **optional** ``is_interactive: bool`` attribute is the single signal the
    interactive build path uses to decide whether to run the HITL guidance tail
    after the automated pipeline (AGENTS.md §14.6.1): a frontend backed by a REAL
    user sets it ``True``; the headless :class:`SimulatedHumanInterface` (and the
    A/B eval, batch runs, and tests that use it) leaves it ``False`` so guidance is
    never invoked non-interactively. It is **deliberately NOT a required Protocol
    member** — making it required would force every existing adapter / test double
    to declare it. Read it via the fail-closed :func:`is_interactive` helper, which
    treats an interface that omits it (or no interface at all) as non-interactive.
    """

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        """Present content to the human and return their decision.

        *purpose* optionally classifies the request (e.g. ``"scan_root"`` for a
        request to approve a new filesystem scan root). Implementations may use
        it to apply stricter handling to security-sensitive escalations.
        """
        ...

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        """Request a specific input value from the human."""
        ...

    # `select_many` is deliberately NOT declared here. Like `is_interactive`, it
    # is an OPTIONAL capability: requiring it would break every existing adapter
    # and test double the moment it was added. Callers must go through
    # :func:`select_many`, which detects support and degrades to a single-choice
    # prompt on frontends that do not offer one.


class SimulatedHumanInterface:
    """Default non-interactive interface: auto-approves and skips input.

    Reproduces the previous stub behaviour so headless/batch runs proceed
    without blocking on a real user — EXCEPT for scan-root escalations, which
    it denies: the simulator can never be the approver for filesystem access
    (fail-closed, #197).

    ``is_interactive`` is ``False`` — this is the headless default, so the
    interactive build path (AGENTS.md §14.6) never runs the HITL guidance tail
    behind it (the A/B eval and batch runs stay automated-only).
    """

    is_interactive: bool = False

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        """Log the presentation and return a simulated decision.

        Benign checkpoints (entity review, etc.) auto-approve as before. A
        scan-root escalation (``purpose == "scan_root"``) is DENIED — the
        non-interactive default must not silently widen filesystem access.
        """
        logger.info("HITL presentation: %s", context)
        if options:
            logger.info("HITL options: %s", options)
        if purpose == SCAN_ROOT_PURPOSE:
            logger.warning(
                "Denying simulated approval of a new scan root (fail-closed): %s",
                context,
            )
            return {"action": "rejected", "comments": None, "edits": None}
        return {"action": "approved", "comments": None, "edits": None}

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        """Log the request and return a skip response."""
        logger.info("HITL input request: %s (type=%s)", prompt, field_type)
        return {"value": None, "skipped": True}

    def select_many(
        self,
        context: str,
        options: list[str],
        purpose: str | None = None,
    ) -> MultiChoiceResponse:
        """Log the request and skip — the simulator never picks on a user's behalf.

        Declared (rather than left to the degrade path) so a headless run does
        not fall through to :meth:`present`, whose auto-approval would look like
        the user having chosen the first option.
        """
        logger.info("HITL multi-choice request: %s (%d options)", context, len(options or []))
        return {"values": [], "skipped": True}


def _default_console_prompt(field_type: str) -> str:
    """Plain-terminal input reader — the default when no UI box is injected.

    Kept here (not in :mod:`builder.agents.ui`) so ``hitl`` stays independent of
    the UI layer: importing ``builder.agents.ui`` from a ``builder.tools`` module
    would form an ``agents → tools → agents`` import cycle. The CLI injects the
    shared rounded ``❯`` box via ``ConsoleHumanInterface(prompt_func=...)`` instead.
    """
    return input(f"({field_type}) > ")


def _default_console_show(text: str) -> None:
    """Plain-terminal question display — the default when no styled renderer is
    injected. The CLI injects a renderer that styles the question as a green-●
    reply (:func:`builder.agents.ui.render_reply`); the injection keeps ``hitl``
    free of a ``builder.agents.ui`` import (no ``agents → tools → agents`` cycle).
    """
    print(text)


# The choices used when a caller presents a decision without naming its own.
# Stated as full sentences: "yes"/"no" alone forced the user to re-read the
# question to work out what a bare "yes" would agree to.
_APPROVE_CHOICES = ["Yes, go ahead", "No, don't do that"]

# The scan-root escalation names the consequence instead of asking yes/no, and
# lists the refusal FIRST so the pre-selected answer denies (#197 fail-closed).
_DENY_ALLOW_CHOICES = ["No, keep the current access", "Yes, allow this folder"]

# The row the console appends to every choice prompt (#596), so an answer the
# caller did not foresee can still be given, in the user's own words. Last, so
# the pre-selected row and the number keys of the offered choices are unchanged.
OWN_ANSWER_CHOICE = "Something else — let me type an answer"

_AFFIRMATIVE = {"y", "yes", "approve", "approved", "ok", "okay", "confirm", "continue"}
_NEGATIVE = {"n", "no", "reject", "rejected", "deny", "decline", "cancel"}


def choice_stance(choice: str) -> bool | None:
    """Whether *choice* is a plain yes (True) / no (False), else ``None``.

    Recognises both a bare word (the ``["yes", "no"]`` options callers pass) and
    the sentence forms above, by reading the leading word. A real menu entry
    ("Use different names") matches neither and comes back ``None`` so it is
    returned to the caller as a selection rather than collapsed to a verdict.
    """
    lead = choice.strip().casefold().replace(",", " ").split()
    if not lead:
        return None
    if lead[0] in _AFFIRMATIVE:
        return True
    if lead[0] in _NEGATIVE:
        return False
    return None


def _default_choice_index(choices: list[str], *, deny_by_default: bool) -> int:
    """Index to pre-select: the refusal when denying by default, else the first.

    Fail-closed matters more than convenience here — for a scan-root escalation
    the pre-selected row must be one that does NOT widen access, so an
    absent-minded Enter cannot grant it. When no negative choice can be
    identified, nothing is safe to pre-approve, so the last option is used.
    """
    if not deny_by_default:
        return 0
    for index, choice in enumerate(choices):
        if choice_stance(choice) is False:
            return index
    return len(choices) - 1


def _decision_from_choice(answer: str, *, deny_by_default: bool) -> HumanResponse:
    """Map a SELECTED choice to the response its frontend must return.

    Shared by every interface that resolves a decision to one of the offered
    choices (the console user arrowing to a row, the smoke-test interface taking
    the pre-selected one), so the two can never drift apart: "confirm the
    pre-selection" is only a meaningful contract while both read a selected row
    the same way — most of all the ``deny_by_default`` line, where anything that
    is not an explicit affirmative must deny (#197).
    """
    stance = choice_stance(answer)
    if stance is not None:
        # A plain yes/no choice is a decision, not a payload: returning "no"
        # as comments used to read as an APPROVAL carrying the text "no".
        return {
            "action": "approved" if stance else "rejected",
            "comments": None,
            "edits": None,
        }
    if deny_by_default:
        # Anything that is not an explicit affirmative denies (#197).
        return {"action": "rejected", "comments": None, "edits": None}
    # A real menu choice: hand the selected option back so callers can act on
    # WHICH option was picked (e.g. an ambiguous publication author).
    return {"action": "approved", "comments": answer, "edits": None}


def match_choice(raw: str, choices: list[str], default: int) -> int | None:
    """Resolve a typed answer to a choice index (``None`` = no match).

    Accepts what people actually type at a prompt like this: nothing (the
    default), a number, or a yes/no word — ``y`` still means yes even though the
    choices are now sentences, so muscle memory keeps working. Also matches a
    leading-word prefix of a choice, so ``rev`` picks "Revise the names".
    """
    answer = raw.strip().casefold()
    if not answer:
        return default
    if answer.isdigit() and 1 <= int(answer) <= len(choices):
        return int(answer) - 1
    stance = choice_stance(answer)
    if stance is not None:
        for index, choice in enumerate(choices):
            if choice_stance(choice) is stance:
                return index
    for index, choice in enumerate(choices):
        if choice.strip().casefold().startswith(answer):
            return index
    return None


def _default_console_select(choices: list[str], default: int) -> int | None:
    """Plain-terminal chooser — the default when no UI selector is injected.

    Prints the numbered choices with the default marked and reads one line;
    empty takes the default. The CLI injects the arrow-navigable rounded box
    (:func:`builder.agents.ui.select_option`) instead.
    """
    for index, choice in enumerate(choices, start=1):
        marker = "❯" if index - 1 == default else " "
        print(f" {marker} {index}. {choice}")
    try:
        raw = input(f"Select [1-{len(choices)}, Enter = {default + 1}]: ")
    except EOFError:
        return None
    return match_choice(raw, choices, default)


def _default_console_select_many(context: str, choices: list[str]) -> list[int] | None:
    """Plain-terminal many-of-N chooser — the default when no UI box is injected.

    Reads one line of numbers (comma- or space-separated); an empty line means
    "none of these", which is a real answer rather than a cancel. The CLI injects
    the checkbox box (:func:`builder.agents.ui.select_options`) instead.
    """
    print(context)
    for index, choice in enumerate(choices, start=1):
        print(f"   {index}. {choice}")
    try:
        raw = input(f"Select any [1-{len(choices)}, comma-separated; Enter = none]: ")
    except EOFError:
        return None
    picked: list[int] = []
    for token in raw.replace(",", " ").split():
        if token.isdigit() and 1 <= int(token) <= len(choices):
            position = int(token) - 1
            if position not in picked:
                picked.append(position)
    return picked


class ConsoleHumanInterface:
    """A REAL interactive HITL interface that prompts on the terminal (stdin).

    This is the CLI frontend the **default interactive build path** runs behind
    (`main.py --interactive` → `run_interactive_build`, AGENTS.md §14.6.1). Unlike
    :class:`SimulatedHumanInterface` it is ``is_interactive = True``, so the
    guidance tail actually runs and routes its ask-user prompts / draft
    confirmations to the user.

    The free-text prompt is read through an injectable ``prompt_func`` (a
    ``field_type -> text`` reader) and the question is displayed through an
    injectable ``show_func`` (a ``text -> None`` renderer). Both default to plain
    terminal I/O so this module never imports the UI layer (avoiding an
    ``agents → tools → agents`` cycle); the CLI injects
    :func:`builder.agents.ui.boxed_input` for the box and
    :func:`builder.agents.ui.render_reply` for the question, so the pipeline's HITL
    prompt renders through the SAME rounded box and green-● styling the ReAct arm
    uses (#344).

    A scan-root escalation still routes through the user — they are the only
    legitimate approver for widening filesystem access (#197); a non-affirmative
    answer denies it (fail-closed). An empty answer to an input request is a skip.
    Reading from a closed / non-tty stdin (``EOFError``) is treated as decline /
    skip so the loop never hangs or crashes on a piped invocation.
    """

    is_interactive: bool = True

    def __init__(
        self,
        prompt_func: Callable[[str], str] | None = None,
        show_func: Callable[[str], None] | None = None,
        select_func: Callable[[list[str], int], int | None] | None = None,
        select_many_func: Callable[[str, list[str]], list[int] | None] | None = None,
    ) -> None:
        """Build the interface, optionally injecting the prompt reader + display.

        Args:
            prompt_func: A ``field_type -> entered-text`` reader used by
                :meth:`request_input`. Defaults to :func:`_default_console_prompt`
                (a plain ``input()``). The CLI passes a reader bound to the shared
                rounded box (:func:`builder.agents.ui.boxed_input`).
            show_func: A ``text -> None`` renderer that displays the question.
                Defaults to :func:`_default_console_show` (a plain ``print``). The
                CLI passes a renderer that styles it as a green-● reply
                (:func:`builder.agents.ui.render_reply`).
            select_func: A ``(choices, default_index) -> index | None`` chooser
                used by :meth:`present`. Defaults to
                :func:`_default_console_select` (numbered lines + ``input()``).
                The CLI passes the arrow-navigable rounded box
                (:func:`builder.agents.ui.select_option`), so a decision and a
                free-text answer look like the same control.
            select_many_func: A ``(hint, choices) -> indices | None`` chooser used
                by :meth:`select_many` for questions with several right answers
                at once. Defaults to :func:`_default_console_select_many`
                (numbered lines, comma-separated reply). The CLI passes the
                checkbox box (:func:`builder.agents.ui.select_options`).
        """
        self._read: Callable[[str], str] = prompt_func or _default_console_prompt
        self._show: Callable[[str], None] = show_func or _default_console_show
        self._select: Callable[[list[str], int], int | None] = (
            select_func or _default_console_select
        )
        self._select_many: Callable[[str, list[str]], list[int] | None] = (
            select_many_func or _default_console_select_many
        )
        self._done = False

    def is_done(self) -> bool:
        """Whether the user requested that guidance stop and the crate be built."""
        return self._done

    @staticmethod
    def _is_stop_command(value: str) -> bool:
        normalized = " ".join(value.casefold().split())
        stop_words = {
            "stop",
            "done",
            "exit",
            "quit",
            "build",
            "build the crate",
            "build the rocrate",
        }
        return normalized in stop_words or normalized.startswith("can you stop here")

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        """Show *context* and read a decision as a single navigable choice.

        One question, not two. The previous prompt printed a numbered menu and
        then asked ``Approve? [Y/n]``, so the answer could plausibly mean either
        "yes to the whole thing" or "option 1" — and typing ``2`` was read as an
        approval whatever option 2 actually said. Here the choices ARE the
        question: the expected answer starts selected and the user arrows to
        another, so the decision is unambiguous both ways.

        Every prompt but a scan-root escalation ends with :data:`OWN_ANSWER_CHOICE`
        (#596): picking it reads the free-text box, and the text comes back as an
        ``edited`` decision — in ``comments`` and in ``edits["value"]`` — so a
        caller that only knows its offered rows sees neither an approval nor a
        rejection it was not given. An empty line there is a skip.

        Fail-closed is preserved for a scan-root escalation: its default lands on
        the denying choice, so an accidental Enter never widens filesystem
        access. It is also the one prompt whose choices are stated as an explicit
        allow/deny rather than a yes/no — and the one with no free-text row,
        because widening access is a decision, not prose (#197).
        """
        deny_by_default = purpose == SCAN_ROOT_PURPOSE
        choices = list(options or [])
        if not choices:
            choices = _DENY_ALLOW_CHOICES if deny_by_default else _APPROVE_CHOICES
        # The safe answer is first except when denying by default, where it is
        # the refusal — whatever it is, it must be the pre-selected row.
        default_index = _default_choice_index(choices, deny_by_default=deny_by_default)
        own_answer = not deny_by_default
        if own_answer:
            choices = [*choices, OWN_ANSWER_CHOICE]

        # Suspend any active terminal spinner so the prompt is readable and stdin
        # is not fighting a Rich Live repaint (ReAct loop scan-root approval).
        with suspend_console_animation():
            self._show(context)
            chosen = self._select(choices, default_index)
            if own_answer and chosen == len(choices) - 1:
                typed = self._read_answer("text")
                if typed is None:
                    return {"action": "skipped", "comments": None, "edits": None}
                return {"action": "edited", "comments": typed, "edits": {"value": typed}}

        if chosen is None:
            # Cancelled / EOF: decline without ending guidance, and fail closed.
            return {"action": "rejected", "comments": None, "edits": None}
        answer = choices[chosen]
        if self._is_stop_command(answer):
            self._done = True
            return {"action": "skipped", "comments": None, "edits": None}

        return _decision_from_choice(answer, deny_by_default=deny_by_default)

    def select_many(
        self,
        context: str,
        options: list[str],
        purpose: str | None = None,
    ) -> MultiChoiceResponse:
        """Ask for any number of *options* at once (see :func:`select_many`).

        Confirming with nothing ticked is a real answer — "none of these" — and
        returns empty values with ``skipped`` False. Cancelling (Esc / EOF) is
        the skip. A scan-root escalation never routes here: widening filesystem
        access is a single fail-closed decision, not a pick-list.
        """
        if purpose == SCAN_ROOT_PURPOSE:
            return {"values": [], "skipped": True}
        choices = [c for c in (options or []) if c]
        if not choices:
            return {"values": [], "skipped": True}
        with suspend_console_animation():
            picked = self._select_many(context, choices)
        if picked is None:
            return {"values": [], "skipped": True}
        return {
            "values": [choices[i] for i in picked if 0 <= i < len(choices)],
            "skipped": False,
        }

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        """Prompt the user for a value; an empty answer (or EOF) is a skip.

        Displays the question via the injected ``show_func`` (a green-● reply in
        the CLI, a plain ``print`` otherwise) and reads via the injected
        ``prompt_func`` (the shared rounded box in the CLI), suspending any active
        terminal spinner so stdin is not fighting a Rich Live repaint.
        """
        with suspend_console_animation():
            self._show(prompt)
            value = self._read_answer(field_type)
        if value is None:
            return {"value": None, "skipped": True}
        return {"value": value, "skipped": False}

    def _read_answer(self, field_type: str) -> str | None:
        """One line from the box; ``None`` is a skip — empty, EOF, or a stop word.

        EOF and a stop word also end guidance (:meth:`is_done`): a closed stdin
        will never answer anything else, and "build" means build.
        """
        try:
            value = self._read(field_type).strip()
        except EOFError:
            self._done = True
            return None
        if self._is_stop_command(value):
            self._done = True
            return None
        return value or None


# The single literal every open field gets in smoke-test mode. It is affirmative
# (an open "shall I go on?" question reads correctly) and it is obviously not a
# person's name, a study description or a protocol — so the placeholder is
# recognisable as one in the crate afterwards, without the mode having to write
# any marker INTO the crate (that would be fabricating metadata, D5).
SMOKE_TEST_ANSWER = "yes, continue"

# The ``field_type`` a frontend is asked with when the question is not a metadata
# field at all but the CONVERSATION itself — the ReAct loop's "what next?"
# prompt. A console frontend cannot tell the difference and should not try: it
# reads a line either way. It exists so an interface that answers ITSELF can,
# because that channel is the one place where "answer everything affirmatively"
# does not terminate on its own (see `SmokeTestHumanInterface.request_input`).
CONVERSATION_FIELD_TYPE = "conversation"

# How many conversational turns a smoke test drives the ReAct loop for before
# ending the session. Small on purpose: the mode proves the loop RUNS unattended
# — reads a turn, acts, comes back for the next — and each extra turn is a real
# model call spent re-proving it. Three is enough to show the come-back-for-more
# behaviour that one turn cannot.
#
# Superseded by a wall-clock budget when one is given (``--smoke-test 20``): a
# turn count is a poor stand-in for "run for a while and then export", since turn
# cost varies by an order of magnitude between a one-tool answer and a full
# lookup fan-out.
SMOKE_TEST_CONVERSATION_TURNS = 3

# Printed at the start of a smoke-test run and again beside the exported crate
# path. Both, deliberately: the opening line makes an accidental ``--smoke-test``
# obvious before the build spends anything, and the closing line means anyone
# reading scrollback — or a CI log — later sees WHAT the crate next to it is.
SYNTHETIC_ANSWER_NOTICE = (
    "SMOKE TEST — THE ANSWERS IN THIS RUN ARE SYNTHETIC, NOT A PERSON'S.\n"
    "Every choice prompt confirms its pre-selected option and every open field "
    f'is answered "{SMOKE_TEST_ANSWER}".\n'
    "Anything this run recorded as prose (a name, a description) is that "
    "placeholder — the crate proves the interactive path runs, it is not "
    "curated metadata."
)


class SmokeTestHumanInterface:
    """Drives the INTERACTIVE build with nobody at the keyboard (``--smoke-test``).

    A test harness, not a curation frontend: it exists so the HITL path — the
    guidance tail included — can be exercised end to end unattended. The crate it
    produces is a by-product; its prose fields hold :data:`SMOKE_TEST_ANSWER`.

    * ``is_interactive`` is ``True``, and that is the whole point. The tail is
      gated on this one signal (AGENTS.md §14.6.1), which is exactly why
      :class:`SimulatedHumanInterface` — ``is_interactive = False`` — cannot be
      used to exercise it: behind the simulator ``run_interactive_build`` degrades
      to ``run_pipeline`` + export and ``run_guidance`` is never called.
    * ``synthesizes_answers`` is ``True`` so the build can SAY so next to the
      crate it wrote (see :func:`answers_are_synthetic`). Nothing about the
      run is marked inside the crate itself — writing "this was a smoke test"
      into the metadata would be fabricating metadata, which D5 forbids.

    **Scan roots are refused outright**, before any choice is consulted. The
    pre-selection rule happens to deny them too, but relying on that was wrong:
    :func:`_default_choice_index` falls back to the LAST option when no choice is
    recognisably negative, and a caller offering
    ``["Show me the folder first", "Yes, allow this folder"]`` therefore had this
    mode approving filesystem access. No production caller passes options today —
    which is precisely why the bug was invisible. A test harness must never be
    the approver for filesystem access (#197), so that is stated here directly
    rather than inherited from a rule written for a human at a keyboard.

    **A real menu is skipped, not answered.** Confirming a pre-selection means
    taking the answer an Enter would give; picking row 1 of a list of candidate
    people is *inventing* one. The only such menu in the tree is the ambiguous
    -author escalation, where answering it would have this mode silently
    asserting which human wrote a paper — the same line :meth:`select_many`
    declines to cross.
    """

    is_interactive: bool = True
    synthesizes_answers: bool = True

    def __init__(
        self,
        conversation_turns: int = SMOKE_TEST_CONVERSATION_TURNS,
        minutes: float | None = None,
    ) -> None:
        """Args:
        conversation_turns: How many turns to drive a conversational loop
            (the ReAct arm) before ending the session. Ignored by the
            default arm, which has no conversational channel, and superseded
            entirely when *minutes* is given. ``<= 0`` ends it at the first
            prompt.
        minutes: Optional wall-clock budget (``--smoke-test 20``). Once it is
            spent, both arms wind down at their next question and export what
            they have. Measured on :func:`time.monotonic` from construction —
            not a wall date — so a clock adjustment mid-run cannot end a
            session early or extend one forever.
        """
        self._conversation_budget = int(conversation_turns)
        self._deadline: float | None = (
            time.monotonic() + float(minutes) * 60.0 if minutes else None
        )

    def _time_is_up(self) -> bool:
        """Whether a wall-clock budget was given and has been spent.

        ``False`` when none was given: an unbudgeted run is bounded by the turn
        count and by each loop's own guards, exactly as before.
        """
        return self._deadline is not None and time.monotonic() >= self._deadline

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        """Confirm the PRE-SELECTED choice — the same row an Enter would take.

        Reuses :func:`_default_choice_index` rather than "the first option" so the
        mode answers whatever the console frontend would have offered as its
        default, including the fail-closed refusal on a scan-root escalation. A
        reimplementation here would quietly grant filesystem access the moment
        that rule changed.
        """
        if purpose == SCAN_ROOT_PURPOSE:
            # Refused here, not left to the pre-selection: that rule falls back to
            # the LAST option when nothing reads as a refusal, so a caller passing
            # ["Show me the folder first", "Yes, allow this folder"] got an
            # approval out of this mode. Fail-closed for filesystem access is not
            # something a test harness may inherit by coincidence (#197).
            logger.warning("smoke-test: refusing a scan-root escalation (fail-closed): %s", context)
            return {"action": "rejected", "comments": None, "edits": None}

        choices = list(options or []) or list(_APPROVE_CHOICES)
        answer = choices[_default_choice_index(choices, deny_by_default=False)]
        if choice_stance(answer) is None:
            # Not a yes/no: this is a menu of alternatives (candidate authors,
            # say), and no row is pre-selected in any meaningful sense. Returning
            # row 1 would make the harness assert something — which candidate is
            # the real person — rather than confirm something.
            logger.info("smoke-test: skipping a menu it cannot confirm: %s", context)
            return {"action": "skipped", "comments": None, "edits": None}
        logger.info("smoke-test: confirming the pre-selected choice %r for: %s", answer, context)
        return _decision_from_choice(answer, deny_by_default=False)

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        """Answer every open field with :data:`SMOKE_TEST_ANSWER`.

        A real answer, not a skip: skipping would leave the guidance loop with
        nothing to commit, so the commit → re-assess → next-gap path this mode
        exists to exercise would never run.

        **:data:`CONVERSATION_FIELD_TYPE` is the exception, and it is why this
        class needs state at all.** Every other channel terminates on its own:
        the guidance loop runs out of gaps, the report runs out of findings,
        ``max_rounds`` bounds the rest. A conversational loop has no such bound —
        it ends when the person says so, and :data:`SMOKE_TEST_ANSWER` is not a
        stop word, so answering it affirmatively forever is an infinite run
        spending a model call per turn. After :data:`SMOKE_TEST_CONVERSATION_TURNS`
        the answer becomes a skip, which the loop reads as end-of-input and
        finalises on — the same ending Ctrl+D gives, so the crate is still
        exported. Budget exhaustion is deliberately NOT ``is_done()``: that
        answers a different question (may guidance stop early?) whose honest
        answer here stays "no".

        An identifier field is safe to answer this way because the value cannot
        become one: the guidance tail routes every reply through
        ``_deterministic_decision`` / the interpret leaf, both of which force an
        identifier-bearing field to a skip (D5), and the citation-author path
        re-verifies any pasted ORCID against a real lookup before use.
        """
        if field_type == CONVERSATION_FIELD_TYPE:
            if self._time_is_up():
                logger.info("smoke-test: time budget spent — ending the session")
                return {"value": None, "skipped": True}
            if self._deadline is not None:
                # A clock was given, so it is the bound: the turn count would
                # otherwise stop a `--smoke-test 20` run after three turns, which
                # is the opposite of what asking for twenty minutes means.
                logger.info("smoke-test: driving a conversational turn (on the clock)")
                return {"value": SMOKE_TEST_ANSWER, "skipped": False}
            if self._conversation_budget <= 0:
                logger.info("smoke-test: conversation budget spent — ending the session")
                return {"value": None, "skipped": True}
            self._conversation_budget -= 1
            logger.info(
                "smoke-test: driving a conversational turn with %r (%d left after this)",
                SMOKE_TEST_ANSWER,
                self._conversation_budget,
            )
            return {"value": SMOKE_TEST_ANSWER, "skipped": False}
        logger.info(
            "smoke-test: answering %r with the synthetic %r (type=%s)",
            prompt,
            SMOKE_TEST_ANSWER,
            field_type,
        )
        return {"value": SMOKE_TEST_ANSWER, "skipped": False}

    def select_many(
        self,
        context: str,
        options: list[str],
        purpose: str | None = None,
    ) -> MultiChoiceResponse:
        """Skip — a multi-select has no pre-selection to confirm.

        Nothing is pre-ticked in a many-of-N box, so there is no "the answer an
        Enter would give" to stand in for; picking some subset would be this mode
        inventing an answer rather than taking the offered one, which is the line
        :class:`SimulatedHumanInterface` also declines to cross. Declared (not
        left to the degrade path in :func:`select_many`) so the question does not
        fall through to :meth:`present`, whose pre-selection would look like the
        user having deliberately ticked the first box.
        """
        logger.info(
            "smoke-test: skipping multi-choice request: %s (%d options)",
            context,
            len(options or []),
        )
        return {"values": [], "skipped": True}

    def is_done(self) -> bool:
        """End guidance only when a wall-clock budget has been spent.

        Without one this is always ``False``, and deliberately so: ending at the
        first opportunity would exercise nothing — the tail would return before
        asking anything, which is precisely the path this mode is for. An
        unbudgeted run therefore leans on ``run_guidance``'s own termination
        guards (the report exhausted, no gap making progress, the hard
        ``max_rounds`` bound), which already guarantee termination without a
        cooperating frontend.

        A ``--smoke-test 20`` run is the exception, and it is the honest answer
        to the question this method actually asks — "does the user want to stop
        now?" — because the user said so up front, in minutes. ``run_guidance``
        consults this at the TOP of every round, so the deadline lands between
        gaps and the crate is exported with whatever was answered, never
        mid-question with a half-applied edit.
        """
        return self._time_is_up()


def answers_are_synthetic(human: HumanInterface | None) -> bool:
    """Whether *human* makes its answers up instead of asking a person.

    Read fail-closed like :func:`is_interactive`: an interface that does not
    declare ``synthesizes_answers`` is assumed to be relaying a real person, so a
    build only claims "these answers are synthetic" when the frontend says so.
    Used by the interactive build to print :data:`SYNTHETIC_ANSWER_NOTICE` beside
    the exported crate path.
    """
    return bool(getattr(human, "synthesizes_answers", False))


def supports_multi_choice(human: HumanInterface | None) -> bool:
    """Whether *human* can take a many-of-N answer natively."""
    return callable(getattr(human, "select_many", None))


def select_many(
    human: HumanInterface | None,
    context: str,
    options: list[str],
    *,
    purpose: str | None = None,
) -> MultiChoiceResponse:
    """Ask *human* to pick any number of *options* — none, one, or several.

    Some questions genuinely have several right answers at once. Forcing them
    through the single-choice prompt makes the asker choose between recording
    one of the true answers or none, and an agent that needs "which of these
    does this apply to?" has no way to say "these three".

    Frontends that implement ``select_many`` answer natively. Anything else —
    including every adapter written before this existed — degrades to the
    single-choice :meth:`~HumanInterface.present` prompt, whose one answer is
    returned as a one-element list. Degrading is why this is a function rather
    than a Protocol method: the capability is optional, and callers should not
    have to ask which frontend they are talking to.

    A ``None`` interface (headless) skips, consistent with :func:`is_interactive`
    being fail-closed.
    """
    choices = [c for c in (options or []) if c]
    if human is None or not choices:
        return {"values": [], "skipped": True}

    native = getattr(human, "select_many", None)
    if callable(native):
        result = native(context, choices, purpose)
        # Trust the frontend's answer but never its bookkeeping: an option the
        # user did not have on offer must not enter the crate.
        offered = set(choices)
        values = [v for v in (result.get("values") or []) if v in offered]
        return {"values": values, "skipped": bool(result.get("skipped")) and not values}

    response = human.present(context, choices, purpose)
    if response.get("action") not in ("approved", "edited"):
        return {"values": [], "skipped": True}
    answer = response.get("comments")
    return {"values": [answer], "skipped": False} if answer in choices else {
        "values": [],
        "skipped": True,
    }


def is_interactive(human: HumanInterface | None) -> bool:
    """Whether *human* is a REAL interactive frontend (vs headless/simulated).

    The single gate the interactive build path uses to decide whether to run the
    HITL guidance tail after the automated pipeline (AGENTS.md §14.6). Reads the
    optional :attr:`HumanInterface.is_interactive` signal **fail-closed**: a
    ``None`` interface (a headless engine), an interface that does not declare the
    attribute, or one that declares it falsy is treated as **non-interactive**.
    Only an interface that explicitly sets ``is_interactive`` truthy — a frontend
    backed by a real user — returns ``True``.
    """
    return bool(getattr(human, "is_interactive", False))


# Shared default simulator backing the module-level convenience functions.
_default_interface: HumanInterface = SimulatedHumanInterface()


def present_to_human(
    context: str,
    options: list[str] | None = None,
    purpose: str | None = None,
) -> HumanResponse:
    """Present content to the human via the default simulated interface.

    Backward-compatible wrapper; new code should inject a
    :class:`HumanInterface` into :class:`~builder.engine.AgentEngine` instead.
    """
    return _default_interface.present(context, options, purpose)


def request_input(
    prompt: str,
    field_type: str = "text",
) -> InputResponse:
    """Request input from the human via the default simulated interface.

    Backward-compatible wrapper; new code should inject a
    :class:`HumanInterface` into :class:`~builder.engine.AgentEngine` instead.
    """
    return _default_interface.request_input(prompt, field_type)


__all__ = [
    "CONVERSATION_FIELD_TYPE",
    "OWN_ANSWER_CHOICE",
    "SCAN_ROOT_PURPOSE",
    "SMOKE_TEST_ANSWER",
    "SMOKE_TEST_CONVERSATION_TURNS",
    "SYNTHETIC_ANSWER_NOTICE",
    "ConsoleHumanInterface",
    "HumanInterface",
    "HumanResponse",
    "InputResponse",
    "MultiChoiceResponse",
    "SimulatedHumanInterface",
    "SmokeTestHumanInterface",
    "answers_are_synthetic",
    "choice_stance",
    "is_interactive",
    "present_to_human",
    "request_input",
    "select_many",
    "supports_multi_choice",
]
