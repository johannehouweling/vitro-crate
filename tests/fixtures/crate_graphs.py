"""Serialized ``@graph`` documents shared by the renderer test modules.

Kept here rather than in one test module so the report's graph views and the
interactive explorer are held to the same crate: a fixture that lives inside the
module asserting one rendering is a fixture the other rendering cannot be
compared against.
"""

from __future__ import annotations

from typing import Any


def tabbed_views_graph() -> dict[str, Any]:
    """A small crate that populates every graph view.

    One of each: an Investigation root, two LabProcesses wired input→output, a
    protocol, a cell line and the culture derived from it, a compound reached
    through a condition table, an author with an affiliation, and a cited
    article. Small enough to assert over exhaustively, complete enough that a
    view which drops its content shows up as an empty set rather than a crate
    that never had any.
    """
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "name": "Crate",
                "hasPart": [{"@id": "#table"}],
                "author": [{"@id": "https://orcid.org/0000-0002-1825-0097"}],
            },
            {"@id": "#cells", "@type": "Sample", "name": "Cultured cells"},
            {
                "@id": "#line",
                "@type": "Sample",
                "additionalType": "CellLine",
                "name": "CHO-K1",
                "identifier": "CVCL_0214",
            },
            {
                "@id": "#culture",
                "@type": "LabProcess",
                "additionalType": "CellCulture",
                "name": "Cell culture",
                "input": {"@id": "#line"},
                "output": {"@id": "#cells"},
            },
            {
                "@id": "#exposure",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure step",
                "object": {"@id": "#cells"},
                "result": {"@id": "#table"},
                "executesLabProtocol": {"@id": "#protocol"},
            },
            {
                "@id": "#protocol",
                "@type": "LabProtocol",
                "name": "Exposure protocol",
                "url": "https://www.protocols.io/view/exposure-abc",
            },
            {
                "@id": "#table",
                "@type": ["File", "csvw:Table"],
                "name": "Condition table",
                "about": [{"@id": "#compound"}],
            },
            {"@id": "#compound", "@type": "MolecularEntity", "name": "Aflatoxin B1"},
            {
                "@id": "https://orcid.org/0000-0002-1825-0097",
                "@type": "Person",
                "name": "Josiah Carberry",
                "affiliation": {"@id": "https://ror.org/05gq02987"},
            },
            {
                "@id": "https://ror.org/05gq02987",
                "@type": "Organization",
                "name": "Brown University",
            },
            {
                "@id": "https://doi.org/10.1007/s00204-024-03787-2",
                "@type": "ScholarlyArticle",
                "name": "Two novel in vitro assays for OATP1C1",
                "datePublished": "2024",
                "author": [{"@id": "https://orcid.org/0000-0002-1825-0097"}],
            },
        ]
    }


