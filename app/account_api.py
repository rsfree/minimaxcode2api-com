"""MiniMax account-service client: password login and device authorisation.

The account host exposes an internal web API rather than a documented OAuth
surface, so a few details have to be reproduced exactly:

* every request carries the query bundle the web client adds
  (``device_platform``, ``biz_id``, ``app_id``, ``version_code``, ``unix`` …);
* ``/oauth2/login`` and ``POST /oauth2/device/authorize`` additionally need
  ``x-timestamp`` / ``x-signature`` / ``yy`` headers, where the signature is an
  MD5 over a fixed salt plus the serialised body;
* the password is RSA/PKCS#1 v1.5 encrypted with the public key baked into the
  web bundle.

All HTTP goes through the platform ``curl`` binary. On networks whose egress
proxy only accepts native TLS stacks (Windows schannel), Python's OpenSSL
handshake is reset before any HTTP is exchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

BASE = "https://account.minimax.io"
CLIENT_ID = "mcode-public"
SCOPE = "agent.default"
AUDIENCE = "agent-backend"
SIGN_SALT = "I*7Cf%WZ#S&%1RlZJ&C2"
IS_WINDOWS = sys.platform.startswith("win")

RSA_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDF5ndG2/UB4L5tbvQaNLHSoBTW"
    "DKbrNBuOmUIP23eCmC2ELMx3kppEikxTp5cV8NxUZl6ii+KLwKugioAXApzypHXb"
    "gXbq13kTKA7OCA1xtAoMdH9cltjBiFAUJlgmVjr0MuJCknhVAjWLjCVRHege+Atl"
    "gkUBUeGa9O+cWcPEwQIDAQAB"
)

# loginType values used by the web client.
LOGIN_PASSWORD = "20"
LOGIN_EMAIL_CODE = "21"


class AccountError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, payload: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


# ---------------------------------------------------------------------------
# crypto
# ---------------------------------------------------------------------------
def _tlv(buf: bytes, pos: int):
    tag = buf[pos]
    length = buf[pos + 1]
    start = pos + 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[start:start + n], "big")
        start += n
    return tag, buf[start:start + length], start + length


def _public_key() -> tuple[int, int]:
    der = base64.b64decode(RSA_PUBLIC_KEY)
    _, spki, _ = _tlv(der, 0)
    _, _, next_pos = _tlv(spki, 0)
    _, bits, _ = _tlv(spki, next_pos)
    _, seq, _ = _tlv(bits[1:], 0)
    _, n_raw, pos = _tlv(seq, 0)
    _, e_raw, _ = _tlv(seq, pos)
    return int.from_bytes(n_raw, "big"), int.from_bytes(e_raw, "big")


_N, _E = _public_key()
_KEY_BYTES = (_N.bit_length() + 7) // 8


def encrypt_password(password: str) -> str:
    msg = password.encode()
    pad_len = _KEY_BYTES - 3 - len(msg)
    if pad_len < 8:
        raise AccountError("password too long for the RSA key")
    padding = bytearray()
    while len(padding) < pad_len:
        byte = os.urandom(1)[0]
        if byte:
            padding.append(byte)
    em = b"\x00\x02" + bytes(padding) + b"\x00" + msg
    cipher = pow(int.from_bytes(em, "big"), _E, _N)
    return base64.b64encode(cipher.to_bytes(_KEY_BYTES, "big")).decode()


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _params(now_ms: int) -> dict:
    return {
        "device_platform": "web",
        "biz_id": 3,
        "app_id": "3001",
        "version_code": "22201",
        "unix": now_ms,
        "timezone_offset": 480,
        "sys_language": "en",
        "uuid": "5d0f3f8a-6c2b-4a1e-9d7a-1c2b3d4e5f60",
        "os_name": "Windows",
        "browser_name": "chrome",
        "browser_language": "en-US",
        "browser_platform": "Win32",
        "client": "web",
    }


def signed_headers(path: str, body: dict) -> tuple[str, dict[str, str]]:
    """Return ``(url_with_params, headers)`` for a signed POST."""
    now_ms = int(time.time() * 1000)
    ts = now_ms // 1000
    body_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    params = _params(now_ms)
    url = path + "?" + urlencode(params)
    yy = md5(quote(url, safe="-_.!~*'()") + "_" + body_json + md5(str(ts)) + "ooui")
    return url, {
        "yy": yy,
        "x-timestamp": str(ts),
        "x-signature": md5(f"{ts}{SIGN_SALT}{body_json}"),
    }


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> dict:
        try:
            data = json.loads(self.body)
        except ValueError as exc:
            raise AccountError(f"non-JSON response: {self.body[:200]!r}") from exc
        return data if isinstance(data, dict) else {}


def _tempfile(suffix: str) -> Path:
    fd, path = tempfile.mkstemp(prefix="mmx-", suffix=suffix)
    # Windows keeps the file locked while the descriptor is open, which would
    # stop curl from writing the cookie jar.
    os.close(fd)
    return Path(path)


class Session:
    """One account's cookie jar, driven through curl."""

    def __init__(self, proxy: str | None = None, timeout: float = 60.0) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self._jar = _tempfile(".cookies")
        self._hdr = _tempfile(".headers")

    def close(self) -> None:
        for path in (self._jar, self._hdr):
            try:
                path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _args(self, method: str, url: str, extra: list[str], json_body: str | None) -> list[str]:
        args = [
            "curl", "-sS", "-X", method, "-D", str(self._hdr),
            "-m", str(int(self.timeout)),
            "-c", str(self._jar), "-b", str(self._jar),
            "-H", "accept: application/json",
            "-H", f"origin: {BASE}",
            "-H", f"referer: {BASE}/unified-login",
        ]
        if IS_WINDOWS:
            args.append("--ssl-no-revoke")
        if self.proxy:
            args += ["-x", self.proxy]
        else:
            args += ["--noproxy", "*"]
        if json_body is not None:
            args += ["-H", "content-type: application/json", "--data-binary", json_body]
        args += extra
        args.append(url)
        return args

    def request(
        self,
        method: str,
        url: str,
        json_body: dict | None = None,
        form: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        extra: list[str] = []
        for key, value in (headers or {}).items():
            extra += ["-H", f"{key}: {value}"]
        if form:
            for key, value in form.items():
                extra += ["--data-urlencode", f"{key}={value}"]
        serialised = None if json_body is None else json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
        self._hdr.write_bytes(b"")  # curl -D appends when the file exists
        proc = subprocess.run(self._args(method, url, extra, serialised), capture_output=True, timeout=self.timeout + 15)
        body = proc.stdout
        try:
            head = self._hdr.read_bytes()
        except OSError:
            head = b""
        status = 0
        parsed: dict[str, str] = {}
        for line in head.decode("iso-8859-1", "ignore").splitlines():
            if line.startswith("HTTP/"):
                parts = line.split(" ", 2)
                if len(parts) >= 2 and parts[1].isdigit():
                    status = int(parts[1])
                    parsed.clear()
            elif ":" in line:
                key, _, value = line.partition(":")
                parsed[key.strip().lower()] = value.strip()
        if status == 0 and proc.returncode != 0:
            raise AccountError(f"curl failed: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
        return Response(status, body, parsed)

    # -- high level -------------------------------------------------------
    def login(self, email: str, password: str, device_id: str = "100000000") -> dict:
        self.request("GET", f"{BASE}/unified-login")
        body = {
            "loginType": LOGIN_PASSWORD,
            "email": email,
            "authToken": encrypt_password(password),
            "deviceID": device_id,
        }
        url, headers = signed_headers("/oauth2/login", body)
        response = self.request("POST", BASE + url, json_body=body, headers=headers)
        payload = response.json()
        code = payload.get("code")
        if code != 0:
            raise AccountError(
                payload.get("msg") or "login failed", code=code if isinstance(code, int) else None, payload=payload
            )
        return payload.get("data") or {}

    def device_code(self) -> dict:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        response = self.request("POST", f"{BASE}/oauth2/device/code", form={
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "audience": AUDIENCE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        grant = response.json()
        if not grant.get("user_code"):
            raise AccountError(f"device code failed: {grant}")
        grant["code_verifier"] = verifier
        return grant

    def approve(self, user_code: str) -> str:
        info = self.request(
            "GET", f"{BASE}/oauth2/device/authorize?user_code={user_code}&app_id=3001"
        ).json()
        groups = info.get("groups") or []
        if not groups:
            raise AccountError(f"no authorisable group: {info}")
        group_id = next((g["group_id"] for g in groups if g.get("is_default")), groups[0]["group_id"])
        body = {"user_code": user_code, "decision": "approve", "group_id": group_id}
        url, headers = signed_headers("/oauth2/device/authorize", body)
        response = self.request("POST", BASE + url, json_body=body, headers=headers)
        if response.status >= 400:
            raise AccountError(f"approve failed: {response.body[:300].decode('utf-8', 'ignore')}")
        return group_id

    def exchange(self, grant: dict) -> dict:
        response = self.request("POST", f"{BASE}/oauth2/token", form={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": grant["device_code"],
            "client_id": CLIENT_ID,
            "code_verifier": grant["code_verifier"],
        })
        body = response.json()
        if not body.get("access_token"):
            raise AccountError(f"token exchange failed: {body}")
        return body


def provision(email: str, password: str, proxy: str | None = None) -> dict:
    """Full flow for one account: login -> device authorise -> tokens."""
    with Session(proxy=proxy) as session:
        profile = session.login(email, password)
        grant = session.device_code()
        group_id = session.approve(grant["user_code"])
        tokens = session.exchange(grant)
    return {
        "region": "en",
        "email": email,
        "username": profile.get("username"),
        "user_id": profile.get("str_user_id"),
        "group_id": group_id,
        "accessToken": tokens["access_token"].strip(),
        "refreshToken": (tokens.get("refresh_token") or "").strip() or None,
        "expiresAtMs": int(time.time() * 1000) + int(tokens.get("expires_in") or 0) * 1000,
        "obtainedAtMs": int(time.time() * 1000),
    }


def refresh(record: dict, proxy: str | None = None) -> dict:
    """Refresh one stored record in place and return the updated record."""
    with Session(proxy=proxy) as session:
        response = session.request("POST", f"{BASE}/oauth2/token", form={
            "grant_type": "refresh_token",
            "refresh_token": record["refreshToken"],
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "audience": AUDIENCE,
        })
        body = response.json()
    if not body.get("access_token"):
        raise AccountError(f"refresh failed: {body}")
    updated = dict(record)
    updated["accessToken"] = body["access_token"].strip()
    updated["refreshToken"] = (body.get("refresh_token") or record.get("refreshToken") or "").strip()
    updated["expiresAtMs"] = int(time.time() * 1000) + int(body.get("expires_in") or 0) * 1000
    updated["obtainedAtMs"] = int(time.time() * 1000)
    return updated
