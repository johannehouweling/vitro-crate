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