def plumbing_heavy_graph() -> dict[str, Any]:
    """A crate whose science is outnumbered by its machinery.

    Carries what a real deposit carries besides the experiment: measured
    parameters as ``PropertyValue``, a table's ``csvw`` schema and columns,
    ontology ``DefinedTerm``s, the build's own ``CreateAction`` and
    ``SoftwareApplication``, a licence, a profile it conforms to — plus one
    reference to an entity nobody described (dangling) and one to an entity that
    lives elsewhere (external). Those are the entities the Researcher view is
    for hiding, and the two stubs are what makes node ordering observable.
    """
    return {
        "@context": "https://w3id.org/ro/crate/1.1/context",
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "name": "Plumbing",
                "hasPart": [{"@id": "readme.txt"}, {"@id": "assay.csv"}],
                "license": {"@id": "https://creativecommons.org/licenses/by/4.0/"},
                "conformsTo": {"@id": "https://w3id.org/ro/crate/isa-tox/1.0"},
                "mentions": {"@id": "#run"},
            },
            {"@id": "readme.txt", "@type": "File", "name": "README"},
            {
                "@id": "assay.csv",
                "@type": ["File", "csvw:Table"],
                "name": "Assay results",
                "tableSchema": {"@id": "#schema"},
            },
            {"@id": "#schema", "@type": ["csvw:Schema", "CreativeWork"], "name": "Schema"},
            {
                "@id": "#col_dose",
                "@type": ["csvw:Column", "schema:DefinedTerm"],
                "name": "dose",
                "propertyUrl": {"@id": "http://purl.obolibrary.org/obo/NCIT_C25488"},
            },
            {
                "@id": "http://purl.obolibrary.org/obo/NCIT_C25488",
                "@type": "DefinedTerm",
                "name": "Dose",
            },
            {
                "@id": "#step",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Dosing",
                "object": {"@id": "#sample"},
                "result": {"@id": "assay.csv"},
                "parameter": [{"@id": "#param_dose"}],
                # Nobody describes this one: a reference with no entity behind it.
                "instrument": {"@id": "#missing-instrument"},
            },
            {"@id": "#param_dose", "@type": "PropertyValue", "name": "Dose", "value": "10 uM"},
            {"@id": "#sample", "@type": "Sample", "name": "Primary hepatocytes"},
            {
                "@id": "https://creativecommons.org/licenses/by/4.0/",
                "@type": "CreativeWork",
                "name": "CC BY 4.0",
            },
            {
                "@id": "https://w3id.org/ro/crate/isa-tox/1.0",
                "@type": ["CreativeWork", "Profile"],
                "name": "ISA-Tox RO-Crate Profile",
            },
            {
                "@id": "#run",
                "@type": "CreateAction",
                "name": "vitro-crate build",
                "instrument": {"@id": "#tool"},
            },
            {"@id": "#tool", "@type": "SoftwareApplication", "name": "vitro-crate"},
        ],
    }


def wide_fanout_graph(files: int = 60) -> dict[str, Any]:
    """A crate shaped like a real deposit's file list: one root, many leaves.

    The shape #619 is about. A deposit's root Dataset ``hasPart`` every file it
    carries, and none of those files points anywhere, so a layered layout puts
    the whole list on one rank — a column *files* nodes tall, whatever the rest
    of the crate looks like. Measured on a real 293-entity crate, that is a
    12,100 px layout inside a 620 px canvas.

    Carries a small ISA backbone besides, so the packing has non-leaf nodes on
    the same ranks to keep out of the way of.

    Args:
        files: How many files hang off the root.
    """
    graph: list[dict[str, Any]] = [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
        {
            "@id": "./",
            "@type": "Dataset",
            "name": "A deposit with a long file list",
            "hasPart": [{"@id": f"data/f{i}.csv"} for i in range(files)] + [{"@id": "#assay"}],
            "mentions": [{"@id": "#process"}],
        },
        {
            "@id": "#assay",
            "@type": "Dataset",
            "additionalType": "Assay",
            "name": "Uptake assay",
            "about": [{"@id": "#process"}],
        },
        {
            "@id": "#process",
            "@type": "LabProcess",
            "name": "Exposure",
            "object": [{"@id": "data/f0.csv"}],
            "result": [{"@id": "data/f1.csv"}],
            "agent": [{"@id": "#lab"}],
        },
        {"@id": "#lab", "@type": "Organization", "name": "A lab"},
    ]
    graph += [
        {
            "@id": f"data/f{i}.csv",
            "@type": "File",
            "name": f"f{i}.csv",
            "encodingFormat": "text/csv",
        }
        for i in range(files)
    ]
    return {"@graph": graph}


