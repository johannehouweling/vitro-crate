# Per-cell-line process chain

**Status:** design, awaiting review
**Crate under study:** `output/svhps22_real_input_crate_v23` (S-VHPS22, 4 assays, 15 LabProcesses)

## Problem

The assembled crate asserts things the deposit does not say.

**1. A co-culture that never happened.** `_crate_mapping._build_process` builds one
`CellCulture` per drafted step, sets `cell_line` to the whole resolved list, and emits a
single output `Sample` whose `derivesFrom` names every line
(`_crate_mapping.py:3083-3117`). On S-VHPS22 that produces:

```
#LabProcess_proc_culture_sk_n_as_h4_and_mo313_neural_cells
    input  = [H4, H-4, MO3.13, SK-N-AS]
    output = #Sample_..._output_sample   (derivesFrom all four)
```

In ISA-Tox that reads as four lines entering one culturing activity and one material
coming out. The lines were cultured separately — the deposit ships one culture protocol
document *per line* (`cell culture protocol SK-N-AS.docx`, `…MO3.13.docx`, `…H4.docx`).

**2. The merge repeats one hop down.** The `Exposure` branch synthesises exactly one
`Exposed (…)` Sample regardless of how many cultured samples it consumed
(`_crate_mapping.py:3153-3162`). Splitting the culture alone would relocate the merge
rather than remove it.

**3. A process that lives at study level.** Only three `CellCulture` steps exist for four
assays: the Deiodinase Assay has no culture of its own and borrows the metabolism assay's.
A LabProcess belongs to an assay; the reused thing is the *protocol*.

**4. The readout skips the exposure.** `_chain_processes` redirects a readout off the
cultured sample onto the exposed one, but groups by **assay**. The Deiodinase Assay's group
contains no `CellCulture`, so `cultured_ids` is empty, the guard
`consumed_ids <= cultured_ids` never matches, and `D3 deiodinase activity readout` still
consumes the cultured sample. This is the surviving half of #650, live in this crate.

**5. One cell line, two entities.** `#CellLineSample_cell_h4` ("H4") and
`#CellLineSample_cell_h_4` ("H-4") are the same line under two spellings from the deposit.
Neither carries a Cellosaurus accession, so `_find_cell_line_by_accession` cannot merge
them. Cellosaurus refuses both names as ambiguous — three exact matches each
(`CVCL_1239` / `CVCL_6C19` / `CVCL_HA56` for "H4").

## Target model

Per assay, per cell line:

```
CellLine (RRID) ─[CellCulture · per assay × line]→ Cultured Sample (one line each)
                                                             │
                   [Exposure] ─executesLabProtocol→ SOP + condition table
                                                             │
                                          Exposed Sample, one per cultured sample
                                                             │
                   [EndpointReadout · one protocol] ────────→ raw data
                                                             │
                   [DataAnalysis] ──────────────────────────→ processed data
```

### Invariants

1. **A `CellCulture` is minted per (assay × cell line).** Never study-level; repeated
   across assays when the same line is grown for each.
2. **A cultured `Sample` derives from exactly one cell line.** No sample is a mixture.
3. **The `Exposure` consumes every cultured Sample of its own assay**, and emits one
   `Exposed` Sample per cultured Sample consumed. The drafter names a single cultured
   sample; after the split that reference covers one line of several, so the exposure's
   `object` is re-wired to the assay's full set at chaining time.
4. **A `LabProtocol` is the reused entity.** A document referenced by more than one assay's
   processes is minted once and hangs off the **Study**; one used by a single assay stays
   nested under that Assay. Processes reach it via `executesLabProtocol`. The generated
   condition table stays with its own Exposure — it describes one exposure's conditions.
5. **The readout consumes the exposed samples** from every line in its assay.
6. **A cell line appears once.** Spelling variants merge into one `CellLineSample`. Where
   no RRID resolves, the entity is kept unidentified and carries the existing SHOULD-level
   warning — the fact that the line was used is not discarded.

### Effect on S-VHPS22

| | before | after |
|---|---|---|
| LabProcesses | 15 | 20 (8 culture · 4 exposure · 4 readout · 4 analysis) |
| Cultured samples | 3 | 8 |
| Exposed samples | 4 | 8 |
| Cell-line entities | 4 (H4 twice) | 3 |
| Assays with a complete chain | 3 of 4 | 4 of 4 |

Per assay: uptake 3 lines, deiodinase 2, metabolism 2, TR activation 1.

## SHACL

