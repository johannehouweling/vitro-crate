"""Every tox shape constraint has a fixture that trips it (#823).

A SHACL constraint that never fires is indistinguishable from one that is
mis-targeted, mis-pathed, or silently skipped by the engine. This test reads the
constraints straight from ``profiles/shapes/tox`` and, for each one, perturbs a
conforming crate the way that constraint forbids:

* ``sh:minCount`` / ``sh:qualifiedMinCount`` -> remove the property
* ``sh:maxCount``                            -> add a second value
* ``sh:class`` (alone)                       -> point at a node of another type
* ``sh:in`` / ``sh:or``                      -> an IRI outside the list
* ``sh:pattern`` on a property               -> a value the pattern refuses
* ``sh:pattern`` on the node itself          -> an @id the pattern refuses
* ``sh:or`` of patterns on the node          -> per member: an @id in that member's
                                                namespace its pattern refuses, or a
                                                value its scheme's pattern refuses
* the root's SPARQL rule                     -> unlink the Exposure from the chain

and asserts the constraint's own ``sh:message`` is reported, at its declared
severity, twice: by the production ISA-Tox pass (``validate_crate_dict``), and by
the shape files run alone with pyshacl, which is what anyone outside
rocrate-validator gets. A shape that selects a class only rocrate-validator infers
passes the first and finds nothing in the second. A constraint that gains a kind
with no recipe here fails loudly rather than going untested, and a targeted shape
that yields no case at all fails too.

The crate is built by the real builder from a ``CrateState`` that reaches every
tox class, with the nodes the builder does not write added by hand: a co-culture
Sample, a DOI and a PubMedID identifier, a chemical at its Compound Wiki address,
a grant at its CORDIS address, and a term in each ontology the build does not
already describe. The fixture itself must pass clean at recommended severity, so
every finding below is the perturbation.
"""

from __future__ import annotations

import copy
import json
import re
import warnings
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pyshacl
import pytest
from rdflib import Graph, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF, SH

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate
from profiles.validator import _LOCAL_CONTEXTS, SHAPES_DIR, validate_crate_dict

pytestmark = pytest.mark.timeout(900)

_SHAPES_DIR = SHAPES_DIR / "tox"
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
    # ("class", local name) | ("sparql", "pred=values") | ("root", "./")
    # | ("subjectsOf"|"objectsOf", IRI) | ("namespace", "IRI prefix")
    # | ("select", shape IRI): the shape's own SPARQL target, run over the fixture
    target: tuple[str, str]
    # The member of a node-level sh:or this case is about: the namespace an @id
    # starts with, or the name of an identifier scheme.
    member: str = ""

    def __str__(self) -> str:
        where = (self.path or "node").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        return f"{self.shape}:{where}" + (f"[{self.member}]" if self.member else "")


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
    if g.value(prop, SH.pattern) is not None:
        return "bad_value"
    return "unsupported"


# A target that selects every @id starting with one of several namespaces, and one
# that selects identifier PropertyValues by the name of their scheme.
_NAMESPACE_FILTER = "REGEX(STR(?this)"
_NAME_FILTER = re.compile(r"FILTER\(\?name IN \(([^)]*)\)\)")


def _literal_prefix(pattern: str) -> str:
    """What every string *pattern* accepts starts with: its text up to the first group."""
    out, escaped = [], False
    for char in pattern.removeprefix("^"):
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char in "([":
            break
        else:
            out.append(char)
    return "".join(out)


