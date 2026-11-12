"""Durable request history, so a redeploy does not erase the evidence.

The counters used to live only in memory, which meant every `docker compose up`
after an image change started the console at zero: request count, success rate
and per-account tallies all reset while the tokens on disk survived. That made
"this deploy broke something" indistinguishable from "nothing has happened yet".

SQLite is used rather than a server because it is in the standard library, the
write pattern is append-only, and the whole thing has to fit in a container with
a read-only root (the database lives on the ``/data`` volume next to the token
files).

Writes are batched on a background thread: a request must never wait on a disk
sync, and losing the last second of rows to a SIGKILL is an acceptable trade.
If the database cannot be opened at all, the store stays inert and the gateway
keeps working with memory-only counters — a console is not worth failing for.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("minimax.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    kind            TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    account         TEXT    NOT NULL,
    ok              INTEGER NOT NULL,
    status          INTEGER NOT NULL,
    latency_ms      INTEGER NOT NULL,
    request_id      TEXT    NOT NULL,
    prompt_tokens   INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    error           TEXT    NOT NULL,
    client          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_account ON calls(account, ts);
"""

COLUMNS = ("ts, kind, model, account, ok, status, latency_ms, request_id, "
           "prompt_tokens, completion_tokens, error, client")

# A single query never pulls more than this. The cap exists so a 30-day window
# cannot turn a status page into a memory spike.
MAX_WINDOW_ROWS = 300_000


def default_path() -> str:
    """``/data/metrics.db`` in the container, ``data/metrics.db`` on a laptop."""
    from .config import settings

    raw = (settings.metrics_db or "").strip()
    if raw.lower() in {"off", "0", "none", "-"}:
        return ""
    if raw:
        return raw
    root = Path("/data")
    return str((root if root.is_dir() else Path("data")) / "metrics.db")


def _row_to_dict(row: tuple) -> dict:
    return {
        "ts": row[0], "kind": row[1], "model": row[2], "account": row[3],
        "ok": bool(row[4]), "status": row[5], "latency_ms": row[6],
        "request_id": row[7], "prompt_tokens": row[8], "completion_tokens": row[9],
        "error": row[10], "client": row[11],
    }


