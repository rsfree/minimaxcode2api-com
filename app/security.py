"""Shared request authentication.

Both the gateway's own routes and the console's `/admin/*` surface need the same
answer to "did this caller present the key", so the rule lives in one place.
"""

from __future__ import annotations

from fastapi import Request

from .config import settings

# Cookie set by the console login form so the page itself sits behind the same
# key as the API. Readable by JS on purpose: the console needs the key for the
# Authorization header, and localStorage may be unavailable.
UI_COOKIE = "mmx_ui"
UI_COOKIE_MAX_AGE = 30 * 24 * 3600


def supplied_key(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    for name in ("x-api-key", "api-key"):
        value = (request.headers.get(name) or "").strip()
        if value:
            return value
    cookie = (request.cookies.get(UI_COOKIE) or "").strip()
    if cookie:
        return cookie
    return (request.query_params.get("key") or "").strip()


def authorized(request: Request) -> bool:
    if not settings.api_keys:
        return True
    return supplied_key(request) in settings.api_keys


def is_loopback(request: Request) -> bool:
    """True only for a direct, un-proxied call from the container itself.

    A reverse proxy in front of the gateway also arrives from 127.0.0.1, so any
    forwarding header means the caller is really remote and must send the key.
    """
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return False
    host = (request.client.host if request.client else "") or ""
    return host in {"127.0.0.1", "::1", "localhost"}
