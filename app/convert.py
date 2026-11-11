"""OpenAI Chat Completions <-> Anthropic Messages translation.

The managed channel speaks Anthropic Messages (``anthropic-version`` header,
``content`` blocks, SSE ``message_start``/``content_block_delta`` events). Most
clients speak OpenAI, so everything is translated here and nowhere else.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator, Iterable

from .catalog import DEFAULT_MODEL, get_model, resolve_model
from .config import settings

FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class ConversionError(ValueError):
    """Client payload cannot be expressed on the upstream wire format."""


# ---------------------------------------------------------------------------
# OpenAI -> Anthropic
# ---------------------------------------------------------------------------
def _content_blocks(content: Any) -> list[dict]:
    """Normalise an OpenAI message ``content`` value into Anthropic blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: list[dict] = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in {"text", "input_text"}:
            text = part.get("text") or ""
            if text:
                blocks.append({"type": "text", "text": text})
        elif kind == "image_url":
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url:
                continue
            if url.startswith("data:"):
                header, _, payload = url.partition(",")
                media_type = header[5:].split(";")[0] or "image/png"
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": payload},
                    }
                )
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        elif kind in {"input_audio", "audio"}:
            raise ConversionError("audio input is not supported by this channel")
    return blocks


def _tool_result_blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, list):
        inner = _content_blocks(content)
        blocks: list[dict] = inner or [{"type": "text", "text": ""}]
    else:
        blocks = [{"type": "text", "text": content or ""}]
    return [
        {
            "type": "tool_result",
            "tool_use_id": message.get("tool_call_id") or "",
            "content": blocks,
        }
    ]


def _assistant_blocks(message: dict) -> list[dict]:
    blocks = _content_blocks(message.get("content"))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        raw_args = function.get("arguments")
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed = {"_raw": raw_args}
        elif isinstance(raw_args, dict):
            parsed = raw_args
        else:
            parsed = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": function.get("name") or "unknown",
                "input": parsed,
            }
        )
    return blocks


def _system_text(messages: Iterable[dict]) -> list[dict]:
    blocks: list[dict] = []
    for message in messages:
        if message.get("role") in {"system", "developer"}:
            blocks.extend(_content_blocks(message.get("content")))
    return blocks


def _convert_tools(tools: Any) -> list[dict]:
    converted: list[dict] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict) or not function.get("name"):
            continue
        converted.append(
            {
                "name": function["name"],
                "description": function.get("description") or "",
                "input_schema": function.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return converted


def _convert_tool_choice(choice: Any) -> dict | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        return {"auto": {"type": "auto"}, "none": {"type": "none"}, "required": {"type": "any"}}.get(
            choice
        )
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            name = (choice.get("function") or {}).get("name")
            if name:
                return {"type": "tool", "name": name}
        if choice.get("type") in {"auto", "any", "none"}:
            return {"type": choice["type"]}
    return None


def build_anthropic_request(body: dict) -> dict:
    """Translate an OpenAI chat-completions body into an Anthropic body."""
    model = resolve_model(body.get("model"))
    catalog_model = get_model(model)

    messages: list[dict] = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in {"system", "developer"}:
            continue
        if role == "tool":
            blocks = _tool_result_blocks(message)
            target_role = "user"
        elif role == "assistant":
            blocks = _assistant_blocks(message)
            target_role = "assistant"
        else:
            blocks = _content_blocks(message.get("content"))
            target_role = "user"
        if not blocks:
            continue
        # Anthropic expects alternating turns; merge repeats in place.
        if messages and messages[-1]["role"] == target_role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": target_role, "content": blocks})

    if not messages:
        raise ConversionError("messages must contain at least one user message")

    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens") or settings.default_max_tokens
    if catalog_model:
        max_tokens = min(int(max_tokens), catalog_model.max_output)

    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(max_tokens),
    }

    system = _system_text(body.get("messages") or [])
    if system:
        request["system"] = system

    if body.get("temperature") is not None:
        request["temperature"] = float(body["temperature"])
    if body.get("top_p") is not None:
        request["top_p"] = float(body["top_p"])
    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        request["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        request["stop_sequences"] = [str(item) for item in stop]
    if body.get("stream"):
        request["stream"] = True

    tools = _convert_tools(body.get("tools"))
    if tools:
        request["tools"] = tools
        choice = _convert_tool_choice(body.get("tool_choice"))
        if choice:
            request["tool_choice"] = choice
        if body.get("parallel_tool_calls") is False:
            request["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        request["thinking"] = thinking
    else:
        effort = body.get("reasoning_effort")
        if effort in {"low", "medium", "high"}:
            request["thinking"] = {"type": "enabled"}
        elif effort == "none":
            request["thinking"] = {"type": "disabled"}

    if body.get("metadata") and isinstance(body["metadata"], dict):
        user_id = body["metadata"].get("user_id")
        if user_id:
            request["metadata"] = {"user_id": str(user_id)}

    return request


# ---------------------------------------------------------------------------
# Anthropic -> OpenAI
# ---------------------------------------------------------------------------
def _usage(payload: dict) -> dict:
    usage = payload.get("usage") or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    cached = int(usage.get("cache_read_input_tokens") or 0)
    details = {"cached_tokens": cached} if cached else {}
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        **({"prompt_tokens_details": details} if details else {}),
    }


def build_openai_response(payload: dict, requested_model: str) -> dict:
    texts: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict] = []
    for block in payload.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            texts.append(block.get("text") or "")
        elif kind in {"thinking", "reasoning"}:
            reasoning.append(block.get("thinking") or block.get("text") or "")
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "unknown",
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )

    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish = FINISH_REASONS.get(payload.get("stop_reason") or "end_turn", "stop")
    if tool_calls and finish == "stop":
        finish = "tool_calls"

    return {
        "id": payload.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model or payload.get("model") or DEFAULT_MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
        "usage": _usage(payload),
    }


