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
import math
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import catalog, media, signin
from .auth import Token, token_manager
from .config import settings
from .metrics import metrics
from .probe import probes
from .security import authorized

def _guard(request: Request) -> None:
    if not authorized(request):
        raise HTTPException(status_code=401, detail="invalid gateway api key")


router = APIRouter(prefix="/admin", tags=["console"], dependencies=[Depends(_guard)])

VERSION = "console-2"
SIGNIN_DAILY = 400  # credits a single account's check-in yields on a normal day


def _uptime() -> int:
    return round(time.time() - metrics.started_at)


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
    pool = token_manager.describe()
    snap = metrics.snapshot(24)
    probe = probes.snapshot()
    return {
        "service": {
            "version": VERSION,
            "region": settings.region,
            "base_url": settings.base_url,
            "uptime_seconds": _uptime(),
            "proxy": bool(settings.upstream_proxy),
            "key_required": bool(settings.api_keys),
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
    pool = token_manager.describe()
    snap = metrics.snapshot(24)
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
    return metrics.snapshot(hours)


@router.get("/logs")
async def logs(limit: int = 200, q: str = "", outcome: str = "") -> dict:
    limit = max(1, min(limit, 1000))
    rows = metrics.recent(limit=limit, query=q, outcome=outcome)
    return {"rows": rows, "count": len(rows), "buffered": len(metrics.rows())}


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


@router.get("/capacity")
async def capacity(hours: float = 24, days: int = 7) -> dict:
    hours = max(1.0, min(hours, 720))
    days = max(1, min(days, 90))
    snap = metrics.snapshot(hours)
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

    remaining = totals.get("credits_remaining") or 0.0
    elapsed_hours = max(0.01, (time.time() - metrics.started_at) / 3600)
    window_days = hours / 24
    tokens_per_day = round(total["total_tokens"] / window_days) if total["total_tokens"] else 0
    requests_per_day = round(total["requests"] / window_days) if total["requests"] else 0

    days_left = round(remaining / drain_per_day, 1) if drain_per_day > 0 else None
    supply_per_day = SIGNIN_DAILY * max(1, pool["pool_size"])

    advice: list[dict] = []
    if drain_per_day <= 0:
        advice.append({
            "level": "info",
            "title": "还测不出积分消耗速率",
            "text": "积分台账需要至少两次间隔 30 分钟以上的采样才能算出日均消耗。"
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
            "消耗速率只认积分台账的实测差值（总剩余下降速度），不按 token 折算 —— 不同模型单价不同，折算会给出假精度。",
            f"补给按每号每天签到 {SIGNIN_DAILY} 积分计（第 4、7 天为 1000，这里取保守值）。",
            "所有数字来自本进程内存 + 探针缓存；容器重启后从零开始采样。",
        ],
    }


@router.get("/config")
async def config() -> dict:
    """Effective configuration, secrets masked. Read-only on purpose.

    Editing runtime config would mean either a writable state file or a restart;
    both are worse than telling the operator what to change in ``.env``.
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
        ]),
        ("鉴权", [
            ("网关密钥", mask(settings.api_keys[0]) if settings.api_keys else "未设置（不鉴权）",
             f"{len(settings.api_keys)} 个密钥在轮换"),
        ]),
        ("号池", [
            ("号池目录", settings.token_pool or "（未设置，用单 token）", "TOKEN_POOL"),
            ("扫描间隔", f"{settings.pool_watch:.0f} 秒", "TOKEN_POOL_WATCH，0 关闭"),
            ("冷却：401", "600 秒", "凭证失效"),
            ("冷却：402", "900 秒", "额度耗尽"),
            ("冷却：429", "60 秒", "被限流"),
        ]),
        ("签到", [
            ("自动签到", "开" if settings.signin_auto else "关", "SIGNIN_AUTO"),
            ("时区", settings.signin_timezone, "必须是 IANA 名字，不能写数字偏移"),
            ("间隔", f"{settings.signin_interval_hours:.0f} 小时", "SIGNIN_INTERVAL_HOURS"),
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
        "env_file": ".env（改动需 docker compose up -d 重建容器）",
    }
