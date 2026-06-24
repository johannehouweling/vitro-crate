"""RO-Crate maturity report (``ro-crate-maturity.html``), embedded in the crate (#85).

Renders a self-contained HTML report (inline CSS, no external assets) covering the
four axes from the issue:

* **Profile adherence** — base / ISA / ISA-Tox conformance, with actionable
  suggestions drawn from the REQUIRED/RECOMMENDED validation issues;
* **FAIR** — the RDA-style indicators and the Data Stewardship Maturity (DSM) level;
* **FAIR Maturity R1.3 (OECD MIT)** — per-module coverage of the in-vitro tox
  MIT checklist;
* **Reproducibility readiness** — a derived checklist.

``export_crate`` embeds the rendered page as a ``CreativeWork`` ``about`` ``./``,
mirroring the entity-graph (#130) and preview (#86) artifacts.

``build_maturity_html`` is pure and cheap: FAIR/MIT come from the deterministic
assessors and the profile-adherence section is rendered from the crate's existing
``state.validation`` — it does **not** run the SHACL validator. That keeps the
embed in ``export_crate`` free of validation cost; validation is a separate step.
"""

from __future__ import annotations

import html

from builder.state import CrateState, ValidationReport
from builder.tools.fair_assessment import assess_fair_maturity
from builder.tools.mit_assessment import assess_mit_coverage

REPORT_FILENAME = "ro-crate-maturity.html"


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
        ("Experimental protocol documented", protocol_ok,
         "Add a LabProtocol or describe each LabProcess."),
        ("Process inputs/outputs wired", io_ok,
         "Link process object/result (the derivation chain) so steps are traceable."),
        ("Instruments / software recorded", instrument_ok,
         "Record the detection instrument, manufacturer, or analysis software."),
        ("Data files included", data_ok,
         "Attach the raw/processed data files referenced by the assays."),
        ("Attribution & identity", attribution_ok,
         "Set a title, at least one Person (author), and an accession/identifier."),
    ]


