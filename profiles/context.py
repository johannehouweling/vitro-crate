"""
ISA-Tox RO-Crate JSON-LD context.

Extracted from generate_crate.py for reuse in crate_builder.py and any other
module that creates an ROCrate with the ISA-Tox profile.
"""

from profiles.ontology_iris import PREFIXES, iri

# Bioschemas splits its vocabulary by kind: types at the bare namespace,
# properties beneath /properties/. Both the ISA-RO-Crate shapes and Bioschemas
# itself use this split, so property IRIs are built from this prefix rather than
# spelled out — a property written at the type namespace is invisible to the
# shapes that ask for it.
BIOSCHEMAS_PROP = "https://bioschemas.org/properties/"

ISA_TOX_CONTEXT: list[dict] = [
    {
        # Use http://schema.org/ (HTTP) to align with the RO-Crate 1.1 context and
        # the rocrate-validator SHACL shapes, which both use the HTTP namespace.
        "@vocab": "http://schema.org/",
        "schema": "http://schema.org/",
        "bioschemas": "https://bioschemas.org/",
        # Bioschemas types
        "LabProcess": "https://bioschemas.org/LabProcess",
        "MolecularEntity": "https://bioschemas.org/MolecularEntity",
        # Retained for compatibility with legacy/curated crates that still type
        # chemicals as ChemicalSubstance or carry a BioChemEntity cell-line node.
        "ChemicalSubstance": "https://bioschemas.org/ChemicalSubstance",
        "LabProtocol": "https://bioschemas.org/LabProtocol",
        "Sample": "https://bioschemas.org/Sample",
        "BioChemEntity": "https://bioschemas.org/BioChemEntity",
        # schema:sampleType — the categorical annotation (a DefinedTerm) on the
        # cell-based test-system Sample (ISA-Tox domain layer).
        "sampleType": "http://schema.org/sampleType",
        # Bioschemas PROPERTIES live under /properties/; only TYPES sit at the
        # bare namespace (https://bioschemas.org/LabProcess resolves, and so does
        # https://bioschemas.org/properties/additionalProperty, while
        # https://bioschemas.org/executesLabProtocol does not). Emitting a
        # property at the type namespace put it on an IRI no shape looks at, so
        # the ISA profile we declare conformance to could not see the protocol
        # link at all — the value was in the crate under a predicate nobody asked
        # about. Same failure mode as the `author`/`creator` alias.
        "processSequence": f"{BIOSCHEMAS_PROP}processSequence",
        "executesLabProtocol": f"{BIOSCHEMAS_PROP}executesLabProtocol",
        # Process parameters are PropertyValue nodes. `parameter` stays on
        # schema:additionalProperty — a real schema.org property, and the path
        # our OWN tox shapes target — while `parameterValue` carries the
        # Bioschemas predicate the ISA profile requires. The builder emits both
        # keys for the same values, because the two profiles we claim ask for
        # the parameters under different predicates and dropping either loses a
        # conformance we advertise.
        "parameterValue": f"{BIOSCHEMAS_PROP}parameterValue",
        "parameter": "http://schema.org/additionalProperty",
        "factorValue": f"{BIOSCHEMAS_PROP}factorValue",
        # schema:object is the real schema.org Action/LabProcess input predicate
        # (https://bioschemas.org/object does not exist); the shapes target schema:object.
        "object": "http://schema.org/object",
        # Friendly alias for a LabProcess's input(s).
        "input": "http://schema.org/object",
        "labEquipment": f"{BIOSCHEMAS_PROP}labEquipment",
        "reagent": f"{BIOSCHEMAS_PROP}reagent",
        "computationalTool": f"{BIOSCHEMAS_PROP}computationalTool",
        # Real schema.org PROPERTIES (the value is a DefinedTerm); previously these keys
        # were mapped to the schema:DefinedTerm *class*, which made every use a nonsense triple.
        "measurementMethod": "http://schema.org/measurementMethod",
        "measurementTechnique": "http://schema.org/measurementTechnique",
        "labProcess": f"{BIOSCHEMAS_PROP}labProcess",
        # Schema.org types (HTTP to match RO-Crate 1.1 context)
        "Dataset": "http://schema.org/Dataset",
        "File": "http://schema.org/MediaObject",
        "CreativeWork": "http://schema.org/CreativeWork",
        "PropertyValue": "http://schema.org/PropertyValue",
        "DefinedTerm": "http://schema.org/DefinedTerm",
        "Person": "http://schema.org/Person",
        "Organization": "http://schema.org/Organization",
        "ScholarlyArticle": "http://schema.org/ScholarlyArticle",
        # Schema.org properties (HTTP)
        "hasPart": "http://schema.org/hasPart",
        # Friendly grouped aliases for hasPart — all expand to the same schema:hasPart,
        # so the RDF graph (and RO-Crate containment) is identical to a single hasPart.
        "studies": "http://schema.org/hasPart",
        "assays": "http://schema.org/hasPart",
        "protocols": "http://schema.org/hasPart",
        "resources": "http://schema.org/hasPart",
        "dataFiles": "http://schema.org/hasPart",
        "intendedUse": "http://schema.org/intendedUse",
        "result": "http://schema.org/result",
        # Friendly alias for a LabProcess's output(s).
        "output": "http://schema.org/result",
        "about": "http://schema.org/about",
        # Friendly alias for the LabProcess list on a Study/Assay (PageTab-aligned;
        # expands to schema:about so containment/validation is unchanged).
        "labProcesses": "http://schema.org/about",
        # Friendly aliases on schema:mentions for the ontology terms a Study/Assay
        # references (AOP-Wiki pathways/events, organism). All expand to schema:mentions.
        "aop": "http://schema.org/mentions",
        "keyEvent": "http://schema.org/mentions",
        "organism": "http://schema.org/mentions",
        # PageTab-aligned: chemicals tested and the biological models used (cell
        # lines). Both expand to schema:mentions; routed in the post-build relabel.
        "chemicals": "http://schema.org/mentions",
        "biologicalModels": "http://schema.org/mentions",
        # Anatomy / tissue context (UBERON terms); expands to schema:mentions.
        "anatomy": "http://schema.org/mentions",
        "name": "http://schema.org/name",
        "description": "http://schema.org/description",
        "identifier": "http://schema.org/identifier",
        "creator": "http://schema.org/creator",
        # `author` is its own schema.org property, NOT an alias for `creator`.
        # This block is the LAST entry in the crate's @context, so it overrides
        # the RO-Crate context that precedes it — aliasing `author` here rewrote
        # every author in the crate to schema:creator, and the ISA shape asking
        # for schema:author then found nothing on an article whose authors were
        # all present and resolvable. Keep the term mapped to itself.
        "author": "http://schema.org/author",
        "publisher": "http://schema.org/publisher",
        "dateCreated": "http://schema.org/dateCreated",
        "datePublished": "http://schema.org/datePublished",
        "dateModified": "http://schema.org/dateModified",
        "releaseDate": "http://schema.org/releaseDate",
        "additionalProperty": "http://schema.org/additionalProperty",
        "additionalType": "http://schema.org/additionalType",
        "derivesFrom": "http://schema.org/isBasedOn",
        "citation": "http://schema.org/citation",
        "license": "http://schema.org/license",
        "funder": "http://schema.org/funder",
        "value": "http://schema.org/value",
        "unitText": "http://schema.org/unitText",
        "propertyID": "http://schema.org/propertyID",
        # External ontology terms
        "DetectionInstrument": iri("BAO:0000697"),
        "catalog number": iri("NCIT:C99286"),
        # --- Terms from lookup result payloads (added to context so every property key
        #     is mapped to a known IRI, as required by compacted JSON-LD) ---
        # Publication / bibliographic terms (from Crossref)
        "alternative": "http://purl.org/dc/terms/alternative",
        "created": "http://purl.org/dc/terms/created",
        "creditText": "http://schema.org/creditText",
        # dcterms:isPartOf — the article is part of the journal. (bibo has only the
        # `Journal` class, no lowercase `journal` property, so the old IRI did not resolve.)
        "journal": "http://purl.org/dc/terms/isPartOf",
        "label": "http://www.w3.org/2000/01/rdf-schema#label",
        "modified": "http://purl.org/dc/terms/modified",
        "page": "http://schema.org/pagination",
        # Cell-line terms (from CELLOSAURUS)
        "accession": "http://schema.org/identifier",
        "biologicalOrganization": "http://schema.org/taxonomicRange",
        "sex": "http://schema.org/gender",
        "species": "http://schema.org/taxonomicRange",
        "taxonomicRange": "http://schema.org/taxonomicRange",
        # OLS / ontology-term metadata (from BAO lookups via OLS API)
        "curie": "http://www.w3.org/2004/02/skos/core#notation",
        "oboId": "http://www.geneontology.org/formats/oboInOwl#id",
        "ontologyName": "http://purl.org/dc/terms/isPartOf",
        "shortForm": "http://www.geneontology.org/formats/oboInOwl#shorthand",
        "synonym": "http://schema.org/alternateName",
        # schema:sameAs — the dereferenceable identity link. Carries the ChEBI
        # ontology IRI (as an @id node) for a compound resolved via the
        # lookup_compound PubChem→ChEBI fallback (Issue #243), so the ChEBI identity
        # is a machine-resolvable reference rather than a bare, context-less key.
        "sameAs": "http://schema.org/sameAs",
        # Chemical identifiers and molecular properties (from CompoundCloud / PubChem)
        "cas": "http://schema.org/identifier",
        # chebiId — the ChEBI CURIE (e.g. "CHEBI:28748") as a schema:identifier,
        # mirroring cas / pubchemCid. The lookup_compound ChEBI fallback emits this
        # (with the IRI on sameAs) instead of the legacy context-less chebi_id/chebi_iri.
        "chebiId": "http://schema.org/identifier",
        "chemblId": "http://schema.org/identifier",
        "dsstoxId": "http://schema.org/identifier",
        "ecNumber": "http://schema.org/identifier",
        "echaInfocardId": "http://schema.org/identifier",
        "formula": "http://schema.org/molecularFormula",
        "inchi": "http://schema.org/inChI",
        "inchikey": "http://schema.org/inChIKey",
        "iupacName": "http://schema.org/iupacName",
        "keggId": "http://schema.org/identifier",
        "mass": "http://schema.org/molecularWeight",
        "pubchemCid": "http://schema.org/identifier",
        "smiles": "http://schema.org/smiles",
        "subclassOf": "http://www.w3.org/2000/01/rdf-schema#subClassOf",
        "wikibaseId": "http://schema.org/identifier",
        # AOP-Wiki types and terms (from the AOP-Wiki lookup)
        "AdverseOutcomePathway": "https://aopwiki.org/ontology/AdverseOutcomePathway",
        "KeyEvent": "https://aopwiki.org/ontology/KeyEvent",
        "KeyEventRelationship": "https://aopwiki.org/ontology/KeyEventRelationship",
        "aopWikiStressorId": "http://schema.org/identifier",
        "downstream_event": "https://aopwiki.org/ontology/downstreamEvent",
        "eventType": "https://aopwiki.org/ontology/eventType",
        "has_adverse_outcome": "https://aopwiki.org/ontology/hasAdverseOutcome",
        "has_key_event": "https://aopwiki.org/ontology/hasKeyEvent",
        "has_key_event_relationship": "https://aopwiki.org/ontology/hasKeyEventRelationship",
        "has_molecular_initiating_event": "https://aopwiki.org/ontology/hasMolecularInitiatingEvent",
        "short_name": "http://schema.org/alternateName",
        "upstream_event": "https://aopwiki.org/ontology/upstreamEvent",
        # --- CSVW (CSV on the Web) for the Exposure condition table + Units (UO) ---
        "csvw": "http://www.w3.org/ns/csvw#",
        "tableSchema": "http://www.w3.org/ns/csvw#tableSchema",
        "columns": "http://www.w3.org/ns/csvw#column",
        "titles": "http://www.w3.org/ns/csvw#title",
        "datatype": "http://www.w3.org/ns/csvw#datatype",
        "propertyUrl": "http://www.w3.org/ns/csvw#propertyUrl",
        "valueUrl": "http://www.w3.org/ns/csvw#valueUrl",
        "UO": PREFIXES["UO"],
    }
]
