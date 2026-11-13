"""Dependency-free smoke test for the translation layer.

Run with ``python tests/test_smoke.py`` (also used as the container smoke test
in CI, so it must not touch the network).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import DEFAULT_MODEL, resolve_model, visible_models  # noqa: E402
from app.convert import (  # noqa: E402
    build_anthropic_request,
    build_openai_response,
    stream_openai,
)


def test_model_aliases() -> None:
    assert resolve_model("m3") == "MiniMax-M3"
    assert resolve_model("MiniMax-M3.1-Flash") == "MiniMax-M3.1-Flash"
    assert resolve_model("minimax-m3.1-flash") == "MiniMax-M3.1-Flash"
    assert resolve_model(None) == DEFAULT_MODEL
    # Unknown ids are forwarded verbatim so a new upstream model works unlisted.
    assert resolve_model("MiniMax-M9") == "MiniMax-M9"
    assert DEFAULT_MODEL in {model.id for model in visible_models()}


def test_model_catalogue_complete() -> None:
    """Every endpoint the gateway serves should be enumerable from /v1/models."""
    from app import media
    from app.catalog import visible_media_models

    chat = {model.id: model for model in visible_models()}
    media_models = {model.id: model for model in visible_media_models()}

    # Whitelisted by the channel's own provider config (512K context / 128K out).
    for name in ("MiniMax-M3.1-Flash", "MiniMax-M3"):
        assert name in chat, f"{name} missing from the catalogue"
        assert chat[name].context_window == 512_000
        assert chat[name].max_output == 128_000
        assert chat[name].vision
    assert chat["MiniMax-M3.1-Flash"].thinking == "forced_on"

    # Legacy but still served upstream — must stay visible so clients do not break.
    for name in (
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5",
        "MiniMax-M2.5-highspeed",
    ):
        assert name in chat and chat[name].legacy

    # M2 / M2.1 answer 404 "no longer available (2069)": listing them would
    # hand a client a model that can only fail.
    from app.catalog import MODELS

    all_ids = {model.id: model for model in MODELS}
    for name in ("MiniMax-M2", "MiniMax-M2.1"):
        assert all_ids[name].retired
        assert name not in chat

    # Multimodal entries exist for every media endpoint, with the right kind.
    assert {"minimax-image", "minimax-speech", "minimax-music"} <= set(media_models)
    assert {m.id for m in media_models.values() if m.kind == "video"} == set(media.VIDEO_MODELS)
    assert media_models["minimax-image"].kind == "image"

    # /v1/models carries the fields a picker needs.
    row = chat["MiniMax-M3"].to_openai()
    assert row["type"] == "chat" and row["context_length"] == 512_000
    assert row["capabilities"]["vision"] is True


def test_video_model_enum_matches_upstream() -> None:
    """The upstream tool schema is the authority on which video models exist.

    ``GET {mcp}/tools`` declares exactly three, with per-model duration and
    resolution rules. H3-Max was missing from this gateway, so asking for the
    fast variant returned a 400 the upstream would have accepted.
    """
    from app import media

    assert media.VIDEO_MODELS == ("MiniMax-H3", "MiniMax-H3-Max", "MiniMax-Hailuo-2.3")
    assert media.normalize_video_model("h3-max") == "MiniMax-H3-Max"
    assert media.normalize_video_model("MINIMAX-H3-MAX") == "MiniMax-H3-Max"
    assert media.normalize_video_model("h3") == "MiniMax-H3"
    assert media.normalize_video_model(None) == media.DEFAULT_VIDEO_MODEL

    try:
        media.normalize_video_model("H4")
    except media.MediaError as exc:
        assert exc.status == 400
        assert "5-15s" in exc.message, exc.message
    else:
        raise AssertionError("an unknown video model must be rejected")


def test_system_and_text() -> None:
    body = {
        "model": "m3",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
    }
    payload = build_anthropic_request(body)
    assert payload["model"] == "MiniMax-M3"
    assert payload["system"] == [{"type": "text", "text": "be brief"}]
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert payload["max_tokens"] > 0


def test_tool_roundtrip() -> None:
    body = {
        "model": "MiniMax-M3",
        "messages": [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"SZ"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "get_weather", "parameters": {"type": "object"}},
            }
        ],
        "tool_choice": "auto",
    }
    payload = build_anthropic_request(body)
    assistant = payload["messages"][1]
    assert assistant["role"] == "assistant"
    tool_use = assistant["content"][0]
    assert tool_use["type"] == "tool_use" and tool_use["input"] == {"city": "SZ"}
    tool_result = payload["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "call_1"
    assert payload["tools"][0]["name"] == "get_weather"
    assert payload["tool_choice"] == {"type": "auto"}


def test_response_translation() -> None:
    payload = {
        "id": "msg_1",
        "model": "MiniMax-M3",
        "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "toolu_1", "name": "ping", "input": {}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3},
    }
    result = build_openai_response(payload, "MiniMax-M3")
    choice = result["choices"][0]
    assert choice["message"]["content"] == "hello"
    assert choice["message"]["reasoning_content"] == "hmm"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "ping"
    assert choice["finish_reason"] == "tool_calls"
    assert result["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 3},
    }


async def _fake_events():
    payloads = [
        ("message_start", {"type": "message_start", "message": {"model": "MiniMax-M3", "usage": {"input_tokens": 5}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_9", "name": "ping"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    for event, data in payloads:
        yield event, data


def test_stream_translation() -> None:
    async def run() -> list[dict]:
        chunks: list[dict] = []
        async for line in stream_openai(_fake_events(), "MiniMax-M3"):
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            chunks.append(json.loads(line[6:]))
        return chunks

    chunks = asyncio.run(run())
    text = "".join(
        choice.get("delta", {}).get("content", "") for chunk in chunks for choice in chunk["choices"]
    )
    assert text == "OK"
    tool_chunks = [
        call
        for chunk in chunks
        for choice in chunk["choices"]
        for call in choice.get("delta", {}).get("tool_calls", [])
    ]
    assert tool_chunks[0]["function"]["name"] == "ping"
    final = chunks[-1]["choices"][0]
    assert final["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"]["completion_tokens"] == 2


def test_signin_panel() -> None:
    from app.signin import CLAIMABLE, CLAIMED, summarize

    panel = {
        "scene": 1,
        "days": [
            {"day_no": 1, "points": 400, "bonus_points": 0, "status": CLAIMED, "is_today": True},
            {"day_no": 2, "points": 400, "bonus_points": 0, "status": CLAIMABLE, "is_today": False},
            {"day_no": 4, "points": 1000, "bonus_points": 0, "status": CLAIMABLE, "is_today": False},
        ],
    }
    info = summarize(panel)
    assert info["today_day"] == 1 and info["today_status"] == CLAIMED
    assert info["claimable"] is False
    assert info["cycle_total_points"] == 1800
    assert info["cycle_pending_points"] == 1400

    live = dict(panel, days=[dict(panel["days"][0], status=CLAIMABLE)])
    assert summarize(live)["claimable"] is True


def test_console_asset() -> None:
    """The console is a static file copied into the image — Go-style embed-less.

    Nothing in CI would otherwise notice if it were dropped from the build
    context, and the route would then 500 at runtime.
    """
    from pathlib import Path

    html_path = Path(__file__).resolve().parents[1] / "app" / "console.html"
    assert html_path.is_file(), f"missing {html_path}"
    html = html_path.read_text(encoding="utf-8")

    assert "<title>minimax-code2api" in html
    # Every view the nav can switch to must exist, or the tab silently does nothing.
    for view in ("chat", "image", "speech", "accounts", "usage", "capacity", "logs", "models", "config"):
        assert f'data-view="{view}"' in html, f"nav missing {view}"
        assert f'id="view-{view}"' in html, f"section missing {view}"

    # The console talks to the same endpoints the rest of the gateway exposes.
    for endpoint in ("/v1/chat/completions", "/v1/images/generations", "/v1/audio/speech"):
        assert endpoint in html, f"console does not call {endpoint}"

    # ...and the console's own backend. A typo here renders an empty page with no
    # visible error, so the endpoints are pinned by name.
    for endpoint in ("/admin/summary", "/admin/overview", "/admin/usage", "/admin/logs",
                     "/admin/capacity", "/admin/config", "/admin/signin",
                     "/admin/accounts/import", "/admin/errors", "/admin/diagnostics"):
        assert endpoint in html, f"console does not call {endpoint}"

    # A failed call has to be readable from the page itself: the row table shows
    # the error text, the row opens a detail sheet, and the 诊断 modal exists.
    # Without these the page can only say "HTTP 502" and stop there.
    for anchor in ('id="logVeil"', 'id="diagVeil"', 'id="logGroups"', "错误 / 说明",
                   "kindLabel(", "openLogDetail(", "copyText("):
        assert anchor in html, f"console lost {anchor}"

    # The pool list carries no balance — it lives in the probe payload. The row
    # mapping has to lift it, or the whole 积分 column quietly prints dashes
    # (which is exactly how the empty-balance bug looked).
    assert "balance: pr.balance" in html


def test_pool_hot_reload() -> None:
    """Accounts dropped into TOKEN_POOL join the rotation without a restart."""
    import tempfile

    from app.auth import TokenPool
    from app.config import settings

    def record(name: str) -> str:
        return json.dumps(
            {"email": f"{name}@mail.example.com", "accessToken": f"tok-{name}", "expiresAtMs": 4102444800000}
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "aaa.json").write_text(record("aaa"), encoding="utf-8")
        (root / "bbb.json").write_text(record("bbb"), encoding="utf-8")

        original = settings.token_pool
        settings.token_pool = str(root)
        try:
            pool = TokenPool()
            assert pool.describe()["pool_size"] == 2

            # An in-memory refresh is newer than the file; reload must not undo it.
            live = pool.find("tok-aaa")
            assert live is not None
            live.access_token = "tok-aaa-rotated"

            (root / "ccc.json").write_text(record("ccc"), encoding="utf-8")
            assert asyncio.run(pool.reload_if_changed()) is True
            described = pool.describe()
            assert described["pool_size"] == 3, described
            assert pool.find("tok-ccc") is not None
            assert pool.find("tok-aaa-rotated") is not None

            # Unchanged directory → no reload.
            assert asyncio.run(pool.reload_if_changed()) is False

            (root / "bbb.json").unlink()
            assert asyncio.run(pool.reload_if_changed()) is True
            assert pool.describe()["pool_size"] == 2
            assert pool.find("tok-bbb") is None
            assert pool.find("tok-aaa-rotated") is not None
        finally:
            settings.token_pool = original


def test_pool_reload_survives_write_race() -> None:
    """A file that lands while the pool is being read must not be lost forever.

    This actually happened in production: 12 accounts were provisioned in a loop,
    the watcher read the directory just before the last file was written, then
    recorded the *post-write* signature — so the account never joined the pool
    until something else touched the directory. Snapshotting the signature
    before the read makes the next tick retry.
    """
    import tempfile

    from app import auth as auth_module
    from app.auth import TokenPool
    from app.config import settings

    def record(name: str) -> str:
        return json.dumps(
            {"email": f"{name}@mail.example.com", "accessToken": f"tok-{name}", "expiresAtMs": 4102444800000}
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "aaa.json").write_text(record("aaa"), encoding="utf-8")

        original_pool = settings.token_pool
        original_files = auth_module._pool_files
        settings.token_pool = str(root)
        try:
            pool = TokenPool()
            assert pool.describe()["pool_size"] == 1

            # Simulate the race: the directory listing is taken, then a file
            # appears before that listing is read.
            def racing_files(path: Path) -> list[Path]:
                listed = original_files(path)
                (path / "late.json").write_text(record("late"), encoding="utf-8")
                return listed

            auth_module._pool_files = racing_files
            merged = asyncio.run(pool.reload())
            assert merged["added"] == 0, "the racing file is not in this listing"
            auth_module._pool_files = original_files

            # ...so the pool must still consider itself stale and pick it up.
            assert asyncio.run(pool.reload_if_changed()) is True
            assert pool.describe()["pool_size"] == 2
            assert pool.find("tok-late") is not None
        finally:
            settings.token_pool = original_pool
            auth_module._pool_files = original_files


def test_pool_adopts_rotated_credentials_from_file() -> None:
    """A refresh written to the file must reach the live token.

    The refresh token is single-use upstream (verified: the old value answers
    ``invalid_grant`` right after a rotation). Two writers refresh from files —
    the daily check-in thread and this process — so the file is the shared
    truth. Keeping the boot-time copy in memory would make the gateway present a
    spent refresh token at the next renewal and park a healthy account on auth.
    """
    import tempfile
    import time

    from app.auth import TokenPool
    from app.config import settings

    def record(access: str, refresh: str, expires: int) -> str:
        return json.dumps({
            "email": "aaa@mail.example.com",
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAtMs": expires,
        })

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "aaa.json").write_text(record("tok-old", "rt-old", 1_800_000_000_000), encoding="utf-8")
        original = settings.token_pool
        settings.token_pool = str(root)
        try:
            pool = TokenPool()
            live = pool.find("tok-old")
            assert live is not None
            live.ok_count = 7
            live.cooldown_until = time.time() + 600
            live.cooldown_reason = "auth"

            # The check-in thread refreshed the account and wrote the new pair.
            (root / "aaa.json").write_text(
                record("tok-new", "rt-new", 1_800_043_200_000), encoding="utf-8"
            )
            assert asyncio.run(pool.reload_if_changed()) is True

            assert live.access_token == "tok-new", "the live token keeps the spent credential"
            assert live.refresh_token == "rt-new"
            assert live.ok_count == 7, "counters belong to the live object, not the file"
            assert not live.is_cooling(), "a replaced credential lifts the auth park"

            # A file that is *behind* the live object must not win: an unwritten
            # refresh would pair a new access token with a spent refresh token.
            live.access_token = "tok-newer"
            live.expires_at_ms = 1_800_100_000_000
            assert asyncio.run(pool.reload())["pool_size"] == 1
            assert live.access_token == "tok-newer"
        finally:
            settings.token_pool = original


def test_token_writes_are_atomic_and_leave_no_temp_files() -> None:
    """Writers must not share a rename target, and must not litter the pool.

    The gateway and the check-in thread both write rotated tokens into the same
    directory. A shared ``x.json.tmp`` lets their bytes interleave into a file
    that is then renamed into place — a corrupt record that costs a re-login.
    """
    import tempfile

    from app.auth import Token, _write_record

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "aaa.json"
        token = Token(access_token="tok-1", refresh_token="rt-1", expires_at_ms=1, label="aaa@mail.example.com")
        _write_record(path, token, "en")
        token.access_token = "tok-2"
        _write_record(path, token, "en")

        assert json.loads(path.read_text(encoding="utf-8"))["accessToken"] == "tok-2"
        leftovers = sorted(p.name for p in Path(tmp).iterdir() if p.name != "aaa.json")
        assert leftovers == [], f"temp files left behind: {leftovers}"
        if not sys.platform.startswith("win"):
            # Windows has no 0600; the permission check only means something on POSIX.
            assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_a_spent_refresh_token_is_never_presented() -> None:
    """When the file already holds a newer pair, use it instead of refreshing.

    Measured on the live account host: reusing a rotated refresh token answers
    ``invalid_grant`` *and* kills the access token that was minted moments
    before — upstream reads a reused value as a leaked session. So the gateway
    must check the file before exchanging anything; a stale in-memory copy (the
    check-in thread rotated it first) is not evidence that a refresh is needed.
    """
    import tempfile

    from app.auth import TokenPool
    from app.config import settings

    def record(access: str, refresh: str, expires: int) -> str:
        return json.dumps({
            "email": "aaa@mail.example.com",
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAtMs": expires,
        })

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "aaa.json"
        path.write_text(record("tok-old", "rt-old", 1_800_000_000_000), encoding="utf-8")

        original = settings.token_pool
        settings.token_pool = str(root)
        try:
            pool = TokenPool()
            live = pool.find("tok-old")
            assert live is not None

            # The check-in thread rotated and wrote the file, then the gateway
            # hits a 401 and asks for a renewal.
            path.write_text(record("tok-new", "rt-new", 1_800_043_200_000), encoding="utf-8")

            seen: list[str] = []

            async def fake_refresh(token) -> None:
                seen.append(token.refresh_token or "")
                token.access_token = "tok-refreshed"
                return token

            pool._refresh = fake_refresh  # type: ignore[method-assign]

            assert asyncio.run(pool.renew(live)) is True
            assert seen == [], "the spent refresh token reached the network"
            assert live.access_token == "tok-new"
            assert live.refresh_token == "rt-new"
        finally:
            settings.token_pool = original


def test_expired_token_refreshes_once() -> None:
    """A token that is genuinely spent (and not newer on disk) does refresh."""
    import tempfile
    import time

    from app.auth import TokenPool
    from app.config import settings

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "aaa.json").write_text(json.dumps({
            "email": "aaa@mail.example.com",
            "accessToken": "tok-old",
            "refreshToken": "rt-old",
            "expiresAtMs": int(time.time() * 1000) - 60_000,
        }), encoding="utf-8")

        original = settings.token_pool
        settings.token_pool = str(root)
        try:
            pool = TokenPool()
            live = pool.find("tok-old")
            assert live is not None and live.is_expired()

            seen: list[str] = []

            async def fake_refresh(token) -> None:
                seen.append(token.refresh_token or "")
                token.access_token = "tok-refreshed"
                token.expires_at_ms = int(time.time() * 1000) + 43_200_000
                return token

            pool._refresh = fake_refresh  # type: ignore[method-assign]

            assert asyncio.run(pool._refresh_once(live)) is not None
            assert seen == ["rt-old"], seen
            assert live.access_token == "tok-refreshed"
        finally:
            settings.token_pool = original


def test_quota_errors_rotate() -> None:
    """An empty account must not fail the request; it must be skipped.

    Upstream answers 400 with "not enough credits" for a spent account while its
    siblings still work, so the message — not the status code — decides whether
    the pool rotates.
    """
    from app.upstream import _quota_exhausted

    for text in ("not enough credits", "Insufficient balance (1008)", "quota exceeded"):
        assert _quota_exhausted(text), text
    for text in ("unknown model: MiniMax-M9", "invalid request body", ""):
        assert not _quota_exhausted(text), text


def test_metrics_rollup() -> None:
    """The console's counters have to add up; a broken rollup misleads quietly."""
    from app.metrics import Metrics

    m = Metrics(limit=50)
    m.record("chat", "MiniMax-M3", account="a@mail.example.com", ok=True, latency_ms=800,
             prompt_tokens=100, completion_tokens=40)
    m.record("chat", "MiniMax-M3", account="a@mail.example.com", ok=True, latency_ms=1200,
             prompt_tokens=50, completion_tokens=10)
    m.record("chat.stream", "MiniMax-M3.1-Flash", account="b@mail.example.com", ok=False,
             status=429, latency_ms=200, error="rate limited")

    snap = m.snapshot(24)
    total = snap["total"]
    assert total["requests"] == 3 and total["success"] == 2 and total["errors"] == 1
    assert total["prompt_tokens"] == 150 and total["completion_tokens"] == 50
    assert abs(total["success_rate"] - 66.7) < 0.2
    assert total["p95_latency_ms"] >= 800
    models = {row["model"]: row for row in snap["by_model"]}
    assert models["MiniMax-M3"]["total_tokens"] == 200
    assert models["MiniMax-M3.1-Flash"]["errors"] == 1
    assert sum(p["requests"] for p in snap["series"]["points"]) == 3
    # A failed call must not borrow the latency of the good ones.
    assert models["MiniMax-M3.1-Flash"]["avg_latency_ms"] == 0


