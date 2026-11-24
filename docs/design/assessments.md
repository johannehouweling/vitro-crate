# Maturity assessments

> MIT coverage, FAIR (RDA + FAIRplus DSM) and AI-readiness scoring against vendored published instruments.

Score against `mit/invitro_tox.yaml`, `fair/indicators.yaml`, `fair/dsm_indicators.yaml` and
`air/criteria.yaml`. All produce scores, not pass/fail, and share one verdict shape
(`builder/tools/assessment_graph.py`).

**Indicator definitions are generated from vendored published instruments, never hand-written**,
so a definition cannot drift into a paraphrase:

| Axis | Instrument | Vendored | Generator |
|---|---|---|---|
| FAIR | RDA FAIR Data Maturity Model (doi:10.15497/rda00050, CC-BY-4.0) | `fair/rda_fdmm.xlsx` | `scripts/gen_fair_indicators.py` |
| DSM | FAIRplus **Dataset** Maturity Model v1.2 (FAIRplus D2.6, doi:10.5281/zenodo.7464523, model text CC-BY-4.0) | `fair/fairplus_dsm_v1.2.xlsx`, `fair/dsm_nanopub_refs.yaml` | `scripts/gen_dsm_indicators.py` |

Each generator carries **all** published indicators (the DSM's 83) verbatim, and keeps which are
assessable, and with which check, in its own `LOCAL_SCOPE`. A scoped indicator whose check is not
registered **raises** — otherwise it is silently skipped and its level awarded free.
`tests/test_dsm_indicators_source.py` and `tests/test_fair_indicators_source.py` pin each YAML to
its generator and workbook. The DSM model text is CC-BY-4.0; the FAIRplus repo's MIT licence covers
only its theme.

**A DSM indicator's definition comes from the workbook; its cross-references from the
nanopublications** under `https://w3id.org/Data-Maturity/`: the related FAIR principles and T4FS
term, plus its canonical IRI and trusty URI. Nothing else is taken from them — they transcribe the
docs site, not v1.2. The 83 trusty URIs are vendored in `fair/dsm_nanopub_refs.yaml`;
hand-run `scripts/fetch_dsm_nanopub_refs.py` refuses to write unless it retrieved exactly 83. The
generator and assessors stay offline.

**Where an instrument's arithmetic is not reproduced, the YAML says so.** The RDA workbook's
per-area level (`calc!C13:F13`) is not published: Accessibility is 12 of 12 `out_of_scope`, so the
ladder would charge the hosting repository's properties to the crate. `fair/indicators.yaml`'s
`scoring:` block records the formulas read from the sheet and that reason; per-indicator met/failed
is the instrument's own (column J). AIR declares the same way; the DSM reproduces its grid outright.

**A check reads the crate, not the session.** An indicator is scored against the assembled
`@graph`, so a third party scoring the published crate reaches our number. Given no graph it
answers *not assessed* (`None`) rather than guessing from `CrateState`
(`assessment_graph.needs_graph`); the RDA count keeps that as its own bucket, a DSM cell drops it
from `pct` and scores 0 in `published_pct`. `_GRAPH_AWARE_FAIR_CHECKS` names the RDA checks that
read the graph, as does every DSM check registered unwrapped in `DSM_CHECKS`. The one exception:
the licence predicates (`_effective_license`) fall back to `state.metadata.license`, because a
deposit-declared licence (#535) is a crate fact. `_state_check` marks the rest — a **burn-down**,
each refuted rewrite enumerated in `tests/test_fair_metrics_can_fail.py`, which may shrink and may
not grow. Two indicators asking one question share one function.

**A check must be able to fail, and must ask the published question.** `len(entities) > 0` scores
that the builder ran. `tests/test_fair_metrics_can_fail.py` scores two empty entities and no
payload and requires every indicator about *the data* to read `False`; the packaging indicators it
may meet are enumerated there. Moving a check to the graph without rewriting its predicate inflates
scores, so **a migration ships with a real crate that fails it, and published scores are expected to
go down** (#670).
