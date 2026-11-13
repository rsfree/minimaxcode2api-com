"""FastAPI front door.

Surface:

* ``POST /v1/chat/completions`` — OpenAI-compatible, streaming and buffered.
* ``POST /v1/messages``         — Anthropic Messages passthrough.
* ``GET  /v1/models``           — catalogue for client pickers.
* ``POST /v1/images/generations`` — image generation, Ark / Seedream request shape.
* ``POST /v1/videos/generations`` — async video submit; ``GET .../{task_id}`` polls.
* ``POST /v1/audio/speech``     — text to speech (mp3).
* ``GET  /v1/audio/voices``     — voice catalogue for the speech endpoint.
* ``POST /v1/music/generations``— music / BGM generation.
* ``GET  /healthz``             — liveness plus credential status.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from . import admin, media, metrics as metrics_mod, probe, security, store as store_mod
from .auth import CredentialError, token_manager
from .security import UI_COOKIE, UI_COOKIE_MAX_AGE, authorized as _authorized, is_loopback as _is_loopback
from .catalog import DEFAULT_MODEL, resolve_model, visible_media_models, visible_models
from .config import settings
from .convert import (
    ConversionError,
    build_anthropic_request,
    build_openai_response,
    iter_sse_events,
    stream_openai,
)
from .media import MediaError
from .upstream import UpstreamError, upstream

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("minimax.gateway")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Durable request history first: everything below writes into it, and it is
    # what makes a redeploy legible instead of a fresh zero.
    store_mod.open_default()
    metrics_mod.metrics.attach(store_mod.store)
    await upstream.startup()
    try:
        await token_manager.get()
    except CredentialError as exc:
        log.error("no usable credential at startup: %s", exc)
    task = asyncio.create_task(_signin_loop()) if _signin_enabled() else None
    watcher = asyncio.create_task(_pool_watch_loop()) if _pool_watch_enabled() else None
    probes_task = (
        asyncio.create_task(probe.probe_loop(settings.probe_interval))
        if settings.probe_auto and settings.probe_interval > 0
        else None
    )
    yield
    for pending in (task, watcher, probes_task):
        if pending is not None:
            pending.cancel()
    await upstream.shutdown()
    store_mod.store.close()


def _pool_watch_enabled() -> bool:
    return bool(settings.token_pool and settings.pool_watch > 0)


async def _pool_watch_loop() -> None:
    """Pick up accounts added to ``TOKEN_POOL`` without restarting the gateway.

    Provisioning writes one JSON file per account, so scaling the pool is just
    "drop the file in and wait one interval". Existing entries are never
    replaced by what is on disk — this process owns the rotated refresh tokens.
    """
    while True:
        await asyncio.sleep(max(2.0, settings.pool_watch))
        try:
            await token_manager.reload_if_changed()
        except Exception as exc:  # noqa: BLE001 - a bad file must not kill the loop
            log.warning("pool reload failed: %s", exc)


def _signin_enabled() -> bool:
    return bool(settings.signin_auto and settings.token_pool)


async def _refresh_before_checkin() -> None:
    """Renew expiring accounts here, where the pool lock serialises them.

    The check-in thread works straight from the files and cannot take that lock.
    If it refreshes an account at the same moment a request renews it, one of the
    two exchanges wins and the other gets ``invalid_grant`` — and upstream treats
    a reused refresh token as a leaked session, so the winner's fresh access
    token dies too. Refreshing everything due *before* the thread starts leaves
    it reading tokens that are still valid, so it never refreshes on its own.
    """
    tokens = [
        t for t in getattr(token_manager, "_tokens", [])
        if t.refresh_token and not t.disabled
    ]
    due = [t for t in tokens if t.is_expired()]
    if not due:
        return
    renewed = 0
    for token in due:
        if await token_manager.renew(token):
            renewed += 1
    log.info("pre-check-in renewal: %s/%s account(s) refreshed", renewed, len(due))


async def _signin_loop() -> None:
    """Claim the daily reward for every pooled account, then idle.

    Runs once at startup — which is what actually keeps a long-lived container
    topped up, since most deployments restart far more often than daily — and
    then every ``SIGNIN_INTERVAL_HOURS``.
    """
    from . import signin

    while True:
        try:
            await _refresh_before_checkin()
            rows = await asyncio.to_thread(
                partial(signin.claim_pool, allow_refresh=False),
                settings.token_pool,
                settings.region,
                settings.signin_timezone,
                settings.upstream_proxy or None,
            )
        except Exception as exc:  # noqa: BLE001 - never take the gateway down
            log.warning("daily check-in skipped: %s", exc)
        else:
            gained = sum(int(row.get("points") or 0) for row in rows if row.get("claimed"))
            claimed = sum(1 for row in rows if row.get("claimed"))
            # A quota park lasts until the daily reset; a claim is the proof that
            # the new day's allowance (and the sign-in points) are in, so lift it
            # here rather than letting the account sit out another cycle.
            lifted = sum(
                1 for row in rows
                if not row.get("error") and token_manager.clear_quota_cooldown(row.get("label"))
            )
            if claimed:
                log.info("daily check-in: %s/%s accounts, +%s credits", claimed, len(rows), gained)
            else:
                log.info("daily check-in: nothing to claim across %s accounts", len(rows))
            if lifted:
                log.info("released %s account(s) parked on quota", lifted)
            # The check-in thread refreshes straight from files and writes the
            # rotated pair back there; a refresh token is single-use, so the
            # in-memory copy is now stale and a later renew would get
            # invalid_grant. Adopt what the thread just wrote.
            try:
                await token_manager.reload()
            except Exception as exc:  # noqa: BLE001 - never take the gateway down
                log.warning("pool resync after check-in failed: %s", exc)
        await asyncio.sleep(max(600.0, settings.signin_interval_hours * 3600))


app = FastAPI(
    title="minimax-code2api",
    version="1.0.0",
    description="OpenAI/Anthropic compatible gateway for the MiniMax Code managed channel.",
    lifespan=lifespan,
)
app.include_router(admin.router)


class AbsoluteTargetMiddleware:
    """Accept absolute-form request targets.

    Any client built on httpx obeys ``HTTP_PROXY`` for *every* host, localhost
    included. When a proxy sits in the environment the request line becomes
    ``POST http://127.0.0.1:8099/v1/chat/completions`` instead of an origin-form
    path, Starlette matches the whole URL as the path, and every endpoint
    answers 404. Rewriting the target back to origin-form keeps those clients
    working without asking the user to unset proxy variables.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            path = _origin_form(scope.get("path", ""))
            if path is not None:
                scope = dict(scope)
                scope["path"] = path
                scope["raw_path"] = path.encode()
        await self.inner(scope, receive, send)