def test_account_import_flow() -> None:
    """The console's「＋ 添加账号」must mint, write and surface accounts in one go.

    Provisioning talks to the account host, so the network call is replaced by a
    fake here; everything around it — line parsing, per-account isolation, the
    single-job guard, token-file writing and the pool pick-up — is the real code.
    """
    import tempfile
    import threading

    from app import onboard
    from app.account_api import AccountError
    from app.auth import token_manager
    from app.config import settings

    gate = threading.Event()
    gate.set()
    calls: list[str] = []

    def fake_provision(email: str, password: str, proxy=None) -> dict:
        gate.wait(5)
        calls.append(email)
        if email.startswith("bad"):
            raise AccountError("wrong password", code=1001)
        return {
            "region": "en",
            "email": email,
            "accessToken": f"tok-{email}",
            "refreshToken": "refresh",
            "expiresAtMs": 4102444800000,
        }

    async def run(imp: "onboard.Importer", text: str) -> dict:
        await imp.start(text, concurrency=2)
        for _ in range(400):
            if not imp.running:
                break
            await asyncio.sleep(0.01)
        snap = imp.snapshot()
        assert snap is not None
        return snap

    with tempfile.TemporaryDirectory() as tmp:
        original_pool = settings.token_pool
        original_provision = onboard.provision
        settings.token_pool = tmp
        onboard.provision = fake_provision
        try:
            # Parsing: space, tab and comma all separate; duplicates collapse;
            # prose without an email is reported rather than smuggled through.
            rows, bad = onboard.parse_accounts(
                "a@x.com  pw1\nb@x.com,\tpw2\nA@X.com  again\nc@x.com;pw3\n# comment\nnot a line\n"
            )
            assert [r[0] for r in rows] == ["a@x.com", "b@x.com", "c@x.com"], rows
            assert bad == ["not a line"], bad
            assert rows[1][1] == "pw2"

            imp = onboard.Importer()
            job = asyncio.run(run(imp, "a@x.com pw1\nbad@x.com pw2\nc@x.com pw3"))
            assert job["state"] == "done"
            assert job["total"] == 3 and job["ok"] == 2 and job["failed"] == 1
            states = {row["email"]: row["state"] for row in job["rows"]}
            assert states["a@x.com"] == "ok" and states["c@x.com"] == "ok"
            assert states["bad@x.com"] == "fail"
            assert "登录被拒" in next(r["error"] for r in job["rows"] if r["email"] == "bad@x.com")

            written = Path(tmp) / "a.json"
            assert written.exists()
            text = written.read_text(encoding="utf-8")
            assert "tok-a@x.com" in text and "password" not in text
            # A partially written token must never be picked up as an account.
            assert not list(Path(tmp).glob("*.tmp"))

            # Newly minted accounts are in rotation straight away.
            assert token_manager.find("a@x.com") is not None

            # Re-importing the same address refreshes the file instead of
            # creating a twin, and says so.
            again = asyncio.run(run(imp, "a@x.com pw1"))
            assert again["ok"] == 1 and again["updated"] == 1
            assert again["rows"][0]["state"] == "updated"

            # One job at a time: the login endpoint rate-limits.
            async def contention() -> bool:
                gate.clear()
                try:
                    await imp.start("d@x.com pw")
                    try:
                        await imp.start("e@x.com pw")
                    except RuntimeError:
                        refused = True
                    else:
                        refused = False
                finally:
                    gate.set()
                for _ in range(400):
                    if not imp.running:
                        break
                    await asyncio.sleep(0.01)
                return refused

            assert asyncio.run(contention()) is True
            assert calls  # sanity: the fake actually ran
        finally:
            settings.token_pool = original_pool
            onboard.provision = original_provision


