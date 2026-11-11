"""FastAPI front door.

Surface:

* ``POST /v1/chat/completions`` — OpenAI-compatible, streaming and buffered.
* ``POST /v1/messages``         — Anthropic Messages passthrough.
* ``GET  /v1/models``           — catalogue for client pickers.
* ``GET  /healthz``             — liveness plus credential status.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .auth import CredentialError, token_manager
from .catalog import DEFAULT_MODEL, resolve_model, visible_models
from .config import settings
from .convert import (
    ConversionError,
    build_anthropic_request,
    build_openai_response,
    iter_sse_events,
    stream_openai,
)
from .upstream import UpstreamError, upstream

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("minimax.gateway")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await upstream.startup()
    try:
        await token_manager.get()
    except CredentialError as exc:
        log.error("no usable credential at startup: %s", exc)
    yield
    await upstream.shutdown()


app = FastAPI(
    title="minimax-code2api",
    version="1.0.0",
    description="OpenAI/Anthropic compatible gateway for the MiniMax Code managed channel.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def error_response(status: int, message: str, code: str = "upstream_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code, "code": code}},
    )


def _authorized(request: Request) -> bool:
    if not settings.api_keys:
        return True
    token = ""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = (request.headers.get("x-api-key") or "").strip()
    return bool(token) and token in settings.api_keys


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
            "GET /healthz",
        ],
        "default_model": DEFAULT_MODEL,
        "models": [model.id for model in visible_models()],
    }


@app.get("/healthz")
async def healthz() -> JSONResponse:
    body: dict[str, Any] = {"status": "ok", "region": settings.region}
    body.update(token_manager.describe())
    body["base_url"] = settings.base_url
    body["proxy"] = bool(settings.upstream_proxy)
    try:
        await token_manager.get()
    except CredentialError as exc:
        body["status"] = "degraded"
        body["error"] = str(exc)
        return JSONResponse(status_code=503, content=body)
    return JSONResponse(status_code=200, content=body)


@app.get("/v1/models")
@app.get("/models")
async def list_models() -> dict:
    return {"object": "list", "data": [model.to_openai() for model in visible_models()]}


@app.get("/v1/models/meta")
async def list_models_meta() -> dict:
    return {"object": "list", "data": [model.to_meta() for model in visible_models()]}


# ---------------------------------------------------------------------------
# OpenAI compatible
# ---------------------------------------------------------------------------
@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    if not _authorized(request):
        return error_response(401, "invalid gateway api key", "authentication_error")
    if request.headers.get("x-api-key") and not settings.api_keys:
        pass

    try:
        body = await _read_json(request)
        anthropic_payload = build_anthropic_request(body)
    except ConversionError as exc:
        return error_response(400, str(exc), "invalid_request_error")

    model = anthropic_payload["model"]
    started = time.time()

    if body.get("stream"):
        try:
            response = await upstream.messages_stream(anthropic_payload)
        except UpstreamError as exc:
            return error_response(exc.status, exc.message)

        async def relay():
            try:
                events = iter_sse_events(response.aiter_lines())
                async for chunk in stream_openai(events, model):
                    yield chunk
            except Exception as exc:  # noqa: BLE001 - surface to the client
                log.warning("stream aborted: %s", exc)
                payload = {"error": {"message": str(exc), "type": "upstream_error"}}
                yield f"data: {json.dumps(payload)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                await response.aclose()
                _log_usage("chat.stream", model, {}, time.time() - started)

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
        payload = await upstream.messages(anthropic_payload)
    except UpstreamError as exc:
        return error_response(exc.status, exc.message)

    _log_usage("chat", model, payload, time.time() - started)
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