The target model violates **nothing** in `profiles/shapes/tox/`. Every constraint there is
`sh:minCount 1`; the tree contains no `sh:maxCount` and no `derivesFrom` rule. Splitting one
process into N, each with one object and one result, satisfies `CellCultureRequirements` and
`ExposureRequirements` as written. `CellLineSampleShouldHaveIdentifier` is `sh:Warning`, so an
unidentified H4 is already legal and already flagged.

Two shapes are **added**, because the absence of each is why a defect above validated clean.

### `tox:CulturedSampleDerivesFromOneLine`

`sh:maxCount 1` on `schema:derivesFrom` for a Sample whose `sampleType` is the cell-culture
term `OBI:0001876`. Violation.

That term is carried by cultured **and** exposed samples deliberately — an exposure changes a
sample's state, not its kind (`_crate_mapping.py:53-59`) — and the rule is correct for both:
an exposed sample derives from exactly one cultured sample.

**Co-culture is accommodated, not forbidden.** A co-culture is a legitimate design, so the
constraint discriminates on what the material claims to be rather than on how many lines went
in:

| | `sampleType` | `derivesFrom` |
|---|---|---|
| Cultured / exposed sample | `OBI:0001876` "cell culture" | exactly 1 |
| Co-culture sample | `NCIT:C93168` "Co-Culture" | 2 or more (`tox:CoCultureDerivesFromSeveralLines`, Violation) |

A co-culture sample carries `NCIT:C93168` **instead of** `OBI:0001876`, so it falls outside the
first shape and inside the second. A co-culture of one line is then as invalid as a mixture
claiming to be a pure culture — both directions are checked.

The producing process keeps `additionalType: "CellCulture"`: no fifth discriminator is invented,
because the four are the profile's own and a fifth would be a category no crate can carry
(`entity_explorer.PROCESS_FLAVOURS`). It carries the intent as a parameter
`Culture Format = co-culture` referencing `OBI:0000153` "cell co-culturing".

**Co-culture must be positively asserted.** N cell lines in one drafted culture is *not*
evidence of co-culture — that ambiguity is the defect being removed. The builder splits by
default and mints a co-culture only on an explicit `co_culture` field on the drafted process.
The interactive build may ask when a drafted culture names several lines; `run_pipeline` stays
non-blocking and takes the default.

### `tox:EndpointReadoutConsumesExposedMaterial`

`schema:object`, `sh:minCount 1`, `sh:or ( [ sh:class schema:MediaObject ] [ sh:class
bioschemas:Sample ] )`. Violation. The readout shape has no input rule today, which is why
defect 4 raised nothing.

The object SHOULD further be an exposure's output, checked with an inverse path — the consumed
node must be the `schema:result` of some `tox:LabProcessExposure`. This is `sh:Warning`, not
Violation: a characterisation run in an assay with **no** exposure legitimately measures the
culture, and that is the truth rather than the defect (`_chain_processes`). The Violation-worthy
case — an exposure exists and the readout skipped it — is enforced in the builder, since the
condition spans entities.

## Components

| Unit | Change |
|---|---|
| `_crate_mapping._add_processes` | A drafted `CellCulture` naming N lines expands to N nodes: id `pid_<line-slug>`, name `Culture <line>`, each registered in `built` and on the Assay's `about`. |
| `_crate_mapping._build_process` (CellCulture) | Takes one line, not a list. Result is that line's cultured Sample, `derivesFrom` a single entity. Where the drafter supplied an output Sample, it is retained as the cultured sample of the first line in deterministic order and its `derivesFrom` corrected to that one line; the remaining lines get synthesised `Cultured (<line>)` samples. No drafted entity is discarded. |
| `_crate_mapping._build_process` (Exposure) | Synthesises one `Exposed` Sample per consumed cultured Sample instead of one per process. |
| `_crate_mapping._chain_processes` | Wires each Exposure to its assay's full set of cultured samples (invariant 3). Resolves cultured samples across assays that share a culture, so the readout redirect fires when the culture was minted elsewhere — largely moot once invariant 1 holds, kept as the backstop for ReAct-built crates. |
| `_crate_mapping._culture_protocols` / `_link_to_study` | Study placement becomes conditional on cross-assay use rather than on being a culture protocol. |
| `composites.resolve_cell_line` | Merges spelling variants onto one entity when no accession can arbitrate. |
| `profiles/shapes/tox/` | `CulturedSampleDerivesFromOneLine`, `CoCultureDerivesFromSeveralLines`, `EndpointReadoutConsumesExposedMaterial`. |
| `_crate_mapping` co-culture path | `co_culture` field on a drafted CellCulture keeps the lines together, types the output `NCIT:C93168`, and parameterises the process with `OBI:0000153`. |

## Testing

TDD, one failing test per invariant, over the shipped S-VHPS22 fixture:

