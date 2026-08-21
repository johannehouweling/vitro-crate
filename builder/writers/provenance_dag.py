"""Render the LabProcess provenance DAG from an assembled RO-Crate.

The paper's core value proposition is that a receiving lab can trace how an
output was produced::

    Sample →[CellCulture]→ Sample →[Exposure]→ condition_table
           →[EndpointReadout]→ raw_measurements →[DataAnalysis]→ figures

This writer reads a serialized metadata document — the ``@graph`` from
``crate.metadata.generate()`` or a parsed ``ro-crate-metadata.json`` — and emits
a Mermaid ``flowchart`` of that derivation chain, drawn **from real data** rather
than by hand. It plots only the provenance subgraph: each ``LabProcess`` and the
entities it consumes (``schema:object``) and produces (``schema:result``), plus
the optional ``about`` / ``derivesFrom`` annotation edges that connect the
compound (a ``MolecularEntity``, never a process object) through the condition
table. Contextual entities off the chain (people, organisations, parameters,
protocols) are deliberately excluded so the DAG stays a derivation graph.

The edge vocabulary mirrors ``builder.tools.provenance`` (``_INPUT_FIELDS`` /
``_OUTPUT_FIELDS``) and the ``@context`` aliases in ``profiles.context`` so the
renderer and the builder agree on what an input/output edge is; the bare
``schema:`` IRIs are accepted too so externally-authored crates render as well.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.parse
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# Property keys that carry a process's consumed (input) and produced (output)
# edges. The compact aliases match the tox/ISA models (``input``/``output`` on
# the tox subclasses, ``object``/``result`` on the base LabProcess) and the
# ``@context`` aliases; the full schema.org IRIs cover expanded documents.
_INPUT_KEYS: tuple[str, ...] = (
    "object",
    "input",
    "samples",
    "cell_line",
    "http://schema.org/object",
    "https://schema.org/object",
)
_OUTPUT_KEYS: tuple[str, ...] = (
    "result",
    "output",
    "http://schema.org/result",
    "https://schema.org/result",
)
_LINEAGE_KEYS: tuple[str, ...] = (
    "derivesFrom",
    "derives_from",
    "isBasedOn",
    "http://schema.org/isBasedOn",
    "https://schema.org/isBasedOn",
)
_ABOUT_KEYS: tuple[str, ...] = (
    "about",
    "http://schema.org/about",
    "https://schema.org/about",
)
_TYPE_KEYS: tuple[str, ...] = ("@type",)
_NAME_KEYS: tuple[str, ...] = ("name", "http://schema.org/name", "https://schema.org/name")
_ADDTYPE_KEYS: tuple[str, ...] = (
    "additionalType",
    "http://schema.org/additionalType",
    "https://schema.org/additionalType",
)

# The four domain LabProcess discriminators (ISA-Tox profile).
_PROCESS_DISCRIMINATORS = frozenset({"CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"})


# ---------------------------------------------------------------------------
# Full crate entity-graph (#130): three-layer classification + typed edges
# ---------------------------------------------------------------------------
#
# Layer classification follows the authoritative precedence derived from the
# models/context/shapes (crate-graph-inventory): root short-circuit → csvw:Table
# @type override → additionalType (domain) → additionalType (structural) → base
# @type. PropertyValue subtypes (ParameterValue/…) stay structural despite having
# an additionalType — the one deliberate exception.
# The owner's names (review comments on the report artifact): the plain layer
# names, not the Packaging/Structural/Domain taxonomy words.
_LAYER_NAMES: dict[int, str] = {
    1: "RO-Crate",
    2: "ISA RO-Crate",
    3: "ISA-Tox RO-Crate",
}

_DOMAIN_ADDTYPES = frozenset(
    {"CellLine", "CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"}
)
_STRUCT_ADDTYPES = frozenset(
    {
        "Investigation",
        "Study",
        "Assay",
        "ParameterValue",
        "FactorValue",
        "CharacteristicValue",
        "Component",
    }
)
# Local names only — `_short` strips any CURIE prefix (csvw:Table → Table), so
# the canonical compact form, the expanded IRI, and a non-standard prefix all
# reduce to the same token before these checks.
_CSVW_TABLE_TYPES = frozenset({"Table"})
_DOMAIN_TYPES = frozenset(
    {
        "MolecularEntity",
        "Schema",
        "Column",
        "AdverseOutcomePathway",
        "KeyEvent",
        "KeyEventRelationship",
    }
)
_STRUCT_TYPES = frozenset(
    {"LabProcess", "LabProtocol", "Sample", "DefinedTerm", "PropertyValue", "Dataset"}
)

# Plumbing nodes dropped from the visualization (used only to locate the root).
# Includes the embedded graph artifact itself, so re-rendering an exported crate
# never depicts its own diagram file.
_EXCLUDED_IDS = frozenset(
    {
        "ro-crate-metadata.json",
        "./ro-crate-metadata.json",
        "ro-crate-preview.html",
        # No longer written (#618); crates built before that carry one, and a
        # re-render of such a crate must still not depict its own diagram file.
        "ro-crate-graph.mmd",
    }
)

# Layer-synonym map for the --layer filter.
_LAYER_SYNONYMS: dict[str, int] = {
    "1": 1,
    "crate": 1,
    "rocrate": 1,
    "ro-crate": 1,
    "packaging": 1,
    "2": 2,
    "isa": 2,
    "structural": 2,
    "structure": 2,
    "3": 3,
    "isa-tox": 3,
    "isatox": 3,
    "tox": 3,
    "domain": 3,
    "all": 3,
}


def normalize_layer(value: str | int | None) -> int:
    """Resolve a ``--layer`` value (number or synonym) to a cumulative depth 1-3.

    ``crate``→1, ``isa``→2, ``isa-tox``/``tox``/``all``→3. ``None``→3 (show all).
    """
    if value is None:
        return 3
    key = str(value).strip().lower()
    if key not in _LAYER_SYNONYMS:
        raise ValueError(f"Unknown layer {value!r}. Use 1/2/3 or crate/isa/isa-tox (or 'all').")
    return _LAYER_SYNONYMS[key]


def _is_uri(value: Any) -> bool:
    """True for an absolute, dereferenceable identifier (contains a scheme)."""
    return isinstance(value, str) and "://" in value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _short(token: str) -> str:
    """Local name of a type token: last path/hash segment of an IRI, then any
    remaining CURIE prefix stripped.

    ``http://schema.org/Person`` → ``Person``; ``.../ns/csvw#Table`` → ``Table``;
    ``csvw:Table`` → ``Table``; ``mycsv:Table`` → ``Table``. The writer has no
    ``@context`` to expand prefixes, so it classifies on local names — this keeps
    classification prefix-agnostic (a crate that aliases the csvw namespace to a
    non-standard prefix still classifies its tables/columns as domain entities).
    """
    seg = token.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return seg.rsplit(":", 1)[-1]


def _types(node: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in _TYPE_KEYS:
        for t in _as_list(node.get(key)):
            if isinstance(t, str):
                out.add(_short(t))
    return out


def _first(node: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in node and node[key] is not None:
            return node[key]
    return None


def _is_ref_string(value: Any) -> bool:
    """A bare string that denotes a link, not a literal: a local fragment
    (``#…``) or an absolute URI (contains ``://``).

    Most RO-Crate references are ``{"@id": …}`` objects, but ro-crate-py
    serializes some reference-typed properties as bare strings — notably CSVW
    ``valueUrl`` (e.g. ``"#compound_aflb1"``). We accept those, while a plain
    literal (``intendedUse: "culture"``, ``measurementTechnique: "qPCR"``) is
    correctly ignored so it never becomes a spurious dangling reference.
    """
    return isinstance(value, str) and (value.startswith("#") or "://" in value)


def _refs(node: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """The reference targets across any of ``keys``.

    A reference is either a ``{"@id": …}`` object (or a list of them) or a bare
    string that looks like a link (see :func:`_is_ref_string`). Plain string
    literals are ignored.
    """
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        for item in _as_list(node.get(key)):
            ref: Any = None
            if isinstance(item, dict):
                ref = item.get("@id")
            elif _is_ref_string(item):
                ref = item
            if isinstance(ref, str) and ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


def _additional_type(node: dict[str, Any]) -> str | None:
    value = _first(node, _ADDTYPE_KEYS)
    if isinstance(value, dict):
        value = value.get("@id")
    return _short(value) if isinstance(value, str) else None


def _is_process(node: dict[str, Any]) -> bool:
    if "LabProcess" in _types(node):
        return True
    return _additional_type(node) in _PROCESS_DISCRIMINATORS


# ---------------------------------------------------------------------------
# The category registry — the single place a functional category's colour and
# shape are decided (#487).
#
# Every view reads it: the report's inline-SVG diagrams (via the CSS custom
# properties and rules generated by `category_css`), the Mermaid crate graph and
# provenance DAG, and every legend. Before this existed the same entity type was
# drawn three different ways — a File was magenta in the report, yellow in the
# crate graph and brown in the provenance DAG — which taught readers that colour
# meant nothing. A category that is not in here cannot be drawn, and that is what
# keeps the views from drifting apart again.
#
# Colours are one constant-lightness ring in CIE Lab (L* 47, chroma 44) with the
# ten hues spread evenly at 36°. Equal lightness means no category shouts louder
# than another across the thousands of tiles the overview draws, and equal
# spacing puts the closest pair as far apart as ten hues allow.
#
# Process and container are the exception: sRGB is narrow in the blues, so those
# two stayed the closest pair at dE 20 even at the ring's optimum. They are split
# on lightness instead (L* 39 and 55), which lifts the worst pair in the whole
# palette to dE 24 — against dE 14 for the hand-picked palette this replaced.
# Every stroke clears 3:1 on the page background.
# `tests/test_provenance_dag.py::TestCategoryRegistry` pins all of it.
#
# Hue assignment keeps the associations the old palettes already agreed on —
# process blue, material green, chemical amber, protocol cyan, container indigo
# — so a reader who learned the old diagrams is not retrained for nothing.


class CategoryStyle(NamedTuple):
    """How one functional category is drawn, in every view."""

    colour: str
    """The category's hue: SVG stroke, overview tile outline, Mermaid stroke."""

    label: str
    """Legend wording. One phrase, reused by every legend that shows it."""

    glyph: str
    """SVG path data for the category's 14x14 glyph, drawn on the explorer's node.

    Shape is the channel that survives greyscale, print and colour vision
    deficiency, so no two categories may share one. The explorer's node is a
    ~200px HTML box, so the shape cannot *be* the node the way it was in the
    inline-SVG diagrams this replaced (#618); it rides along as a badge instead.
    Arc flags are written spaced (``a4 4 0 0 1``), not run together
    (``a4 4 0 01``) — both are legal SVG, but only the spaced form lets a reader
    (or a test) tell a coordinate from a flag.
    """


# One key, one vocabulary: it names the category in the model, selects the CSS
# custom property `category_css` generates, and carries the glyph and colour the
# explorer draws — rather than a mapping between three.
CATEGORY_STYLES: dict[str, CategoryStyle] = {
    "container": CategoryStyle(
        "#667fd6", "Investigation / Study / Assay",
        "M2 3h10v8H2z M4 3v8 M10 3v8",
    ),
    "process": CategoryStyle(
        "#0066a0", "Process",
        "M4 2h6l3 5-3 5H4L1 7z",
    ),
    "protocol": CategoryStyle(
        "#00809a", "Protocol",
        "M4 3h9l-3 8H1z",
    ),
    "material": CategoryStyle(
        "#387e42", "Sample / material",
        "M5 3h4a4 4 0 0 1 0 8H5a4 4 0 0 1 0-8z",
    ),
    "chemical": CategoryStyle(
        "#966527", "Compound",
        "M7 2.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9z",
    ),
    "data": CategoryStyle(
        "#b14e71", "File / table",
        "M2 4c0-1.1 2.2-2 5-2s5 .9 5 2v6c0 1.1-2.2 2-5 2s-5-.9-5-2z M2 4c0 1.1 2.2 2 5 2s5-.9 5-2",
    ),
    "agent": CategoryStyle(
        "#95599b", "Person",
        "M7 1.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11z M7 4a3 3 0 1 1 0 6 3 3 0 0 1 0-6z",
    ),
    "org": CategoryStyle(
        "#00816e", "Organisation",
        "M2 3h10v8H2z",
    ),
    "publication": CategoryStyle(
        "#af5546", "Publication",
        "M1 3h12l-2 8H3z",
    ),
    "pathway": CategoryStyle(
        "#6e7424", "Pathway / key event",
        "M1 3l4 4-4 4z M7 3l4 4-4 4z",
    ),
    # The one category off the ring, muted on purpose: what *qualifies* the work
    # is drawn more faintly than what takes part in it, with `ctx`'s near-grey
    # below it for what the crate never typed at all. The ring is full at ten
    # saturated colours, so this is what an eleventh category costs — see
    # `TestCategoryRegistry.test_the_work_is_drawn_more_strongly_than_what_qualifies_it`.
    "annotation": CategoryStyle(
        "#846050", "Term / parameter",
        "M1 3h9l3 4-3 4H1z",
    ),
}

# The two types the ISA-Tox profile defines for an adverse outcome pathway
# (`profiles/shapes/tox/6_study_aop.ttl`, `7_assay_key_event.ttl`). ONE list,
# read by three rules that would otherwise drift: which entities the Assays view
# follows a `mentions` edge to (#627), what the node is captioned, and what
# colour it is drawn in (#643). A type in one and not the others is drawn as
# science and captioned as vocabulary, or the reverse.
PATHWAY_TYPES = frozenset({"AdverseOutcomePathway", "KeyEvent"})

# Type preference for a node's caption, most specific first: a domain type
# outranks the generic one it refines.
_TAG_PREFERENCE = (
    *sorted(PATHWAY_TYPES),
    "MolecularEntity",
    "Table",
    "File",
    "Sample",
)

# The bucket for an entity no category claims. It is deliberately grey: a node
# drawn in a category colour asserts that the crate said what it is, and an
# unclassified one has not earned that claim.
_CTX_CATEGORY = "ctx"


def _node_class(node: dict[str, Any]) -> str:
    """Style bucket for a node — its functional category, or ``ctx``.

    One vocabulary across every view: the same key selects the Mermaid shape,
    the inline-SVG outline and the CSS class, so a Sample cannot be a stadium
    here and a rounded box there.
    """
    category = _entity_category(node)
    return category if category in CATEGORY_STYLES else _CTX_CATEGORY


def _name(node: dict[str, Any]) -> str:
    value = _first(node, _NAME_KEYS)
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("@value") or value.get("@id")
    return str(value) if value else str(node.get("@id", "?"))


# The prefix the builder stamps on a file it generated itself rather than
# received from the depositor — ``_crate_mapping.AUTOGENERATED_MARKER``, which
# writes names as ``AUTOGENERATED — Condition table``.
#
# Held as a literal, not imported: this module reads a parsed metadata document
# and imports nothing but the stdlib, and reaching into ``builder.tools`` would
# drag the whole ro-crate stack in to read one string.
# ``TestAutogeneratedBadge.test_marker_matches_the_builders`` pins the two
# together so they cannot drift apart.
_AUTOGENERATED_MARKER = "AUTOGENERATED"

# The marker plus whatever separator followed it. The builder writes an em dash,
# but a name that reached the crate by another route may use a plain hyphen or a
# colon, and leaving a stray separator behind is worse than not matching at all.
_AUTOGENERATED_RE = re.compile(
    rf"^\s*{_AUTOGENERATED_MARKER}\s*[—–:-]?\s*",
    re.IGNORECASE,
)

# U+FE0F. Written as an escape because the character is invisible in source, so
# a literal here reads as a typo and cannot survive a careless edit.
_VARIATION_SELECTOR = "\ufe0f"

# The mark that replaces the `AUTOGENERATED` prefix in a node label. Named, not
# inlined: `_display_name` writes it and the tests read it, and a badge nobody
# can name is a badge that drifts between the two.
AUTOGENERATED_BADGE = f"\u26a0{_VARIATION_SELECTOR}"


def _display_name(node: dict[str, Any]) -> str:
    """Node label for a diagram, badging a file the crate generated itself.

    The builder spells the warning out in the name so no reader — human or
    machine — mistakes a scaffold for measured data. That is right for the
    metadata, but it does not survive a node label: the prefix is 16 characters
    and a label is truncated well before that, so *every* generated file read as
    ``AUTOGENERATED — C…``. The warning survived and the filename did not, which
    left several such nodes identical on the page and unreadable.

    Swapping the words for a badge says the same thing in two columns instead of
    sixteen and gives the name its room back. Nothing is hidden: the node's
    ``<title>`` still carries the crate's name verbatim, marker included.
    """
    name = _name(node)
    stripped = _AUTOGENERATED_RE.sub("", name, count=1).strip()
    if stripped == name.strip():
        return name
    # A name that was ONLY the marker leaves nothing to show; fall back to the
    # node id rather than rendering a lone badge.
    return f"{AUTOGENERATED_BADGE} {stripped or str(node.get('@id', '')).strip()}".strip()


def _tag(node: dict[str, Any]) -> str:
    """Short type/discriminator tag shown under the node name."""
    if _is_process(node):
        return _additional_type(node) or "LabProcess"
    types = _types(node)
    # A domain type outranks the generic one it refines. Without this the tag is
    # whichever type sorts first, so a key event — typed `["KeyEvent",
    # "DefinedTerm"]` — reached the canvas captioned "DefinedTerm", telling the
    # reader it was a piece of vocabulary rather than the effect the assay
    # measures (#627).
    for preferred in _TAG_PREFERENCE:
        if preferred in types:
            tag = preferred
            break
    else:
        tag = next(iter(sorted(types)), "Entity")
    add = _additional_type(node)
    return f"{tag} · {add}" if add and add != tag else tag


def _escape(text: str) -> str:
    """Make a crate-controlled string safe to interpolate into a Mermaid label.

    Mermaid node labels are quoted strings whose contents are rendered as HTML
    by mermaid.js, so unescaped crate data (entity names/descriptions) is a
    stored-XSS vector — a name like ``<img src=x onerror=alert(1)>`` would run
    when the embedded diagram is opened (#169). We therefore HTML-escape the
    metacharacters (``& < > "``) so the text renders inert, collapse newlines so
    the label stays single-line, and strip surrounding whitespace.

    The intentional markup the writers add around a label (``<br/>``,
    ``<small>``) is composed *after* this call, so it stays live; only the
    dynamic text is neutralised. ``"`` becomes ``&quot;`` (not ``'``) so a label
    can never break out of its quoted Mermaid string either.
    """
    escaped = html.escape(text, quote=True)
    return escaped.replace("\n", " ").replace("\r", " ").strip()


def _collect_briefs(obj: Any, out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every node-brief reachable inside an inventory, at any nesting depth.

    The inventories nest briefs differently per diagram (``process``/``via``,
    ``source``, band members, plain lists), so this walks the structure rather
    than asking each renderer to enumerate its own — one place to keep correct
    instead of four that must agree.
    """
    out = [] if out is None else out
    if isinstance(obj, dict):
        if isinstance(obj.get("name"), str) and "tag" in obj:
            out.append(obj)
        for value in obj.values():
            _collect_briefs(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _collect_briefs(value, out)
    return out


def _derivation_edges(nodes: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The crate's derivation edges, all pointing downstream.

    ``material --object--> process`` and ``process --result--> data``, so a
    reader follows the chain left to right in the order the work happened. Only
    edges whose endpoints are both in-crate are returned: an off-graph reference
    has nothing to attach to, and half an edge is not a step.

    This is the one definition of "what the derivation chain contains". The
    inline-SVG chain lays these out, and the interactive explorer's LabProcesses
    view is their endpoint set — two renderings of one selection rather than two
    selections that happen to agree today.

    Args:
        nodes: ``@id`` → entity, as :func:`_graph_nodes` returns.

    Returns:
        ``(src, dst, kind)`` triples, ``kind`` being ``"object"`` (consumed) or
        ``"result"`` (produced), in entity order so the output is deterministic.
    """
    edges: list[tuple[str, str, str]] = []
    for nid, node in nodes.items():
        if not _is_process(node):
            continue
        for src in _refs(node, _INPUT_KEYS):
            if src in nodes:
                edges.append((src, nid, "object"))
        for dst in _refs(node, _OUTPUT_KEYS):
            if dst in nodes:
                edges.append((nid, dst, "result"))
    return edges


def _route_hop_ids(process: str | None, via: str | None) -> list[str]:
    """The hops on a member's route back to a process, rightmost last.

    Two hops for the indirect route a compound takes (``process --result-->
    table --about--> compound``); one when a process references the member
    itself (a ``CellCulture`` consuming its cell line) — drawing that process in
    both columns with a ``result`` edge between them would depict a step the
    crate does not contain; none when nothing links the member at all.

    Shared so the routed bands and the explorer's compound/sample views agree on
    which intermediate entities a route drags in with it.
    """
    if via is None:
        return []
    if process is not None and process != via:
        return [process, via]
    return [via]


_HASPART_KEYS = ("hasPart", "has_part", "http://schema.org/hasPart")
_MENTIONS_KEYS = (
    "mentions",
    "aop",
    "keyEvent",
    "key_event",
    "key_events",
    "organism",
    "anatomy",
    "chemicals",
    "biologicalModels",
    "biological_models",
    "cell_lines",
    "http://schema.org/mentions",
)
_AUTHOR_KEYS = (
    "author",
    "creator",
    "http://schema.org/author",
    "http://schema.org/creator",
)
_CONTRIBUTOR_KEYS = (
    "contributor",
    "maintainer",
    "http://schema.org/contributor",
    "http://schema.org/maintainer",
)
# A Person's institution. This is the ONLY edge that reaches an affiliation-only
# Organization: nothing else in the crate references it, so leaving it out of the
# relation sets below reports every ROR-backed institution as an orphan while the
# crate is in fact correct.
_AFFILIATION_KEYS = (
    "affiliation",
    "memberOf",
    "http://schema.org/affiliation",
    "https://schema.org/affiliation",
)
_PROTOCOL_KEYS = (
    "executesLabProtocol",
    # Bioschemas puts properties under /properties/; the bare-namespace form is
    # what this crate used to emit, so both are read to keep older crates legible.
    "https://bioschemas.org/properties/executesLabProtocol",
    "https://bioschemas.org/executesLabProtocol",
)
# A CreateAction's tool/model. Nothing else references the generator's
# SoftwareApplication nodes, so omitting this would report the crate's own
# provenance record as orphaned.
_INSTRUMENT_KEYS = ("instrument", "http://schema.org/instrument")
_ABOUT_GRAPH_KEYS = _ABOUT_KEYS + ("labProcesses",)
_SAMPLETYPE_KEYS = ("sampleType",)
_TABLESCHEMA_KEYS = ("tableSchema", "http://www.w3.org/ns/csvw#tableSchema")
_COLUMNS_KEYS = ("columns", "column", "http://www.w3.org/ns/csvw#column")
_VALUEURL_KEYS = ("valueUrl", "http://www.w3.org/ns/csvw#valueUrl")
_CONFORMSTO_KEYS = ("conformsTo", "http://purl.org/dc/terms/conformsTo")
_CITATION_KEYS = ("citation", "funder", "publisher")
# An entity's identifier PropertyValues (a compound's CAS / PubChem CID / DTXSID,
# a person's ORCID). The compound DOES reference them — `identifier` is how the
# link is expressed — so leaving the predicate out of the vocabulary reported 71
# perfectly-wired nodes as orphans in a real crate. Reachability is only as
# complete as this list.
_IDENTIFIER_REL_KEYS = (
    "identifier",
    "http://schema.org/identifier",
    "https://schema.org/identifier",
)
# The AOP-Wiki subgraph (profiles.context): an AdverseOutcomePathway points at its
# KeyEvents and KeyEventRelationships, and each relationship at its upstream /
# downstream event. Entirely invisible before — the `keyEvent`/`key_events`
# guesses in _MENTIONS_KEYS never matched the names actually serialized.
_AOP_KEYS = (
    "has_key_event",
    "has_key_event_relationship",
    "has_molecular_initiating_event",
    "has_adverse_outcome",
    "upstream_event",
    "downstream_event",
    "https://aopwiki.org/ontology/hasKeyEvent",
    "https://aopwiki.org/ontology/hasKeyEventRelationship",
    "https://aopwiki.org/ontology/hasMolecularInitiatingEvent",
    "https://aopwiki.org/ontology/hasAdverseOutcome",
    "https://aopwiki.org/ontology/upstreamEvent",
    "https://aopwiki.org/ontology/downstreamEvent",
)
# Remaining reference-bearing predicates that reach a contextual node.
_CONTACT_KEYS = ("contactPoint", "http://schema.org/contactPoint")
_PROPERTYURL_KEYS = ("propertyUrl", "http://www.w3.org/ns/csvw#propertyUrl")
_MEASTECH_KEYS = ("measurementTechnique", "measurementMethod", "intendedUse")
# The licence the Root Data Entity declares. Now that a recognised licence is a
# described CreativeWork rather than a bare URL, `schema:license` is a real edge
# from the root — and a predicate missing from this vocabulary is reported as an
# orphan, so the crate accused its own licence of being unreachable while the
# root pointed straight at it.
_LICENSE_KEYS = (
    "license",
    "http://schema.org/license",
    "https://schema.org/license",
)
# A person's role or title WHEN it carries a term rather than a plain string.
# `_refs` ignores literals, so "Associate Professor" contributes no edge and only
# a `{"@id": …}` does — which is how a crate that models the role as a DefinedTerm
# reaches it. Without this the term is reported as an orphan while every author
# points straight at it.
_ROLE_KEYS = (
    "jobTitle",
    "roleName",
    "http://schema.org/jobTitle",
    "https://schema.org/jobTitle",
    "http://schema.org/roleName",
)
_PARAM_KEYS = (
    "parameter",
    "parameterValue",
    "additionalProperty",
    "http://schema.org/additionalProperty",
)

# (keys, label, reversed). Primary edges tell the structural + derivation story;
# secondary edges (CSVW internals, conformsTo, citation/funder) add detail under
# --all-edges. Orphan reachability always uses BOTH sets so it reflects the true
# crate connectivity regardless of what is drawn.
_PRIMARY_RELATIONS: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (_HASPART_KEYS, "hasPart", False),
    (_OUTPUT_KEYS, "result", False),
    (_INPUT_KEYS, "input", True),
    (_LINEAGE_KEYS, "derivesFrom", False),
    (_ABOUT_GRAPH_KEYS, "about", False),
    (_MENTIONS_KEYS, "mentions", False),
    (_AUTHOR_KEYS, "author", False),
    (_CONTRIBUTOR_KEYS, "contributor", False),
    (_AFFILIATION_KEYS, "affiliation", False),
    (_PROTOCOL_KEYS, "executes", False),
    (_INSTRUMENT_KEYS, "instrument", False),
    (_SAMPLETYPE_KEYS, "sampleType", False),
)
_SECONDARY_RELATIONS: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (_TABLESCHEMA_KEYS, "tableSchema", False),
    (_COLUMNS_KEYS, "column", False),
    (_VALUEURL_KEYS, "valueUrl", False),
    (_CONFORMSTO_KEYS, "conformsTo", False),
    (_CITATION_KEYS, "citation", False),
    (_LICENSE_KEYS, "license", False),
    (_ROLE_KEYS, "jobTitle", False),
    (_MEASTECH_KEYS, "measurementTechnique", False),
    (_PARAM_KEYS, "parameter", False),
    (_IDENTIFIER_REL_KEYS, "identifier", False),
    (_AOP_KEYS, "aopEvent", False),
    (_CONTACT_KEYS, "contactPoint", False),
    (_PROPERTYURL_KEYS, "propertyUrl", False),
)


