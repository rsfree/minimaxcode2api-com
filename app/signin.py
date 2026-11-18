"""Daily check-in ("签到") for the managed channel.

Claiming the daily reward is the only way to top up a personal account for
free — 400 credits on most days, 1000 on day 4 and day 7 of the cycle, valid
for 30 days and spendable inside MiniMax Code only.

The endpoints live on the product host rather than the account host, and take
the same OAuth access token the gateway already holds:

    GET  /minimax-cloud/api/v1/signin/status?timezone_id=<IANA>
    GET  /minimax-cloud/api/v1/signin/config?timezone_id=<IANA>
    POST /minimax-cloud/api/v1/signin/claim?timezone_id=<IANA>      body ``{}``

``timezone_id`` must be an IANA name; anything else (including an offset) is
rejected with ``1406010011 invalid timezone_id``. Per-day status codes are
1 = upcoming, 2 = claimable, 3 = claimed, 4 = disabled; the claim response
carries ``claim_result`` 1 = just claimed, 2 = already claimed today.

Claiming is a once-per-local-day resource and three callers can reach the same
account — the check-in loop (straight from the token files), the console button
(through the in-memory pool) and the CLI. So a successful claim is journalled
into the token file itself (key ``signin``) and re-checked under a per-account
lock before any request goes out; see :func:`claim_token`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from secrets import token_hex
from typing import Any
from urllib.parse import urlencode

from .account_api import AccountError, refresh

log = logging.getLogger("minimax.signin")

HOSTS = {
    "en": "https://agent.minimax.io",
    "cn": "https://agent.minimaxi.com",
}
DEFAULT_TIMEZONE = "Asia/Shanghai"
PATH_STATUS = "/minimax-cloud/api/v1/signin/status"
PATH_CLAIM = "/minimax-cloud/api/v1/signin/claim"

# day status
UPCOMING, CLAIMABLE, CLAIMED, DISABLED = 1, 2, 3, 4
# claim_result
JUST_CLAIMED, ALREADY_CLAIMED = 1, 2

IS_WINDOWS = sys.platform.startswith("win")

# Where the "already claimed today" journal lives inside a token record. It has
# to sit in the token file because that is the only store all three callers
# share: the check-in loop reads the files, the console reads the pool the files
# produced, and the CLI reads the files again.
CLAIM_STATE = "signin"


class SigninError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _host(region: str) -> str:
    return HOSTS.get(region, HOSTS["en"])


# ---------------------------------------------------------------------------
# local day / claim journal
# ---------------------------------------------------------------------------
def _tz(tz_name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - a missing tz database must not block a claim
        log.warning("timezone %s unavailable; assuming UTC+8 for the daily reset", tz_name)
        return dt_timezone(timedelta(hours=8))


def local_day(tz_name: str = DEFAULT_TIMEZONE, at: float | None = None) -> str:
    """Local calendar day (``YYYY-MM-DD``) of the check-in timezone.

    The allowance resets at local midnight, so "did we already claim" is a
    calendar-day question. A rolling 24-hour window would both re-claim an
    account that claimed at 23:50 and skip one that claimed at 00:05.
    """
    moment = datetime.fromtimestamp(time.time() if at is None else at, _tz(tz_name))
    return moment.strftime("%Y-%m-%d")


def claim_state(result: dict, timezone: str = DEFAULT_TIMEZONE) -> dict:
    """The journal entry written after a claim that actually landed."""
    return {
        "day": local_day(timezone),
        "claim_id": result.get("claim_id"),
        "points": int(result.get("points") or 0),
        "day_no": result.get("day_no"),
        "claimed_at_ms": int(time.time() * 1000),
    }


def _entry(record: dict) -> dict:
    state = record.get(CLAIM_STATE)
    return state if isinstance(state, dict) else {}


def _claimed_on(record: dict, timezone: str) -> dict | None:
    """Today's journal entry for one token record, or None."""
    state = _entry(record)
    if not state or state.get("day") != local_day(timezone):
        return None
    return state


# A credential that came from the environment has no file to journal into, so
# the process keeps that single entry itself.
_MEMORY_CLAIMS: dict[str, dict] = {}


def claim_today(
    path: Path | None, position: int, label: str, timezone: str = DEFAULT_TIMEZONE
) -> dict | None:
    """Today's journalled claim for one account (file first, memory otherwise)."""
    if path is not None:
        try:
            records, _ = _read(path)
        except (OSError, json.JSONDecodeError):
            return None
        if 0 <= position < len(records) and isinstance(records[position], dict):
            return _claimed_on(records[position], timezone)
        return None
    state = _MEMORY_CLAIMS.get(label)
    if state and state.get("day") == local_day(timezone):
        return state
    return None