1. No `Sample` in a built crate has more than one `derivesFrom` cell line.
2. Every assay with an exposure has at least one `CellCulture` of its own.
3. An exposure consumes every cultured sample in its assay, and produces one exposed
   sample per cultured sample consumed.
4. Every `EndpointReadout` in an assay that has an `Exposure` consumes only exposed samples.
5. A protocol document used by two assays is `hasPart` of the Study exactly once; one used by
   a single assay is nested under that Assay.
6. Two spellings of one cell line yield one `CellLineSample`.
7. A crate whose cultured sample derives from two lines fails validation; the same crate with
   that sample typed `NCIT:C93168` passes.
8. A co-culture sample deriving from one line fails validation.
9. A readout with no `schema:object`, and one whose object is neither File nor Sample, fail
   validation; a readout consuming a cultured sample in an exposure-free assay does not.
10. A drafted culture with `co_culture` set keeps one process and one output sample; the same
   draft without it splits per line.

Validation runs with `VITRO_VALIDATE_SERIAL=1` locally; completion is gated on CI.

## Display

The model fix is a precondition for the readability work that prompted it. The explorer already
lays out `rankdir: 'LR'`, but the crate hands it one connected component: the culture shared
between the Deiodinase and metabolism assays ties two chains together, and 27 result files plus 17
protocols hang off the steps. The LabProcesses view draws **74 nodes for 15 steps**; the Exposure
sub-view draws 19, of which four are captioned identically as `⚠️ Condition table` and none is a
compound.

### The unit is the assay

An assay is what produces a research object, so it is what the view draws. Scoped to one assay the
whole closure is small — the Deiodinase assay is **34 nodes** under the target model — and, once
each assay owns its culture, no node on that lane belongs to another.

This dissolves the layout problem rather than working around it. Every entity is drawn as an
entity: a `LabProtocol` and a `MolecularEntity` are typed nodes and are not suppressed from a view
whose subject is entities. Sharing only forces cross-lane edges when several lanes are on screen,
and here there is one.

### Two bands

**Horizontal is the material chain**; **vertical is what qualifies a step**.

```
spine    CellLine → Culture → Cultured → Exposure → Exposed → Readout → raw → Analysis → processed
             │                              │                    │
band         └── culture protocol           ├── condition table  └── readout protocols
                                            └── 12 compounds (reagent)
```

- **Cell lines** open the spine. Nothing precedes them, so nothing crosses them.
- **Protocols** sit in the band directly under the step that executes them, on a vertical
  `executesLabProtocol` edge. A horizontal reading is undisturbed by a vertical drop, and the
  attachment is unambiguous — which is also what makes it clear which protocol governs which step.
- **Compounds** hang off the condition table in the band, not off the spine. Twelve cost one rank
  of height and no horizontal travel.
- **Study and Investigation** are not in this view. The lane is the assay. The Assay node itself is
  the frame rather than a node: drawn, it would connect to every step and reproduce the star the
  rest of this work removes.

### File stacks

A step's result files draw as an offset stack — N entities, one footprint. The stack **unfolds in
place, at its own rank**, growing downward into its N File nodes; it never relocates to the band,
because raw data is on the material spine and the edge into the analysis must stay horizontal. The
existing layout already packs a wide rank into a grid (`ExplorerLayout.RANK_CAP`), so this needs no
new geometry.

Unfolded, a File is inspectable through the panel the explorer already has, carrying what the crate
knows and currently withholds: `contentSize`, `encodingFormat`, `description`, a condition table's
CSVW columns, and the scanner's `first_rows` for CSV/TSV/XLSX (`builder/state.py`), which is
captured today and never reaches the report.

