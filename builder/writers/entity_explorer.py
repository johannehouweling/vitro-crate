"""Interactive entity explorer for the maturity report (#615).

The report's static views each answer one question about the crate, and the
all-entities view answers "what is in here" without drawing a single edge —
a node-link picture of a whole crate is a hairball on paper. Given a canvas the
reader can pan, filter and interrogate, it stops being one: this module ships
the crate's own entity graph to the browser and lets the reader combine the
views instead of choosing between them.

Two halves:

- :func:`build_explorer_payload` — a pure, deterministic JSON model of the
  crate: the nodes and edges :func:`~builder.writers.provenance_dag.build_crate_graph`
  already computes, the raw document behind them, the category registry, and one
  member list per view.
- :func:`render_explorer_section` — the report section: markup, the payload as a
  ``<script type="application/json">`` data island, and the vendored bundles
  inlined from :mod:`builder.writers.vendor`.

**Self-contained, still.** The report is embedded in the crate and read
offline, so nothing here may reach the network: React, React Flow, dagre and htm
are vendored UMD builds inlined into the page, pinned by ``vendor/manifest.json``
and checked against it at render time. What changed with this section is only
the *means* — the page now carries script, and carries no reference to anything
it does not also carry.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, NamedTuple

from builder.writers.provenance_dag import (
    _CTX_CATEGORY,
    _CTX_COLOUR,
    _CTX_GLYPH,
    _LAYER_NAMES,
    _PROCESS_DISCRIMINATORS,
    CATEGORY_STYLES,
    PATHWAY_TYPES,
    _derivation_edges,
    _graph_nodes,
    _route_hop_ids,
    _types,
    build_cellline_inventory,
    build_chemical_inventory,
    build_citation_inventory,
    build_crate_graph,
    build_isa_inventory,
    build_people_inventory,
)

PAYLOAD_VERSION = 2
"""Bumped when the payload's shape changes, so a stale cached script is loud."""

_CTX_LABEL = "Referenced outside the crate"


class _Crate:
    """One crate, read once, in every form the selectors need."""

    def __init__(self, metadata: dict[str, Any] | list[dict[str, Any]]) -> None:
        self.document: dict[str, Any] = (
            {"@graph": metadata} if isinstance(metadata, list) else metadata
        )
        self.model = build_crate_graph(metadata, layer="all", all_edges=True)
        self.nodes = _graph_nodes(metadata)
        self.root: str | None = self.model["root"]
        self.known = {n["id"] for n in self.model["nodes"]}
        self._inventories: dict[str, Any] = {}

    def inventory(self, name: str) -> dict[str, Any]:
        """The named inventory, built once — four views share three of them."""
        if name not in self._inventories:
            build = {
                "isa": build_isa_inventory,
                "chemical": build_chemical_inventory,
                "cellline": build_cellline_inventory,
                "people": build_people_inventory,
                "citation": build_citation_inventory,
            }[name]
            self._inventories[name] = build(self.document)
        return self._inventories[name]


def _select_all(crate: _Crate) -> set[str]:
    """Everything the crate describes — the overview tile map's own selection.

    A node with no layer is a bare reference: an ``@id`` the crate points at and
    says nothing about. It is kept in the model (the side panel labels it) but it
    belongs to no view of its own.
    """
    return {n["id"] for n in crate.model["nodes"] if n["layer"] is not None}


def _select_researcher(crate: _Crate) -> set[str]:
    """The experiment as a scientist reads it.

    Everything the crate describes except the machinery that describes it:
    measured parameters, column definitions, ontology terms, the licence, the
    profiles it conforms to, the build's own action and software. Those all land
    in the ``annotation`` bucket — the category for an entity that qualifies
    another rather than taking part in the work.

    The rule is by **category, never by layer**: Persons, Organisations and
    articles sit in the base packaging layer along with the plumbing, so a
    layer-based rule would drop exactly the credit a reader looks for. The root
    is kept whatever its category, since a crate whose root is untyped would
    otherwise lose the entity everything else hangs from.
    """
    return {
        n["id"]
        for n in crate.model["nodes"]
        if n["status"] == "in_crate" and (n["category"] != "annotation" or n["id"] == crate.root)
    }


def _select_files(crate: _Crate) -> set[str]:
    """Data files and the datasets holding them — the Files panel's selection."""
    return {n["id"] for n in crate.model["nodes"] if n["category"] in ("data", "container")}


