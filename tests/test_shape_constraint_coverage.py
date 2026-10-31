"""Every tox shape constraint has a fixture that trips it (#823).

A SHACL constraint that never fires is indistinguishable from one that is
mis-targeted, mis-pathed, or silently skipped by the engine. This test reads the
constraints straight from ``profiles/shapes/tox`` and, for each one, perturbs a
conforming crate the way that constraint forbids:

* ``sh:minCount`` / ``sh:qualifiedMinCount`` -> remove the property
* ``sh:maxCount``                            -> add a second value
* ``sh:class`` (alone)                       -> point at a node of another type
* ``sh:in`` / ``sh:or``                      -> an IRI outside the list
* the root's SPARQL rule                     -> unlink the Exposure from the chain

and asserts the constraint's own ``sh:message`` is reported, at its declared
severity, by the production ISA-Tox pass (``validate_crate_dict``). A constraint
that gains a kind with no recipe here fails loudly rather than going untested.

The crate is built by the real builder from a ``CrateState`` that reaches every
tox class, with three nodes the builder does not write added by hand: a
co-culture Sample, and a DOI and a PubMedID identifier. The fixture itself must
pass clean at recommended severity, so every finding below is the perturbation.
"""

from __future__ import annotations

import copy
import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import RDF, SH

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate
from profiles.validator import validate_crate_dict

pytestmark = pytest.mark.timeout(900)

_SHAPES_DIR = Path(__file__).resolve().parent.parent / "profiles" / "shapes" / "tox"
_SCHEMA = "http://schema.org/"

CO_CULTURE = "http://purl.obolibrary.org/obo/NCIT_C93168"
OBI_DOI = "http://purl.obolibrary.org/obo/OBI_0002110"
OBI_PUBMED = "http://purl.obolibrary.org/obo/OBI_0001617"

# The protocol types that classify a step (isa_tox.md, "selected by the intendedUse"),
# each with the legacy additionalType label a step may carry instead.
EXPOSURE = "http://www.ebi.ac.uk/efo/EFO_0003969"
_STEP_LABELS = {
    "http://www.ebi.ac.uk/efo/EFO_0003789": "TestSystemPreparation",
    "http://purl.obolibrary.org/obo/OBI_0001152": "TestSystemPreparation",
    "http://purl.obolibrary.org/obo/GO_0030154": "TestSystemPreparation",
    EXPOSURE: "Exposure",
    "http://www.bioassayontology.org/bao#BAO_0000523": "EndpointReadout",
    "http://purl.obolibrary.org/obo/OBI_0200000": "DataAnalysis",
}
_SEVERITY = {SH.Violation: "required", SH.Warning: "recommended", SH.Info: "optional"}


# --- the cases, read from the shapes ----------------------------------------


@dataclass(frozen=True)
class Case:
    shape: str
    message: str
    severity: str
    kind: str
    path: str | None
    target: tuple[str, str]  # ("class", local name) | ("subjectsOf"|"objectsOf", IRI)

    def __str__(self) -> str:
        return f"{self.shape}:{(self.path or 'node').rsplit('/', 1)[-1].rsplit('#', 1)[-1]}"


def _local(iri) -> str:
    return str(iri).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _severity(g: Graph, node) -> str:
    return _SEVERITY[URIRef(str(g.value(node, SH.severity) or SH.Violation))]


def _kind(g: Graph, prop) -> str:
    for key in (SH.minCount, SH.qualifiedMinCount):
        if (value := g.value(prop, key)) is not None and int(value) >= 1:
            return "remove"
    if g.value(prop, SH.maxCount) is not None:
        return "add_extra"
    if g.value(prop, SH["class"]) is not None:
        return "wrong_type"
    if g.value(prop, SH["in"]) is not None or g.value(prop, SH["or"]) is not None:
        return "unknown_value"
    return "unsupported"


