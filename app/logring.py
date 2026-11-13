"""A bounded in-memory tail of this process's own log.

Warnings like ``account x hit a transport failure (502); trying next`` are the
only place some failures exist at all — a request that never reached a model
leaves no row in the history, and the traceback of a retry burst is spread over
half a dozen lines. Today those land in ``docker logs`` and nowhere else, which
means diagnosing "it broke at 10:38" costs an ssh session to the box.

A ring of the last few hundred warning-and-above records costs a few kilobytes
and turns that into something the console can show, and the operator can paste
into a report. It is deliberately *not* a log pipeline: no persistence, no
levels below WARNING, no formatting.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

MAX_RECORDS = 400


class RingHandler(logging.Handler):
    """Keep the last ``capacity`` records at WARNING or above."""

    def __init__(self, capacity: int = MAX_RECORDS) -> None:
        super().__init__(level=logging.WARNING)
        self._rows: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.counts = {"warning": 0, "error": 0}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not raise here
            message = str(record.msg)
        exc = ""
        if record.exc_info:
            try:
                exc = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                exc = ""
        row = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": message[:800],
            "exc": exc[-1500:] if exc else "",
        }
        with self._lock:
            self._rows.append(row)
            if record.levelno >= logging.ERROR:
                self.counts["error"] += 1
            else:
                self.counts["warning"] += 1

    # -- read --------------------------------------------------------------
    def tail(self, limit: int = 60, level: str = "") -> list[dict]:
        """Newest last, so a page can read top-down like a log file."""
        with self._lock:
            rows = list(self._rows)
        if level:
            rows = [r for r in rows if r["level"].upper() == level.upper()]
        return rows[-limit:] if limit > 0 else rows

    def stats(self) -> dict:
        with self._lock:
            rows = list(self._rows)
        return {
            "buffered": len(rows),
            "capacity": self._rows.maxlen,
            **self.counts,
        }

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()
            self.counts = {"warning": 0, "error": 0}


ring = RingHandler()


def install() -> RingHandler:
    """Attach the ring to the root logger (idempotent).

    Root, not ``minimax.*``: an ``asyncio`` "Task exception was never
    retrieved" or an ``httpx`` warning is exactly the kind of line that
    explains a mystery 502, and neither carries our logger name.
    """
    logger = logging.getLogger()
    for handler in logger.handlers:
        if isinstance(handler, RingHandler):
            return handler
    logger.addHandler(ring)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        # Keep the console's own view at the same verbosity as the container's;
        # the handler filters to WARNING anyway.
        logger.setLevel(logging.INFO)
    return ring
