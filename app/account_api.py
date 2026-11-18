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

Two ways in, one flow out:

* :func:`provision` — email + password, which the account host trades for a
  cookie session and nothing else;
* :func:`provision_with_cookies` — an existing browser session, pasted. It skips
  the password entirely and enters at the same device-authorisation step, which
  is why an account imported either way is indistinguishable inside the pool.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from . import cookies as cookie_io
from .config import REGION_ACCOUNT_BASE

log = logging.getLogger("minimax.account")

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


def base_for(region: str) -> str:
    """Account host for a region, falling back to the international one."""
    return (REGION_ACCOUNT_BASE.get((region or "en").lower()) or BASE).rstrip("/")


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

    def __init__(self, proxy: str | None = None, timeout: float = 60.0, base: str = BASE) -> None:
        self.proxy = proxy
        self.timeout = timeout
        # Which account host this jar belongs to. ``origin``/``referer`` have to
        # match it: the signed endpoints reject a request whose origin is the
        # other region's host.
        self.base = (base or BASE).rstrip("/")
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
            "-H", f"origin: {self.base}",
            "-H", f"referer: {self.base}/unified-login",
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
    def seed_cookies(self, raw: str | dict[str, str], domain: str | None = None) -> dict[str, str]:
        """Load a pasted browser session into the jar. Returns the parsed pairs.

        Raises :class:`AccountError` when the paste holds no usable cookie: an
        empty jar would fail two requests later with "no authorisable group",
        which reads like a banned account rather than a bad copy-paste.
        """
        pairs = dict(raw) if isinstance(raw, dict) else cookie_io.parse(raw)
        if not pairs:
            raise AccountError(
                "cookie 里没有解析出任何 name=value；从浏览器复制整条 Cookie 请求头再试"
            )
        span = domain or cookie_io.domain_for(self.base)
        # The secure flag follows the scheme we are about to use: curl drops a
        # secure cookie on a plain-http request, which would look like a dead
        # session rather than a jar mismatch.
        secure = self.base.lower().startswith("https")
        self._jar.write_text(cookie_io.jar(pairs, span, secure=secure), encoding="utf-8")
        # Names only: this line ends up in `docker logs`, and a session value in a
        # log file is the same leak as a password in a log file.
        log.info("seeded %s for %s", cookie_io.mask(pairs), self.base)
        return pairs

    def login(self, email: str, password: str, device_id: str = "100000000") -> dict:
        self.request("GET", f"{self.base}/unified-login")
        body = {
            "loginType": LOGIN_PASSWORD,
            "email": email,
            "authToken": encrypt_password(password),
            "deviceID": device_id,
        }
        url, headers = signed_headers("/oauth2/login", body)
        response = self.request("POST", self.base + url, json_body=body, headers=headers)
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
        response = self.request("POST", f"{self.base}/oauth2/device/code", form={
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
        """Authorise a device code *as the jar's logged-in user*.

        This is the step a password buys you into, and the step a pasted cookie
        replaces. It is also the only honest way to test whether a session is
        valid: an expired or wrong-domain jar answers 401 / an empty ``groups``
        list here, several calls before anything is written to disk.
        """
        info_response = self.request(
            "GET", f"{self.base}/oauth2/device/authorize?user_code={user_code}&app_id=3001"
        )
        info = info_response.json()
        groups = info.get("groups") or []
        if not groups:
            detail = json.dumps(info, ensure_ascii=False)[:200]
            if info_response.status >= 400:
                raise AccountError(
                    f"session 被拒绝（HTTP {info_response.status}）：{detail}",
                    code=info.get("code") if isinstance(info.get("code"), int) else None,
                    payload=info,
                )
            raise AccountError(
                "session 没被识别成已登录用户（HTTP 200 但 groups 为空）：cookie 可能不属于 "
                f"{self.base}、已过期，或复制时漏了关键字段。上游回：{detail}",
                payload=info,
            )
        group_id = next((g["group_id"] for g in groups if g.get("is_default")), groups[0]["group_id"])
        body = {"user_code": user_code, "decision": "approve", "group_id": group_id}
        url, headers = signed_headers("/oauth2/device/authorize", body)
        response = self.request("POST", self.base + url, json_body=body, headers=headers)
        if response.status >= 400:
            raise AccountError(f"approve failed: {response.body[:300].decode('utf-8', 'ignore')}")
        return group_id

    def exchange(self, grant: dict) -> dict:
        response = self.request("POST", f"{self.base}/oauth2/token", form={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": grant["device_code"],
            "client_id": CLIENT_ID,
            "code_verifier": grant["code_verifier"],
        })
        body = response.json()
        if not body.get("access_token"):
            raise AccountError(f"token exchange failed: {body}")
        return body


def provision(email: str, password: str, proxy: str | None = None, region: str = "en") -> dict:
    """Full flow for one account: login -> device authorise -> tokens."""
    with Session(proxy=proxy, base=base_for(region)) as session:
        profile = session.login(email, password)
        grant = session.device_code()
        group_id = session.approve(grant["user_code"])
        tokens = session.exchange(grant)
    return {
        "region": region,
        "email": email,
        "username": profile.get("username"),
        "user_id": profile.get("str_user_id"),
        "group_id": group_id,
        "accessToken": tokens["access_token"].strip(),
        "refreshToken": (tokens.get("refresh_token") or "").strip() or None,
        "expiresAtMs": int(time.time() * 1000) + int(tokens.get("expires_in") or 0) * 1000,
        "obtainedAtMs": int(time.time() * 1000),
    }


def provision_with_cookies(
    session_cookie: str,
    email: str = "",
    region: str = "en",
    proxy: str | None = None,
    verify: bool = True,
) -> dict:
    """Mint a token from a pasted browser session — no password anywhere.

    Identical to :func:`provision` from ``/oauth2/device/code`` onwards; the only
    change is that the cookie jar is seeded from the paste instead of from
    ``/oauth2/login``. The cookie is stored in the record so the pool can re-mint
    later without a human (see ``auth.TokenPool._remint_from_session``), which is
    the one failure mode that previously required re-adding the account.

    ``verify`` spends one extra GET on the product host to confirm the freshly
    minted token is actually accepted there. Without it a wrong-region cookie
    imports cleanly and then 401s on every request, which is far more expensive
    to diagnose later than one round trip now.
    """
    pairs = cookie_io.parse(session_cookie)
    if not pairs:
        raise AccountError("cookie 里没有解析出任何 name=value；请复制完整的 Cookie 请求头")

    base = base_for(region)
    with Session(proxy=proxy, base=base) as session:
        session.seed_cookies(pairs)
        grant = session.device_code()
        group_id = session.approve(grant["user_code"])
        tokens = session.exchange(grant)

    now_ms = int(time.time() * 1000)
    record = {
        "region": region,
        "email": email or None,
        "group_id": group_id,
        "accessToken": tokens["access_token"].strip(),
        "refreshToken": (tokens.get("refresh_token") or "").strip() or None,
        "expiresAtMs": now_ms + int(tokens.get("expires_in") or 0) * 1000,
        "obtainedAtMs": now_ms,
        # Marks how this record was obtained. The pool reads it only for display;
        # rotation behaves the same either way.
        "source": "cookie",
        "session": {
            "cookie": cookie_io.header(pairs),
            "obtainedAtMs": now_ms,
            "region": region,
        },
    }

    if verify:
        record["signin_check"] = _verify_token(record["accessToken"], region, proxy)
    return record


def _verify_token(access_token: str, region: str, proxy: str | None) -> dict:
    """Read the sign-in panel once, to prove the token works on the product host.

    A business rejection (``base_resp.status_code``) means the token cannot be
    used at all — wrong region, revoked group, banned account — and is raised so
    the import row fails loudly instead of polluting the pool. A transport error
    only means the check could not run, which must not fail an otherwise good
    account, so it is reported and the account stays.
    """
    from . import signin

    try:
        panel = signin.status(access_token, region, signin.DEFAULT_TIMEZONE, proxy)
    except Exception as exc:  # noqa: BLE001 - see docstring for the split
        if getattr(exc, "code", None):
            raise AccountError(
                f"token 换到了，但产品接口用不了（{exc}）——cookie 与账号区域或分组不匹配？"
            ) from exc
        return {"error": str(exc)[:160]}
    return signin.summarize(panel)


def refresh(record: dict, proxy: str | None = None) -> dict:
    """Refresh one stored record in place and return the updated record."""
    with Session(proxy=proxy, base=base_for(record.get("region") or "en")) as session:
        response = session.request("POST", f"{session.base}/oauth2/token", form={
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