def _cases() -> list[Case]:
    g = Graph()
    for ttl in sorted(_SHAPES_DIR.glob("*.ttl")):
        g.parse(ttl, format="turtle")
    cases: list[Case] = []
    for shape in sorted(g.subjects(RDF.type, SH.NodeShape), key=str):
        if not isinstance(shape, URIRef):
            continue  # classifier conditions and nested value shapes
        if (cls := g.value(shape, SH.targetClass)) is not None:
            target = ("class", _local(cls))
        elif (p := g.value(shape, SH.targetSubjectsOf)) is not None:
            target = ("subjectsOf", str(p))
        elif (p := g.value(shape, SH.targetObjectsOf)) is not None:
            target = ("objectsOf", str(p))
        elif (t := g.value(shape, SH.target)) is not None:
            # A sh:SPARQLTarget selects the focus node from the data instead of
            # from an inferred class. Every one here is `?this <pred> <value>`,
            # so the pair it keys on is recoverable without parsing SPARQL —
            # without this branch the shape is dropped and goes untested.
            # Each target states its focus node as concrete <predicate> <value>
            # pairs; a triple whose object is a variable (the hop to a step's
            # protocol record) has none and is skipped. The first predicate is the
            # discriminating one, and a target may offer alternative values for it.
            pairs = re.findall(
                r'<([^>]+)> (?:<([^>]+)>|"([^"]+)")',
                str(g.value(t, SH.select) or ""),
            )
            if not pairs:
                continue
            pred = pairs[0][0]
            values = [iri or lit for p, iri, lit in pairs if p == pred]
            target = ("sparql", f"{_local(URIRef(pred))}={'|'.join(values)}")
        else:
            continue
        for prop in g.objects(shape, SH.property):
            if (message := g.value(prop, SH.message)) is None:
                continue
            path = g.value(prop, SH.path)
            cases.append(
                Case(
                    _local(shape),
                    str(message),
                    _severity(g, prop),
                    _kind(g, prop),
                    str(path) if isinstance(path, URIRef) else None,
                    target,
                )
            )
        for rule in g.objects(shape, SH.sparql):
            message = str(g.value(rule, SH.message))
            cases.append(Case(_local(shape), message, _severity(g, rule), "sparql", None, target))
    return cases


CASES = _cases()


# --- the conforming crate ---------------------------------------------------


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _state() -> CrateState:
    s = CrateState()
    s.add_entity(
        _ent(
            "inv_1",
            "Investigation",
            name="Thyroid NAM study",
            description="Deiodinase inhibition in neural cells",
        )
    )
    s.add_entity(_ent("aop_1", "AdverseOutcomePathway", name="AOP 42"))
    s.add_entity(_ent("ke_1", "KeyEvent", name="KE 1234"))
    s.add_entity(_ent("study_1", "Study", name="Deiodinase study", aop="aop_1"))
    s.add_entity(
        _ent("assay_1", "Assay", name="Deiodinase Assay", key_event="ke_1", study_id="study_1")
    )
    s.add_entity(
        _ent(
            "chem_1",
            "MolecularEntity",
            name="Iopanoic acid",
            identifier="96-83-3",
            inchikey="XQZYPMVTSDWCCE-UHFFFAOYSA-N",
            smiles="CCC(C(=O)O)c1c(I)cc(I)c(N)c1I",
        )
    )
    s.add_entity(_ent("cell_a", "CellLineSample", name="SK-N-AS", accession="CVCL_1700"))
    s.add_entity(
        _ent(
            "cell_p",
            "CellLineSample",
            name="kidney tubuloids, donor T19",
            source_kind="primary cells",
            taxonomicRange="Homo sapiens",
            cell_type="renal tubular epithelial cell",
        )
    )
    s.add_entity(
        _ent(
            "proc_cult",
            "LabProcess",
            name="Culture neural cells",
            process_type="TestSystemPreparation",
            assay_id="assay_1",
            cell_line=["cell_a"],
            culture_medium="CT medium",
        )
    )
    s.add_entity(
        _ent(
            "proc_cult_p",
            "LabProcess",
            name="Culture tubuloids",
            process_type="TestSystemPreparation",
            assay_id="assay_1",
            cell_line=["cell_p"],
            culture_medium="expansion medium",
        )
    )
    s.add_entity(
        _ent(
            "proc_exp",
            "LabProcess",
            name="2-hour D3 activity exposure",
            process_type="Exposure",
            assay_id="assay_1",
            chemicals="chem_1",
            duration="2 hours",
        )
    )
    s.add_entity(_ent("raw_1", "File", name="raw_readout.csv", description="Plate reader export"))
    s.add_entity(
        _ent(
            "proc_read",
            "LabProcess",
            name="Luminescence readout",
            process_type="EndpointReadout",
            assay_id="assay_1",
            detection_instrument="GloMax",
            endpoint="deiodinase activity",
            result="raw_1",
        )
    )
    s.add_entity(
        _ent(
            "script_1",
            "File",
            name="analysis.R",
            dest_path="scripts/analysis.R",
            description="Dose-response fitting",
        )
    )
    s.add_entity(_ent("processed_1", "File", name="ic50.csv", description="Fitted IC50 values"))
    s.add_entity(
        _ent(
            "proc_ana",
            "LabProcess",
            name="IC50 fitting",
            process_type="DataAnalysis",
            assay_id="assay_1",
            software="R drc",
            data_processing="four-parameter logistic fit",
            object="raw_1",
            result="processed_1",
            labprotocol="script_1",
        )
    )
    s.add_entity(_ent("pub_1", "Publication", name="Reference paper", doi="10.1234/paper"))
    return s