def _origin_form(path: str) -> str | None:
    candidate = unquote(path) if "%" in path else path
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    parsed = urlsplit(candidate)
    return parsed.path or "/"


app.add_middleware(AbsoluteTargetMiddleware)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def error_response(status: int, message: str, code: str = "upstream_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code, "code": code}},
    )


def media_error_response(exc: MediaError) -> JSONResponse:
    """Surface a media tool failure with the upstream message intact.

    The 402 text from this channel is the only notice that the account balance is
    gone, so it is passed through verbatim instead of being rewritten.
    """
    body: dict[str, Any] = {
        "error": {
            "message": exc.message,
            "type": "insufficient_quota" if exc.status == 402 else "upstream_error",
            "code": exc.code,
        }
    }
    if exc.status_field:
        body["status"] = exc.status_field
    return JSONResponse(status_code=exc.status, content=body)


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Give every 500 a traceback and a history row.

    A caller relayed ``status_code=500, bad response status code 500`` while the
    console showed nothing at all: the only rows we wrote were successes and
    ``UpstreamError``s, so an unhandled exception left no evidence. That made a
    systematic bug look like an occasional 502. Now the traceback goes to the
    container log and one ``error`` row lands in the history, which is what the
    caller's ref number points at.
    """
    log.exception("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
    try:
        row = metrics_mod.metrics.record(
            "error", f"{request.method} {request.url.path}", ok=False, status=500,
            error=f"{type(exc).__name__}: {exc}",
            client=_client_tag(request),
        )
        ref = row.request_id
    except Exception:  # noqa: BLE001 - never let the handler mask the original
        ref = ""
    return error_response(500, f"internal error (ref {ref})", "internal_error")


async def _read_json(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > settings.request_body_limit:
        raise ConversionError(
            f"request body exceeds {settings.request_body_limit} bytes limit"
        )
    if not raw:
        raise ConversionError("request body is empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConversionError("request body must be a JSON object")
    return payload


def _client_tag(request: Request) -> str:
    """Short caller identity for the request log: forwarded IP, else peer."""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    host = forwarded or (request.client.host if request.client else "") or "-"
    agent = (request.headers.get("user-agent") or "")[:40]
    return f"{host} {agent}".strip()


def _usage_from_chunk(chunk: str) -> dict | None:
    """Pull the usage block out of one SSE write.

    ``stream_openai`` emits the upstream's final frame verbatim, so the usage
    object arrives as part of a ``data:`` line rather than through a callback.
    """
    for line in chunk.splitlines():
        if not line.startswith("data:"):
            continue
        text = line[5:].strip()
        if not text or text == "[DONE]":
            continue
        try:
            event = json.loads(text)
        except ValueError:
            continue
        usage = event.get("usage") if isinstance(event, dict) else None
        if isinstance(usage, dict):
            return usage
    return None


def _log_usage(kind: str, model: str, payload: dict, elapsed: float) -> None:
    usage = payload.get("usage") or {}
    log.info(
        "ok kind=%s model=%s in=%s out=%s %.2fs",
        kind,
        model,
        usage.get("input_tokens", usage.get("prompt_tokens", "?")),
        usage.get("output_tokens", usage.get("completion_tokens", "?")),
        elapsed,
    )


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------
@app.get("/")
async def root() -> dict:
    return {
        "name": "minimax-code2api",
        "endpoints": [
            "POST /v1/chat/completions",
            "POST /v1/messages",
            "GET /v1/models",
            "POST /v1/images/generations",
            "POST /v1/videos/generations",
            "GET /v1/videos/generations/{task_id}",
            "POST /v1/audio/speech",
            "GET /v1/audio/voices",
            "POST /v1/music/generations",
            "GET /v1/media/models",
            "GET /healthz",
            "GET /ui",
        ],
        "default_model": DEFAULT_MODEL,
        "models": [model.id for model in visible_models()],
        "video_models": list(media.VIDEO_MODELS),
    }


CONSOLE_HTML = Path(__file__).with_name("console.html")

# Cookie set by the login form so the console page itself is behind the same
# key as the API. Readable by JS on purpose: the console needs the key for
# `Authorization` on every fetch (localStorage is the other copy).
LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>minimax-code2api · 登录</title>
<style>
  :root{color-scheme:dark light}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;color:#e8ecf2;
       font:14px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
  @media (prefers-color-scheme:light){body{background:#f6f7f9;color:#1a1d23}
    .card{background:#fff;border-color:#e3e6eb;box-shadow:0 8px 30px rgba(15,17,21,.08)}
    input{background:#f6f7f9;border-color:#dfe3e9;color:#1a1d23}}
  .card{width:min(380px,92vw);padding:26px 24px;border:1px solid #242a34;border-radius:14px;
        background:#14171d;box-shadow:0 8px 30px rgba(0,0,0,.4)}
  h1{margin:0 0 4px;font-size:16px;letter-spacing:.2px}
  p{margin:0 0 18px;font-size:12.5px;color:#8b93a1}
  code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
  input{width:100%;padding:10px 12px;border-radius:9px;border:1px solid #2b323d;background:#0f1216;
        color:inherit;font:inherit;outline:none}
  input:focus{border-color:#3b82f6}
  button{margin-top:12px;width:100%;padding:10px 12px;border:0;border-radius:9px;cursor:pointer;
         background:#3b82f6;color:#fff;font:600 14px/1.4 inherit}
  button:hover{background:#2f74e0}
  .err{margin-top:12px;font-size:12.5px;color:#ef4444}
  .foot{margin-top:16px;font-size:11.5px;color:#78808e}
</style></head>
<body><form class="card" method="post" action="/ui/login">
  <h1>minimax-code2api</h1>
  <p>输入部署时设置的 <code>GATEWAY_API_KEY</code></p>
  <input type="password" name="key" placeholder="sk-..." autocomplete="current-password" autofocus required>
  <button type="submit">进入控制台</button>
  __ERROR__
  <div class="foot">登录状态保存在 cookie，30 天有效。</div>
</form></body></html>
"""


