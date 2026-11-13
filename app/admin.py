"""Console backend: the read/act endpoints behind ``/ui``.

Everything here is key-gated with the same rule as the API surface. The console
is served from the same process, so it talks to these routes instead of trying
to infer state from ``/healthz``.

Design rule: these routes answer questions an operator actually asks —
"is the pool healthy", "what is it doing", "what will it run out of" — and never
fan out to upstream on a page load. The one place they do (``/admin/signin``,
``/admin/probes/refresh``) is an explicit button press.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import platform
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import catalog, logring, media, signin, upstream
from .auth import Token, token_manager
from .config import RUNTIME_NOTES, settings
from .metrics import metrics, stamp
from .onboard import importer
from .probe import probes
from .security import authorized
from .store import store

log = logging.getLogger("minimax.admin")

def _guard(request: Request) -> None:
    if not authorized(request):
        raise HTTPException(status_code=401, detail="invalid gateway api key")


router = APIRouter(prefix="/admin", tags=["console"], dependencies=[Depends(_guard)])

VERSION = "console-3"
SIGNIN_DAILY = 400  # credits a single account's check-in yields on a normal day


def _uptime() -> int:
    return round(time.time() - metrics.started_at)


def _with_totals(pool: dict) -> dict:
    """Fold the durable per-account tallies into the live pool description.

    Two different numbers, both true, and conflating them is why the console
    looked wrong: ``ok``/``fail`` count *this process*, while ``ok_total`` /
    ``fail_total`` count every retained request. They also differ in unit — an
    account is credited with an attempt even when the request was retried on
    the next account, so attempts can exceed the request count on the usage page.
    """
    totals = store.account_totals()
    if not totals:
        return pool
    for account in pool.get("accounts", []):
        row = totals.get(account.get("label") or "")
        if not row:
            continue
        account["attempts_total"] = row["attempts"]
        account["ok_total"] = row["ok"]
        account["fail_total"] = row["fail"]
        account["tokens_in_total"] = row["tokens_in"]
        account["tokens_out_total"] = row["tokens_out"]
        account["last_success_total"] = row["last_success"] or None
    return pool


def _alerts(pool: dict, usage: dict, probe_totals: dict) -> list[dict]:
    """Short, ranked reasons the pool needs attention. Empty means healthy."""
    out: list[dict] = []
    total = pool.get("pool_size") or 0
    if not total:
        out.append({"level": "bad", "key": "empty", "text": "号池是空的，任何请求都会失败"})
        return out
    usable = pool.get("usable") or 0
    if usable == 0:
        out.append({"level": "bad", "key": "down", "text": "所有账号都不可用（冷却或已禁用）"})
    elif usable < total * 0.6:
        out.append({
            "level": "warn",
            "key": "thin",
            "text": f"只有 {usable}/{total} 个账号可用，接近一半不在轮换里",
        })
    if pool.get("disabled"):
        out.append({
            "level": "warn",
            "key": "disabled",
            "text": f"{pool['disabled']} 个账号被手动禁用，不参与轮换",
        })
    if pool.get("cooling_quota"):
        out.append({
            "level": "warn",
            "key": "quota",
            "text": f"{pool['cooling_quota']} 个号额度用尽，已冷却到次日重置（签到最后会自动解除）",
        })
    rate = usage.get("success_rate")
    if usage.get("requests", 0) >= 10 and rate is not None and rate < 98:
        out.append({
            "level": "warn",
            "key": "success",
            "text": f"近 24 小时成功率 {rate}%，有 {usage.get('errors', 0)} 次失败",
        })
    if probe_totals.get("claimable_today"):
        out.append({
            "level": "ok",
            "key": "signin",
            "text": f"今日还有 {probe_totals['claimable_today']} 积分没领（{probe_totals.get('claimable_accounts', 0)} 个号）",
        })
    return out


@router.get("/summary")
async def summary() -> dict:
    """Light payload for the 5-second nav pulse."""
    pool = await asyncio.to_thread(lambda: _with_totals(token_manager.describe()))
    snap = await asyncio.to_thread(metrics.snapshot, 24)
    probe = probes.snapshot()
    return {
        "service": {
            "version": VERSION,
            "region": settings.region,
            "base_url": settings.base_url,
            "uptime_seconds": _uptime(),
            "proxy": bool(settings.upstream_proxy),
            "key_required": bool(settings.api_keys),
            "history": store.stats(),
        },
        "pool": {
            "size": pool["pool_size"],
            "usable": pool["usable"],
            "cooling": pool["cooling"],
            "disabled": pool["disabled"],
            "in_flight": pool["in_flight"],
            "watch_seconds": pool["pool_watch_seconds"],
        },
        "usage": snap["total"],
        "probes": probe["totals"],
        "alerts": _alerts(pool, snap["total"], probe["totals"]),
    }


@router.get("/overview")
async def overview() -> dict:
    pool = await asyncio.to_thread(lambda: _with_totals(token_manager.describe()))
    snap = await asyncio.to_thread(metrics.snapshot, 24)
    probe = probes.snapshot()
    return {
        "service": {
            "name": "minimax-code2api",
            "version": VERSION,
            "region": settings.region,
            "base_url": settings.base_url,
            "uptime_seconds": _uptime(),
            "proxy": bool(settings.upstream_proxy),
            "key_required": bool(settings.api_keys),
            "signin_auto": settings.signin_auto,
            "signin_timezone": settings.signin_timezone,
            "probe_auto": settings.probe_auto,
            "probe_interval": settings.probe_interval,
            "transport": settings.upstream_transport,
        },
        "pool": pool,
        "usage": snap["total"],
        "probes": probe,
        "alerts": _alerts(pool, snap["total"], probe["totals"]),
    }


@router.get("/usage")
async def usage(hours: float = 24) -> dict:
    hours = max(0.5, min(hours, 720))
    return await asyncio.to_thread(metrics.snapshot, hours)


@router.get("/logs")
async def logs(limit: int = 200, offset: int = 0, q: str = "", outcome: str = "") -> dict:
    limit = max(1, min(limit, 1000))
    page = await asyncio.to_thread(
        metrics.recent_page, limit, max(0, offset), q, outcome
    )
    return {
        "rows": page["rows"],
        "count": len(page["rows"]),
        "total": page["total"],
        "offset": max(0, offset),
        "buffered": page["buffered"],
        "source": page["source"],
    }


@router.get("/errors")
async def error_groups(hours: float = 24, limit: int = 8) -> dict:
    """Failure messages bucketed by text, most frequent first.

    The request log answers "what happened to this call"; this answers "what
    keeps happening", which is the question behind every 502 hunt.
    """
    hours = max(0.5, min(hours, 720))
    limit = max(1, min(limit, 50))
    groups = await asyncio.to_thread(metrics.error_groups, hours, limit)
    for row in groups:
        row["first"] = stamp(row["first_ts"]) if row.get("first_ts") else "—"
        row["last"] = stamp(row["last_ts"]) if row.get("last_ts") else "—"
    return {
        "hours": hours,
        "groups": groups,
        "total_failures": sum(row.get("count", 0) for row in groups),
    }


@router.get("/diagnostics")
async def diagnostics(lines: int = 80) -> dict:
    """Everything this process knows about itself, in one report.

    The console cannot tell "the gateway is broken" from "the browser could not
    reach the gateway" on its own, and the two need opposite fixes. Pairing the
    browser-side probes with this endpoint makes the difference visible: if the
    browser sees a 502 the gateway never logged, the fault is in the path
    between them.
    """
    lines = max(1, min(lines, 400))
    pool = await asyncio.to_thread(token_manager.describe)
    probe = probes.snapshot() if hasattr(probes, "snapshot") else {}
    store_stats = store.stats()
    recent = await asyncio.to_thread(metrics.recent_page, 6, 0, "", "error")
    groups = await asyncio.to_thread(metrics.error_groups, 24, 6)
    for row in groups:
        row["first"] = stamp(row["first_ts"]) if row.get("first_ts") else "—"
        row["last"] = stamp(row["last_ts"]) if row.get("last_ts") else "—"
    return {
        "generated_at": time.time(),
        "service": {
            "name": "minimax-code2api",
            "version": VERSION,
            "uptime_seconds": _uptime(),
            "region": settings.region,
            "base_url": settings.base_url,
            "proxy": settings.upstream_proxy or "",
            # The configured value is not necessarily the wire in use: an httpx
            # failure latches the process onto curl for a while.
            "transport": upstream.upstream.transport_state(),
            "key_required": bool(settings.api_keys),
            "python": platform.python_version(),
            "pid": os.getpid(),
            "timezone": settings.signin_timezone,
            "server_time": stamp(time.time()),
        },
        "pool": {
            "size": pool["pool_size"],
            "usable": pool["usable"],
            "cooling": pool["cooling"],
            "disabled": pool["disabled"],
            "in_flight": pool["in_flight"],
        },
        "store": store_stats,
        "probes": {
            "updated_at": probe.get("updated_at"),
            "age_seconds": probe.get("age_seconds"),
            "totals": probe.get("totals") or {},
        },
        "recent_errors": recent["rows"],
        "error_groups": groups,
        "log": [
            dict(row, time_label=stamp(row["ts"]))
            for row in logring.ring.tail(lines)
        ],
        "log_stats": logring.ring.stats(),
        "hints": [
            "浏览器报 502 而这里没有对应的「面板」行：请求没到网关，问题在浏览器到服务器之间"
            "（公司代理、VPN、运营商、端口转发），不是网关返回的。",
            "面板行有、但 status 0：网关没来得及回响应（进程重启或客户端中断）。",
            "请求日志里 kind=面板 的行只在 4xx/5xx 或超过 1.5 秒时写入，正常轮询不占地方。",
            "「服务端最近告警」是容器日志里 WARNING 以上的最后若干条，等同 docker logs 的关键部分。",
        ],
    }


@router.get("/probes")
async def get_probes(refresh: bool = False) -> dict:
    if refresh:
        return await probes.refresh()
    return probes.snapshot()


@router.post("/probes/refresh")
async def refresh_probes() -> dict:
    return await probes.refresh()


@router.get("/models")
async def models() -> dict:
    return {
        "object": "list",
        "data": [model.to_openai() for model in catalog.visible_models()],
        "media": {"video": list(media.VIDEO_MODELS)},
    }


@router.post("/signin")
async def claim_signin(request: Request) -> dict:
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - empty body means "all accounts"
        body = {}
    label = (body or {}).get("label") or None
    tokens = [t for t in getattr(token_manager, "_tokens", []) if t.access_token]
    if label:
        target = token_manager.find(label)
        if target is None:
            raise HTTPException(status_code=404, detail=f"unknown account: {label}")
        tokens = [target]

    async def one(token: Token) -> dict:
        name = token.label or token.short
        try:
            if token.is_expired() and token.refresh_token:
                await token_manager.renew(token)
            panel = await asyncio.to_thread(
                signin.status, token.access_token, settings.region,
                settings.signin_timezone, settings.upstream_proxy or None,
            )
            info = signin.summarize(panel)
            if not info["claimable"]:
                return {"label": name, "claimed": False, **info}
            result = await asyncio.to_thread(
                signin.claim, token.access_token, settings.region,
                settings.signin_timezone, settings.upstream_proxy or None,
            )
            if result.get("claim_result") == signin.JUST_CLAIMED:
                return {
                    "label": name,
                    "claimed": True,
                    "points": int(result.get("points") or 0),
                    "day_no": result.get("day_no"),
                    "expire_at_ms": result.get("expire_at_ms"),
                }
            return {"label": name, "claimed": False, "already": True, **info}
        except Exception as exc:  # noqa: BLE001 - one account must not sink the batch
            return {"label": name, "claimed": False, "error": str(exc)[:200]}

    results = await asyncio.gather(*(one(t) for t in tokens))
    # 签到的意义就是回血：签完把这些号从「额度冷却到次日」里放出来。
    for row in results:
        if not row.get("error"):
            token_manager.clear_quota_cooldown(row.get("label"))
    gained = sum(r.get("points") or 0 for r in results if r.get("claimed"))
    return {
        "results": results,
        "claimed": sum(1 for r in results if r.get("claimed")),
        "points": gained,
        "errors": sum(1 for r in results if r.get("error")),
    }


@router.post("/accounts/disable")
async def disable_account(request: Request) -> dict:
    body = await request.json()
    label = (body or {}).get("label")
    if not label:
        raise HTTPException(status_code=400, detail="label is required")
    disabled = bool((body or {}).get("disabled", True))
    token = token_manager.set_disabled(label, disabled)
    if token is None:
        raise HTTPException(status_code=404, detail=f"unknown account: {label}")
    return {"label": token.label or token.short, "disabled": token.disabled}


@router.post("/accounts/import")
async def import_accounts(request: Request) -> dict:
    """Mint tokens for pasted email/password pairs and drop them into the pool.

    Runs in the background (one job at a time); poll ``GET /admin/accounts/import``
    for per-account progress. Passwords are used once and never stored.
    """
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - body is required in practice
        body = {}
    text = (body or {}).get("text") or ""
    accounts = (body or {}).get("accounts")
    if not text and isinstance(accounts, list):
        text = "\n".join(
            f"{a.get('email', '')} {a.get('password', '')}" for a in accounts if isinstance(a, dict)
        )
    concurrency = (body or {}).get("concurrency")
    try:
        job = await importer.start(text, concurrency)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job": job}


@router.get("/accounts/import")
async def import_status() -> dict:
    return {"job": importer.snapshot()}


@router.get("/capacity")
async def capacity(hours: float = 24, days: int = 7) -> dict:
    hours = max(1.0, min(hours, 720))
    days = max(1, min(days, 90))
    snap = await asyncio.to_thread(metrics.snapshot, hours)
    total = snap["total"]
    pool = token_manager.describe()
    probe = probes.snapshot()
    totals = probe["totals"]

    history = probe.get("history") or []
    drain_per_day = 0.0
    span_hours = 0.0
    if len(history) >= 2:
        first, last = history[0], history[-1]
        span_hours = (last["ts"] - first["ts"]) / 3600
        if span_hours >= 0.5 and first["remaining"] > last["remaining"]:
            drain_per_day = (first["remaining"] - last["remaining"]) / span_hours * 24

    # Two credit numbers exist upstream and they answer different questions:
    # `balance` is the membership balance (real, can go negative), while the
    # ledger only lists paid packs and is empty for every free account. Show the
    # balance when the probe has one; the ledger is the historical fallback.
    balance = totals.get("balance")
    balance_known = bool(totals.get("balance_accounts"))
    remaining = float(balance) if (balance_known and balance is not None) else float(totals.get("credits_remaining") or 0)
    ledger_only = not totals.get("credit_batches") and not balance_known
    elapsed_hours = max(0.01, (time.time() - metrics.started_at) / 3600)
    window_days = hours / 24
    tokens_per_day = round(total["total_tokens"] / window_days) if total["total_tokens"] else 0
    requests_per_day = round(total["requests"] / window_days) if total["requests"] else 0

    # A negative balance cannot "last N days"; it is already spent past zero.
    days_left = round(remaining / drain_per_day, 1) if drain_per_day > 0 and remaining > 0 else None
    supply_per_day = SIGNIN_DAILY * max(1, pool["pool_size"])

    advice: list[dict] = []
    if ledger_only:
        advice.append({
            "level": "info",
            "title": "上游还没给出积分余额",
            "text": "探针尚未采到会员余额，有偿积分台账也没有在用批次（免费号本来就没有）。"
                    "点「刷新探针」拉一次余额；在此之前请按请求量和成功率判断供给。",
        })
    elif balance_known and (balance or 0) > 0:
        advice.append({
            "level": "ok",
            "title": f"可用积分合计 {round(remaining)}",
            "text": f"{totals.get('balance_accounts', 0)} 个号采到余额，"
                    f"其中 {totals.get('balance_zero', 0)} 个是 0（今天已花完或还没签到）。"
                    "余额只统计显式可花的积分（免费 + 付费）；免费额度按天另算，"
                    "所以 0 的号仍可能在跑，真跑空时回 402 并冷却到次日重置。",
        })
    elif balance_known:
        advice.append({
            "level": "info",
            "title": "所有号的可用积分都是 0",
            "text": "要么今天的 400 分还没领（点「全部签到」），要么已经花完。"
                    "0 不等于不能用：免费额度与积分分开计，真跑空时上游回 402，网关冷却到次日重置。",
            "action": "signin",
        })
    if drain_per_day <= 0:
        advice.append({
            "level": "info",
            "title": "还测不出余额消耗速率",
            "text": "要算日均消耗，至少需要两次间隔 30 分钟以上、余额真的变化过的采样。"
                    "后台每 " + str(int(settings.probe_interval // 60) or 1) + " 分钟采一次，"
                    "或者点「刷新探针」立刻再采一次。",
        })
    if days_left is not None:
        if days_left < 7:
            need = math.ceil(max(0.0, drain_per_day * days - remaining) / (SIGNIN_DAILY * days)) if days else 0
            advice.append({
                "level": "bad" if days_left < 3 else "warn",
                "title": f"余额约 {days_left} 天耗尽",
                "text": f"按每天 {round(drain_per_day)} 积分消耗、当前剩余 {round(remaining)} 积分估算；"
                        f"覆盖 {days} 天还差 {max(0, round(drain_per_day * days - remaining))} 积分，"
                        f"约需补 {need} 个号（每号每天签到 {SIGNIN_DAILY}）。",
                "action": "signin",
            })
        else:
            advice.append({
                "level": "ok",
                "title": f"余额可撑约 {days_left} 天",
                "text": f"每天消耗约 {round(drain_per_day)} 积分，签到每天补给约 {supply_per_day} 积分。",
            })
    if totals.get("claimable_accounts"):
        advice.append({
            "level": "warn",
            "title": f"{totals['claimable_accounts']} 个号今天还没签到",
            "text": f"可领 {totals.get('claimable_today', 0)} 积分。签到是唯一的免费补给，别让它过期。",
            "action": "signin",
        })
    if pool["pool_size"] and pool["usable"] < pool["pool_size"] * 0.7:
        advice.append({
            "level": "warn",
            "title": "近三成账号不在轮换里",
            "text": f"{pool['cooling']} 个冷却、{pool['disabled']} 个禁用。"
                    "冷却会自愈；禁用是人工的，确认是不是忘了打开。",
        })
    if total["requests"] >= 20 and total["success_rate"] < 98:
        advice.append({
            "level": "warn",
            "title": f"成功率 {total['success_rate']}%",
            "text": f"{hours:.0f} 小时里 {total['errors']} 次失败。看「请求日志」筛「失败」，"
                    "常见原因是额度耗尽（1008）和被上游限流（429）。",
        })

    forecast = []
    for window in (1, 3, 7, 14):
        gap = drain_per_day * window - remaining if drain_per_day > 0 else 0
        forecast.append({
            "days": window,
            "consume": round(drain_per_day * window),
            "remaining": round(remaining),
            "gap": round(max(0.0, gap)),
            "accounts_needed": math.ceil(gap / (SIGNIN_DAILY * window)) if gap > 0 else 0,
            "supply": SIGNIN_DAILY * max(1, pool["pool_size"]) * window,
        })

    return {
        "balance_known": balance_known,
        "generated_at": time.time(),
        "window_hours": hours,
        "horizon_days": days,
        "run_rate": {
            "requests_per_day": requests_per_day,
            "tokens_per_day": tokens_per_day,
            "credits_per_day": round(drain_per_day, 1),
            "supply_per_day": supply_per_day,
            "sample_span_hours": round(span_hours, 2),
            "observed_hours": round(elapsed_hours, 2),
        },
        "balance": {
            "credits_remaining": round(remaining, 1),
            "balance_known": balance_known,
            "balance_negative": totals.get("balance_negative", 0),
            "balance_zero": totals.get("balance_zero", 0),
            "credit_batches": totals.get("credit_batches", 0),
            "plans": totals.get("plans") or {},
            "days_left": days_left,
            "accounts": pool["pool_size"],
            "usable": pool["usable"],
        },
        "quality": {
            "requests": total["requests"],
            "success_rate": total["success_rate"],
            "avg_latency_ms": total["avg_latency_ms"],
            "p95_latency_ms": total["p95_latency_ms"],
            "by_model": snap["by_model"][:8],
        },
        "forecast": forecast,
        "advice": advice,
        "method": [
            "余额取会员接口的 op_credit_summary.total_remaining_amount（免费 + 付费）；"
            "同响应里的 total_remains_credit 恒为 0，不采用。有偿积分台账只记买过/发过的包，"
            "免费号恒为空，三者不叠加。",
            "消耗速率取「余额合计」的实测差值，不按 token 折算 —— 不同模型单价不同，折算会给出假精度。",
            f"补给按每号每天签到 {SIGNIN_DAILY} 积分计（第 4、7 天为 1000，这里取保守值）。",
            "余额为 0 的号仍可能在跑：免费额度与积分分开计。真跑空时上游回 402 insufficient balance，"
            "网关把该号冷却到次日重置，签到后自动解除。",
            "所有数字来自本进程内存 + 探针缓存；容器重启后从零开始采样。",
        ],
    }


@router.get("/config")
async def config() -> dict:
    """Effective configuration, secrets masked.

    The rows are the whole picture; ``editable`` is the subset that can be
    changed without recreating the container — the form in the console is built
    from it, so the two can never drift apart.
    """

    def mask(value: str) -> str:
        if not value:
            return "—"
        return value[:6] + "…" + value[-4:] if len(value) > 12 else "已设置"

    rows = [
        ("服务", [
            ("监听端口", os.environ.get("GATEWAY_PORT", "8080"), "容器内端口，改端口改 compose"),
            ("区域 region", settings.region, "决定上游域名与鉴权端点"),
            ("上游 base_url", settings.base_url, "messages 端点前缀"),
            ("出站代理", settings.upstream_proxy or "直连", "UPSTREAM_PROXY"),
            ("传输方式", settings.upstream_transport, "auto = httpx 失败后回落 curl"),
            ("请求体上限", f"{settings.request_body_limit // (1024 * 1024)} MB", "REQUEST_BODY_LIMIT"),
            ("默认 max_tokens", str(settings.default_max_tokens), "客户端没给时的兜底"),
            ("重试次数", str(settings.retry_attempts), "RETRY_ATTEMPTS，面板可改；0 = 只试一次"),
        ]),
        ("鉴权", [
            ("网关密钥", mask(settings.api_keys[0]) if settings.api_keys else "未设置（不鉴权）",
             f"{len(settings.api_keys)} 个密钥在轮换"),
        ]),
        ("号池", [
            ("号池目录", settings.token_pool or "（未设置，用单 token）", "TOKEN_POOL"),
            ("扫描间隔", f"{settings.pool_watch:.0f} 秒", "TOKEN_POOL_WATCH，0 关闭"),
            ("冷却：401", "600 秒", "凭证失效，等网关自己续期"),
            ("冷却：402", "到次日 00:10", "额度用尽，签到时自动解除"),
            ("冷却：429", "60 秒", "被限流"),
        ]),
        ("签到", [
            ("自动签到", "开" if settings.signin_auto else "关", "SIGNIN_AUTO"),
            ("时区", settings.signin_timezone, "必须是 IANA 名字，不能写数字偏移"),
            ("间隔", f"{settings.signin_interval_hours:.0f} 小时", "SIGNIN_INTERVAL_HOURS"),
        ]),
        ("历史", [
            ("请求历史", "磁盘" if store.available else "仅内存（重启清空）", "METRICS_DB"),
            ("落盘位置", store.path or "—", "在 /data 卷上，和 token 文件同一目录"),
            ("保留天数", f"{settings.metrics_retention_days:.0f} 天", "METRICS_RETENTION_DAYS"),
            ("已存行数", f"{store.stats()['rows']:,}", "含重启前的全部请求"),
            ("时间显示时区", settings.signin_timezone, "控制台按这个时区格式化，不依赖容器 TZ"),
        ]),
        ("控制台", [
            ("探针自动刷新", "开" if settings.probe_auto else "关", "PROBE_AUTO"),
            ("探针间隔", f"{settings.probe_interval:.0f} 秒", "PROBE_INTERVAL，0 = 只手动"),
        ]),
    ]
    return {
        "groups": [{"name": name, "rows": [
            {"key": label, "value": value, "note": note} for label, value, note in items
        ]} for name, items in rows],
        "editable": settings.editable_state(),
        "runtime_file": str(settings.runtime_path()),
        "runtime_notes": RUNTIME_NOTES,
        "env_file": ".env（只有上面「不可运行期修改」的项需要重建容器）",
    }


@router.post("/config")
async def update_config(request: Request) -> dict:
    """Change the editable knobs in place — no rebuild, no restart.

    The write lands in ``/data/runtime.json`` so it survives a container
    recreate, and takes effect on the next request because every consumer reads
    ``settings.<name>`` at use time. Two of them are baked into the httpx client
    (timeout, proxy), so those rebuild the client instead of waiting.
    """
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 - any malformed body is a 400
        raise HTTPException(status_code=400, detail=f"请求体不是 JSON：{exc}") from exc
    if not isinstance(body, dict) or not body:
        raise HTTPException(status_code=400, detail="没有要修改的项")
    if len(body) > 40:
        raise HTTPException(status_code=400, detail="一次最多改 40 项")

    applied, rejected = settings.apply_runtime(body)
    if applied and {"request_timeout", "upstream_transport"} & set(applied):
        await upstream.upstream.reload_client()
        log.info("config: rebuilt the upstream client after %s", sorted(applied))

    if applied:
        log.info("config: applied %s", applied)
    return {
        "applied": applied,
        "rejected": [{"key": key, "why": why} for key, why in rejected],
        "editable": settings.editable_state(),
        "runtime_file": str(settings.runtime_path()),
    }
