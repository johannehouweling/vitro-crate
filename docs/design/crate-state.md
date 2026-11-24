# CrateState

> The central, serializable data model every tool reads and writes.


`CrateState` is the single source of truth. It is serializable to JSON and persists to disk for session resume.

```
CrateState {
    session_id: str, created_at: str, updated_at: str,  # ISO-8601 strings
    metadata: {
        title: str | None, description: str | None, accession: str | None,
        release_date: str | None, date_modified: str | None,
        publisher: str | None, creator: str | None, contact: str | None,
        license: str | None,
        # the licence was READ from the deposit descriptor, not drafted (#535):
        # a depositor's statement is a fact and outranks a drafted guess, so
        # `set_crate_metadata` will not overwrite it
        license_from_deposit: bool,
        # the funding the deposit declares, as it states it; either key may be absent
        funding: [{ funder, identifier }],
        # the grants a lookup confirmed, by the number the deposit states: an EU grant
        # CORDIS holds, with its DOI, CORDIS address and the ROR ID of its funder
        grants: { number: { acronym, title, url, doi, programme, funder_ror } },
        # the people and organisations the deposit identifies, as it states them, and
        # nothing else of them
        people: [{ name, orcid }], organizations: [{ name, ror }],
        input_path: str | None, output_path: str | None,
        exported_at: str | None,  # None until the crate has been written
        # the export writes no payload file (a deposit whose data is not public yet);
        # read by `export_crate` alone, so the session keeps its files
        metadata_only: bool,
        # the profile the crate claims: "isa-tox", or "isa" for a study without an
        # exposure (D18); decides the root's conformsTo and which layers are judged
        profile: str,
    },
    # what produced this crate: application, provider, model(s), architecture,
    # and the run's token / cost / duration usage. Captured through an
    # ALLOWLIST, never a blocklist — never an API key, never a raw `base_url`;
    # `api_host` is the hostname alone, because the crate is shareable
    generator: {
        name, version, url, provider, model, drafter_model, api_host,
        architecture: "react" | None, settings, usage/cost fields,
    },
    entities: {
        investigations: [Entity], studies: [Entity], assays: [Entity],
        lab_processes: [Entity], protocols: [Entity], samples: [Entity],
        molecular_entities: [Entity], people: [Entity], organizations: [Entity],
        publications: [Entity], defined_terms: [Entity],
        property_values: [Entity], files: [Entity], aop_entities: [Entity],
    },
    scanned_files: [{ path, filename, size, mime_type, first_rows,
        reviewed_by_user, classification }],
    approved_scan_roots: set[str],  # user-approved directory roots for file scanning
    # scientific documentation ranked at initialization, read by the ReAct brief
    documents: [{ kind, classification, filename, relative_path, score,
                  reasons, preview }],
    # bounded content captured from documents that were successfully read,
    # keyed by path relative to the approved root. Session evidence, not a
    # general-purpose result cache
    document_evidence: { relative_path: { tool, path, content, truncated, args } },
    validation: {
        base_passed: bool, isa_passed: bool, tox_passed: bool,
        required_issues: [str], should_issues: [str], may_issues: [str],
        # the same findings with structure intact (#510) — the flat lists above
        # are their display projection and stay byte-stable (the ReAct loop
        # parses them); empty on a verdict recorded before the field existed
        issue_records: [{ profile, severity, entity_id, message }],
        assessed_tiers: set[str],
        # whether anything actually looked at the crate's FILES (#530) — the
        # in-memory gate validates a document, where payload checks emit nothing
        payload_checked: bool,
        # whether anything asked which entities the ISA backbone reaches (#537) —
        # the profile's own rules cannot, so a detached entity is skipped not failed
        isa_reachability_checked: bool,
        input_fingerprint: str,
    },
    mit_assessment: { module_scores: { m: { completed, total } }, overall_score,
        # the same parameters bucketed by guidance document (documents overlap,
        # so these do not sum to the checklist total), and each document's
        # bucket split by module
        standard_scores: { doc: { completed, total } },
        standard_module_scores: { doc: { m: { completed, total } } } },
    fair_assessment: { indicator_results, dsm_level },
    checkpoint: { next_actions: [str], completed_checkpoints: [str],
                  reasoning_log: [{"step", "action", "tool", "result", "timestamp"}] },
    # standing answers to "run the broader validation tiers?". A recorded
    # answer means the user has DECIDED — never ask that tier again, and the
    # decision survives `--resume`
    validation_preferences: { "recommended": bool, "optional": bool },
    # the durable half of the HITL record. A tool-result answer lives only in
    # the graph checkpoint, so a rotated thread loses it and the agent re-asks
    # a question already answered; answers belong in state
    user_answers: [{ question, answer }],
    iteration_count: int, max_iterations: int, stuck: bool,
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
    "_provenance": {
        "created_by": "llm", "reviewed_by": null, "lookups_used": ["pubchem"],
    }
}
```

`source` records the *tier* a value came from, never the service: which service
answered is recorded once, in `_provenance.lookups_used`.