def _select_aop(crate: _Crate) -> set[str]:
    """The adverse outcome pathway itself, and who measures it.

    The chain entire — the pathway, its key events and the relationships that
    order them — plus the ISA entity that points into it, because *which assay
    measures which event* is the question the view exists to answer and a bag of
    events answers nothing.

    Assays draws the same chain from the other end: it starts at the backbone and
    follows `mentions` outward, so it shows only what an assay or study points
    at — five of thirty-six on a real deposit. This view starts at the chain, so
    a relationship (which nothing mentions) and a key event no assay measures
    directly are drawn too, and the ISA entities arrive as context rather than as
    the subject (#652).

    Context, not everything that points here: a `CreativeWork` note mentioning a
    key event is not part of the science, so the source is filtered to the
    backbone exactly as `_select_assays` filters its destination by type.

    No empty-crate guard: a crate with no pathway selects nothing here, and
    `build_explorer_payload` already omits a view no entity satisfies rather than
    offering a chip that draws an empty canvas.
    """
    chain = {n["id"] for n in crate.model["nodes"] if n["category"] == "pathway"}
    backbone = {n["id"] for n in crate.inventory("isa")["nodes"]}
    measured_by = {
        edge["src"]
        for edge in crate.model["edges"]
        if edge["label"] == "mentions" and edge["src"] in backbone and edge["dst"] in chain
    }
    return chain | measured_by


def _select_assays(crate: _Crate) -> set[str]:
    """The ISA backbone, and what its assays are for.

    Investigation → Study → Assay, plus the adverse outcome pathway a study
    serves and the key events an assay measures. Those are the one thing in the
    crate that says what an assay is *for*, and the view named after assays used
    to select the backbone alone (#627).

    Followed, never collected: a pathway reaches the canvas only through an ISA
    entity that mentions it, so an ontology term nothing points at stays out.
    ``mentions`` is a general relation, so what is mentioned is filtered by
    type — the science joins the view, the build's own action does not.
    """
    backbone = {n["id"] for n in crate.inventory("isa")["nodes"]}
    described = {
        str(entity["@id"]): entity
        for entity in crate.document.get("@graph", [])
        if isinstance(entity, dict) and entity.get("@id")
    }
    pathway = {
        edge["dst"]
        for edge in crate.model["edges"]
        if edge["label"] == "mentions"
        and edge["src"] in backbone
        and _types(described.get(edge["dst"], {})) & PATHWAY_TYPES
    }
    return backbone | pathway


# What a step *is*, as opposed to what it handled. Each is keyed by the edge the
# crate draws and the direction it points, because the two disagree: a process
# reaches its protocol, and an assay reaches its process (#626).
_PROCESS_CONTEXT: tuple[tuple[str, bool], ...] = (
    ("executes", True),  # process → the protocol it runs
    ("about", False),  # assay → the process it is about
)


def _process_flavour(node: dict[str, Any]) -> str | None:
    """The ISA-Tox discriminator a process node carries, if it carries one.

    A process node's type tag *is* its discriminator, so the flavours need no
    classification of their own (#624). A node captioned with more than one tag
    (``A \u00b7 B``) is matched on any of them.
    """
    for tag in str(node.get("type") or "").split("\u00b7"):
        if tag.strip() in _PROCESS_DISCRIMINATORS:
            return tag.strip()
    return None


def _select_processes(crate: _Crate, flavour: str | None = None) -> set[str]:
    """The derivation chain, and what each step is and belongs to.

    :func:`_derivation_edges` walks the **material** chain — what a process
    consumed and produced. Neither edge that says what a step *is* travels that
    way, so the view used to show every step and every file it touched while
    never saying how a step was done or which assay it served (#626).

    Both are followed here, and followed rather than collected: only the
    protocol a visible process executes and the assay whose ``about`` points at
    a visible process join the selection, so a protocol belonging to no drawn
    step stays out. The two point in opposite directions, which is why this
    reads the model's edges instead of extending the derivation walk.
    """
    edges = _derivation_edges(crate.nodes)
    chain = {e[0] for e in edges} | {e[1] for e in edges}
    processes = {
        n["id"] for n in crate.model["nodes"] if n["category"] == "process" and n["id"] in chain
    }
    if flavour is not None:
        # A flavour is this rule restricted to one discriminator — never a fresh
        # sweep of the crate by type, or it would draw steps the parent leaves
        # out. The chain narrows with it, to what those steps touched.
        processes = {
            n["id"]
            for n in crate.model["nodes"]
            if n["id"] in processes and _process_flavour(n) == flavour
        }
        chain = set(processes)
        for edge in edges:
            src, dst = edge[0], edge[1]
            if src in processes:
                chain.add(dst)
            if dst in processes:
                chain.add(src)
    context = {
        edge["dst"] if outgoing else edge["src"]
        for edge in crate.model["edges"]
        for label, outgoing in _PROCESS_CONTEXT
        if edge["label"] == label and (edge["src"] if outgoing else edge["dst"]) in processes
    }
    return chain | context | _reagents_of(crate, processes)