def _entity_layer(node: dict[str, Any]) -> int:
    """Classify a node into a paper layer (1 packaging / 2 ISA / 3 ISA-Tox).

    Caller handles the root short-circuit (root → 1) before this is reached.
    """
    types = _types(node)
    if types & _CSVW_TABLE_TYPES:  # File + csvw:Table → domain table node
        return 3
    add = _additional_type(node)
    if add in _DOMAIN_ADDTYPES:
        return 3
    if add in _STRUCT_ADDTYPES:  # incl. PropertyValue subtypes — stay structural
        return 2
    if types & _DOMAIN_TYPES:
        return 3
    if types & _STRUCT_TYPES:
        return 2
    return 1


# How strongly a category's colour is mixed into a shape's fill.
#
# The two differ on purpose. A node in the derivation chain is 138x48 with a
# label printed across it, so its fill has to stay out of the text's way. An
# overview tile is 13px with no label at all, so the fill IS the signal — at the
# node's strength the ten categories collapse to a row of near-white squares
# (the closest pair measured dE 3.8, which is no difference at all).


# The colour of the bucket for an entity no category claims. Deliberately grey: a
# node drawn in a category colour asserts that the crate said what it is, and an
# unclassified one has not earned that claim.
_CTX_COLOUR = "#6d7b7e"

