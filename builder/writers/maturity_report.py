"""RO-Crate maturity report (``ro-crate-metadata-maturity.html``), embedded in the crate (#85).

Renders a self-contained, light-mode evaluation dashboard (inline CSS, no external
assets) covering the four axes from the issue:

* **Profile adherence** — base / ISA / ISA-Tox conformance, reported across the
  three SHACL severity tiers Required / Recommended / Optional (#306), with the
  findings from every assessed tier surfaced as actionable suggestions — an author
  who can see what is merely recommended is far likelier to add it than one told
  only that the crate clears the required bar. When the verdict carries structured
  ``issue_records`` each severity row unfolds its own findings, grouped by profile
  layer inside (#510); a pre-records verdict falls back to the flat list;
* **FAIR** — the gated FAIRplus Dataset Maturity level on a ladder whose next rung
  shows the DSM grid's own Total for that level, plus what blocks it;
* **MIT coverage** — per-module coverage of the in-vitro tox MIT checklist;
* **AI-readiness** — the NIH Bridge2AI criteria as a seven-dimension profile,
  with the authors' own per-dimension percentage reported beside ours.

``export_crate`` embeds the rendered page as a ``CreativeWork`` ``about`` ``./``,
mirroring the entity-graph (#130) and preview (#86) artifacts.

``build_maturity_html`` is pure and cheap: FAIR/MIT come from the deterministic
assessors and the profile-adherence section is rendered from the crate's existing
``state.validation`` — it does **not** run the SHACL validator. That keeps the
embed in ``export_crate`` free of validation cost; validation is a separate step.
The one caveat is MIT: scoring the checklist requires the assembled ``@graph``,
so a caller that omits ``graph`` makes ``assess_mit_coverage`` assemble one
in-memory (no disk, no network, no SHACL — cheaper than validation by an order of
magnitude, and the export path pays nothing because it already passes its graph).
The alternative was a second scorer that returned 0.0 for every real crate, which
is not cheapness but a wrong answer (#311).

Severity-tier nuance (#306): the fast in-loop path (``build_and_validate``) gates
at REQUIRED severity and never populates ``should_issues`` / ``may_issues``, so an
empty list at those tiers means the tier was *never evaluated*, not that it is
clean. The report models an explicit "not assessed" state for such tiers and never
renders an unevaluated tier as a green zero. Export closes that gap for the written
crate: ``export_crate`` validates at the OPTIONAL gate, which assesses all three
tiers, so a report embedded on the export path normally has a real verdict in every
row — the "not assessed" state remains for reports rendered from a partial verdict.
"""

from __future__ import annotations

import html
import logging
import re
from collections import Counter
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from builder.state import (
    PROFILE_LAYER_CHAIN,
    AIRReport,
    CrateState,
    FAIRReport,
    MITReport,
    ValidationReport,
)
from builder.tools.fair_assessment import assess_fair_maturity
from builder.tools.mit_assessment import (
    MIT_INDICATORS_URL,
    MIT_STANDARD_LABELS,
    MIT_STANDARD_SOURCES,
    assess_mit_coverage,
    mit_was_assessed,
)
from builder.writers.provenance_dag import (
    RESIDENCES,
    category_css,
)

logger = logging.getLogger(__name__)

REPORT_FILENAME = "ro-crate-metadata-maturity.html"

# One MIT module, one colour (#606) — THE registry, keyed by the module name the
# scorer keys ``MITReport.module_scores`` by. The module rows and every span of a
# guidance-document bar are painted from it (as a ``--mod`` custom property the
# renderer sets inline; the stylesheet derives fill, track and pale state from
# that one token and declares no module colour of its own — a test asserts it),
# the ``CATEGORY_STYLES`` rule (#487) applied to the checklist. Row and span
# order is not this dict's: it is the scorer's, which is the checklist's.
#
# The six colours are a categorical palette chosen by search, not by eye, and
# every floor below is pinned by ``TestMitModuleColours``: all pairs (not just
# neighbours — a module that contributes nothing to a document drops out of
# its bar, so any two can sit side by side) clear OKLab dE 8 under simulated
# protanopia and deuteranopia (Machado 2009, severity 1) and CIE76 dE 20 under
# normal vision; every colour clears 3:1 on the page; and each keeps CIE76 dE
# 12 from the report's status colours (good / warn / low / coverage teal) so
# no module can impersonate a verdict. Lightness alternates between two steps
# because that, not hue, is what keeps six hues apart for a dichromat reader.
# The floors are for the solid colours only: a pale (still-missing) part is
# never read on its own — it sits inside its module's pill, next to that
# module's solid part — so pale-vs-pale separation is not claimed.
# This palette is deliberately independent of the entity-category ring: a
# palette clear of the status colours AND all ten category colours does not
# exist in this lightness band, and the two never share a figure — each is
# keyed where it is used (the module rows name their colours directly).
MIT_MODULE_STYLES: dict[str, str] = {
    "General Information": "#3c52b6",
    "Chemical Information": "#8f4700",
    "Biological Model Information": "#8e8f2b",
    "Exposure Information": "#903081",
    "Endpoint Read Out Information": "#1392d4",
    "Analysis and Statistics": "#b96f8c",
}

# A module the registry does not know — a renamed or added checklist module —
# is drawn, not dropped, and drawn grey: a colour asserts the module is one the
# rows above name, and an unknown one has not earned that. The stylesheet's own
# neutral, referenced rather than copied, so it cannot go stale.
MIT_MODULE_FALLBACK_COLOUR = "var(--na-ink)"


def _validation_has_signal(validation: ValidationReport) -> bool:
    """True if a validation has actually run (some pass set, or any issue logged).

    A default :class:`ValidationReport` (all passes ``False``, no issues) means
    the crate hasn't been validated yet — distinct from "validated and failing".
    """
    return bool(
        validation.base_passed
        or validation.isa_passed
        or validation.tox_passed
        or validation.required_issues
        or validation.should_issues
        or validation.may_issues
        or validation.assessed_tiers
    )


# ---------------------------------------------------------------------------
# Severity tiers (#306)
# ---------------------------------------------------------------------------


def _plural_issues(n: int) -> str:
    return f"{n} issue" if n == 1 else f"{n} issues"


def _severity_tiers(val: ValidationReport) -> list[dict[str, str]]:
    """Profile-adherence severity tiers rendered from existing validation (#306).

    Returns one dict per tier: ``{tier, state, summary, note}`` where ``state`` is
    ``"ok"`` | ``"no"`` | ``"na"``.

    REQUIRED is the build gate, so it is assessed whenever validation has run; it
    passes only when all three profiles pass with no REQUIRED issues. The SHOULD
    and MAY tiers are populated only by a full validation sweep — the fast in-loop
    path stops at REQUIRED and leaves them empty. Emptiness is not the test:
    ``assessed_tiers`` is. A tier the sweep reached and found clean renders a green
    "0 issues"; a tier nobody evaluated renders "not assessed" (``"na"``), never a
    green zero for a tier that was never looked at.

    Called only when :func:`_validation_has_signal` is True (otherwise the whole
    section renders the "not yet validated" branch).
    """
    n_pass = sum((val.base_passed, val.isa_passed, val.tox_passed))
    req_ok = n_pass == 3 and not val.required_issues
    tiers: list[dict[str, str]] = [
        {
            "key": "required",
            "tier": "Required",
            "state": "ok" if req_ok else "no",
            "summary": f"{n_pass} / 3 profiles",
            "note": "",
        }
    ]
    # A note that only restates the tier's own name is noise beside it; the one state
    # a label cannot explain — "not assessed" — keeps its sentence below.
    for label, issues, note in (
        ("Recommended", val.should_issues, ""),
        ("Optional", val.may_issues, ""),
    ):
        key = label.casefold()
        if issues:
            tiers.append(
                {
                    "key": key,
                    "tier": label,
                    "state": "no",
                    "summary": _plural_issues(len(issues)),
                    "note": note,
                }
            )
        elif key in val.assessed_tiers:
            tiers.append(
                {"key": key, "tier": label, "state": "ok", "summary": "0 issues", "note": note}
            )
        else:
            tiers.append(
                {
                    "key": key,
                    "tier": label,
                    "state": "na",
                    "summary": "not assessed",
                    "note": "Not evaluated — the build gates at Required.",
                }
            )
    return tiers


# --- rendering helpers -----------------------------------------------------
_GLYPH = {"ok": "✓", "no": "✗", "na": "–"}
_MK_LABEL = {"ok": "met", "no": "not met", "na": "not assessed"}


def _mk(kind: str) -> str:
    """A status mark (glyph + accessible label) — colour is never the only cue."""
    return f'<span class="mk {kind}" aria-label="{_MK_LABEL[kind]}">{_GLYPH[kind]}</span>'


def _kind(ok: bool | None) -> str:
    if ok is None:
        return "na"
    return "ok" if ok else "no"


_ASSET_DIR = Path(__file__).resolve().parent
_CSS_PATH = _ASSET_DIR / "maturity_report.css"
_SHELL_PATH = _ASSET_DIR / "maturity_report.html"


_CSS_CATEGORY_TOKEN = "__CATEGORY_STYLES__"


@lru_cache(maxsize=1)
def _load_css() -> str:
    """The report stylesheet, inlined into the self-contained page.

    Kept in a sibling ``maturity_report.css`` so the styling lives apart from the
    Python that assembles the markup; it is embedded (not linked) so the exported
    ``ro-crate-metadata-maturity.html`` renders offline with no external assets.

    The one exception to that separation is the per-category palette, which is
    substituted in from :func:`provenance_dag.category_css`. CSS cannot iterate,
    and the rules are the same three lines per category; hand-writing them is how
    the report came to disagree with the diagrams about what colour a file is.
    """
    css = _CSS_PATH.read_text(encoding="utf-8").strip("\n")
    if _CSS_CATEGORY_TOKEN not in css:  # pragma: no cover - guards a bad edit
        raise ValueError(
            f"{_CSS_PATH.name} is missing the {_CSS_CATEGORY_TOKEN} placeholder, so the "
            "report would render with no entity colours at all."
        )
    return css.replace(_CSS_CATEGORY_TOKEN, category_css())


# The shell's placeholders, matched in ONE pass (see build_maturity_html). Kept
# module-level so the pattern is compiled once and the set of sentinels has a
# single definition rather than one per call site.
_SHELL_PLACEHOLDER_RE = re.compile(r"__(?:STYLE|BODY|TITLE)__")


@lru_cache(maxsize=1)
def _load_shell() -> str:
    """The document shell (``maturity_report.html``) whose ``__STYLE__`` /
    ``__TITLE__`` / ``__BODY__`` placeholders are filled at render time."""
    return _SHELL_PATH.read_text(encoding="utf-8")


# Every indicator the model publishes has its own entry on the FAIRplus documentation
# site, anchored by the lowercased identifier. Naming an indicator without a route to
# its definition asks a reader to take our paraphrase of it on trust.
_DSM_DOCS = "https://fairplus.github.io/Data-Maturity/docs/Indicators/#"
# The model publishes one page per level, so a level's name links there.
_DSM_LEVEL_DOCS = "https://fairplus.github.io/Data-Maturity/docs/Levels/Level"


def _dsm_lk(ident: str) -> str:
    """The indicator id, linked to the model's own definition of it."""
    return _lk(_DSM_DOCS + html.escape(ident.lower()), ident)


def _lk(url: str, text: str) -> str:
    """An accent link with the report's card-link styling; crate text escaped.

    Only http(s) targets become links — a URL here is crate-controlled text,
    and any other scheme (``javascript:``, ``data:``) must never reach the page
    as an href. A non-web target renders as plain text instead.
    """
    if not str(url).startswith(("http://", "https://")):
        return html.escape(text)
    return f'<a class="lk" href="{html.escape(url)}">{html.escape(text)}</a>'


_NOT_STATED = '<span class="not-stated">not stated</span>'


def _render_header(title: str, subhead: str) -> str:
    """The page header: eyebrow, the study name as the headline, and a subhead —
    the publication's name when the crate cites one.

    The name leads (#719); the identifier, whatever its shape, is the study
    card's to state, since a reader who cannot see it cannot question it.

    No verdict pill, no chips, no scope caveats: conformance lives in the
    Profile conformance tile and the caveats with the findings they qualify
    (the #607 design handoff)."""
    esc = html.escape
    h1 = title
    sub = f'<p class="subhead">{esc(subhead)}</p>\n' if subhead and subhead != h1 else ""
    return (
        "<header>\n"
        '  <div class="h-left">\n'
        '    <div class="kicker"><span class="eyebrow">vitro-crate maturity report</span></div>\n'
        f"    <h1>{esc(h1)}</h1>\n"
        f"{sub}"
        "  </div>\n"
        "</header>\n"
    )


