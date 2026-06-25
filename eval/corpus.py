"""The evaluation corpus — a small fixed set of crate-build cases as data.

Each :class:`EvalCase` declares:

* a stable ``case_id`` and human ``description``;
* a ``kind`` — one of the three input tiers from AGENTS.md §9
  (``"minimal"`` / ``"structured"`` / ``"unstructured"``);
* a ``prompt`` — the natural-language request handed to a *conversational* agent
  (the ReAct engine / tail agent) to drive the build;
* an optional ``input_path`` — an in-repo, offline directory the agent scans
  (the structured-metadata case);
* an optional ``build_state`` — a zero-arg factory returning a finished
  :class:`CrateState`. The **mock** agent factory uses it to stand in for a real
  build so the harness logic is unit-testable offline; live agents ignore it.

The success predicate is shared and deliberately strict: a case succeeds when its
crate reaches ``{base, isa, tox}`` REQUIRED conformance through ``build_and_validate``
(see :func:`reaches_isa_tox_conformance`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from builder.state import CrateState

CaseKind = Literal["minimal", "structured", "unstructured"]

# The structured-metadata fixture is an in-repo, offline research folder (no real
# experimental data) — the same one the #59 end-to-end quality test uses.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_STRUCTURED_INPUT = _REPO_ROOT / "tests" / "fixtures" / "svhps21_input"


@dataclass(frozen=True)
class EvalCase:
    """One crate-build case in the corpus.

    Attributes:
        case_id: Stable identifier, unique within the corpus.
        description: Human-readable summary of what the case exercises.
        kind: The input tier — ``"minimal"`` / ``"structured"`` / ``"unstructured"``.
        prompt: NL request driving a conversational agent's build.
        input_path: Optional in-repo directory the agent scans (offline).
        build_state: Optional factory of a finished state, used by the mock agent.
    """

    case_id: str
    description: str
    kind: CaseKind
    prompt: str = ""
    input_path: str | None = None
    build_state: Callable[[], CrateState] | None = None


def reaches_isa_tox_conformance(state: CrateState) -> dict[str, Any]:
    """Success predicate: does *state* reach ``{base, isa, tox}`` conformance?

    Runs ``build_and_validate`` at REQUIRED severity over all three layers and
    returns ``{"success": bool, "conformance": {layer: bool}, "issues": [...]}``.
    ``success`` is true only when every layer passes REQUIRED.

    Args:
        state: The crate state produced by an agent build.

    Returns:
        A dict with the boolean verdict, the per-layer conformance map, and the
        list of routable issues (empty on success).
    """
    from eval.metrics import evaluate_success

    result = evaluate_success(state, profile="all", severity="required")
    conformance = result.get("conformance", {})
    layers = ("base", "isa", "tox")
    success = bool(conformance) and all(conformance.get(layer) for layer in layers)
    return {
        "success": success,
        "conformance": conformance,
        "issues": result.get("issues", []),
    }


# --- mock-build state factories (offline stand-ins for a real agent build) -----
#
# These let the harness's runner / report / determinism logic be exercised end to
# end without a live model. A live agent never calls them; they exist so the
# *mock* agent factory has a finished, REQUIRED-clean state to return per case.


def _minimal_state() -> CrateState:
    """A REQUIRED-clean S-VHPS21 backbone — the simplest passing crate."""
    from tests.fixtures.vhps_golden_crates import vhps_fixture_state

    return vhps_fixture_state("S-VHPS21")


def _structured_state() -> CrateState:
    """Stand-in for building from the structured svhps21 input folder."""
    from tests.fixtures.vhps_golden_crates import vhps_fixture_state

    return vhps_fixture_state("S-VHPS21")


def _unstructured_state() -> CrateState:
    """Stand-in for a conversation-driven build with no metadata files."""
    from tests.fixtures.vhps_golden_crates import vhps_fixture_state

    return vhps_fixture_state("S-VHPS21")


DEFAULT_CORPUS: tuple[EvalCase, ...] = (
    EvalCase(
        case_id="minimal-backbone",
        description=(
            "Minimal case: build a conformant ISA-Tox backbone "
            "(Investigation -> Study -> Assay + one Exposure) from a short brief."
        ),
        kind="minimal",
        prompt=(
            "Build a minimal ISA-Tox RO-Crate for an in vitro assay named "
            "'MCT8-MDCK1 cellular uptake assay' studying inhibition of MCT8-mediated "
            "uptake of triiodothyronine. Use accession S-VHPS21. Scaffold the "
            "Investigation/Study/Assay backbone and add the cell line, the compound "
            "Triiodothyronine, and an Exposure process, then build and validate "
            "until base, ISA, and ISA-Tox all pass."
        ),
        build_state=_minimal_state,
    ),
    EvalCase(
        case_id="structured-svhps21",
        description=(
            "Structured-metadata directory: scan the in-repo S-VHPS21 research "
            "folder (README + raw/processed CSVs) and build a conformant crate."
        ),
        kind="structured",
        prompt=(
            "Scan the provided input folder, read its README and data files, draft "
            "the ISA-Tox entities for the S-VHPS21 MCT8 uptake study, attach the "
            "data files, then build and validate until base, ISA, and ISA-Tox pass."
        ),
        input_path=str(_STRUCTURED_INPUT),
        build_state=_structured_state,
    ),
    EvalCase(
        case_id="unstructured-conversation",
        description=(
            "Unstructured / conversational: no metadata files; the whole crate is "
            "elicited from the prompt and built from scratch."
        ),
        kind="unstructured",
        prompt=(
            "I ran an in vitro screen measuring whether test chemicals inhibit "
            "triiodothyronine uptake via MCT8 in overexpressing MDCK1 cells "
            "(study S-VHPS21, by Fabian Wagenaars at Universiteit Utrecht). There "
            "are no metadata files. Please build a conformant ISA-Tox RO-Crate from "
            "this description, validating until base, ISA, and ISA-Tox all pass."
        ),
        build_state=_unstructured_state,
    ),
)
