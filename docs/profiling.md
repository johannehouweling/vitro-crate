# Profiling

The ISA-Tox RO-Crate Builder records structured timing data for every tool call and
graph node execution. This data is written to `sessions/<session_id>/profile.ndjson`
as newline-delimited JSON (NDJSON).

## Enabling Profiling

Profiling is **always active** — every agent session automatically writes a
`profile.ndjson` file. The verbosity of console logging is controlled by
the `-v` / `-vv` flags:

- **Default (no flag):** INFO and above — normal progress messages, no per-file timing.
- **`-v`:** INFO level — ensures you see summary timing (e.g. "Scan complete: 150 files in 2.34s").
- **`-vv`:** DEBUG level — per-file scan timing, read timing, and all profiler events
  are also shown in the console. Recommended for troubleshooting slow operations.

### Interactive vs Batch Mode

- **Interactive mode** (`--interactive`): The Rich spinner shows the current tool name.
  With `-vv`, the spinner updates more frequently because tool-internal DEBUG messages
  appear. Profile data accumulates normally in `profile.ndjson`.
- **Batch mode** (no `--interactive`): Profiling data is written to `profile.ndjson`
  normally. Console output is quiet — use `-v` or `-vv` to see progress.

## Event Schema

Every line in `profile.ndjson` is a JSON object with **required fields**:

| Field      | Type   | Always present | Description |
|------------|--------|----------------|-------------|
| `event`    | string | Yes            | Event type name (see below) |
| `timestamp`| string | Yes            | ISO 8601 UTC timestamp |

### Event Types

| Event | Optional fields | When emitted |
|-------|----------------|--------------|
| `tool_call` | `tool`, `duration_ms`, `iteration`, `args` | After each tool execution completes |
| `node_start` | `node`, `iteration` | When a graph node begins execution |
| `node_end` | `node`, `duration_ms`, `iteration`, `messages_in`, `messages_out`, `produced_tool_calls`, `tools` | When a graph node finishes execution |
| `scan_progress` | `processed`, `total`, `duration_ms` | Every 100 files during scanning (DEBUG level only) |

### Example Lines

```json
{"event": "tool_call", "tool": "scan_files", "duration_ms": 2345.6,
 "timestamp": "2026-06-21T12:30:45", "iteration": 3,
 "args": "{'path': '/data/experiment'}"}

{"event": "node_start", "node": "model",
 "timestamp": "2026-06-21T12:30:46"}

{"event": "node_end", "node": "model", "duration_ms": 1200.5,
 "timestamp": "2026-06-21T12:30:47", "iteration": 3,
 "messages_in": 5, "messages_out": 1, "produced_tool_calls": true}

{"event": "scan_progress", "processed": 100, "total": 350,
 "duration_ms": 1234.5, "timestamp": "2026-06-21T12:30:45"}
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
import json
from pathlib import Path

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
