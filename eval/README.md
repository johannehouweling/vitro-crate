# `eval/` — evaluation harness for the ReAct build

This package is the in-repo evaluation harness for the ReAct build. It measures
*conformance, content, cost, latency, termination and reproducibility* over a fixed
corpus directly, instead of asserting them.

The harness is **agent-agnostic**: it measures whatever `BuildAgent` a factory
returns, and the live factory drives the shipped ReAct build. Every run writes the
same labeled report, so runs under different labels (another model, prompt or
revision) diff cleanly.

> **Cost note.** A *live* harness run makes real LLM calls (your configured
> DeepSeek-flash / OpenAI / Anthropic credentials) — that is its purpose. **CI and the
> unit tests are strictly offline**: they exercise the runner / metrics / report /
> determinism logic against a mock agent factory and never touch a model or the network.

## The agent-agnostic factory

The only thing the harness knows about the build it measures is the
[`BuildAgent`](agent_api.py) contract:

```python
class BuildAgent(Protocol):
    def build(self, case: EvalCase) -> BuildOutcome: ...   # state + session_id + error + stop_reason
```

`run_eval(agent_factory, corpus, *, repeats=2)` takes a **zero-arg `agent_factory`**
that returns a fresh `BuildAgent` per repeat (a fresh agent each time so no engine /
session state leaks into the determinism signal):

| Agent | Factory | Used by |
|-------|---------|---------|
| ReAct build (as shipped) | `eval.react_factory.make_react_agent_factory()` | `python -m eval` (live) |
| Offline tests | an in-memory mock returning canned `CrateState`s | tests only |

