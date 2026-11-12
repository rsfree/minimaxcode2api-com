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
    for view in ("chat", "image", "speech", "accounts", "models"):
        assert f'data-view="{view}"' in html, f"nav missing {view}"
        assert f'id="view-{view}"' in html, f"section missing {view}"

    # The console talks to the same endpoints the rest of the gateway exposes.
    for endpoint in ("/v1/chat/completions", "/v1/images/generations", "/v1/audio/speech", "/healthz"):
        assert endpoint in html, f"console does not call {endpoint}"


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
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
