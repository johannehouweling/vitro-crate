# D9: Approved Scan Roots (Security Guard Rail)

The agent reaches only directories the user approved (`CrateState.approved_scan_roots`): a path
is resolved canonically and must equal or lie under an approved root. Roots come only from a
user-provided input path (`initialize()`) or a real HITL approval. This keeps the LLM, or an
injected prompt, away from arbitrary files and records which directories were ever accessed.

**Fail-closed (#197):**

- The engine always passes a concrete allowlist (`set()`, never `None`); the scanner walks nothing
  when it is `None` or empty. Never reintroduce `None` as "no roots": it reads as "no guard".
- The agent's own `scan_files` never adds a root; no path is approved by being scanned first.
- `scanner._is_forbidden_root` refuses `/`, the home directory itself, `/System`, `/Library`,
  `/private`, `/var`, `/etc`, `/usr`, bare `/Users` and `/Volumes` even when approved, also in
  `engine._directory_to_approve`. Subdirectories are unaffected.
- `SimulatedHumanInterface` rejects `purpose="scan_root"`. `SmokeTestHumanInterface` is
  interactive, so it reaches the escalation, and refuses it in the class itself before any choice
  is consulted — never inherited from the console's pre-selection, which falls back to the LAST
  option. An unattended mode never approves filesystem access (`tests/test_smoke_test_mode.py`).
- The eval runs behind `SimulatedHumanInterface` (`eval/tests/test_headless_engine.py`). An eval
  interface must never claim `is_interactive = True`: that would also un-gate the
  RECOMMENDED/OPTIONAL validation escalation.

**Reads and writes too (#167).** `scanner._contain(candidate, approved_roots)` (realpath, root
check, denylist, fail-closed) guards the read-tool dispatch in `engine.run_tool` (`read_file`,
`read_excel`, `read_docx`, `read_file_sample`, `read_multiple_files`, `extract_pdf_text`,
`preview_archive`, `unzip_file`), `_crate_mapping._file_dest` (an escaping `dest_path` becomes
`data/<slug>`) and `_crate_mapping._file_source` (a source escaping `input_path` is refused). The
scanner's own readers stay unguarded so `scan_files` can sample; the gate is at orchestration.
