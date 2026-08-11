# Upstream request: extend the cited-vocabulary exemption to life-science ontologies

**Target:** [crs4/rocrate-validator](https://github.com/crs4/rocrate-validator)
**Status:** to file
**Local workaround:** `profiles/validator.py::_patch_cited_vocabulary_exemption` — delete once this ships.

## Summary

The base RO-Crate shapes already exempt vocabulary a crate merely *cites* from the
checks that ask an entity for a type, a name, or a description. The exempt list
covers the namespaces a workflow crate cites. It does not cover the ontology
namespaces a life-science crate cites, so those terms are reported as
underdescribed entities — for terms the crate does not own and cannot describe
without duplicating someone else's data.

## Evidence that the exemption is intended

`profiles/ro-crate/1.2/should/0_entity_metadata.ttl`:

```sparql
# Exclude entities with non-IRI identifiers or those from specific namespaces
FILTER (isIRI(?this))
FILTER(!STRSTARTS(STR(?this), "http://www.w3.org/"))
FILTER(!STRSTARTS(STR(?this), "https://w3id.org/ro/crate/"))
FILTER(!STRSTARTS(STR(?this), "http://schema.org/"))
FILTER(!STRSTARTS(STR(?this), "https://schema.org/"))
FILTER(!STRSTARTS(STR(?this), "http://purl.org/"))
FILTER(!STRSTARTS(STR(?this), "https://bioschemas.org/"))
FILTER(!STRSTARTS(STR(?this), "https://github.com/crs4/rocrate-validator/"))
FILTER(!STRSTARTS(STR(?this), "urn:"))
```

The same block appears throughout the base profile — 34 `FILTER(!STRSTARTS(...))`
clauses across eight shape files, at SHOULD and MUST severity alike. The intent
reads unambiguously: an IRI in a vocabulary namespace is a reference, not an
entity the crate is expected to describe.

## The gap

`http://purl.org/` is exempt, so Dublin Core terms pass. `http://purl.obolibrary.org/`
is a **different host** and is not exempt, so every OBO term a crate cites is
reported. The same applies to EFO and to AOP-Wiki's ontology namespace.

Measured on one real ISA-Tox crate (293 entities, 36 AOP nodes):

| | |
|---|---:|
| RECOMMENDED findings before | 211 |
| after exempting the three namespaces below | **124** |
| findings on cited vocabulary | 87 → **0** |

The ~20 distinct IRIs behind those findings include `CHEBI_23367` (molecular
entity), `IAO_0000039` (has measurement unit label), `PATO_0000033`
(concentration of), `NCIT_C60819` (assay), `EFO_0002090` (technical replicate).
Each is maintained, dereferenceable and versioned by its ontology. Copying its
label into the crate duplicates data the crate does not own and goes stale on the
ontology's next release.

## Requested change

Add to the existing filter list, in the same form:

```sparql
FILTER(!STRSTARTS(STR(?this), "http://purl.obolibrary.org/obo/"))
FILTER(!STRSTARTS(STR(?this), "http://www.ebi.ac.uk/efo/"))
```

`purl.obolibrary.org/obo/` covers the OBO Foundry as a whole — CHEBI, IAO, NCIT,
PATO, UO, OBI and the rest — so this is one entry per registry rather than per
ontology.

We also exempt `https://aopwiki.org/ontology/` locally; that one is arguably
ours to carry rather than yours, since AOP-Wiki is domain-specific in a way the
OBO Foundry is not.

Note the term *paths*, not the bare hosts. `https://aopwiki.org/events/2266` is a
Key Event a crate may legitimately describe, and it should keep answering these
checks; only the ontology namespace is a citation.

## A second, separable inconsistency

`should/6_contextual_entity_metadata.ttl` carries these two checks:

- "Contextual entities SHOULD be referenced by other entities."
- "Contextual entities that are referenced by other entities SHOULD be described
  in the same @graph, with at least an RDF type specified."

It contains **no** `STRSTARTS` filters at all — so cited vocabulary is exempt from
"must have a name" but not from "must be described in the same graph". Those two
say much the same thing about the same IRIs. Whether the exemption belongs there
too is a design call for the maintainers, which is why we have not patched it
locally: extending an existing list is one thing, introducing one where you chose
not to have one is another.