def _login_page(error: str = "") -> HTMLResponse:
    markup = LOGIN_HTML.replace("__ERROR__", f'<div class="err">{error}</div>' if error else "")
    return HTMLResponse(markup, status_code=401, headers={"cache-control": "no-store"})


@app.get("/ui", include_in_schema=False)
@app.get("/console", include_in_schema=False)
async def console(request: Request):
    """Single-file console (chat / image / speech / pool / models).

    Behind the same key as the API: the HTML itself is static, but the page
    names the pool, shows every account label and spends the account credits,
    so it is not something to leave open on a public IP. ``/ui?key=…`` logs in
    once and plants the cookie; ``/ui/login`` is the form.
    """
    if not _authorized(request):
        return _login_page()
    response = FileResponse(
        CONSOLE_HTML,
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-store"},
    )
    key = (request.query_params.get("key") or "").strip()
    if key and key in settings.api_keys:
        response.set_cookie(
            UI_COOKIE,
            key,
            max_age=UI_COOKIE_MAX_AGE,
            httponly=False,
            samesite="lax",
            path="/",
        )
    return response


@app.post("/ui/login", include_in_schema=False)
async def ui_login(request: Request):
    """Form login. Accepts ``application/x-www-form-urlencoded`` or JSON."""
    raw = (await request.body()).decode("utf-8", "ignore").strip()
    key = ""
    if raw.startswith("{"):
        try:
            key = str(json.loads(raw).get("key") or "").strip()
        except json.JSONDecodeError:
            key = ""
    elif raw:
        key = (parse_qs(raw).get("key") or [""])[0].strip()
    if not key:
        key = _supplied_key(request)
    if key not in settings.api_keys and settings.api_keys:
        return _login_page("密钥不对，检查 .env 里的 GATEWAY_API_KEY")
    response = RedirectResponse(url="/ui", status_code=303)
    if key:
        response.set_cookie(
            UI_COOKIE,
            key,
            max_age=UI_COOKIE_MAX_AGE,
            httponly=False,
            samesite="lax",
            path="/",
        )
    return response


