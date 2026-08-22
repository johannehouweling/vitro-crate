"""Score a crate against the published NIH Bridge2AI AI-readiness criteria.

The criteria, their verbatim practice text and the scoring arithmetic come from
``air/criteria.yaml``, generated from two vendored distributions (see
``scripts/gen_air_criteria.py``). This module holds only the local part: how each
crate-assessable criterion is decided from one assembled ``@graph``.

Three properties of the instrument are load-bearing and are reproduced exactly:

* **Seven percentages, no aggregate.** Verbatim: *"We do not score it pass/fail
  overall, but along multiple dimensions … yielding a characteristic readiness
  profile."* :class:`~builder.state.AIRReport` has no total, and adding one would
  re-invent the metric this instrument replaced.
* **Binary sub-criteria, unweighted, unequal denominators.** The worksheet's input
  column is ``Criterion met? (Y=1; N=0)`` and its output is
  ``=(SUM(D4:D7)/COUNTIF(C4:C7, "*"))*100``. Characterization's five criteria are
  worth 20 points each and Pre-model Explainability's three are worth 33.3; that is
  the authors' design, not an artefact.
* **Their denominator has no "not assessed".** ``COUNTIF`` counts label cells, which
  are always present, so a criterion a crate cannot evidence scores zero and drags
  the dimension down. Excluding it — as the DSM's ``COUNT``-not-``COUNTA`` rule does —
  is a **local deviation**, so :func:`air_profile` reports both: ``published_pct`` is
  theirs, ``pct`` is ours.

Ethics, governance and hosting criteria are ``na``: a crate on disk cannot evidence
IRB approval, a data-access committee, a retention policy or an API. They are
reported and never failed — and never auto-passed, which for "consent obtained"
would be the worst output this axis could produce.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from builder.state import AIRReport, CrateState, Entity
from builder.tools.assessment_graph import (
    Graph,
    Verdict,
    as_verdict,
    is_external_iri,
    needs_graph,
    node_types,
    nodes,
    ref_id,
)
from builder.tools.document_discovery import (
    CLASS_PROCESSED_DATA,
    CLASS_RAW_DATA,
    FILE_CLASSES,
    classify_file,
)

logger = logging.getLogger(__name__)

AIR_CRITERIA_PATH = Path(__file__).resolve().parent.parent.parent / "air" / "criteria.yaml"

AirCheck = Callable[[CrateState, "Graph"], "bool | None | Verdict"]

# Hosts that keep software retrievable beyond a project's lifetime — what 1.c means
# by "a sustainable repository". A bare name in a `SoftwareApplication` node is a
# mention, not a deposit.
_SOFTWARE_HOSTS = (
    "github.com", "gitlab.com", "bitbucket.org", "zenodo.org",
    "softwareheritage.org", "codeberg.org", "sourceforge.net", "pypi.org",
    "bioconductor.org", "cran.r-project.org",
)

# Media types a human can open and read — the "linked human-readable document" half
# of 3.a. A CSV of measurements is not documentation.
_DOCUMENT_MEDIA_TYPES = frozenset(
    {"text/markdown", "text/html", "application/pdf", "text/plain",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
)

_PROCESS_TYPES = frozenset({"LabProcess", "CreateAction", "Action", "Computation"})
_SOURCE_TYPES = frozenset(
    {"Sample", "CellLineSample", "BioSample", "BioChemEntity", "MolecularEntity", "CellLine"}
)
_PUBLICATION_TYPES = frozenset({"ScholarlyArticle", "Article", "Publication", "CreativeWork"})
_CHECKSUM_FIELDS = ("sha256", "sha512", "md5", "checksum", "contentChecksum", "spdx:checksum")


def _load_yaml(path: Path) -> dict[str, Any] | None:
    try:
        import yaml

        with path.open() as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError:
        logger.warning("AIR criteria file not found: %s", path)
        return None
    except Exception:  # pragma: no cover - a malformed vendored file
        logger.exception("Could not read AIR criteria: %s", path)
        return None


def _root(graph: Graph) -> dict[str, Any]:
    """The Root Data Entity — what the ``ro-crate-metadata.json`` descriptor is about."""
    all_nodes = nodes(graph)
    for node in all_nodes:
        if str(node.get("@id", "")).endswith("ro-crate-metadata.json"):
            about = ref_id(node.get("about"))
            for candidate in all_nodes:
                if candidate.get("@id") == about:
                    return candidate
    for node in all_nodes:
        if node.get("@id") == "./":
            return node
    return {}


def _of_type(graph: Graph, wanted: frozenset[str] | set[str]) -> list[dict[str, Any]]:
    return [n for n in nodes(graph) if node_types(n) & wanted]


def _state_check(
    fn: Callable[[CrateState], bool],
    evidence: Callable[[CrateState], str] | None = None,
) -> AirCheck:
    """Adapt a CrateState-only check to the shared ``(state, graph)`` shape.

    *evidence* only describes what was found; the verdict still comes from *fn*, so a
    criterion can be answered by another instrument's function and still say why —
    without a second implementation quietly drifting away from the first.

    The wrapped function is kept reachable so a test can prove that a criterion
    declaring ``overlap_kind: shared-check`` really does call the other instrument's
    function, rather than a look-alike of its own.
    """

    def _wrapped(state: CrateState, _graph: Graph) -> Verdict:
        return Verdict(fn(state), evidence(state) if evidence else "")

    setattr(_wrapped, "__wrapped_check__", fn)  # noqa: B010
    return _wrapped


# ---------------------------------------------------------------------------
# 0 — FAIRness
# ---------------------------------------------------------------------------


def _check_repository_deposit(state: CrateState, graph: Graph) -> Verdict | None:
    """0.a — deposited in a searchable FAIR-compliant repository.

    Which repository holds a crate is not a property of the crate, so this reports
    the only trace that survives into it: a persistent, resolvable identifier on the
    Root Data Entity. Scoped ``partial`` for exactly that reason — a DOI evidences a
    deposit, it does not evidence that the repository is FAIR-compliant.
    """
    if needs_graph(graph):
        return None
    root = _root(graph)
    found = [
        str(value)
        for key in ("identifier", "url", "sameAs", "@id")
        for value in ([root.get(key)] if not isinstance(root.get(key), list) else root[key])
        if is_external_iri(value)
    ]
    if found:
        return Verdict(True, f"root carries {len(found)} persistent identifier(s): {found[0]}")
    return Verdict(False, "the root data entity carries 0 resolvable identifiers")


def _check_metadata_standalone(state: CrateState, graph: Graph) -> Verdict | None:
    """0.b — descriptive metadata available separately from the data, in a standard.

    An RO-Crate answers this structurally: ``ro-crate-metadata.json`` is a separate
    schema.org document describing the dataset, readable when the payload is not.
    What it does not guarantee is that the description is *there*, so the root's own
    descriptive properties are what decide it.
    """
    if needs_graph(graph):
        return None
    descriptor = [
        n for n in nodes(graph) if str(n.get("@id", "")).endswith("ro-crate-metadata.json")
    ]
    root = _root(graph)
    described = [key for key in ("name", "description") if root.get(key)]
    if descriptor and len(described) == 2:
        return Verdict(
            True, "a separate schema.org descriptor describes the root (name, description)"
        )
    missing = (
        "no descriptor entity"
        if not descriptor
        else f"root lacks {2 - len(described)} of name/description"
    )
    return Verdict(False, f"metadata is not separably descriptive: {missing}")


def _check_formal_specification(state: CrateState, graph: Graph) -> Verdict | None:
    """0.c — data and metadata expressed in formally defined specifications.

    JSON-LD is the serialisation; the assessable part is whether the objects in it
    are actually *typed*, since an untyped node is a JSON blob wearing a linked-data
    file extension.
    """
    if needs_graph(graph):
        return None
    all_nodes = nodes(graph)
    typed = [n for n in all_nodes if node_types(n)]
    context = isinstance(graph, dict) and bool(graph.get("@context"))
    ok = len(typed) == len(all_nodes) and bool(all_nodes)
    return Verdict(
        ok,
        f"{len(typed)}/{len(all_nodes)} nodes carry a formal @type"
        + ("; a JSON-LD @context is declared" if context else ""),
    )


# ---------------------------------------------------------------------------
# 1 — Provenance
# ---------------------------------------------------------------------------


def _check_data_sources_identified(state: CrateState, graph: Graph) -> Verdict | None:
    """1.a — data sources traceable to a ground truth (here: the biological material).

    ``partial``: the criterion asks whether the source is traceable to something real,
    which no structural check can settle. What is checkable is whether the crate names
    its source material at all and wires it into the work that produced the data.
    """
    if needs_graph(graph):
        return None
    sources = _of_type(graph, _SOURCE_TYPES)
    processes = _of_type(graph, _PROCESS_TYPES)
    consumed = {
        ref_id(value)
        for process in processes
        for key in ("object", "input", "instrument")
        for value in _listed(process.get(key))
    }
    wired = [s for s in sources if s.get("@id") in consumed]
    ok = bool(sources) and bool(wired)
    return Verdict(
        ok,
        f"{len(sources)} source entities, {len(wired)} of them consumed by a process",
    )


def _check_transformation_steps_wired(state: CrateState, graph: Graph) -> Verdict | None:
    """1.b — key data transformation steps identified, machine-readably.

    A process node with neither an input nor an output has been *minted*, not
    *identified*: nothing can be traced through it. So every step must be wired, not
    merely present.
    """
    if needs_graph(graph):
        return None
    processes = _of_type(graph, _PROCESS_TYPES)
    if not processes:
        return Verdict(False, "0 process steps in the crate — nothing to trace")
    wired = [p for p in processes if any(p.get(k) for k in ("object", "result", "input", "output"))]
    return Verdict(
        len(wired) == len(processes),
        f"{len(wired)}/{len(processes)} process steps wire an input or an output",
    )


def _check_software_in_repository(state: CrateState, graph: Graph) -> Verdict | None:
    """1.c — the software behind the transformations is in a sustainable repository."""
    if needs_graph(graph):
        return None
    software = _of_type(
        graph, {"SoftwareSourceCode", "SoftwareApplication", "ComputationalWorkflow"}
    )
    hosted = [
        s
        for s in software
        if any(
            is_external_iri(value) and any(host in ref_id(value) for host in _SOFTWARE_HOSTS)
            for key in ("codeRepository", "url", "sameAs", "downloadUrl", "@id")
            for value in _listed(s.get(key))
        )
    ]
    if not software:
        return Verdict(False, "0 software entities in the crate")
    return Verdict(
        bool(hosted),
        f"{len(hosted)}/{len(software)} software entities resolve to a sustainable repository",
    )


def _check_key_actors_identified(state: CrateState, graph: Graph) -> Verdict | None:
    """1.d — the people *and* organizations responsible, referenced by identifier.

    The criterion names both, and its suggested resources are ORCID and ROR, so a
    name string does not satisfy it. Nothing here is remediable by asking a human:
    D5 routes identifiers through a lookup, so a typed-in ORCID would be discarded.
    """
    if needs_graph(graph):
        return None
    people = _of_type(graph, {"Person"})
    orgs = _of_type(graph, {"Organization"})
    with_orcid = [
        p for p in people if "orcid.org" in f"{p.get('@id', '')}{p.get('identifier', '')}"
    ]
    with_ror = [
        o for o in orgs if "ror.org" in f"{o.get('@id', '')}{o.get('identifier', '')}"
    ]
    return Verdict(
        bool(with_orcid) and bool(with_ror),
        f"{len(with_orcid)}/{len(people)} people carry an ORCID, "
        f"{len(with_ror)}/{len(orgs)} organizations a ROR",
    )


# ---------------------------------------------------------------------------
# 2 — Characterization
# ---------------------------------------------------------------------------


def _check_descriptive_metadata_rich(state: CrateState, graph: Graph) -> Verdict | None:
    """2.a — a detailed abstract, keywords, and subject-specific vocabularies.

    The criterion names three things, so all three decide it and the evidence says
    which are missing — that is what makes the resulting gap answerable.
    """
    if needs_graph(graph):
        return None
    root = _root(graph)
    abstract = str(root.get("description") or root.get("abstract") or "")
    keywords = _listed(root.get("keywords"))
    vocabulary = [
        n
        for n in nodes(graph)
        if "DefinedTerm" in node_types(n) and is_external_iri(n.get("@id"))
    ]
    present = {
        "abstract": bool(abstract),
        "keywords": bool(keywords),
        "vocabularies": bool(vocabulary),
    }
    missing = [name for name, ok in present.items() if not ok]
    return Verdict(
        not missing,
        f"abstract {len(abstract)} chars, {len(keywords)} keywords, "
        f"{len(vocabulary)} resolvable vocabulary terms"
        + (f"; missing: {', '.join(missing)}" if missing else ""),
    )


# ---------------------------------------------------------------------------
# 3 — Pre-model Explainability
# ---------------------------------------------------------------------------


def _check_documentation_template(state: CrateState, graph: Graph) -> Verdict | None:
    """3.a — machine-readable metadata *and* a linked human-readable document.

    ``partial``: whether a document is a domain-appropriate extension of the Gebru
    Datasheets concept is a judgement no structural check can make. What is checkable
    is the pairing the criterion asks for — and, harvested from the checklist this
    axis replaced, whether the experimental work is documented at all rather than
    merely enumerated.
    """
    if needs_graph(graph):
        return None
    documents = [
        n
        for n in nodes(graph)
        if "File" in node_types(n)
        and (
            str(n.get("encodingFormat") or "") in _DOCUMENT_MEDIA_TYPES
            or any(
                word in str(n.get("@id", "")).lower()
                for word in ("readme", "protocol", "methods")
            )
        )
    ]
    protocols = _of_type(graph, {"LabProtocol"})
    described = [p for p in protocols if p.get("description")]
    return Verdict(
        bool(documents) or bool(described),
        f"{len(documents)} human-readable documents linked, "
        f"{len(described)}/{len(protocols)} protocols carry a description",
    )


def _check_linked_publications(state: CrateState, graph: Graph) -> Verdict | None:
    """3.b — appropriate use cases identified, previously published analyses linked.

    ``partial``: a crate has no property that states *inappropriate* use, so only the
    second half is assessable. Reporting the whole criterion on the strength of a
    citation would overstate it, which is why the scope says so.
    """
    if needs_graph(graph):
        return None
    root = _root(graph)
    cited = {ref_id(v) for v in _listed(root.get("citation")) if ref_id(v)}
    articles = [
        n
        for n in nodes(graph)
        if node_types(n) & _PUBLICATION_TYPES
        and (n.get("@id") in cited or "doi.org" in str(n.get("@id", "")))
    ]
    return Verdict(
        bool(cited) or bool(articles),
        f"{len(cited)} citations on the root, {len(articles)} publication entities",
    )


def _check_payload_checksums(state: CrateState, graph: Graph) -> Verdict | None:
    """3.c — integrity of *each* dataset assured, e.g. by a cryptographic hash.

    "Each" is the operative word: one hashed file among twelve does not let a reader
    verify the crate, so every payload file must carry one.
    """
    if needs_graph(graph):
        return None
    files = [n for n in nodes(graph) if "File" in node_types(n)]
    if not files:
        return Verdict(False, "0 payload files — nothing whose integrity could be checked")
    hashed = [f for f in files if any(f.get(key) for key in _CHECKSUM_FIELDS)]
    return Verdict(
        len(hashed) == len(files),
        f"{len(hashed)}/{len(files)} payload files carry a checksum",
    )


# ---------------------------------------------------------------------------
# 5 — Sustainability
# ---------------------------------------------------------------------------


def _check_project_level_links(state: CrateState, graph: Graph) -> Verdict | None:
    """5.d — project-level connections between data components, machine-readably.

    The criterion's own suggested resource is RO-Crate, so this is the one it should
    be easiest to satisfy — and it is not automatic. A crate that mints typed entities
    and never references them is a bag of files with extra JSON, which is precisely
    what this asks about.
    """
    if needs_graph(graph):
        return None
    root = _root(graph)
    parts = _listed(root.get("hasPart"))
    referenced: set[str] = set()
    for node in nodes(graph):
        for key, value in node.items():
            if key in ("@id", "@type"):
                continue
            referenced.update(ref for ref in (ref_id(v) for v in _listed(value)) if ref)
    structural = [
        n
        for n in nodes(graph)
        if n.get("@id") not in ("./",)
        and not str(n.get("@id", "")).endswith("ro-crate-metadata.json")
        and not node_types(n) & {"File", "Dataset"}
    ]
    orphans = [n for n in structural if n.get("@id") not in referenced]
    return Verdict(
        bool(parts) and not orphans,
        f"root lists {len(parts)} parts; {len(orphans)}/{len(structural)} typed "
        "entities are unreferenced",
    )


# ---------------------------------------------------------------------------
# 6 — Computability
# ---------------------------------------------------------------------------


def _check_access_conditions(state: CrateState, graph: Graph) -> Verdict | None:
    """4.d — security requirements for storing and accessing this data are specified.

    The criterion quotes its own answers — *"public"*, *"controlled access only"* —
    so this looks for a stated access condition and nothing else. It deliberately does
    NOT reuse the DSM's ``access_info`` check, which credits a crate for having a
    location, an identity, a licence or any data at all: that is a different question,
    it is true of essentially every crate, and borrowing it would have made the one
    Ethics criterion a crate can evidence read 100% for everyone.

    The remaining three Ethics criteria are `na`. Auto-passing "consent obtained" is
    the worst output this axis could produce, and the honest report of an in vitro
    deposit is that a crate cannot show it either way.
    """
    if needs_graph(graph):
        return None
    stated = [
        f"{node.get('@id')}: {node.get(key)}"
        for node in nodes(graph)
        for key in ("conditionsOfAccess", "accessMode", "usageInfo", "isAccessibleForFree")
        if node.get(key)
    ]
    if stated:
        return Verdict(True, f"{len(stated)} access condition(s) stated, e.g. {stated[0]}")
    return Verdict(
        False,
        "0 entities state conditionsOfAccess — a reader cannot tell whether this data "
        "is public or controlled",
    )


# ---------------------------------------------------------------------------
# 6 — Computability (continued): the data-availability predicate, harvested
# ---------------------------------------------------------------------------

def _file_class(entity: Entity) -> str:
    """What a crate File is: its stamped classification, or read from its name.

    ``attach_files`` stamps every File it places, and ``_deposited_outputs``
    stamps the ones it wires (#591). A File the agent drafted directly may carry
    no role at all, and a crate whose data arrived that way must not read as
    having none — so its name and destination path answer instead, through the
    same classifier rather than a second rule.

    Only a role the classifier itself emits is taken as a class. ``role`` is free
    text — ``draft_file`` stamps whatever the agent passes, and the spine stamped
    ``raw_data``/``processed_data`` before the classification existed, which a
    resumed session carries forever. Read as classes those match neither tier, so
    a crate whose data was all present reported having none.
    """
    if (role := str(entity.fields.get("role") or "")) in FILE_CLASSES:
        return role
    name = str(entity.fields.get("name") or "")
    return classify_file(name, "", str(entity.fields.get("dest_path") or name))[0]



def _check_data_components(state: CrateState, _graph: Graph) -> Verdict:
    """6.d — examples of the data components, to aid understanding of their content.

    ``partial``: the criterion's first half — splits, and what was withheld during
    collection — has no crate property to read. Its second half does: a crate that
    ships its measurements is providing the data components; one that ships only
    protocols and empty template tables is not, whatever its file count says.

    The predicate is inherited verbatim from the invented reproducibility checklist
    this axis replaced, including the #591 refinement that made it correct — a role
    string is only read as a class when the classifier itself emits it, because
    ``draft_file`` stamps whatever the agent passes and a resumed session carries the
    pre-classification spellings forever. That was hard-won and is kept as written.
    """
    files = state.list_entities("File")
    data = [f for f in files if _file_class(f) in (CLASS_RAW_DATA, CLASS_PROCESSED_DATA)]
    return Verdict(
        bool(data),
        f"{len(data)}/{len(files)} files classify as raw or processed data",
    )


def _check_validatable_standard(state: CrateState, graph: Graph) -> Verdict | None:
    """6.a — adherence to documented standards, validatable deterministically.

    A declared ``conformsTo`` profile is what makes the adherence checkable by a
    machine: it names the shapes a validator can run. A crate that conforms to a
    profile without saying so cannot be validated deterministically by a reader.
    """
    if needs_graph(graph):
        return None
    profiles = {
        ref_id(value)
        for node in nodes(graph)
        for value in _listed(node.get("conformsTo"))
        if is_external_iri(value)
    }
    return Verdict(
        bool(profiles),
        f"{len(profiles)} conformsTo profile(s) declared"
        + (f": {sorted(profiles)[0]}" if profiles else ""),
    )


def _listed(value: Any) -> list[Any]:
    """A property's values as a list, whether it held one or many."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# The registry. Criteria that ask a question one of the other two instruments already
# asks call the SAME function — two implementations of one question is how two axes
# come to disagree about one crate. Where the existing check is a presence tautology
# rather than a measurement, this module implements its own and `air/criteria.yaml`
# records the overlap as `same-question` rather than `shared-check`.
def _build_registry() -> dict[str, AirCheck]:
    from builder.tools.fair_assessment import (
        _check_license_present,
        _check_local_data_dictionary,
        _check_non_proprietary_format,
    )

    def _licence_evidence(state: CrateState) -> str:
        licensed = [e for e in state.list_entities() if e.fields.get("license")]
        if not licensed:
            return f"0 of {len(state.list_entities())} entities carry a license"
        return f"{len(licensed)} entities carry a license, e.g. {licensed[0].fields['license']}"

    return {
        "repository_deposit": _check_repository_deposit,
        "metadata_standalone": _check_metadata_standalone,
        "formal_specification": _check_formal_specification,
        "usage_license": _state_check(_check_license_present, _licence_evidence),
        "data_sources_identified": _check_data_sources_identified,
        "transformation_steps_wired": _check_transformation_steps_wired,
        "software_in_repository": _check_software_in_repository,
        "key_actors_identified": _check_key_actors_identified,
        "descriptive_metadata_rich": _check_descriptive_metadata_rich,
        "machine_readable_schema": _check_local_data_dictionary,
        "documentation_template": _check_documentation_template,
        "linked_publications": _check_linked_publications,
        "payload_checksums": _check_payload_checksums,
        "access_conditions": _check_access_conditions,
        "project_level_links": _check_project_level_links,
        "data_components_present": _check_data_components,
        "validatable_standard": _check_validatable_standard,
        "portable_formats": _check_non_proprietary_format,
    }


AIR_CHECKS: dict[str, AirCheck] = _build_registry()


def air_verdicts(
    state: CrateState, air_data: dict[str, Any] | None = None, graph: Graph = None
) -> dict[str, Verdict]:
    """Every criterion's tri-state answer, with the evidence behind it.

    One evaluation pass: the profile, the report and the blockers all read this, so
    they cannot disagree with each other about the same crate.

    A criterion scoped ``na`` answers ``None`` without being evaluated; a scoped one
    whose check has no graph to read also answers ``None``. Neither is a failure —
    "not assessed" and "assessed and failed" are different claims and the report
    keeps them apart.
    """
    if air_data is None:
        air_data = _load_yaml(AIR_CRITERIA_PATH)
    if air_data is None:
        return {}

    verdicts: dict[str, Verdict] = {}
    for criterion in air_data.get("criteria", []):
        ident = str(criterion.get("id") or "")
        if not ident:
            continue
        if criterion.get("scope", "na") == "na":
            verdicts[ident] = Verdict(None, "not assessable from a crate alone")
            continue
        check = AIR_CHECKS.get(str(criterion.get("check") or ""))
        if check is None:
            raise KeyError(
                f"{ident} is scoped {criterion.get('scope')!r} but names the unknown "
                f"check {criterion.get('check')!r}. A silently skipped check reads as "
                "coverage we do not have."
            )
        verdicts[ident] = as_verdict(check(state, graph))
    return verdicts


def air_profile(
    state: CrateState, air_data: dict[str, Any] | None = None, graph: Graph = None
) -> list[dict[str, Any]]:
    """The seven-dimension readiness profile — and deliberately nothing more.

    Per dimension: ``met`` criteria, ``assessed`` (our denominator), ``total`` (the
    published denominator), ``pct`` and ``published_pct``. ``pct`` is ``None`` when
    nothing in the dimension was assessed — "we did not look" and "the crate failed"
    are different claims, and ``0.0`` would state the second.

    ``published_pct`` always divides by ``total``, exactly as the authors' worksheet
    does, so the figure their own spreadsheet would produce is always visible beside
    ours rather than replaced by it.
    """
    if air_data is None:
        air_data = _load_yaml(AIR_CRITERIA_PATH)
    if air_data is None:
        return []

    answers = air_verdicts(state, air_data, graph)
    dimensions = air_data.get("dimensions", {})
    buckets: dict[int, dict[str, Any]] = {}

    for criterion in air_data.get("criteria", []):
        dimension = criterion.get("dimension")
        if not isinstance(dimension, int):
            continue
        bucket = buckets.setdefault(
            dimension,
            {
                "dimension": dimension,
                "name": dimensions.get(dimension, ""),
                "met": 0,
                "assessed": 0,
                "total": 0,
                "pct": None,
                "published_pct": 0.0,
            },
        )
        bucket["total"] += 1
        verdict = answers.get(str(criterion.get("id")))
        if verdict is None or verdict.value is None:
            continue
        bucket["assessed"] += 1
        if verdict.value is True:
            bucket["met"] += 1

    for bucket in buckets.values():
        if bucket["assessed"]:
            bucket["pct"] = round(bucket["met"] / bucket["assessed"] * 100, 1)
        if bucket["total"]:
            bucket["published_pct"] = round(bucket["met"] / bucket["total"] * 100, 1)

    return [buckets[key] for key in sorted(buckets)]


def assess_air_readiness(state: CrateState, *, graph: Graph = None) -> AIRReport:
    """Score *state* against the Bridge2AI criteria, reading the assembled *graph*.

    Args:
        state: The crate state to assess.
        graph: The assembled ``@graph`` (or the whole crate document). Most criteria
            ask about entities and their links, which exist only once the crate is
            assembled — with no graph they answer "not assessed" rather than guessing.

    Returns:
        An :class:`~builder.state.AIRReport`: one entry per published criterion, plus
        the seven-dimension profile. There is no aggregate score, by the authors'
        design.
    """
    air_data = _load_yaml(AIR_CRITERIA_PATH)
    if air_data is None:
        return AIRReport()

    answers = air_verdicts(state, air_data, graph)
    results: list[dict[str, Any]] = []
    for criterion in air_data.get("criteria", []):
        ident = str(criterion.get("id") or "")
        verdict = answers.get(ident, Verdict(None, ""))
        entry: dict[str, Any] = {
            "id": ident,
            "dimension": criterion.get("dimension"),
            "label": criterion.get("label", ""),
            "text": criterion.get("text", ""),
            "scope": criterion.get("scope", "na"),
            "passed": verdict.value,
            "evidence": verdict.evidence,
            "remedy": criterion.get("remedy", {}),
        }
        if criterion.get("check"):
            entry["check"] = criterion["check"]
        if criterion.get("overlaps"):
            entry["overlaps"] = list(criterion["overlaps"])
        results.append(entry)

    return AIRReport(
        criterion_results=results,
        dimensions=air_profile(state, air_data, graph),
    )


def air_blockers(state: CrateState, graph: Graph = None) -> list[tuple[str, str, str]]:
    """``(id, published practice text, evidence)`` for every criterion that failed.

    Only genuine failures — a criterion that was never assessed is absent, so the list
    reads as a fix list rather than as a list of things the instrument cannot see.
    """
    air_data = _load_yaml(AIR_CRITERIA_PATH)
    if air_data is None:
        return []
    answers = air_verdicts(state, air_data, graph)
    return [
        (str(c.get("id")), str(c.get("text", "")), answers[str(c.get("id"))].evidence)
        for c in air_data.get("criteria", [])
        if answers.get(str(c.get("id"))) is not None
        and answers[str(c.get("id"))].value is False
    ]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("assess_air_readiness", assess_air_readiness, takes_state=True)
