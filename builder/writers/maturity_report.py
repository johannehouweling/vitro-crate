"""RO-Crate maturity report (``ro-crate-metadata-maturity.html``), embedded in the crate (#85).

Renders a self-contained, light-mode evaluation dashboard (inline CSS, no external
assets) covering the four axes from the issue:

* **Profile adherence** — base / ISA / ISA-Tox conformance, reported across the
  three SHACL severity tiers Required / Recommended / Optional (#306), with the
  REQUIRED/RECOMMENDED issues surfaced as actionable suggestions;
* **FAIR** — the RDA-style indicators rolled up into F/A/I/R pillars plus the Data
  Stewardship Maturity (DSM) level;
* **OECD MIT coverage** — per-module coverage of the in-vitro tox MIT checklist;
* **Reproducibility readiness** — a derived checklist.

``export_crate`` embeds the rendered page as a ``CreativeWork`` ``about`` ``./``,
mirroring the entity-graph (#130) and preview (#86) artifacts.

``build_maturity_html`` is pure and cheap: FAIR/MIT come from the deterministic
assessors and the profile-adherence section is rendered from the crate's existing
``state.validation`` — it does **not** run the SHACL validator. That keeps the
embed in ``export_crate`` free of validation cost; validation is a separate step.

Severity-tier nuance (#306): the fast in-loop path (``build_and_validate``) gates
at REQUIRED severity and never populates ``should_issues`` / ``may_issues``, so an
empty list at those tiers means the tier was *never evaluated*, not that it is
clean. The report models an explicit "not assessed" state for such tiers and never
renders an unevaluated tier as a green zero.
"""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path
from typing import Any

from builder.state import CrateState, FAIRReport, MITReport, ValidationReport
from builder.tools.fair_assessment import assess_fair_maturity
from builder.tools.mit_assessment import assess_mit_coverage

REPORT_FILENAME = "ro-crate-metadata-maturity.html"

# FAIR dimension letters (as emitted by fair/indicators.yaml) → display names.
_DIM_NAMES = {"F": "Findable", "A": "Accessible", "I": "Interoperable", "R": "Reusable"}


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
    )


def _reproducibility_checks(state: CrateState) -> list[tuple[str, bool, str]]:
    """Derive a reproducibility-readiness checklist ``(label, ok, hint)``."""
    processes = state.list_entities("LabProcess")
    protocols = state.list_entities("LabProtocol")

    def _has(entity_list: list, keys: tuple[str, ...]) -> bool:
        return any(any(e.fields.get(k) for k in keys) for e in entity_list)

    protocol_ok = bool(protocols) or _has(processes, ("description",))
    io_ok = _has(processes, ("object", "result", "input", "output", "samples", "derives_from"))
    instrument_ok = _has(
        processes,
        ("detection_instrument", "instrument_manufacturer", "software", "data_processing"),
    )
    data_ok = bool(state.list_entities("File"))
    investigations = state.list_entities("Investigation")
    attribution_ok = (
        bool(state.metadata.title)
        and bool(state.list_entities("Person"))
        and (bool(state.metadata.accession) or _has(investigations, ("identifier",)))
    )

    return [
        (
            "Experimental protocol documented",
            protocol_ok,
            "Add a LabProtocol or describe each LabProcess.",
        ),
        (
            "Process inputs/outputs wired",
            io_ok,
            "Link process object/result (the derivation chain) so steps are traceable.",
        ),
        (
            "Instruments / software recorded",
            instrument_ok,
            "Record the detection instrument, manufacturer, or analysis software.",
        ),
        (
            "Data files included",
            data_ok,
            "Attach the raw/processed data files referenced by the assays.",
        ),
        (
            "Attribution & identity",
            attribution_ok,
            "Set a title, at least one Person (author), and an accession/identifier.",
        ),
    ]


# --- severity tiers (#306) -------------------------------------------------
def _plural_issues(n: int) -> str:
    return f"{n} issue" if n == 1 else f"{n} issues"