def _member_cases(g: Graph, shape, message: str, severity: str) -> list[Case]:
    """One case per member of a node-level ``sh:or``.

    Every member must be tested, not the shape once: a typo in one namespace's
    pattern passes a test that perturbs another's.
    """
    cases = []
    for member in Collection(g, g.value(shape, SH["or"])):
        if (pattern := g.value(member, SH.pattern)) is not None:
            start = _literal_prefix(str(pattern))
            cases.append(
                Case(_local(shape), message, severity, "bad_id", None, ("namespace", start), start)
            )
            continue
        props = [(g.value(p, SH.path), g.value(p, SH.hasValue)) for p in g.objects(member, SH.property)]
        named = [(path, value) for path, value in props if value is not None]
        checked = [path for path, value in props if value is None]
        if len(named) == 1 and len(checked) == 1:
            (key, scheme), = named
            cases.append(
                Case(
                    _local(shape),
                    message,
                    severity,
                    "bad_value",
                    str(checked[0]),
                    ("sparql", f"{_local(key)}={scheme}"),
                    str(scheme),
                )
            )
            continue
        cases.append(Case(_local(shape), message, severity, "unsupported", None, ("class", "")))
    return cases


@cache
def _shapes() -> Graph:
    g = Graph()
    for ttl in sorted(_SHAPES_DIR.glob("*.ttl")):
        g.parse(ttl, format="turtle")
    return g