def _reagents_of(crate: _Crate, processes: set[str]) -> set[str]:
    """The substances the drawn steps used, one hop past their protocols (#686).

    An exposure's compounds are the substances under test, and the view about
    what the steps did drew none of them. They were missing by a hop, not by a
    modelling gap: ISA restricts ``schema:object`` to File/Sample/BioSample at
    Violation severity, so a ``MolecularEntity`` can never be a process input
    directly, and ``reagent`` is a LabProtocol property ranging over it. The
    crate's route — exposure executes a condition table, the table lists its
    reagents — is the correct representation, so the selection follows it (#650).

    Anchored on the protocols *these* steps execute, never on every ``reagent``
    edge in the crate: a compound with no edge to any drawn work is the opposite
    of what the view is for.

    Note the model draws ``reagent`` REVERSED (``src`` is the compound, ``dst``
    the protocol) so the arrow points at the step that consumes the material.
    """
    protocols = {
        edge["dst"]
        for edge in crate.model["edges"]
        if edge["label"] == "executes" and edge["src"] in processes
    }
    return {
        edge["src"]
        for edge in crate.model["edges"]
        if edge["label"] == "reagent" and edge["dst"] in protocols
    }


def _assay_processes(crate: _Crate, assay_id: str) -> list[str]:
    """The steps an assay is ``about``, or nothing if *assay_id* names no assay."""
    for node in crate.inventory("isa")["nodes"]:
        if node["level"] == "Assay" and node["id"] == assay_id:
            return list(node.get("processes") or [])
    return []


def _select_assay_lane(crate: _Crate, assay_id: str) -> set[str]:
    """One assay's chain, and nothing that belongs to another (#686).

    An assay is what produces a research object, so it is what the lane draws.
    Scoped this way the closure is small, and since #678 gave each assay its own
    culture, no node on the lane belongs to a neighbour.

    What it draws: the assay's steps, the materials those steps consumed and
    produced, the protocol under each step, and the compounds one hop past the
    protocols (:func:`_reagents_of`).

    What it does not: the Study, the Investigation, and **the assay itself**.
    Drawn, the assay would connect to every step and reproduce the star this
    change exists to remove — so it frames the view rather than appearing in it.
    That falls out of walking only material and protocol edges, and is pinned by
    a test so a later switch to :data:`_PROCESS_CONTEXT` cannot quietly undo it.

    The material walk is **one hop** from the steps, not a transitive closure:
    every material on a spine is adjacent to the step that handled it, so a hop
    reaches all of them, while a closure would follow a shared file out of the
    assay and undo the scoping.
    """
    processes = set(_assay_processes(crate, assay_id))
    if not processes:
        return set()
    lane = set(processes)
    for src, dst, _kind in _derivation_edges(crate.nodes):
        if src in processes:
            lane.add(dst)
        if dst in processes:
            lane.add(src)
    protocols = {
        edge["dst"]
        for edge in crate.model["edges"]
        if edge["label"] == "executes" and edge["src"] in processes
    }
    return lane | protocols | _reagents_of(crate, processes)


def _routed(crate: _Crate, inventory: str, members: str) -> set[str]:
    """Members of a routed inventory, plus the hops that link them.

    The compound and cell-line panels draw a member beside the process that used
    it, reached through a table where the crate links it indirectly. Without the
    hops the toggle would show compounds with no edge to any work, which is the
    opposite of what the view is for.
    """
    inv = crate.inventory(inventory)
    ids: set[str] = set()
    for member in inv[members]:
        ids.add(member["id"])
        route = member.get("route") or {}
        ids.update(_route_hop_ids(route.get("process"), route.get("via")))
    return ids


def _select_people(crate: _Crate) -> set[str]:
    """Everyone credited, and the entity that credits them."""
    inv = crate.inventory("people")
    ids: set[str] = set()
    for agent in inv["agents"]:
        ids.add(agent["id"])
        ids.update(agent.get("affiliations") or [])
        if agent.get("source"):
            ids.add(agent["source"])
    return ids


def _select_citations(crate: _Crate) -> set[str]:
    """Cited articles, their authors, and what cites them.

    An author the crate names but never describes is a dangling stub, and it is
    deliberately included: "this paper's authors are not entities in the crate"
    is the finding the view exists to show.
    """
    inv = crate.inventory("citation")
    ids: set[str] = set()
    for article in inv["articles"]:
        ids.add(article["id"])
        ids.update(a["id"] for a in article.get("authors") or [])
    for group in inv["groups"]:
        source = group.get("source")
        if isinstance(source, dict) and source.get("id"):
            ids.add(source["id"])
    return ids


