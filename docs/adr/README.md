# Design decisions

> Each record states one decision, why, and what it requires of the code. Code and the
> design docs cite a record by its id (`D5`). There is no D7.

- [D1: Toolbox over Graph](D1-toolbox-over-graph.md)
- [D2: Three-Tier Validation](D2-three-tier-validation.md)
- [D3: The session state is the persistence format, the crate is its output](D3-crate-as-persistence-format.md)
- [D4: Format-Agnostic Core](D4-format-agnostic-core.md)
- [D5: Verify, Don't Trust](D5-verify-dont-trust.md)
- [D6: Field-Level Completion Tracking](D6-field-level-completion-tracking.md)
- [D8: Observability via Reasoning Log](D8-observability-via-reasoning-log.md)
- [D9: Approved Scan Roots (Security Guard Rail)](D9-approved-scan-roots-security-guard-rail.md)
- [D10: State Brief as the Trailing Message, Not the System Prompt](D10-state-brief-as-the-trailing-message-not-the-system-prompt.md)
- [D11: CI Workflow (GitHub Actions)](D11-ci-workflow-github-actions.md)
- [D12: Bounded Message History (Trim + Prune Before Each Model Call)](D12-bounded-message-history-trim--prune-before-each-model-call.md)
- [D13: ISA hasPart Hierarchy — Investigation is the Root](D13-isa-haspart-hierarchy--investigation-is-the-root.md)
- [D15: The ReAct Loop Is the Build](D15-the-react-loop-is-the-build.md)
- [D16: ISA-Tox Specialization via Ontology Terms, Not `@type` Arrays](D16-isa-tox-specialization-via-ontology-terms-not-type-arrays.md)
- [D17: A step executes its deposited documents, or else its own protocol record](D17-a-step-executes-its-deposited-documents-or-else-its-own-prot.md)
- [D18: A crate claims the profile its study can meet](D18-a-crate-claims-the-profile-its-study-can-meet.md)
- D14 (the entity-graph visualization) is a design, not a decision: see [the report and explorer design](../design/report.md).
