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
    ids = {model.id for model in visible_models()}
    assert "MiniMax-M3" in ids and "MiniMax-M2" not in ids


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


def main() -> None:
    tests = [
        test_model_aliases,
        test_system_and_text,
        test_tool_roundtrip,
        test_response_translation,
        test_stream_translation,
        test_signin_panel,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