def _of_category(*categories: str) -> Callable[[_Crate], set[str]]:
    """Every drawn entity in one of *categories* — the subject of a view named
    after a kind of entity rather than after an inventory."""

    def subject(crate: _Crate) -> set[str]:
        return {n["id"] for n in crate.model["nodes"] if n["category"] in categories}

    return subject


def _of_inventory(
    name: str, members: str, level: str | None = None
) -> Callable[[_Crate], set[str]]:
    """The members of an inventory — the same source the matching coverage block
    counts, so the chip and the block cannot drift apart (#625)."""

    def subject(crate: _Crate) -> set[str]:
        inv = crate.inventory(name)
        return {
            member["id"] for member in inv[members] if level is None or member.get("level") == level
        }

    return subject


def _of_process_flavour(flavour: str) -> Callable[[_Crate], set[str]]:
    """The steps of one flavour — what its chip is named for, so the sub-row
    counts the way every other chip does (#625)."""

    def subject(crate: _Crate) -> set[str]:
        return {
            n["id"]
            for n in crate.model["nodes"]
            if n["category"] == "process" and _process_flavour(n) == flavour
        }

    return subject


PROCESS_FLAVOURS: dict[str, str] = {
    "cellculture": "CellCulture",
    "exposure": "Exposure",
    "endpointreadout": "EndpointReadout",
    "dataanalysis": "DataAnalysis",
}
"""The LabProcesses sub-row: chip key -> the ISA-Tox discriminator it draws.

The four are the profile's own LabProcess kinds (``_PROCESS_DISCRIMINATORS``),
each with a shape file of its own; a fifth invented here would be a category no
crate can carry.
"""


class ExplorerView(NamedTuple):
    """One toggle: a named selection over the crate's entities.

    ``select`` is what the toggle *draws*; ``subject`` is what it is *named
    for*, and the chip counts that. The two differ because a selection carries
    the context that makes it readable — the files a step touched, the process
    and table that link a compound to the work — and counting those made every
    chip overstate its own label, LabProcesses by threefold (#625). A view whose
    name covers everything it draws leaves ``subject`` unset.

    ``parent`` makes the view a refinement of another; see the field.
    """

    key: str
    label: str
    hint: str
    default: bool
    select: Callable[[_Crate], set[str]]
    subject: Callable[[_Crate], set[str]] | None = None
    lane: bool = False
    """Whether this view draws one assay's chain, and so wants the lane layout.

    The app reads this to pick a layout rather than matching on a key or on a
    parent: which views are lanes is a fact about the selection, and the browser
    should not have to re-derive it from a naming convention.
    """

    parent: str | None = None
    """The view this one refines, if any.

    A child is drawn as a sub-row under its parent's chip and appears only
    while the parent is on. Children **narrow**: a parent with active children
    contributes the union of those children instead of its own members, so the
    explorer keeps one interaction model — views combine — instead of gaining a
    second one for the sub-row (#624).
    """


ASSAY_LANE_PREFIX = "assay-"
"""Namespace for the per-assay sub-row keys, which are minted per crate.

Every other view key is a constant in :data:`EXPLORER_VIEWS`, because every
other view is a question about the crate rather than about one of its entities.
An assay lane is named for an assay that only this crate has, so its key is
derived from that assay's ``@id`` — see :func:`_lane_key`.
"""