# Corner brackets, not a box: the shape says "described somewhere else". Every
# other category draws a closed silhouette because the crate said what the thing
# is; an unclassified node has not earned one, and a fourth grey rectangle would
# have made it look like an Organisation that lost its colour.
_CTX_GLYPH = "M2 5V3h2M10 3h2v2M12 9v2h-2M4 11H2V9"


def category_css() -> str:
    """The per-category custom properties, generated from the registry.

    CSS cannot loop, and a category nobody remembered to write a rule for was
    how a protocol ended up with a colour in one view and none anywhere else.
    Generating the properties means a category cannot exist without one.

    Only the properties: the figures that had per-category node and tile rules
    are gone (#618), and the explorer takes each node's colour from the payload
    — which is generated from this same registry — rather than from a class.

    Returns:
        The rules, ready to be substituted into ``maturity_report.css`` at the
        ``__CATEGORY_STYLES__`` placeholder.
    """
    lines = [":root, .mat {"]
    for cat, style in CATEGORY_STYLES.items():
        lines.append(f"  --cat-{cat}:{style.colour};")
    lines.append(f"  --cat-ctx:{_CTX_COLOUR};")
    lines.append("}")
    return "\n".join(lines)


def _entity_category(node: dict[str, Any]) -> str:
    """Functional category of an in-crate entity (drives node colour/shape)."""
    if _is_process(node):
        return "process"
    types = _types(node)
    if "LabProtocol" in types:
        return "protocol"
    if "Dataset" in types:
        return "container"
    if types & {"File", "MediaObject", "Table"}:
        return "data"
    if "MolecularEntity" in types:
        return "chemical"
    if "Sample" in types:
        return "material"
    if "Person" in types:
        return "agent"
    # An organisation is its own category, not a shade of "person": the people
    # view has always drawn the two differently, and folding them together here
    # made the same institution slate-blue in one tab and purple in another.
    if types & _ORG_TYPES:
        return "org"
    if "ScholarlyArticle" in types:
        return "publication"
    # Ahead of the fallback, not in it: an adverse outcome pathway and its key
    # events are what the assay measures, not vocabulary that qualifies it, and
    # `annotation` is by definition the bucket for the latter (#643).
    if types & PATHWAY_TYPES:
        return "pathway"
    return "annotation"


def _find_root_id(nodes: dict[str, Any], graph: list[dict[str, Any]]) -> str | None:
    """The Root Data Entity id: what the metadata descriptor's ``about`` points
    at, else ``./`` / ``""``."""
    for node in graph:
        nid = str(node.get("@id", ""))
        if nid.rsplit("/", 1)[-1] == "ro-crate-metadata.json":
            for ref in _refs(node, _ABOUT_KEYS):
                if ref in nodes:
                    return ref
    for candidate in ("./", ""):
        if candidate in nodes:
            return candidate
    return None


def _all_relations(all_edges: bool) -> tuple[tuple[tuple[str, ...], str, bool], ...]:
    return _PRIMARY_RELATIONS + _SECONDARY_RELATIONS if all_edges else _PRIMARY_RELATIONS


def _id_aliases(ref: str) -> tuple[str, ...]:
    """Spellings of *ref* that name the same node once resolved against the base.

    RO-Crate ids are relative, so ``./#term`` and ``#term`` are the SAME IRI —
    both resolve to ``<base>#term`` — and so are ``./data/x.csv`` and
    ``data/x.csv``. This graph matches ids as text, so the two spellings read as
    two different nodes: a crate that wired a Person's ``jobTitle`` to
    ``./#DefinedTerm_dt_corresponding_author`` had that term reported as an
    orphan while the reference resolved to it perfectly well in RDF.

    The bare root ``./`` is left alone — stripping it yields the empty string,
    which is a different node entirely.
    """
    if not ref or ref == "./":
        return (ref,)
    if ref.startswith("./"):
        return (ref, ref[2:])
    return (ref, f"./{ref}")


def _canonical_ref(ref: str, nodes: dict[str, Any]) -> str:
    """*ref* rewritten to the node id it names, or unchanged when it names none."""
    for alias in _id_aliases(ref):
        if alias in nodes:
            return alias
    return ref


def _extract_edges(
    nodes: dict[str, Any], relations: tuple[tuple[tuple[str, ...], str, bool], ...]
) -> list[dict[str, str]]:
    """All (src, dst, label) edges over ``relations``; endpoints may be off-graph
    references (resolved to status later)."""
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for nid, node in nodes.items():
        for keys, label, reverse in relations:
            for raw in _refs(node, keys):
                ref = _canonical_ref(raw, nodes)
                src, dst = (ref, nid) if reverse else (nid, ref)
                key = (src, dst, label)
                if src != dst and key not in seen:
                    seen.add(key)
                    edges.append({"src": src, "dst": dst, "label": label})
    return edges


def _reachable_from(root: str | None, edges: list[dict[str, str]]) -> set[str]:
    """Undirected reachability from the root over the edge set."""
    if root is None:
        return set()
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e["src"], set()).add(e["dst"])
        adj.setdefault(e["dst"], set()).add(e["src"])
    seen = {root}
    stack = [root]
    while stack:
        cur = stack.pop()
        for nxt in adj.get(cur, ()):  # noqa: SIM118
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _unreachable_clusters(unreached: set[str], edges: list[dict[str, str]]) -> list[list[str]]:
    """Group unreachable in-crate nodes into connected components (undirected).

    A component is one unit of repair. Reachability here is undirected, so a
    single link from **any** member to the reachable graph pulls the entire
    component in with it — twelve entities wired to each other are one missing
    link, not twelve. Reporting a flat count of unreachable entities therefore
    overstates the work by however much structure the crate already has, which is
    exactly backwards: the better-connected the stranded island, the worse the
    number looks.

    Restricted to in-crate nodes on purpose. An edge to an external IRI or to a
    dangling stub cannot carry reachability, so it can never make two entities
    rescuable by one link — counting it would merge components that still need
    separate repairs.

    Returns components as sorted id lists, ordered by their first id, so the
    numbering a reader sees is stable across runs.
    """
    adj: dict[str, set[str]] = {nid: set() for nid in unreached}
    for edge in edges:
        src, dst = edge["src"], edge["dst"]
        if src in adj and dst in adj:
            adj[src].add(dst)
            adj[dst].add(src)

    seen: set[str] = set()
    components: list[list[str]] = []
    for start in sorted(unreached):
        if start in seen:
            continue
        seen.add(start)
        stack, group = [start], []
        while stack:
            cur = stack.pop()
            group.append(cur)
            for nxt in sorted(adj[cur]):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        components.append(sorted(group))
    return components