def _severity_tiers(val: ValidationReport) -> list[dict[str, str]]:
    """Profile-adherence severity tiers rendered from existing validation (#306).

    Returns one dict per tier: ``{tier, state, summary, note}`` where ``state`` is
    ``"ok"`` | ``"no"`` | ``"na"``.

    REQUIRED is the build gate, so it is assessed whenever validation has run; it
    passes only when all three profiles pass with no REQUIRED issues. The SHOULD
    and MAY tiers are populated only by a full validation sweep — the fast in-loop
    path stops at REQUIRED and leaves them empty. An empty SHOULD/MAY tier is
    therefore reported as "not assessed" (``"na"``), never as a green zero: doing
    otherwise would be a false pass for a tier that was never evaluated.

    Called only when :func:`_validation_has_signal` is True (otherwise the whole
    section renders the "not yet validated" branch).
    """
    n_pass = sum((val.base_passed, val.isa_passed, val.tox_passed))
    req_ok = n_pass == 3 and not val.required_issues
    tiers: list[dict[str, str]] = [
        {
            "tier": "Required",
            "state": "ok" if req_ok else "no",
            "summary": f"{n_pass} / 3 profiles",
            "note": "Blocking — every layer must pass to build.",
        }
    ]
    for label, issues, note in (
        ("Recommended", val.should_issues, "SHOULD-level quality checks."),
        ("Optional", val.may_issues, "MAY-level informational checks."),
    ):
        if issues:
            tiers.append(
                {"tier": label, "state": "no", "summary": _plural_issues(len(issues)), "note": note}
            )
        else:
            tiers.append(
                {
                    "tier": label,
                    "state": "na",
                    "summary": "not assessed",
                    "note": "Not evaluated — the build gates at Required.",
                }
            )
    return tiers


def _fair_pillars(fair: FAIRReport) -> list[dict[str, Any]]:
    """Roll the flat FAIR indicator list up into F/A/I/R pillars.

    Each pillar: ``{letter, name, met, total, na, state}`` where ``total`` counts
    only *assessable* indicators (``passed is not None``); indicators marked
    out-of-scope (``passed is None``, e.g. the hosting-level Accessible checks)
    count toward ``na`` and, when a pillar is entirely out of scope, render as
    "n/a" rather than a misleading 0.
    """
    pillars: list[dict[str, Any]] = []
    for letter, name in _DIM_NAMES.items():
        inds = [i for i in fair.indicator_results if str(i.get("dimension") or "") == letter]
        met = sum(1 for i in inds if i.get("passed") is True)
        na = sum(1 for i in inds if i.get("passed") is None)
        total = sum(1 for i in inds if i.get("passed") is not None)
        if total == 0:
            state = "na"
        elif met == total:
            state = "ok"
        elif met == 0:
            state = "low"
        else:
            state = "warn"
        pillars.append(
            {"letter": letter, "name": name, "met": met, "total": total, "na": na, "state": state}
        )
    return pillars


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


def _fill_class(met: int, total: int) -> str:
    if total <= 0:
        return "fill-warn"
    if met >= total:
        return "fill-good"
    if met == 0:
        return "fill-low"
    return "fill-warn"


_ASSET_DIR = Path(__file__).resolve().parent
_CSS_PATH = _ASSET_DIR / "maturity_report.css"
_SHELL_PATH = _ASSET_DIR / "maturity_report.html"


@lru_cache(maxsize=1)
def _load_css() -> str:
    """The report stylesheet, inlined into the self-contained page.

    Kept in a sibling ``maturity_report.css`` so the styling lives apart from the
    Python that assembles the markup; it is embedded (not linked) so the exported
    ``ro-crate-metadata-maturity.html`` renders offline with no external assets.
    """
    return _CSS_PATH.read_text(encoding="utf-8").strip("\n")


@lru_cache(maxsize=1)
def _load_shell() -> str:
    """The document shell (``maturity_report.html``) whose ``__STYLE__`` /
    ``__TITLE__`` / ``__BODY__`` placeholders are filled at render time."""
    return _SHELL_PATH.read_text(encoding="utf-8")


