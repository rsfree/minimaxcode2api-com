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
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
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
COOLDOWN_EXHAUSTED = 900.0        # 402 when the charge is one-off (media, long jobs)
COOLDOWN_RATE_LIMITED = 60.0      # 429

# A chat 402 means the account's daily allowance is spent, and that allowance
# only returns with a check-in. Parking "until tomorrow's 00:10" sounds right but
# has two failure modes: an account emptied at 00:06 used to be parked for 24 h
# (the reset had already passed), and when the whole pool ran dry the gateway
# stayed dark until the next 6-hourly check-in. Back off geometrically instead —
# early retries are cheap and catch a check-in that just landed, while the cap
# keeps a genuinely empty pool from hammering upstream.
QUOTA_BACKOFF_BASE = 900.0        # 15 min after the first 402
QUOTA_BACKOFF_MAX = 6 * 3600.0    # one check-in cycle

# Reason tags live on the token so a successful sign-in can lift exactly the
# quota parks (and never an auth one).
REASON_AUTH = "auth"
REASON_QUOTA = "quota"
REASON_RATE = "rate"

# Two capabilities, two budgets. Chat is metered in small slices of the same
# credit balance; image/video work is billed per call (one 1K image is ~115
# credits, one video ~320). An account holding 40 credits therefore answers chat
# perfectly well and cannot draw a single picture — which is why a 402 on the
# media path must not take the account out of rotation for chat, and a 402 on
# chat (balance genuinely gone) does say something about media.
CAPABILITY_TEXT = "text"
CAPABILITY_MEDIA = "media"

# How stale a probed balance may be before the pool stops trusting it. The probe
# sweep is minutes apart; anything older than this is treated as unknown, which
# errs on the side of *sending the request* rather than starving the pool on old
# data.
CREDIT_HINT_TTL = 1800.0


def seconds_until_next_day(tz_name: str, buffer_minutes: int = 10) -> float:
    """Seconds until the *next* local midnight, plus a settle buffer.

    The word "next" is load-bearing. The previous version did
    ``(now + 1 day).replace(hour=0)``, which is always tomorrow: an account that
    ran dry at 00:06 was parked until 00:10 the *following* day, ~24 h instead of
    the four minutes actually left. Observed in production on 2026-11-14 — the
    last account emptied at 00:06 and the whole gateway answered
    "every account is out of quota until the daily reset at 00:10" until the
    01:46 check-in.

    So: today's midnight + buffer if it is still ahead, otherwise tomorrow's.
    """
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - missing tz database must not break rotation
        log.warning("timezone %s unavailable; assuming UTC+8 for the daily reset", tz_name)
        tz = dt_timezone(timedelta(hours=8))
    now = datetime.now(tz)
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=buffer_minutes
    )
    if reset <= now:
        reset += timedelta(days=1)
    return max(60.0, (reset - now).total_seconds())


