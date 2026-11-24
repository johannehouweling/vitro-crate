# CrateState

> The central, serializable data model every tool reads and writes.

`CrateState` is the single source of truth, serialized to JSON for session resume.

```
CrateState {
    session_id, created_at, updated_at: str,  # ISO-8601
    metadata: {
        title, description, accession, release_date, date_modified,
        publisher, creator, contact, license: str | None,
        license_from_deposit: bool,  # READ from the deposit (#535); set_crate_metadata
                                     # will not overwrite it
        funding: [{ funder, identifier }],  # as the deposit states it; either key optional
        grants: { number: { acronym, title, url, doi, programme, funder_ror } },  # CORDIS-confirmed
        people: [{ name, orcid }], organizations: [{ name, ror }],  # identifiers only
        doi, input_path, output_path, exported_at: str | None,
        metadata_only: bool,  # export writes no payload; read by export_crate alone
        profile: str,         # "isa-tox" | "isa" (D18): conformsTo and the layers judged
    },
    # captured through an ALLOWLIST: never an API key or raw base_url; api_host is
    # the hostname alone, because the crate is shareable
    generator: { name, version, url, provider, model, drafter_model, api_host,
                 architecture: "react" | None, settings, usage/cost fields },
    entities: { investigations, studies, assays, lab_processes, protocols, samples,
                molecular_entities, people, organizations, publications, defined_terms,
                property_values, files, aop_entities: [Entity] },
    scanned_files: [{ path, filename, size, mime_type, first_rows,
                      reviewed_by_user, classification }],
    approved_scan_roots: set[str],
    documents: [{ kind, classification, filename, relative_path, score, reasons, preview }],
    # bounded content of documents read, keyed by relative path; session evidence,
    # not a general result cache
    document_evidence: { relative_path: { tool, path, content, truncated, args } },
    validation: {
        base_passed, isa_passed, tox_passed: bool,
        required_issues, should_issues, may_issues: [str],  # byte-stable display
                                     # projection of issue_records; the loop parses them
        issue_records: [{ profile, severity, entity_id, message }],  # #510
        assessed_tiers: set[str], stale_tier_counts,
        payload_checked: bool,           # something looked at the FILES (#530)
        isa_reachability_checked: bool,  # the ISA reachability walk ran (#537)
        input_fingerprint: str,
    },
    mit_assessment: { module_scores: { m: { completed, total } }, overall_score,
        standard_scores: { doc: { completed, total } },  # documents overlap: no sum
        standard_module_scores: { doc: { m: { completed, total } } },
        published_total, published_module_totals, published_standard_totals },
    fair_assessment: { indicator_results, dsm_level },
    checkpoint: { next_actions, completed_checkpoints: [str],
                  reasoning_log: [{"step", "action", "tool", "result", "timestamp"}],
                  iteration_count: int, max_iterations: int, stuck: bool },
    # a recorded answer is a decision: never ask that tier again; survives --resume
    validation_preferences: { "recommended": bool, "optional": bool },
    # answers live in state, not the graph checkpoint, so a rotated thread does
    # not re-ask a question already answered
    user_answers: [{ question, answer }],
    # questions asked with nobody to answer (--unattended); a later answer closes each
    unanswered_questions: [...],
    dsm_answers: { indicator_id: bool },  # --dsm-answers: the depositor's hosting answers
    deposit_structure: { ... },  # studies and assays read from the folder tree
}
```

### Field-Level Completion Metadata

```python
{
    "entity_id": "chem_001", "type": "MolecularEntity",
    "name": "Silychristin A", "identifier": "33889-69-9",
    "_completion": {
        "MolecularEntity:name": {"status": "verified", "source": "user"},
        "MolecularEntity:identifier": {"status": "filled", "source": "lookup"},
        "MolecularEntity:smiles": {"status": "missing", "source": "lookup"},
    },
    "_provenance": {"created_by": "llm", "reviewed_by": null, "lookups_used": ["pubchem"]},
}
```

`source` records the *tier* a value came from, never the service; the service is recorded
once, in `_provenance.lookups_used`.
