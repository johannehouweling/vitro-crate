# Initialization: scanning, classification, deposit structure

> What runs before the agent loop: the file inventory, the one classification per file, the document ranking, and the read of the deposit's studies and assays.

## Scanner (`builder/tools/scanner.py`)

Builds the raw inventory of an input directory or archive (path, size, mime type, first lines)
and never reads a whole large file into context. An archive is extracted to
`<stem>_extracted/`, which becomes the session's `input_path`: every source, relative path and
crate name resolves against it (#819). Restricted by
[the scan-root guard rails](architecture.md#guard-rails-approved-scan-roots).

- **Pruning.** `_safe_walk` prunes hidden / `.git` / `__MACOSX` directories **in place**
  (`dirnames[:] = ...`) so they are never descended; a post-hoc filter loses that.
- **MIME.** `_detect_mime_type`: stdlib `mimetypes`, then `_SCIENTIFIC_MIME_TYPES`, then a
  text sniff. The registry precedes the sniff so binaries are never `text/plain`; unknown
  binary and any header with a NUL byte are `application/octet-stream`.
  `encoding_format_for_name` gives the same derivation with no disk read.
- **Read contract.** Readers in `file_readers.py` share the 100 MB `_MAX_BYTES` ceiling.
  `read_file` returns text/JSON **in full** up to `_TEXT_BUDGET_BYTES` (64 KiB), since a
  clipped return makes weak models loop; over budget it appends the marker `[truncated:
  showing first 64 KiB of N KiB; this is the maximum for this tool — do not re-read]`. That
  marker is verbatim in the tool description and the system prompt too: change all three or
  none. A directory returns "use list_scanned_files" guidance and a reader's `None` becomes an
  "unreadable/too-large — skip it" message — **never a silent `None`**, which makes a weak
  model re-call the reader. Reasoning-log entries carry a bounded repr of the call args.
- **Loop-breaker (ReAct only).** The `_run` wrapper in `_build_langchain_tools` counts
  identical calls returning the same non-progress result (`_is_non_progress_result`); after
  `_LOOP_BREAKER_THRESHOLD` (3) it refuses the call and returns the live `list_scanned_files`
  inventory. Any distinct call or progress result resets the count.

## File classification & document discovery (`builder/tools/document_discovery.py`)

Two questions about each file, answered once and read by everything; the rules live in the
docstrings of `classify_file`, `classify_scanned_files`, `_allocate_slots` and `_fair_shares`.

**One classification, four values (#591).** `classify_file` returns one of `metadata`
(deposit record, metadata workbooks, publications, plate maps — a plate map states intended
design, not a measurement) / `protocol` (SOPs, protocols, scripts) / `raw_data_file` /
`processed_data_file`, plus the deciding signal.

- The order is **content → filename → path**, load-bearing both ways (an extension outranking
  content files an instrument PDF as a protocol; a path outranking the filename files
  `assay1_rawdata/README.txt` as data). Exception: a script (`SCRIPT_SUFFIXES`) is decided by
  its extension, because its text is code (#786).
- In the filename step *what a file is* outranks *which tier* (a paper titled
  "Normalization of…" is a publication). Terms match at a word boundary, open at the end, so
  `raw` refuses "drawings" and `process` refuses "unprocessed". A word-processor file is never
  placed by its folder.
- **Stamped on every scanned file** (`FileClassification.classification`), not the ranked
  subset: what the crate is built from must not depend on a context window.
  `classification_of` answers for an unstamped record from the record alone, without disk.
- Files are grouped by `(directory, extension)` and a *spread* sample of each is read; an
  agreeing sample's verdict covers the group. A group is read in full when it is under four
  times the sample, when its sample disagrees, or when the verdict is not `raw_data_file` (the
  only interchangeable tier). A propagated file has no preview (#587). Known limit: a
  content-only exception inside a large uniform folder is missed.
- Which file is the **design table** is not a classification: `populate_condition_table`
  writes only rows satisfying `data_content.condition_table_fit` (a well key and columns
  mapping onto the canonical schema).
- `File.role` is a free-text *stamp*, not the classification: a reader accepts only a value in
  `FILE_CLASSES` and classifies otherwise — **nothing takes the field at its word**. It never
  reaches the crate (`_crate_mapping` drops it).

Consumers: `composites._deposited_outputs` / `_deposit_evidences` ([the toolbox](toolbox.md)),
`provenance.attach_files` (stamps the role unless one is named), the ReAct `open_items`, and
the maturity report's "data files included" row.

**Ranking.** `discover_documents` ranks readable documentation, reusing the classification's
previews, and reports `kind` (descriptor / tabular / narrative / opaque) beside the class.
`MAX_DOCUMENT_CANDIDATES` is the only cap (#675): the loop lists every ranked candidate and
never re-decides membership, since a file the agent is not told of cannot be read. Slots are
allocated over the classification (#595): every class present gets a floor and the surplus is
split **max-min fair** (`_fair_shares`); `raw_data_file` keeps its floor but stays out of the
redistribution (#598), absorbing only what other classes leave unspent.

## Deposit Structure (`builder/tools/deposit_structure.py`, `builder/agents/structure.py`)

Read once before the loop (`build.read_deposit_structure`, #852). A deterministic summary of
the folder tree (depth 4, files counted by class, names, the README excerpt) is labelled as
studies of assays by one drafter-tier call (`structure.derive_deposit_structure`,
[model tiering](react-loop.md#model-tiering-issue-96)). The labelling is kept only if every
named folder holds a scanned file, no assay nests in another, every assay lies in its study,
and there is at least one assay; a folder of documents alone is still an assay. Otherwise —
failed check, model error, no provider — the fallback is one study, one assay, at the root;
either way one line says what the deposit was read as. The call takes the run's
`ModelOverrides` and `UsageSink`, so `--model` pins it and its tokens reach the profile ledger
(#399, #608). It is kept in `CrateState.deposit_structure` and the session, and the build reads
it for the folder a Study or Assay is identified by (D13), so the validation fingerprint covers
it. It mints no Study or Assay; drafting those is the agent's.
