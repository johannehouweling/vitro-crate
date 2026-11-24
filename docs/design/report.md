# Maturity report and entity explorer

> The embedded HTML report, the entity-graph model, the entity explorer and the assay lanes.

`build_crate_graph(metadata, *, layer, all_edges)` is the deterministic model of a built crate's
`@graph`: nodes classified into the three paper layers (packaging / ISA / ISA-Tox), each referenced
`@id` marked *described* / *external-identifier-backed* / *dangling*, orphans flagged and split by
repair shape (isolated vs stranded), and a cumulative `layer` filter. Every view of a crate reads
this one model.

**`status` and `residence` are two facts and are never read for each other (#687).** `status` says
whether the crate *describes* an id; `residence` says where its bytes are, read off the `@id`
shape alone: `carried` (relative path), `record` (`#fragment`), `elsewhere` (absolute IRI), `named`
(nothing describes it). The shape is the whole rule: it needs no disk or network, and the report is
rendered before `crate.write()` materialises anything. A `File` declared and never materialised
therefore reads as carried; that defect belongs to validation. Node labels carry the
generated-file badge (`_display_name`); each node also carries the crate's own `name`.

The explorer is the only drawing of the model: `python -m main --graph [--view …]` writes it as a
standalone page. The five inventories — `build_chemical_inventory`, `build_cellline_inventory`,
`build_people_inventory`, `build_isa_inventory`, `build_citation_inventory` — feed both the
report's coverage blocks and the explorer's view membership. `ro-crate-graph.mmd` (in crates built
before #618) stays **reserved**: never a payload leaf, a `CrateState` entity or a graph node, so a
re-scanned legacy crate cannot package a generated artifact as data.

**Maturity report.** `export_crate` embeds `ro-crate-metadata-maturity.html` (#85) as a
`File` + `CreativeWork` `about` `./`, rendered by `build_maturity_html` (`builder/writers/maturity_report.py`). It covers profile
adherence (from `state.validation` — it never re-runs SHACL), FAIR indicators + DSM level
(`assess_fair_maturity`; `dsm_ceiling`'s `blocked_by` names what blocks the next level), MIT
coverage (`assess_mit_coverage`) and AI-readiness (`assess_air_readiness`). Embedding is automatic
and best-effort (a reporting failure never fails the export); `export_crate(..., embed_report=False)`
turns it off.

**Header and cards.** The headline is the study name — the root Dataset's `name` from the graph
(`state.metadata.title` only without one); the cited publication's name is the subhead. *About this
study* shows only facts the crate holds or an honest *not stated*. *About this RO-Crate* shows the
build `CreateAction`'s facts, plus a report id `MR-<date>-<hash6>` only when rendered with the
crate's graph. The identifier never headlines (#719) but the study card always states it.
`set_crate_metadata` warns, never refuses, on a value that fails `state.looks_like_identifier`.

**KPI grid.**
- A profile × requirement-level **matrix**: rows are the layers the crate claims (D18), linked to
  specs whose IRIs are pinned to `_crate_mapping`'s `conformsTo` constants by test. The Required
  column is the report's one headline verdict. A `–` reads *not assessed* where the sweep did not
  reach the tier, and *no checks defined at this level* where nothing in the row's layer chain
  declares a rule there; which tiers exist is read from `profiles.validator.tiers_defined`, never
  a hand-kept list. A finding at a tier with no defined check still reads ✗.
- The FAIR ladder: the next rung dashed and filled to the share of indicators met.
- The FAIR principle 1.3 rose: one wedge per MIT module (angle = scorable share, radius = fill).
- A graph tile: the explorer payload's residence tally, pinned to it by test.
- The AI-readiness profile: met of assessed, seven dimension bars; an unassessable dimension is
  drawn hollow, not at zero.

**Sections**, between the KPI grid and Recommendations: entity explorer, entity coverage, profile
adherence, the DSM "% complete" grid, MIT coverage, AI-readiness. There is no FAIR pillar section
and no header verdict pill.

**Recommendations.** Each row: the instrument's words verbatim in a chip prefixed by its layer, a
badge, `remediation.describe`'s instruction and `remediation.why`'s consequence clause. A DSM
indicator blocking the next level is an `Action` of the same shape
(`remediation.dsm_indicator_actions`), at the `MATURITY` tier between `REQUIRED` and `RECOMMENDED`;
its badge names the rung and its instruction is the indicator's `remedy` in
`fair/dsm_indicators.yaml` (the generator refuses an assessable indicator without one). The section
therefore renders for a valid crate whose ladder is incomplete.

**References** close the page: (1) FAIRplus DSM / RDA FDMM and the workbook the grid reproduces,
read from the `source`/`scoring` blocks of `fair/dsm_indicators.yaml`, never literals;
(2) tox-maturity-indicators; (3) the Bridge2AI criteria, named as a preprint quoted under CC BY-ND.

**The DSM grid.** The DSM's published output is the "% complete" grid — level × {content,
representation, hosting, total} (`fair_assessment.dsm_grid`); no sheet formula computes an
achieved level. The report leads with the grid and shows the gated level beside it labelled
**derived**. The heading links the assessment tool from the YAML's `source.assessment_tool`.
`scripts/gen_dsm_indicators.py` carries the workbook's scoring into `fair/dsm_indicators.yaml`
under `scoring` (cell membership, denominators, the nine promotion rules), so the scorer reproduces
the sheet rather than approximating it. Load-bearing properties:

* **A blank scores 0** — the sheet has no "not assessed" state.
* **Cell membership is a multiset carried forward from lower levels** (`DSM-4-H2` sits on two rows
  of the Level-4 hosting cell, which divides by three).
* **Level 0 counts zeros** — its statements are the pre-FAIRification condition in the negative.

Every cell publishes both numbers: `published_pct` (the sheet's, reproducible by an assessor) and
`pct` (over what was assessed; `None` when nothing was). `tests/test_dsm_sheet_parity.py` holds
them to the sheet's formula text.

**DSM verdicts.** `dsm_verdicts` is the single evaluation pass; the level, grid and ceiling all
read it, once per render.
- **Tri-state**: nothing to read answers `None`, which leaves `pct`'s denominator;
  `Verdict.__bool__` raises so `None` cannot collapse to `False`.
- Every verdict carries **evidence** stating what was measured.
- **Promotion**: `_apply_promotion` applies the sheet's nine rules (e.g. `J4 =IF(J5=1,1,H4)`) to a
  fixed point, recording the licensing cell; every source is itself a check that can fail.
- **Depositor answers**: `--dsm-answers` reads `{indicator id: bool}` onto `CrateState.dsm_answers`,
  merged **only where `scope` is `na`**; an omitted id stays unassessed, and the verdict names its
  source.

**The as-received graph** (`assessment_graph.as_received_graph`) exists only to keep the scorer
honest; no page shows an intake number. It is one `File` per scanned file and a root with `hasPart`
and, only if declared, `license`, and **mints nothing else** (no descriptor, identifier, name,
description, `conformsTo` or structure). `tests/test_fair_metrics_can_fail.py` pins the indicators
it may meet.

**MIT coverage.** The aggregate score is the headline; coverage is also broken out per guidance
document (#491) via each parameter's `standards` map (`MITReport.standard_scores`, labels
`MIT_STANDARD_LABELS`, links `MIT_STANDARD_SOURCES`; unregistered documents stay plain text).
Documents overlap, so the rows do not sum to the total. The lead links `MIT_INDICATORS_URL`.

**One MIT module, one colour (#606).** `maturity_report.MIT_MODULE_STYLES` is the only registry of
module colours; the stylesheet declares none (a test asserts it) and derives every state from the
`--mod` token. Hue = module, solid = filled, pale = missing; a document bar is split into one span
per module from `MITReport.standard_module_scores` (see `_score_modules` / `_render_mit_section`).
The palette floors (CVD separation, ≥3:1, clear of status colours) are pinned by tests. This
palette never shares a figure with the entity-category ring.

**Profile adherence** is reported across **Required / Recommended / Optional** (#306).
`build_and_validate` gates at REQUIRED and never fills `should_issues` / `may_issues`, so an empty
advisory list renders as an explicit **"not assessed"** state (glyph + label, never colour only),
never a green zero.

**Conformance is cumulative.** Each row is graded over `_LAYER_CHAIN` — its own checks and every
layer it extends — so nothing passes a tier a layer beneath it fails, and a cell names inherited
findings (*"11 findings at this level, 11 inherited"*). The rule lives in
`state.conformance_by_layer` / `PROFILE_LAYER_CHAIN`, and **every** conformance surface composes
through it: the matrix, the `prof-card`s, the TUI status dots, summary and conformance lines,
`dashboard`, and `session`'s `validation_status`. A raw per-pass flag reports only what its layer
adds and is never shown as the crate's verdict. The cumulative Optional column is composed from
per-layer verdicts, not the validator's inherited reporting, because ISA is 1.1-lineage while the
base pass runs 1.2 (crs4/rocrate-validator#194).

**Payload verdict (#530).** The in-memory gate cannot check that every declared Data Entity is in
the payload, and that silence is never a pass. `verify_payload` asserts the invariant directly
rather than naming a check id: every local file has a source `crate.write()` will materialise (a
directory needs none). Export runs it **before** embedding the report and files findings as
REQUIRED; `payload_checked` records that it ran.

**ISA reachability (#537).** The ISA shapes infer their target class from the very edge whose
absence is the defect, so a missing structural edge silences them. `verify_isa_reachability`
therefore walks **directed** from `./` over every reference; a `LabProcess`, `Sample`,
`LabProtocol` or `AdverseOutcomePathway` never visited is detached (an island referencing its own
members still fails, #738). Entities at absolute URIs are excluded, except the pathway.
`provenance_dag`'s undirected orphan flag is not this test. `fold_isa_reachability` runs it beside
the ISA pass; `isa_reachability_checked` records that it ran.

**Findings fold out of their severity row (#510).** A row with findings is a `<details>` listing
them grouped by layer (base → ISA → ISA-Tox); a REQUIRED row is born `open`; a row with nothing has
no fold; print unfolds all. There is no second per-profile index, and profile cards stay
non-interactive. The source is chosen **per tier**: `validation.issue_records` where that tier has
them, else its flat display list (ungrouped). A row's count is the count of what it unfolds;
advisory caps apply per group and name how many they hid.

**Entity coverage** and the explorer render only when `build_maturity_html` gets the graph
(`graph=crate.metadata.generate()`, as `export_crate` passes). One block per kind, each asking the
question that kind fails at (can the compound be obtained, is the cell line pinned down by an RRID,
does the citation resolve); no diagrams. Block order is the owner's: Files / Assays / Chemicals /
Biological models / Persons & Organisations / Citations. Empty blocks are omitted. Each block is a
fold with its count in the summary (#629); `@media print` forces folds open.

**The entity explorer** (`builder/writers/entity_explorer.py`, #615).
`build_explorer_payload(metadata)` is the pure, deterministic model — `build_crate_graph` nodes and
edges (labels unescaped), the crate document verbatim, the category registry, one member list per
view; `render_explorer_section` emits the mount point, the data island
(`<script type="application/json">`) and the vendored bundles. Drawn with React Flow.

**Views are toggles, not tabs**: the union of the views on, with induced edges. A view no entity
satisfies is omitted by `build_explorer_payload`, so no per-view guard exists.
- **All entities** opens first: every node with a layer. It declares no subject and counts members.
- **Assays**: the ISA backbone plus the AOP a study serves and the key events an assay measures
  (`schema:mentions`, `6_study_aop.ttl`, `7_assay_key_event.ttl`), *followed* from the backbone and
  filtered by type, never swept from the crate (#627).
- **Adverse outcome pathway**: every `pathway`-category entity plus the ISA entity mentioning one;
  its hint points to AOP-Wiki for the rest. Not shortened to "Pathways", which here means
  WikiPathways (#652).
- **LabProcesses**: the derivation chain plus each visible step's protocol and the assay whose
  `about` points at it (#626) — reached only through a drawn step. `_derivation_edges` and
  `_route_hop_ids` are shared with `provenance_dag`, and tests hold each toggle to its selection.

`PATHWAY_TYPES` (pathway, key events, `KeyEventRelationship`) is the one list for which nodes the
views follow to, how they are captioned (a domain type outranks the generic type it refines) and
their `pathway` category colour (#643).

**A chip counts the view's subject, not its selection** (#625), from the same source as the
matching coverage block (a test pins the two to each other), counted as drawn.

**Compounds are one hop past a protocol** (D17): `Exposure --executes--> protocol --reagent-->
compound`, since `schema:object` cannot hold a `MolecularEntity`. Process views follow that hop
from protocols the drawn steps execute; `reagent` is drawn reversed, pointing at the consuming step.

**Navigation.** The chips are the whole toolbar, whole-crate view first; search sits on the canvas;
framing is React Flow's control only. Colour key, count and the entity-coverage fold sit under the
canvas. The inspector is a drawer docked to the window's right edge, present only once an entity is
selected; both sections share that edge and each closes the other's drawer when it opens one; below
the breakpoint the panel sits beside the canvas.

**The inspector.** Properties, links in/out grouped by relation, JSON-LD, or a toggle to the whole
`ro-crate-metadata.json`. Every `@id` is a button that moves the selection. **The explorer writes no
anchor and no link target** — the payload carries the crate verbatim, `javascript:` URLs included;
URLs are offered for copying only, and a test greps the apps for the attributes. The Overview names
each property as the crate expands it (`property_terms()`, falling back to `@vocab`), **one row per
predicate** with the crate's keys on the tooltip; `parameter` and `parameterValue` stay two rows
(two predicates, asked for by the two profiles). The Links tab uses the edges' table.

**Edges (#688).** Selecting a node lights its edges labelled in the crate's serialized vocabulary
(`schema:object`), never the model's words (`input`). `relation_terms()` derives the mapping from
the relation tables and `@context` and ships it in the payload — the browser holds no second copy.
A namespace with no declared prefix shows its full IRI, never an invented prefix. Labels have no
box: edge-coloured text with a `paint-order` halo. Clicking the selected node clears the selection.

**Nodes.** Border = category, fill = residence (tinted = bytes in the crate, #687). Colour only, no
glyph: the inspector names the type in words and the legend states the mapping; the glyph data and
its uniqueness rule are kept so the channel can be restored in one line.

**Layout (`entity_explorer_layout.js`, #619).** Pure geometry in its own `<script>` — no DOM, React
or payload — so tests run the shipped code. A rank holding more than `RANK_CAP` leaves is packed
into a near-square grid, expressed to dagre as ONE stand-in node per block with members' edges
redirected to it, members dealt in afterwards; a rank at or below the cap keeps its column.

**Wire format (`payload_codec.js`, #694).** `build_explorer_payload` stays the readable model every
Python consumer reads; the data island carries a compacted encoding (edges as
`[src_index, dst_index, label]`, view members as indices, `name` omitted when equal to `label`, no
category `glyph`). `expand` runs on the app's first line, so nothing else knows the wire format;
the round trip is tested by running the shipped JavaScript over Python's output. Nodes and edges
are never derived in the browser from `document`: that would be a second `build_crate_graph`.

**The assay lanes are a section of their own (#686).** One assay drawn as its chain — cell line,
preparation, cultured sample, exposure, exposed sample, readout, raw files, analysis, processed
files — one lane at a time, chosen by a chip per assay minted from the ISA inventory. No assay, no
section; no lede. It is not an explorer view because a lane needs a fixed-order flat drawing, not a
pan-and-zoom viewport; it draws **plain hand-built SVG** (no React, React Flow or layout library).

A lane draws the assay's steps, materials **one hop** out (a hop, not a closure, so a shared file
cannot lead out of the assay), each step's protocol, and compounds one hop past those protocols —
never the Study, the Investigation or the assay itself (it would rebuild the star #678 removed).
The lane key is the assay's **name** slug; where names collide, every colliding lane takes an
id-derived suffix, decided across all assays first so keys are order-independent. The section has
**no data island of its own**: it reads the explorer's, so the report emits the explorer first.

Controls sit **on the viewer, not the chip bar**: **Protocols** hides the band; **Unfold files**
opens a file column otherwise drawn as one stack (only file columns fold, so no step is hidden);
**Fit** scales to the viewer, otherwise boxes are reading size and the viewer scrolls. Folds re-run
the geometry on a smaller graph, never edit its answer.

**Two pictures, one of everything else.** Both viewers share the data island, palette, legend,
chips, footer, overlay controls and drawer. `explorer_inspector.js` builds the inspector in plain
DOM for both and owns the vocabulary (`term`, `edgeTerm`, `prop`), so the page never says one thing
two ways. The chosen assay, folds and selection ride in the page's one hash under `lane`, `fold`,
`pick`; each side replaces only its own keys.

**Where an assay's nodes go (`assay_lane_view.js`).** Horizontal is the material chain, vertical is
what qualifies a step. A column is decided by what a node **is**: a step by `additionalType`, a
material by the step whose `result` produced it, else the step consuming it — never by a layered
pass. A missing step is an **empty column**. `derivesFrom` is excluded from ranking (it points back
up the chain). Each rank is ordered from the rank before it, ties by id, so parallel tracks do not
braid. Band connectors run down the gap **left** of the column, one vertical per anchor, so no line
crosses a box. Both band tiers are dealt into grids (height, never width). The module returns
`null` for a graph with no recognised step, and the section says so. The page loads it as a plain
`<script>`, so a test evaluates the page's own script bodies with no `module` in scope.

**The legend names the class, not the category and not a census (#623).** Each key is the class
that puts an entity in that category (`_entity_category`'s rule): `LabProcess`, `LabProtocol`,
`Sample`, `MolecularEntity`, `File`. The census of type tags rides on the `title`. The fallback
bucket and the off-crate reference keep their prose. The wording is decided **once in Python**
(`_legend_wording`), because two sections draw the legend with two renderers.

**Script, but nothing loaded.** React, React Flow, dagre and htm are vendored UMD builds under
`builder/writers/vendor/`, pinned by `manifest.json` (name, version, licence, origin, sha256) and
verified at render time — a mismatch fails the render. They are inlined with a credit banner;
React Flow's CSS joins the report's single `<style>`. The crate's metadata is copied in because
`file://` cannot `fetch` a sibling. Tabs stay CSS-only. `@media print` hides the canvas and shows a
note. The page is self-contained: `maturity_report.css` / `maturity_report.html` (`__STYLE__` /
`__TITLE__` / `__BODY__`) are read (cached) and inlined by `build_maturity_html`; only data-driven
markup is assembled in Python.

**One entity type, one colour — `CATEGORY_STYLES`.** `provenance_dag.CATEGORY_STYLES` is the single
registry, one entry per category key from `_entity_category` (colour, legend wording, glyph); no
view decides its own palette.
- `category_css()` generates the `--cat-*` properties and tile/node/tag rules into
  `maturity_report.css` at `__CATEGORY_STYLES__`; they are **never hand-written**, and a test
  asserts the stylesheet declares no palette.
- `build_explorer_payload` generates the explorer's palette from the same registry.
- `_node_class` / `_node_class_for_brief` both classify through `_entity_category`.

Colours are a constant-lightness CIE Lab ring (L\* 47, chroma 44, hues 36° apart; process and
container split on lightness), worst pair dE 24, every stroke ≥3:1. The ring is **full at ten**;
saturation is the second channel: `annotation` is muted (chroma 19) and `ctx` near-grey, so an
eleventh category costs a demotion, not a new hue. **No two categories may share a glyph.**
`TestCategoryRegistry` pins the glyph rule, palette separation, contrast floor and generated-CSS
coverage.
