# Upstream request (draft)

**Target:** [crs4/rocrate-validator](https://github.com/crs4/rocrate-validator)
**Status:** to file
**Related local work:** `builder/tools/validation.py::_cited_iris` implements the rule below on our side, so the behaviour and the measurements are real rather than hypothetical.

Everything below the line is the issue text.

---

## Decide the excluded namespaces from the graph instead of listing them

The entity checks exclude a fixed set of namespaces. From
`profiles/ro-crate/1.2/should/0_entity_metadata.ttl`:

```sparql
# Exclude entities with non-IRI identifiers or those from specific namespaces
FILTER(!STRSTARTS(STR(?this), "http://www.w3.org/"))
FILTER(!STRSTARTS(STR(?this), "https://w3id.org/ro/crate/"))
FILTER(!STRSTARTS(STR(?this), "http://schema.org/"))
FILTER(!STRSTARTS(STR(?this), "http://purl.org/"))
FILTER(!STRSTARTS(STR(?this), "https://bioschemas.org/"))
FILTER(!STRSTARTS(STR(?this), "urn:"))
```

The same block appears in seven shape files, 34 filters in all, at both SHOULD
and MUST severity:

| file | filters |
|---|---:|
| `profiles/ro-crate/1.2/must/6_contextual_entity_metadata.ttl` | 18 |
| `profiles/ro-crate/1.2/should/0_entity_metadata.ttl` | 8 |
| `profiles/ro-crate/1.2/must/4_data_entity_metadata.ttl` | 2 |
| `profiles/ro-crate/1.2/should/6_organization_metadata.ttl` | 2 |
| `profiles/ro-crate/1.2/should/4_data_entity_metadata.ttl` | 1 |
| `profiles/ro-crate/1.2/should/4_dataset_data_entity.ttl` | 1 |
| `profiles/ro-crate/1.1/must/4_data_entity_metadata.ttl` | 2 |

The intent is clearly right: an IRI a crate only cites is not an entity the crate
has to describe.

### What the list cannot express

`http://purl.org/` is excluded, so Dublin Core passes. `http://purl.obolibrary.org/`
is a different host and is not, so every OBO term a crate cites is reported as
missing a type, a name and a description. The same is true for any registry the
list does not name — and every field has its own.

Adding entries one at a time means a release each time, in several files, in two
profile versions. But the harder problem is that some cases cannot be written as
a namespace at all:

```
https://orcid.org                        a scheme, cited by propertyID
https://orcid.org/0009-0000-5074-6239    an author, described in the crate
```

Same prefix, opposite answers. Excluding the prefix silences the authors;
excluding neither reports the scheme. We hit exactly this and could not resolve it
with any list.

### The rule the list is approximating

Whether an IRI is cited or described is visible in the graph. An IRI is a
citation when all of:

- it is not the subject of any triple other than `rdf:type` — the crate says
  nothing about it;
- every triple with it as object uses a property that *references* rather than
  *asserts*: `schema:propertyID`, `csvw:propertyUrl`, `schema:inDefinedTermSet`,
  `dcterms:conformsTo`;
- which, in RDF, also covers IRIs used only as predicates or as types: they have
  no asserting references by construction.

Anything referenced through `schema:author`, `schema:hasPart`, or a domain
property is an entity the crate is talking about, and stays checked — including
an entity the crate references but forgot to describe, which is a real finding
that must survive.

This subsumes the current list. `http://schema.org/name` is only ever a
predicate; `https://bioschemas.org/Sample` is only ever a type; both fall out
without being named. And it needs no addition for a registry nobody has seen.

It replaces an open-ended list of hosts with a closed list of four properties —
spec-defined, and not something each deployment has to extend.

### Measured

We implemented this on our side, over a 293-entity crate, at the RECOMMENDED
gate:

| | |
|---|---:|
| findings before | 211 |
| entity findings kept | **112** |
| citation findings separated | **99** |

The 99 are OBO, EFO and AOP-Wiki terms, plus three identifier schemes
(`https://orcid.org`, `https://pubchem.ncbi.nlm.nih.gov/compound`,
`https://comptox.epa.gov/dashboard/chemical/details`) — none of which the crate
could describe without copying data maintained elsewhere.

Everything that stays reported is a genuine gap: authors missing an email or job
title, organizations, cell lines, the licence, the publication. The eight authors
under `https://orcid.org/…` keep all their findings while the scheme root is
excluded, which is the case no list could handle.

### Two notes

`profiles/ro-crate/1.2/should/6_contextual_entity_metadata.ttl` has no namespace
filters at all, so today a cited IRI is excluded from "must have a name" but still
reported by "should be described in the same @graph". A rule applied once covers
both, where a list has to be copied into each file.

We have not measured the cost of the `NOT EXISTS` over incoming edges on a large
graph. Ours is small. That seems the main thing worth checking before adopting it.

### If the rule is too large a change

A way to extend the existing list from the caller — a `ValidationSettings` field —
would let each deployment name the namespaces its domain cites, without a release
per registry. It would not solve the `orcid.org` case above, but it would solve
most of them.