_STYLE = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  margin: 2rem auto; max-width: 60rem; padding: 0 1rem; line-height: 1.5; }
h1 { margin-bottom: 0.25rem; }
h2 { margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.2rem; }
.muted { color: #777; }
.ok { color: #1a7f37; } .bad { color: #b3261e; } .na { color: #999; }
.bar { background: #eee; border-radius: 0.4rem; height: 0.7rem;
  overflow: hidden; max-width: 18rem; }
.bar > span { display: block; height: 100%; background: #2a4b8d; }
table { border-collapse: collapse; width: 100%; margin-top: 0.4rem; }
th, td { text-align: left; padding: 0.35rem 0.6rem;
  border-bottom: 1px solid #eee; vertical-align: top; }
ul.sugg { margin: 0.3rem 0 0.6rem 1.1rem; padding: 0; }
ul.sugg li { margin: 0.1rem 0; }
footer { margin-top: 2rem; color: #999; font-size: 0.8rem; }
"""


def _mark(ok: bool | None) -> str:
    if ok is None:
        return "<span class='na'>n/a</span>"
    return "<span class='ok'>&#10003;</span>" if ok else "<span class='bad'>&#10007;</span>"


def build_maturity_html(
    state: CrateState,
    *,
    validation: ValidationReport | None = None,
) -> str:
    """Render the maturity report HTML for *state*.

    The profile-adherence section is rendered from the crate's existing
    validation results (``validation`` or, by default, ``state.validation``) —
    NOT by re-running the SHACL validator. That keeps report generation cheap so
    embedding it in ``export_crate`` adds no validation cost (#85); validation is
    a separate step (e.g. ``build_and_validate`` in the agent loop). If no
    validation has run, the section says so.

    Args:
        state: The crate state being reported on.
        validation: Validation results to render. Defaults to
            ``state.validation``.
    """
    esc = html.escape
    title = state.metadata.title or "RO-Crate"
    fair = assess_fair_maturity(state)
    mit = assess_mit_coverage(state)
    val = validation if validation is not None else state.validation

    # --- Profile adherence (from existing validation results) ---
    if _validation_has_signal(val):
        layers = [
            ("RO-Crate 1.2", val.base_passed),
            ("ISA", val.isa_passed),
            ("ISA-Tox", val.tox_passed),
        ]
        rows = "".join(
            f"<tr><td>{esc(label)}</td><td>{_mark(passed)}</td></tr>" for label, passed in layers
        )
        suggestions = [
            f"<li><strong>Must fix:</strong> {esc(msg)}</li>" for msg in val.required_issues
        ] + [f"<li>Recommended: {esc(msg)}</li>" for msg in val.should_issues[:10]]
        sugg_html = (
            f"<ul class='sugg'>{''.join(suggestions)}</ul>"
            if suggestions
            else "<p class='ok'>No outstanding REQUIRED/RECOMMENDED issues.</p>"
        )
        profile_section = (
            "<table><thead><tr><th>Profile</th><th>REQUIRED passed</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>{sugg_html}"
        )
    else:
        profile_section = (
            "<p class='muted'>Not yet validated — run validation to populate "
            "profile adherence.</p>"
        )

    # --- FAIR ---
    dsm = fair.dsm_level
    fair_rows = "".join(
        f"<tr><td>{esc(str(i.get('dimension') or ''))}</td>"
        f"<td>{esc(str(i.get('text') or i.get('id') or ''))}</td>"
        f"<td>{_mark(i.get('passed'))}</td></tr>"
        for i in fair.indicator_results
    ) or "<tr><td colspan='3' class='muted'>No indicators evaluated.</td></tr>"
    fair_section = (
        f"<p>Data Stewardship Maturity (DSM) level: <strong>{dsm}/5</strong></p>"
        "<table><thead><tr><th>Dimension</th><th>Indicator</th><th>Met</th></tr></thead>"
        f"<tbody>{fair_rows}</tbody></table>"
    )

    # --- MIT (FAIR Maturity R1.3, OECD) ---
    pct = round(mit.overall_score * 100)
    if mit.module_scores:
        mit_rows = "".join(
            f"<tr><td>{esc(name)}</td><td>{sc.get('completed', 0)}/{sc.get('total', 0)}</td></tr>"
            for name, sc in sorted(mit.module_scores.items())
        )
        mit_table = (
            "<table><thead><tr><th>MIT module</th><th>Completed</th></tr></thead>"
            f"<tbody>{mit_rows}</tbody></table>"
        )
    else:
        mit_table = "<p class='muted'>No MIT module scores.</p>"
    mit_section = (
        f"<p>Overall MIT coverage: <strong>{pct}%</strong></p>"
        f"<div class='bar'><span style='width:{pct}%'></span></div>{mit_table}"
    )

    # --- Reproducibility readiness ---
    checks = _reproducibility_checks(state)
    repro_rows = "".join(
        f"<tr><td>{_mark(ok)}</td><td>{esc(label)}</td>"
        f"<td class='muted'>{'' if ok else esc(hint)}</td></tr>"
        for label, ok, hint in checks
    )
    repro_ready = sum(1 for _, ok, _ in checks if ok)
    repro_section = (
        f"<p>{repro_ready}/{len(checks)} readiness checks met.</p>"
        "<table><tbody>" + repro_rows + "</tbody></table>"
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)} — RO-Crate Maturity Report</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        f"<h1>{esc(title)}</h1>\n"
        "<p class='muted'>RO-Crate maturity report — profile adherence, FAIR, MIT coverage, "
        "and reproducibility readiness.</p>\n"
        f"<h2>Profile adherence</h2>\n{profile_section}\n"
        f"<h2>FAIR</h2>\n{fair_section}\n"
        f"<h2>FAIR Maturity R1.3 — OECD MIT coverage</h2>\n{mit_section}\n"
        f"<h2>Reproducibility readiness</h2>\n{repro_section}\n"
        "<footer>Generated by vitro-crate · ro-crate-maturity.html</footer>\n"
        "</body>\n</html>\n"
    )
