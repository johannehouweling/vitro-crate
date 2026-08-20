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
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, NamedTuple

from builder.writers.provenance_dag import (
    _CTX_CATEGORY,
    _CTX_COLOUR,
    _CTX_GLYPH,
    _LAYER_NAMES,
    CATEGORY_STYLES,
    _derivation_edges,
    _graph_nodes,
    _route_hop_ids,
    build_cellline_inventory,
    build_chemical_inventory,
    build_citation_inventory,
    build_crate_graph,
    build_isa_inventory,
    build_people_inventory,
)

PAYLOAD_VERSION = 1
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
        if n["status"] == "in_crate"
        and (n["category"] != "annotation" or n["id"] == crate.root)
    }


def _select_files(crate: _Crate) -> set[str]:
    """Data files and the datasets holding them — the Files panel's selection."""
    return {n["id"] for n in crate.model["nodes"] if n["category"] in ("data", "container")}


def _select_assays(crate: _Crate) -> set[str]:
    """The ISA backbone: Investigation → Study → Assay."""
    return {n["id"] for n in crate.inventory("isa")["nodes"]}


def _select_processes(crate: _Crate) -> set[str]:
    """The derivation chain: every process and what it consumes and produces."""
    edges = _derivation_edges(crate.nodes)
    return {e[0] for e in edges} | {e[1] for e in edges}


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


class ExplorerView(NamedTuple):
    """One toggle: a named selection over the crate's entities."""

    key: str
    label: str
    hint: str
    default: bool
    select: Callable[[_Crate], set[str]]


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
    ExplorerView(
        "all", "All entities", "Everything the crate describes", False, _select_all
    ),
    ExplorerView(
        "files", "Files", "Data files and the datasets that hold them", False, _select_files
    ),
    ExplorerView(
        "assays", "Assays", "Investigation, studies and assays", False, _select_assays
    ),
    ExplorerView(
        "processes",
        "LabProcesses",
        "Processes with what they consumed and produced",
        False,
        _select_processes,
    ),
    ExplorerView(
        "chemicals",
        "MolecularEntities",
        "Compounds and the work that used them",
        False,
        lambda crate: _routed(crate, "chemical", "chemicals"),
    ),
    ExplorerView(
        "samples",
        "Biological Samples",
        "Cell lines and samples, and the work that used them",
        False,
        lambda crate: _routed(crate, "cellline", "celllines"),
    ),
    ExplorerView(
        "people",
        "Persons & Organisations",
        "Who the crate credits",
        False,
        _select_people,
    ),
    ExplorerView(
        "citations", "Citations", "What the crate cites, and who wrote it", False,
        _select_citations,
    ),
)


def _categories() -> dict[str, dict[str, str]]:
    """The drawing registry, as the browser needs it.

    Generated from :data:`CATEGORY_STYLES` for the same reason the stylesheet is
    (`category_css`): a second hand-written palette is how the report came to
    disagree with its own diagrams about what colour a file is.
    """
    out = {
        key: {"colour": style.colour, "label": style.label, "glyph": style.glyph}
        for key, style in CATEGORY_STYLES.items()
    }
    out[_CTX_CATEGORY] = {"colour": _CTX_COLOUR, "label": _CTX_LABEL, "glyph": _CTX_GLYPH}
    return out


def build_explorer_payload(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """The explorer's data island: one crate, in the shape the browser draws.

    Pure and deterministic — the same crate yields the same bytes, so two builds
    of one deposit produce reports that diff to nothing. That needs saying
    because :func:`~builder.writers.provenance_dag.build_crate_graph` yields its
    off-crate stubs out of a set; node order is imposed here.

    Args:
        metadata: A parsed ``ro-crate-metadata.json``, the ``@graph`` list, or a
            ``crate.metadata.generate()`` document.

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
    stubs = sorted(
        (n for n in model["nodes"] if n["layer"] is None), key=lambda n: str(n["id"])
    )
    nodes = [
        {
            "id": n["id"],
            # The model escapes for its SVG; the DOM escapes again downstream, so
            # what travels is the crate's own text.
            "label": html.unescape(n["label"]),
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
    for view in EXPLORER_VIEWS:
        members = view.select(crate) & crate.known
        if not members:
            continue
        views.append(
            {
                "key": view.key,
                "label": view.label,
                "hint": view.hint,
                "default": view.default,
                "members": sorted(members),
            }
        )

    return {
        "version": PAYLOAD_VERSION,
        "root": crate.root,
        "layers": {str(level): name for level, name in _LAYER_NAMES.items()},
        "categories": _categories(),
        "nodes": nodes,
        "edges": [
            {"src": e["src"], "dst": e["dst"], "label": e["label"]} for e in model["edges"]
        ],
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
_MANIFEST_PATH = _VENDOR_DIR / "manifest.json"

_APP_ID = "ex-app"
_DATA_ID = "ex-data"

VENDOR_MANIFEST: tuple[dict[str, Any], ...] = tuple(
    json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
)
"""Every third-party file the page inlines: name, version, licence, origin, digest."""

# react, react-dom, the jsx-runtime shim, React Flow, dagre, htm, the data
# island, the app. Named so a test can state the count without recounting the
# implementation, and so an accidental extra <script> is a failure, not a habit.
EXPLORER_SCRIPT_COUNT = 8

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
def explorer_css() -> str:
    """React Flow's stylesheet, for inlining into the report's one stylesheet.

    The explorer's *own* rules live in ``maturity_report.css`` with the rest of
    the page; this is only the library's, kept unmodified so it can be re-vendored
    without a merge.
    """
    return "\n" + _banner("xyflow-react.style.css") + "\n" + _vendor_text(
        "xyflow-react.style.css"
    )


def _data_island(payload: dict[str, Any]) -> str:
    """The payload, as JSON that cannot be read as HTML.

    ``<``, ``>`` and ``&`` become their JSON unicode escapes — still the same
    string once parsed, but no longer able to close the script element or open a
    tag. The crate is untrusted text (#169) and an entity name is a place a
    ``</script>`` can arrive from.
    """
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )


def render_explorer_section(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> str:
    """The interactive entity explorer, as a self-contained report section.

    Emits the mount point, the payload as a JSON data island, and every script
    the page runs — React, React Flow, dagre, htm and the app — inlined from
    :mod:`builder.writers.vendor`. Nothing is fetched: the report is read offline
    from inside the crate, so a ``src`` here would be a section that renders on
    the machine that built it and nowhere else.

    Args:
        metadata: The crate document, as :func:`build_explorer_payload` takes it.

    Returns:
        The ``<section>…</section>`` markup.
    """
    scripts = []
    for filename in _JS_BUNDLES:
        if filename is None:
            scripts.append(f"<script>{_JSX_SHIM}</script>")
            continue
        scripts.append(f"<script>{_banner(filename)}\n{_vendor_text(filename)}</script>")
    scripts.append(
        f'<script id="{_DATA_ID}" type="application/json">'
        f"{_data_island(build_explorer_payload(metadata))}</script>"
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
