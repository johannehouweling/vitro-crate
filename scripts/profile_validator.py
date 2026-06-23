"""Profile the three-pass SHACL validator to locate its wall-clock bottleneck (#115).

Run:
    uv run python scripts/profile_validator.py [STUDY_CODE]

It builds a representative crate from the VHP4Safety golden fixtures, times each
validation pass (base / isa / tox / all) at both severity gates, and prints a
cProfile breakdown of the dominant tox pass.

FINDINGS (S-VHPS21, roc-validator 0.10, warm best-of-3)
-------------------------------------------------------
Per-pass timing (in-memory ``validate_crate_dict``):

    severity   base    isa     tox     all
    required   0.30s   0.53s   2.67s   3.60s
    optional   0.36s   0.91s   3.13s   4.83s

* The **tox pass dominates** (~74% of the full sweep): it composes the deepest
  inheritance chain (tox-ro-crate -> isa-ro-crate -> ro-crate), i.e. the largest
  combined shapes+ontology graph.
* The dominant cost is **rdflib in-memory graph construction**, NOT owlrl
  reasoning or pyshacl SHACL evaluation. The cProfile top by ``tottime`` is
  entirely rdflib store/term operations:
    - ``rdflib/plugins/stores/memory.py:add``  (~410k calls, ~2.2s cumulative)
    - ``rdflib/term.py:__eq__`` / ``__hash__`` (~1.4M / ~1.2M calls)
    - ``memory.py:triples`` / context bookkeeping
  owlrl/pyshacl do not appear in the cumulative top-40 — inference is not the cost.
* **No effective warm caching**: best-of-3 ≈ each individual call, so the graph is
  re-composed from scratch on every call.
* **Severity gating** (required vs optional) saves ~15-25%. The agent's inner
  build/fix loop already runs at ``required`` via ``build_and_validate`` (the
  dict path's default); the on-disk ``validate`` tool runs the full ``optional``
  report deliberately.

CONCLUSION
----------
The cost is rdflib re-composing the data-independent shapes+ontology graph on
every call. roc-validator exposes no hook to inject a pre-composed graph (see the
rejected cache attempt in #63 / PR #111), and reimplementing its validation loop
would break issue routing / severity / inherited-profile suppression. So the cost
is largely irreducible at our layer. The available safe levers — gate at
``required`` and scope ``profile`` to a single pass — are already applied in the
hot path. The real fix is upstream: a roc-validator API to reuse a compiled
shapes/ontology graph across calls.
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time
from collections.abc import Callable

from builder.tools.builder import assemble_crate
from profiles.validator import validate_crate_dict
from tests.fixtures.vhps_golden_crates import vhps_fixture_state


def _best(fn: Callable[[], object], n: int = 3) -> float:
    """Return the fastest of *n* runs (warm-cache lower bound)."""
    times: list[float] = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return min(times)


def main(study_code: str = "S-VHPS21") -> None:
    state = vhps_fixture_state(study_code)
    crate = assemble_crate(state, None, materialize_payload=False)
    doc = crate.metadata.generate()

    print(f"=== per-pass timing for {study_code} (best of 3) ===")
    for severity in ("required", "optional"):
        cells = []
        for profile in ("base", "isa", "tox", "all"):
            dt = _best(lambda: validate_crate_dict(doc, severity=severity, profile=profile))
            cells.append(f"{profile}={dt:.3f}s")
        print(f"  severity={severity:8s} " + "  ".join(cells))

    print("\n=== cProfile: tox pass @ required (top 15 by tottime) ===")
    profiler = cProfile.Profile()
    profiler.enable()
    validate_crate_dict(doc, severity="required", profile="tox")
    profiler.disable()
    buf = io.StringIO()
    pstats.Stats(profiler, stream=buf).sort_stats("tottime").print_stats(15)
    print(buf.getvalue())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "S-VHPS21")
