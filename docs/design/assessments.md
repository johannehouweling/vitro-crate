# Maturity assessments

> MIT coverage, FAIR (RDA + FAIRplus DSM) and AI-readiness scoring against vendored published instruments.

Score against `mit/invitro_tox.yaml`, `fair/indicators.yaml`,
`fair/dsm_indicators.yaml` and `air/criteria.yaml`. All produce scores, not
pass/fail, and share one verdict shape (`builder/tools/assessment_graph.py`).

**Indicator definitions are generated from vendored published instruments, never
hand-written**, so a definition cannot silently drift into a paraphrase of the model
the paper claims to score:

| Axis | Instrument | Vendored | Generator |
|---|---|---|---|
| FAIR | RDA FAIR Data Maturity Model (doi:10.15497/rda00050, CC-BY-4.0) | `fair/rda_fdmm.xlsx` | `scripts/gen_fair_indicators.py` |
| DSM | FAIRplus **Dataset** Maturity Model v1.2 (FAIRplus D2.6, doi:10.5281/zenodo.7464523, model text CC-BY-4.0) | `fair/fairplus_dsm_v1.2.xlsx`, `fair/dsm_nanopub_refs.yaml` | `scripts/gen_dsm_indicators.py` |

Each generator carries **all** the published indicators — the DSM's full 83, not the
subset we can assess — with verbatim text and the model's own columns, and keeps the
repo-specific decision (which indicators are assessable from one crate, and with which
check) in its own `LOCAL_SCOPE`. An indicator scoped for assessment whose check is not
registered **raises**; it is a wiring bug, not a pass. That trap is why DSM-4-R6 was
silently skipped while its level was awarded for free. `tests/test_dsm_indicators_source.py`
and `tests/test_fair_indicators_source.py` pin each committed YAML to its generator's
output and to the source workbook.

Note the DSM model text is **CC-BY-4.0**; the FAIRplus repository's MIT `LICENSE.txt`
covers only its Jekyll theme and does not license the model.

**A DSM indicator's definition comes from the workbook; its published cross-references
come from the nanopublication network.** The same 83 indicators are published as
nanopublications under `https://w3id.org/Data-Maturity/`, which assert two mappings the
workbook has no column for — the FAIR principles an indicator relates to, and its T4FS
ontology term. `fair/dsm_indicators.yaml` carries both, plus the indicator's canonical
IRI and the trusty URI of the nanopublication asserting them; it takes no text, level,
granularity or cross-reference from there, because those nanopublications transcribe the
docs site rather than v1.2 of the workbook and contradict it on most indicators. The pin
is per indicator — there is no set-level or version IRI — so the 83 trusty URIs are
vendored in `fair/dsm_nanopub_refs.yaml` and a re-published batch is a diff.
`scripts/fetch_dsm_nanopub_refs.py` refuses to write unless it retrieved exactly 83. It
reaches the network, as `scripts/refresh_type_vocabulary.py` does; both run by hand, and
the generator and the assessors read the vendored file off disk and stay offline.

**Where an instrument's own arithmetic is not reproduced, the YAML says so.** The RDA
workbook computes a maturity level per FAIR area (`calc!C13:F13`, Level 0-5 gated on
essential / important / useful thresholds); this tool publishes a met/failed count and
no level. `fair/indicators.yaml`'s `scoring:` block records both, with the formulas
**read from the sheet** rather than restated, and states the reason: every level above
0 requires all of an area's essential indicators to be met, an `out_of_scope` indicator
can never be met, and Accessibility is 12 of 12 `out_of_scope` — so the ladder would
report the hosting repository's properties as the crate's failure. The per-indicator
boolean is a different case and the block says so too: the sheet's own column J
collapses its five-way metric all-or-nothing, so met/failed *is* the instrument's, not
a coarsening of it. AIR carries the same shape of declaration; the DSM reproduces its
instrument's grid outright and needs none.

**A check reads the crate, not the session.** An indicator is scored against the
assembled `@graph` — the bytes a reader receives — so a third party scoring the
published crate reaches our published number. Given no graph a check answers *not
assessed* (`None`) rather than guessing from `CrateState`, and where that answer lands is
the instrument's own arithmetic: the RDA count carries not-assessed as a bucket beside met
and failed, while a DSM cell drops it from `pct` and scores it 0 in `published_pct`, because
the published sheet has no such state; `_GRAPH_AWARE_FAIR_CHECKS` names the RDA checks that read the graph, and
every DSM check registered unwrapped in `DSM_CHECKS` reads it too;
`assessment_graph.needs_graph` states why the alternative is a lie. The licence predicates
are the one deliberate exception: `_effective_license` falls back to
`state.metadata.license`, because a licence the deposit itself declared (#535) is a crate
fact wherever it is read from, so the three of them — five registry entries, the DSM reusing
two — answer a bool with no graph rather than `None`. `_state_check`
marks what is left — a **burn-down**, enumerated with the reason each rewrite was
refuted in `tests/test_fair_metrics_can_fail.py`, which may shrink and may not grow.
Two indicators asking one question share one function, so the axes cannot disagree
about one crate.

**A check must be able to fail, and must ask the published question.** `len(entities)
> 0` is not an assessment: it scores that the builder ran. The floor is pinned by
`tests/test_fair_metrics_can_fail.py`, which scores a crate holding two empty entities
and no payload and requires every indicator whose published text names *the data* to
read `False`; the packaging indicators it may legitimately meet are enumerated there
with reasons. Moving a check to the graph without rewriting its predicate inflates the
score — porting DSM-1-C1 and DSM-1-R0 as-is lifts twelve crates a level for free —
so **a migration ships with a real crate that fails it, and published scores are
expected to go down** (#670).