Opening the file itself is #651 and keeps its constraints: the explorer writes no `href`/`src`
(`test_the_app_never_writes_an_href_or_a_src`), crate text is untrusted (#169), so a target must be
a validated relative path and must degrade to plain text when the report is read outside its crate.

### Selection

One sub-row per assay under the Assays chip, the pattern LabProcesses already uses for its four
kinds (`PROCESS_FLAVOURS`, `parent="processes"`). A child view **narrows** its parent (#624), so
choosing one assay replaces the Assays selection and the study containers drop out with it. Counts
on this crate: Deiodinase 34, TH Transport 41, Whole-cell metabolism 36, TR Activation 27.

### Compounds in LabProcesses

Compounds are absent from the LabProcesses view today by one hop, not by a modelling gap.
`_select_processes` follows the material chain plus `_PROCESS_CONTEXT` (`executes` outward, `about`
inward), which reaches the condition table and stops; the compounds hang off that table by
`reagent`. The selection follows that second hop.

The route stays as it is: ISA restricts `schema:object` to File/Sample/BioSample at Violation
severity, and `reagent` is a LabProtocol property whose published range names
`schema:MolecularEntity`, so `Exposure --executesLabProtocol--> table --reagent--> compound` is the
correct representation rather than a detour to be shortened.

Chip counts are unaffected: an `ExplorerView`'s `select` is what it draws and its `subject` is what
it is named for, and only `subject` is counted (#625).

### Decided (2026-08-24)

The lane ships as a **dedicated layout module beside `entity_explorer_layout.js`**, returning the same
shape — a position per id — so nothing downstream knows which one ran. Everything else is shared:
category colours from the one `CATEGORY_STYLES` registry, the node and edge components, the legend,
selection, and the inspector.

| decision | value |
|---|---|
| Node size | **200×44** everywhere — the shipped `ExplorerLayout.NODE_W/NODE_H`, used by the lane too. One component, one set of constants. |
| Category glyphs | **Dropped. Colour only.** |
| LabProcesses view | **Kept.** It answers "what steps exist across the crate"; the lane answers "how did this assay run". Its four flavour sub-rows (#625) stay. |
| Band label | **Kept**, uppercase, as `LABPROTOCOL`. |
| Inspector | The existing panel's **Properties tab is reworked into the Overview** — qualified property names from `profiles/context.py`, values linked wherever a URL exists or a resolver builds one, JSON-LD aliases merged. Links and JSON-LD tabs stay. |
| Fallback | An assay that does not fit the spine is drawn by the **generic canvas**, same styling, no visible seam. |
| Edge labels on selection | **Both layouts.** Selecting a node lights its edges and labels each with its qualified property (`schema:object`, `schema:result`, `bioschemas:executesLabProtocol`, `bioschemas:reagent`); unrelated edges dim. Labels are drawn with a `paint-order: stroke` halo in the edge's own colour — no box. |
| Payload tint | **Both layouts.** A tinted fill means the bytes are in the crate. |
| Click-again-to-clear | **Both layouts.** Clicking the selected node deselects it, taking the edge labels with it. |

Those three are node and edge *behaviour*, not layout, so they belong to the shared component and apply
to every view — Researcher, All entities, Files and the rest — not only to the lane.

### Residence, and why `status` has to be renamed

The payload tint makes this load-bearing rather than tidy. `build_crate_graph` currently emits
`status: "in_crate"` for **every entity described in the metadata** (`provenance_dag.py:1065`), so a
Cellosaurus IRI, a `#fragment` PropertyValue and a PDF on disk all carry it, while `external` /
`dangling` are reserved for ids that are referenced and never described. Tinting on `status` would
paint a compound as if its bytes were in the crate.

Residence is a second, orthogonal fact, read off the `@id` with no heuristics:

| `@id` shape | residence | drawn | count in S-VHPS22 |
|---|---|---|---|
| relative path | **carried** — bytes in the crate | tinted fill | 60 |
| `#fragment` | **described** — a record, no bytes | plain | 164 |
| absolute IRI | **elsewhere** — resolvable, outside | plain | 21 |
| referenced, never described | **named only** | plain, muted | — |

All 60 relative-path entities in this crate do have bytes on disk (verified), but a `File` can be
declared without being materialised, so the build should confirm against disk rather than trust the
shape alone. `status` keeps its own meaning and is renamed to say it: `described` / `external` /
`dangling`.


**Glyphs — the accepted cost.** Shape was the redundant channel that survived greyscale, print and
colour vision deficiency, and `CATEGORY_STYLES` guaranteed no two categories shared one. Dropping it
leaves eleven categories encoded on a colour ring the registry itself calls full at ten. Two
mitigations do not fight the decision and should ship with it: the inspector names the type in words
on every entity, and the legend remains the one place the mapping is stated. Print and CVD legibility
is knowingly reduced; recorded so the trade is visible rather than forgotten.

**Fallback cases** — characterisation run with no exposure; more than one exposure in an assay; an
assay carrying AOP entities; no assay at all.

Measured by running the shipped layout over the Deiodinase assay's 34 nodes: generic canvas
**2568×848** across 9 dependency columns (widest holding 13); lane **1846×358** across 9 role ranks
plus the band.

## Out of scope

- Populating the condition table. All four tables are header-only — zero rows — so dose,
  concentration and per-well cell assignment are absent from the crate today. That is #669,
  and the per-condition exposed-sample ids depend on it. Picked up separately.
- Per-experiment-run exposed samples (#654).
- Disambiguating H4 against Cellosaurus using study context.
- Implementing the LabProcesses view layout. Its requirement is recorded under **Display**;
  the options follow this work.