def _cases() -> list[Case]:
    g = _shapes()
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
            select = str(g.value(t, SH.select) or "")
            pairs = re.findall(r'<([^>]+)> (?:<([^>]+)>|"([^"]+)")', select)
            if "ro-crate-metadata.json" in select:
                # The root: the Dataset the metadata descriptor is about.
                target = ("root", "./")
            elif _NAMESPACE_FILTER in select:
                target = ("namespace", "")  # each member names its own namespace
            elif names := _NAME_FILTER.search(select):
                values = re.findall(r'"([^"]+)"', names.group(1))
                target = ("sparql", f"name={'|'.join(values)}")
            elif pairs:
                pred = pairs[0][0]
                values = [iri or lit for p, iri, lit in pairs if p == pred]
                target = ("sparql", f"{_local(URIRef(pred))}={'|'.join(values)}")
            else:
                # A target no recipe above reads (an Organization named only as a
                # funder): its own query picks the focus node from the fixture.
                target = ("select", str(shape))
        else:
            continue
        if g.value(shape, SH.pattern) is not None and (message := g.value(shape, SH.message)):
            # A node-level constraint: the pattern applies to the focus node's own @id.
            cases.append(Case(_local(shape), str(message), _severity(g, shape), "bad_id", None, target))
        if g.value(shape, SH["or"]) is not None and (message := g.value(shape, SH.message)):
            cases += _member_cases(g, shape, str(message), _severity(g, shape))
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
    # Keyed by their AOP-Wiki IRIs, as `resolve_aop` and `link_assay_to_key_event` key them: the
    # AOP and Key Event shapes read the IRI.
    aop, ke = "https://aopwiki.org/aops/42", "https://aopwiki.org/events/1234"
    s.add_entity(_ent(aop, "AdverseOutcomePathway", name="AOP 42"))
    s.add_entity(_ent(ke, "KeyEvent", name="KE 1234"))
    s.add_entity(_ent("study_1", "Study", name="Deiodinase study", aop=aop))
    s.add_entity(
        _ent("assay_1", "Assay", name="Deiodinase Assay", key_event=ke, study_id="study_1")
    )
    # Identified by its PubChem address, with the CAS, PubChem CID and DTXSID
    # PropertyValues the build writes for a chemical. The shapes read the form of
    # an identifier, so these need to be well-formed, not looked up.
    s.add_entity(
        _ent(
            "chem_1",
            "MolecularEntity",
            name="Iopanoic acid",
            identifier="96-83-3",
            cas="96-83-3",
            pubchem_cid="3735",
            dtxsid="DTXSID2023157",
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
    # ISA-Tox identifies a Person by an ORCID iD and an Organization by a ROR ID.
    s.add_entity(
        _ent(
            "person_1",
            "Person",
            name="Jente Houweling",
            givenName="Jente",
            familyName="Houweling",
            orcid="0009-0005-3680-0645",
        )
    )
    s.add_entity(_ent("org_1", "Organization", name="Utrecht University", ror="04pp8hn57"))
    # A funder the crate names only as one, so the funder shape has a node to perturb.
    s.add_entity(_ent("org_2", "Organization", name="Dutch Research Council", ror="04jsz6e67"))
    s.metadata.funding = [{"funder": "Dutch Research Council", "identifier": "1292.19.272"}]
    return s


# A well-formed term in each ontology namespace the build does not describe on its
# own, so every ontology-term shape has a node to perturb.
_ONTOLOGY_TERMS = {
    "http://purl.obolibrary.org/obo/CHEBI_23367": "molecular entity",
    "http://purl.obolibrary.org/obo/CL_0000062": "osteoblast",
    "http://purl.obolibrary.org/obo/GO_0030154": "cell differentiation",
    "http://purl.obolibrary.org/obo/IAO_0000030": "information content entity",
    "http://purl.obolibrary.org/obo/MSIO_0000062": "measurement",
    "http://purl.obolibrary.org/obo/NCBITaxon_9606": "Homo sapiens",
    "http://purl.obolibrary.org/obo/PATO_0000033": "concentration of",
    "http://purl.obolibrary.org/obo/UBERON_0002113": "kidney",
    "http://purl.obolibrary.org/obo/UO_0000064": "micromolar",
}


def _add_handwritten_nodes(doc: dict) -> None:
    """The tox targets the builder never writes."""
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
    # The build identifies a chemical by its Compound Wiki item only when a lookup
    # found one, which this offline fixture does not do.
    graph += [
        {
            "@id": "https://compoundcloud.wikibase.cloud/entity/Q2026",
            "@type": "MolecularEntity",
            "name": "Sodium iodide",
            "identifier": ["7681-82-5", {"@id": "#pv_compoundwiki"}],
            "inChIKey": "FVAUCKIRQBBSSJ-UHFFFAOYSA-M",
            "smiles": "[Na+].[I-]",
        },
        {
            "@id": "#pv_compoundwiki",
            "@type": "PropertyValue",
            "name": "Compound Wiki",
            "value": "Q2026",
            "propertyID": {"@id": "https://compoundcloud.wikibase.cloud/entity"},
        },
        # A grant CORDIS holds and the EU Publications Office gave no DOI is written
        # at its CORDIS address.
        {"@id": "https://cordis.europa.eu/project/id/825161", "@type": "Grant", "name": "ATHENA"},
    ]
    described = {n["@id"] for n in graph}
    graph += [
        {"@id": iri, "@type": "DefinedTerm", "name": name}
        for iri, name in _ONTOLOGY_TERMS.items()
        if iri not in described
    ]


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


_BASE = "https://example.org/crate/"


def _data_graph(doc: dict) -> Graph:
    """The crate as RDF, its relative @ids resolved against ``_BASE``."""
    context = [_LOCAL_CONTEXTS[c]["@context"] if isinstance(c, str) else c for c in doc["@context"]]
    return Graph().parse(
        data=json.dumps({**doc, "@context": context}), format="json-ld", base=_BASE
    )


def _standalone_issues(doc: dict) -> list[tuple[str, str]]:
    """The findings of the shape files run alone: pyshacl over the crate's graph."""
    data = _data_graph(doc)
    _, report, _ = pyshacl.validate(data, shacl_graph=_shapes(), advanced=True)
    return [
        (str(report.value(r, SH.resultMessage)), _SEVERITY[report.value(r, SH.resultSeverity)])
        for r in report.subjects(RDF.type, SH.ValidationResult)
    ]


def test_the_fixture_passes_clean(conforming_doc):
    """Guards every case below: a finding there must be the perturbation."""
    issues = _tox_issues(conforming_doc)
    assert issues == [], [(i.entity_id, i.message) for i in issues]
    assert _standalone_issues(conforming_doc) == []


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
    elif kind == "root":
        nodes = [n for n in graph if n["@id"] == name]
    elif kind == "namespace":
        nodes = [n for n in graph if n["@id"].startswith(name) and "@type" in n]
    elif kind == "select":
        select = str(_shapes().value(_shapes().value(URIRef(name), SH.target), SH.select))
        selected = sorted(str(row[0]).removeprefix(_BASE) for row in _data_graph(doc).query(select))
        nodes = [n for focus in selected for n in graph if n["@id"] == focus]
    elif kind == "subjectsOf":
        key = _local(name)
        nodes = [n for n in graph if key in n]
    elif kind == "objectsOf":
        key = _local(name)
        ids = {r for n in graph for r in _refs(n.get(key))}
        nodes = [n for n in graph if n["@id"] in ids]
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


def _mutate(doc: dict, case: Case) -> str:
    """Perturb *doc* the way *case* forbids; the @id of the node perturbed."""
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
    elif case.kind == "bad_value":
        assert keys, f"{case}: the focus node carries no {case.path} to malform"
        node[keys[0]] = "not well-formed!"
    elif case.kind == "bad_id":
        # Still in the namespace the target selects, so the shape still reaches
        # it; no registry's identifier ends in "!".
        kind, name = case.target
        bad = name + "!" if kind == "namespace" else "#not-a-registry-id"
        _rename(doc, node["@id"], bad)
        return bad
    elif case.kind == "sparql":
        exposure = _focus(doc, Case("", "", "", "", None, ("sparql", f"intendedUse={EXPOSURE}")))
        for n in doc["@graph"]:
            for key in ("hasPart", "about"):
                if key in n:
                    n[key] = [v for v in _as_list(n[key]) if v.get("@id") != exposure["@id"]]
    else:
        pytest.fail(f"{case}: no mutation recipe for a {case.kind} constraint")
    return node["@id"]


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _rename(doc: dict, old: str, new: str) -> None:
    """Give the node *old* the @id *new*, and every reference to it with it."""

    def visit(value):
        if isinstance(value, dict):
            if value.get("@id") == old:
                value["@id"] = new
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(doc["@graph"])


@pytest.mark.parametrize("case", CASES, ids=str)
def test_each_constraint_fires_on_its_own_violation(conforming_doc, case):
    doc = copy.deepcopy(conforming_doc)
    perturbed = _mutate(doc, case)
    issues = _tox_issues(doc)
    fired = [(i.message, i.severity) for i in issues]
    assert (case.message, case.severity) in fired, (
        f"{case} did not fire after its perturbation; findings were: {fired}"
    )
    if case.member:
        # One message speaks for every member, so the finding must be about the
        # node this member's perturbation touched.
        about = {(i.entity_id or "").removeprefix("./") for i in issues if i.message == case.message}
        assert perturbed in about, (case, about)
    alone = _standalone_issues(doc)
    assert (case.message, case.severity) in alone, (
        f"{case} fires under rocrate-validator but not when the shapes run alone; "
        f"findings were: {alone}"
    )


def test_every_constraint_has_a_case():
    assert len(CASES) >= 40, [str(c) for c in CASES]
    assert all(c.kind != "unsupported" for c in CASES), [
        str(c) for c in CASES if c.kind == "unsupported"
    ]


def test_every_targeted_shape_yields_a_case():
    """A shape the case reader cannot parse is not tested, and nothing says so.

    A SPARQL target with no `<predicate> <value>` pair used to be skipped in
    silence, which is how a shape selecting by namespace would have gone
    untested. Every named, targeted shape must give at least one case.
    """
    g = _shapes()
    targets = (SH.targetClass, SH.targetSubjectsOf, SH.targetObjectsOf, SH.target)
    targeted = {
        _local(shape)
        for shape in g.subjects(RDF.type, SH.NodeShape)
        if isinstance(shape, URIRef) and any(g.value(shape, t) is not None for t in targets)
    }
    assert targeted - {c.shape for c in CASES} == set()
