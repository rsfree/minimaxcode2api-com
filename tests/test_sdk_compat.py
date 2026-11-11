"""Client-SDK compatibility check against a running gateway.

Not part of CI (needs a live credential). Run it on the box where the gateway
listens:

    GATEWAY=http://127.0.0.1:8080 GATEWAY_KEY=sk-local python tests/test_sdk_compat.py
"""

from __future__ import annotations

import os
import sys

from anthropic import Anthropic
from openai import OpenAI

GATEWAY = os.environ.get("GATEWAY", "http://127.0.0.1:8080")
KEY = os.environ.get("GATEWAY_KEY", "") or "not-needed"
MODEL = os.environ.get("MODEL", "MiniMax-M3.1-Flash")

openai_client = OpenAI(base_url=f"{GATEWAY}/v1", api_key=KEY, timeout=180.0)
anthropic_client = Anthropic(base_url=GATEWAY, api_key=KEY, timeout=180.0)


def check_openai_blocking() -> None:
    response = openai_client.chat.completions.create(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
    )
    text = response.choices[0].message.content or ""
    assert response.choices[0].finish_reason, response
    print(f"openai blocking  -> {text.strip()[:40]!r} usage={response.usage.total_tokens}")


def check_openai_stream() -> None:
    stream = openai_client.chat.completions.create(
        model=MODEL,
        max_tokens=64,
        stream=True,
        messages=[{"role": "user", "content": "Count from 1 to 3."}],
    )
    pieces: list[str] = []
    finish = None
    for chunk in stream:
        choice = chunk.choices[0] if chunk.choices else None
        if choice is None:
            continue
        if choice.delta.content:
            pieces.append(choice.delta.content)
        if choice.finish_reason:
            finish = choice.finish_reason
    assert pieces, "no content chunks received"
    assert finish, "no finish_reason received"
    print(f"openai streaming -> {''.join(pieces)[:40]!r} finish={finish}")


def check_openai_tools() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Look up the weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    response = openai_client.chat.completions.create(
        model=MODEL,
        max_tokens=256,
        tools=tools,
        messages=[{"role": "user", "content": "What is the weather in Shenzhen? Use the tool."}],
    )
    calls = response.choices[0].message.tool_calls or []
    assert calls, response.choices[0].message
    call = calls[0]
    print(
        "openai tools     -> "
        f"{call.function.name}({call.function.arguments}) finish={response.choices[0].finish_reason}"
    )


def check_anthropic() -> None:
    message = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
    )
    text = "".join(block.text for block in message.content if block.type == "text")
    assert text, message
    print(f"anthropic native -> {text.strip()[:40]!r} stop={message.stop_reason}")


def check_anthropic_stream() -> None:
    collected: list[str] = []
    with anthropic_client.messages.stream(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "Count from 1 to 3."}],
    ) as stream:
        for text in stream.text_stream:
            collected.append(text)
    assert collected, "no text deltas"
    print(f"anthropic stream -> {''.join(collected)[:40]!r}")


def main() -> None:
    checks = [
        check_openai_blocking,
        check_openai_stream,
        check_openai_tools,
        check_anthropic,
        check_anthropic_stream,
    ]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