def _raw_nodes(graph: dict[str, Any] | list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """``@id -> node`` for a raw metadata document (or ``{}`` without one)."""
    items = graph.get("@graph", []) if isinstance(graph, dict) else (graph or [])
    return {
        str(n["@id"]): n for n in items if isinstance(n, dict) and isinstance(n.get("@id"), str)
    }


def _root_of(nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The Root Data Entity: what the metadata descriptor is ``about``."""
    desc = nodes.get("ro-crate-metadata.json") or {}
    about = desc.get("about")
    rid = about.get("@id") if isinstance(about, dict) else about
    return nodes.get(str(rid) or "./") or nodes.get("./") or {}


def _ref_ids(node: dict[str, Any], key: str) -> list[str]:
    """The @ids (or bare strings) a property points at, listified."""
    value = node.get(key)
    values = value if isinstance(value, list) else [value] if value is not None else []
    out = []
    for v in values:
        if isinstance(v, dict) and v.get("@id"):
            out.append(str(v["@id"]))
        elif isinstance(v, str) and v:
            out.append(v)
    return out


def _type_set(node: dict[str, Any]) -> set[str]:
    """The node's ``@type`` local names — ``"Dataset"`` or ``["File", "csvw:Table"]``
    alike — so a type test reads the same whatever form the serializer chose."""
    value = node.get("@type")
    values = value if isinstance(value, list) else [value] if value is not None else []
    return {str(v).rsplit(":", 1)[-1].rsplit("/", 1)[-1] for v in values if isinstance(v, str)}


def _study_facts(
    state: CrateState, graph: dict[str, Any] | list[dict[str, Any]] | None
) -> dict[str, Any]:
    """What the "About this study" card states, read from the crate itself.

    Every value is either a fact the graph (or, without one, ``state.metadata``)
    holds, or absent — the card renders an honest "not stated", never a guess.
    """
    facts: dict[str, Any] = {
        "contact": None,  # (name, url|None)
        "affiliation": None,
        "funder": None,
        "licence": None,
        "publication": None,  # (doi url|None, display text, article name)
        "dataset_doi": None,
        "description": state.metadata.description or "",
        "accession": (state.metadata.accession or "").strip(),
    }
    if graph is None:
        m = state.metadata
        if m.contact:
            facts["contact"] = (m.contact, m.contact if "://" in m.contact else None)
        if m.license:
            facts["licence"] = (m.license, m.license if "://" in m.license else None)
        return facts

    from builder.writers.provenance_dag import build_citation_inventory, build_people_inventory

    nodes = _raw_nodes(graph)
    root = _root_of(nodes)
    if isinstance(root.get("description"), str) and root["description"].strip():
        facts["description"] = root["description"]

    def pid_url(agent: dict[str, Any]) -> str | None:
        pid, scheme = agent.get("pid"), agent.get("pid_scheme")
        if not pid:
            return None
        if "://" in str(pid):
            return str(pid)
        host = {"ORCID": "https://orcid.org/", "ROR": "https://ror.org/"}.get(str(scheme))
        return f"{host}{pid}" if host else None

    inv = build_people_inventory(graph)
    agents = {a["id"]: a for a in inv["agents"]}
    for prop in ("contactPoint", "creator", "publisher", "author"):
        refs = _ref_ids(root, prop)
        person = next(
            (agents[r] for r in refs if agents.get(r, {}).get("kind") == "person"), None
        )
        if person:
            facts["contact"] = (person["name"], pid_url(person))
            org = next((agents[o] for o in person.get("affiliations", []) if o in agents), None)
            if org:
                org_url = pid_url(org) or (org["id"] if "://" in str(org["id"]) else None)
                facts["affiliation"] = (org["name"], org_url)
            break
        # A bare literal ("creator": "Jane Doe") is legal JSON-LD and common in
        # hand-authored crates: the crate DOES state a contact, so say it.
        literal = next((r for r in refs if r not in nodes and "://" not in r), None)
        if literal:
            facts["contact"] = (literal, None)
            break

    funder = next((nodes[r] for r in _ref_ids(root, "funder") if r in nodes), None)
    if funder is not None:
        name = funder.get("name") or funder.get("@id")
        url = str(funder.get("url") or funder.get("@id") or "")
        facts["funder"] = (str(name), url if url.startswith(("http://", "https://")) else None)

    for ref in _ref_ids(root, "license"):
        node = nodes.get(ref) or {}
        name = str(node.get("name") or "")
        if "not stated" in name.lower():
            break  # the depositor stated no terms — that IS the fact (#540)
        url = ref if ref.startswith(("http://", "https://")) else str(node.get("url") or "")
        if not name:
            # A bare licence URL is the common convention; the registry knows
            # the reader-facing name ("CC BY 4.0"), and only when it does not
            # is the URL's last path segment better than nothing.
            try:
                from profiles.licenses import describe_license

                described = describe_license(ref) or describe_license(url)
                if described:
                    name = str(described.get("name") or "")
            except Exception:  # noqa: BLE001 — a name lookup must not fail a report
                logger.debug("licence lookup failed for %r", ref, exc_info=True)
        facts["licence"] = (name or ref.rstrip("/").rsplit("/", 1)[-1], url or None)
        break

    for value in _ref_ids(root, "identifier"):
        raw = value
        if raw in nodes:  # a PropertyValue entity
            raw = str(nodes[raw].get("value") or "")
        doi = _doi_url(raw)
        if doi:
            facts["dataset_doi"] = doi
            break
        if raw:  # not a DOI: the accession the crate states (#719)
            facts["accession"] = raw

    articles = build_citation_inventory(graph)["articles"]
    cited = [a for a in articles if a["state"] == "cited"] or articles
    with_doi = [a for a in cited if a.get("doi")]
    if with_doi:
        a = with_doi[0]
        # The inventory keeps the DOI in whatever spelling the crate used
        # (usually the bare 10.x form); normalise so the cell links it.
        url = _doi_url(str(a["doi"])) or ""
        facts["publication"] = (url or None, _doi_text(url) if url else str(a["doi"]),
                                str(a["name"]))
    elif cited:
        facts["publication"] = (None, "", str(cited[0]["name"]))
    return facts


def _doi_url(value: str) -> str | None:
    """A DOI in any of its spellings, normalised to ``https://doi.org/…``."""
    v = (value or "").strip()
    if v.lower().startswith("doi:"):
        v = v[4:]
    if v.startswith("10.") and "/" in v:
        return f"https://doi.org/{v}"
    if "doi.org/" in v:
        return v if v.startswith(("http://", "https://")) else f"https://{v}"
    return None


def _doi_text(url: str) -> str:
    """``doi.org/10.x/…`` — the display form of a DOI link."""
    return url.split("://", 1)[-1]


def _render_study_card(facts: dict[str, Any]) -> str:
    """The "About this study" card: who to ask, under what terms, and where the
    work is published — with the study description under a rule."""
    esc = html.escape

    def cell(label: str, value: str) -> str:
        return f'<div class="hcell"><span class="hlabel">{label}</span>{value}</div>'

    def linked(pair: tuple[str, str | None] | None) -> str:
        if not pair or not pair[0]:
            return _NOT_STATED
        name, url = pair
        return _lk(url, name) if url else esc(name)

    pub_cell = ""
    pub = facts.get("publication")
    dataset = facts.get("dataset_doi")
    if pub or dataset:
        parts = ""
        if pub:
            url, text, name = pub
            parts += _lk(url, text) if url else esc(name)
        if dataset:
            # Stacked under the publication in the SAME cell — pinning the two
            # to grid columns broke their pairing every time a field was added.
            label = '<span class="hlabel">Dataset</span>' if pub else ""
            parts += f'<span class="hstack">{label}{_lk(dataset, _doi_text(dataset))}</span>'
        pub_cell = cell("Publication" if pub else "Dataset", parts)
    description = (
        f'<div class="hnote">{esc(facts["description"])}</div>' if facts["description"] else ""
    )
    return (
        '<div class="hcard">\n'
        '  <div class="hcard-h">About this study</div>\n'
        '  <div class="hgrid">'
        + (cell("Identifier", esc(facts["accession"])) if facts.get("accession") else "")
        + cell("Contact person", linked(facts.get("contact")))
        + cell("Affiliation", linked(facts.get("affiliation")))
        + cell("Funder", linked(facts.get("funder")))
        + cell("Licence", linked(facts.get("licence")))
        + pub_cell
        + "</div>\n"
        f"{description}"
        "</div>\n"
    )


def _report_id(state: CrateState, graph: dict[str, Any] | list[dict[str, Any]] | None) -> str:
    """``MR-<date>-<hash6>`` — the date the run ended plus six hex of the graph
    the report was rendered from (or, without one, of the state fingerprint).

    The digest covers the document *as of report generation*: the shipped
    ``ro-crate-metadata.json`` additionally lists the report file itself, so
    the id identifies the content the figures were computed over rather than
    promising byte-recomputability from the shipped file."""
    import hashlib
    import json as _json

    ended = (state.generator.ended_at or "")[:10]
    try:
        payload = (
            _json.dumps(graph, sort_keys=True, default=str)
            if graph is not None
            else state.validation_fingerprint()
        )
    except Exception:  # noqa: BLE001 — an id must never fail a report
        payload = state.metadata.title or ""
    digest = hashlib.sha256(str(payload).encode("utf-8")).hexdigest()[:6]
    return f"MR-{ended}-{digest}" if ended else f"MR-{digest}"


def _render_crate_card(
    state: CrateState,
    val: ValidationReport | None,
    graph: dict[str, Any] | list[dict[str, Any]] | None,
    *,
    validated: bool = False,
) -> str:
    """The "About this RO-Crate" card — the report's closing colophon: the
    build facts the crate itself records (the data behind its
    ``vitro-crate build`` CreateAction), and how this report came to be.

    Cells render only what the run actually recorded — a bare state renders the
    tool and nothing invented. The closing provenance note renders only when the
    report is built WITH the crate's graph: it claims the figures come from the
    crate's own metadata, and a state-only render cannot claim that.
    """
    esc = html.escape
    gen = state.generator
    if not gen.name:
        return ""

    def cell(label: str, value: str) -> str:
        return f'<div class="hcell"><span class="hlabel">{label}</span>{value}</div>'

    cells = []
    built = f"{gen.name} {gen.version}".strip()
    cells.append(cell("Built by", _lk(gen.url, built) if gen.url else esc(built)))
    if gen.model:
        from builder.tools._crate_mapping import _model_docs_url

        model = _lk(url, gen.model) if (url := _model_docs_url(gen.model)) else esc(gen.model)
        run = ""
        seconds = gen.model_seconds or gen.duration_seconds or 0
        if gen.llm_calls:
            run = f" · {gen.llm_calls} calls"
            if seconds:
                minutes = max(1, round(seconds / 60))
                run += f", {minutes} minute{'s' if minutes != 1 else ''}"
        cells.append(cell("Model", f"{model}{esc(run)}"))
    if gen.ended_at:
        ts = gen.ended_at[:16].replace("T", " ")
        if gen.ended_at.endswith("Z") or "+00:00" in gen.ended_at:
            ts += " UTC"
        elif zone := re.search(r"[+-]\d{2}:\d{2}$", gen.ended_at):
            # An unlabelled wall time invites reading +02:00 as UTC.
            ts += f" {zone.group(0)}"
        cells.append(cell("Created", f"<b>{esc(ts)}</b>"))
    if gen.input_tokens or gen.output_tokens:
        cells.append(
            cell(
                "Tokens",
                f'<span title="input tokens">&darr; {gen.input_tokens:,}</span>, '
                f'<span title="output tokens">&uarr; {gen.output_tokens:,}</span>',
            )
        )
    assessed = getattr(val, "assessed_tiers", None) or set()
    gate = next((t for t in ("optional", "recommended", "required") if t in assessed), None)
    if gate:
        cells.append(cell("Validation gate", f"<b>{gate}</b>"))

    note = ""
    if graph is not None:
        # The conformance clause states that a SHACL verdict exists — it may
        # only appear when one does (fresh, not stale): a note that claims a
        # validation nobody ran is exactly the false green this report bans.
        conformance = (
            "conformance from a SHACL validation against the three profiles, " if validated else ""
        )
        code = '<code class="hcode">{}</code>'
        note = (
            '<div class="hnote">This report is '
            + code.format(esc(_report_id(state, graph)))
            + ", generated by vitro-crate at export and travelling inside the crate as "
            + code.format(REPORT_FILENAME)
            + ". Every figure on this page is computed from the crate&rsquo;s own "
            + code.format("ro-crate-metadata.json")
            + ": the build facts above come from its "
            + code.format("vitro-crate build")
            + f" CreateAction, {conformance}"
            "and the FAIR and domain scores from deterministic assessors run over the same "
            "graph. Nothing is fetched at view time and no figure is estimated.</div>"
        )
    return (
        '<div class="hcard">\n'
        '  <div class="hcard-h">About this RO-Crate</div>\n'
        f'  <div class="hgrid">{"".join(cells)}</div>\n'
        f"{note}"
        "</div>\n"
    )


# The spec each conformance row links to. The IRIs mirror the crate's own
# ``conformsTo`` declarations (``_crate_mapping.ROCRATE_SPEC`` / ``PROFILE_ISA``
# / ``PROFILE_ISATOX``); a test pins the two lists together so they cannot
# drift apart.
_PROFILE_SPEC_URLS: dict[str, str] = {
    "base": "https://w3id.org/ro/crate/1.2",
    "isa": "https://github.com/nfdi4plants/isa-ro-crate-profile",
    "tox": "https://w3id.org/ro/crate/isa-tox/1.0",
}

_TIER_KEYS: tuple[str, ...] = ("required", "recommended", "optional")

# Cell state -> the mark it paints, and the words that carry it to a screen
# reader. "none" paints the same neutral mark as "na" but says something else:
# not "nobody looked here" but "there is nothing here to look at".
_CELL_MARK: dict[str, str] = {"ok": "ok", "no": "no", "na": "na", "none": "na"}
_CELL_WORDS: dict[str, str] = {**_MK_LABEL, "none": "no checks defined"}


@lru_cache(maxsize=1)
def _tier_capability() -> dict[str, frozenset[str]]:
    """Which tiers each profile layer defines checks at — see
    :func:`profiles.validator.tiers_defined`.

    Imported lazily and cached for the process: importing the validator runs its
    upstream-bootstrap patching, and a report rendered from a verdict-less
    checkpoint never needs the answer. A validator that will not import leaves
    every layer unknown, and an unknown tier reads as unverified rather than as
    a pass.
    """
    try:
        from profiles.validator import tiers_defined
    except ImportError:  # no validator installed: nothing could have been checked
        logger.warning("The validator is not importable; profile tier capability is unknown")
        return {}
    return {key: tiers_defined(key) for key, _ in _PROFILE_LAYERS}


def _profile_tier_counts(val: ValidationReport | None) -> dict[str, dict[str, int]]:
    """``{profile: {tier: findings}}`` from the verdict's records — or, for a
    tier that predates records, parsed from the flat display strings' own
    ``[profile]`` prefix. Unattributable findings land under ``""``."""
    counts: dict[str, dict[str, int]] = {}
    if val is None:
        return counts

    def add(profile: str, tier: str) -> None:
        bucket = counts.setdefault(profile, {})
        bucket[tier] = bucket.get(tier, 0) + 1

    records = getattr(val, "issue_records", None) or []
    seen_tiers = {str(r.get("severity") or "").lower() for r in records}
    for r in records:
        add(str(r.get("profile") or ""), str(r.get("severity") or "").lower())
    for tier, flat in (
        ("required", val.required_issues),
        ("recommended", val.should_issues),
        ("optional", val.may_issues),
    ):
        if tier in seen_tiers:
            continue
        for message in flat or []:
            m = re.match(r"\[(\w+)\]", str(message))
            add(m.group(1) if m else "", tier)
    return counts


def _profile_matrix_tile(
    val: ValidationReport | None, tiers: list[dict[str, str]] | None, stale: bool
) -> str:
    """Profile conformance as a profile × requirement-level matrix: rows the
    three layers (each linked to its spec), columns the severity tiers, cells a
    mark with the finding count on its ``title``. An unassessed tier — and every
    cell of a stale or never-run verdict — is the neutral mark, never a green.

    So is a tier NOTHING in the stack defines a check at
    (:func:`_tier_capability`): a green there would report the profiles' silence
    as the crate's cleanliness. Only a tier that could have failed may pass.

    Each row is cumulative over :data:`~builder.state.PROFILE_LAYER_CHAIN` — its
    own checks and every layer it extends — because that is what conformance to
    a layered profile means. It is also what makes the OPTIONAL column answerable at all: ISA and
    ISA-Tox declare no ``sh:Info`` shape of their own, but they extend a profile
    that declares twelve, and a crate conforming to ISA-Tox conforms to those
    too. Reporting each layer in isolation left that column a permanent dash,
    which reads as "this level does not apply" rather than "inherited"."""
    heads = "".join(f'<span class="pmx-h">{t.capitalize()}</span>' for t in _TIER_KEYS)
    rows = ""
    counts = _profile_tier_counts(val if tiers is not None else None)
    assessed = (getattr(val, "assessed_tiers", None) or {"required"}) if tiers else set()
    passed = {
        "base": val.base_passed if val else False,
        "isa": val.isa_passed if val else False,
        "tox": val.tox_passed if val else False,
    }
    for key, label in _PROFILE_LAYERS:
        chain = PROFILE_LAYER_CHAIN[key]
        cells = ""
        for tier in _TIER_KEYS:
            own = counts.get(key, {}).get(tier, 0)
            n = sum(counts.get(layer, {}).get(tier, 0) for layer in chain)
            inherited = n - own
            tail = f", {inherited} inherited" if inherited else ""
            if tiers is None:
                state, title = "na", "not yet validated"
            elif stale:
                state, title = "na", "recorded before the crate&rsquo;s latest changes"
            elif tier not in assessed:
                state, title = "na", "not assessed at this level"
            elif tier == "required" and not all(passed[layer] for layer in chain):
                state = "no"
                title = (
                    f"{n} finding{'s' if n != 1 else ''} at this level{tail}"
                    if n
                    else (
                        "profile gate failed"
                        if not passed[key]
                        else "a profile it extends did not pass"
                    )
                )
            elif n:
                state = "no"
                title = f"{n} finding{'s' if n != 1 else ''} at this level{tail}"
            elif not any(tier in _tier_capability().get(layer, frozenset()) for layer in chain):
                # Nothing in the stack has a rule at this level, so an empty
                # result is the profiles' silence rather than the crate's
                # cleanliness (#620). Ranked below the count: a finding filed at
                # such a tier — by a local checker, or by a checkpoint from a
                # profile version that did have rules here — is still a finding.
                state, title = "none", "no checks defined at this level"
            else:
                state, title = "ok", "no findings at this level"
            # The cell carries the whole sentence for AT — the grid has no
            # table semantics, so an unassociated "met / not met" stream would
            # say nothing about which profile or tier it belongs to.
            mark, words = _CELL_MARK[state], _CELL_WORDS[state]
            plain = title.replace("&rsquo;", "'")
            cells += (
                f'<span class="pmx-c" data-cell="{key}-{tier}" title="{title}" role="img" '
                f'aria-label="{label}, {tier}: {words} — {plain}">'
                f'<span class="mk {mark}" aria-hidden="true">{_GLYPH[mark]}</span></span>'
            )
        rows += f'<span class="pmx-p">{_lk(_PROFILE_SPEC_URLS[key], label)}</span>{cells}'
    # Findings the validator did not attribute to a layer are reported, never
    # dropped: a cell claiming "no findings" while the tier holds unattributed
    # ones would be the false green this matrix exists to refuse.
    if any(counts.get("", {}).get(t) for t in _TIER_KEYS):
        cells = ""
        for tier in _TIER_KEYS:
            n = counts.get("", {}).get(tier, 0)
            state = "no" if n else ("ok" if tier in assessed else "na")
            title = (
                f"{n} finding{'s' if n != 1 else ''} not attributed to a profile"
                if n
                else ("no findings at this level" if tier in assessed else "not assessed")
            )
            words = _CELL_WORDS[state]
            cells += (
                f'<span class="pmx-c" data-cell="unattributed-{tier}" title="{title}" '
                f'role="img" aria-label="unattributed, {tier}: {words} — {title}">'
                f'<span class="mk {state}" aria-hidden="true">{_GLYPH[state]}</span></span>'
            )
        rows += f'<span class="pmx-p">unattributed</span>{cells}'
    return (
        '<article class="kpi">'
        '<div class="kpi-h"><span class="eyebrow">Profile conformance</span></div>'
        f'<div class="pmx"><span></span>{heads}{rows}</div>'
        "</article>"
    )


def _fair_tile(
    fair: FAIRReport,
    blockers: list[tuple[str, str, str]],
    ceiling: dict[str, Any] | None = None,
    grid: dict[int, dict[str, Any]] | None = None,
) -> str:
    """FAIR maturity: the derived DSM level, a ladder whose *next* rung shows how much
    of that level the sheet already scores as complete (a gated 0 must not read as
    "nothing done"), and — in red — how many indicators stand before it.

    The rung is filled from the DSM grid's own Total for the level in question: every
    number on a DSM-labelled bar is the DSM's, never the RDA indicator set's.
    """
    # The denominator is the highest level a crate can REACH, not the model's top
    # rung: Level 5 is scored entirely on hosting and enterprise data governance, so
    # "/ 5" advertises a rung no RO-Crate can stand on. Falls back to 5 only when the
    # ceiling could not be computed.
    cap = int((ceiling or {}).get("ceiling") or 5)
    nxt = (grid or {}).get(fair.dsm_level + 1, {}).get("TOTAL") or {}
    pct = round(nxt.get("published_pct") or 0)
    title = f'{nxt.get("met", 0)} of {nxt.get("total", 0)} indicators at that level'
    rungs = ""
    for level in range(1, cap + 1):
        if level <= fair.dsm_level:
            rungs += '<span class="rung2 done"></span>'
        elif level == fair.dsm_level + 1:
            rungs += (
                f'<span class="rung2 next" title="{title}">'
                f'<i style="width:{pct}%"></i></span>'
            )
        else:
            rungs += '<span class="rung2 off"></span>'
    blocked = ""
    if blockers:
        # The count is a claim a reader must be able to drill into, and it now drills
        # into the one place the page answers "what do I do": Recommendations, where
        # each of these is a row carrying an instruction. Restating the list here as
        # bare indicator text — the model's question, with no fix — is what made a
        # maturity gap read differently from a conformance finding.
        n = len(blockers)
        blocked = (
            f'<a class="blockers" href="#next"><b>{n} '
            f'indicator{"s" if n != 1 else ""}</b> to level {fair.dsm_level + 1}</a>'
        )
    reach = ""
    if cap and fair.dsm_level >= cap:
        reach = '<div class="kpi-sub">at the ceiling for a crate</div>'
    return (
        '<article class="kpi fair-tile">'
        '<div class="kpi-h"><span class="eyebrow">FAIR maturity</span></div>'
        f'<div class="kpi-v"><b>{fair.dsm_level}</b><span class="den">/ {cap}</span> '
        '<span class="tag-inline">DSM level<a class="fn" href="#fn-dsm">1</a></span></div>'
        f'<div class="ladder2" role="img" aria-label="DSM level {fair.dsm_level} of {cap}; '
        f'level {fair.dsm_level + 1} is {pct}% complete">{rungs}</div>'
        f"{reach}{blocked}"
        "</article>"
    )


def _wrap_label(text: str, width: int = 20) -> list[str]:
    """*text* broken into at most two lines of about *width* characters.

    SVG text does not wrap, and the longest module name ("Endpoint Read Out
    Information") is wider than the rose at label size — an unwrapped line is
    clipped by the viewBox rather than shrunk.
    """
    words = text.split()
    lines: list[str] = [""]
    for word in words:
        candidate = f"{lines[-1]} {word}".strip()
        if len(candidate) <= width or not lines[-1]:
            lines[-1] = candidate
        elif len(lines) < 2:
            lines.append(word)
        else:
            lines[-1] = f"{lines[-1]} {word}"
    return [line for line in lines if line]


def _mit_totals(mit: MITReport) -> tuple[int, int]:
    """``(completed, total)`` over the module buckets — the one sum the rose
    tile's sub-line, the rose's wedges and the MIT section header
    all read, so the three cannot disagree. ``overall_score`` is this ratio
    (see ``_score_modules``)."""
    completed = sum(sc.get("completed", 0) for sc in mit.module_scores.values())
    total = sum(sc.get("total", 0) for sc in mit.module_scores.values())
    return completed, total


def _mit_rose_svg(mit: MITReport) -> str:
    """The MIT coverage rose: one wedge per module, angle = the module's share
    of the checklist, radius = how much of that module is filled. A faint
    full-radius wedge behind each carries the share, so an empty module still
    shows the ground it owes. Pure trigonometry over the scorer's own buckets.

    Each module is one ``<g>`` holding its pale share wedge, its filled wedge,
    a ``<title>`` (the native tooltip, and what a screen reader reads) and a
    centred label that CSS reveals while the group is hovered — hovering
    anywhere in a module's slice, filled or not, names it and its numbers.
    The interaction is CSS-only because the report ships with no script, and
    it is an enhancement, never the only path: the module rows in the MIT
    coverage section below carry the same numbers as text.
    """
    import math

    _, total_all = _mit_totals(mit)
    if not total_all:
        return ""
    cx = cy = 87.0
    radius = 82.0

    def point(angle: float, r: float) -> tuple[float, float]:
        rad = math.radians(angle - 90)  # 12 o'clock start, clockwise
        return cx + r * math.cos(rad), cy + r * math.sin(rad)

    def wedge(a0: float, a1: float, r: float, fill: str) -> str:
        if a1 - a0 >= 360:  # a single module owns the whole ring
            return f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="{fill}"></circle>'
        x0, y0 = point(a0, r)
        x1, y1 = point(a1, r)
        large = 1 if (a1 - a0) > 180 else 0
        return (
            f'<path d="M {cx},{cy} L {x0:.2f},{y0:.2f} '
            f'A {r:.2f},{r:.2f} 0 {large} 1 {x1:.2f},{y1:.2f} Z" fill="{fill}"></path>'
        )

    groups = ""
    angle = 0.0
    for name, sc in mit.module_scores.items():
        total, completed = sc.get("total", 0), sc.get("completed", 0)
        if not total:
            continue
        sweep = total / total_all * 360
        caption = f"{name} — {completed} of {total} filled"
        lines = _wrap_label(name)
        label = "".join(
            f'<tspan x="{cx}" dy="{0 if i == 0 else 12}">{html.escape(line)}</tspan>'
            for i, line in enumerate(lines)
        )
        label += f'<tspan class="rw-n" x="{cx}" dy="13">{completed} of {total} filled</tspan>'
        groups += (
            f'<g class="rw"><title>{html.escape(caption)}</title>'
            + wedge(angle, angle + sweep, radius, "#f2f5f5")
            + (
                wedge(angle, angle + sweep, radius * completed / total, _mit_module_colour(name))
                if completed
                else ""
            )
            + f'<text class="rw-l" x="{cx}" y="{cy - 6 * len(lines):.0f}">{label}</text>'
            + "</g>"
        )
        angle += sweep
    return (
        '<svg viewBox="0 0 174 174" preserveAspectRatio="xMidYMid meet" role="img" '
        'aria-label="MIT coverage by module: wedge angle is the module&#x27;s share of the '
        'checklist, radius is how much of it is filled">'
        f"{groups}</svg>"
    )


def _mit_rose_tile(mit: MITReport) -> str:
    """FAIR principle 1.3 — the domain (MIT) coverage tile: the aggregate score
    over the module rose. An unmeasured MITReport renders "not assessed",
    never 0% (#311)."""
    head = (
        '<div class="kpi-h"><span class="eyebrow">FAIR principle 1.3'
        '<a class="fn" href="#fn-mit">2</a></span></div>'
    )
    if not mit_was_assessed(mit):
        return (
            '<article class="kpi rose-tile">'
            + head
            + f'<div class="kpi-v"><b>&ndash;</b> {_mk("na")}</div>'
            '<div class="kpi-sub">not assessed</div>'
            "</article>"
        )
    pct = round(mit.overall_score * 100)
    completed, total = _mit_totals(mit)
    note = (
        '<div class="tile-note">NB: this score does not ensure data quality '
        "or assess whether the science is right. It does measure how completely the domain "
        "reporting fields are filled.</div>"
    )
    return (
        '<article class="kpi rose-tile">'
        + head
        + f'<div class="kpi-v"><b>{pct}</b><span class="den">%</span></div>'
        f'<div class="rose-wrap">{_mit_rose_svg(mit)}</div>'
        f'<div class="kpi-sub">{completed} of {total} MIT checklist fields filled</div>'
        f"{note}"
        "</article>"
    )


def _air_tile(air: AIRReport, *, wide: bool = False) -> str:
    """AI-readiness: coverage of the assessable criteria, over the seven dimensions.

    There is deliberately **no percentage headline**. The instrument's authors refuse
    an aggregate — *"We do not score it pass/fail overall"* — so one number here would
    be exactly the invented metric this axis replaced. The figure shown is coverage
    (met of assessed) and the bars are the profile; a dimension nothing could be
    assessed in is drawn hollow, not empty-at-zero, because "we did not look" and "the
    crate failed" are different claims.
    """
    if not air.criterion_results:
        # The criteria file could not be read. Every other axis still renders — but a
        # tile that quietly disappears reads as a report with four axes rather than
        # one whose fifth could not be scored, which is the exact confusion this
        # instrument's "not assessed" discipline exists to prevent.
        return (
            f'<article class="kpi{" wide" if wide else ""}">'
            '<div class="kpi-h"><span class="eyebrow">AI-readiness</span></div>'
            '<div class="kpi-v"><b class="air-na">not assessed</b></div>'
            '<div class="kpi-sub">the Bridge2AI criteria could not be read</div>'
            "</article>"
        )
    met = sum(1 for c in air.criterion_results if c.get("passed") is True)
    assessed = sum(1 for c in air.criterion_results if c.get("passed") is not None)
    total = len(air.criterion_results)
    bars = ""
    for dim in air.dimensions:
        pct = dim.get("pct")
        label = html.escape(str(dim.get("name") or ""))
        if pct is None:
            bars += f'<span class="airbar na" title="{label}: not assessed"></span>'
        else:
            bars += (
                f'<span class="airbar" title="{label}: {pct:g}% of '
                f'{dim.get("assessed")} assessed">'
                f'<i style="height:{max(pct, 3):g}%"></i></span>'
            )
    return (
        f'<article class="kpi{" wide" if wide else ""}">'
        '<div class="kpi-h"><span class="eyebrow">AI-readiness</span></div>'
        f'<div class="kpi-v"><b>{met}</b><span class="den">/ {assessed}</span> '
        '<span class="tag-inline">criteria met<a class="fn" href="#fn-air">3</a></span></div>'
        f'<div class="airbars" role="img" aria-label="{met} of {assessed} assessable '
        f'Bridge2AI criteria met across seven dimensions">{bars}</div>'
        f'<div class="kpi-sub">{total - assessed} of {total} not assessable from a crate</div>'
        "</article>"
    )


def _render_kpis(
    tiers: list[dict[str, str]] | None,
    val: ValidationReport | None,
    fair: FAIRReport,
    blockers: list[tuple[str, str, str]],
    mit: MITReport,
    air: AIRReport,
    residence: Mapping[str, int] | None,
    *,
    ceiling: dict[str, Any] | None = None,
    grid: dict[int, dict[str, Any]] | None = None,
    stale: bool = False,
) -> str:
    """The KPI grid: the profile × tier conformance matrix, FAIR maturity, the
    domain-coverage rose (spanning both rows), the graph tile (where the
    entities live, only when a graph was supplied) and the AI-readiness
    profile."""
    tiles = _profile_matrix_tile(val, tiers, stale)
    tiles += _fair_tile(fair, blockers, ceiling, grid)
    tiles += _mit_rose_tile(mit)
    if residence is not None:
        tiles += _graph_tile(residence)
    tiles += _air_tile(air, wide=residence is None)
    return f'<div class="kgrid">{tiles}</div>\n'


def _graph_tile(residence: Mapping[str, int]) -> str:
    """Where the crate's entities live — the explorer payload's residence tally
    (#720) — as one bar and its four figures.

    Orphans are the explorer legend's and the coverage blocks' to report; what
    no other figure states is *where* the entities are. A residence no entity
    has draws no segment and reads 0, so a crate with nothing looked up says so.
    """
    total = sum(residence.values())
    counts = [(key, residence.get(key, 0)) for key in RESIDENCES]
    segments = "".join(
        f'<i class="{key}" style="flex-grow:{n}" title="{n} {key}"></i>' for key, n in counts if n
    )
    keys = "".join(
        f'<span title="{RESIDENCES[key]}"><i class="{key}"></i><b>{n}</b> {key}</span>'
        for key, n in counts
    )
    described = ", ".join(f"{n} {key}" for key, n in counts)
    return (
        '<article class="kpi">'
        '<div class="kpi-h"><span class="eyebrow">Graph</span></div>'
        f'<div class="kpi-v"><b>{total}</b> <span class="tag-inline">entities</span></div>'
        f'<div class="res" role="img" aria-label="{total} entities: {described}">{segments}</div>'
        f'<div class="res-keys">{keys}</div>'
        "</article>"
    )


# The three profile layers in fix order: base must pass before ISA is
# meaningful, ISA before ISA-Tox (the validation-gate ordering contract). The
# adherence cards and the grouped improvement list both follow it.
_PROFILE_LAYERS: tuple[tuple[str, str], ...] = (
    ("base", "RO-Crate 1.2"),
    ("isa", "ISA"),
    ("tox", "ISA-Tox"),
)


# How many findings of each tier the suggestion list shows before it summarises
# the rest. REQUIRED is uncapped: those block conformance, so every one is named.
# With grouped rendering (#510) the advisory caps apply per profile group — the
# cap exists to bound the page, and a bound per group still bounds the page.
_SUGGESTION_CAPS: dict[str, int | None] = {
    "required": None,
    "recommended": 10,
    "optional": 5,
}

# Per-tier item templates, shared by the fold-outs and the flat fallback so the
# two renderings can never drift apart in vocabulary.
_TIER_TEMPLATES: dict[str, str] = {
    "required": '<li class="must"><strong>Must fix:</strong> {msg}</li>',
    "recommended": "<li>Recommended: {msg}</li>",
    "optional": '<li class="opt">Optional: {msg}</li>',
}
_TIER_RENDER_ORDER: tuple[str, ...] = ("required", "recommended", "optional")


def _capped_tier_items(tier: str, rendered: list[str]) -> list[str]:
    """Apply the tier's cap, putting the remainder behind a second fold-out.

    The cap bounds the page; it is not a decision about what the reader may see.
    It used to end at "+9 further recommended findings not listed here", which
    named a number and then offered no way to reach it — the crate's own report
    was the one place those findings existed, so a reader who wanted them had
    nowhere else to look.

    The overflow now sits in a nested ``<details>``: closed by default, so the
    page stays the length it was, and one click from complete.
    """
    cap = _SUGGESTION_CAPS.get(tier)
    shown = rendered if cap is None else rendered[:cap]
    rest = rendered[len(shown) :]
    if not rest:
        return shown
    noun = "finding" if len(rest) == 1 else "findings"
    return [
        *shown,
        '<li class="more">'
        f'<details class="more-fold"><summary>+{len(rest)} further {tier} {noun}</summary>'
        f'<ul class="sugg">{"".join(rest)}</ul>'
        "</details>"
        "</li>",
    ]


def _tier_findings_html(records: list[dict[str, str]], tier: str) -> str:
    """One severity tier's findings, grouped by profile layer (#510).

    Profile is the *secondary* axis: severity is the fix order (REQUIRED blocks
    the build, the advisory tiers do not), and only within a tier does layer
    order matter — base before ISA before ISA-Tox, per the validation-gate
    ordering contract. So each layer gets a subheading and its own list inside
    the tier's fold, rather than a second index restating the same counts. A
    finding the validator does not attribute to a layer lands under
    "unattributed" — reported, never dropped. The tier's cap applies per layer.
    """
    esc = html.escape
    labels = dict(_PROFILE_LAYERS)
    layer_rank = {key: i for i, (key, _) in enumerate(_PROFILE_LAYERS)}
    groups: dict[str, list[dict[str, str]]] = {}
    for record in records:
        groups.setdefault(str(record.get("profile") or ""), []).append(record)

    template = _TIER_TEMPLATES.get(tier, "<li>{msg}</li>")
    out: list[str] = []
    for key in sorted(groups, key=lambda k: (layer_rank.get(k, len(layer_rank)), k)):
        items = []
        for record in groups[key]:
            entity = str(record.get("entity_id") or "")
            chip = f"<code>{esc(entity)}</code> " if entity else ""
            items.append(template.format(msg=f"{chip}{esc(str(record.get('message') or ''))}"))
        out.append(
            f'<p class="sugg-prof-h">{esc(labels.get(key, key or "unattributed"))}'
            f'<span class="pc">{len(groups[key])}</span></p>'
            f'<ul class="sugg">{"".join(_capped_tier_items(tier, items))}</ul>'
        )
    return "".join(out)


def _tier_records(val: ValidationReport) -> dict[str, list[dict[str, str]]]:
    """The verdict's structured findings, bucketed by severity tier."""
    buckets: dict[str, list[dict[str, str]]] = {tier: [] for tier in _TIER_RENDER_ORDER}
    for record in val.issue_records:
        buckets.setdefault(str(record.get("severity") or ""), []).append(record)
    return buckets


# Which display list each tier's findings live in when the verdict carries no
# structured records for it (mirrors the write-back's own tier→field mapping).
_TIER_STRING_FIELDS: dict[str, str] = {
    "required": "required_issues",
    "recommended": "should_issues",
    "optional": "may_issues",
}


def _tier_body(val: ValidationReport, tier: str) -> tuple[str, int]:
    """One tier's findings as ``(html, count)`` — records first, strings second.

    The choice is made per tier, never once for the whole report: a verdict can
    legitimately hold records for one tier and only display strings for another
    (a pre-records checkpoint that then takes a REQUIRED-gate write-back).
    Deciding globally hid the string-only tiers' findings while the severity row
    went on counting them, so the report counted findings its own list omitted.

    Records carry the profile attribution and render grouped by layer; strings
    have none and render as one ungrouped list. Either way the count returned is
    the count of what the caller is about to show.
    """
    records = _tier_records(val).get(tier, [])
    if records:
        return _tier_findings_html(records, tier), len(records)
    field = _TIER_STRING_FIELDS.get(tier)
    issues: list[str] = getattr(val, field) if field else []
    if not issues:
        return "", 0
    template = _TIER_TEMPLATES.get(tier, "<li>{msg}</li>")
    items = [template.format(msg=html.escape(str(msg))) for msg in issues]
    return f'<ul class="sugg">{"".join(_capped_tier_items(tier, items))}</ul>', len(issues)


def _render_severity_detail(val: ValidationReport, tiers: list[dict[str, str]]) -> str:
    """The "By severity" block, each row unfolding its own findings (#510).

    The severity rows are the index: a row that has findings becomes a
    ``<details>`` whose summary is the row itself and whose body lists those
    findings grouped by profile. A row with nothing to show — clean, or a tier
    the sweep never assessed (#306) — stays a plain row with no fold, so a
    disclosure caret always means "there is something here".

    A row holding REQUIRED findings is born ``open``: a collapsed fold must
    never hide a blocking issue.
    """
    rows: list[str] = []
    for tier in tiers:
        body, count = _tier_body(val, tier["key"])
        summary = tier["summary"]
        # Where the summary IS a count of findings, it is the count of what the
        # row unfolds — never a number taken from a different list. REQUIRED is
        # left alone: its summary counts passing profiles, a different quantity,
        # and the fold answers "which findings" underneath it.
        if count and tier["key"] != "required":
            summary = _plural_issues(count)
        row = (
            f'{_mk(tier["state"])}<span class="st">{tier["tier"]}</span>'
            f'<span class="sc">{summary}</span><span class="sn">{tier["note"]}</span>'
        )
        if not count:
            rows.append(f'<div class="sev-drow">{row}</div>')
            continue
        rows.append(
            f'<details class="sev-fold"{" open" if tier["key"] == "required" else ""}>'
            f'<summary class="sev-drow">{row}</summary>'
            f'<div class="sev-body">{body}</div>'
            "</details>"
        )
    # A finding whose severity is none of the three tiers has no row of its own
    # to fold out of. The writers cannot produce one today, but this report does
    # not silently drop findings — it grows a row rather than losing them.
    for key, records in _tier_records(val).items():
        if key in _TIER_RENDER_ORDER or not records:
            continue
        rows.append(
            '<details class="sev-fold">'
            f'<summary class="sev-drow">{_mk("no")}'
            f'<span class="st">{html.escape(key.capitalize() or "Other")}</span>'
            f'<span class="sc">{_plural_issues(len(records))}</span>'
            '<span class="sn">Reported at a severity outside the three tiers.</span></summary>'
            f'<div class="sev-body">{_tier_findings_html(records, key)}</div>'
            "</details>"
        )
    return (
        '<div class="sev-detail"><span class="sev-detail-label">By severity</span>'
        f"{''.join(rows)}</div>"
    )


def _clean_note(val: ValidationReport) -> str:
    """The empty-state line, honest about how much was actually checked."""
    assessed = val.assessed_tiers
    if {"required", "recommended", "optional"} <= assessed:
        return "Clean at every severity tier — nothing outstanding to improve."
    unassessed = [t for t in ("recommended", "optional") if t not in assessed]
    if unassessed:
        return (
            "No outstanding REQUIRED issues. "
            f"The {' and '.join(unassessed).upper()} tier"
            f"{'s were' if len(unassessed) > 1 else ' was'} not assessed."
        )
    return "No outstanding REQUIRED issues."


def _payload_caveat(val: ValidationReport) -> str:
    """Name the one question a metadata-only verdict cannot answer (#530).

    Whether the crate contains the files it describes is REQUIRED by the base
    profile and answerable only where the payload exists. A verdict computed
    from the assembled document alone never looked, and the checks that would
    have asked emit nothing there — so its verdict is about the metadata, not
    about the crate.

    Rendered independently of whether the crate has findings: a verdict can be
    clean at REQUIRED, carry advisory findings, and still head the report with
    "Conformant" — so hanging this off the no-findings note would drop it in
    exactly the case where the green pill is doing the over-claiming. Export
    verifies the payload and clears it.
    """
    if val.payload_checked:
        return ""
    return (
        '<p class="lead payload-note">The crate\'s files were not checked against the '
        "metadata — this verdict covers the metadata document only, so it cannot say "
        "whether the crate contains the files it describes.</p>"
    )


def _backbone_caveat(val: ValidationReport) -> str:
    """Name the one question the ISA profile cannot answer of itself (#537).

    Whether the backbone reaches the structural entities the crate mints is
    REQUIRED, and unaskable by the profile: its rules target a class inferred
    from the very edge whose absence is the defect, so a detached process is
    skipped rather than failed and the silence reads as a pass. A verdict from
    the in-memory gate never asked. Export asks and clears it.

    Rendered on the same terms as :func:`_payload_caveat`, and for the same
    reason: a verdict clean at REQUIRED is exactly where the green pill
    over-claims hardest.
    """
    if val.isa_reachability_checked:
        return ""
    return (
        '<p class="lead payload-note">The crate\'s ISA backbone was not checked for '
        "detached entities — this verdict cannot say whether every process, protocol "
        "and sample it describes is connected to anything.</p>"
    )


def _render_profile_section(
    val: ValidationReport, tiers: list[dict[str, str]] | None, *, stale: bool = False
) -> str:
    esc = html.escape
    if stale:
        return (
            '<section id="adherence">\n'
            '  <div class="sec-h"><h2>Profile adherence</h2>'
            '<span class="sec-meta">out of date</span></div>\n'
            '  <p class="lead">The last recorded verdict was computed against an earlier '
            "version of this crate, so it is not reported here. Re-run validation to "
            "restore profile adherence.</p>\n"
            "</section>\n"
        )
    if tiers is None:
        return (
            '<section id="adherence">\n'
            '  <div class="sec-h"><h2>Profile adherence</h2></div>\n'
            '  <p class="lead">Not yet validated — run validation to populate profile '
            "adherence.</p>\n"
            "</section>\n"
        )

    # Cumulative, on the same terms as the matrix (`PROFILE_LAYER_CHAIN`): these
    # cards are the same conformance claim in a second place, and a card reading
    # "ISA-Tox met" beside a cell reading "ISA-Tox not met" is the report
    # contradicting itself.
    passed_by_layer = {"base": val.base_passed, "isa": val.isa_passed, "tox": val.tox_passed}
    cards = "".join(
        f'<div class="prof-card">'
        f"{_mk(_kind(all(passed_by_layer[layer] for layer in PROFILE_LAYER_CHAIN[key])))}"
        f"<span>{esc(name)}</span><em>REQUIRED</em></div>"
        for key, name in _PROFILE_LAYERS
    )
    severity_detail = _render_severity_detail(val, tiers)
    # Every finding lives in the severity row it belongs to, so the only thing
    # left to say underneath is that there were none — and that line stays
    # honest about how much of the crate was actually checked (#306).
    has_findings = any(_tier_body(val, tier["key"])[1] for tier in tiers) or any(
        records for key, records in _tier_records(val).items() if key not in _TIER_RENDER_ORDER
    )
    sugg = "" if has_findings else f'<p class="good-note">{_clean_note(val)}</p>'

    return (
        '<section id="adherence">\n'
        '  <div class="sec-h"><h2>Profile adherence</h2></div>\n'
        f'  <div class="prof-grid">{cards}</div>\n'
        f"  {severity_detail}\n"
        f"  {sugg}\n"
        f"  {_payload_caveat(val)}\n"
        f"  {_backbone_caveat(val)}\n"
        "</section>\n"
    )


def _mit_module_colour(name: str) -> str:
    return MIT_MODULE_STYLES.get(name, MIT_MODULE_FALLBACK_COLOUR)


def _mit_scope_note(mit: MITReport, scored: int) -> str:
    """"44 of 220 have no crate slot", when the two denominators differ.

    The percentage beside it is of what could be scored: a parameter carrying no
    ``crate_slot`` names nothing a crate field could hold, so it is outside every
    denominator here (`mit_assessment.iter_scorable_params`). Printing only our
    figure under this heading restates the instrument.
    A clause rather than a sentence because the section carries one lead and no
    more; the per-module split, which is the sharper number, rides on each bar's
    accessible name. Empty for a report serialised before these counts existed.
    """
    if not mit.published_total or mit.published_total <= scored:
        return ""
    return f" · {mit.published_total - scored} of {mit.published_total} have no crate slot"


def _render_mit_section(mit: MITReport) -> str:
    """The MIT coverage card — six module rows, each in its own colour —
    followed by a sibling "Per guidance document" card: one bar per guidance
    document split into those modules. Two adjacent ``<section>``s in one
    string; the second exists only when the report carries document buckets.

    Module rows and document bars speak one vocabulary — solid = filled, pale
    = still missing, the hue = the module — so the rows double as the key. A
    document's bar is a row of pills, one per module that contributes to it,
    in the scorer's (the checklist's) order, each sized by the module's share
    of the document (its field count as ``flex-grow``, so the gaps between
    pills come out of the row rather than out of any share); inside the pill
    the filled part is solid and the missing part pale, so every pill is that
    module's own progress bar and a wide pale pill is where the document's
    remaining gaps live. Every pill is one ``MITReport.standard_module_scores``
    bucket. The same numbers ride on the bar's accessible name
    (``aria-label``) and on each segment's tooltip.
    A document with no module split — a report serialised before the split
    existed — falls back to the plain single-colour bar rather than inventing
    one.
    """
    esc = html.escape
    # Nothing measured -> say so, and print no fraction and no percentage. The
    # old empty-scores branch still rendered "0/0 fields · 0%" in the section
    # header, which asserts a coverage figure just as loudly as the bar chart
    # underneath it would have (#311).
    if not mit_was_assessed(mit):
        return (
            "<section>\n"
            '  <div class="sec-h"><h2>Minimal information table for toxicological assays</h2>'
            f'<span class="sec-meta">{_mk("na")} not assessed</span></div>\n'
            '  <p class="lead">Coverage was not '
            "measured for this crate — the checklist could not be read, or the crate could "
            "not be assembled to score against. This is not a score of zero.</p>\n"
            "</section>\n"
        )
    completed_all, total_all = _mit_totals(mit)
    pct = round(mit.overall_score * 100)

    def mrow(name: str, bar: str, sc: dict[str, int], style: str = "", url: str = "") -> str:
        label = _lk(url, name) if url else esc(name)
        return (
            f'<div class="mrow"{style}><div class="mname">{label}</div>'
            f'<div class="mbar">{bar}</div>'
            f'<div class="mfrac">{sc.get("completed", 0)}'
            f'<span class="den">/{sc.get("total", 0)}</span></div></div>'
        )

    def plain_bar(
        sc: dict[str, int], meter_class: str, fill_class: str, extra: str = ""
    ) -> str:
        width = round(sc.get("completed", 0) / sc["total"] * 100) if sc.get("total") else 0
        return (
            f'<div class="{meter_class}" role="img" '
            f'aria-label="{sc.get("completed", 0)} of {sc.get("total", 0)}{esc(extra)}">'
            f'<i class="{fill_class}" style="width:{width}%"></i></div>'
        )

    def module_row(name: str, sc: dict[str, int]) -> str:
        # The bar is drawn over what could be scored; where that is less than the
        # module the checklist defines, its accessible name says so. Two modules'
        # bars are otherwise indistinguishable when one covers 98% of its module
        # and the other 17%.
        published = mit.published_total_for(name)
        scoped = ""
        if published > sc.get("total", 0):
            scoped = f'; {sc.get("total", 0)} of the checklist\'s {published} for this module'
        return mrow(
            name,
            plain_bar(sc, "meter mod", "fill-mod", extra=scoped),
            sc,
            style=f' style="--mod:{_mit_module_colour(name)}"',
        )

    def module_span(name: str, b: dict[str, int], doc_total: int) -> str:
        completed, total = b.get("completed", 0), b.get("total", 0)
        missing = total - completed
        segments = ""
        if completed:
            segments += (
                f'<i class="seg" style="width:{completed / total * 100:.2f}%" '
                f'title="{esc(f"{name}: {completed} of {total} filled")}"></i>'
            )
        if missing:
            segments += (
                f'<i class="seg pale" style="width:{missing / total * 100:.2f}%" '
                f'title="{esc(f"{name}: {missing} of {total} still missing")}"></i>'
            )
        # Share of the row by field count (flex-grow), not a percentage: the
        # pills are gapped, and a gap must come out of the row, not a share.
        return (
            f'<span class="mod" style="--mod:{_mit_module_colour(name)};'
            f'flex-grow:{total}">{segments}</span>'
        )

    def document_row(
        label: str, sc: dict[str, int], by_module: dict[str, dict[str, int]], url: str = ""
    ) -> str:
        doc_total = sc.get("total", 0)
        if not by_module or not doc_total:
            return mrow(label, plain_bar(sc, "meter", "fill-cov"), sc, url=url)
        # The scorer's module order (the checklist's), then anything the split
        # names that the module rows do not — kept, not dropped. A bucket with
        # nothing in it draws nothing and is not described either.
        order = [m for m in mit.module_scores if m in by_module]
        order += sorted(m for m in by_module if m not in mit.module_scores)
        order = [m for m in order if by_module[m].get("total", 0) > 0]
        spans = "".join(module_span(m, by_module[m], doc_total) for m in order)
        described = ", ".join(
            f"{m} {by_module[m].get('completed', 0)} of {by_module[m].get('total', 0)}"
            for m in order
        )
        bar = (
            f'<div class="meter stack" role="img" '
            f'aria-label="{sc.get("completed", 0)} of {doc_total}: {esc(described)}">'
            f"{spans}</div>"
        )
        return mrow(label, bar, sc, url=url)

    # Reached only when there ARE module scores — `mit_was_assessed` is exactly
    # "has module scores", so the old empty-scores fallback row is unreachable.
    rows = "".join(module_row(name, sc) for name, sc in mit.module_scores.items())
    section = (
        "<section>\n"
        '  <div class="sec-h"><h2>Minimal information table for toxicological assays</h2>'
        f'<span class="sec-meta"><b>{completed_all}/{total_all}</b> fields'
        f'{_mit_scope_note(mit, total_all)} · {pct}%</span></div>\n'
        '  <p class="lead">Each item is a FAIR maturity indicator as defined in '
        f'<a href="{MIT_INDICATORS_URL}">tox-maturity-indicators</a>.</p>\n'
        f'  <div class="mit">{rows}</div>\n'
        "</section>\n"
    )
    if mit.standard_scores:
        # Canonical order first (the YAML's own column order), then any key the
        # label map doesn't know — rendered raw rather than dropped.
        ordered = [k for k in MIT_STANDARD_LABELS if k in mit.standard_scores]
        ordered += sorted(k for k in mit.standard_scores if k not in MIT_STANDARD_LABELS)
        srows = "".join(
            document_row(
                MIT_STANDARD_LABELS.get(k, k),
                mit.standard_scores[k],
                mit.standard_module_scores.get(k) or {},
                url=MIT_STANDARD_SOURCES.get(k, ""),
            )
            for k in ordered
        )
        # No lead and no header aggregate: the bars explain themselves (the
        # module card directly above is the key), and rows overlap by design —
        # a document's numbers are its own, never a share of the checklist
        # total, so any sum in the header would double-count.
        section += (
            "<section>\n"
            '  <div class="sec-h"><h2>Per guidance document</h2></div>\n'
            f'  <div class="mit">{srows}</div>\n'
            "</section>\n"
        )
    return section


def _render_recommendations(
    val: ValidationReport | None,
    graph: dict[str, Any] | list[dict[str, Any]] | None,
    *,
    dsm: list[Any] | None = None,
    dsm_level: int = 0,
    stale: bool = False,
) -> str:
    """Recommendations: the findings collapsed into the actions that clear them.

    Each row is three parts, in reading order: the instrument's own words,
    verbatim, in a mono chip prefixed by the layer they came from; a badge; then
    the plain-language instruction in bold with one muted clause on why it
    matters (``remediation.why``). The rest of the report answers "what is
    wrong" one finding at a time — this section answers "what do I do", and says
    how many findings each action closes.

    **One shape for both instruments.** A DSM indicator blocking the next level
    arrives here as an ``Action`` like any other, so "the crate is not valid
    until you do X" and "the crate does not reach Level 2 until you do Y" are
    read the same way and ranked against each other. Its chip carries the
    published indicator text, its badge names the rung rather than borrowing a
    validator severity, and its instruction comes from the indicator's
    ``remedy`` in ``fair/dsm_indicators.yaml``. The section therefore renders
    for a crate with a clean validation run and an open maturity gap, which it
    previously declined to do.

    Deterministic and cheap like everything else here: the grouping is pure and
    the phrasing falls back to a template, so embedding the report in an export
    still costs no model call and no network. Renders nothing when there is
    nothing to act on — an empty exhortation is worse than silence.
    """
    from builder.tools.remediation import (
        _TIER_RANK,
        TIER_LABEL,
        describe,
        group_findings,
        group_orphans,
        why,
    )
    from builder.writers.provenance_dag import build_crate_graph

    dsm_actions = list(dsm or [])
    if (val is None or not _validation_has_signal(val)) and not dsm_actions:
        return ""
    # A stale verdict was recorded against a different crate, so prescribing its
    # findings would assert a diagnosis the page's own matrix refuses. The DSM rows are
    # not stale — they were measured from the graph in hand a moment ago — so they still
    # stand, and the notice says which half is missing.
    stale_note = (
        '  <p class="lead">The last recorded validation verdict was computed against an '
        "earlier version of this crate, so its findings are held back &mdash; re-run "
        "validation for those. The maturity rows below are current.</p>\n"
    )
    if stale and not dsm_actions:
        return (
            '<section id="next">\n'
            '  <div class="sec-h"><h2>Recommendations</h2></div>\n'
            '  <p class="lead">The last recorded verdict was computed against an earlier '
            "version of this crate &mdash; re-run validation to get current "
            "recommendations.</p>\n"
            "</section>\n"
        )
    issues = [dict(r) for r in (getattr(val, "issue_records", None) or [])]
    if val is None or stale:
        issues = []
    raw_nodes = graph.get("@graph", []) if isinstance(graph, dict) else (graph or [])
    labels = {
        str(n.get("@id")): str(n.get("name"))
        for n in raw_nodes
        if isinstance(n, dict) and isinstance(n.get("name"), str)
    }
    types = {
        str(n.get("@id")): n.get("@type")
        for n in raw_nodes
        if isinstance(n, dict) and n.get("@type") is not None
    }
    actions = group_findings(issues, labels=labels, types=types)
    if graph is not None and not stale:
        model = build_crate_graph(graph)
        orphans = [str(n["id"]) for n in model.get("nodes", []) if n.get("orphan")]
        actions += group_orphans(orphans, labels=labels, types=types)
    actions += dsm_actions
    live = [a for a in actions if a.actionable and a.cleared]
    if not live:
        return ""

    # Tier, then IMPACT, then size. Size last on purpose: "add a job title for 8
    # people" clears more findings than "say which measurement technique was
    # used", and ranking by count put the first above the second — which is
    # backwards for anyone who has to reuse the data.
    live.sort(key=lambda a: (_TIER_RANK.get(a.tier, 4), a.impact, -a.cleared, a.subject))
    esc = html.escape

    # The source layer the chip names, in the crate's own vocabulary.
    source_labels = {
        **dict(_PROFILE_LAYERS),
        "fair": "FAIR",
        "dsm": "DSM",
        "mit": "MIT",
        "air": "AI-readiness",
        "graph": "Graph",
    }

    def _row(action: Any) -> str:
        chip = ""
        if action.message:
            source = source_labels.get(action.source, action.source)
            # A DSM row names the indicator itself, linked to the model's own entry for
            # it: the chip carries the published wording, and the id is how a reader
            # checks that wording against the source.
            prefix = (
                f"{_dsm_lk(action.subject)} &middot; "
                if action.source == "dsm"
                else (f"{esc(source)} &middot; " if source else "")
            )
            chip = f'<code class="rec-chip">{prefix}{esc(action.message)}</code>'
        badge_class = {"REQUIRED": "req", "MATURITY": "lvl", "RECOMMENDED": "rec"}.get(
            action.tier, "opt"
        )
        # A maturity row names the rung it unblocks rather than a validator severity:
        # nothing in the DSM makes a crate invalid, and borrowing "Required" would say
        # it does.
        label = (
            f"Level {dsm_level}"
            if action.tier == "MATURITY" and dsm_level
            else TIER_LABEL.get(action.tier, action.tier.title())
        )
        badge = f'<span class="rec-badge {badge_class}">{esc(label)}</span>'

        reason = why(action)
        clause = f' <span class="rec-why">{esc(reason)}</span>' if reason else ""
        return (
            f'<li><span class="rec-n">{action.cleared}</span>'
            f'<span class="rec-b"><span class="rec-top">{chip}{badge}</span>'
            f'<span class="rec-body"><span class="rec-do">{esc(describe(action))}</span>'
            f"{clause}</span></span></li>"
        )

    rows = "".join(_row(a) for a in live[:_RECOMMENDATION_CAP])
    if len(live) > _RECOMMENDATION_CAP:
        rest = sum(a.cleared for a in live[_RECOMMENDATION_CAP:])
        # The cap bounds the page, not what the reader may see: the row names
        # how many findings it holds back and where they remain readable.
        more = len(live) - _RECOMMENDATION_CAP
        rows += (
            f'<li><span class="rec-n">{rest}</span><span class="rec-b">'
            f'<span class="rec-body">&hellip;and {more} further '
            f"{'action' if more == 1 else 'actions'} ({rest} "
            f"{'finding' if rest == 1 else 'findings'}), in "
            '<a href="#adherence">Profile adherence</a>.</span></span></li>'
        )
    return (
        '<section id="next">\n'
        '  <div class="sec-h"><h2>Recommendations</h2></div>\n'
        + (stale_note if stale else "")
        + '  <p class="lead">Each row is the instrument&rsquo;s own wording, what it counts '
        "against, then a plain-language instruction. A <b>Level</b> badge marks a FAIRplus "
        "DSM indicator standing between this crate and its next maturity level; the rest "
        "are profile conformance findings.</p>\n"
        f'  <ol class="recs">{rows}</ol>\n'
        "</section>\n"
    )


# The page stays a page. Everything past the cap is still reachable in the tier
# lists under Profile adherence, so nothing is hidden — only deferred.
_RECOMMENDATION_CAP = 8


def _render_references() -> str:
    """The numbered notes the page's superscripts point at.

    Note 1 names what the DSM ladder is scored against — the FAIRplus Dataset
    Maturity model's crate-assessable indicators (``fair/dsm_indicators.yaml``
    implements them), itself derived from the RDA FAIR Data Maturity Model.
    Note 2 names what the domain checklist is: the tox-maturity-indicators
    FAIR maturity indicators under principle R1.3. Note 3 names the AI-readiness
    instrument and, deliberately, that it is still a preprint and that its criterion
    text is quoted verbatim under a no-derivatives licence.
    """
    return (
        '<div class="refs">\n'
        '  <span class="refs-h">References</span>\n'
        '  <p id="fn-dsm"><span class="ref-n">1</span> FAIRplus Dataset Maturity (DSM) level, '
        "1&ndash;5 &mdash; a <b>derived</b> number: the published model computes a percentage "
        "grid (below) and no single level. Levels are gated here: every indicator of a level "
        "must pass before the next is reached, so a crate can meet most indicators and still "
        "sit at level 0, and the ladder tops out at 4 because Level 5 is scored entirely on "
        "hosting-environment and enterprise data-governance capability, which a crate cannot "
        "evidence about the environment that serves it. Scored against "
        "the crate-assessable indicators of the FAIRplus Dataset Maturity (DSM) model &mdash; "
        + _lk("https://fairplus.github.io/Data-Maturity/", "fairplus.github.io/Data-Maturity")
        + " &mdash; itself derived from the RDA FAIR Data Maturity Model, "
        + _lk("https://doi.org/10.15497/rda00050", "doi.org/10.15497/rda00050")
        + ".</p>\n"
        '  <p id="fn-mit"><span class="ref-n">2</span> The in-vitro toxicology Minimal '
        "Information Table (MIT): every item is a FAIR maturity indicator under principle "
        "R1.3 (domain-relevant community standards), as defined in "
        + _lk(MIT_INDICATORS_URL, "tox-maturity-indicators")
        + ".</p>\n"
        '  <p id="fn-air"><span class="ref-n">3</span> AI-readiness is scored against the NIH '
        "Bridge2AI &ldquo;AI-readiness Criteria for Biomedical Data&rdquo; &mdash; "
        + _lk("https://doi.org/10.1101/2024.10.23.619844", "doi.org/10.1101/2024.10.23.619844")
        + " (v6, 2026&ndash;04&ndash;24; <b>still a preprint</b>) &mdash; using the scoring "
        "model from the authors&rsquo; own self-evaluation worksheet, "
        + _lk("https://doi.org/10.5281/zenodo.13961091", "doi.org/10.5281/zenodo.13961091")
        + ". Criterion text is quoted verbatim under CC&nbsp;BY-ND&nbsp;4.0. It is a "
        "self-evaluation instrument, and its authors describe their own evaluation as "
        "subjective.</p>\n"
        "</div>\n"
    )


def _render_dsm_grid_section(
    grid: dict[int, dict[str, Any]], levels: dict[int, str], tool: str = ""
) -> str:
    """The DSM's own **"% Complete" grid** — the published instrument's only output.

    No formula in the assessment workbook computes an achieved maturity level; what it
    computes is this grid, six levels x {content, representation, hosting} plus a total.
    So this is the section a depositor can check: fill the sheet in by hand, or answer
    the online tool, and these percentages are the ones that come back. The heading's
    meta links that tool — ``tool`` is the YAML's ``source.assessment_tool``.

    **The headline number is the sheet's.** Its validation column is entirely formulas,
    so an unanswered indicator evaluates to 0 and counts against the score — the
    instrument has no "not assessed" state. Publishing only that number would report
    Level 0 as fully escaped on the strength of never having looked, so every cell also
    states how much of it we actually assessed. Where that reads ``0 of 4``, the
    percentage beside it is the sheet's arithmetic over four blanks, not a measurement.
   
    The workbook is a before/after instrument, and the deposit-as-received baseline is
    still captured at intake and carried in the session — but it is not drawn here. Two
    percentages per cell buried the one the grid exists to show, and a single summary
    line was no clearer; what the reader wants from this table is where the crate is.
    """
    if not grid:
        return ""
    meta = f'<span class="sec-meta">{_lk(tool, tool.split("//", 1)[-1])}</span>' if tool else ""
    # The published tool's own columns, in its own order and wording.
    cats = (("R", "Representation &amp; Format"), ("C", "Content &amp; Context"),
            ("H", "Hosting Environment Capabilities"), ("TOTAL", "Overall Level % Completion"))
    head = "".join(f"<th>{label}</th>" for _code, label in cats)
    rows = ""
    for level in sorted(grid):
        # Level 0 is not a rung and the published tool does not report it: its
        # statements are the pre-FAIRification condition in the negative, scored by
        # counting zeros, so an unanswered row reads 100% — "fully escaped", on the
        # strength of nobody having looked. The tool's own grid runs 1 to 5.
        if level < 1:
            continue
        cells = ""
        for code, _label in cats:
            cell = grid[level].get(code)
            if not cell or not cell.get("total"):
                cells += '<td class="dsm-na">—</td>'
                continue
            pct = cell.get("published_pct")
            if pct is None:
                cells += '<td class="dsm-na">—</td>'
                continue
            assessed, total = cell["assessed"], cell["total"]
            # The sheet's own number, unconditionally: an unanswered indicator
            # validates to 0 there, so it lowers the percentage rather than leaving it.
            # The coverage line beside it is what stops a low number reading as a
            # measured failure when nobody was asked.
            state = "full" if pct >= 100 else ("part" if pct > 0 else "none")
            if not assessed:
                state = "na"
            cells += (
                f'<td class="dsm-{state}"><span class="dsm-pct">{pct:g}%</span>'
                f'<span class="dsm-den">{assessed} of {total} assessed</span></td>'
            )
        rows += (
            f'<tr><th scope="row"><b>{level}</b> '
            f'<span class="dsm-lvl">{_lk(f"{_DSM_LEVEL_DOCS}{level}/", levels.get(level, ""))}'
            f"</span></th>{cells}</tr>"
        )
    return (
        "<section>\n"
        '  <div class="sec-h"><h2>FAIRplus Dataset Maturity Model</h2>'
        f"{meta}</div>\n"
        '  <div class="tbl-scroll"><table class="dsm-grid">\n'
        f"    <thead><tr><th>Level</th>{head}</tr></thead>\n"
        f"    <tbody>{rows}</tbody>\n"
        "  </table></div>\n"
        '  <p class="dsm-note">'
        "Percentages are the published sheet&rsquo;s own: satisfied "
        "&divide; the cell&rsquo;s denominator &times; 100. The sheet has no "
        "&ldquo;not assessed&rdquo; state &mdash; its validation column is all formulas, "
        "so a blank scores 0 &mdash; which is why each cell states how many of its "
        "indicators were actually assessed, and why a cell with none says so rather "
        "than publishing the number the sheet would compute over blanks. A cell&rsquo;s "
        "membership is the "
        "sheet&rsquo;s: higher levels carry lower ones forward, so a statement can be "
        "counted at more than one level. Hosting-environment indicators describe the "
        "environment serving the dataset, so a crate cannot evidence them &mdash; the "
        "published tool asks a person, and so does the checklist below.</p>\n"
        "</section>\n"
    )


def _render_dsm_levels(
    dsm_data: dict[str, Any], answers: dict[str, Any], levels: dict[int, str]
) -> str:
    """What each maturity level still needs, the way the published tool reports it.

    The FAIRplus tool's own output is a per-level checklist: *"Based on this assessment,
    9 indicators still need to be satisfied for your Datasets to reach Maturity Level
    1"*, followed by every indicator that level counts — the lower-level ones it carries
    forward included — each ticked or not, and each labelled with its identifier. That
    is the actionable half of the instrument, and reproducing it is what lets a reader
    check this report against a run of the tool itself.

    Two departures, both deliberate. The styling is ours. And the tool has two states
    because a person answers every question, while this one has three: an indicator no
    crate can evidence and nobody has answered is **not assessed**, which is neither met
    nor failed. Collapsing it into "still to satisfy" would assert a failure nothing
    measured; it is counted and named separately instead.

    A member listed twice is printed twice: the model's Level-4 hosting question carries
    ``DSM-4-H2`` on two rows and divides by three, and the published tool prints it twice
    as well. Silently deduplicating here would disagree with both.
    """
    spec = (dsm_data.get("scoring") or {}).get("grid") or []
    grid = {(c["level"], c["category"]): c for c in spec}
    text = {str(i.get("id")): str(i.get("text") or "") for i in dsm_data.get("indicators", [])}
    esc = html.escape

    blocks = ""
    for level in sorted({lvl for lvl, cat in grid if cat == "TOTAL" and lvl >= 1}):
        members = grid[(level, "TOTAL")]["members"]
        if not members:
            continue
        rows, todo, unknown = "", 0, 0
        for ident in members:
            verdict = answers.get(ident)
            value = None if verdict is None else verdict.value
            if value is True:
                state, mark = "met", "&#10003;"
            elif value is False:
                state, mark = "unmet", "&times;"
                todo += 1
            else:
                state, mark = "unknown", "?"
                todo += 1
                unknown += 1
            rows += (
                f'<li class="lvl-{state}"><span class="lvl-mark" aria-hidden="true">{mark}</span>'
                f'<code class="q-id">{_dsm_lk(ident)}</code>'
                f'<span class="q-txt">{esc(text.get(ident, ""))}</span></li>'
            )
        name = _lk(f"{_DSM_LEVEL_DOCS}{level}/", levels.get(level, ""))
        tail = (
            f' <span class="lvl-note">{unknown} of them not yet assessed</span>'
            if unknown
            else ""
        )
        blocks += (
            f'<div class="lvlblock"><h3><b>{todo}</b> indicator{"s" if todo != 1 else ""} '
            f"still to satisfy for Maturity Level {level} "
            f'<span class="lvl-name">{name}</span>{tail}</h3>'
            f'<ul class="lvllist">{rows}</ul></div>'
        )

    if not blocks:
        return ""
    return (
        '<section id="ladder">\n'
        '  <div class="sec-h"><h2>What each level still needs</h2>'
        '<span class="sec-meta">the assessment tool&rsquo;s own checklist</span></div>\n'
        '  <p class="lead">Each level counts the indicators below it as well as its own, '
        "so a statement can appear at more than one level. Every identifier links to the "
        "model&rsquo;s definition of it. A <b>?</b> marks an indicator no crate can "
        "evidence &mdash; the hosting environment, enterprise governance, and compliance "
        "with a Minimum Information Reporting Guideline &mdash; which the "
        "published tool puts to a person; answer those in a YAML file "
        "(<code>DSM-1-H1: true</code>, one per line) and pass it as "
        "<code>--dsm-answers</code>.</p>\n"
        f"  {blocks}\n"
        "</section>\n"
    )


def _render_air_section(air: AIRReport) -> str:
    """The Bridge2AI profile: seven dimensions, both denominators, then what failed.

    Two percentages per dimension, and the pair is the point. ``theirs`` is the
    published formula — met divided by every criterion in the dimension, exactly as
    the authors' worksheet computes it. ``ours`` divides by what a crate could
    actually be assessed on. Reporting only the second would quietly restate a
    32-author instrument as something it is not; reporting only the first would score
    a crate 0% on research ethics for having no way to show consent.

    Failing criteria are named in the instrument's own words, with the evidence behind
    each verdict — this is a self-evaluation instrument, and "why did it say no?" has
    to be answerable without reading the source.
    """
    if not air.criterion_results:
        return ""
    esc = html.escape
    rows = ""
    for dim in air.dimensions:
        pct, published = dim.get("pct"), dim.get("published_pct")
        ours = (
            '<span class="dsm-pct air-na">not assessed</span>'
            if pct is None
            else f'<span class="dsm-pct">{pct:g}%</span>'
        )
        rows += (
            f'<tr><th scope="row">{esc(str(dim.get("name") or ""))}</th>'
            f"<td>{ours}</td>"
            f'<td><span class="dsm-pct air-theirs">{published:g}%</span></td>'
            f'<td><span class="dsm-den">{dim.get("met")}/{dim.get("assessed")} '
            f'of {dim.get("total")}</span></td></tr>'
        )
    failing = [c for c in air.criterion_results if c.get("passed") is False]
    unmet = ""
    if failing:
        items = "".join(
            f'<li><code>{esc(str(c.get("id")))}</code> {esc(str(c.get("text", "")))}'
            + (
                f'<span class="blk-why">{esc(str(c.get("evidence")))}</span>'
                if c.get("evidence")
                else ""
            )
            + "</li>"
            for c in failing
        )
        unmet = (
            f'<details class="blockers"><summary><b>{len(failing)} '
            f'criteri{"a" if len(failing) != 1 else "on"}</b> assessed and not met'
            f'</summary><ul class="blk">{items}</ul></details>'
        )
    return (
        "<section>\n"
        '  <div class="sec-h"><h2>AI-readiness &mdash; the Bridge2AI profile</h2></div>\n'
        '  <div class="tbl-scroll"><table class="dsm-grid air-grid">\n'
        "    <thead><tr><th>Dimension</th><th>of assessed</th><th>published</th>"
        "<th>met</th></tr></thead>\n"
        f"    <tbody>{rows}</tbody>\n"
        "  </table></div>\n"
        f"  {unmet}\n"
        '  <p class="dsm-note">The authors report no aggregate score &mdash; '
        "&ldquo;we do not score it pass/fail overall&rdquo; &mdash; so neither does this. "
        "<b>Published</b> is their own formula: criteria met &divide; every criterion in "
        "the dimension &times; 100. <b>Of assessed</b> excludes criteria a crate cannot "
        "evidence &mdash; research ethics, repository governance, hosting and APIs "
        "&mdash; which is a documented deviation, since the published denominator has "
        "no &ldquo;not assessed&rdquo; state. Criteria are quoted verbatim under "
        "CC&nbsp;BY-ND&nbsp;4.0.</p>\n"
        "</section>\n"
    )


# Cap the actionable orphan/dangling lists so a pathological crate can't blow up
# the page; anything beyond is summarised as "+N more" (#310).
_TOPO_LIST_CAP = 10


_ISA_LEVEL_NOTE = {
    "Investigation": "the question the crate answers",
    "Study": "a coherent body of work toward it",
    "Assay": "one measurement campaign",
}


def _render_isa_panel(inv: dict[str, Any]) -> tuple[str, str]:
    """The ISA structure view: the Investigation / Study / Assay backbone.

    ISA is the skeleton the other views hang off, and it is expressed purely as
    ``hasPart`` between Datasets that differ only by ``additionalType`` — so it is
    invisible in the JSON and breaks in ways that still validate: a Study nobody
    lists as a part, an Assay with no process attached, a level with no
    ``identifier`` (which is what makes an ISA node citable at all).

    Returns:
        ``(panel html, tab badge)`` — ``("", "")`` when the crate has no ISA nodes.
    """
    from builder.writers.provenance_dag import ISA_COVERAGE_FIELDS

    nodes = inv["nodes"]
    if not nodes:
        return "", ""
    counts = inv["counts"]
    detached = counts["detached"]
    pct = (
        round(counts["fields_met"] / counts["fields_total"] * 100) if counts["fields_total"] else 0
    )


    notes = []
    if detached:
        loose = [n["label"] for n in nodes if n["state"] == "detached"]
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{detached} ISA container'
            f"{'' if detached == 1 else 's'} sit outside the hierarchy</b> "
            f"({', '.join(loose[:4])}{'&hellip;' if len(loose) > 4 else ''}). "
            "Nothing lists them under <code>hasPart</code>, so a reader walking the "
            "Investigation never reaches them.</span></p>"
        )
    hollow = [n for n in nodes if n["fields"]["Contains the next level"] is False]
    if hollow:
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{len(hollow)} container'
            f"{'' if len(hollow) == 1 else 's'} contain nothing below them</b> "
            f"({', '.join(n['label'] for n in hollow[:4])}). An Assay needs a "
            "<code>LabProcess</code>, a Study an Assay, an Investigation a Study — "
            "otherwise the level is a label with no work under it.</span></p>"
        )
    if not notes:
        notes.append(
            '<p class="good-note">The Investigation / Study / Assay hierarchy is complete.</p>'
        )

    head = "".join(
        f'<th scope="col" title="{html.escape(full)}">{html.escape(short)}</th>'
        for full, short in ISA_COVERAGE_FIELDS
    )
    rows = []
    for n in sorted(nodes, key=lambda n: (n["state"] == "linked", n["met"], n["id"])):
        flag = (
            ""
            if n["state"] == "linked"
            else '<span class="chem-flag" title="nothing lists this container under '
            'hasPart">detached</span>'
        )
        extra = (
            f'<span class="ty">{len(n["processes"])} process'
            f"{'' if len(n['processes']) == 1 else 'es'}</span>"
            if n["level"] == "Assay"
            else ""
        )
        cells = "".join(
            f"<td>{_mk(_kind(n['fields'].get(full)))}</td>" for full, _short in ISA_COVERAGE_FIELDS
        )
        rows.append(
            f'<tr><th scope="row">{_mk("ok" if n["state"] == "linked" else "no")}'
            f'<span class="cn">{n["label"]}</span>'
            f'<span class="ty" title="{html.escape(_ISA_LEVEL_NOTE[n["level"]])}">'
            f"{n['level']}</span>{extra}{flag}</th>{cells}</tr>"
        )
    matrix = (
        '<div class="chem-tbl-scroll"><table class="chem-tbl">'
        '<caption class="sr-only">Structural fields carried by each ISA container</caption>'
        f'<thead><tr><th scope="col">Container</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )

    panel = (
        '<p class="cov-cap">The ISA backbone every other view hangs off — the '
        "Investigation that states the question, the Studies under it, and the Assays "
        f"whose processes the LabProcesses view traces. <b>{counts['investigations']}</b> "
        f"investigation · <b>{counts['studies']}</b> stud"
        f"{'y' if counts['studies'] == 1 else 'ies'} · <b>{counts['assays']}</b> assay"
        f"{'' if counts['assays'] == 1 else 's'} · <b>{counts['processes']}</b> process"
        f"{'' if counts['processes'] == 1 else 'es'} · <b>{pct}%</b> complete.</p>\n"
        f"  {''.join(notes)}\n"
        f"  {matrix}"
    )
    # The badge counts the assays alone (review comment) — the view is named
    # Assays, and badging every ISA container overstated it.
    return panel, str(counts["assays"])


# The coverage blocks, in reading order. The names and their order are the
# owner's, reviewed on the report artifact (#607); they were tab labels while
# each block led with a diagram, and are headings now that the diagrams live in
# the entity explorer (#618).
_COVERAGE_BLOCKS: tuple[tuple[str, str], ...] = (
    # Files before Assays: the review's "flip dataset and assays".
    ("cov-data", "Files"),
    ("cov-isa", "Assays"),
    ("cov-chem", "Chemicals"),
    ("cov-cell", "Biological models"),
    ("cov-people", "Persons &amp; Organisations"),
    # Last, and next to People: the two answer the same kind of question about
    # credit, and a reader who has just checked who the crate credits is the one
    # who wants to know whether the papers it cites credit anybody either.
    ("cov-cite", "Citations"),
)


def _render_entity_coverage_section(
    graph: dict[str, Any] | list[dict[str, Any]], chem_inv: dict[str, Any]
) -> str:
    """How completely the crate identifies the things it describes.

    A fold under the entity explorer rather than a section of its own: it is an
    inventory of the same entities the explorer draws, it answers the same
    question about them from the other side, and as a section it put a
    six-block contents list between the reader and the rest of the report.

    One block per kind of entity, each asking the question that kind fails at:
    can this compound be *obtained* (CAS / PubChem CID / DTXSID plus structure),
    is this cell line *pinned down* (a Cellosaurus RRID names one stock where a
    name names a family), does this citation *resolve*, is this person someone a
    registry can find. These are completeness verdicts, not pictures, and no
    diagram answers them.

    Until #618 each block opened with a diagram of the same inventory — "can a
    reader get from a process to this compound" — and the section was tabbed so
    six diagrams would not stack. The entity explorer answers that question
    better and interactively, so the diagrams went and the tabs went with them:
    what is left per block is a note and a matrix, and the report stacks its
    sections.

    A block with nothing to report is omitted rather than shown empty, the way
    its tab used to disappear. Called only when a crate ``@graph`` is supplied.
    """
    from builder.writers.provenance_dag import (
        build_cellline_inventory,
        build_citation_inventory,
        build_crate_graph,
        build_isa_inventory,
        build_people_inventory,
    )

    model = build_crate_graph(graph, all_edges=True)
    blocks = {
        "cov-isa": _render_isa_panel(build_isa_inventory(graph)),
        "cov-data": _render_datasets_panel(graph, model),
        "cov-chem": _render_chemicals_panel(chem_inv),
        "cov-cell": _render_celllines_panel(build_cellline_inventory(graph)),
        "cov-people": _render_people_panel(build_people_inventory(graph)),
        "cov-cite": _render_citations_panel(build_citation_inventory(graph)),
    }
    live = [(bid, label) for bid, label in _COVERAGE_BLOCKS if blocks[bid][0]]
    if not live:
        return ""

    # One fold per block (#629). The section is an inventory of a whole crate —
    # on a real deposit the Files block alone lists 59 files — so left open it
    # sits between the reader and everything below it. Closed, the section reads
    # as a contents list: a name, a count, and a place to look. The count rides
    # in the summary because it is the whole value of a block nobody opens, and
    # `@media print` forces every fold open so a printed copy keeps its
    # inventory. The Files block's own per-Dataset folds nest unchanged.
    bodies = "".join(
        f'<details class="cov" id="{bid}"><summary class="cov-h">{label}'
        + (f'<span class="cov-n">{blocks[bid][1]}</span>' if blocks[bid][1] else "")
        + f'</summary><div class="cov-body">{blocks[bid][0]}</div></details>'
        for bid, label in live
    )
    total = sum(int(blocks[bid][1] or 0) for bid, _label in live)
    return (
        '<details class="cov-all"><summary class="cov-h cov-all-h">Entity coverage'
        f'<span class="cov-n">{total}</span></summary>'
        f'<div class="cov-all-body">{bodies}</div></details>'
    )


def _render_datasets_panel(
    graph: dict[str, Any] | list[dict[str, Any]], model: dict[str, Any]
) -> tuple[str, str]:
    """The Datasets view: every ``Dataset`` the crate declares, each unfolded
    to the files it lists under ``hasPart`` — what kind of file, its format and
    size, whether it is described, and whether a reader walking from the root
    actually reaches it.

    One fold per Dataset, in ISA backbone order (Investigation, Study, Assay,
    then plain folder Datasets), named by its level and ``name``. A file sits
    under *every* Dataset whose ``hasPart`` names it — the root lists the whole
    tree and an Assay lists its own, and both are the crate's claims — so the
    view reports the structure as written rather than inventing one owner. A
    Dataset that lists only containers is still shown, with "0 files", so an
    empty Assay is visible rather than silently absent. Files no Dataset lists
    lead, in their own group: they are the rows worth acting on.

    Rows are the crate graph's own ``data``-category nodes (the same
    classification every other view uses, #487), so the folds and the
    All-entities map cannot disagree about what counts as data. Within a
    fold, unreachable rows sort first.

    Returns ``("", "")`` when the crate declares no data entities.
    """
    esc = html.escape
    nodes = _raw_nodes(graph)
    rows_src = [n for n in model.get("nodes", []) if n.get("category") == "data"]
    if not rows_src:
        return "", ""

    def fact(nid: str, *keys: str) -> str:
        node = nodes.get(str(nid)) or {}
        for key in keys:
            value = node.get(key)
            if isinstance(value, list):
                value = value[0] if value else None
            if isinstance(value, dict):
                value = value.get("@value")
            if value not in (None, "", []):
                return str(value)
        return ""

    def size_words(raw: str) -> str:
        try:
            n = int(float(raw))
        except (TypeError, ValueError):
            return esc(raw)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:,} {unit}" if unit == "B" else f"{n:,.0f} {unit}"
            n //= 1024
        return esc(raw)

    def row(n: dict[str, Any]) -> str:
        nid = str(n.get("id"))
        name = str(n.get("label") or nid)  # label is pre-escaped by the model
        fmt = fact(nid, "encodingFormat", "format")
        size = fact(nid, "contentSize")
        described = bool(fact(nid, "description"))
        reachable = not n.get("orphan")
        # The muted span states the file's PATH — its @id in the crate, which
        # is where a reader finds it — not a kind word (review comment). A
        # file whose display name already is its path gets no duplicate span.
        path = f'<span class="ty">{esc(nid)}</span>' if name != esc(nid) else ""
        return (
            f'<tr><th scope="row">{_mk("ok" if reachable else "no")}'
            f'<span class="cn">{name}</span>{path}</th>'
            f"<td>{esc(fmt) if fmt else _mk('no')}</td>"
            f"<td>{size_words(size) if size else _mk('no')}</td>"
            f"<td>{_mk('ok' if described else 'no')}</td>"
            f"<td>{_mk('ok' if reachable else 'no')}</td></tr>"
        )

    def table(members: list[dict[str, Any]], caption: str) -> str:
        if not members:
            return ""
        ordered = sorted(
            members, key=lambda n: (not n.get("orphan"), str(n.get("label") or "").casefold())
        )
        return (
            '<div class="chem-tbl-scroll"><table class="chem-tbl">'
            f'<caption class="sr-only">{caption}</caption>'
            '<thead><tr><th scope="col">Data entity</th><th scope="col">Format</th>'
            '<th scope="col">Size</th><th scope="col" title="Has a description">Described</th>'
            '<th scope="col" title="Reachable from the crate root">Reachable</th></tr></thead>'
            f"<tbody>{''.join(row(n) for n in ordered)}</tbody></table></div>"
        )

    def fold(level: str, name: str, members: list[dict[str, Any]], *, title: str = "") -> str:
        n = len(members)
        count = f"{n} file{'s' if n != 1 else ''}"
        chip = f'<span class="ds-lvl">{level}</span> ' if level else ""
        return (
            f'<details class="ds-fold" open><summary{title}>'
            f'{chip}<b>{name}</b> <span class="ds-n">{count}</span></summary>'
            f"{table(members, f'Files listed by {name}')}</details>"
        )

    # The Datasets, in backbone order: the ISA inventory names the level of
    # each container (the root is the Investigation whether it says so or not);
    # any other Dataset is a plain folder and sorts after them.
    from builder.writers.provenance_dag import build_isa_inventory

    levels = {str(c["id"]): str(c["level"]) for c in build_isa_inventory(graph)["nodes"]}
    rank = {"Investigation": 0, "Study": 1, "Assay": 2}
    data_by_id = {str(n.get("id")): n for n in rows_src}
    datasets = sorted(
        ((nid, node) for nid, node in nodes.items() if "Dataset" in _type_set(node)),
        key=lambda kv: (
            rank.get(levels.get(kv[0], ""), 3),
            str(kv[1].get("name") or kv[0]).casefold(),
        ),
    )
    listed: set[str] = set()
    folds: list[str] = []
    for nid, node in datasets:
        members = [data_by_id[m] for m in _ref_ids(node, "hasPart") if m in data_by_id]
        listed.update(str(m.get("id")) for m in members)
        folds.append(
            fold(
                levels.get(nid, "Dataset"),
                esc(str(node.get("name") or nid)),
                members,
                title=f' title="{esc(nid)}"',
            )
        )
    unlisted = [n for n in rows_src if str(n.get("id")) not in listed]
    if unlisted:
        folds.insert(0, fold("", "Not listed by any Dataset", unlisted))
    unreached = sum(1 for n in rows_src if n.get("orphan"))
    note = (
        f'<p class="chem-warn">{_mk("no")}<span><b>{unreached} of {len(rows_src)} data '
        "entities cannot be reached from the crate root.</b> A reader walking the crate "
        "never arrives at them — link each from the Dataset or process that owns it."
        "</span></p>"
        if unreached
        else '<p class="good-note">Every data entity is reachable from the crate root.</p>'
    )
    return f"{note}\n  {''.join(folds)}", str(len(rows_src))