def build_crate_graph(
    metadata: dict[str, Any] | list[dict[str, Any]],
    *,
    layer: str | int = "all",
    all_edges: bool = False,
) -> dict[str, Any]:
    """Turn a serialized RO-Crate ``@graph`` into a deterministic graph model.

    Classifies every node into a paper layer, marks each referenced ``@id`` as
    ``in_crate`` / ``external`` (resolvable URI, not a node) / ``dangling`` (not a
    node and not resolvable), flags orphans (in-crate nodes not connected to the
    Root Data Entity), and applies a cumulative ``--layer`` filter.

    Args:
        metadata: Parsed ``ro-crate-metadata.json`` dict, the ``@graph`` list, or
            the ``crate.metadata.generate()`` document.
        layer: Cumulative depth — ``1``/``crate``, ``2``/``isa``,
            ``3``/``isa-tox``/``all``. Nodes above the depth are dropped.
        all_edges: Include secondary edges (CSVW internals, conformsTo, citation).

    Returns:
        ``{"nodes": [...], "edges": [...], "hidden_count": int, "counts": {...},
        "root": str|None}``. Each node: ``{id, label, type, layer, status,
        identifier_backed, orphan, reach, cluster, cluster_size}``.

        ``orphan`` stays the union "not reachable from the root". ``reach``
        refines it into ``linked`` / ``isolated`` (unreachable and joined to
        nothing) / ``stranded`` (unreachable but joined to other unreachable
        entities), and ``cluster`` numbers the island a stranded node belongs to.
        ``counts["unreachable_clusters"]`` is how many links would reconnect
        everything — see :func:`_unreachable_clusters`.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    raw = _graph_nodes(graph)
    root_id = _find_root_id(raw, list(graph))

    # In-crate nodes = everything except plumbing descriptors.
    nodes = {
        nid: n
        for nid, n in raw.items()
        if str(nid).rsplit("/", 1)[-1] not in _EXCLUDED_IDS and nid not in _EXCLUDED_IDS
    }

    depth = normalize_layer(layer)

    # Edges for connectivity/status use ALL relations; drawn edges may be a subset.
    full_edges = _extract_edges(nodes, _all_relations(all_edges=True))
    draw_edges = _extract_edges(nodes, _all_relations(all_edges))
    reachable = _reachable_from(root_id, full_edges)

    # Referenced ids that are NOT in-crate nodes → external or dangling stubs.
    # The generated graph artifact is registered in the exported metadata as a
    # ``File`` about the root, but it is intentionally omitted from the graph
    # visualization input to avoid drawing the diagram inside itself. Treat that
    # reserved artifact as external plumbing rather than a dangling entity.
    referenced = {e["src"] for e in full_edges} | {e["dst"] for e in full_edges}
    stub_ids = {
        r for r in referenced
        if r not in nodes and str(r).rsplit("/", 1)[-1] not in _EXCLUDED_IDS
    }

    def _layer_of(nid: str) -> int:
        return 1 if nid == root_id else _entity_layer(nodes[nid])

    # Split "unreachable" into the two states a reader can act on differently:
    # an entity linked to nothing at all, and an island of entities linked to
    # each other but not to the root. The second is one link from being fixed
    # however large it is (#see _unreachable_clusters).
    unreached = {nid for nid in nodes if nid != root_id and nid not in reachable}
    clusters = _unreachable_clusters(unreached, full_edges)
    cluster_of: dict[str, int] = {}
    for index, group in enumerate(clusters, start=1):
        for nid in group:
            cluster_of[nid] = index
    cluster_size = {nid: len(clusters[i - 1]) for nid, i in cluster_of.items()}

    model_nodes: dict[str, dict[str, Any]] = {}
    for nid, node in nodes.items():
        lyr = _layer_of(nid)
        is_root = nid == root_id
        id_backed = _is_uri(nid) or bool(node.get("identifier") or node.get("url"))
        raw_label = _name(node)
        label = "Crate root" if is_root and raw_label in (nid, "./", "") else raw_label
        model_nodes[nid] = {
            "id": nid,
            # Badged, not raw: a generated file's name leads with a 16-character
            # warning, and a node label is ellipsised long before that — so every
            # one of them read as "AUTOGENERATED — C…", warning intact and
            # filename gone. `name` keeps the crate's own wording for the places
            # that have room for it (#618: the diagrams that used to do this are
            # gone, and the explorer is where labels are read now).
            "label": _escape(_display_name(node) if not is_root else label),
            "name": _escape(label),
            "type": _tag(node),
            "category": _entity_category(node),
            "layer": lyr,
            "status": "in_crate",
            "identifier_backed": id_backed,
            # Kept as the union of the two unreachable states so every existing
            # consumer (mermaid overlay, matrices, topology strip) is unaffected.
            "orphan": nid in unreached,
            "reach": (
                "linked"
                if nid not in unreached
                else ("isolated" if cluster_size.get(nid, 1) == 1 else "stranded")
            ),
            "cluster": cluster_of.get(nid),
            "cluster_size": cluster_size.get(nid),
        }
    for sid in stub_ids:
        external = _is_uri(sid)
        model_nodes[sid] = {
            "id": sid,
            "label": _escape(_external_label(sid)),
            "type": "external" if external else "unresolved",
            "category": None,
            "layer": None,
            "status": "external" if external else "dangling",
            "identifier_backed": external,
            "orphan": False,
            "reach": "linked",
            "cluster": None,
            "cluster_size": None,
        }

    # Cumulative layer filter: drop in-crate nodes deeper than `depth`; drop
    # stubs that lose their only connection; drop edges touching dropped nodes.
    kept = {nid for nid, n in model_nodes.items() if n["layer"] is None or n["layer"] <= depth}
    visible_edges = [e for e in draw_edges if e["src"] in kept and e["dst"] in kept]
    connected_stubs = {e["src"] for e in visible_edges} | {e["dst"] for e in visible_edges}
    final_nodes = [
        n
        for nid, n in model_nodes.items()
        if nid in kept and (n["layer"] is not None or nid in connected_stubs)
    ]
    final_ids = {n["id"] for n in final_nodes}
    visible_edges = [e for e in visible_edges if e["src"] in final_ids and e["dst"] in final_ids]

    hidden_count = sum(
        1 for n in model_nodes.values() if n["layer"] is not None and n["layer"] > depth
    )
    counts = {
        "layer1": sum(1 for n in model_nodes.values() if n["layer"] == 1),
        "layer2": sum(1 for n in model_nodes.values() if n["layer"] == 2),
        "layer3": sum(1 for n in model_nodes.values() if n["layer"] == 3),
        "external": sum(1 for n in model_nodes.values() if n["status"] == "external"),
        "dangling": sum(1 for n in model_nodes.values() if n["status"] == "dangling"),
        "orphan": sum(1 for n in model_nodes.values() if n["orphan"]),
        "isolated": sum(1 for n in model_nodes.values() if n.get("reach") == "isolated"),
        "stranded": sum(1 for n in model_nodes.values() if n.get("reach") == "stranded"),
        # The repair estimate: one link per component reconnects every entity in
        # it, so this — not `orphan` — is the number of edits the crate needs.
        "unreachable_clusters": len(clusters),
    }

    return {
        "nodes": final_nodes,
        "edges": visible_edges,
        "hidden_count": hidden_count,
        "counts": counts,
        "root": root_id,
    }


def _external_label(uri: str) -> str:
    """A compact, human-readable label for an off-graph reference."""
    if not _is_uri(uri):
        return uri  # dangling local ref — show as-is
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    host = uri.split("://", 1)[-1].split("/", 1)[0].replace("www.", "")
    return f"{tail} ({host})" if tail and tail != host else host


# ---------------------------------------------------------------------------
# Chemical inventory (#85) — which compounds a crate declares, how each one
# reaches the experiment, and how completely it is identified.
#
# A tox crate's compounds are the one thing a receiving lab must be able to pin
# down exactly, and they are also the easiest thing to leave dangling. ISA
# forbids a MolecularEntity as a LabProcess ``object`` (objects MUST be
# File/Sample/BioSample), so a compound is *never* wired to its Exposure
# directly: it hangs off the produced condition table
# (``table --about--> MolecularEntity``, see ``_crate_mapping._synth_condition_table``),
# off that table's ``compound`` CSVW column via ``valueUrl``, and — at a glance —
# off the Study via ``schema:mentions``. Miss that indirection and the crate
# still validates while every compound sits orphaned: fully described, but
# unreachable from the experiment that used it.
#
# This model therefore reports both halves, because either alone is misleading:
#   * the ROUTE      — which process produced which table which names which
#                      compound (or where that chain breaks), and
#   * the IDENTITY   — CAS / PubChem CID / DTXSID plus the structure fields that
#                      let someone else obtain the same substance.
# ---------------------------------------------------------------------------

_CHEMICAL_TYPES = frozenset({"MolecularEntity", "ChemicalSubstance"})

_IDENTIFIER_KEYS: tuple[str, ...] = (
    "identifier",
    "http://schema.org/identifier",
    "https://schema.org/identifier",
)
_VALUE_KEYS: tuple[str, ...] = ("value", "http://schema.org/value", "https://schema.org/value")
_PROPERTYID_KEYS: tuple[str, ...] = (
    "propertyID",
    "http://schema.org/propertyID",
    "https://schema.org/propertyID",
)

# A bare `identifier` string that is a CAS Registry Number (2-7 / 2 / 1 check
# digit) — ro-crate-py serializes some crates that way instead of minting an
# identifier PropertyValue, and dropping it would under-report identification.
_CAS_RE = re.compile(r"\d{2,7}-\d{2}-\d")

# Registry identifier schemes in the order ``_crate_mapping._MOLECULAR_IDENTIFIERS``
# mints them (CAS → PubChem CID → EPA DTXSID). ``aliases`` are matched
# case-folded against the identifier PropertyValue's ``name``; ``hosts`` against
# its ``propertyID`` *and* against the compound's own ``@id``, so a crate whose
# @id is the PubChem/CompTox page still counts as carrying that identifier even
# when it never spells the scheme out as a PropertyValue.
# ``pattern`` guards the URL fallback below: a host match alone would turn ANY
# PubChem page (a /bioassay/, a /patent/) into a "CID", inflating the
# identification score with a value that identifies nothing.
_CHEM_ID_SCHEMES: tuple[
    tuple[str, str, tuple[str, ...], tuple[str, ...], "re.Pattern[str] | None"], ...
] = (
    ("CAS", "CAS", ("cas", "casrn", "cas rn", "cas number", "cas registry number"), (), _CAS_RE),
    (
        "PubChem CID",
        "CID",
        ("pubchem cid", "pubchem", "cid"),
        ("pubchem.ncbi.nlm.nih.gov/compound",),
        re.compile(r"\d+"),
    ),
    (
        "DTXSID",
        "DTXSID",
        ("dtxsid", "dsstox", "dsstox substance id"),
        ("comptox.epa.gov/dashboard/chemical",),
        re.compile(r"DTXSID\w+", re.IGNORECASE),
    ),
)

# Where an identifier can be looked up — the external source behind a ✓ in the
# identification matrix, keyed by the scheme label of ``_CHEM_ID_SCHEMES`` /
# ``_CHEM_STRUCTURE_FIELDS``. A field with no public resolver (SMILES, formula,
# mass) has no entry and stays a plain mark. The value is percent-encoded into
# the template by :func:`chem_source_url`; it is crate text.
CHEM_SOURCE_URLS: dict[str, str] = {
    "CAS": "https://commonchemistry.cas.org/detail?cas_rn={}",
    "PubChem CID": "https://pubchem.ncbi.nlm.nih.gov/compound/{}",
    "DTXSID": "https://comptox.epa.gov/dashboard/chemical/details/{}",
    "InChIKey": "https://pubchem.ncbi.nlm.nih.gov/#query={}",
}


def chem_source_url(scheme: str, value: str | None) -> str | None:
    """The page where *scheme*'s identifier *value* can be looked up, or
    ``None`` when the scheme has no public resolver or the value is empty."""
    template = CHEM_SOURCE_URLS.get(scheme)
    if not template or not value or not str(value).strip():
        return None
    return template.format(urllib.parse.quote(str(value).strip(), safe=""))


# Structure/characterisation fields carried directly on the compound. The
# compact aliases are the ``profiles.context`` terms; the schema.org IRIs cover
# an expanded document.
_CHEM_STRUCTURE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "InChIKey",
        ("inchikey", "inChIKey", "http://schema.org/inChIKey", "https://schema.org/inChIKey"),
    ),
    ("SMILES", ("smiles", "http://schema.org/smiles", "https://schema.org/smiles")),
    (
        "Formula",
        (
            "formula",
            "molecularFormula",
            "molecular_formula",
            "http://schema.org/molecularFormula",
            "https://schema.org/molecularFormula",
        ),
    ),
    (
        "Mass",
        (
            "mass",
            "molecularWeight",
            "molecular_weight",
            "http://schema.org/molecularWeight",
            "https://schema.org/molecularWeight",
        ),
    ),
)

# The coverage matrix columns: (full name, column header). Registry identifiers
# first (they are what makes a compound orderable), then structure.
CHEM_COVERAGE_FIELDS: tuple[tuple[str, str], ...] = tuple(
    (scheme, short) for scheme, short, *_rest in _CHEM_ID_SCHEMES
) + tuple((label, label) for label, _keys in _CHEM_STRUCTURE_FIELDS)

# The less canonical ways an entity can still be referenced. They are scanned
# last (a canonical route is preferred when both exist) but they MUST be scanned:
# the panel states outright that "nothing in the crate references this compound",
# and the topology strip a few lines below it reaches the same node over the
# fuller `_PRIMARY_RELATIONS`/`_SECONDARY_RELATIONS` vocabulary. Omitting them
# let the two halves of one section contradict each other.
_EXTRA_LINK_RELATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_OUTPUT_KEYS, "result"),
    (_LINEAGE_KEYS, "derivesFrom"),
    (_HASPART_KEYS, "hasPart"),
    (_PARAM_KEYS, "parameter"),
)

# Relations by which some other entity can point AT a compound, most canonical
# first — the order decides which route is drawn when a compound is reachable
# more than one way. ``about`` is the condition table's link, ``valueUrl`` the
# per-well column link, ``mentions`` (which subsumes the ``chemicals`` alias) the
# Study-level one; ``input``/``parameter`` are non-conformant but real, and a
# crate that uses them should see them rather than be told it has no route.
_CHEM_LINK_RELATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_ABOUT_KEYS, "about"),
    (_VALUEURL_KEYS, "valueUrl"),
    (_MENTIONS_KEYS, "mentions"),
    (_INPUT_KEYS, "input"),
) + _EXTRA_LINK_RELATIONS
# Containment relations walked *upward* from a referrer to the entity a process
# actually produced: a `compound` column is owned by a csvw:Schema, which is the
# tableSchema of the condition table, which is the Exposure's result.
_CHEM_CONTAINMENT_KEYS: tuple[tuple[str, ...], ...] = (_TABLESCHEMA_KEYS, _COLUMNS_KEYS)
_CHEM_ANCHOR_HOPS = 3


def _graph_nodes(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Index a serialized document by ``@id``, skipping malformed entries.

    ``@id`` MUST be a string, but a hand-edited or machine-mangled crate can
    carry a number or an object there. Indexing on it unguarded raises
    ``TypeError`` out of the report writer, and because the report is built
    inside ``export_crate`` that costs the crate its entire maturity report over
    one bad node. Skipping the node degrades one row instead.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    return {
        n["@id"]: n
        for n in graph
        if isinstance(n, dict) and isinstance(n.get("@id"), str)
    }


def _is_chemical(node: dict[str, Any]) -> bool:
    return bool(_types(node) & _CHEMICAL_TYPES)


def _literal(node: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """First non-empty literal across ``keys``, unwrapping a list / ``@value``."""
    for key in keys:
        value = node.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            value = value.get("@value")
        if value not in (None, "", []):
            return str(value)
    return None


def _registry_identifiers(
    node: dict[str, Any],
    nodes: dict[str, Any],
    schemes: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], Any], ...],
) -> dict[str, str]:
    """Persistent identifiers an entity carries, as ``{scheme: value}``.

    Reads the ``identifier`` PropertyValues that ``_crate_mapping`` mints —
    matching the scheme by the PropertyValue's ``name`` or its ``propertyID``
    host — then falls back to the entity's own ``@id`` when that is itself a
    registry URL (an ORCID, a ROR, a PubChem compound page). The @id fallback
    matters: it is the *most* actionable form of the identifier, and a crate that
    uses it without also minting a PropertyValue is better identified, not worse.
    """
    found: dict[str, str] = {}

    def _scheme_of(name: str | None, url: str | None) -> str | None:
        key = (name or "").strip().casefold().replace("_", " ")
        for scheme, _short, aliases, hosts, _pat in schemes:
            if key and key in aliases:
                return scheme
            if url and any(host in url for host in hosts):
                return scheme
        return None

    def _from_url(url: str) -> tuple[str, str] | None:
        """``(scheme, value)`` when *url* is a registry URL whose tail actually
        looks like that registry's identifier."""
        for scheme, _short, _aliases, hosts, pattern in schemes:
            if not hosts or not any(host in url for host in hosts):
                continue
            tail = url.rstrip("/").rsplit("/", 1)[-1]
            if not tail:
                return None
            if pattern is None:
                return (scheme, tail)
            match = pattern.fullmatch(tail)
            # A host match alone is not an identifier: /bioassay/1234 lives on the
            # PubChem host and identifies no compound.
            return (scheme, match.group(0)) if match else None
        return None

    for key in _IDENTIFIER_KEYS:
        for item in _as_list(node.get(key)):
            if not isinstance(item, dict):
                continue
            pv = nodes.get(item["@id"], item) if isinstance(item.get("@id"), str) else item
            prop_id = _first(pv, _PROPERTYID_KEYS)
            if isinstance(prop_id, dict):
                prop_id = prop_id.get("@id")
            scheme = _scheme_of(
                _literal(pv, _NAME_KEYS), prop_id if isinstance(prop_id, str) else None
            )
            value = _literal(pv, _VALUE_KEYS)
            if scheme and value and scheme not in found:
                found[scheme] = value
                continue
            # The identifier node may BE the registry URL (an ORCID or a ROR
            # given as {"@id": "https://orcid.org/…"}) with no separate value.
            own = pv.get("@id")
            if isinstance(own, str) and (hit := _from_url(own)) and hit[0] not in found:
                found[hit[0]] = hit[1]

    for candidate in (_literal(node, _URL_KEYS), node.get("@id")):
        if not isinstance(candidate, str):
            continue
        hit = _from_url(candidate)
        if hit and hit[0] not in found:
            found[hit[0]] = hit[1]
    return found