def process_context_graph() -> dict[str, Any]:
    """A crate where each step says what it is and what it belongs to.

    The shape #626 is about. A LabProcess is reached from its Assay's ``about``
    — an edge pointing *into* the process — and reaches its protocol through
    ``executesLabProtocol``, an edge pointing *out*. Neither is part of the
    material chain the derivation walk follows, so a selection built from that
    walk alone shows the step and its files and never says how it was done or
    which assay it serves.

    Carries a second step that is off the material chain entirely — no inputs,
    no outputs — with a protocol of its own and an assay pointing at it. That
    step is not drawn, so neither is its context; a selector that swept up every
    ``executes`` and ``about`` edge in the crate would draw both and pass a test
    written with only an unreferenced protocol to exclude.
    """
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "An investigation",
                "hasPart": [{"@id": "#assay"}, {"@id": "result.csv"}],
            },
            {
                "@id": "#assay",
                "@type": "Dataset",
                "additionalType": "Assay",
                "name": "Uptake assay",
                "about": [{"@id": "#exposure"}],
            },
            {
                "@id": "#exposure",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
                "object": {"@id": "#cells"},
                "result": {"@id": "result.csv"},
                "executesLabProtocol": {"@id": "#protocol"},
            },
            {"@id": "#protocol", "@type": "LabProtocol", "name": "Exposure protocol"},
            {
                "@id": "#orphan-step",
                "@type": "LabProcess",
                "additionalType": "DataAnalysis",
                "name": "A step on no chain",
                "executesLabProtocol": {"@id": "#unused-protocol"},
            },
            {
                "@id": "#orphan-assay",
                "@type": "Dataset",
                "additionalType": "Assay",
                "name": "An assay for the undrawn step",
                "about": [{"@id": "#orphan-step"}],
            },
            {"@id": "#unused-protocol", "@type": "LabProtocol", "name": "A protocol nothing draws"},
            {"@id": "#cells", "@type": "Sample", "name": "Cultured cells"},
            {
                "@id": "result.csv",
                "@type": "File",
                "name": "result.csv",
                "encodingFormat": "text/csv",
            },
        ]
    }


def aop_linked_graph() -> dict[str, Any]:
    """A crate whose assays say what they are *for*.

    The shape #627 is about. The ISA-Tox profile links an Assay to the key
    events it measures and a Study to the adverse outcome pathway it serves,
    both through ``schema:mentions`` (``profiles/shapes/tox/7_assay_key_event.ttl``
    and ``6_study_aop.ttl``). The Assays view selected the ISA backbone alone, so
    the one thing that says what an assay is for was missing from the view named
    after assays.

    The chain also carries a ``KeyEventRelationship`` — what makes a pathway a
    pathway rather than a bag of events. Nothing ``mentions`` it, so it is the
    case that separates "part of the pathway" from "followed from an assay".

    ``mentions`` is a general relation, so the crate also mentions something that
    is neither — the build's own action, exactly as a real crate does — and it
    must stay out of a view about the science. A second key event is mentioned
    only by an entity outside the ISA backbone, so a rule that followed every
    ``mentions`` edge in the crate rather than the ones leaving an assay or study
    would draw a pathway no assay claims.
    """
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "An investigation",
                "hasPart": [{"@id": "#study"}],
                "mentions": [{"@id": "#build"}],
            },
            {
                "@id": "#study",
                "@type": "Dataset",
                "additionalType": "Study",
                "name": "A study",
                "hasPart": [{"@id": "#assay"}],
                "mentions": [{"@id": "https://aopwiki.org/aops/610"}],
            },
            {
                "@id": "#assay",
                "@type": "Dataset",
                "additionalType": "Assay",
                "name": "Transport assay",
                "mentions": [{"@id": "https://aopwiki.org/events/2258"}],
            },
            {
                "@id": "https://aopwiki.org/aops/610",
                "@type": ["AdverseOutcomePathway", "schema:DefinedTerm"],
                "name": "Decreased thyroid hormone levels in the brain",
            },
            {
                "@id": "https://aopwiki.org/events/2258",
                "@type": ["KeyEvent", "schema:DefinedTerm"],
                "name": "Inhibition, monocarboxylate transporter 8",
            },
            {
                "@id": "#term",
                "@type": "DefinedTerm",
                "name": "An ontology term no assay mentions",
            },
            {
                "@id": "#note",
                "@type": "CreativeWork",
                "name": "A note outside the ISA backbone",
                "mentions": [{"@id": "https://aopwiki.org/events/9999"}],
            },
            {
                "@id": "https://aopwiki.org/events/9999",
                "@type": ["KeyEvent", "schema:DefinedTerm"],
                "name": "A key event no assay measures",
            },
            {
                "@id": "https://aopwiki.org/relationships/4615",
                "@type": ["KeyEventRelationship", "schema:DefinedTerm"],
                "name": "Inhibition, MCT8 → A key event no assay measures",
                "upstream_event": {"@id": "https://aopwiki.org/events/2258"},
                "downstream_event": {"@id": "https://aopwiki.org/events/9999"},
            },
            {"@id": "#build", "@type": "CreateAction", "name": "vitro-crate build"},
        ]
    }