_CHEM_STATE_MARK = {"wired": "ok", "mentioned": "na", "unlinked": "no"}
_CHEM_STATE_NOTE = {
    "wired": "reachable from a process",
    "mentioned": "named in the crate, but produced by no process",
    "unlinked": "nothing in the crate references this compound",
}


def _render_chemicals_panel(inv: dict[str, Any]) -> tuple[str, str]:
    """The Chemicals view: how each compound reaches the experiment, and how
    completely it is identified.

    Two views of the same inventory, because either alone misleads. The diagram
    answers *can a reader get from a process to this compound* — the chain ISA
    forces to run through the condition table rather than the process object, and
    the one that quietly breaks. The matrix answers *could a reader obtain this
    substance* — CAS / PubChem CID / DTXSID plus the structure fields. A crate can
    score perfectly on one and fail the other, so the section never collapses them
    into a single number.

    Returns ``("", "")`` when the crate declares no compounds — the view drops
    its tab entirely, because an empty chemicals panel on a non-chemical crate
    would read as a failure rather than as "not applicable".

    Returns:
        ``(panel html, tab badge)``.
    """
    from builder.writers.provenance_dag import (
        CHEM_COVERAGE_FIELDS,
        chem_source_url,
    )

    chems = inv["chemicals"]
    if not chems:
        return "", ""
    counts = inv["counts"]
    total, wired = counts["total"], counts["wired"]

    unreached = total - wired
    if unreached:
        route_note = (
            f'<p class="chem-warn">{_mk("no")}<span><b>{unreached} of {total} compounds '
            "cannot be reached from any process.</b> ISA forbids a MolecularEntity as a "
            "LabProcess <code>object</code>, so a compound is linked <em>through</em> the "
            "Exposure&rsquo;s condition table — give the table an <code>about</code> "
            "pointing at the compound (and the <code>compound</code> column a "
            "<code>valueUrl</code>), or the substance stays described but unused.</span></p>"
        )
    else:
        route_note = (
            '<p class="good-note">Every compound is reachable from the process that used it.</p>'
        )

    # Per-compound identification matrix. Unwired first, then worst-covered, so
    # the rows that survive the cap are the ones worth acting on.
    ordered = sorted(
        chems, key=lambda c: (c["state"] == "wired", c["met"], c["name"].casefold(), c["id"])
    )
    head = "".join(
        f'<th scope="col" title="{html.escape(full)}">{html.escape(short)}</th>'
        for full, short in CHEM_COVERAGE_FIELDS
    )
    # Every compound is listed, matching the diagram: this is a metadata-checking
    # view, and a truncated tail hides the rows worth acting on.
    rows = []
    for c in ordered:
        # A compound whose @id is a web page links its name there. Only http(s):
        # the @id is crate text, and any other scheme (javascript:, data:) must
        # not reach the page as an href.
        name = (
            f'<a class="ext" href="{html.escape(c["id"])}">{c["label"]}</a>'
            if str(c["id"]).startswith(("http://", "https://"))
            else c["label"]
        )
        flag = (
            ""
            if c["state"] == "wired"
            else f'<span class="chem-flag" title="{html.escape(_CHEM_STATE_NOTE[c["state"]])}">'
            f"{'not linked' if c['state'] == 'unlinked' else 'no process'}</span>"
        )
        # Every ✓ that has a public source behind it is a link to that source.
        values = {**c["identifiers"], **c.get("structure", {})}
        cells = ""
        for full, _short in CHEM_COVERAGE_FIELDS:
            if not c["fields"].get(full):
                cells += f"<td>{_mk('no')}</td>"
                continue
            url = chem_source_url(full, values.get(full))
            if url:
                cells += (
                    f'<td><a class="ext" href="{html.escape(url)}" '
                    f'title="{html.escape(f"{full} {values[full]} — open the source")}">'
                    f"{_mk('ok')}</a></td>"
                )
            else:
                cells += f"<td>{_mk('ok')}</td>"
        rows.append(
            f'<tr><th scope="row">{_mk(_CHEM_STATE_MARK[c["state"]])}'
            f'<span class="cn">{name}</span>{flag}</th>{cells}</tr>'
        )
    matrix = (
        '<div class="chem-tbl-scroll"><table class="chem-tbl">'
        f'<caption class="sr-only">Identification fields carried by each compound</caption>'
        f'<thead><tr><th scope="col">Compound</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )

    # Compounds only (#506) — the legend defines the two states a compound node
    # can be in, not the route shapes this view no longer draws.

    # No caption: the KPI tile already carries the wired / identified figures,
    # and the diagram + matrix say the rest (the owner's call, #606).
    panel = f"{route_note}\n  {matrix}"
    return panel, str(total)


# An organisation reached through a person's affiliation is CORRECTLY linked —
# that is the normal shape — so it reads as met, with a muted chip naming the
# route. Only "unattached" is a defect.
_CELL_STATE_NOTE = {
    "wired": "consumed by a process",
    "mentioned": "named in the crate, but consumed by no process",
    "unlinked": "nothing in the crate references this biological sample",
}


def _render_celllines_panel(inv: dict[str, Any]) -> tuple[str, str]:
    """The Biological models view: the test system, and whether it is pinned down.

    The reader-facing name is the owner's (review comment on the report
    artifact); the entities are cell lines, and the wording keeps
    "cell line" only where it names the declaration being checked.
    The same two questions the compound view asks, because a model fails the
    same two ways. It is *unreachable* when the ``CellCulture`` consumes a freshly
    minted generic ``Sample`` instead of the declared ``CellLineSample`` — the
    line is then described and used by nothing. It is *unidentified* when it
    carries a name but no Cellosaurus RRID: "CHO-K1" names a family of divergent
    stocks, ``CVCL_0214`` names one, and organ / tissue / passage are what let
    another lab reproduce the culture rather than merely recognise it.

    Returns:
        ``(panel html, tab badge)`` — ``("", "")`` when the crate declares none.
    """
    from builder.writers.provenance_dag import CELLLINE_COVERAGE_FIELDS

    lines = inv["celllines"]
    if not lines:
        return "", ""
    counts = inv["counts"]
    total, wired = counts["total"], counts["wired"]
    rrid_backed = sum(1 for c in lines if c["rrid"])


    notes = []
    unreached = total - wired
    if unreached:
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{unreached} of {total} biological '
            "samples are not consumed by any process.</b> The <code>CellCulture</code> should "
            "take the declared sample as its <code>input</code> — when it takes a freshly "
            "minted generic <code>Sample</code> instead, the declared one is described in the "
            "crate and used by nothing.</span></p>"
        )
    if total - rrid_backed:
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{total - rrid_backed} of {total} '
            "biological samples carry no Cellosaurus RRID.</b> A name identifies a family of "
            "divergent stocks; <code>CVCL_…</code> identifies the one that was used.</span></p>"
        )
    if not notes:
        notes.append(
            '<p class="good-note">Every biological sample is consumed by a process and '
            "RRID-backed.</p>"
        )

    ordered = sorted(
        lines, key=lambda c: (c["state"] == "wired", c["met"], c["name"].casefold(), c["id"])
    )
    head = "".join(
        f'<th scope="col" title="{html.escape(full)}">{html.escape(short)}</th>'
        for full, short in CELLLINE_COVERAGE_FIELDS
    )
    rows = []
    for c in ordered:
        link = " 🔗" if c["resolvable"] else ""
        rrid = f'<span class="ty">{html.escape(c["rrid"])}</span>' if c["rrid"] else ""
        flag = (
            ""
            if c["state"] == "wired"
            else f'<span class="chem-flag" title="{html.escape(_CELL_STATE_NOTE[c["state"]])}">'
            f"{'not linked' if c['state'] == 'unlinked' else 'no process'}</span>"
        )
        cells = "".join(
            f"<td>{_mk('ok' if c['fields'].get(full) else 'no')}</td>"
            for full, _short in CELLLINE_COVERAGE_FIELDS
        )
        rows.append(
            f'<tr><th scope="row">{_mk(_CHEM_STATE_MARK[c["state"]])}'
            f'<span class="cn">{c["label"]}{link}</span>{rrid}{flag}</th>{cells}</tr>'
        )
    matrix = (
        '<div class="chem-tbl-scroll"><table class="chem-tbl">'
        '<caption class="sr-only">Identification fields carried by each biological sample'
        "</caption>"
        f'<thead><tr><th scope="col">Biological model</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )

    # No summary caption (review comment): the panel opens with the diagram.
    # The total shows in the tab badge; the warnings call out the gaps.
    panel = f"{''.join(notes)}\n  {matrix}"
    return panel, str(total)


_AGENT_STATE_MARK = {"credited": "ok", "affiliated": "ok", "unattached": "no"}
_AGENT_STATE_NOTE = {
    "credited": "credited directly by an entity in the crate",
    "affiliated": "linked through a person's affiliation",
    "unattached": "nothing in the crate references this agent",
}
_AGENT_STATE_CHIP = {"affiliated": ("muted", "via affiliation"), "unattached": ("", "unattached")}


def _render_people_panel(inv: dict[str, Any]) -> tuple[str, str]:
    """The People & organisations view: who the crate credits, how resolvably.

    Attribution is where a crate quietly stops being machine-actionable. A bare
    ``name`` satisfies every profile while crediting nobody a registry can
    resolve, and the diagram makes the two failure shapes visible: a person with
    no ``affiliation`` (the institution behind the work is unrecorded) and an
    agent nothing references at all — which is what a duplicated institution
    looks like, one copy ROR-backed and carrying edges, the other locally minted
    and carrying none.

    Returns ``("", "")`` when the crate names no people or organisations.

    Returns:
        ``(panel html, tab badge)``.
    """
    from builder.writers.provenance_dag import AGENT_COVERAGE_FIELDS

    agents = inv["agents"]
    if not agents:
        return "", ""
    counts = inv["counts"]
    total, pid_backed, unattached = counts["total"], counts["pid_backed"], counts["unattached"]


    notes = []
    if unattached:
        loose = [a["label"] for a in agents if a["state"] == "unattached"]
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{unattached} of {total} agents are '
            "referenced by nothing in the crate</b> "
            f"({', '.join(loose[:4])}{'…' if len(loose) > 4 else ''}). An agent no entity "
            "credits is usually a duplicate of one that is — the same institution minted "
            "twice, once with its ROR and once locally. Point the crediting entity at the "
            "identifier-backed copy and drop the other.</span></p>"
        )
    missing_pid = total - pid_backed
    if missing_pid:
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{missing_pid} of {total} agents carry '
            "no persistent identifier.</b> Give each person an ORCID and each organisation a "
            "ROR — a name string credits nobody a machine can resolve.</span></p>"
        )
    if not notes:
        notes.append('<p class="good-note">Every agent is credited and identifier-backed.</p>')

    ordered = sorted(
        agents, key=lambda a: (a["state"] == "credited", a["met"], a["name"].casefold(), a["id"])
    )
    head = "".join(
        f'<th scope="col" title="{html.escape(full)}">{html.escape(short)}</th>'
        for full, short in AGENT_COVERAGE_FIELDS
    )
    # Every agent is listed — no cap. This view exists so a person can CHECK the
    # attribution entity by entity, and a truncated tail is exactly where a
    # duplicated institution or a missing-ORCID author would hide.
    rows = []
    for a in ordered:
        link = " 🔗" if a["resolvable"] else ""
        chip = _AGENT_STATE_CHIP.get(a["state"])
        flag = (
            ""
            if chip is None
            else f'<span class="chem-flag{" " + chip[0] if chip[0] else ""}" '
            f'title="{html.escape(_AGENT_STATE_NOTE[a["state"]])}">{chip[1]}</span>'
        )
        kind = "Person" if a["kind"] == "person" else "Organisation"
        cells = "".join(
            f"<td>{_mk(_kind(a['fields'].get(full)))}</td>"
            for full, _short in AGENT_COVERAGE_FIELDS
        )
        rows.append(
            f'<tr><th scope="row">{_mk(_AGENT_STATE_MARK[a["state"]])}'
            f'<span class="cn">{a["label"]}{link}</span>'
            f'<span class="ty">{kind}</span>{flag}</th>{cells}</tr>'
        )
    matrix = (
        '<div class="chem-tbl-scroll"><table class="chem-tbl">'
        '<caption class="sr-only">Attribution fields carried by each agent</caption>'
        f'<thead><tr><th scope="col">Agent</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )

    panel = (
        '<p class="cov-cap">Who the crate credits, and whether that credit resolves — '
        f"an ORCID for a person, a ROR for an institution. <b>{counts['people']}</b> "
        f"{'person' if counts['people'] == 1 else 'people'} · <b>{counts['orgs']}</b> "
        f"organisation{'' if counts['orgs'] == 1 else 's'} · <b>{pid_backed}</b> "
        "identifier-backed.</p>\n"
        f"  {''.join(notes)}\n"
        f"  {matrix}"
    )
    return panel, str(total)


_CITE_STATE_NOTE = {
    "cited": "something in the crate points at this article",
    "uncited": "nothing in the crate cites this article",
}


def _render_citations_panel(inv: dict[str, Any]) -> tuple[str, str]:
    """The Citations view: the literature the crate stands on, and whether the
    reference goes anywhere.

    The same two questions the other views ask, because a citation fails the same
    two ways. It is *unreachable* when nothing points at it — the Root Data
    Entity's ``citation`` is what puts a paper into the deposit's record, and an
    article no entity cites is described in the crate and referenced by nothing.
    It is *unidentified* one hop further out than a compound is, because a
    citation refers to two things: the work, which needs a DOI (a title names a
    paper, ``10.…`` retrieves it), and the people who wrote it, which have to be
    entities the crate actually contains. An ``author`` ``@id`` no node answers to
    — the ``#CitationAuthor_…`` stub minted for a Crossref author with no ORCID —
    leaves an article that looks fully attributed in the JSON and credits nobody
    (#532).

    Returns:
        ``(panel html, tab badge)`` — ``("", "")`` when the crate declares none.
    """
    from builder.writers.provenance_dag import CITATION_COVERAGE_FIELDS

    articles = inv["articles"]
    if not articles:
        return "", ""
    counts = inv["counts"]
    total, cited, doi_backed = counts["total"], counts["cited"], counts["doi_backed"]
    broken = counts["unresolved_authors"]
    # An article with an EMPTY credit list has no unresolved reference, so `broken`
    # is 0 and every warning below stays silent — the green note then reports that
    # "every author resolves" about a paper that credits nobody. Vacuous truth is
    # the one thing this view must not print: it is the same shape as the "MIT
    # coverage 0%" claim #311 removed, a confident statement about something never
    # examined.
    uncredited = sum(1 for a in inv["articles"] if not a["authors"])
    pct = (
        round(counts["fields_met"] / counts["fields_total"] * 100) if counts["fields_total"] else 0
    )

    notes = []
    if total - cited:
        loose = [a["label"] for a in articles if a["state"] == "uncited"]
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{total - cited} of {total} articles are '
            "cited by nothing in the crate</b> "
            f"({', '.join(loose[:4])}{'&hellip;' if len(loose) > 4 else ''}). Point the Root "
            "Data Entity&rsquo;s <code>citation</code> at the article — or the Study that "
            "rests on it — otherwise the paper is described in the deposit and referenced "
            "by nothing.</span></p>"
        )
    if total - doi_backed:
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{total - doi_backed} of {total} articles '
            "carry no resolvable DOI.</b> A title names a paper; <code>10.…</code> retrieves "
            "it, and is the only form a machine can follow.</span></p>"
        )
    if broken:
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{broken} author reference'
            f"{'' if broken == 1 else 's'} resolve"
            f"{'s' if broken == 1 else ''} to no entity in the crate.</b> The article&rsquo;s "
            "<code>author</code> list points at an <code>@id</code> no node carries — usually a "
            "<code>#CitationAuthor_&hellip;</code> stub minted for a co-author Crossref gave no "
            "ORCID for. Add the <code>Person</code> entity or drop the reference: a credit that "
            "resolves to nothing credits nobody.</span></p>"
        )
    if uncredited:
        notes.append(
            f'<p class="chem-warn">{_mk("no")}<span><b>{uncredited} of {total} articles '
            "credit nobody.</b> The work carries no <code>author</code> or "
            "<code>contributor</code> at all, so the crate says who was cited but not "
            "who wrote it — and an empty credit list cannot fail the resolution check "
            "below, which is why it reads as clean.</span></p>"
        )
    if not notes:
        notes.append(
            '<p class="good-note">Every article is cited, DOI-backed, and every author '
            "resolves.</p>"
        )

    ordered = sorted(
        articles, key=lambda a: (a["state"] == "cited", a["met"], a["name"].casefold(), a["id"])
    )
    head = "".join(
        f'<th scope="col" title="{html.escape(full)}">{html.escape(short)}</th>'
        for full, short in CITATION_COVERAGE_FIELDS
    )
    # Every article is listed — no cap. This is a metadata-checking view, and a
    # truncated tail is exactly where an uncited paper would hide.
    rows = []
    for a in ordered:
        link = " 🔗" if a["resolvable"] else ""
        doi = f'<span class="ty">{html.escape(a["doi"])}</span>' if a["doi"] else ""
        flag = (
            ""
            if a["state"] == "cited"
            else f'<span class="chem-flag" title="{html.escape(_CITE_STATE_NOTE["uncited"])}">'
            "uncited</span>"
        )
        # The credit list is where #532 lives, so the row states how much of it
        # actually reaches an entity rather than leaving it to the tick alone.
        dangling = sum(1 for author in a["authors"] if not author["resolved"])
        credit = (
            f'<span class="chem-flag" title="author @ids that resolve to no entity">'
            f"{dangling} of {len(a['authors'])} authors unresolved</span>"
            if dangling
            else ""
        )
        cells = "".join(
            f"<td>{_mk(_kind(a['fields'].get(full)))}</td>"
            for full, _short in CITATION_COVERAGE_FIELDS
        )
        rows.append(
            f'<tr><th scope="row">{_mk("ok" if a["state"] == "cited" else "no")}'
            f'<span class="cn">{a["label"]}{link}</span>{doi}{flag}{credit}</th>{cells}</tr>'
        )
    matrix = (
        '<div class="chem-tbl-scroll"><table class="chem-tbl">'
        '<caption class="sr-only">Identification fields carried by each cited article</caption>'
        f'<thead><tr><th scope="col">Article</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )

    panel = (
        '<p class="cov-cap">The literature the crate stands on — whether each paper is '
        "actually cited from the record, and whether a reader could retrieve it and reach "
        f"the people who wrote it. <b>{total}</b> article{'' if total == 1 else 's'} · "
        f"<b>{cited}</b> cited · <b>{doi_backed}</b> DOI-backed · <b>{pct}%</b> complete.</p>\n"
        f"  {''.join(notes)}\n"
        f"  {matrix}"
    )
    return panel, str(total)


def build_maturity_html(
    state: CrateState,
    *,
    validation: ValidationReport | None = None,
    graph: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> str:
    """Render the maturity report HTML for *state*.

    The profile-adherence section is rendered from the crate's existing
    validation results (``validation`` or, by default, ``state.validation``) —
    NOT by re-running the SHACL validator. That keeps report generation cheap so
    embedding it in ``export_crate`` adds no validation cost (#85); validation is
    a separate step (e.g. ``build_and_validate`` in the agent loop). If no
    validation has run, the section says so. Adherence is reported across the
    three severity tiers Required / Recommended / Optional (#306); an unevaluated
    SHOULD/MAY tier renders as "not assessed", never a false green zero.

    When a crate ``graph`` (the ``@graph`` from ``crate.metadata.generate()``) is
    supplied, the report also folds in the tabbed graph-views section — the
    All-entities composition map, the LabProcess derivation chain and the other
    per-question diagrams, all self-contained inline SVG. Omitting ``graph``
    skips that section — the report is still complete without it.

    Args:
        state: The crate state being reported on.
        validation: Validation results to render. Defaults to
            ``state.validation``.
        graph: The crate's serialized ``@graph`` (or the full metadata document)
            used to render the graph views. When ``None``
            the LabProcesses & graph section is omitted — but MIT coverage is
            still scored against an assembled graph, which the assessor then
            builds itself (#311). Omitting ``graph`` costs one extra in-memory
            assembly and reports on that document: it is a real measurement of a
            real crate, at worst marginally more conservative than the export
            document (``_assemble_graph`` leaves out auto-included scanned-file
            leaves). What it is never again is 0% for a crate nobody scored.
    """
    esc = html.escape
    # The crate's own name: the root Dataset of the graph the writer is handed.
    # The session's metadata is what a state-only render has (#719).
    root_name = _root_of(_raw_nodes(graph)).get("name")
    title = (
        (root_name.strip() if isinstance(root_name, str) else "")
        or state.metadata.title
        or "RO-Crate"
    )
    page_title = f"{title} — vitro-crate maturity report"
    # MIT is scored against the assembled @graph — the crate_slot vocabulary
    # describes the serialized crate, not CrateState (#311). The export path
    # passes the graph it already built; without one the assessor assembles its
    # own rather than report a number it did not measure. Either way the report
    # prints a real measurement or "not assessed", never a 0% nobody computed.
    mit = assess_mit_coverage(state, graph=graph)
    # Feed that MIT result into FAIR so the mit_coverage indicator (RDA-R1.3-01D)
    # reflects the graph-based coverage — state.mit_assessment is never populated
    # on this path (#311).
    fair = assess_fair_maturity(state, mit=mit, graph=graph)
    val = validation if validation is not None else state.validation

    tiers = _severity_tiers(val) if _validation_has_signal(val) else None
    # Does the recorded verdict still describe THIS state? `export_crate`
    # re-validates when it does not, so a stale banner here means the report was
    # built directly from a state that outran its last validation.
    stale = tiers is not None and val.is_stale_for(state)
    if stale:
        for tier in tiers:
            tier["state"] = "na"
            # The summary goes too: "3 / 3 profiles" asserts a pass just as
            # loudly as a green tick, and it was measured on a different crate.
            tier["summary"] = "out of date"
            tier["note"] = "Recorded before the crate's latest changes."

    # The study facts prefer the graph (its publication names the subhead);
    # without one the card renders what the state itself holds.
    study = _study_facts(state, graph)
    publication = study.get("publication")
    subhead = (publication[2] if publication and publication[2] else "") or title
    header = _render_header(title, subhead)
    study_card = _render_study_card(study)
    crate_card = _render_crate_card(
        state,
        val if _validation_has_signal(val) else None,
        graph,
        validated=tiers is not None and not stale,
    )

    # The chemicals inventory is shared between the Chemicals graph view and the
    # Graph tile's source model — one cheap pass over the graph each.
    chem_inv: dict[str, Any] | None = None
    explorer_section = ""
    lane_section = ""
    explorer_style = ""
    residence: Counter[str] | None = None
    if graph is not None:
        from builder.writers.assay_lane import render_assay_lane_section
        from builder.writers.entity_explorer import explorer_css, render_explorer_section
        from builder.writers.provenance_dag import build_chemical_inventory, build_crate_graph

        chem_inv = build_chemical_inventory(graph)
        # The interactive counterpart to those views (#615). It carries script,
        # which the rest of the page does not — but nothing it loads comes from
        # off the page, so the report is still the self-contained artifact it
        # has to be to travel inside a crate. The coverage inventory folds into
        # it: same entities, asked about from the other side.
        explorer_section = render_explorer_section(
            graph, coverage=_render_entity_coverage_section(graph, chem_inv)
        )
        # And one lane per assay (#686), which reads the island the explorer
        # writes — so it follows the explorer here, and a crate with no assay
        # to draw returns nothing rather than an empty heading.
        lane_section = render_assay_lane_section(graph)
        explorer_style = explorer_css()
        # The same model the explorer's payload is cut from — `all_edges` so the
        # named-only stubs a secondary edge reaches are counted, as they are there.
        residence = Counter(
            n["residence"] for n in build_crate_graph(graph, all_edges=True)["nodes"]
        )
    from builder.tools.air_assessment import assess_air_readiness
    from builder.tools.fair_assessment import (
        DSM_INDICATORS_PATH,
        _load_yaml,
        dsm_ceiling,
        dsm_grid,
        dsm_verdicts,
    )
    from builder.tools.remediation import dsm_indicator_actions

    # Every axis reads the SAME assembled graph. AI-readiness asks about entities and
    # the links between them, which exist only once the crate is assembled — with no
    # graph its criteria report "not assessed" rather than guessing.
    air = assess_air_readiness(state, graph=graph)

    dsm_data = _load_yaml(DSM_INDICATORS_PATH) or {}
    # ONE evaluation pass, shared by the grid, the level, the ceiling and the blockers,
    # so the four cannot disagree and the checks walk the graph once rather than six
    # times. The DSM's Level 2-4 field/value indicators live in the assembled graph,
    # not CrateState, so the graph is threaded here.
    dsm_answers = dsm_verdicts(state, dsm_data, graph)
    dsm_reach = dsm_ceiling(state, dsm_data, graph, dsm_answers)
    dsm_cells = dsm_grid(state, dsm_data, graph, answers=dsm_answers)

    dsm_section = _render_dsm_grid_section(
        dsm_cells,
        dsm_data.get("levels") or {},
        str((dsm_data.get("source") or {}).get("assessment_tool") or ""),
    )
    dsm_section += _render_dsm_levels(dsm_data, dsm_answers, dsm_data.get("levels") or {})
    # The indicators standing before the next level, worded as instructions and ranked
    # against the conformance findings in one list — see _render_recommendations.
    dsm_recommendations = dsm_indicator_actions(dsm_reach["blocked_by"], dsm_data)

    kpis = _render_kpis(
        tiers,
        val if _validation_has_signal(val) else None,
        fair,
        dsm_reach["blocked_by"],
        mit,
        air,
        residence,
        ceiling=dsm_reach,
        grid=dsm_cells,
        stale=stale,
    )
    prof_section = _render_profile_section(val, tiers, stale=stale)
    mit_section = _render_mit_section(mit)
    air_section = _render_air_section(air)

    # The crate card closes the content: how the report was built is provenance
    # a reader wants last, not between the headline and the verdict. The
    # references sit under it and close the page — the footer's slogan spans
    # were removed on review, and the empty footer went with them (the crate
    # card already states the generator and the report's filename).
    body = (
        f'<div class="masthead">{header}{study_card}</div>\n'
        + kpis
        + explorer_section
        + lane_section
        + prof_section
        + dsm_section
        + mit_section
        + air_section
        + _render_recommendations(
            val,
            graph,
            dsm=dsm_recommendations,
            dsm_level=dsm_reach["attained"] + 1,
            stale=stale,
        )
        + crate_card
        + _render_references()
    )

    # ONE pass over the shell, so nothing a substitution inserts is ever scanned
    # again. Chained `.replace()` calls cannot give that guarantee in any order:
    # BODY carries crate-controlled entity ids and validation messages, and
    # `html.escape` leaves underscores alone, so a finding on an entity whose
    # `@id` is `#__TITLE__` came out as `#My Crate` — naming an entity that does
    # not exist. Reversing the order only moves the hole to a crate titled
    # `__BODY__`, which would paste the whole body into `<title>`. Both strings
    # come from the crate, so neither can be the trusted one.
    # React Flow's stylesheet joins the report's own rather than riding in a
    # second <style> in the body: the page has always had exactly one, every
    # section-scoped assertion in the suite reads the body as "after the first
    # </style>", and only rules in the head are inside the print block.
    filling = {
        "__STYLE__": _load_css() + explorer_style,
        "__BODY__": body,
        "__TITLE__": esc(page_title),
    }
    return _SHELL_PLACEHOLDER_RE.sub(lambda m: filling[m.group(0)], _load_shell())