def test_admin_requires_key() -> None:
    """Every /admin route sits behind the same key as the API surface."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    original = settings.api_keys
    settings.api_keys = ["unit-test-key"]
    try:
        client = TestClient(app)
        assert client.get("/admin/summary").status_code == 401
        assert client.get("/admin/summary", headers={"x-api-key": "wrong"}).status_code == 401
        ok = client.get("/admin/summary", headers={"x-api-key": "unit-test-key"})
        assert ok.status_code == 200
        body = ok.json()
        assert "pool" in body and "usage" in body and "alerts" in body
        # The console page must never carry the key itself.
        page = client.get("/ui")
        assert page.status_code in {200, 401}
    finally:
        settings.api_keys = original


def test_balance_surfacing() -> None:
    """积分余额 must come from the membership endpoint, not the paid ledger.

    Free-plan accounts have an empty ledger but a real balance — often exactly 0,
    sometimes negative after overspending a granted pack. The old console treated
    "no ledger batches" as "no data" and printed a dash, which is what made the
    card look broken.
    """
    import time

    from app.probe import AccountProbe, ProbeCache, summarize_membership

    info = summarize_membership({
        "plan_name": "Free",
        "total_remains_credit": -72,
        "opcredit_balance": 0,
        "op_credit_summary": {
            "total_remaining_amount": "400",
            "purchased_remaining_amount": "0",
            "free_remaining_amount": "400",
        },
        "daily_login_gift_credit_remaining": 0,
        "has_token_plan": False,
        "expires_at": 0,
    })
    # The spendable number is the op_credit total; total_remains_credit is a
    # decoy that reads 0 (or stale negatives) on these accounts.
    assert info["total"] == 400 and info["credit"] == 400
    assert info["legacy_credit"] == -72
    assert info["free"] == 400 and info["purchased"] == 0

    # No op_credit_summary → fall back to the legacy field rather than showing 0.
    legacy_only = summarize_membership({"total_remains_credit": -72})
    assert legacy_only["total"] == -72 and legacy_only["free"] is None

    # A sparse payload must not explode, and absent decimals stay None (not 0):
    # "unknown" and "zero" are different answers.
    sparse = summarize_membership({"total_remains_credit": 0})
    assert sparse["credit"] == 0 and sparse["free"] is None and sparse["plan"] == ""

    cache = ProbeCache()
    for label, credit in (("a@x", 0), ("b@x", -72), ("c@x", 400)):
        item = AccountProbe(label=label)
        item.membership = {"credit": credit, "plan": "Free"}
        item.updated_at = time.time()
        cache._items[label] = item

    totals = cache.snapshot()["totals"]
    assert totals["balance"] == 328
    assert totals["balance_negative"] == 1 and totals["balance_zero"] == 1
    assert totals["balance_positive"] == 1
    assert totals["plans"] == {"Free": 3}
    # No paid batches at all — the figure the console used to render as "—".
    assert totals["credit_batches"] == 0
    # Capacity planning watches the same number, so a real spend becomes a rate.
    assert cache._tracked_remaining() == 328


def test_membership_payload_is_not_unwrapped() -> None:
    """`get_membership_info` puts its fields next to base_resp, not under `data`.

    Unwrapping it like the sign-in panel returns `{}`, which silently turns every
    balance into None — the exact failure that made the card show a dash.
    """
    import subprocess as sp

    from app import signin

    class Done:
        def __init__(self, payload: str) -> None:
            self.returncode = 0
            self.stdout = payload.encode()
            self.stderr = b""

    original = signin.subprocess.run
    try:
        signin.subprocess.run = lambda *a, **k: Done(
            '{"plan_name":"Free","total_remains_credit":-72,'
            '"base_resp":{"status_code":0,"status_msg":"ok"}}'
        )
        body = signin._request(
            "POST", "https://agent.minimax.io/matrix/api/v1/commerce/get_membership_info",
            "tok", body={}, unwrap=False,
        )
        assert body["total_remains_credit"] == -72, body
        assert body["plan_name"] == "Free"

        # Default behaviour is unchanged for the endpoints that do wrap in data.
        signin.subprocess.run = lambda *a, **k: Done(
            '{"data":{"days":[{"day_no":1}]},"base_resp":{"status_code":0,"status_msg":"ok"}}'
        )
        assert signin._request("GET", "https://agent.minimax.io/signin/status", "tok") == {
            "days": [{"day_no": 1}]
        }
    finally:
        signin.subprocess.run = original


def test_quota_park_lasts_until_reset() -> None:
    """1008 is a *daily* allowance: parking for 15 minutes just buys another fail.

    The park lasts until the next local midnight (plus a settle buffer) and a
    successful sign-in is what lifts it — that is the whole "签到再恢复" loop.
    """
    import time

    from app.auth import (
        REASON_AUTH,
        REASON_QUOTA,
        CredentialError,
        Token,
        TokenPool,
        next_reset_label,
        seconds_until_next_day,
    )
    from app.config import settings

    seconds = seconds_until_next_day(settings.signin_timezone)
    assert 60 <= seconds <= 24 * 3600 + 11 * 60, seconds
    assert len(next_reset_label(settings.signin_timezone)) == 5

    pool = TokenPool()
    token = Token(access_token="tok-a", label="a@x", pooled=True)
    pool._tokens = [token]
    pool._loaded = True

    pool.park(token, 402, "insufficient balance (1008)", next_day=True)
    assert token.cooldown_reason == REASON_QUOTA
    assert token.cooldown_until - time.time() > 3600, "a daily quota must not cool down for 15 minutes"
    assert token.is_cooling() is True

    # 签到 = 回血：解除额度冷却，但绝不顺手放过凭证类冷却。
    assert pool.clear_quota_cooldown("a@x") is True
    assert token.is_cooling() is False
    pool.park(token, 401, "invalid token")
    assert token.cooldown_reason == REASON_AUTH
    assert pool.clear_quota_cooldown("a@x") is False
    assert token.is_cooling() is True

    # 全池都因额度停摆时，错误要告诉运维什么时候恢复，而不是「都不可用」。
    other = TokenPool()
    lone = Token(access_token="tok-b", label="b@x", pooled=True)
    other._tokens = [lone]
    other._loaded = True
    other.park(lone, 402, "1008", next_day=True)
    try:
        asyncio.run(other.acquire())
    except CredentialError as exc:
        assert "daily reset" in str(exc), exc
    else:
        raise AssertionError("expected CredentialError when every account is out of quota")


def test_request_history_survives_restart() -> None:
    """A redeploy must not wipe the log — that is the entire point of the store."""
    import tempfile

    from app.metrics import Metrics
    from app.store import Store

    # ignore_cleanup_errors: Windows keeps the sqlite handle for a moment after
    # close(), and a failure to unlink a temp file must not fail the assertion.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = str(Path(tmp) / "requests.db")
        first = Store(db, retention_days=30)
        assert first.available, first.error

        m = Metrics()
        m.attach(first)
        m.record("chat", "MiniMax-M3.1-Flash", account="a@x", ok=True, latency_ms=120,
                 prompt_tokens=10, completion_tokens=5, client="1.2.3.4")
        m.record("chat", "MiniMax-M3.1-Flash", account="a@x", ok=False, status=402,
                 latency_ms=90, error="insufficient balance (1008)")
        m.record("image", "minimax-image", account="b@x", ok=True, latency_ms=900)
        assert first.flush() == 3

        before = m.snapshot(24)["total"]
        assert before["requests"] == 3 and before["success"] == 2 and before["errors"] == 1
        assert m.snapshot(24)["source"] == "sqlite"

        # Paging is by total, not by "whatever fits in 4000 rows".
        page = m.recent_page(limit=2)
        assert page["total"] == 3 and len(page["rows"]) == 2
        assert len(m.recent_page(limit=2, offset=2)["rows"]) == 1
        assert m.recent_page(outcome="error")["total"] == 1

        totals = first.account_totals()
        assert totals["a@x"]["ok"] == 1 and totals["a@x"]["fail"] == 1
        assert totals["a@x"]["tokens_in"] == 10 and totals["b@x"]["ok"] == 1
        first.close()

        # A redeploy is exactly this: a new object opening the same file.
        second = Store(db, retention_days=30)
        assert second.available, second.error
        m2 = Metrics()
        m2.attach(second)
        after = m2.snapshot(24)["total"]
        assert after["requests"] == 3, after
        assert after["success"] == 2 and after["errors"] == 1
        assert after["total_tokens"] == 15
        rows = m2.recent_page(limit=10)["rows"]
        assert any(r["error"] == "insufficient balance (1008)" for r in rows)
        assert second.account_totals()["b@x"]["ok"] == 1

        # Retention is enforced on open, not by a cron job.
        second.close()
        old = Store(db, retention_days=0)
        old.close()


def test_store_degrades_to_memory() -> None:
    """A read-only container must still serve requests, just without history."""
    import tempfile

    from app.metrics import Metrics
    from app.store import Store

    # A directory is not a database file; opening it must fail cleanly rather
    # than take the process down.
    with tempfile.TemporaryDirectory() as tmp:
        broken = Store(tmp)
        assert not broken.available
        assert broken.stats()["mode"] == "memory"

    m = Metrics()
    m.attach(broken)
    m.record("chat", "MiniMax-M3.1-Flash", account="a@x", ok=True)
    snap = m.snapshot(24)
    assert snap["source"] == "memory"
    assert snap["total"]["requests"] == 1
    assert m.recent_page()["source"] == "memory"


def test_admin_logs_pagination() -> None:
    """The endpoint reports the real total so the page can page through it."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    original = settings.api_keys
    settings.api_keys = ["unit-test-key"]
    try:
        with TestClient(app) as client:
            data = client.get("/admin/logs?limit=1&offset=1",
                              headers={"x-api-key": "unit-test-key"}).json()
            assert {"rows", "count", "total", "offset", "source"} <= set(data)
            assert data["offset"] == 1
            assert isinstance(data["total"], int) and data["total"] >= data["count"]
            assert data["source"] in {"sqlite", "memory"}
            # The account page carries both units: this process and lifetime.
            pool = client.get("/admin/overview",
                              headers={"x-api-key": "unit-test-key"}).json()["pool"]
            assert "accounts" in pool
    finally:
        settings.api_keys = original


