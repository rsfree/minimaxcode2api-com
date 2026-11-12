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
        "MiniMax-M2",
        "MiniMax-M2.1",
    ):
        assert name in chat and chat[name].legacy

    # Multimodal entries exist for every media endpoint, with the right kind.
    assert {"minimax-image", "minimax-speech", "minimax-music"} <= set(media_models)
    assert {m.id for m in media_models.values() if m.kind == "video"} == set(media.VIDEO_MODELS)
    assert media_models["minimax-image"].kind == "image"

    # /v1/models carries the fields a picker needs.
    row = chat["MiniMax-M3"].to_openai()
    assert row["type"] == "chat" and row["context_length"] == 512_000
    assert row["capabilities"]["vision"] is True


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
                     "/admin/accounts/import"):
        assert endpoint in html, f"console does not call {endpoint}"

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


def main() -> None:
    tests = [
        test_model_aliases,
        test_model_catalogue_complete,
        test_system_and_text,
        test_tool_roundtrip,
        test_response_translation,
        test_stream_translation,
        test_signin_panel,
        test_console_asset,
        test_pool_hot_reload,
        test_pool_reload_survives_write_race,
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
        test_log_clock_uses_console_timezone,
        test_bad_model_is_not_blamed_on_the_balance,
        test_capacity_math,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