class Store:
    """Append-only request log with a batched writer."""

    def __init__(self, path: str = "", retention_days: float = 30.0) -> None:
        self.path = ""
        self.retention_days = retention_days
        self.error = ""
        self.written = 0
        self.dropped = 0
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._counter_cache: tuple[float, dict] = (0.0, {})
        if path:
            self.open(path, retention_days)

    # -- lifecycle ---------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._conn is not None

    def open(self, path: str, retention_days: float = 30.0) -> bool:
        self.close()
        self.path = path or ""
        self.retention_days = retention_days
        self.error = ""
        self.written = 0
        self.dropped = 0
        self._stopping = False
        if not self.path:
            self.error = "no path configured"
            return False
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.commit()
            self._conn = conn
            self._queue = queue.Queue(maxsize=50_000)
            removed = self.purge()
            log.info("request store ready: %s (%d old rows purged)", self.path, removed)
        except Exception as exc:  # noqa: BLE001 - a broken store must not stop the gateway
            self.error = str(exc)[:200]
            self._conn = None
            self._queue = None
            log.warning("request store unavailable (%s); counters stay in memory", self.error)
            return False
        self.start()
        return True

    def start(self) -> None:
        if not self.available or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._flush_loop, name="store-flush", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stopping = True
        self.flush()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3)
        with self._lock:
            conn, self._conn = self._conn, None
            self._queue = None
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- writes ------------------------------------------------------------
    def write(self, row) -> None:
        """Queue one call row. Never blocks, never raises."""
        if not self.available or self._queue is None:
            return
        try:
            self._queue.put_nowait((
                row.ts, row.kind, row.model, row.account, int(bool(row.ok)), row.status,
                row.latency_ms, row.request_id, row.prompt_tokens, row.completion_tokens,
                row.error, row.client,
            ))
        except queue.Full:
            self.dropped += 1

    def _flush_loop(self) -> None:
        last_purge = time.time()
        while not self._stopping:
            time.sleep(1.0)
            self.flush()
            # Retention has to hold for a long-running container too, not only
            # across restarts: one sweep every six hours is plenty for a table
            # that only grows by appends.
            if time.time() - last_purge > 21_600:
                removed = self.purge()
                if removed:
                    log.info("purged %d row(s) older than %.0f day(s)",
                             removed, self.retention_days)
                last_purge = time.time()

    def flush(self) -> int:
        """Drain the queue into the database. Returns rows written."""
        if not self.available or self._queue is None:
            return 0
        batch: list[tuple] = []
        while len(batch) < 500:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            try:
                conn.executemany(
                    f"INSERT INTO calls ({COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                conn.commit()
                self.written += len(batch)
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)[:200]
                self.dropped += len(batch)
                log.warning("dropping %d row(s): %s", len(batch), self.error)
                return 0
        return len(batch)

    def purge(self) -> int:
        """Delete rows older than the retention window (0 keeps everything)."""
        if not self.available or self.retention_days <= 0:
            return 0
        floor = time.time() - self.retention_days * 86_400
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            cur = conn.execute("DELETE FROM calls WHERE ts < ?", (floor,))
            conn.commit()
            return cur.rowcount or 0

    # -- reads -------------------------------------------------------------
    def tail(self, limit: int = 200, offset: int = 0, query: str = "",
             outcome: str = "") -> tuple[list[dict], int]:
        """Newest-first page of rows plus the total matching the filter."""
        if not self.available:
            return [], 0
        where, params = self._filter(query, outcome)
        with self._lock:
            conn = self._conn
            if conn is None:
                return [], 0
            try:
                total = conn.execute(f"SELECT COUNT(*) FROM calls{where}", params).fetchone()[0]
                rows = conn.execute(
                    f"SELECT {COLUMNS} FROM calls{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                ).fetchall()
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)[:200]
                return [], 0
        return [_row_to_dict(r) for r in rows], int(total)

    @staticmethod
    def _filter(query: str, outcome: str) -> tuple[str, tuple]:
        clauses: list[str] = []
        params: list[str] = []
        if outcome == "success":
            clauses.append("ok = 1")
        elif outcome == "error":
            clauses.append("ok = 0")
        q = (query or "").strip().lower()
        if q:
            like = f"%{q}%"
            clauses.append("(lower(model) LIKE ? OR lower(account) LIKE ? OR lower(error) LIKE ? "
                           "OR lower(request_id) LIKE ? OR lower(client) LIKE ?)")
            params.extend([like] * 5)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)

    def window(self, hours: float) -> list[dict]:
        """Every row inside the last ``hours``, oldest first, capped."""
        if not self.available:
            return []
        floor = time.time() - hours * 3600
        with self._lock:
            conn = self._conn
            if conn is None:
                return []
            try:
                rows = conn.execute(
                    f"SELECT {COLUMNS} FROM calls WHERE ts >= ? ORDER BY id ASC LIMIT ?",
                    (floor, MAX_WINDOW_ROWS),
                ).fetchall()
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)[:200]
                return []
        return [_row_to_dict(r) for r in rows]

    def account_totals(self, ttl: float = 60.0) -> dict:
        """Cumulative per-account tallies across all retained history.

        Aggregated from the rows themselves rather than maintained in a second
        table: one writer, one source of truth. A counter table drifting from
        the log is exactly what makes a console untrustworthy.
        """
        if not self.available:
            return {}
        cached_at, cached = self._counter_cache
        if cached and time.time() - cached_at < ttl:
            return cached
        with self._lock:
            conn = self._conn
            if conn is None:
                return {}
            try:
                rows = conn.execute(
                    "SELECT account, COUNT(*), SUM(ok), SUM(prompt_tokens), "
                    "SUM(completion_tokens), MAX(CASE WHEN ok = 1 THEN ts ELSE 0 END) "
                    "FROM calls GROUP BY account"
                ).fetchall()
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)[:200]
                return {}
        totals = {
            label: {
                "attempts": int(count),
                "ok": int(ok or 0),
                "fail": int(count) - int(ok or 0),
                "tokens_in": int(prompt or 0),
                "tokens_out": int(completion or 0),
                "last_success": float(last or 0),
            }
            for label, count, ok, prompt, completion, last in rows
        }
        self._counter_cache = (time.time(), totals)
        return totals

    def stats(self) -> dict:
        rows, oldest = 0, None
        with self._lock:
            conn = self._conn
            if conn is not None:
                try:
                    rows = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
                    oldest = conn.execute("SELECT MIN(ts) FROM calls").fetchone()[0]
                except Exception:  # noqa: BLE001
                    pass
        return {
            "path": self.path,
            "available": self.available,
            "mode": "sqlite" if self.available else "memory",
            "rows": rows,
            "oldest_ts": oldest,
            "retention_days": self.retention_days,
            "written": self.written,
            "dropped": self.dropped,
            "queued": self._queue.qsize() if self._queue is not None else 0,
            "error": self.error or None,
        }


store = Store()  # inert until main's lifespan opens it


def open_default(retention_days: float | None = None) -> Store:
    """Open the module-level store at the configured path."""
    from .config import settings

    store.open(
        default_path(),
        settings.metrics_retention_days if retention_days is None else retention_days,
    )
    return store