def remember_claim(path: Path | None, position: int, label: str, state: dict) -> None:
    """Journal one claim without touching anything else in the file.

    The file is re-read here instead of reusing the copy loaded at the top of
    the claim: a refresh may have rotated the credentials during the two round
    trips, and writing the older pair back would cost a password login.
    """
    if path is not None:
        try:
            records, as_array = _read(path)
        except (OSError, json.JSONDecodeError):
            records, as_array = [], False
        if 0 <= position < len(records) and isinstance(records[position], dict):
            records[position][CLAIM_STATE] = state
            _write(path, records, as_array)
            return
    _MEMORY_CLAIMS[label] = state


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: Path | str) -> threading.Lock:
    """One lock per account.

    Upstream's "already claimed" answer only arrives *after* the reward is
    booked, so two callers that both saw ``claimable`` would race: one wins the
    credits, the other is told they were already taken, and the two disagree
    about whether the account was topped up. Serialising per account makes the
    second caller read the journal the first one just wrote.
    """
    name = os.path.normcase(os.path.abspath(str(key)))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(name)
        if lock is None:
            lock = _LOCKS[name] = threading.Lock()
        return lock


def _request(
    method: str,
    url: str,
    token: str,
    body: dict | None = None,
    proxy: str | None = None,
    timeout: float = 45.0,
    unwrap: bool = True,
) -> dict:
    """Call a product endpoint.

    Most of them wrap the payload in ``data``; the commerce endpoints put their
    fields at the top level next to ``base_resp``, hence ``unwrap=False``.
    """
    host = url.split("/minimax-cloud/")[0]
    args = [
        "curl", "-sS", "-X", method, "-m", str(int(timeout)),
        "-H", "accept: application/json",
        "-H", f"authorization: Bearer {token}",
        "-H", f"origin: {host}",
        "-H", f"referer: {host}/",
    ]
    if IS_WINDOWS:
        args.append("--ssl-no-revoke")
    if proxy:
        args += ["-x", proxy]
    else:
        args += ["--noproxy", "*"]
    if body is not None:
        args += ["-H", "content-type: application/json", "--data-binary",
                 json.dumps(body, separators=(",", ":"))]
    args.append(url)

    proc = subprocess.run(args, capture_output=True, timeout=timeout + 15)
    if proc.returncode != 0:
        raise SigninError(f"curl failed: {proc.stderr.decode('utf-8', 'ignore')[:200]}")
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise SigninError(f"non-JSON response: {proc.stdout[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise SigninError(f"unexpected payload: {payload!r}"[:200])
    base = payload.get("base_resp") or {}
    code = base.get("status_code")
    if isinstance(code, int) and code != 0:
        raise SigninError(base.get("status_msg") or f"status {code}", code=code)
    return payload if not unwrap else (payload.get("data") or {})


def status(token: str, region: str = "en", timezone: str = DEFAULT_TIMEZONE,
           proxy: str | None = None) -> dict:
    url = f"{_host(region)}{PATH_STATUS}?{urlencode({'timezone_id': timezone})}"
    return _request("GET", url, token, proxy=proxy)


def claim(token: str, region: str = "en", timezone: str = DEFAULT_TIMEZONE,
          proxy: str | None = None) -> dict:
    url = f"{_host(region)}{PATH_CLAIM}?{urlencode({'timezone_id': timezone})}"
    return _request("POST", url, token, body={}, proxy=proxy)


def summarize(panel: dict) -> dict:
    """Fold the 7-day panel into something readable."""
    days = panel.get("days") or []
    today = next((d for d in days if d.get("is_today")), None)
    claimed = sum(1 for d in days if d.get("status") == CLAIMED)
    pending = sum(int(d.get("points") or 0) for d in days if d.get("status") == CLAIMABLE)
    return {
        "today_day": (today or {}).get("day_no"),
        "today_status": (today or {}).get("status"),
        "today_points": int((today or {}).get("points") or 0),
        "bonus_points": int((today or {}).get("bonus_points") or 0),
        "claimable": (today or {}).get("status") == CLAIMABLE,
        "claimed_days": claimed,
        "cycle_pending_points": pending,
        "cycle_total_points": sum(int(d.get("points") or 0) + int(d.get("bonus_points") or 0) for d in days),
    }


# ---------------------------------------------------------------------------
# token files
# ---------------------------------------------------------------------------
def _read(path: Path) -> tuple[list, bool]:
    """Return ``(records, was_array)``: one file may hold an array of accounts.

    Elements are returned as they are, including anything that is not an object,
    so a position computed here means the same thing as in ``auth.TokenPool``.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, True
    if isinstance(data, dict):
        return [data], False
    return [], False


def _write(path: Path, records: list, as_array: bool) -> None:
    # Unique temp name: the gateway writes rotated tokens into the same
    # directory, and two writers must not share one rename target.
    payload = records if as_array else (records[0] if len(records) == 1 else records)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{token_hex(4)}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _fresh_token(record: dict, proxy: str | None) -> tuple[str, bool]:
    """Return ``(access_token, rotated)``; refresh when the token is spent."""
    token = (record.get("accessToken") or "").strip()
    expires = record.get("expiresAtMs")
    needs = not token or (
        isinstance(expires, (int, float)) and time.time() * 1000 + 120_000 >= expires
    )
    if needs and record.get("refreshToken"):
        record.update(refresh(record, proxy=proxy))
        return record["accessToken"], True
    return token, False


def _cached_row(label: str, state: dict) -> dict:
    """The row a repeat call gets for a claim that is already on record."""
    return {
        "label": label,
        "claimed": False,
        "already": True,
        "signed_in": True,
        "cached": True,
        "points": int(state.get("points") or 0),
        "day": state.get("day"),
        "day_no": state.get("day_no"),
        "claim_id": state.get("claim_id"),
    }


def claim_token(
    access_token: str,
    label: str | None = None,
    path: Path | None = None,
    position: int = 0,
    region: str = "en",
    timezone: str = DEFAULT_TIMEZONE,
    proxy: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Read one account's panel and claim today's reward, journalling a success.

    Idempotent by construction: a journal entry for today short-circuits the
    whole sequence, and both the check and the claim happen under the account's
    lock, so a second caller (the loop, the console button, a CLI run) cannot
    slip in between "the panel says claimable" and "book it".

    ``path``/``position`` identify the token record to journal into; a token
    that came from the environment passes ``path=None`` and is remembered in
    the process instead.
    """
    label = label or (path.stem if path is not None else "account")
    with _lock_for(path if path is not None else f"label:{label}"):
        done = claim_today(path, position, label, timezone)
        if done is not None and not dry_run:
            log.info("skip %s: already signed in today (+%s, %s)",
                     label, done.get("points"), done.get("claim_id"))
            return _cached_row(label, done)

        panel = status(access_token, region, timezone, proxy)
        info = summarize(panel)
        if dry_run or not info["claimable"]:
            return {"label": label, "claimed": False, **info}

        result = claim(access_token, region, timezone, proxy)
        outcome = result.get("claim_result")
        if outcome == JUST_CLAIMED:
            state = claim_state(result, timezone)
            remember_claim(path, position, label, state)
            log.info("signed in %s: +%s credits (day %s, expires %s)",
                     label, result.get("points"), result.get("day_no"),
                     result.get("expire_at_ms"))
            return {"label": label, "claimed": True, "signed_in": True, **state}
        if outcome == ALREADY_CLAIMED:
            # Somebody else took today's reward — the browser, another process,
            # a run whose journal entry was lost. Worth journalling so the next
            # sweep stops asking, but it proves nothing about the balance *now*
            # (the account may have spent the credits minutes after claiming),
            # so no park is lifted on the strength of it.
            remember_claim(path, position, label, {
                "day": local_day(timezone),
                "claim_id": None,
                "points": 0,
                "day_no": result.get("day_no"),
                "claimed_at_ms": int(time.time() * 1000),
                "external": True,
            })
        return {"label": label, "claimed": False, "already": outcome == ALREADY_CLAIMED, **info}


def claim_file(
    path: Path,
    region: str = "en",
    timezone: str = DEFAULT_TIMEZONE,
    proxy: str | None = None,
    dry_run: bool = False,
    allow_refresh: bool = True,
    position: int = 0,
) -> dict:
    """Check one stored account and claim today's reward when it is available.

    ``allow_refresh=False`` is for the gateway's own loop: it renews expiring
    accounts under the pool lock first, and a second writer refreshing from here
    would race it into ``invalid_grant`` (upstream revokes the session when a
    refresh token is reused).
    """
    path = Path(path).expanduser()
    records, as_array = _read(path)
    if not (0 <= position < len(records)) or not isinstance(records[position], dict):
        raise SigninError(f"no token record at {path}#{position}")
    record = records[position]
    label = record.get("email") or (path.stem if position == 0 else f"{path.stem}#{position}")

    # Check the journal before anything else: a claimed account must not cost a
    # token refresh either, and the loop would otherwise re-rotate credentials
    # for nothing on every restart.
    done = claim_today(path, position, label, timezone)
    if done is not None and not dry_run:
        log.info("skip %s: already signed in today (+%s)", label, done.get("points"))
        return _cached_row(label, done)

    if allow_refresh:
        token, rotated = _fresh_token(record, proxy)
        # The refresh token rotates on every use, so a refresh has to reach the
        # file even when the reward turns out to be already claimed.
        if rotated:
            _write(path, records, as_array)
    else:
        token = (record.get("accessToken") or "").strip()

    return claim_token(token, label, path, position, region, timezone, proxy, dry_run)


def claim_pool(root: str | Path, region: str = "en", timezone: str = DEFAULT_TIMEZONE,
               proxy: str | None = None, dry_run: bool = False,
               allow_refresh: bool = True) -> list[dict]:
    base = Path(root).expanduser()
    files = [base] if base.is_file() else sorted(base.glob("*.json"))
    if not files:
        raise SigninError(f"no token files under {base}")
    results = []
    for path in files:
        if path.name == "index.json":
            continue
        try:
            records, _ = _read(path)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("check-in failed for %s: %s", path.name, exc)
            results.append({"label": path.stem, "claimed": False, "error": str(exc)[:200]})
            continue
        # One file may hold an array of accounts; each element has its own
        # journal entry, so they are claimed (and reported) one by one.
        for position in range(len(records)):
            label = path.stem if position == 0 else f"{path.stem}#{position}"
            try:
                results.append(
                    claim_file(path, region, timezone, proxy, dry_run, allow_refresh, position)
                )
            except (SigninError, AccountError, OSError, json.JSONDecodeError) as exc:
                log.warning("check-in failed for %s: %s", label, exc)
                results.append({"label": label, "claimed": False, "error": str(exc)[:200]})
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import settings

    parser = argparse.ArgumentParser(
        prog="python -m app.signin",
        description="Claim the daily check-in reward for one account or a whole token pool.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--dir", help="token pool directory (default: TOKEN_POOL)")
    source.add_argument("--file", help="a single token json")
    parser.add_argument("--status", action="store_true", help="report only, do not claim")
    parser.add_argument("--region", default=None, help="en (default) or cn")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="IANA name, e.g. Asia/Shanghai")
    parser.add_argument("--proxy", default=None, help="egress proxy (default: UPSTREAM_PROXY)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    target = args.file or args.dir or settings.token_pool
    if not target:
        parser.error("no token source: pass --dir/--file or set TOKEN_POOL")

    results = claim_pool(
        target,
        region=args.region or settings.region,
        timezone=args.timezone,
        proxy=args.proxy if args.proxy is not None else (settings.upstream_proxy or None),
        dry_run=args.status,
    )

    gained = 0
    claimed = 0
    for row in results:
        label = (row.get("label") or "?")[:34]
        if row.get("error"):
            print(f"{label:34}  ERROR  {row['error']}")
            continue
        if row.get("claimed"):
            claimed += 1
            gained += int(row.get("points") or 0)
            print(f"{label:34}  +{row['points']:<5} day {row.get('day_no')}  claim_id={row.get('claim_id')}")
        elif row.get("cached"):
            # 幂等：今天的领取已经记在号文件里，这次连上游都没问。
            print(f"{label:34}  already signed in today (+{row.get('points', 0)})")
        elif args.status:
            state = {UPCOMING: "upcoming", CLAIMABLE: "claimable", CLAIMED: "claimed",
                     DISABLED: "disabled"}.get(row.get("today_status"), "?")
            print(f"{label:34}  day {row.get('today_day')} {state:<9} "
                  f"cycle={row.get('cycle_total_points')} pts")
        else:
            print(f"{label:34}  already claimed today")

    if args.status:
        pending = sum(int(r.get("cycle_pending_points") or 0) for r in results if not r.get("error"))
        print(f"\n{len(results)} account(s), {pending} credits still claimable this cycle")
    else:
        print(f"\n{claimed}/{len(results)} claimed, +{gained} credits")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

