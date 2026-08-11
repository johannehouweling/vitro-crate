# Upstream request: let a caller declare the vocabulary namespaces its crate cites

**Target:** [crs4/rocrate-validator](https://github.com/crs4/rocrate-validator)
**Status:** to file
**Local workaround:** `profiles/validator.py::_patch_cited_vocabulary_exemption` — delete once this ships.

## Summary

The base shapes already hold the position that vocabulary a crate merely *cites*
should not be interrogated as though the crate described it. That position is
currently expressed as literal namespace prefixes inside SPARQL targets, so it
covers exactly the vocabularies the authors had in mind and no others.

**The ask is an interface, not an entry:** a way for a caller to declare the
vocabulary namespaces *its* domain cites, so extending the set does not require a
release of this project.

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

The same block recurs throughout the base profile — **34 `FILTER(!STRSTARTS(...))`
clauses across eight shape files**, at SHOULD and MUST severity alike.

## Why a list of literals does not scale

`http://purl.org/` is exempt, so Dublin Core passes. `http://purl.obolibrary.org/`
is a **different host** and is not, so every OBO term a life-science crate cites
is reported as an underdescribed entity — for terms the crate does not own and
cannot describe without copying data that the ontology maintains and versions.

Measured on one real ISA-Tox crate (293 entities):

| | |
|---|---:|
| RECOMMENDED findings | 211 |
| after exempting OBO / EFO / AOP-Wiki | **124** |
| findings on cited vocabulary | 87 → **0** |

The ~20 distinct IRIs behind them include `CHEBI_23367` (molecular entity),
`IAO_0000039` (has measurement unit label), `PATO_0000033` (concentration of),
`NCIT_C60819` (assay), `EFO_0002090` (technical replicate).

Adding those two hosts fixes toxicology. It leaves earth science, astronomy, the
humanities and every other domain to open the same issue for their own registries
— and each one costs a release. The problem is not which namespaces are on the
list; it is that the list is compiled into SPARQL.

## Proposed interface

A `ValidationSettings` field, beside the reporting controls already there
(`disable_inherited_profiles_issue_reporting`, `requirement_severity_only`,
`disable_check_for_duplicates`, the skip-checks list):

```python
settings = ValidationSettings(
    rocrate_uri=...,
    profile_identifier="ro-crate-1.2",
    cited_vocabulary_namespaces=[
        "http://purl.obolibrary.org/obo/",
        "http://www.ebi.ac.uk/efo/",
        "https://aopwiki.org/ontology/",
    ],
)
```

A focus node whose IRI starts with one of these is not reported — the same
outcome the SPARQL filters produce today, decided by the caller who knows which
vocabularies their crate cites.

Three properties this has that adding entries does not:

**It applies uniformly.** `should/6_contextual_entity_metadata.ttl` carries
"Contextual entities SHOULD be referenced by other entities" and "…referenced by
other entities SHOULD be described in the same @graph", and contains **no**
`STRSTARTS` filters at all. So today a cited term is exempt from *must have a
name* but not from *must be described in the same graph* — two checks saying much
the same thing about the same IRIs, disagreeing. A settings-level filter closes
that without anyone having to decide, shape by shape, where the block belongs.

**It is declared where the knowledge is.** Which namespaces a crate cites is a
property of the domain, known by the caller. It is not knowable in advance here.

**It absorbs the current literals.** The 34 clauses could become the default
value of the same setting — one list, in one place, instead of prefixes repeated
across eight files where they can and do drift apart.

## If the interface is not wanted

The minimal change that unblocks life-science crates is two lines in the existing
filter list:

```sparql
FILTER(!STRSTARTS(STR(?this), "http://purl.obolibrary.org/obo/"))
FILTER(!STRSTARTS(STR(?this), "http://www.ebi.ac.uk/efo/"))
```

`purl.obolibrary.org/obo/` covers the OBO Foundry as a whole — CHEBI, IAO, NCIT,
PATO, UO, OBI — so it is one entry per registry rather than per ontology. We would
carry `https://aopwiki.org/ontology/` locally either way; AOP-Wiki is
domain-specific in a way the OBO Foundry is not.

This leaves the `should/6` inconsistency above unaddressed, which is the main
reason we would rather have the interface.

## A note on scoping, either way

Term **paths**, not bare hosts. `https://aopwiki.org/events/2266` is a Key Event
a crate may legitimately fetch, name and describe — ours does — and it should keep
answering these checks like any other entity the crate asserts. Only
`https://aopwiki.org/ontology/` is a citation. An exemption keyed on the bare host
would silence a check that was right to fire.