def _lane_slug(name: str, assay_id: str) -> str:
    """The readable half of a lane key: the assay's name, hyphenated.

    Falls back to the ``@id`` only when an assay has no name, which the ISA
    shapes already treat as a violation.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if slug:
        return slug
    return re.sub(r"[^a-z0-9]+", "-", assay_id.lower()).strip("-") or "lane"


def _lane_key(slug: str, assay_id: str, ambiguous: bool) -> str:
    """A hash-safe view key for one assay's lane.

    View keys are joined with commas into the location hash, so a key carrying
    one would split into two views that answer to nothing; the slug is
    ``[a-z0-9-]`` by construction.

    Built from the **name** because the key is what a shared link carries, and
    real assay ids repeat their own kind — ``#Assay_assay_deiodinase_assay``
    slugs to ``assay-assay-assay-deiodinase-assay``, which is unique and
    unreadable. Names are not guaranteed unique, so when two assays share one,
    the ``@id`` settles it.

    *ambiguous* is decided across all the crate's assays before any key is
    minted, so **every** lane sharing a slug is suffixed rather than the first
    one winning the bare key. Otherwise the keys would depend on which assay the
    graph happened to list first, and two builds of one deposit must produce
    reports that diff to nothing.
    """
    key = ASSAY_LANE_PREFIX + slug
    if ambiguous:
        key += "-" + hashlib.sha1(assay_id.encode("utf-8")).hexdigest()[:6]
    return key


def _assay_lane_views(crate: _Crate) -> list[ExplorerView]:
    """One sub-row per assay, in the crate's own order.

    A child view narrows its parent (#624), so choosing one assay replaces the
    Assays selection and the containers drop out with it — which is what makes
    this a lane rather than another top-level chip.

    ``subject`` is left unset: the view is named for an assay and everything it
    draws belongs to that assay, so the count is the membership (#625).
    """
    assays = [n for n in crate.inventory("isa")["nodes"] if n["level"] == "Assay"]
    named = []
    for node in assays:
        assay_id = str(node["id"])
        raw_name = str(node.get("name") or "")
        named.append(
            (
                assay_id,
                html.unescape(raw_name or assay_id),
                _lane_slug(raw_name, assay_id),
            )
        )
    shared = {slug for slug, count in Counter(s for _i, _n, s in named).items() if count > 1}
    return [
        ExplorerView(
            _lane_key(slug, assay_id, slug in shared),
            name,
            "This assay end to end — its materials, steps, protocols and compounds",
            False,
            (lambda i: lambda c: _select_assay_lane(c, i))(assay_id),
            lane=True,
            parent="assays",
        )
        for assay_id, name, slug in named
    ]


# Order matters: "Researcher" opens the section, and the rest follow the tabbed
# section's reviewed order so a reader who learned it there is not retrained.
EXPLORER_VIEWS: tuple[ExplorerView, ...] = (
    ExplorerView(
        "researcher",
        "Researcher",
        "The experiment as a scientist reads it — no packaging or parameters",
        True,
        _select_researcher,
    ),
    ExplorerView("all", "All entities", "Everything the crate describes", False, _select_all),
    ExplorerView(
        "files",
        "Files",
        "Data files and the datasets that hold them",
        False,
        _select_files,
        _of_category("data"),
    ),
    ExplorerView(
        "assays",
        "Assays",
        "Investigation, studies and assays",
        False,
        _select_assays,
        _of_inventory("isa", "nodes", level="Assay"),
    ),
    ExplorerView(
        "processes",
        "LabProcesses",
        "Processes with what they consumed and produced",
        False,
        _select_processes,
        _of_category("process"),
    ),
    *(
        ExplorerView(
            key,
            label,
            hint,
            False,
            (lambda kind: lambda crate: _select_processes(crate, kind))(kind),
            _of_process_flavour(kind),
            parent="processes",
        )
        for key, kind, label, hint in (
            (
                "cellculture",
                "CellCulture",
                "Cell culture",
                "Only the culture steps, with what they used and produced",
            ),
            (
                "exposure",
                "Exposure",
                "Exposure",
                "Only the exposure steps, with what they used and produced",
            ),
            (
                "endpointreadout",
                "EndpointReadout",
                "Endpoint readout",
                "Only the readout steps, with what they measured and produced",
            ),
            (
                "dataanalysis",
                "DataAnalysis",
                "Data analysis",
                "Only the analysis steps, with what they read and produced",
            ),
        )
    ),
    ExplorerView(
        "chemicals",
        "Chemicals",
        "Compounds and the work that used them",
        False,
        lambda crate: _routed(crate, "chemical", "chemicals"),
        _of_inventory("chemical", "chemicals"),
    ),
    ExplorerView(
        "samples",
        "Biological models",
        "Cell lines and samples, and the work that used them",
        False,
        lambda crate: _routed(crate, "cellline", "celllines"),
        _of_inventory("cellline", "celllines"),
    ),
    # Named for the framework, not for "pathways": in the toxicology community
    # this crate is written for, a pathway is a WikiPathways molecular pathway —
    # a different resource entirely — so the short label would promise genes and
    # deliver key events. The full term is not jargon to the reader who needs
    # this view; it is the framework they already work in.
    ExplorerView(
        "aop",
        "Adverse outcome pathway",
        "The pathway your assays measure — the events, how they follow one "
        "another, and which assay measures which",
        False,
        _select_aop,
        _of_category("pathway"),
    ),
    ExplorerView(
        "people",
        "Persons & Organisations",
        "Who the crate credits",
        False,
        _select_people,
        _of_inventory("people", "agents"),
    ),
    ExplorerView(
        "citations",
        "Citations",
        "What the crate cites, and who wrote it",
        False,
        _select_citations,
        _of_inventory("citation", "articles"),
    ),
)


def _categories(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The drawing registry, as the browser needs it.

    Colours and glyphs are generated from :data:`CATEGORY_STYLES` for the same
    reason the stylesheet is (`category_css`): a second hand-written palette is
    how the report came to disagree with its own diagrams about what colour a
    file is.

    ``types`` is the census the legend labels itself from — the distinct type
    tags this crate's nodes carry in that category, most common first (#623).
    Every node on the canvas is captioned with its type, so a legend written in
    category prose explains the colours in a vocabulary the reader can see
    nowhere else; naming the tags makes the legend and the nodes say the same
    words. Derived from the crate rather than kept by hand, so a category that
    gains a type is labelled with it the day it does — and so the fallback
    bucket, which has no single type to name, is labelled as honestly as the
    rest.

    A refinement folds into its base (``Dataset · Assay`` counts as
    ``Dataset``): the colour is the base type's, and the refinements are what
    the nodes themselves spell out.
    """
    census: dict[str, Counter] = {}
    for node in nodes:
        tag = str(node.get("type") or "").split(" · ")[0].strip()
        if not tag:
            continue
        census.setdefault(str(node.get("category") or ""), Counter())[tag] += 1

    def types_for(key: str) -> list[str]:
        # Commonest first, ties by name: the legend spells out only the first
        # few, and the payload is pinned byte-for-byte, so an unstable tie would
        # break the crate's reproducibility and not merely the wording.
        counted = census.get(key) or Counter()
        return [tag for tag, _ in sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))]

    out: dict[str, dict[str, Any]] = {
        key: {
            "colour": style.colour,
            "label": style.label,
            "glyph": style.glyph,
            "types": types_for(key),
        }
        for key, style in CATEGORY_STYLES.items()
    }
    # An off-crate reference has no type because the crate never describes it,
    # so this key keeps its wording: it names a provenance status, not a
    # category, and is the one label the census cannot supply.
    out[_CTX_CATEGORY] = {
        "colour": _CTX_COLOUR,
        "label": _CTX_LABEL,
        "glyph": _CTX_GLYPH,
        "types": [],
    }
    return out