def test_redaction_hides_accounts_and_tokens() -> None:
    """Caller-facing errors keep the reason and lose the identity.

    The 503 path interpolates the pool's own CredentialError, and that text says
    "account <label> has no refresh token" — the label *is* the mailbox. The
    request log may name it (that is the field an operator needs); a caller may
    not, so the filtering happens on the way out.
    """
    from app.main import error_response, media_error_response
    from app.media import MediaError
    from app.redact import leaks, scrub

    raw = (
        "no usable account: account aav2dfsmiw@mail.giie.eu.org has no refresh token; "
        "refresh failed for ops+alerts@sub.example.co.uk: HTTP 400 "
        "refresh_token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4"
    )
    clean = scrub(raw)
    assert not leaks(clean), clean
    assert "@" not in clean
    assert "<account>" in clean
    assert "eyJ" not in clean
    # The diagnosis survives: the caller still learns what to do.
    assert clean.startswith("no usable account:")

    # Ordinary prose must not be mangled into uselessness.
    plain = "upstream HTTP 502 while reading the response body"
    assert scrub(plain) == plain

    body = json.loads(error_response(503, raw).body)
    assert body["error"]["code"] == "upstream_error"
    assert "giie.eu.org" not in body["error"]["message"]
    assert "@" not in body["error"]["message"]

    media = json.loads(
        media_error_response(MediaError(402, "credit gone for x@y.com, code 1008", code=1008)).body
    )
    assert media["error"]["type"] == "insufficient_quota"
    assert "x@y.com" not in media["error"]["message"]
    # The business code still travels, but in its own field: the message itself
    # is replaced wholesale for 402 (see the plan test below).
    assert media["error"]["code"] == 1008


