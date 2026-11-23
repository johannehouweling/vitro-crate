"""What discovery ranks must be what the agent is told (#675).

The ReAct loop is told the ranked documents by
:func:`builder.agents.react.agent_loop._format_document_context`. It once sliced
the list to a hardcoded 20, so a deposit with more ranked candidates hid the rest:
on S-VHPS22 (1468 files) discovery ranks 40, balanced across the four classes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.hitl import SimulatedHumanInterface

pytestmark = pytest.mark.timeout(300)

FIXTURE = Path("tests/fixtures/svhps22_real_input")


def _engine() -> AgentEngine:
    engine = AgentEngine(state=CrateState(), human_interface=SimulatedHumanInterface())
    engine.initialize(input_path=str(FIXTURE.resolve()))
    return engine


@pytest.fixture(scope="module")
def discovered() -> list[dict]:
    """The ranked documents discovery left on the state."""
    if not FIXTURE.exists():  # pragma: no cover - fixture not checked out
        pytest.skip("S-VHPS22 fixture not available")
    return list(_engine().state.documents)


def test_the_fixture_ranks_more_than_the_old_hardcoded_cap(discovered) -> None:
    """Otherwise every assertion below passes on a list that never exceeded 20."""
    assert len(discovered) > 20, len(discovered)


class TestTheAgentIsToldThemAll:
    """The loop renders its own list from the ranking, which it once sliced to 20."""

    def test_the_ranked_list_names_every_candidate(self, discovered) -> None:
        from builder.agents.react.agent_loop import _format_document_context

        documents = discovered

        listing = _format_document_context(documents)

        missing = [d["relative_path"] for d in documents if d["relative_path"] not in listing]
        assert not missing, f"{len(missing)} of {len(documents)} never named: {missing[:5]}"

    def test_the_cap_has_one_home(self, discovered) -> None:
        """A hardcoded 20 downstream could disagree with the ranking's own cap —
        and did, halving it. Asserted on what the listing NAMES rather than on the
        source text: a grep for the old slice matches its own obituary in a
        comment, and would pass while the behaviour regressed.
        """
        from builder.agents.react.agent_loop import _format_document_context
        from builder.tools.document_discovery import MAX_DOCUMENT_CANDIDATES

        documents = discovered

        assert MAX_DOCUMENT_CANDIDATES > 20
        assert len(documents) <= MAX_DOCUMENT_CANDIDATES
        # The listing tracks the ranking's length rather than saturating at 20.
        assert _format_document_context(documents).count("\n") + 1 == len(documents)
