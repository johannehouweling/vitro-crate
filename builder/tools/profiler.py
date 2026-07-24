"""Structured profiling logger for the ISA-Tox RO-Crate Builder.

Writes structured profiling events as NDJSON to
``sessions/<session_id>/profile.ndjson``.

Each line is a self-describing JSON object. The file is opened in
append mode so concurrent append-writes are safe (single-threaded
agent, but multiple saves over time accumulate cleanly).

Event schema examples::

    {"event": "tool_call", "tool": "scan_files", "duration_ms": 1234.5,
     "timestamp": "2026-06-21T12:30:45", "iteration": 3,
     "args": "{'path': '/data/...'}"}

    {"event": "node_start", "node": "model",
     "timestamp": "2026-06-21T12:30:45"}

    {"event": "node_end", "node": "tools", "duration_ms": 567.8,
     "timestamp": "2026-06-21T12:30:46", "iteration": 3}
"""

from __future__ import annotations

import json
import logging
from typing import Any

import builder.config as _config

logger = logging.getLogger(__name__)

SESSION_DIR = _config.session_root()


class ProfilingLogger:
    """Writes structured profiling events to ``sessions/<session_id>/profile.ndjson``.

    Each line is a JSON object (NDJSON format). The file is append-mode
    safe and degrades gracefully when the file system is not writable.
    """

    def __init__(self, session_id: str) -> None:
        """Open ``profile.ndjson`` for the given *session_id*.

        If the ``sessions/`` directory or the file cannot be created, a
        warning is logged and the logger operates in silent mode (all
        subsequent calls are no-ops).
        """
        self._file: Any | None = None
        self._session_id = session_id
        self._silent = False  # degraded mode flag

        if not session_id:
            logger.warning("ProfilingLogger: empty session_id — will not write profile.ndjson")
            self._silent = True
            return

        session_path = SESSION_DIR / session_id
        try:
            session_path.mkdir(parents=True, exist_ok=True)
            profile_path = session_path / "profile.ndjson"
            self._file = open(profile_path, "a")  # noqa: SIM115
        except OSError:
            logger.warning(
                "ProfilingLogger: could not open %s/profile.ndjson — profiling disabled",
                session_path,
                exc_info=True,
            )
            self._silent = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_event(
        self,
        event: str,
        tool: str | None = None,
        duration_ms: float | None = None,
        iteration: int | None = None,
        args: str | None = None,
        node: str | None = None,
        **extra: Any,
    ) -> None:
        """Write one event line to the profile file.

        Parameters
        ----------
        event:
            Event type name (e.g. ``"tool_call"``, ``"node_start"``).
        tool:
            Tool name (for tool-related events).
        duration_ms:
            Elapsed wall-clock time in milliseconds.
        iteration:
            Agent iteration counter at the time of the event.
        args:
            Stringified call arguments (truncated to avoid huge lines).
        node:
            Graph node name (e.g. ``"model"``, ``"tools"``).
        **extra:
            Any additional key-value pairs to include in the event.
        """
        if self._silent or self._file is None:
            return

        record: dict[str, Any] = {
            "event": event,
            "timestamp": _config.now().isoformat(),
        }
        if tool is not None:
            record["tool"] = tool
        if duration_ms is not None:
            # Store the unrounded duration so sub-millisecond tool/node timings
            # survive (rounding to 1 decimal collapsed fast calls to 0.0).
            # Consumers round at display/analysis time (see docs/profiling.md).
            record["duration_ms"] = duration_ms
        if iteration is not None:
            record["iteration"] = iteration
        if args is not None:
            record["args"] = args
        if node is not None:
            record["node"] = node
        record.update(extra)

        try:
            line = json.dumps(record, default=str) + "\n"
            self._file.write(line)
            self._file.flush()
        except OSError:
            logger.warning(
                "ProfilingLogger: write failed — disabling profiling",
                exc_info=True,
            )
            self._silent = True

    def log_tool_call(
        self,
        tool_name: str,
        duration_ms: float,
        iteration: int,
        args: str | None = None,
        result: str | None = None,
    ) -> None:
        """Convenience wrapper that logs a ``"tool_call"`` event.

        Parameters
        ----------
        tool_name:
            Name of the tool that was called.
        duration_ms:
            Wall-clock execution time in milliseconds.
        iteration:
            Agent iteration counter at the time of the call.
        args:
            Stringified call arguments (truncated, see ``log_event``).
        result:
            Stringified tool return value (truncated to 500 chars to
            avoid bloating the profile file).
        """
        self.log_event(
            event="tool_call",
            tool=tool_name,
            duration_ms=duration_ms,
            iteration=iteration,
            args=args,
            result=result,
        )

    def close(self) -> None:
        """Close the underlying file, if open.

        This method is idempotent — calling it multiple times is safe.
        """
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                logger.warning(
                    "ProfilingLogger: error closing profile.ndjson",
                    exc_info=True,
                )
            finally:
                self._file = None
                self._silent = True