def _chem_identifiers(node: dict[str, Any], nodes: dict[str, Any]) -> dict[str, str]:
    """Registry identifiers a compound carries: ``{scheme: value}``.

    :func:`_registry_identifiers` plus the bare-string CAS fallback — some crates
    serialize ``identifier`` as the literal Registry Number rather than as a
    PropertyValue, and dropping it would under-report identification.
    """
    found = _registry_identifiers(node, nodes, _CHEM_ID_SCHEMES)
    if "CAS" not in found:
        for key in _IDENTIFIER_KEYS:
            for item in _as_list(node.get(key)):
                if isinstance(item, str) and _CAS_RE.fullmatch(item.strip()):
                    found["CAS"] = item.strip()
                    return found
    return found


def _referrers_to(
    nodes: dict[str, Any],
    member_ids: set[str],
    relations: tuple[tuple[tuple[str, ...], str], ...],
) -> dict[str, list[tuple[str, str]]]:
    """``member id -> [(referrer id, relation label)]``, deterministically ordered.

    References *between* members are ignored: they are not a route into the
    experiment, and counting them would let a cluster of mutually-referencing
    entities mask the fact that none of them reaches a process.
    """
    order = {label: i for i, (_keys, label) in enumerate(relations)}
    out: dict[str, set[tuple[str, str]]] = {}
    for nid, node in nodes.items():
        if nid in member_ids or str(nid).rsplit("/", 1)[-1] in _EXCLUDED_IDS:
            continue
        for keys, label in relations:
            for ref in _refs(node, keys):
                if ref in member_ids:
                    out.setdefault(ref, set()).add((nid, label))
    return {mid: sorted(pairs, key=lambda p: (order[p[1]], p[0])) for mid, pairs in out.items()}


def _route_resolver(
    nodes: dict[str, Any],
    member_ids: set[str],
    relations: tuple[tuple[tuple[str, ...], str], ...],
) -> Any:
    """Build ``resolve(member_id) -> route | None`` for a routed inventory.

    A *route* is how a reader gets from a ``LabProcess`` to the entity:
    ``{process, via, link, edge}``. Some referrers are not themselves produced by
    a process — a CSVW column is owned by a schema which is the ``tableSchema`` of
    the table a process actually produced — so the referrer is walked up the
    containment chain to the nearest process-produced ancestor before giving up.

    ``process`` is ``None`` when the entity is referenced in-crate but by nothing
    a process produced; the whole route is ``None`` when nothing references it.
    """
    referrers = _referrers_to(nodes, member_ids, relations)

    producers: dict[str, list[str]] = {}
    parents: dict[str, list[str]] = {}
    for nid, node in nodes.items():
        if _is_process(node):
            for dst in _refs(node, _OUTPUT_KEYS):
                producers.setdefault(dst, []).append(nid)
        for keys in _CHEM_CONTAINMENT_KEYS:
            for ref in _refs(node, keys):
                parents.setdefault(ref, []).append(nid)

    def _anchor_of(ref_id: str) -> str | None:
        """Nearest ancestor of *ref_id* (itself included) that a process produced."""
        seen: set[str] = set()
        frontier = [ref_id]
        for _ in range(_CHEM_ANCHOR_HOPS + 1):
            nxt: list[str] = []
            for cur in frontier:
                if cur in seen:
                    continue
                seen.add(cur)
                if cur in producers:
                    return cur
                nxt.extend(parents.get(cur, ()))
            if not nxt:
                break
            frontier = sorted(nxt)
        return None

    def _resolve(mid: str) -> dict[str, Any] | None:
        fallback: dict[str, Any] | None = None
        for rid, label in referrers.get(mid, []):
            if _is_process(nodes[rid]):
                return {"process": rid, "via": rid, "link": rid, "edge": label}
            anchor = _anchor_of(rid)
            if anchor is not None:
                return {
                    "process": sorted(producers[anchor])[0],
                    "via": anchor,
                    "link": rid,
                    "edge": label,
                }
            if fallback is None:
                fallback = {"process": None, "via": rid, "link": rid, "edge": label}
        return fallback

    return _resolve


_ROUTE_STATE_ORDER = {"wired": 0, "mentioned": 1, "unlinked": 2}


def _route_state(route: dict[str, Any] | None) -> str:
    if route is None:
        return "unlinked"
    return "wired" if route["process"] else "mentioned"


def _route_bands(
    members: list[dict[str, Any]], nodes: dict[str, Any]
) -> list[dict[str, Any]]:
    """Group routed members into the diagram's bands.

    Members sharing a ``(process, via)`` pair travel together — the relation is
    *not* part of the key, so a table that reaches some of its members by
    ``about`` and others by the column's ``valueUrl`` still draws one band; the
    band's edge label then names both mechanisms rather than the diagram
    repeating the same process and table twice.
    """
    banded: dict[tuple[str, str | None, str | None], list[dict[str, Any]]] = {}
    for member in members:
        route = member["route"] or {}
        banded.setdefault(
            (member["state"], route.get("process"), route.get("via")), []
        ).append(member)
    return [
        {
            "state": state,
            "edge": " · ".join(sorted({m["route"]["edge"] for m in group if m["route"]})),
            "process": _chem_node_brief(proc, nodes[proc]) if proc else None,
            "via": _chem_node_brief(via, nodes[via]) if via else None,
            "members": group,
        }
        for (state, proc, via), group in sorted(
            banded.items(),
            key=lambda kv: (_ROUTE_STATE_ORDER[kv[0][0]], kv[0][1] or "", kv[0][2] or ""),
        )
    ]


def _route_counts(members: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(members),
        "wired": sum(1 for m in members if m["state"] == "wired"),
        "mentioned": sum(1 for m in members if m["state"] == "mentioned"),
        "unlinked": sum(1 for m in members if m["state"] == "unlinked"),
        "fields_met": sum(m["met"] for m in members),
        "fields_total": sum(m["total"] for m in members),
    }


_EMPTY_ROUTE_COUNTS = {
    "total": 0,
    "wired": 0,
    "mentioned": 0,
    "unlinked": 0,
    "fields_met": 0,
    "fields_total": 0,
}


def _chem_node_brief(nid: str, node: dict[str, Any]) -> dict[str, str]:
    """The minimal description a drawn node needs.

    ``name``/``tag`` are the RAW crate strings and ``label`` the escaped name.
    Both are carried because the two consumers escape at different points: the
    HTML matrix interpolates ``label`` directly, while the SVG must ellipsise
    *before* escaping — truncating an escaped string can cut a character
    reference (``&amp;``) in half.
    """
    return {"id": nid, "name": _name(node), "label": _escape(_name(node)), "tag": _tag(node)}