def _add_handwritten_nodes(doc: dict) -> None:
    """The three tox targets the builder never writes."""
    graph = doc["@graph"]
    cell_line_id = next(n["@id"] for n in graph if n.get("name") == "SK-N-AS")
    graph += [
        {
            "@id": "#co_culture",
            "@type": "Sample",
            "name": "SK-N-AS and tubuloid co-culture",
            "sampleType": {"@id": CO_CULTURE},
            "derivesFrom": [{"@id": cell_line_id}, {"@id": "#CellLineSample_cell_p"}],
        },
        {"@id": CO_CULTURE, "@type": "DefinedTerm", "name": "co-culture"},
        {
            "@id": "#pv_doi",
            "@type": "PropertyValue",
            "name": "DOI",
            "value": "10.1234/paper",
            "propertyID": {"@id": OBI_DOI},
        },
        {
            "@id": "#pv_pmid",
            "@type": "PropertyValue",
            "name": "PubMedID",
            "value": "12345678",
            "propertyID": {"@id": OBI_PUBMED},
        },
    ]
    article = next(n for n in graph if n.get("@type") == "ScholarlyArticle")
    article["identifier"] = [{"@id": "#pv_doi"}, {"@id": "#pv_pmid"}]


@pytest.fixture(scope="module")
def conforming_doc(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("coverage") / "crate"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert build_crate(_state(), str(out))["success"] is True
    doc = json.loads((out / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    _add_handwritten_nodes(doc)
    return doc


def _tox_issues(doc: dict):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        (result,) = validate_crate_dict(doc, severity="recommended", profile="tox")
    return result.issues


def test_the_fixture_passes_clean(conforming_doc):
    """Guards every case below: a finding there must be the perturbation."""
    issues = _tox_issues(conforming_doc)
    assert issues == [], [(i.entity_id, i.message) for i in issues]


# --- focus nodes and mutations ----------------------------------------------


def _refs(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [v["@id"] for v in values if isinstance(v, dict) and "@id" in v]


def _step_type(doc: dict, node: dict) -> str | None:
    by_id = {n["@id"]: n for n in doc["@graph"]}
    for record_id in _refs(node.get("executesLabProtocol")):
        for term in _refs(by_id.get(record_id, {}).get("intendedUse")):
            return term
    return None


def _focus(doc: dict, case: Case) -> dict:
    graph = doc["@graph"]
    kind, name = case.target
    if kind == "sparql":
        key, _, raw = name.partition("=")
        wanted = set(raw.split("|"))
        if key == "intendedUse":  # a step, reached through its protocol record
            nodes = [n for n in graph if _step_type(doc, n) in wanted]
        else:
            nodes = [
                n
                for n in graph
                if wanted & set(_refs(n.get(key)))
                or (isinstance(n.get(key), str) and n[key] in wanted)
            ]
    elif kind == "subjectsOf":
        key = _local(name)
        nodes = [n for n in graph if key in n]
    elif kind == "objectsOf":
        key = _local(name)
        ids = {r for n in graph for r in _refs(n.get(key))}
        nodes = [n for n in graph if n["@id"] in ids]
    elif name == "RootDataEntity":
        nodes = [n for n in graph if n["@id"] == "./"]
    elif name in ("Study", "Assay"):
        nodes = [n for n in graph if n["@id"].startswith(f"#{name}_")]
    elif name in ("Table", "Column"):
        nodes = [n for n in graph if f"csvw:{name}" in _refs_types(n)]
    else:
        nodes = [n for n in graph if name in _refs_types(n)]
    assert nodes, f"the fixture reaches no {case.target} for {case}"
    return nodes[0]


def _refs_types(node: dict) -> list[str]:
    t = node.get("@type", [])
    return t if isinstance(t, list) else [t]


def _keys_for(doc: dict, iri: str) -> set[str]:
    """Every JSON key the crate's context expands to *iri*."""
    keys = {_local(iri)} if iri.startswith(_SCHEMA) else set()
    for ctx in doc["@context"]:
        if not isinstance(ctx, dict):
            continue
        for key, value in ctx.items():
            expanded = value.get("@id") if isinstance(value, dict) else value
            if expanded == iri:
                keys.add(key)
    return keys


def _mutate(doc: dict, case: Case) -> None:
    node = _focus(doc, case)
    step_term = _step_type(doc, node)  # read before a removal can take the record away
    keys = [k for k in _keys_for(doc, case.path) if k in node] if case.path else []
    if case.kind == "remove":
        assert keys, f"{case}: the focus node carries no {case.path} to remove"
        for key in keys:
            del node[key]
        if "executesLabProtocol" in keys and step_term in _STEP_LABELS:
            # A step is selected by its record's intendedUse, so without the
            # record no tox shape targets it. The legacy additionalType label is
            # the alias the targets still read (isa_tox.md, Conformance).
            node["additionalType"] = _STEP_LABELS[step_term]
    elif case.kind == "add_extra":
        assert keys, f"{case}: the focus node carries no {case.path} to duplicate"
        current = node[keys[0]]
        node[keys[0]] = (current if isinstance(current, list) else [current]) + [
            {"@id": "https://example.org/an-extra-value"}
        ]
    elif case.kind == "wrong_type":
        assert keys, f"{case}: the focus node carries no {case.path} to retype"
        other = next(n for n in doc["@graph"] if "Sample" in _refs_types(n) and n is not node)
        node[keys[0]] = {"@id": other["@id"]}
    elif case.kind == "unknown_value":
        assert keys, f"{case}: the focus node carries no {case.path} to replace"
        node[keys[0]] = {"@id": "https://example.org/not-a-known-term"}
    elif case.kind == "sparql":
        exposure = _focus(doc, Case("", "", "", "", None, ("sparql", f"intendedUse={EXPOSURE}")))
        for n in doc["@graph"]:
            for key in ("hasPart", "about"):
                if key in n:
                    n[key] = [v for v in _as_list(n[key]) if v.get("@id") != exposure["@id"]]
    else:
        pytest.fail(f"{case}: no mutation recipe for a {case.kind} constraint")


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


@pytest.mark.parametrize("case", CASES, ids=str)
def test_each_constraint_fires_on_its_own_violation(conforming_doc, case):
    doc = copy.deepcopy(conforming_doc)
    _mutate(doc, case)
    fired = [(i.message, i.severity) for i in _tox_issues(doc)]
    assert (case.message, case.severity) in fired, (
        f"{case} did not fire after its perturbation; findings were: {fired}"
    )


def test_every_constraint_has_a_case():
    assert len(CASES) >= 40, [str(c) for c in CASES]
    assert all(c.kind != "unsupported" for c in CASES), [
        str(c) for c in CASES if c.kind == "unsupported"
    ]
