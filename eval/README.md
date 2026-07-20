# `eval/` — A/B evaluation harness (the ReAct → pipeline decision gate)

This package is the in-repo A/B evaluation harness the pipeline-default decision was
**gated on** (AGENTS.md §14.1, D15): the
defensible win for this system is *cost, latency, reproducibility, testability,
predictability* — not blanket correctness — so we measure those directly instead of
asserting them.

The harness is **agent-agnostic**: it measures the current prose-prompt ReAct engine
today and a future deterministic pipeline tomorrow, by swapping a single factory. The
corpus, metrics, and report are unchanged across architectures, so two runs diff cleanly.

> **Cost note.** A *live* harness run makes real LLM calls (your configured
> DeepSeek-flash / OpenAI / Anthropic credentials) — that is its purpose. **CI and the
> unit tests are strictly offline**: they exercise the runner / metrics / report /
> determinism logic against a mock agent factory and never touch a model or the network.

## The agent-agnostic factory

The only thing the harness knows about an architecture is the
[`BuildAgent`](agent_api.py) contract:

```python
class BuildAgent(Protocol):
    def build(self, case: EvalCase) -> BuildOutcome: ...   # state + session_id + error + stop_reason
```

`run_eval(agent_factory, corpus, *, repeats=2)` takes a **zero-arg `agent_factory`**
that returns a fresh `BuildAgent` per repeat (a fresh agent each time so no engine /
session state leaks into the determinism signal). To A/B two architectures you change
only the factory:

| Architecture | Factory | Status |
|--------------|---------|--------|
| ReAct engine (as-built) | `eval.react_factory.make_react_agent_factory()` | implemented — **DEFAULT** |
| Deterministic pipeline | `eval.pipeline_factory.make_pipeline_agent_factory()` (same shape) | implemented (opt-in, §14.5) |
| Offline tests | an in-memory mock returning canned `CrateState`s | tests only |

`ReActBuildAgent` wraps the existing LangGraph ReAct loop: it creates a headless
`AgentEngine` behind `eval.hitl.TrustedCorpusHumanInterface` (a headless
`SimulatedHumanInterface` subclass that also **approves scan-root escalations** against
the trusted in-repo corpus fixtures, so the exploring ReAct arm is not refused a fixture
the pipeline arm never has to ask for — a fairness fix, #329), `initialize()`s the
case's input directory (which also assigns the `session_id` and opens this run's
`profile.ndjson`), drives the graph once with the case's prompt, and returns the final
`CrateState`. The model-driving step is injected so the wiring is unit-tested offline.

`PipelineBuildAgent` wraps the deterministic spine (`builder/agents/pipeline.py::run_pipeline`,
AGENTS.md §14.5): same headless-engine + `initialize()` setup, but instead of driving
the model it runs the code-driven spine (scaffold → draft → build_and_validate → bounded
fix loop) once over the engine. It calls **no model** (zero tokens), so it runs in CI for
real; the spine call is injected so the wiring is unit-tested in isolation. **ReAct stays
the default** — the pipeline is an opt-in parallel path until this A/B gate proves it.

## The corpus

A small fixed set of crate-build cases ([`corpus.py`](corpus.py)), each declared as
data — all inputs are in-repo and offline:

| `case_id` | `kind` | What it exercises |
|-----------|--------|-------------------|
| `minimal-backbone` | `minimal` | Build a conformant ISA-Tox backbone from a short brief. |
| `structured-svhps21` | `structured` | Scan the in-repo `tests/fixtures/svhps21_input` folder (README + raw/processed CSVs) and build from its structured metadata. |
| `structured-svhps22` | `structured` | **Entity-drafting** case: scan a *richer* `tests/fixtures/svhps22_input` folder (README naming a compound + cell line + protocol, plus raw/processed CSVs) and draft the full ISA-Tox domain set. Carries a `min_entities` content quota (below). |
| `unstructured-conversation` | `unstructured` | No metadata files — the whole crate is elicited from the prompt. |

**Success predicate** (shared, strict): a case succeeds when its crate reaches
`{base, isa, tox}` REQUIRED conformance via `build_and_validate`
(`reaches_isa_tox_conformance`).

