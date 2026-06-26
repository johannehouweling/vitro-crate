# Validator bottleneck profiling and decision (#115)

Forward-looking investigation of the *remaining* three-pass SHACL validation cost,
after the working-directory-walk fix already landed (the `_patch_in_memory_descriptor_id`
note in [AGENTS.md](../AGENTS.md) §"Validator"). The earlier fix removed a ~57s
`rglob` of the CWD; this study profiles what is left and asks where the residual
tox-pass time actually goes, whether severity gating or pass-folding is a safe
lever, and whether process reuse helps.

**Headline:** the issue's premise — "the dominant ~2.5s is library-internal
**inference**" — does **not** hold on the current stack. Measured, the dominant
cost is **per-call profile composition + check-override resolution inside
`rocrate_validator`**, not owlrl inference and not SHACL evaluation. owlrl + SHACL
together are only ~14% of the tox pass; ~86% is graph (re)composition / routing
bookkeeping that the library redoes on every `validate()` call.

## Method

- Crate under test: the golden `S-VHPS21` fixture
  (`tests/fixtures/vhps_golden_crates.py`), a realistic ISA-Tox crate — 29 `@graph`
  nodes, full `tox-ro-crate → isa-ro-crate → ro-crate` inheritance chain.
- Two entry points measured: the in-memory `validate_crate_dict` (the agent's hot
  build/fix path) and the on-disk `validate_crate` (the `validate` tool).
- Wall-clock split obtained with targeted `time.perf_counter` timers around three
  fused boundaries, plus a deterministic `cProfile` trace for call-level
  attribution:
  - **owlrl inference** — wrap `pyshacl.run_type.PySHACLRunType._run_pre_inference`
    (the only `owlrl.DeductiveClosure.expand` call).
  - **SHACL evaluation** — `pyshacl.validate` total minus owlrl.
  - **composition + routing** — pass wall-clock minus the `SHACLValidator.validate`
    wrapper (i.e. everything `rocrate_validator` does *outside* pyshacl: building the
    ontology graph, assembling the shapes registry, resolving check overrides,
    mapping violations back to checks).
- Medians of 7 warm reps (first call discarded). Machine quiet, single process.

