"""Golden-crate regression fixtures: the VHP4Safety study datasets (Issue #97).

The paper anchors its evaluation on nine VHP4Safety study datasets
(S-VHPS16/21/22/23/24/25/26/27/28; deposited in EBI BioStudies). These are the
natural "does the toolbox still produce a valid crate?" regression anchors.

Each study is described by a small, self-contained :class:`VhpsStudySpec` (no
network, no on-disk EBI data — the raw archives live under the git-ignored
``input/`` and must not be a test dependency). :func:`vhps_fixture_state` turns a
spec into a :class:`CrateState` that ``build_and_validate`` passes clean at
REQUIRED severity. S-VHPS21 (single assay, thyroid MCT8 uptake) is fully
specified; the registry + factory are the framework for adding the remaining
eight — append a :class:`VhpsStudySpec` to :data:`VHPS_STUDIES`.
"""

from __future__ import annotations

from dataclasses import dataclass

from builder.state import CrateState, Entity, EntityProvenance, EntityType


@dataclass(frozen=True)
class VhpsStudySpec:
    """A minimal, REQUIRED-clean description of one VHP4Safety study dataset."""

    accession: str
    title: str
    description: str
    doi: str
    release_date: str
    author_name: str
    author_orcid: str
    organization: str
    assay_name: str
    cell_line: str
    compound: str


def _ent(entity_id: str, type_: EntityType, **fields: object) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="user"),
    )


def build_state(spec: VhpsStudySpec) -> CrateState:
    """Assemble a :class:`CrateState` for ``spec`` (REQUIRED-clean when built)."""
    state = CrateState()
    state.metadata.title = spec.title
    state.metadata.description = spec.description
    state.metadata.accession = spec.accession

    # ISA backbone: Investigation → Study → Assay.
    state.add_entity(
        _ent(
            "inv",
            "Investigation",
            name=spec.title,
            description=spec.description,
            identifier=spec.accession,
        )
    )
    state.add_entity(
        _ent(
            "study",
            "Study",
            name=spec.title,
            description=spec.description,
            identifier=spec.accession,
            investigation_id="inv",
            datePublished=spec.release_date,
        )
    )
    state.add_entity(
        _ent(
            "assay",
            "Assay",
            name=spec.assay_name,
            identifier=f"{spec.accession}-assay",
            study_id="study",
        )
    )

    # Contributors. The ISA Person shape requires a given name; split the full
    # name into given/family on the first space.
    given, _, family = spec.author_name.partition(" ")
    state.add_entity(
        _ent(
            "author",
            "Person",
            name=spec.author_name,
            givenName=given,
            familyName=family or given,
            orcid=spec.author_orcid,
        )
    )
    state.add_entity(_ent("org", "Organization", name=spec.organization))

    # Domain entities and the Exposure that anchors the assay's derivation chain.
    state.add_entity(_ent("cell", "CellLineSample", name=spec.cell_line))
    state.add_entity(_ent("compound", "MolecularEntity", name=spec.compound))
    state.add_entity(
        _ent(
            "exposure",
            "LabProcess",
            name=f"{spec.compound} uptake exposure",
            process_type="Exposure",
            assay_id="assay",
            samples="cell",
            chemicals="compound",
        )
    )
    return state


# Registry of known-good study fixtures. S-VHPS21 is fully specified; append more
# VhpsStudySpec entries here to extend coverage to the rest of the nine.
VHPS_STUDIES: dict[str, VhpsStudySpec] = {
    "S-VHPS21": VhpsStudySpec(
        accession="S-VHPS21",
        title=(
            "Inhibition of MCT8-mediated cellular uptake of triiodothyronine in "
            "an overexpressing cell model"
        ),
        description=(
            "A cell based in vitro assay to screen chemicals for their capacity "
            "to inhibit triiodothyronine (T3) uptake by human monocarboxylate 8 "
            "(MCT8) using an overexpressing cell model."
        ),
        doi="10.6019/S-VHPS21",
        release_date="2025-11-10",
        author_name="Fabian Wagenaars",
        author_orcid="0000-0003-4766-7358",
        organization="Universiteit Utrecht",
        assay_name="MCT8-MDCK1 cellular uptake assay",
        cell_line="MDCK1 MCT8-overexpressing cells",
        compound="Triiodothyronine",
    ),
}


def vhps_fixture_state(study_code: str) -> CrateState:
    """Return the golden :class:`CrateState` for a registered study code.

    Raises:
        KeyError: If ``study_code`` is not in :data:`VHPS_STUDIES`.
    """
    if study_code not in VHPS_STUDIES:
        raise KeyError(
            f"No golden fixture for {study_code!r}; known: {sorted(VHPS_STUDIES)}."
        )
    return build_state(VHPS_STUDIES[study_code])