**Content-quality signal** (additive, per-case): conformance only measures whether
the agent *acted* — an almost-empty backbone can pass it. A case may also declare
`min_entities` (a minimum count of domain entities by `@type`); the harness then
records `meets_quota` + `entity_counts` (`meets_entity_quota`) so the A/B can
compare whether the drafted *content* is actually there, not just that the agent
acted. `min_entities` never changes the success predicate — cases without it report
`meets_quota = None`. The `structured-svhps22` case uses it to demand a compound, a
cell line, and both attached data files; `structured-svhps21` (conformance-only)
is deliberately kept as the looser baseline.

## The metrics

Per case, across `repeats` runs ([`metrics.py`](metrics.py), [`runner.py`](runner.py)):

- **success** (bool) + the per-layer **conformance** map;
- **content quality** — `meets_quota` (bool / `None`) + `entity_counts`, recorded for
  cases that declare a `min_entities` quota (see above); `None` otherwise;
- **tokens** — input / output / total, summed from the run's `profile.ndjson`
  (`node_end`/`model` events);
- **latency** — wall-clock seconds for the build;
- **iteration count** and **tool-call count** — also mined from `profile.ndjson`;
- **stop-reason** (#331) — `completed` (the agent self-terminated), `cap_hit` (the
  ReAct loop hit its recursion cap — a valid-at-the-cutoff run, **not** a clean win),
  or `error`. The pipeline always `completed`;
- **model + cost** (#331) — `model_name` mined from `profile.ndjson`, and `cost_usd`
  from `eval.metrics.MODEL_PRICES` (or the `--price-input`/`--price-output` override
  for an unlisted model). An unpriced model records `cost_usd = None` — never a
  guessed `0`;
- **transient retries** (#331) — how many transient network/API failures were re-run
  before the result counted (a connection drop / timeout / rate-limit is not an
  architecture failure);
- **determinism** — a stable SHA-256 hash of the assembled crate `@graph` (volatile
  `datePublished`/`dateModified` stripped, nodes sorted by `@id`) compared across
  repeats; identical ⇒ deterministic. With `repeats == 1`, determinism is `None`.

The aggregate `EvalReport.summary()` reports **success rate**, **mean/median tokens**,
**mean/median latency**, the **determinism rate**, the **stop-reason breakdown**
(`num_completed` / `num_cap_hit` / `num_error`), and **`total_cost_usd`** (summed over
priced cases, `None` when no case was priced).

## Report format

`write_report(report, path)` writes one labeled **ndjson** per run: a
`{"record": "case", ...}` line per case carrying every metric, then one
`{"record": "summary", ...}` line with the aggregates. `compare_reports(a, b, ...)`
returns a side-by-side A/B diff (both summaries plus a per-case table keyed by
`case_id`) so a ReAct report and a pipeline report compare field-for-field.

## Running it

### Live baseline (human-triggered, uses your credentials)

```bash
# Requires an LLM provider configured (VITRO_OPENAI_API_KEY / OPENAI_API_KEY, etc.).
uv run --extra langchain python -m eval --label react-baseline
# -> writes eval_reports/react-baseline.ndjson
```

Then **freeze the baseline** so future pipeline runs have a stable comparison point:

```bash
git tag react-baseline      # tags the ReAct baseline commit (AGENTS.md §14.5)
git push origin react-baseline
```

### Deterministic pipeline run (offline — no credentials, no model)

```bash
# The spine calls no LLM, so this needs no provider configured.
uv run --extra langchain python -m eval --arch pipeline --label pipeline
# -> writes eval_reports/pipeline.ndjson
```

Then diff it against the frozen baseline with `compare_reports`. The pipeline is
expected to beat the ReAct baseline on the D15 levers: **higher conformance**,
**determinism rate 1.0** (identical `@graph` hash across repeats), and **much lower
tokens** (zero — no model) **and latency**.

`--arch react|pipeline` (DEFAULT `react`) selects the factory; ReAct stays the default
so the existing baseline workflow is unchanged. Other options: `--repeats N` (builds per
case; **default 3** so variance is over more than one or two samples), `--out PATH`,
`--provider`, `--model`, `--api-base`. For a fair, defensible A/B (#331):
`--price-input`/`--price-output` (USD per 1M tokens — price a model not in the built-in
table, e.g. for the paper re-run) and `--max-transient-retries N` (re-run transient
network/API failures before they count; default 2). See `python -m eval --help`.

### Offline tests (CI)

```bash
uv run --extra langchain pytest eval/tests
```

All `eval/tests` are mock-backed and offline. Tests that touch `build_and_validate`
carry a module-level `pytest.mark.timeout(120)`.