def build_explorer_payload(
    metadata: dict[str, Any] | list[dict[str, Any]],
    *,
    default_views: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """The explorer's data island: one crate, in the shape the browser draws.

    Pure and deterministic — the same crate yields the same bytes, so two builds
    of one deposit produce reports that diff to nothing. That needs saying
    because :func:`~builder.writers.provenance_dag.build_crate_graph` yields its
    off-crate stubs out of a set; node order is imposed here.

    Args:
        metadata: A parsed ``ro-crate-metadata.json``, the ``@graph`` list, or a
            ``crate.metadata.generate()`` document.
        default_views: Which view keys open, overriding
            :data:`EXPLORER_VIEWS`' own defaults — how ``--graph --view`` picks
            what the page opens on. A key no view answers to is ignored, and an
            override that selects nothing leaves the registry's default alone:
            a page that opens on an empty canvas would look broken.

    Returns:
        A JSON-safe dict: ``nodes``/``edges`` (the graph model, labels
        unescaped), ``document`` (the crate verbatim, read by the JSON panel),
        ``categories``, ``views`` (member ids per toggle), ``root`` and
        ``counts``. A view no entity satisfies is omitted rather than offered
        empty.
    """
    crate = _Crate(metadata)
    model = crate.model

    # In-crate nodes keep the model's order (the crate's own); stubs are sorted,
    # because the model yields them from a set and a report must not depend on
    # which way that fell today.
    described = [n for n in model["nodes"] if n["layer"] is not None]
    stubs = sorted((n for n in model["nodes"] if n["layer"] is None), key=lambda n: str(n["id"]))
    nodes = [
        {
            "id": n["id"],
            # The model escapes for its SVG; the DOM escapes again downstream, so
            # what travels is the crate's own text.
            "label": html.unescape(n["label"]),
            # The crate's own wording, where the label carries a badge instead.
            # The badge abbreviates the label, never the fact: a tooltip has no
            # width limit, so it says what the crate says.
            "name": html.unescape(n.get("name") or n["label"]),
            "type": html.unescape(n["type"]),
            "category": n["category"] or _CTX_CATEGORY,
            "layer": n["layer"],
            "status": n["status"],
            "orphan": n["orphan"],
            "reach": n["reach"],
            "identifier_backed": n["identifier_backed"],
        }
        for n in [*described, *stubs]
    ]

    views = []
    # The lanes are minted from this crate's assays, so they are appended
    # rather than declared. They carry `parent`, and the app draws children
    # from that alone, so position among them is the crate's order.
    for view in (*EXPLORER_VIEWS, *_assay_lane_views(crate)):
        members = view.select(crate) & crate.known
        if not members:
            continue
        # The count is the subject *as drawn*: a subject the view cannot show
        # would be a number the reader has no way to go and look at.
        counted = members if view.subject is None else (view.subject(crate) & members)
        views.append(
            {
                "key": view.key,
                "label": view.label,
                "hint": view.hint,
                "default": view.default,
                "parent": view.parent,
                "lane": view.lane,
                "count": len(counted),
                "members": sorted(members),
            }
        )
    wanted = {key for key in (default_views or ()) if any(v["key"] == key for v in views)}
    if wanted:
        for view in views:
            view["default"] = view["key"] in wanted

    return {
        "version": PAYLOAD_VERSION,
        "root": crate.root,
        "layers": {str(level): name for level, name in _LAYER_NAMES.items()},
        "categories": _categories(nodes),
        "nodes": nodes,
        "edges": [{"src": e["src"], "dst": e["dst"], "label": e["label"]} for e in model["edges"]],
        "views": views,
        "counts": {**model["counts"], "nodes": len(nodes), "edges": len(model["edges"])},
        "document": crate.document,
    }


# --- the report section -------------------------------------------------------
#
# Assets live beside this module and are read at render time, the way the report
# reads its stylesheet: the page is assembled from files, not from string
# literals buried in Python, so the CSS is CSS and the JavaScript is JavaScript.

_ASSET_DIR = Path(__file__).resolve().parent
_VENDOR_DIR = _ASSET_DIR / "vendor"
_APP_PATH = _ASSET_DIR / "entity_explorer.js"
_LAYOUT_PATH = _ASSET_DIR / "entity_explorer_layout.js"
_LANE_PATH = _ASSET_DIR / "assay_lane_layout.js"
_MANIFEST_PATH = _VENDOR_DIR / "manifest.json"

_APP_ID = "ex-app"
_DATA_ID = "ex-data"

VENDOR_MANIFEST: tuple[dict[str, Any], ...] = tuple(
    json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
)
"""Every third-party file the page inlines: name, version, licence, origin, digest."""

# react, react-dom, the jsx-runtime shim, React Flow, dagre, htm, the layout
# module, the assay lane's layout, the data island, the app. Named so a test can
# state the count without recounting the implementation, and so an accidental
# extra <script> is a failure, not a habit.
EXPLORER_SCRIPT_COUNT = 10

_JS_BUNDLES = (
    "react.production.min.js",
    "react-dom.production.min.js",
    # React Flow's UMD build takes React's automatic-runtime helpers as a global
    # (`jsxRuntime`), which React's own UMD build does not expose. Three lines
    # over `createElement` is the whole adapter; a bundler would be a build step.
    None,
    "xyflow-react.umd.js",
    "dagre.min.js",
    "htm.umd.js",
)

_JSX_SHIM = """window.jsxRuntime={Fragment:React.Fragment,
jsx:(t,p,k)=>React.createElement(t,k===undefined?p:Object.assign({},p,{key:k})),
jsxs:(t,p,k)=>React.createElement(t,k===undefined?p:Object.assign({},p,{key:k}))};"""


def _entry(filename: str) -> dict[str, Any]:
    for entry in VENDOR_MANIFEST:
        if entry["file"] == filename:
            return entry
    raise KeyError(f"{filename} is not pinned in {_MANIFEST_PATH.name}")


@lru_cache(maxsize=None)
def _vendor_text(filename: str) -> str:
    """A vendored file, verified against its pinned digest.

    Nobody reviews a minified bundle on its way past, so the digest is the
    review: a file that no longer matches the manifest does not get inlined into
    every crate built afterwards — it fails here.
    """
    blob = (_VENDOR_DIR / filename).read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    expected = _entry(filename)["sha256"]
    if digest != expected:
        raise ValueError(
            f"{filename} does not match the digest pinned in {_MANIFEST_PATH.name} "
            f"({digest} != {expected}). Re-vendor it from its source_url, or "
            f"update the manifest deliberately."
        )
    return blob.decode("utf-8")


def _banner(filename: str, comment: str = "/*! {} */") -> str:
    entry = _entry(filename)
    return comment.format(
        f"{entry['name']} {entry['version']} — {entry['license']} — {entry['source_url']}"
    )


@lru_cache(maxsize=1)
def _app_js() -> str:
    """The explorer's own script."""
    return _APP_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _layout_js() -> str:
    """The layout module the app takes its node positions from.

    Its own file, and its own ``<script>``: the geometry is pure — no DOM, no
    React, no payload — so a test can run the code the page runs over a real
    crate's graph, rather than over a second copy of it kept in the test.
    """
    return _LAYOUT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _lane_js() -> str:
    """The layout an assay lane takes its node positions from.

    Loaded after :func:`_layout_js` and before the app: it reads that module's
    node size at factory time, so the order is a requirement rather than a
    convention.
    """
    return _LANE_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def explorer_css() -> str:
    """React Flow's stylesheet, for inlining into the report's one stylesheet.

    The explorer's *own* rules live in ``maturity_report.css`` with the rest of
    the page; this is only the library's, kept unmodified so it can be re-vendored
    without a merge.
    """
    return "\n" + _banner("xyflow-react.style.css") + "\n" + _vendor_text("xyflow-react.style.css")


def _data_island(payload: dict[str, Any]) -> str:
    """The payload, as JSON that cannot be read as HTML.

    ``<``, ``>`` and ``&`` become their JSON unicode escapes — still the same
    string once parsed, but no longer able to close the script element or open a
    tag. The crate is untrusted text (#169) and an entity name is a place a
    ``</script>`` can arrive from.
    """
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_explorer_section(
    metadata: dict[str, Any] | list[dict[str, Any]],
    *,
    default_views: tuple[str, ...] | list[str] | None = None,
) -> str:
    """The interactive entity explorer, as a self-contained report section.

    Emits the mount point, the payload as a JSON data island, and every script
    the page runs — React, React Flow, dagre, htm and the app — inlined from
    :mod:`builder.writers.vendor`. Nothing is fetched: the report is read offline
    from inside the crate, so a ``src`` here would be a section that renders on
    the machine that built it and nowhere else.

    Args:
        metadata: The crate document, as :func:`build_explorer_payload` takes it.
        default_views: Which view keys open; see :func:`build_explorer_payload`.

    Returns:
        The ``<section>…</section>`` markup.
    """
    scripts = []
    for filename in _JS_BUNDLES:
        if filename is None:
            scripts.append(f"<script>{_JSX_SHIM}</script>")
            continue
        scripts.append(f"<script>{_banner(filename)}\n{_vendor_text(filename)}</script>")
    scripts.append(f"<script>{_layout_js()}</script>")
    scripts.append(f"<script>{_lane_js()}</script>")
    scripts.append(
        f'<script id="{_DATA_ID}" type="application/json">'
        f"{_data_island(build_explorer_payload(metadata, default_views=default_views))}"
        "</script>"
    )
    scripts.append(f"<script>{_app_js()}</script>")

    return (
        '<section class="explorer" id="entity-explorer">\n'
        '  <div class="sec-h"><h2>Entity explorer</h2></div>\n'
        f'  <div class="ex-app" id="{_APP_ID}"></div>\n'
        '  <p class="ex-print-note">The entity explorer is interactive: open this '
        "report in a browser to combine views, search the crate and read any "
        "entity&rsquo;s JSON-LD.</p>\n"
        '  <noscript><p class="ex-noscript">The entity explorer needs JavaScript. '
        "The same entities and links are in the crate&rsquo;s "
        "<code>ro-crate-metadata.json</code>.</p></noscript>\n"
        f"  {''.join(scripts)}\n"
        "</section>\n"
    )


# The canvas is one section among many inside the report and takes a slice of the
# page; on a page of its own it *is* the page, so it takes the window.
_STANDALONE_CSS = ".mat .ex-app{height:calc(100vh - 5.5rem); max-height:none;}"


def render_explorer_page(
    metadata: dict[str, Any] | list[dict[str, Any]],
    *,
    title: str = "RO-Crate entity explorer",
    default_views: tuple[str, ...] | list[str] | None = None,
) -> str:
    """The explorer as a standalone, self-contained HTML document.

    The same section the report embeds, in the report's own shell and stylesheet
    — one explorer, rendered in two places, rather than a second one to keep in
    step. This is what the ``--graph`` CLI writes.

    Args:
        metadata: The crate document, as :func:`build_explorer_payload` takes it.
        title: The document title. Escaped: a crate's own name reaches this.
        default_views: Which view keys open; see :func:`build_explorer_payload`.

    Returns:
        A complete ``<!DOCTYPE html>`` document.
    """
    # Imported here, as the report imports this module: both directions are lazy,
    # so neither is a cycle at import time.
    from builder.writers.maturity_report import (
        _SHELL_PLACEHOLDER_RE,
        _load_css,
        _load_shell,
    )

    filling = {
        "__STYLE__": _load_css() + explorer_css() + _STANDALONE_CSS,
        "__BODY__": render_explorer_section(metadata, default_views=default_views),
        "__TITLE__": html.escape(title),
    }
    return _SHELL_PLACEHOLDER_RE.sub(lambda m: filling[m.group(0)], _load_shell())