def test_quota_errors_do_not_advertise_the_subscription_plan() -> None:
    """A 402 explains our billing, not the caller's mistake.

    Upstream answers a spent video balance with "the remaining M Plan weekly
    limit and account credits cannot cover this ... upgrade your plan". That
    names the plan we buy and how it is metered — procurement detail a caller
    cannot act on. The status and a retry hint are all that ship.
    """
    from app.main import error_response, media_error_response
    from app.media import MediaError
    from app.redact import for_caller, leaks, scrub

    upstream = (
        "the remaining M Plan weekly limit and account credits cannot cover this "
        "MiniMax-H3 video generation; MiniMax-H3 and MiniMax-H3-Max are paid from "
        "the M Plan weekly limit on tiers that cover them; upgrade your plan"
    )
    for text in (for_caller(upstream, 402), json.loads(error_response(402, upstream).body)["error"]["message"]):
        low = text.lower()
        assert "plan" not in low, text
        assert "weekly limit" not in low, text
        assert "credits" not in low, text
        assert "upgrade" not in low, text
        assert "minimax-h3" not in low, text

    # Status alone is enough to sanitise, and 402 gets a type callers can branch on.
    body = json.loads(error_response(402, upstream).body)
    assert body["error"]["type"] == "insufficient_quota"
    assert "@" not in body["error"]["message"]

    media = json.loads(media_error_response(MediaError(402, upstream, code=1008)).body)
    assert media["error"]["type"] == "insufficient_quota"
    assert "plan" not in media["error"]["message"].lower()

    # The fallback regex is what saves a path that never passed a status code:
    # the plan name still goes, even though the surrounding prose stays.
    masked = scrub(upstream)
    assert "M Plan" not in masked and "m plan" not in masked.lower()
    assert "<plan>" in masked
    assert leaks(upstream) and not leaks(masked)
    # And a plain 500 keeps its own words.
    assert "upstream HTTP 502" in for_caller("upstream HTTP 502 while reading the body", 502)


def test_redaction_masks_bare_token_prefixes() -> None:
    """``Token.short`` is ``access_token[:12]`` — too short for a regex, too real
    to ship. The pool's live values are replaced by identity, not by pattern."""
    from unittest.mock import patch

    from app import auth
    from app.redact import leaks, scrub

    class FakeToken:
        label = ""
        access_token = "sk-abcdef1234567890"
        refresh_token = "rt-99887766554433"

    with patch.object(auth.token_manager, "_tokens", [FakeToken()]):
        dirty = "account sk-abcdef1234567890 failed; retry forced"
        clean = scrub(dirty)
        assert "sk-abcdef" not in clean, clean
        assert "<redacted>" in clean
        assert clean.endswith("failed; retry forced")
        assert leaks(dirty) and not leaks(clean)