def build_chemical_inventory(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Model the crate's compounds: their route into the experiment + their identity.

    For every ``MolecularEntity`` the crate declares, resolve how it is reachable
    from a ``LabProcess`` — normally ``process --result--> table --about--> compound``,
    or via the condition table's ``compound`` column ``valueUrl`` — and how many
    of the identification fields (CAS / PubChem CID / DTXSID / InChIKey / SMILES /
    formula / mass) it actually carries.

    Pure and cheap: one pass over the serialized ``@graph``, no validation and no
    network. All crate-controlled text on the returned nodes is HTML-escaped
    (#169) so it can be interpolated straight into the report.

    Args:
        metadata: Parsed ``ro-crate-metadata.json`` dict, the ``@graph`` list, or
            the ``crate.metadata.generate()`` document.

    Returns:
        ``{"chemicals": [...], "groups": [...], "counts": {...}}``.

        Each chemical: ``{id, label, tag, resolvable, identifiers, structure,
        met, total, state, route}`` where ``state`` is ``"wired"`` (reachable
        from a process), ``"mentioned"`` (referenced in-crate but by nothing a
        process produced) or ``"unlinked"`` (referenced by nothing at all).

        ``groups`` are the diagram's route bands — compounds sharing the same
        ``(process, via)`` pair — ordered wired → mentioned → unlinked. ``counts``
        carries ``total`` / ``wired`` / ``mentioned`` / ``unlinked`` plus
        ``fields_met`` / ``fields_total`` for the identification coverage.
    """
    nodes = _graph_nodes(metadata)
    chem_ids = {nid for nid, n in nodes.items() if _is_chemical(n)}
    if not chem_ids:
        return {"chemicals": [], "groups": [], "counts": dict(_EMPTY_ROUTE_COUNTS)}

    resolve = _route_resolver(nodes, chem_ids, _CHEM_LINK_RELATIONS)

    chemicals: list[dict[str, Any]] = []
    for cid in sorted(chem_ids, key=lambda c: (_name(nodes[c]).casefold(), c)):
        node = nodes[cid]
        ids = _chem_identifiers(node, nodes)
        fields: dict[str, bool] = {
            scheme: scheme in ids for scheme, *_rest in _CHEM_ID_SCHEMES
        }
        structure = {
            label: value
            for label, keys in _CHEM_STRUCTURE_FIELDS
            if (value := _literal(node, keys)) is not None
        }
        fields.update({label: label in structure for label, _keys in _CHEM_STRUCTURE_FIELDS})
        route = resolve(cid)
        chemicals.append(
            {
                **_chem_node_brief(cid, node),
                "resolvable": _is_uri(cid),
                "identifiers": ids,
                "structure": structure,
                "fields": fields,
                "met": sum(1 for ok in fields.values() if ok),
                "total": len(fields),
                "state": _route_state(route),
                "route": route,
            }
        )

    return {
        "chemicals": chemicals,
        "groups": _route_bands(chemicals, nodes),
        "counts": _route_counts(chemicals),
    }


# ---------------------------------------------------------------------------
# Cell lines (#85) — the biological test system, and whether it is pinned down.
#
# A cell line is the other half of "what was tested", and it fails the same two
# ways a compound does. It is *unreachable* when the CellCulture consumes a
# freshly minted generic Sample instead of the declared CellLineSample — the line
# is then described in the crate and used by nothing, exactly the shape the
# compound view exposes. And it is *unidentified* when it carries a name but no
# Cellosaurus RRID: "CHO-K1" names a family of divergent stocks, RRID CVCL_0214
# names one, and passage/organ/tissue are what let another lab reproduce the
# culture rather than merely recognise it.
# ---------------------------------------------------------------------------

_CELLLINE_TYPES = frozenset({"CellLine", "CellLineSample"})
_CELL_LINE_TERM_HINT = "cell line"

# Cellosaurus accessions, with or without the RRID: prefix (RRID:CVCL_0214).
_RRID_RE = re.compile(r"(?:RRID[:\s]*)?CVCL[_:][A-Z0-9]+", re.IGNORECASE)

_ADDPROP_KEYS: tuple[str, ...] = (
    "additionalProperty",
    "http://schema.org/additionalProperty",
    "https://schema.org/additionalProperty",
)
_URL_KEYS: tuple[str, ...] = ("url", "http://schema.org/url", "https://schema.org/url")

# ISA Sample Characteristics that make a culture reproducible, matched
# case-folded against each additionalProperty PropertyValue's ``name`` (the
# shape ``_crate_mapping._CELL_LINE_CHARACTERISTICS`` mints).
_CELLLINE_CHARACTERISTICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Organ", ("organ",)),
    ("Tissue", ("tissue",)),
    ("Passage", ("passage", "passage number")),
)

CELLLINE_COVERAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Cellosaurus RRID", "RRID"),
    ("Typed as a cell line", "Type"),
) + tuple((label, label) for label, _keys in _CELLLINE_CHARACTERISTICS)

# How something can point at a cell line. ``input`` is canonical and first: the
# CellCulture consumes the line (``_INPUT_KEYS`` already covers the ``cell_line``
# alias). ``derivesFrom`` catches the cultured Sample that descends from it, and
# ``mentions`` the Study-level ``cell_lines``/``biologicalModels`` alias.
_CELLLINE_LINK_RELATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_INPUT_KEYS, "input"),
    (_ABOUT_KEYS, "about"),
    (_VALUEURL_KEYS, "valueUrl"),
    (_MENTIONS_KEYS, "mentions"),
) + _EXTRA_LINK_RELATIONS


def _is_cellline(node: dict[str, Any], nodes: dict[str, Any]) -> bool:
    """True for a cell-line entity.

    Accepts the canonical ISA-Tox shape (a ``Sample`` with
    ``additionalType: CellLine``), a bare ``CellLine`` type, and a ``Sample``
    whose ``sampleType`` resolves to the shared "cell line" ``DefinedTerm`` —
    the last so a crate that types its line only by term still appears.
    """
    if _types(node) & _CELLLINE_TYPES or _additional_type(node) in _CELLLINE_TYPES:
        return True
    if "Sample" not in _types(node):
        return False
    return any(
        _CELL_LINE_TERM_HINT in (_literal(nodes[ref], _NAME_KEYS) or "").casefold()
        for ref in _refs(node, _SAMPLETYPE_KEYS)
        if ref in nodes
    )


def _cellline_rrid(node: dict[str, Any], nodes: dict[str, Any]) -> str | None:
    """The Cellosaurus accession, from an ``identifier``, a ``url`` or the ``@id``."""
    candidates: list[str] = []
    for key in _IDENTIFIER_KEYS:
        for item in _as_list(node.get(key)):
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                pv = nodes.get(item["@id"], item) if "@id" in item else item
                candidates.extend(
                    v for v in (_literal(pv, _VALUE_KEYS), pv.get("@id")) if isinstance(v, str)
                )
    candidates.extend(
        v for v in (_literal(node, _URL_KEYS), node.get("@id")) if isinstance(v, str)
    )
    for cand in candidates:
        match = _RRID_RE.search(cand)
        if match:
            return match.group(0)
    return None


def _cellline_characteristics(node: dict[str, Any], nodes: dict[str, Any]) -> dict[str, bool]:
    """Which reproducibility characteristics (organ / tissue / passage) are recorded.

    Read from the ISA ``additionalProperty`` PropertyValues the builder mints,
    falling back to a bare literal on the entity so an externally-authored crate
    that never promoted them still scores.
    """
    present: set[str] = set()
    for key in _ADDPROP_KEYS:
        for item in _as_list(node.get(key)):
            if not isinstance(item, dict):
                continue
            pv = nodes.get(item["@id"], item) if "@id" in item else item
            if not isinstance(pv, dict) or _literal(pv, _VALUE_KEYS) is None:
                continue
            name = (_literal(pv, _NAME_KEYS) or "").strip().casefold()
            for label, aliases in _CELLLINE_CHARACTERISTICS:
                if name in aliases:
                    present.add(label)
    for label, aliases in _CELLLINE_CHARACTERISTICS:
        if label not in present and _literal(node, aliases) is not None:
            present.add(label)
    return {label: label in present for label, _aliases in _CELLLINE_CHARACTERISTICS}


def build_cellline_inventory(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Model the crate's cell lines: their route into the experiment + their identity.

    Resolves, for every cell-line entity, how it is reachable from a
    ``LabProcess`` — canonically the ``CellCulture`` that consumes it as
    ``input``/``cell_line``, or the cultured ``Sample`` that ``derivesFrom`` it —
    and whether it carries a Cellosaurus RRID, an ontology-backed ``sampleType``,
    and the organ / tissue / passage characteristics another lab needs to
    reproduce the culture.

    Pure and cheap: one pass over the serialized ``@graph``, no validation and no
    network. Crate-controlled text is HTML-escaped in ``label`` (#169).

    Returns:
        ``{"celllines": [...], "groups": [...], "counts": {...}}``, shaped exactly
        like :func:`build_chemical_inventory` so both feed the same renderer —
        ``state`` is ``"wired"`` / ``"mentioned"`` / ``"unlinked"``.
    """
    nodes = _graph_nodes(metadata)
    line_ids = {nid for nid, n in nodes.items() if _is_cellline(n, nodes)}
    if not line_ids:
        return {"celllines": [], "groups": [], "counts": dict(_EMPTY_ROUTE_COUNTS)}

    resolve = _route_resolver(nodes, line_ids, _CELLLINE_LINK_RELATIONS)

    lines: list[dict[str, Any]] = []
    for lid in sorted(line_ids, key=lambda c: (_name(nodes[c]).casefold(), c)):
        node = nodes[lid]
        rrid = _cellline_rrid(node, nodes)
        typed = bool(
            _additional_type(node) in _CELLLINE_TYPES
            or _types(node) & _CELLLINE_TYPES
            or any(ref in nodes for ref in _refs(node, _SAMPLETYPE_KEYS))
        )
        fields: dict[str, bool] = {
            "Cellosaurus RRID": rrid is not None,
            "Typed as a cell line": typed,
        }
        fields.update(_cellline_characteristics(node, nodes))
        route = resolve(lid)
        lines.append(
            {
                **_chem_node_brief(lid, node),
                "resolvable": _is_uri(lid),
                "rrid": rrid,
                "fields": fields,
                "met": sum(1 for ok in fields.values() if ok),
                "total": len(fields),
                "state": _route_state(route),
                "route": route,
            }
        )

    return {
        "celllines": lines,
        "groups": _route_bands(lines, nodes),
        "counts": _route_counts(lines),
    }


_PERSON_TYPES = frozenset({"Person"})
_ORG_TYPES = frozenset(
    {
        "Organization",
        "ResearchOrganization",
        "EducationalOrganization",
        "GovernmentOrganization",
        "Corporation",
        "NGO",
        "Consortium",
        "Project",
        "FundingAgency",
    }
)

# Persistent-identifier schemes for agents, resolved the same way compounds are
# (PropertyValue name / propertyID host / the entity's own @id).
_AGENT_ID_SCHEMES: tuple[
    tuple[str, str, tuple[str, ...], tuple[str, ...], "re.Pattern[str] | None"], ...
] = (
    (
        "ORCID",
        "ORCID",
        ("orcid", "orcid id", "orcid.org"),
        ("orcid.org/",),
        re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dXx]"),
    ),
    ("ROR", "ROR", ("ror", "ror id"), ("ror.org/",), re.compile(r"0[0-9a-z]{6}[0-9]{2}")),
    ("ISNI", "ISNI", ("isni",), ("isni.org/isni/",), re.compile(r"[\d ]{15,20}[\dXx]")),
    ("GRID", "GRID", ("grid", "grid id"), ("grid.ac/institutes/",), re.compile(r"grid\.\d+\.\w+")),
)
# Which of those count as "identified" for each kind, most preferred first.
_PID_FOR_KIND: dict[str, tuple[str, ...]] = {
    "person": ("ORCID", "ISNI"),
    "org": ("ROR", "GRID", "ISNI"),
}

# Ways an entity can credit an agent, most canonical first.
_CREDIT_RELATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_AUTHOR_KEYS, "author"),
    (("contributor", "http://schema.org/contributor"), "contributor"),
    (("publisher", "http://schema.org/publisher"), "publisher"),
    (("funder", "http://schema.org/funder"), "funder"),
    (("maintainer", "http://schema.org/maintainer"), "maintainer"),
    (("sponsor", "provider", "sourceOrganization"), "provider"),
)

AGENT_COVERAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Persistent identifier", "PID"),
    ("Name", "Name"),
    ("Affiliation", "Affiliation"),
    # "Linked", not "Credited": an organisation is normally reached through a
    # person's affiliation rather than credited directly, and that is a correct
    # crate — the column asks whether anything connects the agent at all.
    ("Linked in crate", "Linked"),
)


def _agent_kind(node: dict[str, Any]) -> str | None:
    """``"person"`` / ``"org"`` for an agent node, else ``None``."""
    types = _types(node)
    if types & _PERSON_TYPES:
        return "person"
    if types & _ORG_TYPES:
        return "org"
    return None


