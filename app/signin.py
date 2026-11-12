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
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
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


class SigninError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _host(region: str) -> str:
    return HOSTS.get(region, HOSTS["en"])


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
def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, record: dict) -> None:
    # Unique temp name: the gateway writes rotated tokens into the same
    # directory, and two writers must not share one rename target.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{token_hex(4)}.tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
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


def claim_file(
    path: Path,
    region: str = "en",
    timezone: str = DEFAULT_TIMEZONE,
    proxy: str | None = None,
    dry_run: bool = False,
    allow_refresh: bool = True,
) -> dict:
    """Check one stored account and claim today's reward when it is available.

    ``allow_refresh=False`` is for the gateway's own loop: it renews expiring
    accounts under the pool lock first, and a second writer refreshing from here
    would race it into ``invalid_grant`` (upstream revokes the session when a
    refresh token is reused).
    """
    record = _load(path)
    label = record.get("email") or path.stem
    if allow_refresh:
        token, rotated = _fresh_token(record, proxy)
        # The refresh token rotates on every use, so a refresh has to reach the
        # file even when the reward turns out to be already claimed.
        if rotated:
            _save(path, record)
    else:
        token = (record.get("accessToken") or "").strip()

    panel = status(token, region, timezone, proxy)
    info = summarize(panel)
    if dry_run or not info["claimable"]:
        return {"label": label, "claimed": False, **info}

    result = claim(token, region, timezone, proxy)
    outcome = result.get("claim_result")
    if outcome == JUST_CLAIMED:
        log.info("signed in %s: +%s credits (day %s, expires %s)",
                 label, result.get("points"), result.get("day_no"), result.get("expire_at_ms"))
        return {
            "label": label, "claimed": True,
            "points": int(result.get("points") or 0),
            "day_no": result.get("day_no"),
            "expire_at_ms": result.get("expire_at_ms"),
            "claim_id": result.get("claim_id"),
        }
    return {"label": label, "claimed": False, "already": outcome == ALREADY_CLAIMED, **info}


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
            results.append(claim_file(path, region, timezone, proxy, dry_run, allow_refresh))
        except (SigninError, AccountError, OSError, json.JSONDecodeError) as exc:
            log.warning("check-in failed for %s: %s", path.stem, exc)
            results.append({"label": path.stem, "claimed": False, "error": str(exc)[:200]})
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

