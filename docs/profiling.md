# Profiling

The ISA-Tox RO-Crate Builder records structured timing data for every tool call and
graph node execution. This data is written to `sessions/<session_id>/profile.ndjson`
as newline-delimited JSON (NDJSON).

## Enabling Profiling

Profiling is **always active** — every agent session automatically writes a
`profile.ndjson` file. The verbosity of console logging is controlled by
the `-v` / `-vv` flags:

- **Default (no flag):** WARNING and above — quiet, no per-file timing. Under
  `--interactive` with no `-v`/`-vv` this is raised to INFO automatically so that
  pipeline progress is visible; an explicitly requested verbosity is never downgraded.
- **`-v`:** INFO level — ensures you see summary timing (e.g. "Scan complete: 150 files in 2.34s").
- **`-vv`:** DEBUG level — per-file scan timing, read timing, and scan progress every
  100 files. Recommended for troubleshooting slow operations. Profiler events are never
  mirrored to the console at any verbosity: read them from `profile.ndjson` itself, or
  with `--dashboard`.

### Interactive vs Batch Mode

- **Interactive mode** (`--interactive`): The Rich spinner shows the current tool name.
  With `-vv`, the spinner updates more frequently because tool-internal DEBUG messages
  appear. Profile data accumulates normally in `profile.ndjson`.
- **Batch mode** (no `--interactive`): No agent runs — the input is scanned, a summary
  is printed and the command exits, so `profile.ndjson` is created but stays empty.
  Console output is quiet — use `-v` or `-vv` to see progress.

## Event Schema

Every line in `profile.ndjson` is a JSON object with **required fields**:

| Field      | Type   | Always present | Description |
|------------|--------|----------------|-------------|
| `event`    | string | Yes            | Event type name (see below) |
| `timestamp`| string | Yes            | ISO 8601 UTC timestamp |

### Event Types

| Event | Optional fields | When emitted |
|-------|----------------|--------------|
| `tool_call` | `tool`, `duration_ms`, `iteration`, `args`, `result` | After each tool execution completes. `result` is the stringified return value, truncated to 500 characters. |
| `node_start` | `node`, `iteration` | When a graph node begins execution |
| `node_end` | `node`, `duration_ms`, `iteration`, `messages_in`, `messages_out`, `produced_tool_calls`, `tools`, `input_tokens`, `output_tokens`, `model_name`, `response_text` | When a graph node finishes execution. For `"node": "model"` events, `input_tokens`, `output_tokens`, `model_name`, and `response_text` are populated from the LLM response (when available). `response_text` is truncated to ~2000 characters. |
| `tool_start` | `tool`, `iteration`, `args` | *Before* a tool begins executing, so a long call is visible while it runs; the matching `tool_call` follows on return. |
| `tool_failed` | `tool`, `iteration`, `args`, `error` | When a tool raises. A raising tool writes no `tool_call`, so this is its only record. `error` is truncated to 300 characters. |
| `tool_suppressed` | `tool`, `iteration`, `args`, `reason` | When a ReAct guard refuses a tool call before dispatch. No `tool_start` or `tool_call` follows, so without this the model bouncing off a guard is indistinguishable from idle time. |
| `hitl_wait` | `tool` | Emitted *before* the agent blocks on a human-in-the-loop tool (`present_to_human` / `request_input`). The matching `tool_call` event for the same `tool` is written only after the human responds, so a trailing `hitl_wait` with no following `tool_call` marks an open ⏸ pause. The dashboard uses this for its ▶/⏸ status badge (issue #193). |

`duration_ms` is stored **unrounded** so that sub-millisecond tool and node timings
survive — rounding at write time collapsed fast calls to `0.0`. Consumers round at
display or analysis time.

### Example Lines

```json
{"event": "tool_call", "tool": "scan_files", "duration_ms": 2345.6,
 "timestamp": "2026-06-21T12:30:45", "iteration": 3,
 "args": "{'path': '/data/experiment'}"}

{"event": "node_start", "node": "model",
 "timestamp": "2026-06-21T12:30:46"}

{"event": "node_end", "node": "model", "duration_ms": 1200.5,
 "timestamp": "2026-06-21T12:30:47", "iteration": 3,
 "messages_in": 5, "messages_out": 1, "produced_tool_calls": true,
 "input_tokens": 350, "output_tokens": 240, "model_name": "gpt-4o"}

{"event": "tool_failed", "tool": "lookup_compound", "iteration": 4,
 "timestamp": "2026-06-21T12:30:48",
 "args": "{'name': 'unobtainium'}", "error": "TimeoutError: read timed out"}
```

## Analysing Profile Logs

### Using `jq`

```bash
# Top 5 slowest tool calls
jq -r 'select(.event == "tool_call") | [.tool, .duration_ms] | @tsv' \
  sessions/*/profile.ndjson | sort -k2 -rn | head -5

# Average tool call duration per tool
jq -r 'select(.event == "tool_call") | [.tool, .duration_ms] | @tsv' \
  sessions/*/profile.ndjson | awk '{sum[$1]+=$2; cnt[$1]++} END{for(t in sum) print t, sum[t]/cnt[t]}' \
  | sort -k2 -rn

# Node timing summary
jq -r 'select(.event == "node_end") | [.node, .duration_ms, .iteration] | @tsv' \
  sessions/*/profile.ndjson
```

### Using Python / pandas

```python
# pandas is not a project dependency — run this with `uv run --with pandas python`
import json
from pathlib import Path

import pandas as pd

records = []
for line in Path("sessions/test-session/profile.ndjson").read_text().splitlines():
    if line.strip():
        records.append(json.loads(line))

df = pd.DataFrame(records)
tool_calls = df[df["event"] == "tool_call"]
print(tool_calls.groupby("tool")["duration_ms"].agg(["count", "mean", "max"]))
```

### Using a simple shell script

```bash
# Count events by type
cut -d'"' -f4 sessions/*/profile.ndjson | sort | uniq -c | sort -rn
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No `profile.ndjson` created | Session directory not writable | Check permissions on `sessions/` |
| Profile file exists but is empty | No tool calls were made | Verify the agent ran |
| Spinner text not updating | DEBUG messages suppressed | Run with `-vv` |
| Very large profile file | Long agent session with many tool calls | Archive or rotate the file; profiling doesn't affect crate output |

## File Location

```
sessions/<session_id>/
├── crate_state.json
├── profile.ndjson         # ← profiling events
├── working_crate/
└── session.log
```
