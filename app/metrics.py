"""Request accounting for the console.

The hot path stays in memory — a deque of recent calls, so a page can render
without a query — but the durable copy lives in ``app/store.py``. Before that,
every redeploy reset the counters, which made "a deploy broke something" look
identical to "nothing has happened yet".

One writer (`record`), many readers (`snapshot` / `recent_page`), all under a
plain threading lock: FastAPI runs handlers on the event loop, but media work
happens in worker threads and the background loops touch this too.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings

MAX_ROWS = 4000


@dataclass
class Call:
    ts: float
    kind: str                       # chat / chat.stream / image / video / speech / music
    model: str
    account: str
    ok: bool
    status: int
    latency_ms: int
    request_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    client: str = ""

    def dump(self) -> dict:
        return {
            "ts": self.ts,
            "time": _clock(self.ts),
            "kind": self.kind,
            "model": self.model,
            "account": self.account,
            "ok": self.ok,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "request_id": self.request_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "error": self.error,
            "client": self.client,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Call":
        return cls(
            ts=row["ts"], kind=row["kind"], model=row["model"], account=row["account"],
            ok=bool(row["ok"]), status=row["status"], latency_ms=row["latency_ms"],
            request_id=row.get("request_id") or "",
            prompt_tokens=row.get("prompt_tokens") or 0,
            completion_tokens=row.get("completion_tokens") or 0,
            error=row.get("error") or "", client=row.get("client") or "",
        )


_TZ: object | None = None
_TZ_NAME = ""


def _tz():
    """Display timezone, taken from SIGNIN_TIMEZONE.

    The container runs on UTC, so ``time.localtime`` printed times eight hours
    behind what the operator sees on their own clock — which reads as "the log
    is broken", not "the container is in UTC". Formatting explicitly makes the
    page independent of the host's TZ setting.
    """
    global _TZ, _TZ_NAME
    name = (settings.signin_timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
    if _TZ is not None and _TZ_NAME == name:
        return _TZ
    _TZ_NAME = name
    try:
        _TZ = ZoneInfo(name)
    except Exception:  # noqa: BLE001
        # No tzdata (a bare Windows box, a stripped image) or a bad name. UTC is
        # always available and never wrong by a random offset, which a guessed
        # +08:00 would be for anyone outside China.
        import logging

        logging.getLogger("minimax.metrics").warning(
            "timezone %r unavailable; falling back to UTC for log timestamps", name
        )
        _TZ = timezone.utc
    return _TZ


def _clock(ts: float) -> str:
    # Date included on purpose: history spans days now, and "12:04:11" alone
    # cannot tell today from the day before yesterday.
    return datetime.fromtimestamp(ts, _tz()).strftime("%m-%d %H:%M:%S")


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[idx]


class Metrics:
    def __init__(self, limit: int = MAX_ROWS) -> None:
        self._lock = threading.Lock()
        self._rows: deque[Call] = deque(maxlen=limit)
        self._seq = 0
        self._store = None
        self.started_at = time.time()

    # -- wiring ------------------------------------------------------------
    def attach(self, store) -> None:
        """Point the metrics at a durable store (``app.store.Store``)."""
        self._store = store

    def _durable(self) -> bool:
        return self._store is not None and getattr(self._store, "available", False)

    # -- write -------------------------------------------------------------
    def record(
        self,
        kind: str,
        model: str,
        account: str = "",
        ok: bool = True,
        status: int = 200,
        latency_ms: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str = "",
        client: str = "",
    ) -> Call:
        with self._lock:
            self._seq += 1
            row = Call(
                ts=time.time(),
                kind=kind,
                model=model or "-",
                account=account or "-",
                ok=ok,
                status=status,
                latency_ms=max(0, int(latency_ms)),
                request_id=f"{int(time.time() * 1000):x}-{self._seq:x}",
                prompt_tokens=max(0, int(prompt_tokens or 0)),
                completion_tokens=max(0, int(completion_tokens or 0)),
                error=(error or "")[:300],
                client=client,
            )
            self._rows.append(row)
        if self._store is not None:
            self._store.write(row)
        return row

    # -- read --------------------------------------------------------------
    def rows(self, hours: float | None = None) -> list[Call]:
        """Rows in the window, from the store when it is open.

        Memory is only a fallback: it cannot answer for anything older than the
        current process, which is precisely the question this exists to answer.
        """
        if hours and self._durable():
            self._store.flush()
            return [Call.from_row(row) for row in self._store.window(hours)]
        with self._lock:
            rows = list(self._rows)
        if hours:
            floor = time.time() - hours * 3600
            return [r for r in rows if r.ts >= floor]
        return rows

    def recent_page(self, limit: int = 200, offset: int = 0, query: str = "",
                    outcome: str = "") -> dict:
        """Newest-first page plus the number of rows matching the filter."""
        if self._durable():
            self._store.flush()
            rows, total = self._store.tail(limit, offset, query, outcome)
            return {
                "rows": [self._dump_row(row) for row in rows],
                "total": total,
                "source": "sqlite",
                "buffered": total,
            }
        rows = self._filtered(query, outcome)
        total = len(rows)
        page = list(reversed(rows))[offset:offset + limit]
        return {
            "rows": [r.dump() for r in page],
            "total": total,
            "source": "memory",
            "buffered": len(self.rows()),
        }

    def _filtered(self, query: str, outcome: str) -> list[Call]:
        rows = self.rows()
        if outcome == "success":
            rows = [r for r in rows if r.ok]
        elif outcome == "error":
            rows = [r for r in rows if not r.ok]
        q = (query or "").strip().lower()
        if q:
            rows = [
                r for r in rows
                if q in r.model.lower()
                or q in r.account.lower()
                or q in r.error.lower()
                or q in r.request_id.lower()
                or q in r.client.lower()
            ]
        return rows

    def recent(self, limit: int = 200, query: str = "", outcome: str = "") -> list[dict]:
        return self.recent_page(limit=limit, query=query, outcome=outcome)["rows"]

    @staticmethod
    def _dump_row(row: dict) -> dict:
        out = dict(row)
        out["time"] = _clock(row["ts"])
        out["total_tokens"] = (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
        return out

    def snapshot(self, hours: float = 24) -> dict:
        rows = self.rows(hours)
        return {
            "window_hours": hours,
            "started_at": self.started_at,
            "uptime_seconds": round(time.time() - self.started_at),
            "source": "sqlite" if self._durable() and hours else "memory",
            "store": self._store.stats() if self._store is not None else None,
            "total": self._rollup(rows),
            "series": self._series(rows, hours),
            "by_model": self._group(rows, "model"),
            "by_account": self._group(rows, "account"),
        }

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _rollup(rows: list[Call]) -> dict:
        ok = [r for r in rows if r.ok]
        latencies = [r.latency_ms for r in ok if r.latency_ms]
        prompt = sum(r.prompt_tokens for r in rows)
        completion = sum(r.completion_tokens for r in rows)
        return {
            "requests": len(rows),
            "success": len(ok),
            "errors": len(rows) - len(ok),
            "success_rate": round(len(ok) / len(rows) * 100, 1) if rows else 0.0,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
            "p95_latency_ms": _percentile(latencies, 0.95),
        }

    @staticmethod
    def _group(rows: list[Call], key: str) -> list[dict]:
        buckets: dict[str, list[Call]] = {}
        for row in rows:
            buckets.setdefault(getattr(row, key) or "-", []).append(row)
        out = []
        for name, items in buckets.items():
            ok = [r for r in items if r.ok]
            latencies = [r.latency_ms for r in ok if r.latency_ms]
            out.append({
                key: name,
                "requests": len(items),
                "success": len(ok),
                "errors": len(items) - len(ok),
                "success_rate": round(len(ok) / len(items) * 100, 1) if items else 0.0,
                "prompt_tokens": sum(r.prompt_tokens for r in items),
                "completion_tokens": sum(r.completion_tokens for r in items),
                "total_tokens": sum(r.prompt_tokens + r.completion_tokens for r in items),
                "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
                "last_ts": max(r.ts for r in items),
            })
        out.sort(key=lambda b: b["total_tokens"] or b["requests"], reverse=True)
        return out

    @staticmethod
    def _series(rows: list[Call], hours: float) -> dict:
        if hours <= 2:
            bucket = 300
        elif hours <= 12:
            bucket = 900
        elif hours <= 48:
            bucket = 3600
        else:
            bucket = 21600
        now = time.time()
        start = now - hours * 3600
        slots = int((now - start) // bucket) + 1
        points = [
            {"t": start + i * bucket, "prompt": 0, "completion": 0, "requests": 0, "errors": 0}
            for i in range(slots)
        ]
        for row in rows:
            idx = int((row.ts - start) // bucket)
            if 0 <= idx < len(points):
                point = points[idx]
                point["prompt"] += row.prompt_tokens
                point["completion"] += row.completion_tokens
                point["requests"] += 1
                if not row.ok:
                    point["errors"] += 1
        return {"bucket_seconds": bucket, "points": points}


metrics = Metrics()