def assay_lane_graph() -> dict[str, Any]:
    """Two assays, each a full material chain, sharing nothing (#686).

    The shape the lane view draws: cell line -> culture -> cultured sample ->
    exposure -> exposed sample -> readout -> raw files -> analysis -> processed
    file, with a protocol under each step and the exposure's compounds hanging
    off its condition table by ``reagent``.

    Two assays rather than one, because a lane must exclude the *other* assay's
    steps, and a fixture with a single assay cannot tell "scoped to this assay"
    apart from "everything on a chain". Since #678 each assay cultures its own
    line, so the two chains are genuinely disjoint.

    Carries the exclusions each rule needs to be tested rather than assumed:

    * ``#spare-protocol`` executes nothing, and its ``#spare-compound`` is a
      reagent of it alone. A compound hop that swept every ``reagent`` edge in
      the crate would draw that compound with no edge to any work.
    * The Study and Investigation are real and populated, so a lane that leaves
      them out is doing so by rule rather than because the crate has none.
    """
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "An investigation",
                "hasPart": [{"@id": "#study"}],
            },
            {
                "@id": "#study",
                "@type": "Dataset",
                "additionalType": "Study",
                "name": "A study",
                "hasPart": [{"@id": "#assay-a"}, {"@id": "#assay-b"}],
            },
            # --- assay A: the lane under test ---------------------------------
            {
                "@id": "#assay-a",
                "@type": "Dataset",
                "additionalType": "Assay",
                "name": "Deiodinase assay",
                "about": [
                    {"@id": "#culture-a"},
                    {"@id": "#exposure-a"},
                    {"@id": "#readout-a"},
                    {"@id": "#analysis-a"},
                ],
            },
            {
                "@id": "#cellline-a",
                "@type": "Sample",
                "additionalType": "CellLineSample",
                "name": "SK-N-AS",
            },
            {
                "@id": "#culture-a",
                "@type": "LabProcess",
                "additionalType": "CellCulture",
                "name": "Culture SK-N-AS",
                "object": {"@id": "#cellline-a"},
                "result": {"@id": "#cultured-a"},
                "executesLabProtocol": {"@id": "#culture-protocol-a"},
            },
            {
                "@id": "#culture-protocol-a",
                "@type": "LabProtocol",
                "name": "Cell culture protocol SK-N-AS",
            },
            {"@id": "#cultured-a", "@type": "Sample", "name": "Cultured (SK-N-AS)"},
            {
                "@id": "#exposure-a",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
                "object": {"@id": "#cultured-a"},
                "result": {"@id": "#exposed-a"},
                "executesLabProtocol": {"@id": "#conditions-a"},
            },
            {
                "@id": "#conditions-a",
                "@type": "LabProtocol",
                "name": "Condition table",
                "reagent": [{"@id": "#compound-a1"}, {"@id": "#compound-a2"}],
            },
            {
                "@id": "#compound-a1",
                "@type": "MolecularEntity",
                "name": "Amiodarone",
                "inChIKey": "IYIKLHRQXLHMJQ-UHFFFAOYSA-N",
            },
            {
                "@id": "#compound-a2",
                "@type": "MolecularEntity",
                "name": "Cisplatin",
                "inChIKey": "LXZZYRPGZAFOLE-UHFFFAOYSA-L",
            },
            {"@id": "#exposed-a", "@type": "Sample", "name": "Exposed (SK-N-AS)"},
            {
                "@id": "#readout-a",
                "@type": "LabProcess",
                "additionalType": "EndpointReadout",
                "name": "Deiodinase readout",
                "object": {"@id": "#exposed-a"},
                "result": [{"@id": "raw/a1.csv"}, {"@id": "raw/a2.csv"}],
                "executesLabProtocol": {"@id": "#readout-protocol-a"},
            },
            {
                "@id": "#readout-protocol-a",
                "@type": "LabProtocol",
                "name": "Deiodinase readout protocol",
            },
            {
                "@id": "raw/a1.csv",
                "@type": "File",
                "name": "a1.csv",
                "encodingFormat": "text/csv",
            },
            {
                "@id": "raw/a2.csv",
                "@type": "File",
                "name": "a2.csv",
                "encodingFormat": "text/csv",
            },
            {
                "@id": "#analysis-a",
                "@type": "LabProcess",
                "additionalType": "DataAnalysis",
                "name": "Deiodinase analysis",
                "object": [{"@id": "raw/a1.csv"}, {"@id": "raw/a2.csv"}],
                "result": {"@id": "processed/a.csv"},
                "executesLabProtocol": {"@id": "#analysis-protocol-a"},
            },
            {
                "@id": "#analysis-protocol-a",
                "@type": "LabProtocol",
                "name": "Deiodinase analysis script",
            },
            {
                "@id": "processed/a.csv",
                "@type": "File",
                "name": "a.csv",
                "encodingFormat": "text/csv",
            },
            # --- assay B: the lane that must NOT bleed in ---------------------
            {
                "@id": "#assay-b",
                "@type": "Dataset",
                "additionalType": "Assay",
                "name": "TH transport assay",
                "about": [{"@id": "#culture-b"}, {"@id": "#exposure-b"}],
            },
            {
                "@id": "#cellline-b",
                "@type": "Sample",
                "additionalType": "CellLineSample",
                "name": "MO3.13",
            },
            {
                "@id": "#culture-b",
                "@type": "LabProcess",
                "additionalType": "CellCulture",
                "name": "Culture MO3.13",
                "object": {"@id": "#cellline-b"},
                "result": {"@id": "#cultured-b"},
                "executesLabProtocol": {"@id": "#culture-protocol-b"},
            },
            {
                "@id": "#culture-protocol-b",
                "@type": "LabProtocol",
                "name": "Cell culture protocol MO3.13",
            },
            {"@id": "#cultured-b", "@type": "Sample", "name": "Cultured (MO3.13)"},
            {
                "@id": "#exposure-b",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
                "object": {"@id": "#cultured-b"},
                "result": {"@id": "#exposed-b"},
                "executesLabProtocol": {"@id": "#conditions-b"},
            },
            {
                "@id": "#conditions-b",
                "@type": "LabProtocol",
                "name": "Condition table",
                "reagent": [{"@id": "#compound-b1"}],
            },
            {
                "@id": "#compound-b1",
                "@type": "MolecularEntity",
                "name": "Thyroxine",
            },
            {"@id": "#exposed-b", "@type": "Sample", "name": "Exposed (MO3.13)"},
            # --- what no rule may sweep up ------------------------------------
            {
                "@id": "#spare-protocol",
                "@type": "LabProtocol",
                "name": "A protocol no step executes",
                "reagent": [{"@id": "#spare-compound"}],
            },
            {
                "@id": "#spare-compound",
                "@type": "MolecularEntity",
                "name": "A compound no step uses",
            },
        ]
    }