def _render_header(title: str, accession: str, tiers: list[dict[str, str]] | None) -> str:
    esc = html.escape
    chip = f'<span class="chip mono">{esc(accession)}</span>' if accession else ""
    if tiers is None:
        verdict = (
            '<span class="vpill warning"><span class="glyph"></span>Not yet validated</span>'
            '<span class="vsub">Run validation to populate profile adherence.</span>'
        )
    elif tiers[0]["state"] == "ok":
        verdict = (
            '<span class="vpill good"><span class="glyph"></span>Conformant</span>'
            '<span class="vsub">All required profile layers pass.</span>'
        )
    else:
        verdict = (
            '<span class="vpill critical"><span class="glyph"></span>Not conformant</span>'
            '<span class="vsub">One or more required profile layers fail.</span>'
        )
    return (
        "<header>\n"
        '  <div class="h-left">\n'
        '    <div class="kicker">'
        f'<span class="eyebrow">RO-Crate maturity report</span>{chip}</div>\n'
        f"    <h1>{esc(title)}</h1>\n"
        "  </div>\n"
        f'  <div class="verdict">{verdict}</div>\n'
        "</header>\n"
    )


def _render_kpis(
    tiers: list[dict[str, str]] | None,
    fair: FAIRReport,
    mit: MITReport,
    repro_ready: int,
    repro_total: int,
) -> str:
    # Profile-adherence tile: severity mini-rows, or an "awaiting validation" row.
    if tiers is None:
        prof_mk = _mk("na")
        sev_rows = (
            '<div class="sev-row">'
            + _mk("na")
            + '<span class="sev-t">Awaiting validation</span></div>'
        )
    else:
        prof_mk = _mk(tiers[0]["state"])
        sev_rows = "".join(
            f'<div class="sev-row">{_mk(t["state"])}'
            f'<span class="sev-t">{t["tier"]}</span>'
            f'<span class="sev-s">{t["summary"]}</span></div>'
            for t in tiers
        )
    prof_tile = (
        '<article class="kpi">'
        f'<div class="kpi-h"><span class="eyebrow">Profile adherence</span>{prof_mk}</div>'
        f'<div class="sev">{sev_rows}</div>'
        "</article>"
    )

    # FAIR / DSM tile.
    met_all = sum(1 for i in fair.indicator_results if i.get("passed") is True)
    na_all = sum(1 for i in fair.indicator_results if i.get("passed") is None)
    assessed = sum(1 for i in fair.indicator_results if i.get("passed") is not None)
    fair_sub = f"{met_all} of {assessed} indicators met"
    if na_all:
        fair_sub += f" · {na_all} n/a"
    rungs = "".join(
        f'<span class="rung {"on" if lvl <= fair.dsm_level else "off"}"></span>'
        for lvl in range(1, 6)
    )
    fair_tile = (
        '<article class="kpi">'
        '<div class="kpi-h"><span class="eyebrow">FAIR maturity</span></div>'
        f'<div class="kpi-v"><b>{fair.dsm_level}</b><span class="den">/ 5</span> '
        '<span class="tag-inline">DSM level</span></div>'
        f'<div class="kpi-sub">{fair_sub}</div>'
        f'<div class="ladder" role="img" aria-label="DSM level {fair.dsm_level} of 5">{rungs}</div>'
        "</article>"
    )

    # OECD MIT coverage tile.
    completed_all = sum(sc.get("completed", 0) for sc in mit.module_scores.values())
    total_all = sum(sc.get("total", 0) for sc in mit.module_scores.values())
    pct = round(mit.overall_score * 100)
    mit_tile = (
        '<article class="kpi">'
        '<div class="kpi-h"><span class="eyebrow">OECD MIT coverage</span></div>'
        f'<div class="kpi-v"><b>{pct}</b><span class="den">%</span></div>'
        f'<div class="kpi-sub">{completed_all} of {total_all} checklist fields</div>'
        f'<div class="meter" role="img" aria-label="MIT coverage {pct}%">'
        f'<i class="fill-cov" style="width:{pct}%"></i></div>'
        "</article>"
    )

    # Reproducibility tile.
    dots = "".join(
        f'<span class="dot {"on" if i < repro_ready else "off"}"></span>'
        for i in range(repro_total)
    )
    repro_tile = (
        '<article class="kpi">'
        '<div class="kpi-h"><span class="eyebrow">Reproducibility</span></div>'
        f'<div class="kpi-v"><b>{repro_ready}</b><span class="den">/ {repro_total}</span></div>'
        '<div class="kpi-sub">readiness checks met</div>'
        f'<div class="dots" role="img" aria-label="{repro_ready} of {repro_total}">{dots}</div>'
        "</article>"
    )

    return f'<div class="kpis">{prof_tile}{fair_tile}{mit_tile}{repro_tile}</div>\n'


