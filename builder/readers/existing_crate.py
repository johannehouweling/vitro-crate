"""Input reader for existing RO-Crates — reconstructs CrateState from metadata."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from builder.state import CrateState, Entity, EntityProvenance, EntityType

logger = logging.getLogger(__name__)

_VALID_TYPES = frozenset(
    {
        "Investigation",
        "Study",
        "Assay",
        "LabProcess",
        "LabProtocol",
        "Sample",
        "MolecularEntity",
        "CellLineSample",
        "Person",
        "Organization",
        "Publication",
        "DefinedTerm",
        "PropertyValue",
        "File",
        # AOP-Wiki subgraph nodes round-trip by their own @type (Issue #180).
        "AdverseOutcomePathway",
        "KeyEvent",
        "KeyEventRelationship",
    }
)
_DATASET_SUBTYPES = frozenset({"Investigation", "Study", "Assay"})


def _crate_type(node: dict) -> EntityType | None:
    """Map a graph node's @type/additionalType to a CrateState entity type.

    A valid ISA-Tox crate types Investigation/Study/Assay as ``Dataset`` +
    ``additionalType`` and the cell-based test system as ``Sample`` +
    ``additionalType "CellLine"``; LabProcess subtypes keep ``@type LabProcess``
    with the subtype carried in ``additionalType``. Returns None for nodes that
    are not reconstructable CrateState entities (e.g. Profile descriptors).
    """
    t = node.get("@type")
    primary = t[0] if isinstance(t, list) else t
    add = node.get("additionalType")
    if isinstance(add, list):
        add = add[0] if add else None

    if primary == "Dataset":
        return add if add in _DATASET_SUBTYPES else None
    if primary == "LabProcess":
        return "LabProcess"
    if primary == "Sample":
        return "CellLineSample" if add == "CellLine" else "Sample"
    if primary == "ScholarlyArticle":
        return "Publication"
    if primary == "MediaObject":
        return "File"
    if primary in _VALID_TYPES:
        return primary
    return None


def _relative_path_id(node_id: str) -> str | None:
    """The node @id as a crate-relative path, or None if it isn't one.

    A File node's @id IS its crate-relative location (ro-crate-1.2.0.md
    §Data-Entities: a File's @id is a path relative to the crate root). To make
    build → read → build idempotent for file placement (AGENTS.md §D13), the
    reader feeds this path back as the reconstructed File's ``dest_path`` — the
    field ``_file_dest`` consumes first when re-placing a File.

    Returns None for ids that are not usable relative paths — ``#``-fragments,
    absolute URIs (``http(s)://``, any ``scheme://``) and root-absolute paths —
    so the builder falls back to its ``data/<name>`` default rather than inventing
    a path from a non-path id (D5: derive only from the node's own @id).
    """
    if not node_id or node_id.startswith(("#", "/")) or "://" in node_id:
        return None
    return node_id


def _ref_ids(value: object) -> list[str]:
    """The @id strings under a reference property (id, {"@id": …}, or list)."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        rid = item.get("@id") if isinstance(item, dict) else item
        if isinstance(rid, str):
            out.append(rid)
    return out


def read_existing_crate(crate_dir: str) -> CrateState:
    """Read an existing RO-Crate and reconstruct CrateState from its metadata.

    1. Read ro-crate-metadata.json
    2. Parse the @graph for entities
    3. Create Entity objects from each graph node
    4. Return populated CrateState

    If crate doesn't exist or is invalid, return a default CrateState.

    Args:
        crate_dir: Path to the directory containing ro-crate-metadata.json.

    Returns:
        A CrateState reconstructed from the crate's metadata.
    """
    crate_path = Path(crate_dir)
    metadata_path = crate_path / "ro-crate-metadata.json"

    if not metadata_path.is_file():
        logger.warning("No ro-crate-metadata.json found in %s", crate_dir)
        return CrateState()

    try:
        with open(metadata_path) as f:
            data = json.load(f)

        state = CrateState()
        state.metadata.input_path = crate_dir

        graph = data.get("@graph", [])
        id_to_entity: dict[str, Entity] = {}
        # If the crate keeps a separate Investigation node, don't ALSO reconstruct
        # one from the root (avoid a duplicate Investigation on rebuild).
        has_separate_inv = any(
            _crate_type(n) == "Investigation" and n.get("@id") not in ("./", "") for n in graph
        )

        for node in graph:
            node_id = node.get("@id", "")

            # Only the Root Data Entity (@id "./") describes the crate as a whole;
            # the metadata descriptor is skipped. Study/Assay are real entities.
            if node_id == "./":
                md = state.metadata
                md.title = node.get("name", md.title)
                md.description = node.get("description", md.description)
                md.accession = node.get("identifier", md.accession)
                # The root IS the Investigation (ISA: ./ represents the
                # Investigation; the builder folds it onto the root rather than
                # emitting a separate node). Reconstruct it as an Investigation
                # entity so a rebuild reproduces the same structure.
                if _crate_type(node) == "Investigation" and not has_separate_inv:
                    fields = {
                        k: v
                        for k, v in node.items()
                        if not k.startswith("@") and k not in ("conformsTo", "hasPart")
                    }
                    inv = Entity(
                        entity_id="investigation",
                        type="Investigation",
                        fields=fields,
                        _provenance=EntityProvenance(created_by="scanner"),
                    )
                    state.add_entity(inv)
                    id_to_entity[node_id] = inv
                continue
            # Skip regenerable plumbing — the metadata descriptor and the
            # auto-embedded preview/graph artifacts are not CrateState entities
            # (export_crate re-creates the preview and ro-crate-graph.mmd).
            if node_id in (
                "ro-crate-metadata.json",
                "ro-crate-preview.html",
                "ro-crate-graph.mmd",
            ):
                continue

            ctype = _crate_type(node)
            if ctype is None:
                continue

            # Recover the BARE entity_id: strip the leading '#' AND the
            # type-qualifier the builder prepends (``#Study_study_1`` → ``study_1``).
            # Stripping only '#' would leave 'Study_study_1', which _mint_id then
            # re-prefixes on rebuild, corrupting @ids/identifiers every cycle.
            entity_id = node_id[1:] if node_id.startswith("#") else node_id
            prefix = f"{ctype}_"
            if node_id.startswith("#") and entity_id.startswith(prefix):
                entity_id = entity_id[len(prefix) :]
            fields = {k: v for k, v in node.items() if not k.startswith("@")}
            # Preserve a File's in-crate location: its @id is the crate-relative
            # path, which the builder re-reads from ``dest_path`` to re-place it.
            # Without this the rebuild defaults to ``data/<name>``, drifting the
            # File's path every cycle (Issue #180). Only set it when the @id is a
            # usable relative path and the node didn't already carry an explicit
            # placement field (D5: derive from the node's own @id, never invent).
            if ctype == "File" and not any(
                fields.get(k) for k in ("dest_path", "path", "contentUrl")
            ):
                rel = _relative_path_id(node_id)
                if rel is not None:
                    fields["dest_path"] = rel
            entity = Entity(
                entity_id=entity_id,
                type=ctype,
                fields=fields,
                _provenance=EntityProvenance(created_by="scanner"),
            )
            state.add_entity(entity)
            id_to_entity[node_id] = entity

        # Reconstruct the structural linkages the crate encodes via hasPart/about
        # (the builder's study_id/assay_id fields are not serialized): a Study's
        # hasPart Assay → assay.study_id, and an Assay's about LabProcess →
        # process.assay_id. Without this, a rebuild promotes the Assay to the root
        # and orphans the result Files off their Assay.
        for node in graph:
            src = id_to_entity.get(node.get("@id", ""))
            if src is None:
                continue
            if src.type == "Study":
                for ref in _ref_ids(node.get("hasPart")):
                    tgt = id_to_entity.get(ref)
                    if tgt is not None and tgt.type == "Assay":
                        tgt.fields["study_id"] = src.entity_id
            elif src.type == "Assay":
                for ref in _ref_ids(node.get("about")):
                    tgt = id_to_entity.get(ref)
                    if tgt is not None and tgt.type == "LabProcess":
                        tgt.fields["assay_id"] = src.entity_id

        logger.info(
            "Read existing crate from %s — found %d entities",
            crate_dir,
            len(state.list_entities()),
        )
        return state

    except (json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning("Failed to read existing crate from %s: %s", crate_dir, e)
        return CrateState()
