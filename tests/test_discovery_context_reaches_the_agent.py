"""What discovery bounds must be what the agent receives (#675).

`engine._run_document_discovery` built the bounded context and threw it away —
the string was used for its LENGTH in a log line and nothing else. Both arms
rendered their own from `state.documents` instead, each slicing to a hardcoded
20, so everything `format_document_context` does stopped at the engine:

* the max-min fair character budget (#587),
* the header guard that refuses an entry too small to name its own file (#591),
* the slot allocation across the four classes (#595).

Measured on S-VHPS22 (1468 files) before the fix: discovery produced 40
candidates balanced 13 metadata / 13 processed / 12 protocol / 2 raw; the
pipeline sent 26 587 characters — against an 18 000 budget — carrying 4 / 13 /
**1** / 2. #595's headline result never reached a model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.agents.pipeline.pipeline import _gather_context
from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.document_discovery import _MAX_CONTEXT_CHARS
from builder.tools.hitl import SimulatedHumanInterface

pytestmark = pytest.mark.timeout(300)

FIXTURE = Path("tests/fixtures/svhps22_real_input")

_HEADING = "Discovered documentation:"


def _engine() -> AgentEngine:
    engine = AgentEngine(state=CrateState(), human_interface=SimulatedHumanInterface())
    engine.initialize(input_path=str(FIXTURE.resolve()))
    return engine


@pytest.fixture(scope="module")
def discovered() -> tuple[list[dict], str]:
    """The ranked documents, and the documents block the pipeline actually sends."""
    if not FIXTURE.exists():  # pragma: no cover - fixture not checked out
        pytest.skip("S-VHPS22 fixture not available")
    engine = _engine()
    context = _gather_context(engine)
    block = context.split(_HEADING, 1)[-1] if _HEADING in context else ""
    return list(engine.state.documents), block


class TestTheAgentGetsWhatDiscoveryDecided:
    def test_the_fixture_ranks_more_than_the_old_hardcoded_cap(self, discovered) -> None:
        """Otherwise every assertion below passes on a list that never exceeded 20."""
        documents, _ = discovered

        assert len(documents) > 20, len(documents)

    def test_every_ranked_candidate_is_named(self, discovered) -> None:
        """A file the agent is not told exists is a file it cannot read. The cap
        belongs to `discover_documents`, not to a literal 20 downstream that
        silently halves it."""
        documents, block = discovered

        missing = [
            d["relative_path"]
            for d in documents
            if d.get("filename") not in block and d.get("relative_path") not in block
        ]

        assert not missing, f"{len(missing)} of {len(documents)} never named: {missing[:5]}"

    def test_the_documents_block_respects_the_budget(self, discovered) -> None:
        """`preview[:2000]` per document with no ceiling sent 26 587 characters
        against an 18 000 budget. `_fair_shares` exists to stop exactly that.

        Measured over the entries, excluding the trailing "(N of M scanned files
        shown)" note — `format_document_context` appends that after the split,
        and `test_the_ceiling_still_holds` already draws the line there.
        """
        _, block = discovered

        entries = block.split("\n\n(", 1)[0]

        assert len(entries) <= _MAX_CONTEXT_CHARS, len(entries)

    def test_the_classes_discovery_balanced_survive(self, discovered) -> None:
        """#595 allocated the slots across the four classes; taking the top 20 by
        score threw the balance away — 12 protocols became 1."""
        documents, block = discovered

        named = {
            d["classification"]
            for d in documents
            if d.get("filename") in block or d.get("relative_path") in block
        }

        assert named == {c["classification"] for c in documents}, named


class TestTheReactArmNamesThemAllToo:
    """The other arm rendered its own list and sliced it to 20 as well."""

    def test_the_ranked_list_names_every_candidate(self, discovered) -> None:
        from builder.agents.react.agent_loop import _format_document_context

        documents, _ = discovered

        listing = _format_document_context(documents)

        missing = [d["relative_path"] for d in documents if d["relative_path"] not in listing]
        assert not missing, f"{len(missing)} of {len(documents)} never named: {missing[:5]}"

    def test_the_cap_has_one_home(self, discovered) -> None:
        """Two hardcoded 20s downstream could disagree with the ranking's own cap
        — and did, halving it. Asserted on what each renderer NAMES rather than
        on the source text: a grep for the old slice matches its own obituary in
        a comment, and would pass while the behaviour regressed.
        """
        from builder.agents.react.agent_loop import _format_document_context
        from builder.tools.document_discovery import MAX_DOCUMENT_CANDIDATES

        documents, block = discovered

        assert MAX_DOCUMENT_CANDIDATES > 20
        assert len(documents) <= MAX_DOCUMENT_CANDIDATES
        # Both renderers track the ranking's length rather than saturating at 20.
        assert _format_document_context(documents).count("\n") + 1 == len(documents)
        assert sum(d["relative_path"] in block for d in documents) == len(documents)