def _render_profile_section(val: ValidationReport, tiers: list[dict[str, str]] | None) -> str:
    esc = html.escape
    if tiers is None:
        return (
            "<section>\n"
            '  <div class="sec-h"><h2>Profile adherence</h2></div>\n'
            '  <p class="lead">Not yet validated — run validation to populate profile '
            "adherence.</p>\n"
            "</section>\n"
        )

    profiles = [
        ("RO-Crate 1.2", val.base_passed),
        ("ISA", val.isa_passed),
        ("ISA-Tox", val.tox_passed),
    ]
    cards = "".join(
        f'<div class="prof-card">{_mk(_kind(passed))}<span>{esc(name)}</span>'
        "<em>REQUIRED</em></div>"
        for name, passed in profiles
    )
    detail_rows = "".join(
        f'<div class="sev-drow">{_mk(t["state"])}<span class="st">{t["tier"]}</span>'
        f'<span class="sc">{t["summary"]}</span><span class="sn">{t["note"]}</span></div>'
        for t in tiers
    )
    sugg_items = [
        f'<li class="must"><strong>Must fix:</strong> {esc(msg)}</li>'
        for msg in val.required_issues
    ] + [f"<li>Recommended: {esc(msg)}</li>" for msg in val.should_issues[:10]]
    if sugg_items:
        sugg = f'<ul class="sugg">{"".join(sugg_items)}</ul>'
    else:
        sugg = '<p class="good-note">No outstanding REQUIRED issues.</p>'

    return (
        "<section>\n"
        '  <div class="sec-h"><h2>Profile adherence</h2>'
        '<span class="sec-meta">3 layers · 3 severity tiers</span></div>\n'
        f'  <div class="prof-grid">{cards}</div>\n'
        '  <div class="sev-detail"><span class="sev-detail-label">By severity</span>'
        f"{detail_rows}</div>\n"
        f"  {sugg}\n"
        "</section>\n"
    )


def _render_fair_section(fair: FAIRReport) -> str:
    esc = html.escape
    pillars = _fair_pillars(fair)
    pillar_html = []
    for p in pillars:
        if p["state"] == "na":
            pv = '<span class="pv na">n/a</span>'
            meter = '<div class="meter na" role="img" aria-label="not assessed">out of scope</div>'
            note = "assessed by the hosting repository, not the crate"
        else:
            pct = round(p["met"] / p["total"] * 100) if p["total"] else 0
            fill = _fill_class(p["met"], p["total"])
            pv = f'<span class="pv">{p["met"]}<span class="den">/ {p["total"]}</span></span>'
            meter = (
                f'<div class="meter" role="img" aria-label="{p["met"]} of {p["total"]}">'
                f'<i class="{fill}" style="width:{pct}%"></i></div>'
            )
            note = f"{p['met']} of {p['total']} indicators met"
        pillar_html.append(
            '<div class="pillar">'
            f'<div class="pl-h"><span class="pl-letter">{p["letter"]}</span>'
            f'<span class="pl-name">{esc(p["name"])}</span>{pv}</div>'
            f"{meter}"
            f'<div class="pl-note">{note}</div>'
            "</div>"
        )

    # Disclosure lists for dimensions that have at least one failing indicator.
    discs = []
    for p in pillars:
        inds = [
            i
            for i in fair.indicator_results
            if str(i.get("dimension") or "") == p["letter"] and i.get("passed") is not None
        ]
        if not any(i.get("passed") is False for i in inds):
            continue
        items = "".join(
            f"<li>{_mk(_kind(bool(i.get('passed'))))} "
            f"<span>{esc(str(i.get('text') or i.get('id') or ''))}</span></li>"
            for i in inds
        )
        discs.append(
            f'<details class="disc"><summary>{esc(p["name"])} — {p["met"]} of {p["total"]} '
            f'indicators · what’s missing</summary><ul class="ind">{items}</ul></details>'
        )

    return (
        "<section>\n"
        '  <div class="sec-h"><h2>FAIR</h2>'
        f'<span class="sec-meta">Data Stewardship Maturity <b>{fair.dsm_level}/5</b></span></div>\n'
        f'  <div class="pillars">{"".join(pillar_html)}</div>\n'
        f"  {''.join(discs)}\n"
        "</section>\n"
    )


