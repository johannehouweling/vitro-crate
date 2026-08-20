"""Case selectors shared by the eval tests.

Lives beside the tests rather than in :mod:`eval.corpus`: the harness itself
never needs to pick a case by shape, so a selector there would be a production
symbol with only test callers.
"""

from __future__ import annotations

from eval.corpus import DEFAULT_CORPUS, EvalCase


def first_folder_case() -> EvalCase:
    """The first corpus case backed by an input directory.

    Both arms attempt a folder-backed case; only the ReAct arm attempts a
    conversational one, because the pipeline is folder-driven by design and
    reports those ``not_applicable`` (#609). A test about shared build wiring
    must therefore pick one of these rather than ``DEFAULT_CORPUS[0]``, which is
    conversational.
    """
    return next(c for c in DEFAULT_CORPUS if c.input_path)
