"""Access-token lifecycle for the managed MiniMax Code channel.

Three credential sources, in priority order:

1. ``MINIMAX_ACCESS_TOKEN`` — a static token (simplest, expires in ~12h).
2. ``TOKEN_STORE`` — a JSON file this process owns and refreshes in place.
3. ``MINIMAX_AUTH_FILE`` — the desktop client's ``auth.json``. Re-read on
   every use so a locally running client keeps the gateway authorised.

A refresh token, when present, is used 120 s before expiry and once more after
a 401, so a long-lived container survives token rotation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger("minimax.auth")

EXPIRY_SKEW_MS = 120_000
CLIENT_ID = "mcode-public"
SCOPES = "agent.default"
AUDIENCE = "agent-backend"


class CredentialError(RuntimeError):
    """Raised when no usable credential can be produced."""


@dataclass
class Token:
    access_token: str
    refresh_token: str | None = None
    expires_at_ms: int | None = None

    def is_expired(self, skew_ms: int = EXPIRY_SKEW_MS) -> bool:
        if not self.expires_at_ms:
            return False
        return time.time() * 1000 + skew_ms >= self.expires_at_ms


def _parse_auth_file(path: Path) -> Token | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("cannot read auth file %s: %s", path, exc)
        return None
    records = data.get("records") or {}
    if not isinstance(records, dict) or not records:
        return None
    record = next(iter(records.values()))
    access = (record.get("accessToken") or "").strip()
    if not access:
        return None
    expires = record.get("expiresAtMs")
    return Token(
        access_token=access,
        refresh_token=(record.get("refreshToken") or "").strip() or None,
        expires_at_ms=int(expires) if isinstance(expires, (int, float)) else None,
    )


def _parse_store(path: Path) -> Token | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    access = (data.get("accessToken") or "").strip()
    if not access:
        return None
    expires = data.get("expiresAtMs")
    return Token(
        access_token=access,
        refresh_token=(data.get("refreshToken") or "").strip() or None,
        expires_at_ms=int(expires) if isinstance(expires, (int, float)) else None,
    )


def _write_store(path: Path, token: Token) -> None:
    payload = {
        "region": settings.region,
        "accessToken": token.access_token,
        "refreshToken": token.refresh_token,
        "expiresAtMs": token.expires_at_ms,
        "obtainedAtMs": int(time.time() * 1000),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        path.chmod(0o600)
    except OSError as exc:
        log.warning("cannot persist token store %s: %s", path, exc)


class TokenManager:
    def __init__(self) -> None:
        self._token: Token | None = None
        self._lock = asyncio.Lock()
        self.store_path = Path(settings.token_store) if settings.token_store else None

    # -- source resolution -------------------------------------------------
    def _from_sources(self, prefer_refresh: bool = False) -> Token | None:
        if settings.access_token:
            return Token(access_token=settings.access_token, refresh_token=settings.refresh_token or None)

        if self.store_path:
            token = _parse_store(self.store_path)
            if token and not prefer_refresh:
                return token

        auth_file = settings.default_auth_file()
        if auth_file:
            token = _parse_auth_file(auth_file)
            if token:
                return token

        if self.store_path:
            return _parse_store(self.store_path)
        return None

    async def _refresh(self, refresh_token: str) -> Token:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": SCOPES,
            "audience": AUDIENCE,
        }
        async with httpx.AsyncClient(proxy=settings.upstream_proxy or None, timeout=60.0) as client:
            response = await client.post(
                settings.token_endpoint,
                data=form,
                headers={"accept": "application/json"},
            )
        if response.status_code != 200:
            raise CredentialError(
                f"refresh failed: HTTP {response.status_code} {response.text[:300]}"
            )
        body = response.json()
        access = (body.get("access_token") or "").strip()
        if not access:
            raise CredentialError(f"refresh response missing access_token: {body}")
        expires_in = body.get("expires_in")
        expires_at = int(time.time() * 1000) + int(expires_in) * 1000 if expires_in else None
        token = Token(
            access_token=access,
            refresh_token=(body.get("refresh_token") or refresh_token).strip(),
            expires_at_ms=expires_at,
        )
        if self.store_path:
            _write_store(self.store_path, token)
        log.info("access token refreshed, expires_at_ms=%s", expires_at)
        return token

    # -- public API --------------------------------------------------------
    async def get(self, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and not self._token.is_expired():
                return self._token.access_token

            token = self._from_sources(prefer_refresh=force)

            if token is None:
                if settings.refresh_token:
                    token = await self._refresh(settings.refresh_token)
                else:
                    raise CredentialError(
                        "no credential available: set MINIMAX_ACCESS_TOKEN, or provide "
                        "MINIMAX_REFRESH_TOKEN, or mount the desktop client's auth.json"
                    )
            elif token.is_expired() and token.refresh_token:
                try:
                    token = await self._refresh(token.refresh_token)
                except CredentialError:
                    if settings.refresh_token and settings.refresh_token != token.refresh_token:
                        token = await self._refresh(settings.refresh_token)
                    else:
                        raise

            self._token = token
            return token.access_token

    async def invalidate(self) -> None:
        async with self._lock:
            self._token = None

    def peek(self) -> Token | None:
        """Return a credential without ever going to the network.

        Used by ``/healthz`` so a probe can never block on a token refresh.
        """
        if self._token is not None:
            return self._token
        return self._from_sources()

    def describe(self) -> dict:
        token = self._token
        return {
            "region": settings.region,
            "source": (
                "env"
                if settings.access_token
                else ("refresh_token" if settings.refresh_token else "auth_file")
            ),
            "cached": bool(token),
            "expires_at_ms": token.expires_at_ms if token else None,
        }


token_manager = TokenManager()