def _render_mit_section(mit: MITReport) -> str:
    esc = html.escape
    completed_all = sum(sc.get("completed", 0) for sc in mit.module_scores.values())
    total_all = sum(sc.get("total", 0) for sc in mit.module_scores.values())
    pct = round(mit.overall_score * 100)
    if mit.module_scores:
        rows = "".join(
            f'<div class="mrow"><div class="mname">{esc(name)}</div>'
            f'<div class="mbar"><div class="meter" role="img" '
            f'aria-label="{sc.get("completed", 0)} of {sc.get("total", 0)}">'
            f'<i class="fill-cov" style="width:'
            f'{round(sc.get("completed", 0) / sc["total"] * 100) if sc.get("total") else 0}%">'
            "</i></div></div>"
            f'<div class="mfrac">{sc.get("completed", 0)}'
            f'<span class="den">/{sc.get("total", 0)}</span></div>'
            "</div>"
            for name, sc in sorted(mit.module_scores.items())
        )
        body = f'<div class="mit">{rows}</div>'
    else:
        body = '<p class="lead">No MIT module scores.</p>'
    return (
        "<section>\n"
        '  <div class="sec-h"><h2>OECD MIT coverage</h2>'
        f'<span class="sec-meta"><b>{completed_all}/{total_all}</b> fields · {pct}%</span></div>\n'
        '  <p class="lead">Coverage of the OECD in-vitro toxicology reporting checklist. Low '
        "coverage is expected for an auto-built crate — it measures how many domain fields are "
        "filled, not whether the crate is valid.</p>\n"
        f"  {body}\n"
        "</section>\n"
    )


def _render_repro_section(checks: list[tuple[str, bool, str]]) -> str:
    esc = html.escape
    ready = sum(1 for _, ok, _ in checks if ok)
    items = "".join(
        f'<li>{_mk(_kind(ok))}<div><span class="rl">{esc(label)}</span>'
        + ("" if ok else f'<span class="hint">{esc(hint)}</span>')
        + "</div></li>"
        for label, ok, hint in checks
    )
    return (
        "<section>\n"
        '  <div class="sec-h"><h2>Reproducibility readiness</h2>'
        f'<span class="sec-meta"><b>{ready}/{len(checks)}</b> met</span></div>\n'
        f'  <ul class="repro">{items}</ul>\n'
        "</section>\n"
    )


def _render_topology_strip(counts: dict[str, int]) -> str:
    """The graph-topology metrics strip (relocated into the Provenance section).

    Renders the crate's entity composition by paper layer (packaging / ISA
    structural / ISA-Tox domain) plus any orphan/dangling-reference flags, from
    the deterministic :func:`build_crate_graph` counts.
    """
    total = counts.get("layer1", 0) + counts.get("layer2", 0) + counts.get("layer3", 0)
    parts = [
        '<span class="topo-label">Graph topology</span>',
        f'<span class="c"><b>{total}</b>&nbsp;entities</span>',
        '<span class="c"><span class="sw" style="background:var(--muted)"></span>'
        f'{counts.get("layer1", 0)} packaging</span>',
        '<span class="c"><span class="sw" style="background:var(--cat-process)"></span>'
        f'{counts.get("layer2", 0)} ISA structural</span>',
        '<span class="c"><span class="sw" style="background:var(--cat-data)"></span>'
        f'{counts.get("layer3", 0)} ISA-Tox domain</span>',
    ]
    flags: list[str] = []
    n_orphan = counts.get("orphan", 0)
    n_dangling = counts.get("dangling", 0)
    if n_orphan:
        flags.append(f"{n_orphan} orphan" + ("" if n_orphan == 1 else "s"))
    if n_dangling:
        flags.append(f"{n_dangling} dangling ref" + ("" if n_dangling == 1 else "s"))
    if flags:
        parts.append(f'<span class="c warnflag">{_mk("no")}&nbsp;{" · ".join(flags)}</span>')
    return f'<div class="comp topo">{"".join(parts)}</div>'