`ReActBuildAgent` wraps the shipped LangGraph ReAct loop: it creates a headless
`AgentEngine` behind the production `SimulatedHumanInterface` — the human batch mode
gets, so nothing blocks on stdin, no scan root is widened and no
RECOMMENDED/OPTIONAL sweep runs (#609) — `initialize()`s the case's input directory
(which also assigns the `session_id` and opens this run's `profile.ndjson`), then runs
the **shipped** build (`builder.agents.build.run_build(engine, ..., interactive=False)`,
which reads the deposit's structure and runs the ReAct loop) with the case's prompt as
its kickoff and returns the final `CrateState`. It used to build its own tools/model/graph
and `invoke` once — a copy of the loop's setup that measured a strictly smaller budget
than the build users run: no wall-clock timeout guard, no self-continue, and no
autonomous continuation, so a turn that narrated instead of asking ended the measured
build then and there (#609). The model-driving step is injected so the wiring is
unit-tested offline.

An agent may report a case `not_applicable` — one it does not attempt. Such cases are
counted (`num_not_applicable`) and left out of every average, so a capability gap
never scores as a $0 win; `num_cases_compared` is the denominator for the rest (#609).

## The corpus

A small fixed set of crate-build cases ([`corpus.py`](corpus.py)), each declared as
data — all inputs are in-repo and offline:

| `case_id` | `kind` | What it exercises |
|-----------|--------|-------------------|
| `minimal-backbone` | `minimal` | Build a conformant ISA-Tox backbone from a short brief. |
| `structured-svhps21` | `structured` | Scan the in-repo `tests/fixtures/svhps21_input` folder (README + raw/processed CSVs) and build from its structured metadata. |
| `structured-svhps22` | `structured` | **Entity-drafting** case: scan a *richer* `tests/fixtures/svhps22_input` folder (README naming a compound + cell line + protocol, plus raw/processed CSVs) and draft the full ISA-Tox domain set. Carries a `min_entities` content quota (below). |
| `unstructured-conversation` | `unstructured` | No metadata files — the whole crate is elicited from the prompt. |
| `arbitrary-tox-folder` | `unstructured` | Realistic arbitrary research folder, **no metadata file**: a study description, a methods/protocol write-up, a compound list, and nested measurement + analysis CSVs. Exercises the full scan → extract → materialize → assess path. Carries a complete-study `min_entities` quota (below). |

**Success predicate** (shared, strict): a case succeeds when its crate reaches
`{base, isa, tox}` REQUIRED conformance via `build_and_validate`
(`reaches_isa_tox_conformance`).

**Content-quality signal** (additive, per-case): conformance only measures whether
the agent *acted* — an almost-empty backbone can pass it. A case may also declare
`min_entities` (a minimum count of domain entities by `@type`); the harness then
records `meets_quota` + `entity_counts` (`meets_entity_quota`) so runs can be
compared on whether the drafted *content* is actually there, not just that the agent
acted. `min_entities` never changes the success predicate — cases without it report
`meets_quota = None`. Two cases declare one: `structured-svhps22` demands a compound,
a cell line, both attached data files and the AOP-Wiki pathway; `arbitrary-tox-folder`
demands a complete study — the ISA backbone, a cell line, a compound, a protocol, the
four-step `LabProcess` chain and both data files. `structured-svhps21`
(conformance-only) is deliberately kept as the looser baseline.

## The metrics

Per case, across `repeats` runs ([`metrics.py`](metrics.py), [`runner.py`](runner.py)):

- **success** (bool, repeat #1) beside `success_per_repeat`, `success_rate` and
  `always_succeeds` across repeats (#405), + the per-layer **conformance** map;
- **content quality** — `meets_quota` (bool / `None`) + `entity_counts`, recorded for
  cases that declare a `min_entities` quota (see above); `None` otherwise;
- **the manuscript's MIT axis** (#474, [`scorers.py`](scorers.py)) —
  `mit_propertyid`: per-parameter MIT coverage joined via `schema:propertyID`
  against the ontology IRIs curated in `mit/invitro_tox.yaml`, counting only
  non-empty non-placeholder bindings; the denominator is the IRI-bearing
  parameters, and `mit_propertyid_detail` names which bound so a zero is
  diagnosable (an exact-IRI join: where the crate and the MIT YAML curate
  different terms for one concept, the parameter honestly reads unbound).
  It is additive and never touches `success`; it surfaces per repeat
  (`mit_propertyid_per_repeat`) and in `compare_reports`;
- **tokens** — input / output / total, summed from the run's `profile.ndjson`
  (`node_end`/`model` events);
- **latency** — wall-clock seconds for the build;
- **iteration count** and **tool-call count** — also mined from `profile.ndjson`;
- **stop-reason** (#331) — `completed` (the agent self-terminated), `cap_hit` (the
  ReAct loop hit its recursion cap — a valid-at-the-cutoff run, **not** a clean win),
  `error`, or `not_applicable` (the agent does not attempt the case; it is counted
  and named, never averaged in);
- **model + cost** (#331) — `model_name` mined from `profile.ndjson`, and `cost_usd`
  from `eval.metrics.MODEL_PRICES` (or the `--price-input`/`--price-output` override
  for an unlisted model). An unpriced model records `cost_usd = None` — never a
  guessed `0`. Every repeat is priced into `cost_usd_per_repeat`, and their sum is
  the case's `total_cost_usd` — the money actually spent (#401). `cost_usd` itself
  stays repeat #1's, matching the token fields, so existing consumers are unmoved;
- **per-repeat spread** (#335, #400) — `total_tokens_per_repeat`,
  `latency_per_repeat`, `stop_reasons`, `input_tokens_per_repeat` /
  `output_tokens_per_repeat`, plus a `variance` block (`mean`/`min`/`max`/`stdev`
  for tokens and latency). Note `stop_reasons` beside the singular `stop_reason`:
  a case that self-terminated once and hit the cap twice must not read as a clean
  win;
- **transient retries** (#331) — how many transient network/API failures were re-run
  before the result counted (a connection drop / timeout / rate-limit is not a
  build failure);
- **determinism** — a stable SHA-256 hash of the assembled crate `@graph` (volatile
  `datePublished`/`dateModified` stripped, nodes sorted by `@id`) compared across
  repeats; identical ⇒ deterministic. With `repeats == 1`, determinism is `None`.

The aggregate `EvalReport.summary()` reports **`success_rate`** — the share of
*attempted* (case × repeat) builds that reached conformance, **not** repeat #1's
pass rate (#405) — beside **`num_success_all_repeats`** (every repeat conformed) and
**`num_success_any_repeat`** (at least one did), whose gap is where a flaky case
shows up, **mean/median tokens**,
**mean/median latency**, the **determinism rate**, the **stop-reason breakdown**
(`num_completed` / `num_cap_hit` / `num_error`), **`num_not_applicable`** and
**`num_cases_compared`** (the denominator every rate above uses — cases the agent
attempted), **`mean_total_tokens_cv`** (the
run-level "how stochastic" number), and two cost figures: **`total_cost_usd`** — the
run's actual spend, every repeat of every priced case, `None` when no case was priced
— and **`mean_cost_usd_per_repeat`**, the same money divided by `repeats` so runs made
with different `--repeats` stay comparable.

`compare_reports` carries the per-case spread through to the diff, not just the
means (#400) — without it a real tweak is indistinguishable from run-to-run noise on
the stochastic ReAct build, which is the whole reason `--repeats` defaults to 3.

## Report format

`write_report(report, path)` writes one labeled **ndjson** per run: a
`{"record": "case", ...}` line per case carrying every metric, then one
`{"record": "summary", ...}` line with the aggregates. `compare_reports(a, b, ...)`
returns a side-by-side diff — the summaries, a per-case table keyed by `case_id`, and
a `head_to_head` block over the cases every run attempted — so two labeled reports
compare field-for-field.

## Running it

### Live baseline (human-triggered, uses your credentials)

```bash
# Requires an LLM provider configured (VITRO_OPENAI_API_KEY / OPENAI_API_KEY, etc.).
uv run --extra langchain python -m eval --label react-baseline
# -> writes eval_reports/react-baseline.ndjson
```

Then **freeze the baseline** so later runs have a stable comparison point:

```bash
git tag react-baseline      # tags the ReAct baseline commit
git push origin react-baseline
```

### Comparing runs

```bash
uv run --extra langchain python -m eval --label react-gpt-4o --model gpt-4o
# -> writes eval_reports/react-gpt-4o.ndjson
```

Then diff it against the frozen baseline with `compare_reports`. Quote the
`head_to_head` block, which covers only the cases every run attempted, and read the
per-case `variance` / `*_per_repeat` fields before calling a difference real: the
ReAct build is stochastic. The levers worth watching are **clean self-termination**
(`num_cap_hit`), the **determinism rate**, and **cost, tokens, and latency**.

Options: `--label NAME` (default `react-baseline`), `--repeats N` (builds per case;
**default 3** so variance is over more than one or two samples), `--out PATH`,
`--provider`, `--model`, `--api-base`. For a defensible comparison (#331):
`--price-input`/`--price-output` (USD per 1M tokens — price a model not in the built-in
table, e.g. for the paper re-run) and `--max-transient-retries N` (re-run transient
network/API failures before they count; default 2). See `python -m eval --help`.

### Offline tests (CI)

```bash
uv run --extra langchain pytest eval/tests
```

All `eval/tests` are mock-backed and offline. Tests that touch `build_and_validate`
carry a module-level `pytest.mark.timeout(120)`.
