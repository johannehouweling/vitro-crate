# Slice 6a: Crate Assembly (build_crate)

## What to build

`build_crate(output_path)` — converts the CrateState entity model into a valid `ro-crate-metadata.json` using the existing entity classes in `profiles/models/isa.py` and `profiles/models/tox.py`, and the JSON-LD context in `profiles/context.py`.

This uses the existing `rocrate` library (`ro-crate-py`) to assemble the crate. Each CrateState entity maps to the appropriate `rocrate.model.ContextEntity` subclass. The output is a partial or complete RO-Crate directory at the given output path.

Can be called at any point — partial crates are valid (missing fields just aren't included).

## Acceptance criteria

- [ ] `build_crate(output_path)` creates a directory with `ro-crate-metadata.json`
- [ ] All CrateState entities are represented in the JSON-LD metadata
- [ ] Uses existing `profiles/models/isa.py` and `tox.py` entity classes
- [ ] Uses `profiles/context.py` for JSON-LD context
- [ ] Partial crates are valid — entities with only name are still included
- [ ] File entities (from scanned_files) are included with `@id` matching their path
- [ ] Relationships between entities are preserved (e.g., Study -> Investigation)
- [ ] Tests: build crate from populated CrateState, verify ro-crate-metadata.json structure

## Blocked by

- Slice 1 (CrateState — source of all entity data)
- Slices 4a, 4b, 4c (entities to assemble)