@app.api_route("/ui/logout", methods=["GET", "POST"], include_in_schema=False)
async def ui_logout() -> RedirectResponse:
    response = RedirectResponse(url="/ui", status_code=303)
    response.delete_cookie(UI_COOKIE, path="/")
    return response


@app.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    # The body names every account label, so it is not public. The container
    # healthcheck calls it over loopback and therefore needs no key.
    if not (_authorized(request) or _is_loopback(request)):
        return JSONResponse(
            status_code=401,
            content={"status": "unauthorized", "error": "send the gateway api key"},
        )
    body: dict[str, Any] = {"status": "ok", "region": settings.region}
    body.update(token_manager.describe())
    body["base_url"] = settings.base_url
    body["proxy"] = bool(settings.upstream_proxy)

    token = token_manager.peek()
    if token is None:
        body["status"] = "degraded"
        body["error"] = (
            "no credential available: set MINIMAX_ACCESS_TOKEN, MINIMAX_REFRESH_TOKEN, "
            "or provide MINIMAX_AUTH_FILE / TOKEN_STORE"
        )
        return JSONResponse(status_code=503, content=body)

    if token.expires_at_ms:
        body["expires_at_ms"] = token.expires_at_ms
        body["expired"] = token.is_expired()
        if token.is_expired() and not token.refresh_token:
            body["status"] = "degraded"
            body["error"] = "access token expired and no refresh token available"
            return JSONResponse(status_code=503, content=body)
    return JSONResponse(status_code=200, content=body)


@app.get("/v1/models")
@app.get("/models")
async def list_models(include_media: bool = True) -> dict:
    """Every model the gateway can serve, chat models first.

    Multimodal ids (`minimax-image`, `MiniMax-H3`, …) are included by default —
    clients enumerate one list and a picker that silently hides half the
    gateway is worse than an extra id — and carry ``type`` so they can be
    filtered. ``?include_media=0`` gives the chat-only list.
    """
    data = [model.to_openai() for model in visible_models()]
    if include_media:
        data.extend(model.to_openai() for model in visible_media_models())
    return {"object": "list", "data": data}


@app.get("/v1/models/meta")
async def list_models_meta() -> dict:
    data = [model.to_meta() for model in visible_models()]
    data.extend(model.to_meta() for model in visible_media_models())
    return {"object": "list", "data": data}


