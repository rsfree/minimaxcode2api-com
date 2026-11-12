"""Access-token lifecycle for the managed MiniMax Code channel.

Credential sources, in priority order:

1. ``MINIMAX_ACCESS_TOKEN`` — a single static token (simplest, expires in ~12h).
2. ``TOKEN_POOL`` — a directory of ``*.json`` files (or one JSON array) holding
   one record per account. Accounts are used round-robin; a token that the
   upstream rejects or that runs out of quota is parked for a cool-down instead
   of failing the whole gateway.
3. ``TOKEN_STORE`` — a JSON file this process owns and refreshes in place.
4. ``MINIMAX_AUTH_FILE`` — the desktop client's ``auth.json``.

A refresh token, when present, is used 120 s before expiry and once more after
a 401, so long-lived containers survive token rotation.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger("minimax.auth")

EXPIRY_SKEW_MS = 120_000
CLIENT_ID = "mcode-public"
SCOPES = "agent.default"
AUDIENCE = "agent-backend"

# How long an account is parked after a hard failure.
COOLDOWN_UNAUTHORIZED = 600.0     # 401 — token or session is dead
COOLDOWN_EXHAUSTED = 900.0        # 402 / quota codes — balance or limit hit
COOLDOWN_RATE_LIMITED = 60.0      # 429


class CredentialError(RuntimeError):
    """Raised when no usable credential can be produced."""


@dataclass
class Token:
    access_token: str
    refresh_token: str | None = None
    expires_at_ms: int | None = None
    label: str = ""
    origin: Path | None = None      # file this record came from, if any
    position: int = 0               # index inside that file (one file may hold an array)
    index: int = 0
    pooled: bool = False            # True only for records read from TOKEN_POOL
    cooldown_until: float = 0.0
    last_error: str = ""
    uses: int = 0
    # Console-facing counters. They live on the token so the pool page can show
    # per-account success/failure without a second data structure to keep in sync.
    disabled: bool = False          # operator took it out of rotation by hand
    ok_count: int = 0
    fail_count: int = 0
    last_success: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    in_flight: int = 0

    def is_expired(self, skew_ms: int = EXPIRY_SKEW_MS) -> bool:
        if not self.expires_at_ms:
            return False
        return time.time() * 1000 + skew_ms >= self.expires_at_ms

    def is_cooling(self) -> bool:
        return self.cooldown_until > time.time()

    @property
    def short(self) -> str:
        return self.access_token[:12] + "…" if self.access_token else "-"

    @property
    def slot(self) -> str:
        """Stable identity of a pooled record: its file plus position in it."""
        return f"{self.origin}#{self.position}" if self.origin is not None else ""


def _record_to_token(record: dict, index: int, origin: Path | None = None, position: int = 0) -> Token | None:
    access = (record.get("accessToken") or record.get("access_token") or "").strip()
    if not access:
        return None
    expires = record.get("expiresAtMs")
    return Token(
        access_token=access,
        refresh_token=(record.get("refreshToken") or record.get("refresh_token") or "").strip() or None,
        expires_at_ms=int(expires) if isinstance(expires, (int, float)) else None,
        label=(record.get("email") or record.get("label") or "").strip(),
        origin=origin,
        position=position,
        index=index,
    )


def _parse_auth_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("cannot read auth file %s: %s", path, exc)
        return None
    records = data.get("records") or {}
    if not isinstance(records, dict) or not records:
        return None
    return next(iter(records.values()))


def _write_record(path: Path, token: Token, region: str) -> None:
    payload = {
        "region": region,
        "email": token.label or None,
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
        log.warning("cannot persist token file %s: %s", path, exc)


def _pool_files(root: Path) -> list[Path]:
    if root.is_dir():
        return sorted(p for p in root.glob("*.json") if p.name != "index.json")
    return [root]


class TokenPool:
    """Holds every credential the gateway may use."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tokens: list[Token] = []
        self._cursor = itertools.count()
        self._loaded = False
        self._signature: tuple = ()

    # -- loading -----------------------------------------------------------
    def _load(self) -> None:
        self._tokens = []
        index = 0

        if settings.access_token:
            self._tokens.append(Token(access_token=settings.access_token, label="env", index=index))
            index += 1

        if settings.token_pool:
            root = Path(settings.token_pool).expanduser()
            if not root.exists():
                log.error("TOKEN_POOL points at %s, which does not exist", root)
            for path in _pool_files(root):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    log.warning("skip unreadable token file %s: %s", path, exc)
                    continue
                records = data if isinstance(data, list) else [data]
                for position, record in enumerate(records):
                    if not isinstance(record, dict):
                        continue
                    token = _record_to_token(record, index, path, position)
                    if token:
                        token.pooled = True
                        self._tokens.append(token)
                        index += 1

        if not self._tokens and settings.token_store:
            path = Path(settings.token_store)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                token = _record_to_token(data, index, path)
                if token:
                    self._tokens.append(token)
            except (OSError, json.JSONDecodeError):
                pass

        if not self._tokens:
            auth_file = settings.default_auth_file()
            if auth_file:
                record = _parse_auth_file(auth_file)
                if record:
                    token = _record_to_token(record, index, auth_file)
                    if token:
                        self._tokens.append(token)

        if not self._tokens and settings.refresh_token:
            self._tokens.append(
                Token(access_token="", refresh_token=settings.refresh_token, label="env-refresh", index=0)
            )

        self._loaded = True
        self._signature = self._pool_signature()
        log.info("token pool loaded: %d account(s) %s",
                 len(self._tokens), [t.label or t.short for t in self._tokens] if self._tokens else "")

    def _ensure(self) -> None:
        if not self._loaded:
            self._load()

    # -- hot reload --------------------------------------------------------
    def _pool_signature(self) -> tuple:
        """Cheap identity of the pool directory: names, mtimes, sizes."""
        if not settings.token_pool:
            return ()
        root = Path(settings.token_pool).expanduser()
        parts: list[tuple] = []
        try:
            if root.is_dir():
                for path in sorted(root.glob("*.json")):
                    if path.name == "index.json":
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    parts.append((path.name, stat.st_mtime_ns, stat.st_size))
            elif root.exists():
                stat = root.stat()
                parts.append((root.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            pass
        return tuple(parts)

    async def reload(self) -> dict:
        """Merge the pool directory into the running pool.

        New files become usable immediately; deleted files leave rotation. An
        account already in memory is kept as-is — its access/refresh tokens are
        newer than what is on disk, because this process is the one that writes
        rotated refresh tokens back out.
        """
        async with self._lock:
            if not settings.token_pool:
                return {"added": 0, "removed": 0, "pool_size": len(self._tokens)}
            root = Path(settings.token_pool).expanduser()
            fresh: list[tuple[str, Token]] = []
            for path in _pool_files(root):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    log.warning("skip unreadable token file %s: %s", path, exc)
                    continue
                records = data if isinstance(data, list) else [data]
                for position, record in enumerate(records):
                    if not isinstance(record, dict):
                        continue
                    token = _record_to_token(record, 0, path, position)
                    if token:
                        fresh.append((token.slot, token))
            if not fresh:
                log.warning("pool reload skipped: no readable account file under %s", root)
                return {"added": 0, "removed": 0, "pool_size": len(self._tokens)}

            known = {token.slot: token for token in self._tokens if token.pooled}
            fixed = [t for t in self._tokens if not t.pooled]
            before = len(self._tokens) - len(fixed)
            merged: list[Token] = list(fixed)
            added = 0
            for slot, candidate in fresh:
                live = known.get(slot)
                if live is None:
                    added += 1
                    candidate.pooled = True
                    live = candidate
                    log.info("pool: +1 account %s", live.label or live.short)
                merged.append(live)
            removed = max(0, before - (len(merged) - len(fixed)))
            for position, token in enumerate(merged):
                token.index = position
            self._tokens = merged
            self._signature = self._pool_signature()
            if added or removed:
                log.info("pool reloaded: %d account(s) (+%d/-%d)", len(self._tokens), added, removed)
            return {"added": added, "removed": removed, "pool_size": len(self._tokens)}

    async def reload_if_changed(self) -> bool:
        """Reload only when the pool directory actually changed on disk."""
        if not self._loaded or not settings.token_pool:
            return False
        signature = self._pool_signature()
        if signature == self._signature:
            return False
        await self.reload()
        return True

    # -- refresh -----------------------------------------------------------
    async def _refresh(self, token: Token) -> Token:
        if not token.refresh_token:
            raise CredentialError(f"account {token.label or token.short} has no refresh token")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": CLIENT_ID,
            "scope": SCOPES,
            "audience": AUDIENCE,
        }
        status, body_bytes = await self._post_token(form)
        if status != 200:
            raise CredentialError(
                f"refresh failed for {token.label or token.short}: "
                f"HTTP {status} {body_bytes[:200].decode('utf-8', 'ignore')}"
            )
        body = json.loads(body_bytes)
        access = (body.get("access_token") or "").strip()
        if not access:
            raise CredentialError(f"refresh response missing access_token: {body}")
        expires_in = body.get("expires_in")
        token.access_token = access
        token.refresh_token = (body.get("refresh_token") or token.refresh_token or "").strip()
        token.expires_at_ms = int(time.time() * 1000) + int(expires_in) * 1000 if expires_in else None
        token.cooldown_until = 0.0
        token.last_error = ""
        if token.origin:
            _write_record(token.origin, token, settings.region)
        log.info("refreshed account %s, expires_at_ms=%s", token.label or token.short, token.expires_at_ms)
        return token

    async def _post_token(self, form: dict[str, str]) -> tuple[int, bytes]:
        """Token endpoint via curl first, httpx as fallback.

        The account host sits behind the same egress as the model host, so on
        networks that only accept a native TLS client the httpx path can be
        reset mid-handshake.
        """
        from . import curl_transport

        if settings.upstream_transport != "httpx":
            try:
                return await curl_transport.post_form(
                    settings.token_endpoint,
                    form,
                    proxy=settings.upstream_proxy or None,
                    headers={"accept": "application/json"},
                )
            except Exception as exc:  # noqa: BLE001 - curl missing or blocked
                log.warning("curl token request failed (%s); falling back to httpx", exc)
        async with httpx.AsyncClient(proxy=settings.upstream_proxy or None, timeout=60.0) as client:
            response = await client.post(
                settings.token_endpoint, data=form, headers={"accept": "application/json"}
            )
        return response.status_code, response.content

    async def _usable(self, token: Token) -> Token | None:
        if token.disabled:
            return None
        if token.is_cooling():
            return None
        if token.is_expired() and token.refresh_token:
            try:
                return await self._refresh(token)
            except CredentialError as exc:
                log.warning("%s", exc)
                token.cooldown_until = time.time() + COOLDOWN_UNAUTHORIZED
                token.last_error = str(exc)[:200]
                return None
        if not token.access_token:
            return None
        return token

    # -- public API --------------------------------------------------------
    async def acquire(self, exclude: set[str] | None = None) -> Token:
        """Return the next usable account, round-robin."""
        async with self._lock:
            self._ensure()
            if not self._tokens:
                raise CredentialError(
                    "no credential available: set MINIMAX_ACCESS_TOKEN, or TOKEN_POOL to a "
                    "directory of token json files, or MINIMAX_REFRESH_TOKEN"
                )
            skip = exclude or set()
            count = len(self._tokens)
            start = next(self._cursor) % count
            for offset in range(count):
                token = self._tokens[(start + offset) % count]
                key = token.access_token or token.label
                if key in skip:
                    continue
                usable = await self._usable(token)
                if usable is not None:
                    token.uses += 1
                    return token
            raise CredentialError("every account in the pool is cooling down or unusable")

    async def get(self, force: bool = False) -> str:
        """Back-compat helper: the next usable access token as a string."""
        token = await self.acquire()
        if force and token.refresh_token:
            async with self._lock:
                token = await self._refresh(token)
        return token.access_token

    async def invalidate(self) -> None:
        async with self._lock:
            self._loaded = False
            self._tokens = []

    async def renew(self, token: Token) -> bool:
        """Force a refresh of one account. Returns False when it has no refresh token."""
        if not token.refresh_token:
            return False
        async with self._lock:
            try:
                await self._refresh(token)
                return True
            except CredentialError as exc:
                log.warning("%s", exc)
                token.last_error = str(exc)[:200]
                return False

    def park(self, token: Token, status: int | None = None, reason: str = "") -> None:
        """Temporarily remove an account from rotation."""
        if status == 401:
            delay = COOLDOWN_UNAUTHORIZED
        elif status in {402, 429}:
            delay = COOLDOWN_EXHAUSTED if status == 402 else COOLDOWN_RATE_LIMITED
        else:
            delay = COOLDOWN_RATE_LIMITED
        token.cooldown_until = time.time() + delay
        token.last_error = reason[:200] or f"HTTP {status}"
        log.warning("parked %s for %.0fs: %s", token.label or token.short, delay, token.last_error)

    # -- console-facing bookkeeping ----------------------------------------
    def note_success(self, token: Token, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        token.ok_count += 1
        token.last_success = time.time()
        token.tokens_in += max(0, int(prompt_tokens or 0))
        token.tokens_out += max(0, int(completion_tokens or 0))
        token.last_error = ""

    def note_failure(self, token: Token, reason: str = "") -> None:
        token.fail_count += 1
        if reason:
            token.last_error = reason[:200]

    def set_disabled(self, key: str, disabled: bool) -> Token | None:
        """Take one account out of rotation (or put it back) by label or token."""
        token = self.find(key)
        if token is None:
            return None
        token.disabled = disabled
        if not disabled:
            token.cooldown_until = 0.0
            token.last_error = ""
        log.info("%s account %s", "disabled" if disabled else "enabled", token.label or token.short)
        return token

    def peek(self) -> Token | None:
        """Return a credential without ever going to the network."""
        self._ensure()
        for token in self._tokens:
            if token.access_token and not token.is_cooling():
                return token
        return self._tokens[0] if self._tokens else None

    def find(self, key: str | None) -> Token | None:
        """Look one account up by label or token, ignoring cool-down.

        Async media tasks belong to the account that created them, so a caller
        that remembers the owner has to reach exactly that account again.
        """
        if not key:
            return None
        self._ensure()
        for token in self._tokens:
            if key in {token.label, token.access_token, token.short}:
                return token
        return None

    def describe(self) -> dict:
        self._ensure()
        now = time.time()
        total = len(self._tokens)
        usable = sum(1 for t in self._tokens if t.access_token and not t.disabled and not t.is_cooling())
        return {
            "pool_size": total,
            "usable": usable,
            "disabled": sum(1 for t in self._tokens if t.disabled),
            "cooling": sum(1 for t in self._tokens if (not t.disabled) and t.is_cooling()),
            "in_flight": sum(t.in_flight for t in self._tokens),
            "pool_watch_seconds": settings.pool_watch if settings.token_pool else 0,
            "accounts": [
                {
                    "label": t.label or t.short,
                    "usable": bool(t.access_token) and not t.disabled and not t.is_cooling(),
                    "disabled": t.disabled,
                    "expires_at_ms": t.expires_at_ms,
                    "cooling_for": round(max(0.0, t.cooldown_until - now)),
                    "uses": t.uses,
                    "ok": t.ok_count,
                    "fail": t.fail_count,
                    "in_flight": t.in_flight,
                    "tokens_in": t.tokens_in,
                    "tokens_out": t.tokens_out,
                    "last_success": t.last_success,
                    "last_error": t.last_error or None,
                }
                for t in self._tokens
            ],
        }


token_manager = TokenPool()