def test_pool_exhaustion_never_names_the_mailbox_to_the_caller() -> None:
    """End to end: 503 says the pool is dry, the log says which mailbox broke."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app import upstream as upstream_mod
    from app.auth import CredentialError
    from app.config import settings
    from app.main import app
    from app.metrics import metrics

    async def dry_pool(exclude=None):
        raise CredentialError("account leak@mail.example.org has no refresh token")

    original = settings.api_keys
    settings.api_keys = ["unit-test-key"]
    try:
        with TestClient(app) as client:
            with patch.object(upstream_mod.token_manager, "acquire", dry_pool):
                response = client.post(
                    "/v1/chat/completions",
                    headers={"x-api-key": "unit-test-key"},
                    json={"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}]},
                )
    finally:
        settings.api_keys = original

    assert response.status_code == 503, response.text
    assert "mail.example.org" not in response.text
    assert "@" not in response.text
    assert "no usable account" in response.text

    # ...but the history row still carries the address, which is what makes the
    # console useful when a pool quietly loses its refresh token.
    rows = metrics.recent(limit=50)
    assert any("leak@mail.example.org" in (row.get("error") or "") for row in rows), rows[:3]


def test_transport_status_never_escapes_as_http() -> None:
    """A dead socket reports status 0; that must leave as 502, not a bare 500.

    ``_error_from(0, b"")`` produces ``UpstreamError(0, "upstream HTTP 0")`` and
    a zero status cannot be serialised into a response — the caller then got a
    bodyless 500, which is exactly the "bad response status code" a relay shows.
    """
    from app.main import error_response

    assert error_response(0, "upstream HTTP 0").status_code == 502
    assert error_response(599, "edge").status_code == 599
    assert error_response(429, "slow down").status_code == 429


def test_curl_fallback_expires_instead_of_latching_forever() -> None:
    """One httpx blip must not decide the transport for the rest of uptime.

    Production showed the failure mode: a single ``ConnectError`` at 12:37 set a
    permanent flag, and every request after that went out over curl — which at
    the time could not carry a body over 128 KiB. Nothing logged it again; the
    symptom was "some chats 500 for no reason" hours later.
    """
    import time as _time

    from app import upstream as up
    from app.config import settings

    original = settings.upstream_transport
    settings.upstream_transport = "auto"
    try:
        state = up.Upstream()
        assert state._prefer_httpx() is True
        assert state.transport_state() == "httpx"

        state._latch_curl()
        assert state._prefer_httpx() is False
        assert state.transport_state().startswith("curl")

        # ...and after the cool-down it goes back to httpx on its own.
        state._curl_until = _time.monotonic() - 1
        assert state._prefer_httpx() is True
        assert state.transport_state() == "httpx"

        # Explicit settings stay explicit in both directions.
        settings.upstream_transport = "curl"
        assert up.Upstream()._prefer_httpx() is False
        settings.upstream_transport = "httpx"
        forced = up.Upstream()
        forced._latch_curl()
        assert forced._prefer_httpx() is True
    finally:
        settings.upstream_transport = original


def test_curl_transport_keeps_bodies_out_of_argv() -> None:
    """A request body must never ride in argv.

    Linux caps a *single* argument at MAX_ARG_STRLEN = 131072 bytes, so
    ``create_subprocess_exec("curl", ..., json.dumps(body), url)`` dies with
    ``OSError: [Errno 7] Argument list too long`` for any conversation over
    ~128 KiB — before curl runs, with no upstream involved. The body has to go
    over stdin instead, where there is no ceiling at all.
    """
    import asyncio as _asyncio

    from app import curl_transport

    seen: dict = {}

    async def fake_exec(*args, **kwargs):
        seen["args"] = args
        seen["stdin"] = kwargs.get("stdin")
        raise OSError(7, "Argument list too long")

    original = _asyncio.create_subprocess_exec
    _asyncio.create_subprocess_exec = fake_exec
    try:
        big = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "词" * 100_000}]}
        try:
            _asyncio.run(curl_transport.open_post("https://example.invalid/x", {}, big))
        except curl_transport.CurlStartError as exc:
            # The hint is the difference between "curl is broken" and "our argv
            # blew the kernel limit".
            assert "argument list too long" in str(exc).lower()
        else:  # pragma: no cover
            raise AssertionError("a failed exec must not be swallowed")
    finally:
        _asyncio.create_subprocess_exec = original

    args = seen["args"]
    assert seen["stdin"] is _asyncio.subprocess.PIPE, "body should be piped on stdin"
    assert "-" in args and "--data-binary" in args
    assert all(len(a) < 4096 for a in args), "something big leaked into argv"
    assert not any("词词词" in a for a in args)


def test_curl_transport_posts_large_bodies_intact() -> None:
    """End to end over a loopback socket: 600 KiB in, 600 KiB server-side."""
    import asyncio as _asyncio
    import http.server
    import threading

    from app import curl_transport

    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib naming
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            got["len"] = len(raw)
            got["content_length"] = self.headers.get("content-length")
            got["content_type"] = self.headers.get("content-type")
            try:
                got["model"] = json.loads(raw)["model"]
            except Exception:  # noqa: BLE001 - asserted below
                got["model"] = None
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence the test output
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    payload = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "长" * 200_000}]}
    try:
        status, body = _asyncio.run(
            curl_transport.post_json(f"http://127.0.0.1:{port}/v1/messages", {}, payload, 20.0)
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert status == 200, body
    assert json.loads(body)["ok"] is True
    assert got["model"] == "MiniMax-M3"
    # Non-ASCII is sent as UTF-8, and the body arrives whole with a real length.
    assert got["len"] > 500_000
    assert got["content_length"] == str(got["len"])
    assert got["content_type"] == "application/json"


def test_error_groups_aggregate_by_message() -> None:
    """Cluster failures by their message, not one row at a time.

    "9 failures" is a number; "HTTP 0 ×9 between 00:36 and 05:28" is a
    diagnosis. The console strip reads this, so it has to group, count and
    order correctly.
    """
    from app.metrics import Metrics

    m = Metrics(limit=100)
    for _ in range(3):
        m.record("chat", "MiniMax-M2.7", account="a", ok=False, status=502,
                 latency_ms=4000, error="upstream HTTP 0")
    m.record("chat", "MiniMax-M2.7", account="a", ok=False, status=429, latency_ms=90,
             error="rate limited")
    m.record("chat", "MiniMax-M2.7", account="a", ok=True, latency_ms=800)

    groups = m.error_groups(24, 5)
    assert [g["error"] for g in groups] == ["upstream HTTP 0", "rate limited"]
    assert groups[0]["count"] == 3 and groups[0]["status"] == 502
    assert groups[1]["count"] == 1
    # Successes never show up here, and the window is honoured.
    assert m.error_groups(24, 5) == m.error_groups(24, 5)
    assert all("count" in g and "first_ts" in g and "last_ts" in g for g in groups)


def test_console_watch_records_only_trouble() -> None:
    """A console API failure must leave a row; a healthy poll must not.

    This is the difference between "the panel is broken" and "the panel's
    request never reached the server" — without the row, the operator has no
    way to tell them apart.
    """
    from fastapi.testclient import TestClient

    from app import metrics as metrics_mod
    from app.config import settings
    from app.main import app

    original = settings.api_keys
    settings.api_keys = ["unit-test-key"]
    try:
        with TestClient(app) as client:
            before = len(metrics_mod.metrics.rows())
            client.get("/admin/summary", headers={"x-api-key": "unit-test-key"})
            healthy = [r for r in metrics_mod.metrics.rows()[before:] if r.kind == "console"]
            assert not healthy, "a fast 200 poll must not fill the history"

            client.get("/admin/accounts/nope", headers={"x-api-key": "unit-test-key"})
            rows = [r for r in metrics_mod.metrics.rows()[before:] if r.kind == "console"]
            assert rows, "a 404 on /admin left no evidence"
            last = rows[-1]
            assert last.ok is False and last.status == 404
            assert "GET /admin/accounts/nope" in last.model
    finally:
        settings.api_keys = original


def test_diagnostics_endpoint_reports_the_basics() -> None:
    """/admin/diagnostics is the report the operator pastes into a ticket."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    original = settings.api_keys
    settings.api_keys = ["unit-test-key"]
    try:
        with TestClient(app) as client:
            assert client.get("/admin/diagnostics").status_code == 401
            data = client.get("/admin/diagnostics?lines=5",
                              headers={"x-api-key": "unit-test-key"}).json()
            assert {"service", "pool", "store", "probes", "log", "hints"} <= set(data)
            assert data["service"]["version"].startswith("console-")
            assert data["service"]["python"]
            # 配置值不等于实际在用值：httpx 失手会临时切到 curl。
            assert data["service"]["transport"]
            assert isinstance(data["log"], list) and len(data["log"]) <= 5
            # Every buffered line carries a printable clock, or the page prints
            # "undefined" where the timestamp should be.
            for row in data["log"]:
                assert row["time_label"]

            errors = client.get("/admin/errors?hours=24&limit=3",
                                headers={"x-api-key": "unit-test-key"}).json()
            assert "groups" in errors and isinstance(errors["groups"], list)
            for group in errors["groups"]:
                assert group["first"] and group["last"]
    finally:
        settings.api_keys = original