def build_people_inventory(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Model the crate's attribution: who is credited, by what, and how resolvably.

    Resolves, for every ``Person`` and ``Organization``: the persistent
    identifier it carries (ORCID / ROR / ISNI / GRID, from an ``identifier``
    PropertyValue or from the entity's own ``@id``), whether a person declares an
    ``affiliation``, and which entity credits it (``author`` / ``contributor`` /
    ``publisher`` / ``funder`` / …). The Root Data Entity is preferred as a
    person's credit source when several apply, so the common case — the crate
    itself crediting its authors — is the one the diagram draws.

    Pure and cheap: one pass over the serialized ``@graph``, no validation and no
    network. Crate-controlled text is HTML-escaped in ``label`` (#169).

    Args:
        metadata: Parsed ``ro-crate-metadata.json`` dict, the ``@graph`` list, or
            the ``crate.metadata.generate()`` document.

    Returns:
        ``{"agents": [...], "groups": [...], "counts": {...}}``.

        Each agent: ``{id, name, label, tag, kind, pid, pid_scheme, resolvable,
        affiliations, fields, met, total, state}`` where ``state`` is
        ``"credited"`` (something in the crate credits it), ``"affiliated"`` (an
        organisation reached only through a person's affiliation) or
        ``"unattached"`` (nothing references it — the duplicate-institution
        signature).

        ``groups`` are the diagram's bands: a credit source, the people it
        credits, and those people's organisations.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    nodes = _graph_nodes(metadata)
    kinds = {nid: _agent_kind(n) for nid, n in nodes.items()}
    agent_ids = {nid for nid, kind in kinds.items() if kind}
    empty_counts = {
        "people": 0,
        "orgs": 0,
        "total": 0,
        "credited": 0,
        "unattached": 0,
        "pid_backed": 0,
        "fields_met": 0,
        "fields_total": 0,
    }
    if not agent_ids:
        return {"agents": [], "groups": [], "counts": empty_counts}

    root_id = _find_root_id(nodes, [n for n in graph if isinstance(n, dict)])

    # Who credits whom. An agent crediting another agent (a ScholarlyArticle's
    # author list is fine, but Person --author--> Person is not attribution)
    # is kept: the source is drawn, and a self-reference is skipped.
    rel_order = {label: i for i, (_keys, label) in enumerate(_CREDIT_RELATIONS)}
    credits: dict[str, set[tuple[str, str]]] = {}
    for nid, node in nodes.items():
        if str(nid).rsplit("/", 1)[-1] in _EXCLUDED_IDS:
            continue
        for keys, label in _CREDIT_RELATIONS:
            for ref in _refs(node, keys):
                if ref in agent_ids and ref != nid:
                    credits.setdefault(ref, set()).add((nid, label))

    # Person → organisation(s), and the inverse. EVERY affiliation is recorded,
    # not just the first: a co-affiliated researcher is ordinary, and stopping at
    # one leaves the second institution referenced by nothing — which this view
    # then reports as an unattached duplicate and advises the reader to delete.
    affiliation: dict[str, list[str]] = {}
    affiliates: dict[str, list[str]] = {}
    for nid in sorted(agent_ids):
        if kinds[nid] != "person":
            continue
        orgs = [ref for ref in _refs(nodes[nid], _AFFILIATION_KEYS) if kinds.get(ref) == "org"]
        if orgs:
            affiliation[nid] = orgs
            for ref in orgs:
                affiliates.setdefault(ref, []).append(nid)

    def _credit_source(aid: str) -> tuple[str, str] | None:
        """Best credit source: the crate root when it credits this agent (that is
        the attribution a reader is looking for), else the most canonical other."""
        found = credits.get(aid)
        if not found:
            return None
        rooted = [p for p in found if p[0] == root_id]
        return sorted(rooted or found, key=lambda p: (rel_order[p[1]], p[0]))[0]

    # The @id tiebreak is load-bearing: `agent_ids` is a SET, so two agents with
    # the same display name would otherwise be ordered by the per-process string
    # hash seed — and same-named agents are exactly the duplicate-entity case
    # this view exists to surface. The embedded artifact must be byte-stable.
    def _agent_order(aid: str) -> tuple[bool, str, str]:
        return (kinds[aid] != "person", _name(nodes[aid]).casefold(), aid)

    agents: list[dict[str, Any]] = []
    for aid in sorted(agent_ids, key=_agent_order):
        node = nodes[aid]
        kind = kinds[aid]
        assert kind is not None
        ids = _registry_identifiers(node, nodes, _AGENT_ID_SCHEMES)
        pid_scheme = next((s for s in _PID_FOR_KIND[kind] if s in ids), None)
        source = _credit_source(aid)
        if source is not None:
            state = "credited"
        elif kind == "org" and affiliates.get(aid):
            state = "affiliated"
        else:
            state = "unattached"
        fields: dict[str, bool | None] = {
            "Persistent identifier": pid_scheme is not None,
            "Name": _literal(node, _NAME_KEYS) is not None,
            # An organisation has no affiliation of its own — n/a, not a miss.
            "Affiliation": (aid in affiliation) if kind == "person" else None,
            "Linked in crate": state != "unattached",
        }
        agents.append(
            {
                **_chem_node_brief(aid, node),
                "kind": kind,
                "pid": ids.get(pid_scheme) if pid_scheme else None,
                "pid_scheme": pid_scheme,
                "identifiers": ids,
                "resolvable": _is_uri(aid),
                "affiliations": affiliation.get(aid, []),
                "fields": fields,
                "met": sum(1 for ok in fields.values() if ok),
                "total": sum(1 for ok in fields.values() if ok is not None),
                "state": state,
                "source": source[0] if source else None,
                "edge": source[1] if source else "",
            }
        )
    by_id = {a["id"]: a for a in agents}

    # Bands: one per credit source, holding the people it credits plus those
    # people's organisations. Organisations credited directly (a publisher or
    # funder) join their crediting source's band with no person in between;
    # everything nothing references falls into a trailing unattached band.
    banded: dict[tuple[str | None, str], dict[str, list[dict[str, Any]]]] = {}
    for agent in agents:
        if agent["state"] == "unattached":
            continue
        if agent["kind"] == "person":
            key = (agent["source"], agent["edge"])
            banded.setdefault(key, {"persons": [], "orgs": []})["persons"].append(agent)
        elif agent["state"] == "credited":
            key = (agent["source"], agent["edge"])
            banded.setdefault(key, {"persons": [], "orgs": []})["orgs"].append(agent)
    # Affiliation organisations ride in the band(s) of the people that name them.
    for key, band in banded.items():
        seen = {o["id"] for o in band["orgs"]}
        for person in band["persons"]:
            for org_id in person["affiliations"]:
                if org_id not in seen and org_id in by_id:
                    seen.add(org_id)
                    band["orgs"].append(by_id[org_id])

    groups = [
        {
            "source": _chem_node_brief(src, nodes[src]) if src else None,
            "edge": edge,
            "persons": band["persons"],
            "orgs": band["orgs"],
            "state": "credited",
        }
        for (src, edge), band in sorted(
            banded.items(), key=lambda kv: (kv[0][0] != root_id, kv[0][0] or "", kv[0][1])
        )
    ]
    # Anything no band picked up — the unattached, plus an organisation whose only
    # affiliate is itself unattached (counted and listed, but previously drawn
    # nowhere). The trailing band mixes states, so the diagram marks each node
    # from its OWN state rather than the band's.
    placed = {a["id"] for band in banded.values() for a in band["persons"] + band["orgs"]}
    loose = [a for a in agents if a["id"] not in placed]
    if loose:
        groups.append(
            {
                "source": None,
                "edge": "",
                "persons": [a for a in loose if a["kind"] == "person"],
                "orgs": [a for a in loose if a["kind"] == "org"],
                "state": "unattached",
            }
        )

    counts = {
        "people": sum(1 for a in agents if a["kind"] == "person"),
        "orgs": sum(1 for a in agents if a["kind"] == "org"),
        "total": len(agents),
        "credited": sum(1 for a in agents if a["state"] != "unattached"),
        "unattached": sum(1 for a in agents if a["state"] == "unattached"),
        "pid_backed": sum(1 for a in agents if a["pid_scheme"]),
        "fields_met": sum(a["met"] for a in agents),
        "fields_total": sum(a["total"] for a in agents),
    }
    return {"agents": agents, "groups": groups, "counts": counts}


_ISA_LEVELS: tuple[str, ...] = ("Investigation", "Study", "Assay")
_DESCRIPTION_KEYS: tuple[str, ...] = (
    "description",
    "http://schema.org/description",
    "https://schema.org/description",
)

ISA_COVERAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Identifier", "Identifier"),
    ("Description", "Description"),
    ("Listed by its parent", "In parent"),
    ("Contains the next level", "Contains"),
)


def _isa_level(node: dict[str, Any], is_root: bool) -> str | None:
    """``Investigation`` / ``Study`` / ``Assay`` for an ISA container, else None.

    The Root Data Entity is the Investigation whether or not it says so — a crate
    whose root omits ``additionalType`` is still an investigation-level record,
    and reporting it as "not an ISA node" would hide the whole hierarchy.
    """
    add = _additional_type(node)
    if add in _ISA_LEVELS:
        return add
    return "Investigation" if is_root and "Dataset" in _types(node) else None


def build_isa_inventory(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Model the crate's ISA backbone: Investigation → Study → Assay.

    Resolves each container's level, the parent that lists it under ``hasPart``,
    the children it lists in turn, and — for an Assay — the ``LabProcess``es it is
    ``about``. Reports per node whether it carries an ``identifier`` and a
    ``description``, whether a parent lists it, and whether it contains the level
    below (an Assay's "level below" is its processes).

    Pure and cheap: one pass over the serialized ``@graph``, no validation and no
    network. Crate-controlled text is HTML-escaped in ``label`` (#169).

    Returns:
        ``{"nodes": [...], "counts": {...}}``. Each node carries ``level``,
        ``parent``, ``children``, ``processes``, ``fields``, ``met``, ``total``
        and ``state`` — ``"linked"`` when a parent lists it (or it is the
        Investigation), ``"detached"`` when nothing does.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    nodes = _graph_nodes(metadata)
    root_id = _find_root_id(nodes, [n for n in graph if isinstance(n, dict)])
    levels = {
        nid: lvl
        for nid, n in nodes.items()
        if (lvl := _isa_level(n, nid == root_id)) is not None
    }
    if not levels:
        return {
            "nodes": [],
            "counts": {
                "total": 0,
                "investigations": 0,
                "studies": 0,
                "assays": 0,
                "processes": 0,
                "detached": 0,
                "fields_met": 0,
                "fields_total": 0,
            },
        }

    # hasPart, restricted to ISA containers — the parent/child skeleton. A file
    # part is not a structural child and must not make an Assay look populated.
    children: dict[str, list[str]] = {}
    parent: dict[str, str] = {}
    for nid in sorted(levels):
        kids = [ref for ref in _refs(nodes[nid], _HASPART_KEYS) if ref in levels]
        if kids:
            children[nid] = kids
        for kid in kids:
            parent.setdefault(kid, nid)

    processes: dict[str, list[str]] = {}
    for nid in sorted(levels):
        if levels[nid] != "Assay":
            continue
        procs = [
            ref
            for ref in _refs(nodes[nid], _ABOUT_GRAPH_KEYS)
            if ref in nodes and _is_process(nodes[ref])
        ]
        if procs:
            processes[nid] = procs

    def _order(nid: str) -> tuple[int, str, str]:
        return (_ISA_LEVELS.index(levels[nid]), _name(nodes[nid]).casefold(), nid)

    out: list[dict[str, Any]] = []
    for nid in sorted(levels, key=_order):
        node = nodes[nid]
        level = levels[nid]
        is_root = nid == root_id
        kids = children.get(nid, [])
        procs = processes.get(nid, [])
        contains = bool(procs) if level == "Assay" else bool(kids)
        fields: dict[str, bool | None] = {
            "Identifier": _literal(node, _IDENTIFIER_KEYS) is not None
            or bool(_refs(node, _IDENTIFIER_KEYS))
            or _is_uri(nid),
            "Description": _literal(node, _DESCRIPTION_KEYS) is not None,
            # The Investigation is the top of the hierarchy — it has no parent to
            # be listed by, which is not a defect.
            "Listed by its parent": None if level == "Investigation" else nid in parent,
            "Contains the next level": contains,
        }
        out.append(
            {
                **_chem_node_brief(nid, node),
                "level": level,
                "parent": parent.get(nid),
                "children": kids,
                "processes": procs,
                "fields": fields,
                "met": sum(1 for ok in fields.values() if ok),
                "total": sum(1 for ok in fields.values() if ok is not None),
                "state": "linked" if (is_root or nid in parent) else "detached",
            }
        )

    counts = {
        "total": len(out),
        "investigations": sum(1 for n in out if n["level"] == "Investigation"),
        "studies": sum(1 for n in out if n["level"] == "Study"),
        "assays": sum(1 for n in out if n["level"] == "Assay"),
        "processes": len({p for n in out for p in n["processes"]}),
        "detached": sum(1 for n in out if n["state"] == "detached"),
        "fields_met": sum(n["met"] for n in out),
        "fields_total": sum(n["total"] for n in out),
    }
    return {"nodes": out, "counts": counts}


_CITATION_KEYS_FULL: tuple[str, ...] = (
    "citation",
    "http://schema.org/citation",
    "https://schema.org/citation",
)
_ISBASEDON_KEYS: tuple[str, ...] = (
    "isBasedOn",
    "isBasedOnUrl",
    "http://schema.org/isBasedOn",
    "https://schema.org/isBasedOn",
)
_HEADLINE_KEYS: tuple[str, ...] = (
    "headline",
    "http://schema.org/headline",
    "https://schema.org/headline",
)
_DATEPUBLISHED_KEYS: tuple[str, ...] = (
    "datePublished",
    "http://schema.org/datePublished",
    "https://schema.org/datePublished",
)

# How something can point AT an article, most canonical first — the order decides
# which citer is drawn when several apply. ``citation`` is what the builder
# writes onto the Root Data Entity; ``isBasedOn`` and ``mentions``/``about`` are
# how an externally-authored crate or a Study/Assay commonly reaches one, and a
# crate that uses them should see its route rather than be told it has none.
_CITATION_LINK_RELATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_CITATION_KEYS_FULL, "citation"),
    (_ISBASEDON_KEYS, "isBasedOn"),
    (_MENTIONS_KEYS, "mentions"),
    (_ABOUT_KEYS, "about"),
    (_HASPART_KEYS, "hasPart"),
)

# A DOI: the ``10.`` prefix, the registrant code, then the opaque suffix. Matched
# rather than read off the tail of the URL the way `_registry_identifiers` reads
# an ORCID — a DOI *contains* a slash, so splitting on the last one yields
# "s-vhps22" out of "10.6019/s-vhps22" and would report a truncated identifier
# that resolves to nothing.
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+")

# Literals that carry no information but are not empty. The publication resolver
# stringifies a missing Crossref field, so a real crate ships
# ``"datePublished": "None"`` — and `_literal` cannot tell that from a date.
# Scoring it as present would paint a green Date column for an article that
# states no date at all, which is exactly the kind of false green these views
# exist to remove.
_PLACEHOLDER_LITERALS = frozenset({"", "none", "null", "nan", "n/a", "na", "unknown", "-"})

# The coverage matrix columns: (full name, column header). The work first (is the
# paper retrievable), then its credit list (does the reference reach anybody),
# then the route (does anything cite it at all).
CITATION_COVERAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Resolvable DOI", "DOI"),
    ("Title", "Title"),
    ("Publication date", "Date"),
    ("Authors listed", "Authors"),
    ("Every author resolves to an entity", "Resolve"),
    ("Cited in the crate", "Cited"),
)

def _is_article(node: dict[str, Any]) -> bool:
    """True for a publication entity.

    Asked through :func:`_entity_category` rather than by testing ``@type``
    directly, so this view draws exactly the entities the rest of the report
    colours as publications — commit ``ac3fc9b``'s one colour and one shape per
    entity type only holds if there is one definition of what a publication is.
    """
    return _entity_category(node) == "publication"


def _meaningful(value: str | None) -> str | None:
    """*value* when it says something, else ``None`` (see `_PLACEHOLDER_LITERALS`)."""
    if value is None:
        return None
    text = value.strip()
    return text if text.casefold() not in _PLACEHOLDER_LITERALS else None


def _article_doi(node: dict[str, Any], nodes: dict[str, Any]) -> str | None:
    """The article's DOI, from its ``@id``, ``url``, or an ``identifier``.

    Every carrier is tried because the crate's own writer and an externally
    authored crate disagree about which one is canonical: the builder mints the
    DOI URL as the ``@id`` *and* repeats it under ``identifier``/``url``, while a
    hand-written crate may give the bare ``10.…`` string alone.
    """
    candidates: list[str] = []
    for key in _IDENTIFIER_KEYS:
        for item in _as_list(node.get(key)):
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                pv = nodes.get(item["@id"], item) if isinstance(item.get("@id"), str) else item
                candidates.extend(
                    v for v in (_literal(pv, _VALUE_KEYS), pv.get("@id")) if isinstance(v, str)
                )
    candidates.extend(
        v for v in (_literal(node, _URL_KEYS), node.get("@id")) if isinstance(v, str)
    )
    for candidate in candidates:
        match = _DOI_RE.search(candidate)
        if match:
            return match.group(0)
    return None


def _citation_authors(
    node: dict[str, Any], nodes: dict[str, Any]
) -> list[dict[str, Any]]:
    """The article's credit list, resolved and unresolved alike.

    ``contributor`` is read alongside ``author`` and the two deduplicated: the
    builder writes the same Crossref person into both, and an article credited
    only through ``contributor`` still has a credit list — reporting it as having
    none would be a defect this view invented.

    An unresolved reference is KEPT, and that is the point. Dropping it would
    leave an article whose authors all resolve to nothing looking author-less, or
    — worse — looking fine because the surviving references happened to resolve.
    The brief carries the raw ``@id`` as its name, because that string is what a
    reader has to go and fix.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for keys, label in ((_AUTHOR_KEYS, "author"), (_CONTRIBUTOR_KEYS, "contributor")):
        for ref in _refs(node, keys):
            if ref in seen:
                continue
            seen.add(ref)
            target = nodes.get(ref)
            if target is None:
                out.append(
                    {
                        "id": ref,
                        "name": ref,
                        "label": _escape(ref),
                        # Named as the failure, not as a Person: the crate has no
                        # entity here, and tagging the box "Person" would assert
                        # the very thing that is missing.
                        "tag": "Unresolved @id",
                        "resolved": False,
                        "pid": None,
                        "edge": label,
                    }
                )
                continue
            ids = _registry_identifiers(target, nodes, _AGENT_ID_SCHEMES)
            out.append(
                {
                    **_chem_node_brief(ref, target),
                    "resolved": True,
                    "pid": next((ids[s] for s in _PID_FOR_KIND["person"] if s in ids), None),
                    "edge": label,
                }
            )
    return out


def build_citation_inventory(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Model the crate's citations: their route into the record + their identity.

    Resolves, for every ``ScholarlyArticle`` the crate declares, which entity
    cites it (the Root Data Entity's ``citation`` is preferred when several do,
    because that is the attribution a reader is looking for), whether it carries
    a resolvable DOI, a title and a publication date, and whether every ``@id``
    in its credit list points at an entity the crate actually contains.

    Pure and cheap: one pass over the serialized ``@graph``, no validation and no
    network. Crate-controlled text is HTML-escaped in ``label`` (#169).

    Args:
        metadata: Parsed ``ro-crate-metadata.json`` dict, the ``@graph`` list, or
            the ``crate.metadata.generate()`` document.

    Returns:
        ``{"articles": [...], "groups": [...], "counts": {...}}``.

        Each article: ``{id, name, label, tag, doi, resolvable, authors, fields,
        met, total, state, source, edge}`` where ``state`` is ``"cited"``
        (something in the crate points at it) or ``"uncited"`` (nothing does).
        Each entry of ``authors`` carries ``resolved`` — ``False`` for an ``@id``
        no node in the graph answers to (#532).

        ``groups`` are the diagram's bands: a citing entity, the articles it
        cites, and those articles' authors. Anything nothing cites falls into a
        trailing band with no source.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    nodes = _graph_nodes(metadata)
    article_ids = {nid for nid, n in nodes.items() if _is_article(n)}
    empty_counts = {
        "total": 0,
        "cited": 0,
        "uncited": 0,
        "doi_backed": 0,
        "authors": 0,
        "unresolved_authors": 0,
        "fields_met": 0,
        "fields_total": 0,
    }
    if not article_ids:
        return {"articles": [], "groups": [], "counts": dict(empty_counts)}

    root_id = _find_root_id(nodes, [n for n in graph if isinstance(n, dict)])
    citers = _referrers_to(nodes, article_ids, _CITATION_LINK_RELATIONS)

    def _citer(aid: str) -> tuple[str, str] | None:
        """Best citing entity: the crate root when it cites this article (that is
        the citation a reader is looking for), else the most canonical other."""
        found = citers.get(aid)
        if not found:
            return None
        rooted = [p for p in found if p[0] == root_id]
        return (rooted or found)[0]

    articles: list[dict[str, Any]] = []
    # The @id tiebreak is load-bearing: `article_ids` is a SET, so two articles
    # sharing a title would otherwise be ordered by the per-process string hash
    # seed, and the embedded artifact must be byte-stable across runs.
    for aid in sorted(article_ids, key=lambda a: (_name(nodes[a]).casefold(), a)):
        node = nodes[aid]
        doi = _article_doi(node, nodes)
        authors = _citation_authors(node, nodes)
        source = _citer(aid)
        fields: dict[str, bool | None] = {
            "Resolvable DOI": doi is not None,
            "Title": _meaningful(_literal(node, _NAME_KEYS + _HEADLINE_KEYS)) is not None,
            "Publication date": _meaningful(_literal(node, _DATEPUBLISHED_KEYS)) is not None,
            "Authors listed": bool(authors),
            # An article with no credit list has nothing to resolve — n/a, not a
            # miss, or the same absence would be counted against it twice.
            "Every author resolves to an entity": (
                all(a["resolved"] for a in authors) if authors else None
            ),
            "Cited in the crate": source is not None,
        }
        articles.append(
            {
                **_chem_node_brief(aid, node),
                "doi": doi,
                "resolvable": _is_uri(aid),
                "authors": authors,
                "fields": fields,
                "met": sum(1 for ok in fields.values() if ok),
                "total": sum(1 for ok in fields.values() if ok is not None),
                "state": "cited" if source else "uncited",
                "source": source[0] if source else None,
                "edge": source[1] if source else "",
            }
        )

    # Bands: one per citing entity, holding the articles it cites. Uncited
    # articles fall into a trailing band drawn with the ✗ stub on their left,
    # exactly as an unattached agent does in the people view.
    banded: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for article in articles:
        if article["source"] is not None:
            banded.setdefault((article["source"], article["edge"]), []).append(article)
    groups: list[dict[str, Any]] = []
    for (src, edge), band in sorted(
        banded.items(), key=lambda kv: (kv[0][0] != root_id, kv[0][0], kv[0][1])
    ):
        brief = _chem_node_brief(src, nodes[src])
        groups.append(
            {
                "source": brief,
                # The citing entity's own category, carried out of the builder so
                # the panel's legend can name the shape that was actually drawn
                # rather than assume the root Dataset every crate does not have.
                "source_cls": _node_class_for_brief(brief),
                "edge": edge,
                "articles": band,
                "state": "cited",
            }
        )
    loose = [a for a in articles if a["state"] == "uncited"]
    if loose:
        groups.append(
            {"source": None, "source_cls": "", "edge": "", "articles": loose, "state": "uncited"}
        )

    # Author references are counted DISTINCTLY across the crate: two papers by the
    # same unresolvable stub are one broken @id to fix, and reporting it twice
    # would overstate the work.
    author_ids = {a["id"] for art in articles for a in art["authors"]}
    unresolved = {a["id"] for art in articles for a in art["authors"] if not a["resolved"]}
    counts = {
        "total": len(articles),
        "cited": sum(1 for a in articles if a["state"] == "cited"),
        "uncited": sum(1 for a in articles if a["state"] == "uncited"),
        "doi_backed": sum(1 for a in articles if a["doi"]),
        "authors": len(author_ids),
        "unresolved_authors": len(unresolved),
        "fields_met": sum(a["met"] for a in articles),
        "fields_total": sum(a["total"] for a in articles),
    }
    return {"articles": articles, "groups": groups, "counts": counts}


def _node_class_for_brief(brief: dict[str, str]) -> str:
    """Style bucket for a drawn route hop, from the type tag already computed.

    The tag is the only thing the band carries (the node itself is not re-read),
    and it is enough: a condition table tags as ``Table``/``File``, an
    Investigation/Study/Assay as ``Dataset``.

    The tag is turned back into a stub node and run through the one classifier
    every other view uses, rather than re-deciding here what a type means. The
    hand-written ladder this replaced was missing ``Dataset``, so an
    Investigation drew as a barred indigo block in the ISA tab and an anonymous
    grey box in the cell-line and people tabs.

    ``additionalType`` is filled in as well as ``@type`` because the process
    kinds (``CellCulture``, ``Exposure``, …) reach the tag from there.
    """
    head = brief.get("tag", "").split(" · ", 1)[0]
    return _node_class({"@type": head, "additionalType": head})


