"""Per-account upstream probes: the sign-in panel and credit batches.

Both are cheap read-only GETs, but there is one round trip per account and the
account host is not always fast. The console therefore never fans them out on
page load: a background loop keeps a cache warm, `/admin/probes` serves that
cache, and `?refresh=1` forces a synchronous re-read when the operator asks for
one explicitly.

Credits deserve a caveat that the UI repeats: the upstream ledger only lists
batches it has something to say about (granted / consumed / expired). An account
that has never been granted anything returns `total_count: 0` with no `details`
key at all, which is *not* an error — it is an empty ledger.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from . import signin
from .config import settings

log = logging.getLogger("minimax.probe")

CREDIT_PATH = "/minimax-cloud/api/v1/credit/details"
PAGE_SIZE = 100
MAX_PAGES = 5


@dataclass
class AccountProbe:
    label: str
    signin: dict | None = None
    credits: list[dict] = field(default_factory=list)
    credit_total: int = 0
    error: str = ""
    updated_at: float = 0.0

    def dump(self) -> dict:
        batches = self.credits
        remaining = sum(float(b.get("remaining_amount") or 0) for b in batches)
        granted = sum(float(b.get("granted_amount") or 0) for b in batches)
        consumed = sum(float(b.get("consumed_amount") or 0) for b in batches)
        now_ms = time.time() * 1000
        live = [b for b in batches if float(b.get("remaining_amount") or 0) > 0]
        live.sort(key=lambda b: b.get("expire_at_ms") or 0)
        return {
            "label": self.label,
            "signin": self.signin,
            "credits": {
                "batches": len(batches),
                "remaining": round(remaining, 2),
                "granted": round(granted, 2),
                "consumed": round(consumed, 2),
                "expiring": [
                    {
                        "amount": round(float(b.get("remaining_amount") or 0), 2),
                        "expire_at_ms": b.get("expire_at_ms"),
                        "days_left": round(((b.get("expire_at_ms") or 0) - now_ms) / 86_400_000, 1),
                    }
                    for b in live[:5]
                ],
                "next_expiry_ms": (live[0].get("expire_at_ms") if live else None),
            },
            "error": self.error or None,
            "updated_at": self.updated_at,
        }


def _batches(token: str, region: str) -> tuple[list[dict], int]:
    """Walk the credit ledger. Returns ``(details, total_count)``."""
    host = signin._host(region)
    url = f"{host}{CREDIT_PATH}?page={{page}}&page_size={PAGE_SIZE}"
    rows: list[dict] = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        payload = signin._request("GET", url.format(page=page), token, proxy=settings.upstream_proxy or None)
        total = int(payload.get("total_count") or 0)
        chunk = payload.get("details") or []
        if not isinstance(chunk, list):
            break
        rows.extend(item for item in chunk if isinstance(item, dict))
        if len(rows) >= total or not chunk:
            break
    return rows, total


def probe_one(token: str, label: str, region: str, timezone: str) -> AccountProbe:
    """Blocking: one account, both endpoints. Runs in a worker thread."""
    item = AccountProbe(label=label, updated_at=time.time())
    try:
        panel = signin.status(token, region=region, timezone=timezone,
                              proxy=settings.upstream_proxy or None)
        item.signin = signin.summarize(panel)
    except Exception as exc:  # noqa: BLE001 - one account must not sink the sweep
        item.error = f"signin: {exc}"[:200]
    try:
        rows, total = _batches(token, region)
        item.credits = rows
        item.credit_total = total
    except Exception as exc:  # noqa: BLE001
        item.error = (item.error + " | " if item.error else "") + f"credits: {exc}"[:200]
    item.updated_at = time.time()
    return item


class ProbeCache:
    def __init__(self) -> None:
        self._items: dict[str, AccountProbe] = {}
        self._updated_at = 0.0
        self._lock = asyncio.Lock()
        # (timestamp, credits remaining across the pool). Capacity planning needs
        # a drain rate, and the only honest way to get one is to watch the
        # ledger move; a single snapshot cannot tell you a rate.
        self._history: deque[tuple[float, float]] = deque(maxlen=288)

    async def refresh(self, labels: list[tuple[str, str]] | None = None) -> dict:
        """Probe every pooled account (or just the given label/token pairs)."""
        async with self._lock:
            pairs = labels if labels is not None else [
                (t.label or t.short, t.access_token)
                for t in getattr(token_pool_ref(), "_tokens", [])
                if t.access_token
            ]
            sem = asyncio.Semaphore(4)

            async def one(label: str, token: str) -> None:
                async with sem:
                    item = await asyncio.to_thread(
                        probe_one, token, label, settings.region, settings.signin_timezone
                    )
                    self._items[label] = item

            await asyncio.gather(*(one(label, token) for label, token in pairs))
            self._updated_at = time.time()
            total = sum(
                float(b.get("remaining_amount") or 0)
                for item in self._items.values()
                for b in item.credits
            )
            self._history.append((self._updated_at, total))
            log.info("probes refreshed for %d account(s)", len(pairs))
            return self.snapshot()

    def snapshot(self) -> dict:
        items = list(self._items.values())
        owed = 0
        claimable = 0
        claimable_accounts = 0
        batches = 0
        remaining = 0.0
        for item in items:
            panel = item.signin or {}
            if panel.get("claimable"):
                claimable += int(panel.get("today_points") or 0) + int(panel.get("bonus_points") or 0)
                claimable_accounts += 1
            owed += int(panel.get("cycle_pending_points") or 0)
            batches += len(item.credits)
            remaining += sum(float(b.get("remaining_amount") or 0) for b in item.credits)
        return {
            "updated_at": self._updated_at,
            "age_seconds": round(time.time() - self._updated_at) if self._updated_at else None,
            "totals": {
                "accounts": len(items),
                "claimable_today": claimable,
                "claimable_accounts": claimable_accounts,
                "cycle_pending": owed,
                "credits_remaining": round(remaining, 2),
                "credit_batches": batches,
            },
            "history": [{"ts": ts, "remaining": round(value, 2)} for ts, value in self._history],
            "accounts": {item.label: item.dump() for item in items},
        }


def token_pool_ref():
    from .auth import token_manager

    return token_manager


probes = ProbeCache()


async def probe_loop(interval: float) -> None:
    """Warm the cache once, then keep it fresh. Never raises."""
    await asyncio.sleep(8)  # let the pool load and the first requests settle
    while True:
        try:
            await probes.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a probe sweep must not kill the app
            log.warning("probe sweep failed: %s", exc)
        await asyncio.sleep(max(60.0, interval))
