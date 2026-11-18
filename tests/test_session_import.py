"""Cookie / session import: parsing, the minting pipeline, and the re-mint path.

Run with ``python tests/test_session_import.py`` (no network, no credentials).
The pipeline tests substitute a fake ``Session`` for the curl-driven one, so what
is being asserted here is the *shape* of a cookie-imported record and the wiring
between it and ``TokenPool`` — the three HTTP round trips themselves can only be
proven against the live host.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import account_api, cookies, onboard, provision  # noqa: E402
from app.auth import CredentialError, Token, TokenPool, _record_to_token  # noqa: E402


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_cookie_header_is_parsed() -> None:
    pairs = cookies.parse("Cookie: _token=abc.def; sessionid=xyz%3D; theme=dark")
    assert pairs == {"_token": "abc.def", "sessionid": "xyz%3D", "theme": "dark"}
    # A bare value with no "Cookie:" prefix is the same thing minus the noise.
    assert cookies.parse("a=1; b=2") == {"a": "1", "b": "2"}
    # Multi-line paste: one header per line, all merged.
    assert cookies.parse("a=1\nCookie: b=2") == {"a": "1", "b": "2"}


def test_cookie_json_export_is_parsed() -> None:
    item = [{"name": "token", "value": "t1", "domain": ".minimax.io"}, {"Name": "sid", "Value": "s1"}]
    assert cookies.parse(json.dumps(item)) == {"token": "t1", "sid": "s1"}
    assert cookies.parse(json.dumps({"cookies": item})) == {"token": "t1", "sid": "s1"}
    assert cookies.parse(json.dumps({"token": "t2"})) == {"token": "t2"}


def test_netscape_jar_is_parsed() -> None:
    jar = (
        "# Netscape HTTP Cookie File\n"
        "\n"
        ".minimax.io\tTRUE\t/\tTRUE\t1797071451\t_token\tj1\n"
        "#HttpOnly_.minimax.io\tTRUE\t/\tTRUE\t0\tsessionid\tj2\n"
    )
    assert cookies.parse(jar) == {"_token": "j1", "sessionid": "j2"}


def test_cookie_junk_is_dropped_not_stored() -> None:
    # Empty values, control characters and non-token names would produce a
    # malformed Cookie header, so they never reach the jar.
    pairs = cookies.parse("good=1; empty=; bad name=2; ctl=a\x01b; =novalue")
    assert pairs == {"good": "1"}


def test_cookie_input_is_bounded() -> None:
    try:
        cookies.parse("a=" + "x" * (cookies.MAX_INPUT + 10))
    except ValueError as exc:
        assert "上限" in str(exc)
    else:  # pragma: no cover - the guard is the point
        raise AssertionError("an oversized paste must be refused")


def test_jar_and_domain_shape() -> None:
    assert cookies.domain_for("https://account.minimax.io") == ".minimax.io"
    assert cookies.domain_for("https://account.minimax.cn/") == ".minimax.cn"
    # An IP literal keeps its own name, and a secure cookie is flagged as such.
    assert cookies.domain_for("http://127.0.0.1:8080") == "127.0.0.1"
    text = cookies.jar({"a": "1", "b": "2"}, ".minimax.io")
    rows = [line.split("\t") for line in text.splitlines() if line and not line.startswith("#")]
    assert rows == [
        [".minimax.io", "TRUE", "/", "TRUE", "0", "a", "1"],
        [".minimax.io", "TRUE", "/", "TRUE", "0", "b", "2"],
    ]
    # secure=FALSE is only for a plain-http account base (a local mock): curl
    # drops a secure cookie over http, so the flag has to follow the scheme.
    plain = cookies.jar({"a": "1"}, ".minimax.io", secure=False)
    assert plain.splitlines()[2].split("\t")[3] == "FALSE"
    assert cookies.header({"a": "1", "b": "2"}) == "a=1; b=2"
    # mask() is what goes into a log line: names only, never values.
    masked = cookies.mask({"a": "secret-value", "b": "s2"})
    assert "secret-value" not in masked and "s2" not in masked
    assert "a" in masked


# ---------------------------------------------------------------------------
# the minting pipeline (fake Session, no network)
# ---------------------------------------------------------------------------
class FakeSession:
    """Stands in for the curl-driven Session at the only three seams we use."""

    def __init__(self, proxy=None, timeout=60.0, base=account_api.BASE) -> None:
        self.proxy = proxy
        self.base = base
        self.seeded: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def seed_cookies(self, raw, domain=None):
        pairs = dict(raw) if isinstance(raw, dict) else cookies.parse(raw)
        if not pairs:
            raise account_api.AccountError("cookie 里没有解析出任何 name=value")
        self.seeded = pairs
        return pairs

    def device_code(self) -> dict:
        return {"user_code": "ABCD-1234", "device_code": "dc", "code_verifier": "verifier"}

    def approve(self, user_code: str) -> str:
        assert self.seeded, "approve() must run against a seeded jar"
        return "group-1"

    def exchange(self, grant: dict) -> dict:
        return {"access_token": "mmoat_imported", "refresh_token": "mmort_imported",
                "expires_in": 43200}


def test_cookie_import_record_shape() -> None:
    with patch.object(account_api, "Session", FakeSession), \
            patch.object(account_api, "_verify_token", lambda *a, **k: {"claimable": True}):
        record = account_api.provision_with_cookies(
            "Cookie: _token=t; sessionid=s", "someone@mail.example", "en", None
        )
    assert record["accessToken"] == "mmoat_imported"
    assert record["refreshToken"] == "mmort_imported"
    assert record["region"] == "en"
    assert record["source"] == "cookie"
    assert record["session"]["cookie"] == "_token=t; sessionid=s"
    assert record["session"]["region"] == "en"
    assert record["expiresAtMs"] > record["obtainedAtMs"] - 1000
    assert record["signin_check"] == {"claimable": True}
    # A password never exists on this path, so it can never leak into the file.
    assert "password" not in record


def test_cookie_import_needs_a_cookie() -> None:
    with patch.object(account_api, "Session", FakeSession):
        try:
            account_api.provision_with_cookies("nothing useful here", "x@y.z")
        except account_api.AccountError as exc:
            assert "name=value" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("an empty cookie must not reach the network")


def test_record_roundtrip_keeps_the_session() -> None:
    """The pool re-reads token files constantly; the cookie has to survive that."""
    record = {
        "region": "en",
        "email": "someone@mail.example",
        "accessToken": "mmoat_a",
        "refreshToken": "mmort_a",
        "expiresAtMs": 1_900_000_000_000,
        "session": {"cookie": "_token=t", "obtainedAtMs": 1_700_000_000_000, "region": "en"},
        "signin": {"day": "2026-11-18", "points": 400},
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = provision.write_record(Path(tmp), record)
        stored = json.loads(path.read_text("utf-8"))
        assert stored["session"]["cookie"] == "_token=t"
        token = _record_to_token(stored, 0, path, 0)
        assert token is not None
        assert token.session_cookie == "_token=t"
        assert token.region == "en"
        assert token.label == "someone@mail.example"


# ---------------------------------------------------------------------------
# pool-side re-mint
# ---------------------------------------------------------------------------
def _token_with_session(tmp: Path) -> Token:
    record = {
        "region": "en",
        "accessToken": "mmoat_old",
        "refreshToken": "mmort_old",
        "expiresAtMs": 1_900_000_000_000,
        "session": {"cookie": "_token=t", "region": "en"},
    }
    path = provision.write_record(tmp, record)
    return _record_to_token(record, 0, path, 0)


def test_remint_replaces_credentials_and_persists() -> None:
    fresh = {
        "accessToken": "mmoat_new",
        "refreshToken": "mmort_new",
        "expiresAtMs": 1_900_000_500_000,
        "session": {"cookie": "_token=t"},
    }
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(account_api, "provision_with_cookies", lambda *a, **k: fresh):
        token = _token_with_session(Path(tmp))
        assert token is not None
        pool = TokenPool()
        asyncio.run(pool._remint_from_session(token))
        assert token.access_token == "mmoat_new"
        assert token.refresh_token == "mmort_new"
        on_disk = json.loads(token.origin.read_text("utf-8"))
        assert on_disk["accessToken"] == "mmoat_new"
        # The credential that makes the next re-mint possible must not be lost by
        # the rewrite (``_write_record`` merges instead of replacing).
        assert on_disk["session"]["cookie"] == "_token=t"


def test_rejected_refresh_falls_back_to_the_cookie() -> None:
    """The one failure the README used to call unrecoverable."""
    fresh = {
        "accessToken": "mmoat_new",
        "refreshToken": "mmort_new",
        "expiresAtMs": 1_900_000_500_000,
        "session": {"cookie": "_token=t"},
    }

    async def scenario() -> Token:
        with tempfile.TemporaryDirectory() as tmp:
            token = _token_with_session(Path(tmp))
            assert token is not None
            pool = TokenPool()

            async def boom(_token):
                raise CredentialError("invalid_grant")

            with patch.object(pool, "_refresh", boom), \
                    patch.object(account_api, "provision_with_cookies", lambda *a, **k: fresh):
                return await pool._refresh_once(token)

    token = asyncio.run(scenario())
    assert token.access_token == "mmoat_new"


def test_failed_remint_still_parks_the_account() -> None:
    async def scenario() -> str:
        with tempfile.TemporaryDirectory() as tmp:
            token = _token_with_session(Path(tmp))
            assert token is not None
            pool = TokenPool()

            async def boom(_token):
                raise CredentialError("no refresh token")

            def dead_cookie(*_a, **_k):
                raise account_api.AccountError("session 被拒绝（HTTP 401）")

            with patch.object(pool, "_refresh", boom), \
                    patch.object(account_api, "provision_with_cookies", dead_cookie):
                try:
                    await pool._refresh_once(token)
                except Exception as exc:  # noqa: BLE001 - the type is part of the contract
                    return type(exc).__name__
                return "no error"

    assert asyncio.run(scenario()) == "CredentialError"


# ---------------------------------------------------------------------------
# console / importer parsing
# ---------------------------------------------------------------------------
def test_importer_parses_cookie_lines() -> None:
    text = (
        "# comment\n"
        "\n"
        "someone@mail.example _token=a; sessionid=b\n"
        "other@mail.example\t_token=c\n"
        "Cookie: _token=d\n"          # no label -> must be rejected, not imported
        "third@mail.example not-a-cookie\n"
        "someone@mail.example _token=dup\n"
    )
    rows, bad = onboard.parse_accounts(text, "cookie")
    assert rows == [
        ("someone@mail.example", "_token=a; sessionid=b"),
        ("other@mail.example", "_token=c"),
    ]
    assert len(bad) == 2


def test_importer_still_parses_password_lines() -> None:
    rows, bad = onboard.parse_accounts("a@mail.example pw1\nb@mail.example pw2\njunk\n")
    assert rows == [("a@mail.example", "pw1"), ("b@mail.example", "pw2")]
    assert bad == ["junk"]


def test_importer_rejects_unknown_mode() -> None:
    async def scenario() -> str:
        try:
            await onboard.Importer().start("a@mail.example _token=x", mode="oauth")
        except ValueError as exc:
            return str(exc)
        return "no error"

    assert "未知导入方式" in asyncio.run(scenario())


def test_console_exposes_the_cookie_mode() -> None:
    """The mode switch is static HTML: nothing else would catch it going away."""
    html = (Path(__file__).resolve().parents[1] / "app" / "console.html").read_text("utf-8")
    assert 'data-addmode="cookie"' in html
    assert "mode: addMode" in html
    assert "ADD_DOC" in html
    # The empty-state hint must mention the second way in, or nobody finds it.
    assert "邮箱 Cookie" in html


def test_admin_import_routes_the_mode() -> None:
    """The console sends {"mode": "cookie"}; the endpoint must not ignore it."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    original = settings.api_keys
    settings.api_keys = ["unit-test-key"]
    headers = {"x-api-key": "unit-test-key"}
    try:
        client = TestClient(app)
        empty = client.post("/admin/accounts/import", headers=headers,
                            json={"text": "", "mode": "cookie"})
        assert empty.status_code == 400, empty.text
        assert "Cookie" in empty.json()["detail"]

        unknown = client.post("/admin/accounts/import", headers=headers,
                              json={"text": "a@b.c _token=x", "mode": "oauth"})
        assert unknown.status_code == 400, unknown.text
        assert "未知导入方式" in unknown.json()["detail"]
    finally:
        settings.api_keys = original


TESTS = [
    test_cookie_header_is_parsed,
    test_cookie_json_export_is_parsed,
    test_netscape_jar_is_parsed,
    test_cookie_junk_is_dropped_not_stored,
    test_cookie_input_is_bounded,
    test_jar_and_domain_shape,
    test_cookie_import_record_shape,
    test_cookie_import_needs_a_cookie,
    test_record_roundtrip_keeps_the_session,
    test_remint_replaces_credentials_and_persists,
    test_rejected_refresh_falls_back_to_the_cookie,
    test_failed_remint_still_parks_the_account,
    test_importer_parses_cookie_lines,
    test_importer_still_parses_password_lines,
    test_importer_rejects_unknown_mode,
    test_console_exposes_the_cookie_mode,
    test_admin_import_routes_the_mode,
]


def main() -> None:
    for test in TESTS:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(TESTS)} cookie/session tests passed")


if __name__ == "__main__":
    main()