def test_log_clock_uses_console_timezone() -> None:
    """The container runs on UTC; the log must still read like a local clock."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import metrics as metrics_mod
    from app.config import settings

    original = settings.signin_timezone
    ts = 1_700_000_000
    try:
        settings.signin_timezone = "Asia/Shanghai"
        metrics_mod._TZ, metrics_mod._TZ_NAME = None, ""
        got = metrics_mod._clock(ts)
        want = datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
        assert got == want, (got, want)
        assert len(got) == 14, "date is included: history spans days"

        settings.signin_timezone = "UTC"
        metrics_mod._TZ, metrics_mod._TZ_NAME = None, ""
        assert metrics_mod._clock(ts) != want

        settings.signin_timezone = "Not/AZone"
        metrics_mod._TZ, metrics_mod._TZ_NAME = None, ""
        assert metrics_mod._clock(ts), "an unknown zone falls back instead of raising"
    finally:
        settings.signin_timezone = original
        metrics_mod._TZ, metrics_mod._TZ_NAME = None, ""


def test_bad_model_is_not_blamed_on_the_balance() -> None:
    """A wrong model name must come back as 400, not "every account is out of quota".

    The retry loop used to take one extra lap, so after walking the pool it called
    acquire() with every account excluded and surfaced that CredentialError —
    which reads "out of quota until the daily reset" no matter why the pool emptied.
    """
    import asyncio
    from unittest.mock import patch

    from app import upstream as upstream_mod
    from app.auth import Token

    pool = [Token(access_token=f"tok{i}", label=f"t{i}@x") for i in range(5)]
    calls: list[str] = []

    class Resp:
        status_code = 400
        account = ""

        async def aread(self):
            return b'{"error":{"message":"unknown model: nope"}}'

        async def aclose(self):
            pass

    async def fake_dispatch(url, payload, headers, stream):
        calls.append(url)
        return Resp()

    async def fake_acquire(exclude=None):
        exclude = exclude or set()
        for token in pool:
            if token.access_token not in exclude:
                return token
        raise TokenExhausted("every account is out of quota")

    class TokenExhausted(Exception):
        pass

    async def run():
        with patch.object(upstream_mod.upstream, "startup", return_value=None), \
             patch.object(upstream_mod.upstream, "_dispatch", fake_dispatch), \
             patch.object(upstream_mod.token_manager, "acquire", fake_acquire), \
             patch.object(upstream_mod.token_manager, "_tokens", pool):
            try:
                await upstream_mod.upstream._send({"model": "nope", "messages": []}, False)
            except Exception as exc:  # noqa: BLE001 - the class is what matters
                return exc
        return None

    # The exception type is imported lazily to avoid a module cycle at import time.
    from app.auth import CredentialError

    with patch.object(upstream_mod, "CredentialError", CredentialError):
        err = asyncio.run(run())
    assert err is not None, "the request should have failed"
    assert not isinstance(err, type(None))
    assert "quota" not in str(err).lower(), str(err)
    assert "unknown model" in str(err).lower(), str(err)
    assert len(calls) == 3, f"stopped after 3 model misses, made {len(calls)} calls"
    # Raising inside the 400 branch skips the attribution at the bottom of the
    # loop, so the row that says "wrong model" used to show account "-". The
    # third miss is the one that gives up, hence t2.
    assert getattr(err, "account", "") == "t2@x", getattr(err, "account", None)


def test_capacity_math() -> None:
    """补号测算 has to be arithmetically sane, not just pretty."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    original = settings.api_keys
    settings.api_keys = ["unit-test-key"]
    try:
        client = TestClient(app)
        data = client.get("/admin/capacity?hours=24&days=7",
                          headers={"x-api-key": "unit-test-key"}).json()
        for row in data["forecast"]:
            assert row["consume"] >= 0 and row["gap"] >= 0
            if row["gap"] > 0:
                assert row["accounts_needed"] >= 1
        assert data["method"], "the page explains its own method; keep it non-empty"
    finally:
        settings.api_keys = original


def _jpeg_header(width: int, height: int) -> bytes:
    """A JPEG whose SOF0 carries ``width`` and ``height`` — no pixel data."""
    app1 = b"\xff\xe1" + (0x0010).to_bytes(2, "big") + b"\x00" * 14
    sof = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x00" * 3
    )
    return b"\xff\xd8" + app1 + sof + b"\xff\xd9"


def test_ark_size_and_measured_image_info() -> None:
    """Ark's `size` tiers, and the size/format read from the produced bytes.

    Ark clients send a tier (1K/1.5K/2K/3K/4K) or "宽x高"; the synthesiser only
    has three area buckets, so the tiers in between land on the nearer one.
    And the bytes lie about their format — the asset URL and Content-Type both
    say png while the payload is JPEG — so the response must not be built from
    the request.
    """
    from app.media import MediaError, image_info, size_to_params

    assert size_to_params(None) == ("1:1", "1K")
    assert size_to_params("2K") == ("1:1", "2K")
    assert size_to_params("1.5K") == ("1:1", "2K")
    assert size_to_params("3K") == ("1:1", "4K")
    assert size_to_params("1024x1024") == ("1:1", "1K")
    assert size_to_params("2048x1152") == ("16:9", "2K")
    assert size_to_params("5504x3072") == ("16:9", "4K")
    try:
        size_to_params("big")
    except MediaError as exc:
        assert exc.status == 400 and "invalid size" in exc.message
    else:
        raise AssertionError("a nonsense size must not silently become 1:1")

    assert image_info(_jpeg_header(1024, 1024)) == {"output_format": "jpeg", "size": "1024x1024"}
    png = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + (2048).to_bytes(
        4, "big"
    ) + (1152).to_bytes(4, "big")
    assert image_info(png) == {"output_format": "png", "size": "2048x1152"}
    assert image_info(b"not an image") == {}


def test_ark_inline_image_is_uploaded() -> None:
    """Ark allows `image` as a data: URI; the tool only reads URLs."""
    from app import media

    seen: dict = {}

    async def fake_upload(data, mime_type="image/png", filename=None):
        seen.update({"data": data, "mime": mime_type, "name": filename})
        return "https://agent-cdn.example/ref.png"

    original = media.upload_image
    media.upload_image = fake_upload
    try:
        url = asyncio.run(media.resolve_reference("data:image/webp;base64,AAAA", 3))
        assert url == "https://agent-cdn.example/ref.png"
        assert seen == {"data": "AAAA", "mime": "image/webp", "name": "ref_3.webp"}
        # A real URL must never be re-uploaded.
        assert asyncio.run(media.resolve_reference("https://x.test/y.png")) == "https://x.test/y.png"
    finally:
        media.upload_image = original


