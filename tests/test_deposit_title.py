"""The deposit's declared title and accession are read, not left unset (#722).

The pipeline never wrote ``state.metadata.title``, so DSM-1-R0 (an identifiable dataset
descriptor) held every pipeline crate at level 0, and the root kept the scaffold's
placeholder name "Investigation". The descriptor states both, beside the DOI:

    {"accno": "S-VHPS22", "attributes": [{"name": "Title", "value": "Neural cell ..."}]}
"""

from __future__ import annotations

import shutil
from pathlib import Path

from builder.engine import AgentEngine, _read_declared_identity
from builder.state import CrateState
from builder.tools import fair_assessment as fa
from builder.tools.builder import assemble_crate

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TITLE = "Neural cell screening models for endocrine disruption of thyroid hormone signaling"


def _initialised(tmp_path: Path) -> AgentEngine:
    deposit = tmp_path / "deposit"
    shutil.copytree(FIXTURES / "svhps22_real_input", deposit)
    engine = AgentEngine()
    engine.initialize(str(deposit))
    return engine


def _root(state: CrateState) -> dict:
    graph = assemble_crate(state, output_dir=None, materialize_payload=False)
    return next(n for n in graph.metadata.generate()["@graph"] if n.get("@id") == "./")


def test_the_declared_title_and_accession_are_read_at_initialize(tmp_path: Path) -> None:
    state = _initialised(tmp_path).state
    assert state.metadata.title == TITLE
    assert state.metadata.accession == "S-VHPS22"
    # The crate is named for the deposit, and R0 holds.
    assert _root(state)["name"] == TITLE
    assert fa._check_has_descriptor(state) is True


def test_an_empty_crate_still_fails_r0() -> None:
    state = CrateState()
    _root(state)
    assert fa._check_has_descriptor(state) is False


def test_a_value_already_set_is_left_alone(tmp_path: Path) -> None:
    engine = _initialised(tmp_path)
    engine.state.metadata.title = "Mine"
    engine.state.metadata.accession = "S-MINE"
    _read_declared_identity(engine)
    assert (engine.state.metadata.title, engine.state.metadata.accession) == ("Mine", "S-MINE")
