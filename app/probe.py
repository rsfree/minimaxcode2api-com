"""Per-account upstream probes: the sign-in panel, the credit ledger and the balance.

All three are cheap read-only calls, but there is one round trip per account and
the account host is not always fast. The console therefore never fans them out on
page load: a background loop keeps a cache warm, `/admin/probes` serves that
cache, and `?refresh=1` forces a synchronous re-read when the operator asks for
one explicitly.

Two different "credit" numbers exist upstream and conflating them is what made
the console show a dash:

* ``/minimax-cloud/api/v1/credit/details`` — the **paid** ledger. It lists packs
  that were granted or consumed, so an account that never bought one answers
  ``total_count: 0`` with no ``details`` key. Empty is not an error; it is the
  normal state of a free account.
* ``/matrix/api/v1/commerce/get_membership_info`` — the **balance**: plan name,
  ``total_remains_credit``, the free/paid split and the check-in gift. Free-plan
  accounts sit at ``0`` and some go *negative* (spend beyond a granted pack);
  neither stops them from serving, because requests are metered against a daily
  free allowance that upstream does not expose. A 402 ``insufficient balance
  (1008)`` is that allowance running out, not this number.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field

from . import signin
from .config import settings

log = logging.getLogger("minimax.probe")

CREDIT_PATH = "/minimax-cloud/api/v1/credit/details"
MEMBERSHIP_PATH = "/matrix/api/v1/commerce/get_membership_info"
PAGE_SIZE = 100
MAX_PAGES = 5


def _number(value) -> float | None:
    """Upstream mixes ints and decimal strings in the same payload."""
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def summarize_membership(payload: dict) -> dict:
    """Fold ``get_membership_info`` into the few numbers the console shows.

    The spendable figure is the ``op_credit_summary`` total (free + purchased).
    ``total_remains_credit`` — the obvious-looking field — reads 0 on every
    account we have ever probed, including ones holding 400 free credits, so it
    is kept only as a last-resort fallback and reported separately for debugging.
    """
    summary = payload.get("op_credit_summary") or {}
    op_total = _number(summary.get("total_remaining_amount"))
    legacy = _number(payload.get("total_remains_credit"))
    op_balance = _number(payload.get("opcredit_balance"))
    if op_total is not None:
        total = op_total
    elif legacy is not None:
        total = legacy
    else:
        total = op_balance
    return {
        "total": total,
        "credit": total,          # console-facing alias for the headline number
        "legacy_credit": legacy,  # total_remains_credit, for troubleshooting only
        "op_credit": op_balance,
        "free": _number(summary.get("free_remaining_amount")),
        "purchased": _number(summary.get("purchased_remaining_amount")),
        "gift": _number(payload.get("daily_login_gift_credit_remaining")),
        "plan": (payload.get("plan_name") or "").strip(),
        "subscription": (payload.get("subscription_type") or "").strip(),
        "token_plan": bool(payload.get("has_token_plan")),
        "expires_at_ms": payload.get("expires_at") or None,
    }


@dataclass
class AccountProbe:
    label: str
    signin: dict | None = None
    credits: list[dict] = field(default_factory=list)
    credit_total: int = 0
    membership: dict = field(default_factory=dict)
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
            "balance": self.membership or None,
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


def _membership(token: str, region: str) -> dict:
    host = signin._host(region)
    payload = signin._request(
        "POST", f"{host}{MEMBERSHIP_PATH}", token, body={},
        proxy=settings.upstream_proxy or None, unwrap=False,
    )
    return summarize_membership(payload)


def probe_one(token: str, label: str, region: str, timezone: str) -> AccountProbe:
    """Blocking: one account, all endpoints. Runs in a worker thread."""
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
    try:
        item.membership = _membership(token, region)
    except Exception as exc:  # noqa: BLE001
        item.error = (item.error + " | " if item.error else "") + f"balance: {exc}"[:200]
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
                    _publish_balance(item)
                    _release_if_topped_up(item)

            await asyncio.gather(*(one(label, token) for label, token in pairs))
            self._updated_at = time.time()
            self._history.append((self._updated_at, self._tracked_remaining()))
            log.info("probes refreshed for %d account(s)", len(pairs))
            return self.snapshot()

    def _tracked_remaining(self) -> float:
        """The number capacity planning watches.

        The membership balance when upstream answers it (it is the only figure
        that reflects spends), else the paid ledger. Everything is added across
        the pool; a negative total is legitimate and means the pool has spend
        beyond whatever was granted.
        """
        balances = [
            float(item.membership["credit"])
            for item in self._items.values()
            if item.membership.get("credit") is not None
        ]
        if balances:
            return round(sum(balances), 2)
        return round(sum(
            float(b.get("remaining_amount") or 0)
            for item in self._items.values()
            for b in item.credits
        ), 2)

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

        balances = [
            item.membership for item in items
            if item.membership and item.membership.get("credit") is not None
        ]
        credit_sum = round(sum(float(b["credit"]) for b in balances), 2) if balances else None
        plans = Counter((b.get("plan") or "未知") for b in balances)
        return {
            "updated_at": self._updated_at,
            "age_seconds": round(time.time() - self._updated_at) if self._updated_at else None,
            "totals": {
                "accounts": len(items),
                "claimable_today": claimable,
                "claimable_accounts": claimable_accounts,
                "cycle_pending": owed,
                # 真实积分余额。None = 还没采到；0 和负数都是有效值。
                "balance": credit_sum,
                "balance_accounts": len(balances),
                "balance_negative": sum(1 for b in balances if float(b["credit"]) < 0),
                "balance_zero": sum(1 for b in balances if float(b["credit"]) == 0),
                "balance_positive": sum(1 for b in balances if float(b["credit"]) > 0),
                "plans": dict(plans),
                # 有偿积分台账：只记买过/发过的包，免费号恒为空。
                "credits_remaining": round(remaining, 2),
                "credit_batches": batches,
            },
            "history": [{"ts": ts, "remaining": round(value, 2)} for ts, value in self._history],
            "accounts": {item.label: item.dump() for item in items},
        }


def token_pool_ref():
    from .auth import token_manager

    return token_manager


# Lifting a park is a small bet: the balance says the account can spend again,
# but the reading may predate the spend that got it parked. Bet once per half
# hour at most, so a wrong guess costs one failed request instead of a loop of
# park → probe → park that burns one call every PROBE_INTERVAL.
_LIFT_INTERVAL = 1800.0
_last_lift: dict[str, float] = {}


def _publish_balance(item: AccountProbe) -> None:
    """Hand the probed balance to the pool.

    The media path needs it before the request goes out: an account with 40
    credits can chat for a long time and cannot pay for one image (~115), so
    without a balance the only way to learn that is a 300 s round trip that
    ends in a 402. The hint is advisory — a stale or missing value never blocks
    a request, it only lets a *known* shortfall be skipped.
    """
    credit = (item.membership or {}).get("credit")
    if credit is None:
        return
    token_pool_ref().set_credit_hint(item.label, credit)


def _release_if_topped_up(item: AccountProbe) -> None:
    """Put a parked account back in rotation as soon as the balance is positive.

    Parks were lifted in exactly one place: the check-in sweep, right after it
    claimed. That missed every other way an account gets topped up — importing a
    batch that was already claimed elsewhere, a manual claim in the browser, a
    pack landing late — and the console then contradicted itself: "积分 400" in
    one column, "冷却至 00:10" and "insufficient balance (1008)" in the next,
    for the rest of the day.

    The probe already reads that balance every cycle, so let it lift the park.
    """
    if item.error:
        return
    try:
        credit = float((item.membership or {}).get("credit"))
    except (TypeError, ValueError):
        return
    if credit <= 0:
        return
    now = time.time()
    if now - _last_lift.get(item.label, 0.0) < _LIFT_INTERVAL:
        return
    if token_pool_ref().clear_quota_cooldown(item.label):
        _last_lift[item.label] = now
        log.info("probe: %s holds %.0f credits, lifted the quota park", item.label, credit)


probes = ProbeCache()


async def probe_loop(interval: float = 0.0) -> None:
    """Warm the cache once, then keep it fresh. Never raises.

    The interval and the on/off switch are read every lap, so the console can
    change them without a restart (``interval`` is only the startup fallback).
    """
    await asyncio.sleep(8)  # let the pool load and the first requests settle
    while True:
        if settings.probe_auto:
            try:
                await probes.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a probe sweep must not kill the app
                log.warning("probe sweep failed: %s", exc)
        await asyncio.sleep(max(60.0, settings.probe_interval or interval))