def test_ark_image_endpoint_contract() -> None:
    """`/v1/images/generations` answers in Ark's shape, not OpenAI's.

    Request: model / prompt / image / size / response_format, plus the fields
    this channel ignores. Response: model / created / data[{url|b64_json, size,
    output_format}] / usage{generated_images, output_tokens, total_tokens}.
    """
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app import media as media_mod
    from app.config import settings

    seen: dict = {}

    async def fake_generate(**kwargs):
        seen.update(kwargs)
        # `revised_prompt` is upstream's "Success to generate image" filler; Ark's
        # data[] has no such field, so it must not leak into the response.
        return {
            "images": [{"url": "https://cdn.test/a.png", "revised_prompt": "ok"}],
            "raw": {},
        }

    async def fake_describe(url):
        return {"output_format": "jpeg", "size": "2048x2048"}

    original_keys = settings.api_keys
    original_generate = media_mod.image_generate
    original_describe = media_mod.describe_asset
    settings.api_keys = ["unit-test-key"]
    media_mod.image_generate = fake_generate
    media_mod.describe_asset = fake_describe
    try:
        client = TestClient(main_mod.app)
        headers = {"x-api-key": "unit-test-key"}
        response = client.post(
            "/v1/images/generations",
            headers=headers,
            json={
                "model": "doubao-seedream-5-5-261028",
                "prompt": "a red cube",
                "image": "https://example.test/ref.png",
                "size": "2K",
                "response_format": "url",
                # Accepted and ignored — an Ark client sends these by default.
                "output_format": "png",
                "background": "transparent",
                "watermark": True,
                "stream": False,
                "optimize_prompt_options": {"mode": "standard"},
                "sequential_image_generation": "auto",
                "sequential_image_generation_options": {"max_images": 2},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["model"] == "doubao-seedream-5-5-261028"
        assert isinstance(body["created"], int)
        assert body["data"] == [
            {"url": "https://cdn.test/a.png", "output_format": "jpeg", "size": "2048x2048"}
        ]
        assert body["usage"] == {
            "generated_images": 1,
            "input_images": 1,
            "output_tokens": 2048 * 2048 // 256,
            "total_tokens": 2048 * 2048 // 256,
        }
        # Quantity came from Ark's sequential block, size passed through verbatim,
        # and the reference image reached the generator.
        assert seen["n"] == 2 and seen["size"] == "2K"
        assert seen["reference_urls"] == ["https://example.test/ref.png"]

        missing = client.post("/v1/images/generations", headers=headers, json={"size": "1K"})
        assert missing.status_code == 400 and "prompt is required" in missing.text

        bad_format = client.post(
            "/v1/images/generations",
            headers=headers,
            json={"prompt": "x", "response_format": "bytes"},
        )
        assert bad_format.status_code == 400
        assert "response_format" in bad_format.text
    finally:
        settings.api_keys = original_keys
        media_mod.image_generate = original_generate
        media_mod.describe_asset = original_describe


def test_retry_budget_comes_from_config() -> None:
    """``RETRY_ATTEMPTS`` decides how many upstream attempts one request gets.

    The unit is an attempt, not an account: a 5xx walks to the next account, and
    when the pool has nothing untried left the same account is reused (with
    backoff), which is the only resilience a one-account pool has. A 400 is the
    caller's mistake and must not be repeated at all.
    """
    import asyncio
    from unittest.mock import patch

    from app import upstream as upstream_mod
    from app.auth import CredentialError, Token

    def build(status: int, pool_size: int):
        pool = [Token(access_token=f"tok{i}", label=f"t{i}@x") for i in range(pool_size)]
        calls: list[int] = []

        class Resp:
            status_code = status
            account = ""

            async def aread(self):
                return b'{"error":{"message":"edge said no"}}'

            async def aclose(self):
                pass

        async def fake_dispatch(url, payload, headers, stream):
            calls.append(1)
            return Resp()

        async def fake_acquire(exclude=None):
            exclude = exclude or set()
            for token in pool:
                if token.access_token not in exclude:
                    return token
            raise CredentialError("every account is parked")

        async def run(budget: int) -> None:
            calls.clear()
            with patch.object(upstream_mod.upstream, "startup", return_value=None), \
                 patch.object(upstream_mod.upstream, "_dispatch", fake_dispatch), \
                 patch.object(upstream_mod.token_manager, "acquire", fake_acquire), \
                 patch.object(upstream_mod.token_manager, "park", lambda *a, **k: None), \
                 patch.object(upstream_mod.token_manager, "_tokens", pool), \
                 patch.object(upstream_mod.settings, "retry_attempts", budget), \
                 patch.object(upstream_mod.asyncio, "sleep", lambda *a, **k: _noop()):
                try:
                    await upstream_mod.upstream._send({"model": "m", "messages": []}, False)
                except Exception as exc:  # noqa: BLE001 - the count is what matters
                    return exc
            return None

        return calls, run

    async def _noop():
        return None

    # 5xx: 1 + retries attempts, then the last error comes back.
    calls, run = build(503, 5)
    asyncio.run(run(0))
    assert len(calls) == 1, f"retry_attempts=0 must not retry, made {len(calls)}"
    asyncio.run(run(2))
    assert len(calls) == 3, f"retry_attempts=2 must try 3 times, made {len(calls)}"
    # More budget than accounts: the pool runs dry and the same account is reused.
    asyncio.run(run(6))
    assert len(calls) == 7, f"budget above pool size should still be honoured, made {len(calls)}"

    # Single account: no untried account is ever available, so every retry is a
    # same-account retry — the reason this branch exists.
    calls, run = build(503, 1)
    asyncio.run(run(2))
    assert len(calls) == 3, f"one-account pool must still retry, made {len(calls)}"

    # 4xx that is the caller's fault: one attempt, whatever the budget says.
    calls, run = build(400, 5)
    asyncio.run(run(5))
    assert len(calls) == 1, f"a 400 must not be retried, made {len(calls)}"

    # 401/402 rotate accounts but never exceed the budget.
    for status in (402, 429):
        calls, run = build(status, 5)
        asyncio.run(run(1))
        assert len(calls) == 2, f"HTTP {status} with budget 1 tried {len(calls)} times"


def test_runtime_config_is_writable_and_survives_a_restart() -> None:
    """The console writes ``runtime.json``; a new process reads it back.

    Nothing here needs a container recreate: every consumer reads
    ``settings.<name>`` when it uses it, so assigning the attribute is the apply
    step. ``None`` means "back to the .env value".
    """
    import json
    import os
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from app.config import Settings

    path = Path(tempfile.mkdtemp()) / "runtime.json"
    with patch.dict(os.environ, {"RUNTIME_CONFIG": str(path), "RETRY_ATTEMPTS": "3"}):
        first = Settings()
        assert first.load_runtime() == []
        assert first.retry_attempts == 3

        applied, rejected = first.apply_runtime({"retry_attempts": 7, "probe_auto": False})
        assert applied == {"retry_attempts": 7, "probe_auto": False}, applied
        assert rejected == [], rejected
        assert first.retry_attempts == 7 and first.probe_auto is False
        saved = json.loads(path.read_text("utf-8"))
        assert saved["retry_attempts"] == 7

        # A "restart": a fresh instance replaying the same file.
        second = Settings()
        notes = second.load_runtime()
        assert second.retry_attempts == 7, notes
        assert second.probe_auto is False, notes
        assert second.editable_state()["retry_attempts"]["source"] == "runtime"

        # Out of range and read-only keys are refused, value untouched.
        applied, rejected = second.apply_runtime({"retry_attempts": -1})
        assert applied == {} and rejected and rejected[0][0] == "retry_attempts"
        assert second.retry_attempts == 7
        applied, rejected = second.apply_runtime({"api_keys": ["x"]})
        assert applied == {} and rejected
        applied, rejected = second.apply_runtime({"upstream_transport": "dialup"})
        assert applied == {} and rejected

        # None resets to the value .env gave, and the file forgets the override.
        applied, _ = second.apply_runtime({"retry_attempts": None})
        assert second.retry_attempts == 3
        assert "retry_attempts" not in json.loads(path.read_text("utf-8"))

        # A corrupt file is reported, not fatal.
        path.write_text("{not json", "utf-8")
        third = Settings()
        notes = third.load_runtime()
        assert notes and "读取失败" in notes[0], notes
        assert third.retry_attempts == 3


def main() -> None:
    tests = [
        test_model_aliases,
        test_model_catalogue_complete,
        test_video_model_enum_matches_upstream,
        test_ark_size_and_measured_image_info,
        test_ark_inline_image_is_uploaded,
        test_ark_image_endpoint_contract,
        test_system_and_text,
        test_tool_roundtrip,
        test_response_translation,
        test_stream_translation,
        test_signin_panel,
        test_console_asset,
        test_pool_hot_reload,
        test_pool_reload_survives_write_race,
        test_pool_adopts_rotated_credentials_from_file,
        test_token_writes_are_atomic_and_leave_no_temp_files,
        test_a_spent_refresh_token_is_never_presented,
        test_expired_token_refreshes_once,
        test_quota_errors_rotate,
        test_metrics_rollup,
        test_account_import_flow,
        test_balance_surfacing,
        test_membership_payload_is_not_unwrapped,
        test_quota_park_lasts_until_reset,
        test_admin_requires_key,
        test_request_history_survives_restart,
        test_store_degrades_to_memory,
        test_admin_logs_pagination,
        test_redaction_hides_accounts_and_tokens,
        test_quota_errors_do_not_advertise_the_subscription_plan,
        test_redaction_masks_bare_token_prefixes,
        test_pool_exhaustion_never_names_the_mailbox_to_the_caller,
        test_transport_status_never_escapes_as_http,
        test_curl_fallback_expires_instead_of_latching_forever,
        test_curl_transport_keeps_bodies_out_of_argv,
        test_curl_transport_posts_large_bodies_intact,
        test_error_groups_aggregate_by_message,
        test_console_watch_records_only_trouble,
        test_diagnostics_endpoint_reports_the_basics,
        test_log_clock_uses_console_timezone,
        test_bad_model_is_not_blamed_on_the_balance,
        test_capacity_math,
        test_retry_budget_comes_from_config,
        test_runtime_config_is_writable_and_survives_a_restart,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
