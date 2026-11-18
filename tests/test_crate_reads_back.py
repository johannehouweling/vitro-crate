"""A crate this tool writes must load in the reference implementation (#532).

Every crate in ``output/`` failed ``ROCrate(path)`` with the same complaint —
a data entity not linked from the root's ``hasPart``. The cause was deliberate:
D13 nests result files and PageTab-alias files under their Assay and then
*removes* them from the root, on the stated grounds that they "stay reachable
from the root transitively (File → Assay → Study → ./)".

They do not. RO-Crate's file tree is walked from the root through **directory**
Datasets, and the ISA Study/Assay are contextual entities identified by
``#Study_…`` / ``#Assay_…`` — logical containers, not directories. ro-crate-py
does not traverse them, so a file parented only there is unreachable and the
canonical consumer refuses the whole crate. It passed all three SHACL profiles
the entire time, because none of them asks this question.

RO-Crate permits a data entity to be ``hasPart`` of more than one Dataset, so
the ISA nesting is kept and the root reference is kept with it.

The oracle here is ro-crate-py rather than a hand-written rule: it is the
implementation real consumers use, so it cannot drift from the thing we care
about.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from builder.tools.builder import export_crate
from tests.fixtures.vhps_golden_crates import vhps_fixture_state

pytestmark = pytest.mark.timeout(180)


def _ids(value: object) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v.get("@id")) if isinstance(v, dict) else str(v) for v in items]


@pytest.fixture(scope="module")
def written_crate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("readback") / "crate"
    state = vhps_fixture_state("S-VHPS21")
    state.metadata.output_path = str(out)
    result = export_crate(state, str(out))
    assert result["success"], result["error"]
    return out


class TestTheReferenceImplementationCanReadIt:
    def test_a_receiving_lab_can_open_the_crate(self, written_crate: Path) -> None:
        """``ROCrate(path)`` in a process that knows nothing about this repo.

        Deliberately a subprocess: ro-crate-py builds its read-time class map
        from every imported subclass of ``ContextEntity`` and matches by class
        *name*, so importing ``profiles.models`` changes how a crate is read.
        A receiving lab has only vanilla ro-crate-py, and that is the claim
        worth pinning — reading it in-process would test our import graph as
        much as the crate.

        (In-process reading is separately broken and tracked; the models'
        constructors are incompatible with ro-crate-py's positional read call.
        That bug masks this one but is not this one.)
        """
        script = (
            "from rocrate.rocrate import ROCrate;"
            f"c = ROCrate({str(written_crate)!r});"
            "assert list(c.data_entities), 'no data entities'"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, (
            f"a plain ro-crate-py consumer cannot open the crate:\n{proc.stderr[-800:]}"
        )

    def test_every_local_data_entity_is_reachable_from_the_root(
        self, written_crate: Path
    ) -> None:
        """The rule ro-crate-py enforces, asserted directly.

        Stated separately from the load test so a future ro-crate-py that
        loosens (or tightens) its check still leaves the invariant pinned.
        """
        import json

        graph = json.loads(
            (written_crate / "ro-crate-metadata.json").read_text(encoding="utf-8")
        )["@graph"]
        root = next(e for e in graph if e.get("@id") == "./")
        root_parts = set(_ids(root.get("hasPart")))

        unreachable = [
            e["@id"]
            for e in graph
            if "File" in str(e.get("@type"))
            and not str(e["@id"]).startswith(("#", "http://", "https://"))
            and e["@id"] not in root_parts
        ]
        assert unreachable == [], f"files unreachable from the root: {unreachable}"


# Namespaces this builder MINTS entities in. A reference into one of these names
# something the crate is supposed to have defined, so a dangling one is a defect
# even though the string is a perfectly well-formed absolute IRI — which is
# exactly how three truncated ORCIDs shipped green (#532): `_mint_id` derives a
# Person's @id from the ORCID, so `https://orcid.org/0002-5392-0519` (missing the
# leading `0000-` block) LOOKS external and resolves to nothing, while the
# correct `…/0000-0002-5392-0519` entity sits in the same graph.
#
# Deliberately a mint-list, not a blocklist. External vocabulary — EFO term IRIs
# on a csvw:Column, a CompTox `propertyID`, PURL/w3id profile ids — is REFERENCED
# and never defined locally, which is legal and normal; asserting those resolve
# would fail every correct crate.
_MINTED_NAMESPACES: tuple[str, ...] = (
    "https://orcid.org/",
    "https://ror.org/",
    "https://doi.org/",
    "https://www.cellosaurus.org/",
    "https://pubchem.ncbi.nlm.nih.gov/",
)


def _references(node: object) -> list[str]:
    """Every ``{"@id": ...}`` reference anywhere inside *node*, at any depth."""
    out: list[str] = []
    if isinstance(node, dict):
        target = node.get("@id")
        # A one-key {"@id": ...} is a reference; a node with other keys is a
        # DEFINITION and its own @id is not a reference to anything.
        if isinstance(target, str) and set(node) == {"@id"}:
            out.append(target)
        for key, value in node.items():
            if key != "@id":
                out.extend(_references(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_references(item))
    return out


class TestEveryReferenceResolves:
    """The other half of #532, and the half SHACL cannot ask.

    Part A of that issue was seven `@id` references pointing at nothing — three
    truncated ORCIDs, three bare organization slugs (`org_erasmus_mc`, no `#`, no
    ROR), and a `#CitationAuthor_…` that was never defined. All seven passed all
    three profiles, because a SHACL shape validates the nodes that ARE there and
    never asks whether an edge leads anywhere.

    The defects themselves are fixed. This is the guard that was asked for and
    not written, so they cannot come back silently.
    """

    def test_every_local_reference_is_defined(self, written_crate: Path) -> None:
        """Catches the bare slug and the undefined local fragment.

        Anything that is not an absolute IRI can only mean an entity in THIS
        crate, so it has to be in the graph. `org_erasmus_mc` and
        `#CitationAuthor_Zhongli_Chen` are both caught here.
        """
        import json

        graph = json.loads(
            (written_crate / "ro-crate-metadata.json").read_text(encoding="utf-8")
        )["@graph"]
        defined = {str(e.get("@id")) for e in graph}

        dangling = sorted(
            {
                ref
                for ref in _references(graph)
                if "://" not in ref and ref not in defined
            }
        )
        assert dangling == [], f"local references that resolve to nothing: {dangling}"

    def test_every_reference_into_a_minted_namespace_is_defined(
        self, written_crate: Path
    ) -> None:
        """Catches the truncated ORCID, which the test above cannot see.

        A malformed ORCID is still an absolute IRI, so "external references are
        someone else's problem" waves it through. But this builder MINTS Person
        @ids from ORCIDs, so a reference to an orcid.org id it never defined is a
        reference to an entity that was supposed to be here.
        """
        import json

        graph = json.loads(
            (written_crate / "ro-crate-metadata.json").read_text(encoding="utf-8")
        )["@graph"]
        defined = {str(e.get("@id")) for e in graph}

        dangling = sorted(
            {
                ref
                for ref in _references(graph)
                if ref.startswith(_MINTED_NAMESPACES) and ref not in defined
            }
        )
        assert dangling == [], (
            f"references into a namespace this builder mints in, resolving to "
            f"nothing: {dangling}"
        )

    def test_external_vocabulary_is_not_required_to_resolve(
        self, written_crate: Path
    ) -> None:
        """The control, and the reason this is a mint-list rather than a rule
        about absolute IRIs.

        A correct crate cites terms it does not define — an EFO term on a
        csvw:Column, a profile id under w3id. If the guards above ever grew to
        demand those resolve, every honest crate would fail and the assertion
        would be turned off rather than fixed. Proving the fixture HAS such a
        reference is what stops that rewrite from looking harmless.
        """
        import json

        graph = json.loads(
            (written_crate / "ro-crate-metadata.json").read_text(encoding="utf-8")
        )["@graph"]
        defined = {str(e.get("@id")) for e in graph}
        external_unresolved = [
            ref
            for ref in _references(graph)
            if "://" in ref
            and not ref.startswith(_MINTED_NAMESPACES)
            and ref not in defined
        ]
        assert external_unresolved, (
            "this fixture cites no external vocabulary, so the two guards above "
            "are not actually distinguishing external references from broken ones"
        )


class TestTheIsaNestingSurvives:
    """Dual-parenting, not re-parenting: the ISA hierarchy is the point of D13."""

    def test_files_stay_nested_under_their_isa_container(self, written_crate: Path) -> None:
        import json

        graph = json.loads(
            (written_crate / "ro-crate-metadata.json").read_text(encoding="utf-8")
        )["@graph"]
        containers = [
            e
            for e in graph
            if str(e.get("additionalType")) in ("Assay", "Study")
            and _ids(e.get("hasPart"))
        ]
        assert containers, "no ISA container carries hasPart — the nesting was lost"

        nested = {cid for c in containers for cid in _ids(c.get("hasPart"))}
        root = next(e for e in graph if e.get("@id") == "./")
        root_parts = set(_ids(root.get("hasPart")))
        both = nested & root_parts
        assert both, (
            "no entity is listed by both an ISA container and the root — "
            "re-parented rather than dual-parented"
        )