# ---------------------------------------------------------------------------
# OpenAI compatible
# ---------------------------------------------------------------------------
@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")

    try:
        body = await _read_json(request)
        anthropic_payload = build_anthropic_request(body)
    except ConversionError as exc:
        return error_response(400, str(exc), "invalid_request_error")

    model = anthropic_payload["model"]
    started = time.time()
    client = _client_tag(request)

    if body.get("stream"):
        try:
            response = await upstream.messages_stream(anthropic_payload)
        except UpstreamError as exc:
            metrics_mod.metrics.record(
                "chat.stream", model, account=getattr(exc, "account", "") or "-",
                ok=False, status=exc.status,
                latency_ms=(time.time() - started) * 1000, error=exc.message, client=client,
            )
            return error_response(exc.status, exc.message)

        account = response.account

        async def relay():
            usage: dict = {}
            failed = ""
            try:
                events = iter_sse_events(response.aiter_lines())
                async for chunk in stream_openai(events, model):
                    # Usage rides on the final SSE frame; grabbing it here is the
                    # only way the console can report tokens for streamed calls.
                    if '"usage"' in chunk:
                        usage = _usage_from_chunk(chunk) or usage
                    yield chunk
            except Exception as exc:  # noqa: BLE001 - surface to the client
                failed = str(exc)
                log.warning("stream aborted: %s", exc)
                payload = {"error": {"message": str(exc), "type": "upstream_error"}}
                yield f"data: {json.dumps(payload)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                await response.aclose()
                _log_usage("chat.stream", model, {"usage": usage}, time.time() - started)
                metrics_mod.metrics.record(
                    "chat.stream", model, account=account, ok=not failed,
                    status=200 if not failed else 502,
                    latency_ms=(time.time() - started) * 1000,
                    prompt_tokens=usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
                    completion_tokens=usage.get("completion_tokens") or usage.get("output_tokens") or 0,
                    error=failed, client=client,
                )

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-store",
                "x-accel-buffering": "no",
                "connection": "keep-alive",
            },
        )

    try:
        payload, account = await upstream.messages_ex(anthropic_payload)
    except UpstreamError as exc:
        metrics_mod.metrics.record(
            "chat", model, account=getattr(exc, "account", "") or "-",
            ok=False, status=exc.status,
            latency_ms=(time.time() - started) * 1000, error=exc.message, client=client,
        )
        return error_response(exc.status, exc.message)

    _log_usage("chat", model, payload, time.time() - started)
    usage = payload.get("usage") or {}
    metrics_mod.metrics.record(
        "chat", model, account=account, ok=True, status=200,
        latency_ms=(time.time() - started) * 1000,
        prompt_tokens=usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
        completion_tokens=usage.get("output_tokens") or usage.get("completion_tokens") or 0,
        client=client,
    )
    return JSONResponse(content=build_openai_response(payload, model))