def short_delay(seconds: float) -> str:
    """Human-sized duration for caller-facing messages: 45s / 14m / 3.2h."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


class CredentialError(RuntimeError):
    """Raised when no usable credential can be produced."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        # 402 when the pool is healthy but too poor for the request at hand; the
        # media path turns that into a caller-facing 402 instead of a 502.
        self.status = status


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
    # Text-channel park. Auth and rate-limit failures are pool-wide and are
    # written to both this and the media pair below.
    cooldown_until: float = 0.0
    cooldown_reason: str = ""       # auth / quota / rate — see REASON_*
    # Consecutive chat 402s. Drives the geometric quota backoff: the first one
    # parks 15 min, each repeat doubles it up to a check-in cycle. A successful
    # check-in resets it, because a top-up makes the next 402 a fresh story.
    quota_streak: int = 0
    # Media-channel park, kept separate because the two budgets are separate:
    # "cannot afford one image" is not "cannot answer a prompt".
    media_cooldown_until: float = 0.0
    media_cooldown_reason: str = ""
    # Balance as last read by the probe sweep, plus when it was read. Used to
    # skip accounts that provably cannot pay for the job instead of spending a
    # 300 s round trip to be told so.
    credit: float | None = None
    credit_at: float = 0.0
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

    def is_cooling(self, capability: str = CAPABILITY_TEXT) -> bool:
        until = self.media_cooldown_until if capability == CAPABILITY_MEDIA else self.cooldown_until
        return until > time.time()

    def cooldown(self, capability: str = CAPABILITY_TEXT) -> tuple[float, str]:
        """``(until, reason)`` for one capability."""
        if capability == CAPABILITY_MEDIA:
            return self.media_cooldown_until, self.media_cooldown_reason
        return self.cooldown_until, self.cooldown_reason

    def credit_hint(self) -> float | None:
        """The probed balance, or None when it was never read or is stale."""
        if self.credit is None:
            return None
        if time.time() - self.credit_at > CREDIT_HINT_TTL:
            return None
        return self.credit

    def can_afford(self, need: float) -> bool:
        """Whether a media job costing ``need`` credits may be sent here.

        Unknown balances answer True on purpose: refusing work because the probe
        has not run yet would starve a pool that is in fact solvent, and one
        failed call is cheaper than a pool-wide outage.
        """
        if need <= 0:
            return True
        hint = self.credit_hint()
        return hint is None or hint >= need

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
        # Unique temp name per write: the check-in thread and the console
        # importer write the same directory, and two writers sharing one .tmp
        # path can interleave their bytes into a file that is then renamed into
        # place — a corrupt record that costs a login to recover.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
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
        # Snapshot first. If a file lands while we are reading, the stored
        # signature stays stale and the next watcher tick reloads again — the
        # other order silently forgets a file that appeared mid-read.
        signature = self._pool_signature()

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
        self._signature = signature
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

        New files become usable immediately; deleted files leave rotation. For
        an account already in memory the *live* object is kept — it owns the
        counters, cool-downs and disabled flag — but its credentials are taken
        from disk when the file is newer.

        That last part matters because the refresh token is single-use: whoever
        refreshes second must present the value the first writer stored, or the
        exchange fails with ``invalid_grant``. Two writers exist (this process
        and the check-in thread, which works straight from files), so the file
        is the shared truth and the in-memory copy is only a cache of it.
        """
        async with self._lock:
            if not settings.token_pool:
                return {"added": 0, "removed": 0, "pool_size": len(self._tokens)}
            root = Path(settings.token_pool).expanduser()
            # Same ordering rule as _load: the signature describes the state we
            # are about to read, not the state after it. A file written during
            # the read then leaves the signature stale, so the next scan retries
            # instead of believing it already loaded everything.
            signature = self._pool_signature()
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
                elif self._adopt(live, candidate):
                    log.info("pool: %s adopted rotated credentials from disk",
                             live.label or live.short)
                merged.append(live)
            removed = max(0, before - (len(merged) - len(fixed)))
            for position, token in enumerate(merged):
                token.index = position
            self._tokens = merged
            self._signature = signature
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
    @staticmethod
    def _adopt(token: Token, candidate: Token, force: bool = False) -> bool:
        """Copy a newer credential from a freshly-read record onto a live token.

        Returns True when something actually changed. Counters, cool-downs and
        the disabled flag stay on the live object; only the credential moves.
        An auth park is lifted here because the rejection applied to the old
        token, not the new one — a quota park is not, since a fresh access token
        says nothing about the daily allowance.

        ``force`` is for the resync after a failed exchange: at that point the
        file is known to be the newer of the two, whatever its expiry says.
        Without it, only a record that outlives the in-memory one wins; equal or
        earlier expiry means the file is behind (a refresh that has not been
        written back yet) and adopting it would pair a new access token with a
        spent refresh token.
        """
        if not candidate.access_token or candidate.access_token == token.access_token:
            return False
        if not force:
            current = token.expires_at_ms
            if candidate.expires_at_ms is None or (
                current is not None and candidate.expires_at_ms <= current
            ):
                return False
        token.access_token = candidate.access_token
        if candidate.refresh_token:
            token.refresh_token = candidate.refresh_token
        token.expires_at_ms = candidate.expires_at_ms
        if token.cooldown_reason == REASON_AUTH:
            token.cooldown_until = 0.0
            token.cooldown_reason = ""
            token.last_error = ""
        return True

    def _resync(self, token: Token) -> bool:
        """Re-read one account's file and adopt a rotated credential, if any."""
        if token.origin is None:
            return False
        try:
            data = json.loads(token.origin.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        records = data if isinstance(data, list) else [data]
        if token.position >= len(records) or not isinstance(records[token.position], dict):
            return False
        candidate = _record_to_token(records[token.position], token.index)
        return candidate is not None and self._adopt(token, candidate, force=True)

    async def _refresh_once(self, token: Token) -> Token:
        """Make sure the live token carries a usable credential.

        The check-in loop refreshes in a worker thread and writes the new pair
        straight to the file, so the in-memory refresh token can already be
        spent by the time a request needs it. Upstream treats a *reused* refresh
        token as a leak and revokes the whole session, not just that exchange —
        observed on the live host: the second exchange with an old value answers
        ``invalid_grant`` and the access token minted moments earlier stops
        working too. So the file is checked *first*; a spent value must never be
        presented just to find out.
        """
        if self._resync(token) and not token.is_expired():
            log.info("account %s: using the credential written to disk",
                     token.label or token.short)
            return token
        try:
            return await self._refresh(token)
        except CredentialError:
            # Nothing newer on disk and the exchange was rejected: the refresh
            # token itself is dead, so parking is correct. Re-check anyway —
            # a writer may have landed a pair during the round trip.
            if self._resync(token) and token.access_token:
                return token
            raise

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

    async def _usable(
        self, token: Token, capability: str = CAPABILITY_TEXT, need: float = 0.0
    ) -> Token | None:
        if token.disabled:
            return None
        if token.is_cooling(capability):
            return None
        if capability == CAPABILITY_MEDIA and not token.can_afford(need):
            return None
        if token.is_expired() and token.refresh_token:
            try:
                return await self._refresh_once(token)
            except CredentialError as exc:
                log.warning("%s", exc)
                token.cooldown_until = time.time() + COOLDOWN_UNAUTHORIZED
                token.cooldown_reason = REASON_AUTH
                token.last_error = str(exc)[:200]
                return None
        if not token.access_token:
            return None
        return token

    # -- public API --------------------------------------------------------
    async def acquire(
        self,
        exclude: set[str] | None = None,
        capability: str = CAPABILITY_TEXT,
        need: float = 0.0,
    ) -> Token:
        """Return the next usable account, round-robin.

        ``capability`` picks which park to respect, and for ``media`` ``need``
        filters out accounts whose last probed balance cannot pay for the job.
        That filter is a skip, not a park: the account stays in rotation for
        chat, and the next probe may lift it back into the media pool.
        """
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
                usable = await self._usable(token, capability, need)
                if usable is not None:
                    token.uses += 1
                    return token
            warm = [
                t for t in self._tokens
                if not t.disabled and not t.is_cooling(capability) and t.access_token
            ]
            if capability == CAPABILITY_MEDIA and warm:
                # Every warm account was skipped by the balance filter. Say so
                # precisely: "all cooling" would send an operator looking for a
                # quota reset when what is needed is a top-up.
                best = max((t.credit_hint() or 0.0) for t in warm)
                raise CredentialError(
                    f"no account can afford this media job (need {need:.0f} credits, "
                    f"best probed balance {best:.0f})",
                    status=402,
                )
            cooling = [t for t in self._tokens if not t.disabled and t.is_cooling(capability)]
            if cooling and all(t.cooldown(capability)[1] == REASON_QUOTA for t in cooling):
                # Report when the pool actually comes back, not a fixed "00:10":
                # with the geometric backoff that is minutes, not a day, and the
                # old wording sent everyone waiting for a reset that had passed.
                soonest = min(t.cooldown(capability)[0] for t in cooling)
                raise CredentialError(
                    "every account is out of credits right now; earliest retry in "
                    f"{short_delay(soonest - time.time())} "
                    f"({settings.signin_timezone})"
                )
            raise CredentialError("every account in the pool is cooling down or unusable")

    async def get(self, force: bool = False) -> str:
        """Back-compat helper: the next usable access token as a string."""
        token = await self.acquire()
        if force and token.refresh_token:
            async with self._lock:
                token = await self._refresh_once(token)
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
                await self._refresh_once(token)
                return True
            except CredentialError as exc:
                log.warning("%s", exc)
                token.last_error = str(exc)[:200]
                return False

    def park(
        self,
        token: Token,
        status: int | None = None,
        reason: str = "",
        capability: str = CAPABILITY_TEXT,
    ) -> None:
        """Temporarily remove an account from rotation.

        A chat 402 means the allowance is spent, so the delay is a capped
        geometric backoff (:data:`QUOTA_BACKOFF_BASE` doubling up to
        :data:`QUOTA_BACKOFF_MAX`, never past the next reset): retrying early is
        cheap and catches a check-in that just landed, while the cap keeps a
        genuinely empty pool from hammering upstream. Parking "until the daily
        reset" read well but put an account offline for 24 h when it emptied
        minutes *after* midnight.

        One-off charges (media, where a single video costs 320 credits) keep the
        flat short cooldown — being too poor for one expensive job says nothing
        about the text quota.

        ``capability`` scopes a 402 to one budget. A media 402 only proves the
        account cannot pay for an image; it can still answer chat, so parking the
        text channel for 15 minutes because of it was silently costing the pool
        usable capacity. 401 and 429 are different: a dead or throttled
        credential is dead for both, so those two write to both channels.
        """
        if status == 401:
            delay, tag = COOLDOWN_UNAUTHORIZED, REASON_AUTH
        elif status == 402:
            tag = REASON_QUOTA
            if capability == CAPABILITY_TEXT:
                token.quota_streak = min(token.quota_streak + 1, 10)
                delay = min(
                    QUOTA_BACKOFF_BASE * (2 ** (token.quota_streak - 1)),
                    QUOTA_BACKOFF_MAX,
                    seconds_until_next_day(settings.signin_timezone),
                )
            else:
                delay = COOLDOWN_EXHAUSTED
        else:
            delay, tag = COOLDOWN_RATE_LIMITED, REASON_RATE

        until = time.time() + delay
        message = reason[:200] or f"HTTP {status}"
        scopes = [CAPABILITY_TEXT, CAPABILITY_MEDIA] if status in {401, 429} else [capability]
        # Chat's 402 says the balance itself is gone, so images cannot be paid for
        # either; parking media too saves discovering that 300 s later.
        if status == 402 and capability == CAPABILITY_TEXT:
            scopes.append(CAPABILITY_MEDIA)
        for scope in scopes:
            if scope == CAPABILITY_MEDIA:
                token.media_cooldown_until = until
                token.media_cooldown_reason = tag
            else:
                token.cooldown_until = until
                token.cooldown_reason = tag
        token.last_error = message
        log.warning("parked %s for %.0fs (%s/%s): %s",
                    token.label or token.short, delay, capability, tag, token.last_error)

    def clear_quota_cooldown(self, key: str | None) -> bool:
        """Put an account back in rotation after a top-up (sign-in).

        Only quota parks are lifted: a dead token or a rate limit has nothing to
        do with a claim, and clearing it would just produce another 401. Both
        channels are cleared — a top-up refills the one balance they share.
        """
        token = self.find(key)
        if token is None:
            return False
        # A claim is proof the allowance is back, so the next 402 (if any) starts
        # a fresh backoff instead of continuing the previous day's streak.
        token.quota_streak = 0
        cleared = False
        if token.cooldown_reason == REASON_QUOTA:
            token.cooldown_until = 0.0
            token.cooldown_reason = ""
            cleared = True
        if token.media_cooldown_reason == REASON_QUOTA:
            token.media_cooldown_until = 0.0
            token.media_cooldown_reason = ""
            cleared = True
        if cleared:
            token.last_error = ""
            log.info("unparked %s after top-up", token.label or token.short)
        return cleared

    def set_credit_hint(self, key: str | None, credit: float | None) -> None:
        """Record the probed balance so the media path can pre-filter on it."""
        token = self.find(key)
        if token is None or credit is None:
            return
        try:
            value = float(credit)
        except (TypeError, ValueError):
            return
        token.credit = value
        token.credit_at = time.time()

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
            token.cooldown_reason = ""
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
            "cooling_quota": sum(
                1 for t in self._tokens
                if (not t.disabled) and t.is_cooling() and t.cooldown_reason == REASON_QUOTA
            ),
            # Media is counted separately: an account parked for chat can still
            # draw, and vice versa. A single number here is what made the panel
            # claim an account was unusable when only one budget was empty.
            # The balance is part of the test: an account holding 0.8 credits is
            # not "available for images", it just has not been told so yet.
            "usable_media": sum(
                1 for t in self._tokens
                if t.access_token and not t.disabled and not t.is_cooling(CAPABILITY_MEDIA)
                and t.can_afford(settings.media_min_credits)
            ),
            "cooling_media": sum(
                1 for t in self._tokens if (not t.disabled) and t.is_cooling(CAPABILITY_MEDIA)
            ),
            "in_flight": sum(t.in_flight for t in self._tokens),
            "pool_watch_seconds": settings.pool_watch if settings.token_pool else 0,
            "accounts": [
                {
                    "label": t.label or t.short,
                    "usable": bool(t.access_token) and not t.disabled and not t.is_cooling(),
                    "usable_media": bool(t.access_token) and not t.disabled
                    and not t.is_cooling(CAPABILITY_MEDIA)
                    and t.can_afford(settings.media_min_credits),
                    "disabled": t.disabled,
                    "expires_at_ms": t.expires_at_ms,
                    "cooling_for": round(max(0.0, t.cooldown_until - now)),
                    "cooldown_until": t.cooldown_until or None,
                    "cooldown_reason": t.cooldown_reason or None,
                    "media_cooling_for": round(max(0.0, t.media_cooldown_until - now)),
                    "media_cooldown_until": t.media_cooldown_until or None,
                    "media_cooldown_reason": t.media_cooldown_reason or None,
                    "credit": t.credit,
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