**Environment:** macOS-15.7.7 arm64 (Apple Silicon), Python 3.12.2,
`pyshacl` 0.31.0, `roc-validator` 0.11.0, `rocrate` per `uv.lock`. Absolute numbers
are machine-relative (this laptop is ~1.5–2× slower than the box that produced the
issue's "~3.4s" figure, and the library versions are newer); the **splits and
speedup factors below are the portable result**.

## 1. Where the tox time goes (in-memory dict path, median of 7)

| pass | severity | total | owlrl | SHACL eval | compose + route | issues |
|------|----------|------:|------:|-----------:|----------------:|-------:|
| base | required |  990 ms |  44 ms |  835 ms |  110 ms | 1 |
| base | optional | 1619 ms |  44 ms | 1351 ms |  221 ms | 98 |
| isa  | required |  605 ms |  43 ms |  164 ms |  397 ms | 0 |
| isa  | optional | 1016 ms |  46 ms |  171 ms |  798 ms | 14 |
| **tox** | **required** | **2868 ms** | **220 ms** | **222 ms** | **2425 ms** | 0 |
| **tox** | **optional** | **3588 ms** | **294 ms** | **212 ms** | **3082 ms** | 6 |

The tox pass dominates the sweep, exactly as the issue said — but the split is the
opposite of the assumption:

- **owlrl inference: ~0.29 s (~8%)**
- **SHACL evaluation: ~0.21 s (~6%)**
- **composition + routing: ~3.08 s (~86%)**

Caching the parsed SHACL `.ttl` (the lever already rejected in #63/#111) would
touch only the small ontology-parse slice inside composition — it cannot reach the
86%.

### What the 86% actually is (cProfile, deterministic attribution)

cProfile inflates absolute time ~2.5×, but call counts and proportions are exact.
Per tox-pass call, the cumulative cost localizes to **one library method**:

| function | per-call | what it does |
|----------|---------:|--------------|
| `SHACLValidationContext.__set_current_validation_profile__` | **~7.2 s** | per-pass composition (the whole bucket) |
| ↳ `models.py:335 shapes_graph` (rdflib `addN`/`add`) | ~2.5 s | assembling the inheritance-merged **shapes graph** |
| ↳ `requirement.overridden`/`overridden_by` | ~2.3 s | check-**override** resolution across sibling profiles |
| ↳ `get_sibling_profiles` + `__get_specification_property__` | ~5.2 / ~4.0 s | re-traversing/re-querying the profile spec graphs (uncached) |
| ↳ `extend` (shapes-registry merge) | ~1.7 s | merging inherited profile shapes |
| ↳ `__load_ontology_graph__` (ttl parse) | ~0.8 s | parsing inherited ontology `.ttl` |
| `pyshacl.validate` (owlrl `expand` ~0.71 s + SHACL eval ~0.52 s) | ~1.23 s | the actual validation |

So the cost is **library-internal profile bookkeeping that scales with the depth of
the inheritance chain** (tox inherits isa inherits ro-crate, so it pays the most),
recomputed from scratch on every call with no reuse hook — the same "no injectable
pre-built graph" wall that sank PR #111, but for the *composition*, not the parse.

## 2. Severity gating: `required` vs `OPTIONAL`

| | required | optional | OPTIONAL penalty |
|---|---:|---:|---:|
| **dict** full 3-pass | 5813 ms | 8335 ms | **1.43× slower** |
| **disk** full 3-pass | 8203 ms | 10770 ms | **1.31× slower** |
| disk base | 2264 ms | 3264 ms | 1.44× |
| disk isa | 1369 ms | 1649 ms | 1.20× |
| disk tox | 4570 ms | 5856 ms | 1.28× |

Gating at `required` is a **real but modest ~1.3–1.4× win**, *not* the order-of-
magnitude lever the bottleneck would need. It is modest precisely *because* the
dominant cost (composition) is paid regardless of severity — severity only prunes
which violations are collected/reported, not the graph assembly.

`validate_crate_dict` already defaults to `required` (the agent's inner loop is
already on the fast side). The on-disk `validate_crate` hard-codes `OPTIONAL` on all
three passes. Switching the agent-loop disk path to `required` and reserving
`OPTIONAL` for the final export report would save ~1.3×, but that is a
**behaviour decision** (it changes which issues the loop sees) and is left to the
maintainer — recommended, not flipped here.

## 3. Pass-folding feasibility (the key result)

The three passes exist for per-layer issue **routing**: each pass runs with
`disable_inherited_profiles_issue_reporting=True` so it reports only its own layer.
Reading the library confirms that flag only sets `skip_event_notify` — the inherited
checks **still execute** (`requirement.py:280` runs `execute_check` regardless). So
**the single tox pass already runs base+isa+tox checks internally**; the 3-pass
design re-does the inherited composition/inference up to **3× redundantly** purely
to attribute issues to layers.

That makes folding *look* free. We tested it: one tox pass with
`disable_inherited_profiles_issue_reporting=False`, attributing each issue to a
layer via the originating check's profile identifier.

**Attribution works** — every folded issue carries an originating profile id
(`{isa-ro-crate: 14, tox-ro-crate-1.0: 6, ro-crate-1.1: 6}`, 0 missing).

**But the issue set is NOT equivalent:**

| | issues |
|---|---:|
| current 3-pass union (OPTIONAL) | 118 |
| folded single pass (OPTIONAL) | 26 |
| only in 3-pass (would be **lost**) | 98 |
| only in folded (would be **new**) | 6 |
| identical set? | **No** |

The divergence has a precise cause: the dedicated base pass validates against
**`ro-crate-1.2`** (the spec the repo deliberately flipped to in #110), but the
inherited base layer in the bundled `tox → isa → ro-crate` chain is
**`ro-crate-1.1`**. The folded pass therefore emits `ro-crate-1.1_*` checks instead
of `ro-crate-1.2_*` — 98 base-1.2 findings disappear and 6 base-1.1 findings appear.
Folding would silently **downgrade base validation from 1.2 to 1.1** and change the
reported issues. That is a behaviour/coverage change, not a safe refactor.

**Wall-clock if we folded anyway:** 11.5 s → 5.0 s, **~2.3× faster** (OPTIONAL) — the
biggest single lever measured, because it eliminates the redundant inherited
composition. But it is unsafe as-is: it is not result-equivalent and downgrades the
base spec version.

## 4. Process reuse / persistent worker

The composition cost lives in a fresh `ValidationContext` per `validate()` call;
nothing module-level warms after the first call (medians for calls 2–7 are flat — no
decay). A long-lived process therefore **does not amortize** the per-call
composition across a session's many `validate` calls. The only structural lever is
upstream: an injectable, pre-built/pre-inferred shapes+ontology graph (the hook PR
#111 lacked) — but now the missing hook is for the *composed shapes registry +
override resolution*, not just the parsed `.ttl`.

## Recommendation

1. **Do not pursue shape-parse caching or owlrl tuning.** Profiling shows owlrl
   (~8%) and SHACL eval (~6%) are not the bottleneck; the parse is a sub-slice of
   the 86% composition cost. #63/#111's "won't-fix" stands, for a sharper reason:
   the dominant cost is per-call profile composition + override resolution with no
   reuse hook.
2. **Severity gating is worth taking (modest, safe).** Recommend the maintainer
   switch the agent-loop disk path to `required` and reserve `OPTIONAL` for the
   final export report: ~1.3× for free, no coverage change within a layer. Left to
   the maintainer because it changes which issues the loop observes. The dict path
   already defaults to `required`.
3. **Pass-folding is the only large lever (~2.3×) but is NOT safe today.** Folding
   loses result-equivalence and downgrades the base layer from RO-Crate 1.2 to the
   1.1 the bundled chain inherits. It is feasible *for routing* (issues do carry an
   originating profile id), so it becomes safe only if/when the bundled
   `tox/isa → ro-crate` chain is rebased onto `ro-crate-1.2` (tracked in #110) and a
   byte-identical issue-set test passes. Until then, keep the three passes.
4. **File the upstream request** with `crs4/rocrate-validator`: a reusable, injectable
   **composed** shapes-registry / pre-inferred ontology graph so the inheritance
   composition + override resolution isn't recomputed per call. That is the only
   path to an order-of-magnitude win at our layer.

**Net:** no safe self-contained code lever was found that is provably
result-equivalent, so no validator behaviour is changed in this PR. The residual
tox cost is, for now, irreducible at our layer; the realistic gains are a ~1.3×
severity-gating policy change (maintainer decision) and a ~2.3× pass-fold that is
gated on the #110 base-spec rebase.
