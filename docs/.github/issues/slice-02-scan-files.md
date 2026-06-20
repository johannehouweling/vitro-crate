# Slice 2: Scan Files Tool

## What to build

`scan_files(path)` — walks an input directory and builds a raw file inventory. For each file, records: relative path, filename, size in bytes, MIME type (detected from content/magic bytes), and first 20 rows (if CSV, TSV, or XLSX). No role classification, no ARC sorting — just a list of what's in the directory.

Also provides `read_file_sample(path, lines=20)` to read a sample of any text file, bounded to avoid loading large files into context.

The scanned file records are stored in `CrateState.scanned_files`.

## Acceptance criteria

- [ ] `scan_files(path)` walks a directory recursively
- [ ] Each entry: path, filename, size, mime_type, first_rows (CSV/TSV/XLSX only)
- [ ] MIME type detection uses python-magic or equivalent (not just file extension)
- [ ] First rows truncated to 20 rows, bounded to 10KB max
- [ ] Binary files report mime type and size but no first_rows
- [ ] `read_file_sample(path, lines)` returns first N lines for text files, None for binary
- [ ] Large files (>100MB) are skipped for `read_file_sample` with warning
- [ ] Results stored in `CrateState.scanned_files` with `reviewed_by_user: False`
- [ ] Tests: scan a directory with mixed files, verify all fields populated

## Blocked by

- Slice 1 (CrateState — stores scanned file records)