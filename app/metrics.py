"""Request accounting for the console.

Deliberately in-memory: the gateway's whole job is forwarding, and asking it to
carry a database just so a status page can draw a chart is a poor trade. The
consequence is stated on the page itself — figures reset when the container
restarts — rather than being papered over.

One writer (`record`), many readers (`snapshot`), all under a plain threading
lock: FastAPI runs handlers on the event loop, but media work happens in worker
threads and the background loops touch this too.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

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


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


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
        self.started_at = time.time()

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
            return row

    # -- read --------------------------------------------------------------
    def rows(self, hours: float | None = None) -> list[Call]:
        with self._lock:
            rows = list(self._rows)
        if hours:
            floor = time.time() - hours * 3600
            return [r for r in rows if r.ts >= floor]
        return rows

    def recent(self, limit: int = 200, query: str = "", outcome: str = "") -> list[dict]:
        rows = self.rows()
        q = (query or "").strip().lower()
        if outcome == "success":
            rows = [r for r in rows if r.ok]
        elif outcome == "error":
            rows = [r for r in rows if not r.ok]
        if q:
            rows = [
                r for r in rows
                if q in r.model.lower()
                or q in r.account.lower()
                or q in r.error.lower()
                or q in r.request_id.lower()
                or q in r.client.lower()
            ]
        return [r.dump() for r in reversed(rows[-limit:])]

    def snapshot(self, hours: float = 24) -> dict:
        rows = self.rows(hours)
        return {
            "window_hours": hours,
            "started_at": self.started_at,
            "uptime_seconds": round(time.time() - self.started_at),
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
