# Initialization: scanning, classification, deposit structure

> What runs before the agent loop: the file inventory, the one classification per file, the document ranking, and the read of the deposit's studies and assays.

## Scanner (`builder/tools/scanner.py`)
Examines an input directory (or zip archive) and builds a raw file inventory
(path, size, mime type, first lines of readable files). Never reads entire
large files into context. An archive is extracted to `<stem>_extracted/`, and
that folder is the session's `input_path`: every file source, relative path and
crate name resolves against it (#819). The inventory is not the whole of initialization — file classification,
document ranking and the declared-licence read all run before the agent loop
([architecture overview](architecture.md#architecture-overview)) — but it is what the agent uses during entity drafting to bind files to
`LabProcess` instances as annotations emerge. Restricted by approved scan
roots (see [the scan-root guard rails](architecture.md#guard-rails-approved-scan-roots)).

**Bounded results.** `max_files` and `max_line_length` cap result size and preview
length; `read_file_sample` takes `precomputed_size`/`already_text` so a caller that
already knows them skips a second `stat()`/MIME syscall. `_safe_walk` prunes
hidden/`.git`/`__MACOSX` directories **in place** during `os.walk`
(`dirnames[:] = [...]`) — the in-place mutation is what stops them being descended
at all; a refactor into a post-hoc filter loses that.

**MIME detection.** `_detect_mime_type` resolves in order: stdlib `mimetypes`, then
a scientific-format registry (`_SCIENTIFIC_MIME_TYPES`) covering MS/microscopy/flow
extensions the stdlib does not know (`.mzML` → `application/x-mzml`, `.fcs` →
`application/vnd.isac.fcs`, vendor binaries `.raw/.wiff/.czi/.nd2/.lif/.d/…` →
`application/octet-stream`), then a text-content sniff. The registry is consulted
**before** the sniff so binaries are never mislabeled `text/plain`;
`application/octet-stream` is the default for unknown binary; and a NUL byte in the
header forces that default even when the bytes decode as UTF-8.
`encoding_format_for_name` exposes the same extension→media-type derivation with no
disk read, for entity drafting.

**Size ceilings and the read contract.** The dedicated readers in `file_readers.py`
share the scanner's 100 MB `_MAX_BYTES` ceiling; row/line caps keep memory bounded.
`read_file` returns plain-text/JSON **in full** up to `_TEXT_BUDGET_BYTES` (64 KiB),
because a line-clipped return makes weak models loop "let me read the rest". Over
budget it returns the content shown plus an explicit, machine-stable marker
(`[truncated: showing first 64 KiB of N KiB; this is the maximum for this tool —
do not re-read]`) so the model knows re-reading yields nothing more; that marker is
a verbatim contract, duplicated in the tool description and the system prompt, so it
changes in all three places or none. The 100 MB `_MAX_BYTES` hard cap still skips
genuinely huge binaries entirely. A *directory* handed to
`read_file`/`read_file_sample` returns "`<path>` is a directory … use
list_scanned_files …", **never a silent `None`**, and a bare `None` from any reader
(`read_file_sample`/`read_file`/`read_excel`/`read_docx`) is turned into an
actionable "unreadable/too-large — skip it" message — silence is what makes a weak
model re-call the same reader. `read_file_sample`'s `lines` argument controls how
much 'content' mode returns. Reasoning-log entries embed a compact, bounded repr of
each tool's call args (`run_tool: read_file(path='…')`) so the recorded action shows
*which* path/hints a tool ran with, not just its result.

**Repeated non-progress loop-breaker (ReAct only).** An identical non-progress call
re-issued by a weak model burns millions of tokens, so the `_run` wrapper in
`_build_langchain_tools` tracks the last tool-call signature (name + sorted args)
and the consecutive count of the **same non-progress result** (a directory message,
an unreadable/`None` message, or an `{"error": …}` dict —
`_is_non_progress_result`) on the engine. After `_LOOP_BREAKER_THRESHOLD` (3)
identical non-progress repeats it **refuses to run the call again** and returns a
forceful corrective message carrying the live `list_scanned_files` inventory
(concrete file paths to read instead). Any *distinct* call or any *progress* result
resets the counter, so legitimately-repeated different calls and a single normal
retry never trip it.

## File classification & document discovery (`builder/tools/document_discovery.py`)

Two questions about the same file, answered once and read by everything. The
line-level rules and the fixture measurements behind them live in the module's
docstrings (`classify_file`, `classify_scanned_files`, `_allocate_slots`,
`_fair_shares`); what follows is what must hold.

**What a file IS — one classification, four values** (#591). `classify_file`
returns exactly one of `metadata` / `protocol` / `raw_data_file` /
`processed_data_file`, plus the signal that decided it. `metadata` covers the
deposit record, assay-metadata workbooks, publications and plate maps;
`protocol` covers SOPs, lab protocols and analysis scripts; the other two are
the data tiers a derivation chain is wired from. A plate map is `metadata` — it
states the design that was intended, not a value that was measured.

The order is **content → filename → path**, and it is load-bearing in both
directions: letting the extension outrank the content files an instrument
printout under `raw data/` as a protocol because it is a `.pdf`, and letting the
path outrank the filename files `assay1_rawdata/README.txt` as a measurement. A
script (`SCRIPT_SUFFIXES`) is the one exception: its extension decides before its
content, because its text is code, not prose (#786).
Within the filename step, *what a file is* outranks *which tier it would be* — a
paper titled "Normalization of Data for Viability…" is a publication, not
processed data. Terms are matched at a word boundary and open at the end, so one
spelling covers a family (`normali` catches "normalised") while `raw` still
refuses "drawings" and `process` refuses "unprocessed", which names the opposite
tier. A word-processor format is never placed by its folder: nothing but a person
writes a `.docx`, so bench notes filed beside the measurements are not
measurements.

The classification is **stamped on every scanned file** (`classify_scanned_files`
→ `FileClassification.classification`), not on the ranked subset — what the crate
is built from must not depend on what fits in a context window.
`classification_of` answers for a record that was never stamped (a resumed
session saved before the classification existed, or any caller holding the
inventory without the deposit mounted) from the record alone, without touching
the disk.

Stamping every file does not mean OPENING every file. A deposit is a handful of
homogeneous folders, not N distinct things, so files are grouped by `(directory,
extension)`, a *spread* sample of each group is read — spread, because a folder
sorted by run date puts its exception at the end as often as at the start — and a
group whose sample agrees takes that verdict across the rest. A group is opened in
full when it is smaller than four times the sample, when its sample DISAGREES
(which is what says the folder is heterogeneous and cannot be summarised at all),
or when the sample is anything but `raw_data_file` — instrument output is the only
tier whose files are interchangeable. A propagated file has no preview, and a
workbook with no preview is ranked on its filename alone (#587). The standing
limitation: a file whose CONTENT alone makes it an exception in the interior of a
large uniform folder is invisible to this, because nothing about its name,
extension or directory sets it apart.

Which file is the design table is **not** what a file IS, and is deliberately not
folded into the classification: the same content is `metadata` as
`plate_map.csv`, `raw_data_file` as `conditions.csv`, and `processed_data_file`
when it carries the measured value alongside the design. The rows are asked
instead, through `data_content.condition_table_fit` — a well key AND columns that
map onto the canonical schema. That is the predicate `populate_condition_table`
must satisfy to write anything.

Consumers: `composites._deposited_outputs` / `_deposit_evidences` ([the toolbox](toolbox.md), *Derivation
Chain Tools*), `provenance.attach_files` (which stamps it as the File's `role`
unless the caller names one), the ReAct loop's `open_items`, and the maturity
report's "data files included" row.

`File.role` is the *stamp*, not the classification. It is free text — `draft_file`
records whatever the agent passes, and a resumed session carries whatever was
stamped for the rest of its life. A reader
deriving a class from it therefore accepts only a value in `FILE_CLASSES` and
classifies the file otherwise; **nothing may take the field at its word.** The role
never reaches the crate in any case — `_crate_mapping` drops it, as it is absent
from the RO-Crate `@context`.

**What reaches the prompt — ranking.** `discover_documents` screens and ranks
readable documentation, reusing the previews classification already read. It
reports `kind` (descriptor / tabular / narrative / opaque) alongside the
classification, because form and function are two questions with different
answers for the same file. `MAX_DOCUMENT_CANDIDATES` is the only cap on the ranking
(#675): the ReAct loop lists every ranked candidate — its class, its path relative
to the input root and its score — as a numbered markdown list, which is also the
user-facing reply, and takes its membership from the ranking rather than
re-deciding what fits. A file the agent is not told exists is a file it cannot read.

**The slots are allocated over the classification (#595).** The cap is
the agent's whole view of the submission, and it used to be spent on one axis — "how
document-like is this?" — over a population #591 can classify into four, so whole
tiers vanished: svhps26 named 14 interchangeable plate readouts and *none* of its 8
GraphPad analysis files, each carrying a kilobyte of readable content. Every class
present now takes a floor, so a tier is never wholly absent, and the surplus is
split **max-min fair** (`_fair_shares`), not spent in rank order.
`raw_data_file` takes its floor and stands out of the redistribution — #598 established it is the one tier whose members are
interchangeable, so a sixth gamma-counter printout says nothing the first five did,
while a sixth protocol is a different experiment — but it still absorbs whatever the
other classes leave unspent, because an empty slot helps nobody.

## Deposit Structure (`builder/tools/deposit_structure.py`, `builder/agents/structure.py`)

The deposit's studies and assays, read from its folder tree once, before the loop
starts (`build.read_deposit_structure`, #852). The tree is summarised
deterministically (one line per folder to depth 4, files counted by classification,
document and data names, the README excerpt the ranking read), one drafter-tier call
labels it as studies of assays (`structure.derive_deposit_structure`, [model tiering](react-loop.md#model-tiering-issue-96)), and
the labelling is kept only when fixed checks accept it: every named folder holds a
scanned file, no assay lies inside another, every assay lies inside its study, at
least one assay. A folder of documents alone is still an assay (S-VHPS27 deposits its
assays with the data to follow). A failed check, a model error or no provider stores
the fallback — one study, one assay, at the root — and either way one line says what
the deposit was read as. The call takes the run's `ModelOverrides` and `UsageSink`
from its caller, so `--model X` pins it and its tokens reach the same profile ledger
the ReAct model node writes (#399, #608). The structure rides in
`CrateState.deposit_structure` and the session, so a resumed build keeps it. The
build reads it for the folder a Study or an Assay is identified by (D13), so the
validation fingerprint covers it; it mints no Study and no Assay — drafting those is
the agent's.