# ---------------------------------------------------------------------------
# SSE translation
# ---------------------------------------------------------------------------
async def iter_sse_events(lines: AsyncIterator[bytes] | AsyncIterator[str]) -> AsyncIterator[tuple[str, dict]]:
    """Yield ``(event, data)`` pairs from an Anthropic SSE stream."""
    event = "message"
    buffer: list[str] = []
    async for raw in lines:
        line = (raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw).rstrip("\r\n")
        if line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            buffer.append(line[5:].lstrip())
        elif line == "":
            if buffer:
                payload = "\n".join(buffer)
                buffer.clear()
                if payload != "[DONE]":
                    try:
                        yield event, json.loads(payload)
                    except json.JSONDecodeError:
                        continue
            event = "message"
    if buffer:
        payload = "\n".join(buffer)
        try:
            yield event, json.loads(payload)
        except json.JSONDecodeError:
            pass


def _chunk(model: str, chunk_id: str, created: int, delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
    choice: dict[str, Any] = {"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}
    body: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


async def stream_openai(
    events: AsyncIterator[tuple[str, dict]],
    requested_model: str,
) -> AsyncIterator[str]:
    """Translate Anthropic SSE events into OpenAI ``chat.completion.chunk``."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    model = requested_model or DEFAULT_MODEL
    started = False
    usage: dict = {}
    finish: str | None = None
    tool_index: dict[int, int] = {}
    next_tool = 0

    def ensure_start() -> str:
        return _chunk(model, chunk_id, created, {"role": "assistant", "content": ""})

    async for event, data in events:
        kind = data.get("type") or event

        if kind == "message_start":
            message = data.get("message") or {}
            model = message.get("model") or model
            usage = _usage(message)
            if not started:
                started = True
                yield ensure_start()

        elif kind == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                index = next_tool
                tool_index[int(data.get("index", 0))] = index
                next_tool += 1
                if not started:
                    started = True
                    yield ensure_start()
                yield _chunk(
                    model,
                    chunk_id,
                    created,
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                                "type": "function",
                                "function": {"name": block.get("name") or "unknown", "arguments": ""},
                            }
                        ]
                    },
                )

        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta" and delta.get("text"):
                if not started:
                    started = True
                    yield ensure_start()
                yield _chunk(model, chunk_id, created, {"content": delta["text"]})
            elif dtype == "thinking_delta" and delta.get("thinking"):
                if not started:
                    started = True
                    yield ensure_start()
                yield _chunk(model, chunk_id, created, {"reasoning_content": delta["thinking"]})
            elif dtype == "input_json_delta":
                index = tool_index.get(int(data.get("index", 0)), 0)
                yield _chunk(
                    model,
                    chunk_id,
                    created,
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "function": {"arguments": delta.get("partial_json") or ""},
                            }
                        ]
                    },
                )

        elif kind == "message_delta":
            delta = data.get("delta") or {}
            if delta.get("stop_reason"):
                finish = FINISH_REASONS.get(delta["stop_reason"], "stop")
            if (data.get("usage") or {}).get("output_tokens") is not None:
                usage = {**usage, **_usage({"usage": {**usage, **data["usage"]}})}

        elif kind == "message_stop":
            break

        elif kind == "error":
            error = data.get("error") or {}
            payload = {
                "error": {
                    "message": error.get("message") or "upstream error",
                    "type": error.get("type") or "upstream_error",
                    "code": error.get("code"),
                }
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return

    if not started:
        yield ensure_start()
    if finish is None:
        finish = "tool_calls" if next_tool else "stop"
    yield _chunk(model, chunk_id, created, {}, finish=finish, usage=usage or None)
    yield "data: [DONE]\n\n"
