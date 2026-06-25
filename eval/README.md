# `eval/` — A/B evaluation harness (the ReAct → pipeline decision gate)

This package is **task 6** of the deterministic-pipeline migration (AGENTS.md §14.4).
It is the in-repo A/B that AGENTS.md §14.1 says the full cutover is **gated on**: the
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
    def build(self, case: EvalCase) -> BuildOutcome: ...   # state + session_id + error
```

`run_eval(agent_factory, corpus, *, repeats=2)` takes a **zero-arg `agent_factory`**
that returns a fresh `BuildAgent` per repeat (a fresh agent each time so no engine /
session state leaks into the determinism signal). To A/B two architectures you change
only the factory:

| Architecture | Factory | Status |
|--------------|---------|--------|
| ReAct engine (as-built) | `eval.react_factory.make_react_agent_factory()` | implemented |
| Deterministic pipeline | a future `make_pipeline_agent_factory()` (same shape) | when §14 tasks 1–5 land |
| Offline tests | an in-memory mock returning canned `CrateState`s | tests only |

`ReActBuildAgent` wraps the existing LangGraph ReAct loop: it creates a headless
`AgentEngine` (`SimulatedHumanInterface`, so HITL auto-approves), `initialize()`s the
case's input directory (which also assigns the `session_id` and opens this run's
`profile.ndjson`), drives the graph once with the case's prompt, and returns the final
`CrateState`. The model-driving step is injected so the wiring is unit-tested offline.

## The corpus

A small fixed set of crate-build cases ([`corpus.py`](corpus.py)), each declared as
data — all inputs are in-repo and offline:

| `case_id` | `kind` | What it exercises |
|-----------|--------|-------------------|
| `minimal-backbone` | `minimal` | Build a conformant ISA-Tox backbone from a short brief. |
| `structured-svhps21` | `structured` | Scan the in-repo `tests/fixtures/svhps21_input` folder (README + raw/processed CSVs) and build from its structured metadata. |
| `unstructured-conversation` | `unstructured` | No metadata files — the whole crate is elicited from the prompt. |

**Success predicate** (shared, strict): a case succeeds when its crate reaches
`{base, isa, tox}` REQUIRED conformance via `build_and_validate`
(`reaches_isa_tox_conformance`).

## The metrics

Per case, across `repeats` runs ([`metrics.py`](metrics.py), [`runner.py`](runner.py)):

- **success** (bool) + the per-layer **conformance** map;
- **tokens** — input / output / total, summed from the run's `profile.ndjson`
  (`node_end`/`model` events);
- **latency** — wall-clock seconds for the build;
- **iteration count** and **tool-call count** — also mined from `profile.ndjson`;
- **determinism** — a stable SHA-256 hash of the assembled crate `@graph` (volatile
  `datePublished`/`dateModified` stripped, nodes sorted by `@id`) compared across
  repeats; identical ⇒ deterministic. With `repeats == 1`, determinism is `None`.

The aggregate `EvalReport.summary()` reports **success rate**, **mean/median tokens**,
**mean/median latency**, and the **determinism rate**.

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
git tag react-baseline      # tags the pre-migration ReAct commit (AGENTS.md §14.4)
git push origin react-baseline
```

Later, when the deterministic pipeline lands, run the harness with the pipeline factory
under `--label pipeline` and compare the two reports with `compare_reports`.

Options: `--repeats N` (builds per case), `--out PATH`, `--provider`, `--model`,
`--api-base`. See `python -m eval --help`.

### Offline tests (CI)

```bash
uv run --extra langchain pytest eval/tests
```

All `eval/tests` are mock-backed and offline. Tests that touch `build_and_validate`
carry a module-level `pytest.mark.timeout(120)`.
