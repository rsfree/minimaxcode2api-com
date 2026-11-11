"""Device-code login with PKCE, so a headless server can mint its own credential.

Usage::

    python -m app.login --out /data/token.json

Mirrors the public client the desktop app uses
(``client_id=mcode-public``, ``scope=agent.default``, ``audience=agent-backend``)
including the S256 PKCE challenge the account service requires. The refresh
token written to ``--out`` keeps the gateway alive after the access token
expires.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time

import httpx

from .auth import AUDIENCE, CLIENT_ID, SCOPES
from .config import REGION_ACCOUNT_BASE, _env

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _fail(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _expiry_seconds(body: dict, now_ms: int) -> int:
    """Account deployments return ``expired_in`` as an absolute ms timestamp."""
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return int(expires_in)
    expired_in = body.get("expired_in")
    if isinstance(expired_in, (int, float)):
        if expired_in < 1_000_000_000_000:
            return int(expired_in)
        remaining = int((expired_in - now_ms) / 1000)
        if remaining > 0:
            return remaining
    return 600


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniMax Code device login (PKCE)")
    parser.add_argument("--region", default=_env("MINIMAX_REGION", "en"), choices=sorted(REGION_ACCOUNT_BASE))
    parser.add_argument("--out", default=_env("TOKEN_STORE", "token.json"))
    parser.add_argument("--proxy", default=_env("UPSTREAM_PROXY"))
    args = parser.parse_args()

    base = REGION_ACCOUNT_BASE[args.region]
    proxy = args.proxy or None
    verifier, challenge = _pkce_pair()

    with httpx.Client(proxy=proxy, timeout=60.0, follow_redirects=True) as client:
        response = client.post(
            f"{base}/oauth2/device/code",
            data={
                "client_id": CLIENT_ID,
                "scope": SCOPES,
                "audience": AUDIENCE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        if response.status_code != 200:
            _fail(f"device code request failed: HTTP {response.status_code} {response.text[:300]}")
        grant = response.json()

        user_code = grant.get("user_code")
        device_code = grant.get("device_code")
        verify_uri = grant.get("verification_uri") or grant.get("verification_url")
        verify_full = grant.get("verification_uri_complete") or verify_uri

        # Two shapes exist: the standard device grant returns `device_code` to
        # poll with; the account variant returns only `user_code` and is polled
        # with that instead, with `interval`/`expired_in` in milliseconds.
        user_code_polling = not device_code and bool(user_code) and "expired_in" in grant
        polling_field = "user_code" if user_code_polling else "device_code"
        polling_value = user_code if user_code_polling else device_code
        if not polling_value or not user_code:
            _fail(f"unexpected device code response: {grant}")

        interval = float(grant.get("interval") or 5)
        if user_code_polling and interval > 1000:
            interval = interval / 1000
        interval = max(2.0, interval)

        expires = _expiry_seconds(grant, int(time.time() * 1000))

        print()
        print("  1. open this page :", verify_uri)
        print("  2. enter the code :", user_code)
        print("     or open directly:", verify_full)
        print(f"     (code expires in {max(1, expires // 60)} min)")
        print()
        print("waiting for authorisation...", flush=True)

        deadline = time.time() + expires
        token_payload: dict | None = None
        while time.time() < deadline:
            time.sleep(interval)
            poll = client.post(
                f"{base}/oauth2/token",
                data={
                    "grant_type": DEVICE_GRANT,
                    polling_field: polling_value,
                    "client_id": CLIENT_ID,
                    "code_verifier": verifier,
                },
            )
            try:
                body = poll.json()
            except ValueError:
                body = {}

            status = str(body.get("status") or body.get("error") or "").lower()
            if poll.status_code == 200 and body.get("access_token"):
                token_payload = body
                break
            if poll.status_code == 200 and status in {"pending", "authorization_pending"}:
                continue
            if status == "slow_down":
                interval += 5
                continue
            if status in {"denied", "access_denied"}:
                _fail("authorisation denied")
            if status in {"expired", "expired_token"}:
                _fail("device code expired, start again")
            if status in {"", "authorization_pending"} and poll.status_code < 400:
                continue
            _fail(f"token poll failed: HTTP {poll.status_code} {poll.text[:300]}")

        if not token_payload:
            _fail("timed out waiting for authorisation")

        access = (token_payload.get("access_token") or "").strip()
        if not access:
            _fail(f"token response missing access_token: {token_payload}")

        expires_in = token_payload.get("expires_in")
        record = {
            "region": args.region,
            "accessToken": access,
            "refreshToken": (token_payload.get("refresh_token") or "").strip() or None,
            "expiresAtMs": int(time.time() * 1000) + int(expires_in) * 1000 if expires_in else None,
            "obtainedAtMs": int(time.time() * 1000),
        }

        out_path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
        try:
            os.chmod(out_path, 0o600)
        except OSError:
            pass

        print(f"\nOK  access token : {access[:12]}...")
        print(f"    refresh token: {'yes' if record['refreshToken'] else 'no'}")
        print(f"    written to   : {out_path}")
        print(f"\nSet TOKEN_STORE={out_path} and MINIMAX_REGION={args.region} for the gateway.")


if __name__ == "__main__":
    main()