# ---------------------------------------------------------------------------
# Anthropic native passthrough
# ---------------------------------------------------------------------------
@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")

    try:
        body = await _read_json(request)
    except ConversionError as exc:
        return error_response(400, str(exc), "invalid_request_error")

    body = dict(body)
    body["model"] = resolve_model(body.get("model"))
    if not body.get("max_tokens"):
        body["max_tokens"] = settings.default_max_tokens
    model = body["model"]
    started = time.time()

    if body.get("stream"):
        try:
            response = await upstream.messages_stream(body)
        except UpstreamError as exc:
            return error_response(exc.status, exc.message)

        async def relay_raw():
            try:
                # aiter_bytes() transparently un-gzips; the raw variant would
                # leak compressed bytes into an SSE stream we do not label.
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                _log_usage("messages.stream", model, {}, time.time() - started)

        return StreamingResponse(
            relay_raw(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-store",
                "x-accel-buffering": "no",
                "connection": "keep-alive",
            },
        )

    try:
        payload = await upstream.messages(body)
    except UpstreamError as exc:
        return error_response(exc.status, exc.message)

    _log_usage("messages", model, payload, time.time() - started)
    return JSONResponse(content=payload)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Rough estimate; the channel exposes no tokenizer endpoint."""
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")
    try:
        body = await _read_json(request)
    except ConversionError as exc:
        return error_response(400, str(exc), "invalid_request_error")

    def text_of(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(text_of(item) for item in value)
        if isinstance(value, dict):
            return "".join(text_of(item) for item in value.values())
        return ""

    characters = len(text_of(body.get("system"))) + len(text_of(body.get("messages")))
    return {"input_tokens": max(1, characters // 3)}


@app.get("/favicon.ico")
async def favicon() -> PlainTextResponse:
    return PlainTextResponse("", status_code=204)


# ---------------------------------------------------------------------------
# multimodal — image / video / speech / music
# ---------------------------------------------------------------------------
def _as_url_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str) and item]
    return []


@app.get("/v1/media/models")
async def list_media_models() -> dict:
    """Capability catalogue for the multimodal endpoints."""
    return {
        "object": "list",
        "data": [
            {
                "id": "image",
                "object": "media.model",
                "type": "image",
                "endpoint": "POST /v1/images/generations",
                "options": {
                    "protocol": "Volcengine Ark POST /api/v3/images/generations",
                    "prompt": "required",
                    "image": "public URL or data: base64; string or array",
                    "size": "1K | 2K | 4K (1.5K/3K snap to the nearer tier) or WxH",
                    "response_format": "url | b64_json",
                    "sequential_image_generation": "auto + options.max_images, 1-15",
                    "ignored": "output_format background watermark stream layer_decomposition "
                               "optimize_prompt_options tools safety_identifier",
                },
            },
            {
                "id": "video",
                "object": "media.model",
                "type": "video",
                "endpoint": "POST /v1/videos/generations, GET /v1/videos/generations/{id}",
                "options": {
                    "models": list(media.VIDEO_MODELS),
                    "duration": "MiniMax-H3: 4-15s; MiniMax-Hailuo-2.3: 6 or 10s",
                    "resolution": "768P | 1080P | 2K",
                    "ratio": "21:9 16:9 4:3 1:1 3:4 9:16 adaptive",
                    "image": "first frame (public URL)",
                    "last_frame_image": "MiniMax-H3 only (public URL)",
                    "reference_images": "MiniMax-H3 only, 1-9",
                },
            },
            {
                "id": "speech",
                "object": "media.model",
                "type": "audio",
                "endpoint": "POST /v1/audio/speech, GET /v1/audio/voices",
                "options": {"format": "mp3", "emotion": "happy sad angry fearful disgusted surprised neutral"},
            },
            {
                "id": "music",
                "object": "media.model",
                "type": "audio",
                "endpoint": "POST /v1/music/generations",
                "options": {"lyrics": "optional", "format": "mp3"},
            },
        ],
    }


def _image_count(body: dict[str, Any]) -> int:
    """How many images to produce.

    Ark has no ``n``: quantity comes from ``sequential_image_generation: auto``
    plus ``sequential_image_generation_options.max_images``. Both that and the
    OpenAI-style ``n`` are honoured, and 15 is Ark's own ceiling.
    """
    raw: Any = body.get("n")
    if raw in (None, "", 0):
        mode = str(body.get("sequential_image_generation") or "").strip().lower()
        options = body.get("sequential_image_generation_options")
        if mode == "auto" and isinstance(options, dict):
            raw = options.get("max_images")
    if raw in (None, ""):
        raw = 1
    try:
        count = int(raw)
    except (TypeError, ValueError):
        raise ConversionError("n must be an integer") from None
    if count < 1:
        raise ConversionError("n must be at least 1")
    return min(count, 15)


def _pixel_count(size_text: str) -> int:
    try:
        width_text, _, height_text = str(size_text).partition("x")
        return int(width_text) * int(height_text)
    except (TypeError, ValueError):
        return 0


@app.post("/v1/images/generations")
@app.post("/images/generations")
async def images_generations(request: Request):
    """Image generation with Ark's request and response shape.

    Modelled on ``POST /api/v3/images/generations`` (Volcengine Ark, Seedream):
    ``prompt`` / ``image`` / ``size`` / ``response_format`` in, and
    ``model`` / ``created`` / ``data[]`` / ``usage`` out, where each item carries
    ``url`` or ``b64_json`` plus the real ``size`` and ``output_format``.

    Fields this channel cannot honour are accepted and ignored rather than
    rejected, because a client written against Ark sends them by default:
    ``output_format`` (the tool always returns JPEG), ``background``,
    ``watermark``, ``stream``, ``layer_decomposition`` and its options,
    ``optimize_prompt_options``, ``tools``, ``safety_identifier``. ``size`` is
    advisory — pixels snap to the nearest supported ratio and area tier — and
    the response says what was actually produced.
    """
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")
    try:
        body = await _read_json(request)
    except ConversionError as exc:
        return error_response(400, str(exc), "invalid_request_error")

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(400, "prompt is required", "invalid_request_error")

    try:
        count = _image_count(body)
    except ConversionError as exc:
        return error_response(400, str(exc), "invalid_request_error")

    response_format = str(body.get("response_format") or "url").strip().lower()
    if response_format not in {"url", "b64_json"}:
        return error_response(
            400, "response_format must be 'url' or 'b64_json'", "invalid_request_error"
        )

    reference_urls = _as_url_list(
        body.get("image") or body.get("reference_images") or body.get("input_urls")
    )

    started = time.time()
    try:
        result = await media.image_generate(
            prompt=prompt,
            n=count,
            size=body.get("size"),
            aspect_ratio=body.get("aspect_ratio"),
            resolution=body.get("resolution"),
            reference_urls=reference_urls,
        )
        images = result["images"]
        if response_format == "b64_json":
            blobs = await asyncio.gather(*(media.download(image["url"]) for image in images))
            infos = [media.image_info(blob) for blob in blobs]
        else:
            blobs = []
            infos = await asyncio.gather(
                *(media.describe_asset(image["url"]) for image in images)
            )
    except MediaError as exc:
        return media_error_response(exc)

    data: list[dict[str, Any]] = []
    pixels = 0
    for index, image in enumerate(images):
        if blobs:
            entry: dict[str, Any] = {"b64_json": base64.b64encode(blobs[index]).decode("ascii")}
        else:
            entry = {"url": image["url"]}
        entry.update(infos[index] or {})
        if image.get("revised_prompt"):
            entry["revised_prompt"] = image["revised_prompt"]
        pixels += _pixel_count(entry.get("size", ""))
        data.append(entry)

    usage: dict[str, Any] = {"generated_images": len(data)}
    if reference_urls:
        usage["input_images"] = len(reference_urls)
    if pixels:
        # Ark bills output tokens as sum(width * height) / 256, rounded down.
        usage["output_tokens"] = pixels // 256
        usage["total_tokens"] = usage["output_tokens"]

    log.info("ok kind=image n=%d %.2fs", len(data), time.time() - started)
    return JSONResponse(
        content={
            "model": body.get("model") or "minimax-image",
            "created": int(time.time()),
            "data": data,
            "usage": usage,
        }
    )


# A video task_id is only visible to the account that submitted it: querying
# from a sibling account answers 404 "does not belong to the current caller".
# Remember the owner so polling sticks to it, even though chat rotates freely.
_VIDEO_OWNERS: dict[str, str] = {}
_VIDEO_OWNERS_MAX = 512


def _remember_video_owner(task_id: str, account: str) -> None:
    if len(_VIDEO_OWNERS) >= _VIDEO_OWNERS_MAX:
        # Cheap FIFO eviction; losing an entry only costs a 404 on a stale poll.
        for stale in list(_VIDEO_OWNERS)[: _VIDEO_OWNERS_MAX // 4]:
            _VIDEO_OWNERS.pop(stale, None)
    _VIDEO_OWNERS[task_id] = account


def _video_owner(task_id: str) -> str | None:
    return _VIDEO_OWNERS.get(task_id)


@app.post("/v1/videos/generations")
@app.post("/videos/generations")
async def videos_generations(request: Request):
    """Submit an async video task. Poll ``GET /v1/videos/generations/{id}``."""
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")
    try:
        body = await _read_json(request)
    except ConversionError as exc:
        return error_response(400, str(exc), "invalid_request_error")

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(400, "prompt is required", "invalid_request_error")

    try:
        model = media.normalize_video_model(body.get("model"))
    except MediaError as exc:
        return media_error_response(exc)

    payload: dict[str, Any] = {"model": model, "prompt": prompt}

    duration = body.get("duration") or body.get("duration_seconds")
    if duration is None:
        duration = 6 if model == "MiniMax-Hailuo-2.3" else 6
    try:
        payload["duration"] = int(duration)
    except (TypeError, ValueError):
        return error_response(400, "duration must be an integer", "invalid_request_error")

    resolution = body.get("resolution")
    if resolution:
        payload["resolution"] = resolution
    ratio = body.get("ratio") or body.get("aspect_ratio")
    if ratio:
        payload["ratio"] = ratio

    first_frame = body.get("image") or body.get("image_url") or body.get("first_frame_image")
    if isinstance(first_frame, str) and first_frame:
        payload["input_image_path"] = first_frame
    last_frame = body.get("last_frame_image") or body.get("last_frame_image_url")
    if isinstance(last_frame, str) and last_frame:
        payload["last_frame_image_path"] = last_frame
    if body.get("reference_type"):
        payload["reference_type"] = body["reference_type"]

    reference_images = _as_url_list(body.get("reference_images"))
    if reference_images:
        payload["reference_image_paths"] = reference_images
    reference_videos = _as_url_list(body.get("reference_videos"))
    if reference_videos:
        payload["reference_video_paths"] = reference_videos
        durations = body.get("reference_video_durations")
        if isinstance(durations, list) and len(durations) == len(reference_videos):
            payload["reference_video_duration_seconds"] = durations
    reference_audios = _as_url_list(body.get("reference_audios"))
    if reference_audios:
        payload["reference_audio_paths"] = reference_audios

    started = time.time()
    try:
        response, served = await media.video_submit(payload)
    except MediaError as exc:
        return media_error_response(exc)

    task_id = response.get("task_id")
    if not task_id:
        return error_response(502, "upstream did not return a task_id")
    _remember_video_owner(str(task_id), served)
    log.info("ok kind=video.submit model=%s id=%s %.2fs", model, task_id, time.time() - started)
    return JSONResponse(
        status_code=202,
        content={
            "id": str(task_id),
            "object": "video.generation",
            "status": response.get("status") or "submitted",
            "model": model,
            "duration": payload["duration"],
            "resolution": payload.get("resolution"),
            "created": int(time.time()),
            "poll": f"/v1/videos/generations/{task_id}?model={model}",
        },
    )


@app.get("/v1/videos/generations/{task_id}")
@app.get("/videos/generations/{task_id}")
async def videos_generations_get(request: Request, task_id: str, model: str | None = None):
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")
    try:
        resolved = media.normalize_video_model(model)
    except MediaError as exc:
        return media_error_response(exc)

    try:
        response = await media.video_query(task_id, resolved, account=_video_owner(task_id))
    except MediaError as exc:
        body = {
            "error": {
                "message": exc.message,
                "type": "upstream_error",
                "code": exc.code,
            },
            "id": task_id,
            "object": "video.generation",
            "model": resolved,
        }
        if exc.status_field:
            body["status"] = exc.status_field
        # The channel reports 503 when the task rendered but the server could not
        # stage the file yet. That is worth another poll, not a resubmit.
        if exc.code == 503:
            body["retryable"] = True
        # A 404 here almost always means the poll landed on a different pooled
        # account than the one that submitted the task.
        if exc.code == 404 and _video_owner(task_id) is None:
            body["hint"] = (
                "this task_id is not associated with any account in this process; "
                "a video task can only be polled by the account that created it, and "
                "that mapping is lost after a gateway restart"
            )
        return JSONResponse(status_code=exc.status, content=body)

    status = response.get("status") or "unknown"
    body = {
        "id": str(response.get("task_id") or task_id),
        "object": "video.generation",
        "status": status,
        "model": resolved,
    }
    video_url = response.get("video_url")
    if video_url:
        body["video_url"] = video_url
    if response.get("duration") is not None:
        body["duration"] = response["duration"]
    if response.get("resolution"):
        body["resolution"] = response["resolution"]
    if response.get("message"):
        body["message"] = response["message"]
    return JSONResponse(content=body)


@app.post("/v1/audio/speech")
@app.post("/audio/speech")
async def audio_speech(request: Request):
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")
    try:
        body = await _read_json(request)
    except ConversionError as exc:
        return error_response(400, str(exc), "invalid_request_error")

    text = body.get("input") or body.get("text")
    if not isinstance(text, str) or not text.strip():
        return error_response(400, "input is required", "invalid_request_error")

    response_format = (body.get("response_format") or "mp3").lower()
    if response_format not in {"mp3", "mpeg"}:
        return error_response(
            400,
            f"response_format {response_format!r} is not supported: the managed channel "
            "only returns mp3",
            "invalid_request_error",
        )

    started = time.time()
    try:
        url = await media.speech(
            text=text,
            voice_id=body.get("voice") or body.get("voice_id"),
            speed=body.get("speed"),
            volume=body.get("volume"),
            pitch=body.get("pitch"),
            emotion=body.get("emotion"),
        )
        audio = await media.download(url)
    except MediaError as exc:
        return media_error_response(exc)

    log.info("ok kind=speech bytes=%d %.2fs", len(audio), time.time() - started)
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"cache-control": "no-store"},
    )


@app.get("/v1/audio/voices")
@app.get("/audio/voices")
async def audio_voices(request: Request, language: str | None = None):
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")
    try:
        response = await media.voices()
    except MediaError as exc:
        return media_error_response(exc)

    items = response.get("available_voices") or []
    if language:
        needle = language.lower()
        items = [
            voice
            for voice in items
            if isinstance(voice, dict)
            and needle in str(voice.get("voice_id", "")).lower()
        ]
    return JSONResponse(
        content={"object": "list", "data": items, "count": len(items)}
    )


@app.post("/v1/music/generations")
@app.post("/music/generations")
async def music_generations(request: Request):
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")
    try:
        body = await _read_json(request)
    except ConversionError as exc:
        return error_response(400, str(exc), "invalid_request_error")

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(400, "prompt is required", "invalid_request_error")
    if not 10 <= len(prompt) <= 300:
        return error_response(
            400,
            "prompt length must be between 10 and 300 characters",
            "invalid_request_error",
        )

    try:
        n = max(1, min(int(body.get("n") or 1), 5))
    except (TypeError, ValueError):
        return error_response(400, "n must be an integer", "invalid_request_error")

    started = time.time()
    try:
        result = await media.music(
            prompt=prompt,
            lyrics=body.get("lyrics"),
            sample_rate=body.get("sample_rate"),
            bitrate=body.get("bitrate"),
            audio_format=body.get("response_format") or body.get("format"),
            n=n,
        )
    except MediaError as exc:
        return media_error_response(exc)

    log.info("ok kind=music n=%d %.2fs", len(result["tracks"]), time.time() - started)
    return JSONResponse(
        content={
            "created": int(time.time()),
            "data": result["tracks"],
            "model": body.get("model") or "music",
        }
    )