def _render_provenance_section(graph: dict[str, Any] | list[dict[str, Any]]) -> str:
    """Fold the provenance chain + graph topology into the report (#85).

    Draws the LabProcess derivation chain as a self-contained inline SVG (offline,
    no script) with a shape legend, and appends the graph-topology strip. When the
    crate records no derivation chain, the SVG is replaced by a note but the
    topology strip still renders. Called only when a crate ``@graph`` is supplied.
    """
    from builder.writers.provenance_dag import build_crate_graph, render_provenance_svg

    svg = render_provenance_svg(graph)
    counts = build_crate_graph(graph).get("counts", {})
    if svg:
        body = (
            '<p class="prov-cap">The derivation chain a receiving lab follows to trace an '
            "output back to its inputs — materials, the processes applied, and the files "
            "each step produced.</p>\n"
            f'  <div class="prov-scroll">{svg}</div>\n'
            '  <div class="prov-legend">'
            '<span class="lg"><svg width="20" height="14" aria-hidden="true">'
            '<polygon points="4,1 14,1 18,7 14,13 4,13 1,7" fill="var(--accent-soft)" '
            'stroke="var(--cat-process)" stroke-width="1.6"/></svg> Process</span>'
            '<span class="lg"><svg width="22" height="14" aria-hidden="true">'
            '<rect x="1" y="2" width="20" height="10" rx="5" fill="var(--surface-2)" '
            'stroke="var(--cat-material)" stroke-width="1.6"/></svg> Sample / material</span>'
            '<span class="lg"><svg width="18" height="14" aria-hidden="true">'
            '<rect x="1" y="1" width="15" height="12" rx="2" fill="var(--surface-2)" '
            'stroke="var(--cat-data)" stroke-width="1.6"/></svg> File / table</span>'
            '<span class="lg"><span class="gl obj"></span> consumes (object)</span>'
            '<span class="lg"><span class="gl"></span> produces (result)</span>'
            "</div>"
        )
    else:
        body = (
            '<p class="lead">No derivation chain recorded — this crate has no LabProcess '
            "input/output edges to trace.</p>"
        )
    return (
        "<section>\n"
        '  <div class="sec-h"><h2>Provenance &amp; graph</h2>'
        '<span class="sec-meta">how the result was produced</span></div>\n'
        f"  {body}\n"
        f"  {_render_topology_strip(counts)}\n"
        "</section>\n"
    )


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
    supplied, the report also folds in a Provenance & graph section: the LabProcess
    derivation chain drawn as a self-contained inline SVG, plus a graph-topology
    strip (entity composition by paper layer, orphan/dangling flags). Omitting
    ``graph`` skips that section — the report is still complete without it.

    Args:
        state: The crate state being reported on.
        validation: Validation results to render. Defaults to
            ``state.validation``.
        graph: The crate's serialized ``@graph`` (or the full metadata document)
            used to render the provenance chain and topology strip. When ``None``
            the Provenance & graph section is omitted.
    """
    esc = html.escape
    title = state.metadata.title or "RO-Crate"
    accession = state.metadata.accession or ""
    fair = assess_fair_maturity(state)
    mit = assess_mit_coverage(state)
    val = validation if validation is not None else state.validation

    tiers = _severity_tiers(val) if _validation_has_signal(val) else None
    checks = _reproducibility_checks(state)
    repro_ready = sum(1 for _, ok, _ in checks if ok)

    header = _render_header(title, accession, tiers)
    kpis = _render_kpis(tiers, fair, mit, repro_ready, len(checks))
    prov_section = _render_provenance_section(graph) if graph is not None else ""
    prof_section = _render_profile_section(val, tiers)
    fair_section = _render_fair_section(fair)
    mit_section = _render_mit_section(mit)
    repro_section = _render_repro_section(checks)

    footer = (
        "<footer><span>Generated by vitro-crate · ro-crate-metadata-maturity.html</span>"
        "<span>Self-contained · offline · print-friendly</span></footer>\n"
    )
    body = (
        header + kpis + prov_section + prof_section + fair_section
        + mit_section + repro_section + footer
    )

    # Fill the shell placeholders. STYLE and BODY first (neither can contain a
    # sentinel), TITLE last so crate-controlled text can never re-trigger a
    # replacement.
    return (
        _load_shell()
        .replace("__STYLE__", _load_css())
        .replace("__BODY__", body)
        .replace("__TITLE__", esc(title))
    )
