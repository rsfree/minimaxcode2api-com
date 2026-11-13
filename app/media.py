"""Multimodal tools on the managed desktop channel.

The desktop client exposes its image / video / speech / music capabilities as
MCP tools behind a desktop gateway proxy, not on the LLM path:

    POST {mcp_url}/image_synthesize
    POST {mcp_url}/submit_video_generation
    POST {mcp_url}/query_video_generation
    POST {mcp_url}/synthesize_speech
    POST {mcp_url}/batch_text_to_music
    POST {mcp_url}/get_voice_list

Two things differ from the Messages endpoint and are easy to get wrong:

* **Business codes ride inside a 200.** The envelope is
  ``{"code": 0, "message": "ok", ...}``. ``code=402`` means the account balance
  is exhausted, ``code=503`` on a video query means the task finished but the
  server could not prepare the file for delivery. Both must surface as errors,
  and the 402 text is worth relaying verbatim: it is the only signal a caller
  gets that topping up is required.
* **File paths are client-side.** Every tool returns an OSS/agent-CDN URL; the
  desktop app downloads it into the workspace. Here the URL *is* the result, so
  media endpoints hand it back instead of writing a file.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from .auth import token_manager
from .metrics import metrics
from .upstream import UpstreamError, upstream

log = logging.getLogger("minimax.media")

# Generation can run for minutes: the client allows 10 min for image/video/TTS
# and the MCP proxy itself caps the request there too.
IMAGE_TIMEOUT = 300.0
UPLOAD_TIMEOUT = 120.0
VIDEO_SUBMIT_TIMEOUT = 120.0
VIDEO_QUERY_TIMEOUT = 90.0
SPEECH_TIMEOUT = 300.0
MUSIC_TIMEOUT = 600.0
VOICES_TIMEOUT = 60.0

# The upstream tool's `model` enum, verbatim:
#   MiniMax-H3        4–15s, 768P or 2K        — best quality, paid
#   MiniMax-H3-Max    5–15s, 480P or 768P      — fast variant (~20s), paid
#   MiniMax-Hailuo-2.3 6 or 10s, 768P (1080P only at 6s) — the free-ish default
# H3 / H3-Max bill against the M Plan weekly limit and answer 402 with a long
# explanation when that is spent; Hailuo is what the bundled accounts can run.
VIDEO_MODELS = ("MiniMax-H3", "MiniMax-H3-Max", "MiniMax-Hailuo-2.3")
DEFAULT_VIDEO_MODEL = "MiniMax-Hailuo-2.3"

# Aspect ratios the image synthesiser accepts, as width:height prime pairs.
_ASPECT_PRESETS = ((21, 9), (16, 9), (3, 2), (4, 3), (1, 1), (4, 5), (3, 4), (2, 3), (9, 16))


class MediaError(RuntimeError):
    """An upstream tool failure with an HTTP status already mapped."""

    def __init__(
        self,
        status: int,
        message: str,
        code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.payload = payload

    @property
    def status_field(self) -> str | None:
        """The upstream task status, when the failure carried one."""
        if isinstance(self.payload, dict):
            value = self.payload.get("status")
            if isinstance(value, str):
                return value
        return None


def _status_for(code: int) -> int:
    """Map an upstream business code onto an HTTP status for our caller."""
    if code in {400, 401, 402, 403, 404, 429, 503}:
        return code
    return 502


async def call(tool: str, payload: dict, timeout: float, account: str | None = None) -> dict:
    """Invoke one MCP tool and unwrap the business-code envelope."""
    data, _ = await call_ex(tool, payload, timeout, account)
    return data


def _account_label(served: str) -> str:
    """Map the token that served a call back to its account label."""
    token = token_manager.find(served)
    return (token.label or token.short) if token else "-"


async def call_ex(
    tool: str, payload: dict, timeout: float, account: str | None = None
) -> tuple[dict, str]:
    """Same as :func:`call`, but also returns the account that served it."""
    started = time.time()
    try:
        data, served = await upstream.mcp_ex(tool, payload, timeout=timeout, account=account)
    except UpstreamError as exc:
        metrics.record(
            f"mcp:{tool}", tool,
            account=account or getattr(exc, "account", "") or "-",
            ok=False, status=exc.status,
            latency_ms=(time.time() - started) * 1000, error=exc.message,
        )
        raise MediaError(exc.status, exc.message, payload=exc.payload) from exc

    label = _account_label(served)
    code = data.get("code", 0)
    if isinstance(code, int) and code != 0:
        message = data.get("message") or f"upstream tool {tool} failed (code {code})"
        # A business code on an HTTP 200 is still a failed call; the console
        # would otherwise show a 100% success rate while generation never works.
        metrics.record(
            f"mcp:{tool}", tool, account=label, ok=False, status=_status_for(code),
            latency_ms=(time.time() - started) * 1000, error=str(message),
        )
        raise MediaError(_status_for(code), str(message), code=code, payload=data)

    metrics.record(
        f"mcp:{tool}", tool, account=label, ok=True, status=200,
        latency_ms=(time.time() - started) * 1000,
    )
    return data, served


# ---------------------------------------------------------------------------
# image
# ---------------------------------------------------------------------------
# Ark's `size` speaks in tiers: 1K / 1.5K / 2K / 3K / 4K per model family. The
# synthesiser only has three buckets, picked by total area, so the tiers in
# between land on the nearer one instead of being rejected.
_SIZE_TIERS = {
    "1k": "1K",
    "1.5k": "2K",
    "2k": "2K",
    "3k": "4K",
    "4k": "4K",
}
_AREA_1K = 1024 * 1024
_AREA_2K = 2048 * 2048


def size_to_params(size: str | None) -> tuple[str, str]:
    """Translate Ark's ``size`` (tier or ``WxH``) into (aspect_ratio, resolution).

    Tiers fix the resolution and leave the ratio alone; ``WxH`` snaps to the
    nearest supported ratio and the nearest area bucket, which is as close as
    this channel can get — the returned pixels are whatever the tool produces,
    and the response reports that truthfully rather than echoing the request.
    """
    if not size:
        return "1:1", "1K"
    text = str(size).strip().lower().replace(" ", "")
    if not text or text == "auto":
        return "1:1", "1K"
    if text in _SIZE_TIERS:
        return "1:1", _SIZE_TIERS[text]

    try:
        width_text, height_text = text.split("x", 1)
        width, height = int(width_text), int(height_text)
        if width <= 0 or height <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        raise MediaError(
            400, f"invalid size {size!r}: use a tier (1K/2K/4K) or 'WxH'", code=400
        ) from None

    ratio = width / height
    best = min(_ASPECT_PRESETS, key=lambda pair: abs(math.log(ratio) - math.log(pair[0] / pair[1])))
    area = width * height
    if area <= 2 * _AREA_1K:
        resolution = "1K"
    elif area <= 2 * _AREA_2K:
        resolution = "2K"
    else:
        resolution = "4K"
    return f"{best[0]}:{best[1]}", resolution


def _jpeg_size(raw: bytes) -> tuple[int, int] | None:
    index = 2
    length = len(raw)
    while index + 9 < length:
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xDA:  # start of scan: no more headers after this
            return None
        segment = int.from_bytes(raw[index + 2:index + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(raw[index + 5:index + 7], "big")
            width = int.from_bytes(raw[index + 7:index + 9], "big")
            return width, height
        if segment < 2:
            return None
        index += 2 + segment
    return None


def image_info(raw: bytes) -> dict[str, Any]:
    """Format and pixel size read from the bytes themselves.

    The asset URL ends in ``.png`` and the CDN answers ``image/png``, but the
    payload is JPEG with an AIGC watermark in Exif. The bytes are the only
    trustworthy source, and Ark's response carries the real format and size, so
    they are read from here rather than guessed from the request.
    """
    info: dict[str, Any] = {}
    if len(raw) >= 4 and raw[:3] == b"\xff\xd8\xff":
        info["output_format"] = "jpeg"
        size = _jpeg_size(raw)
    elif len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        info["output_format"] = "png"
        size = (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big"))
    elif len(raw) >= 30 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        info["output_format"] = "webp"
        size = None
        if raw[12:16] == b"VP8X":
            size = (
                int.from_bytes(raw[24:27], "little") + 1,
                int.from_bytes(raw[27:30], "little") + 1,
            )
    else:
        return {}
    if size and size[0] and size[1]:
        info["size"] = f"{size[0]}x{size[1]}"
    return info


async def describe_asset(url: str, timeout: float = 20.0) -> dict[str, Any]:
    """Read only the head of a generated asset to learn its format and pixels.

    A ranged GET keeps this at a few kilobytes instead of the 8 MB a 4K image
    weighs; the CDN honours the range, and when it does not the stream is
    abandoned as soon as the header is parsed.
    """
    await upstream.startup()
    try:
        async with upstream.client.stream(
            "GET",
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"range": "bytes=0-131071"},
        ) as response:
            if response.status_code not in (200, 206):
                return {}
            head = b""
            async for chunk in response.aiter_bytes():
                head += chunk
                info = image_info(head)
                # Keep reading past the magic bytes: the format is known after
                # four of them, but SOF0 (the pixel size) sits behind the Exif
                # block, so stopping at the first hit is what made every 1K
                # asset come back without a size.
                if info.get("size") or len(head) >= 524288:
                    return info
            return image_info(head)
    except Exception as exc:  # noqa: BLE001 - metadata is a nicety, never fatal
        log.debug("cannot describe asset %s: %s", url, exc)
        return {}


async def upload_image(
    data: str, mime_type: str = "image/png", filename: str | None = None
) -> str:
    """Push a base64 image to the channel's own CDN and return its URL.

    Ark accepts ``image`` as a URL *or* a ``data:`` URI. The generation tool only
    reads URLs, so an inline image has to be materialised first — this tool is
    the only way to do that without a caller-visible bucket.
    """
    payload: dict[str, Any] = {"data": data, "mime_type": mime_type}
    if filename:
        payload["filename"] = filename
    result = await call("upload_to_cdn", payload, UPLOAD_TIMEOUT)
    url = result.get("cdn_url") or ""
    if not url:
        raise MediaError(502, "image upload returned no cdn_url", payload=result)
    return url


async def resolve_reference(value: str, index: int = 0) -> str:
    """Turn a URL or an inline base64 image into something the tool can read."""
    text = (value or "").strip()
    if not text:
        raise MediaError(400, "empty image reference", code=400)
    if text.startswith("data:"):
        header, _, blob = text.partition(",")
        mime = (header[5:].split(";")[0] or "image/png").strip().lower()
        extension = mime.rsplit("/", 1)[-1].replace("jpg", "jpeg")
        return await upload_image(blob, mime, f"ref_{index}.{extension}")
    if "://" in text:
        return text
    # Bare base64: some Ark SDKs drop the data: prefix on the way through.
    return await upload_image(text, "image/png", f"ref_{index}.png")


async def image_generate(
    *,
    prompt: str,
    n: int = 1,
    size: str | None = None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    reference_urls: list[str] | None = None,
) -> dict:
    """Run ``image_synthesize`` and return the raw envelope plus a clean item list."""
    derived_aspect, derived_resolution = size_to_params(size)
    aspect = (aspect_ratio or "").strip() or derived_aspect
    if aspect == "auto":
        aspect = derived_aspect
    res = _SIZE_TIERS.get((resolution or "").strip().lower()) or (resolution or "").strip().upper() or derived_resolution

    resolved_refs: list[str] = []
    for index, reference in enumerate(reference_urls or []):
        resolved_refs.append(await resolve_reference(reference, index))

    requests: list[dict[str, Any]] = []
    for index in range(max(1, n)):
        item: dict[str, Any] = {"prompt": prompt, "output_file": f"slot_{index}.png"}
        if resolved_refs:
            item["input_urls"] = resolved_refs
        item["aspect_ratio"] = aspect
        item["resolution"] = res
        requests.append(item)

    data = await call("image_synthesize", {"requests": requests}, IMAGE_TIMEOUT)

    images: list[dict[str, Any]] = []
    for item in data.get("success_items") or []:
        url = item.get("output_url")
        if not url:
            continue
        image: dict[str, Any] = {"url": url}
        if item.get("clean_oss_key"):
            image["oss_key"] = item["clean_oss_key"]
        if item.get("text_response"):
            image["revised_prompt"] = item["text_response"]
        images.append(image)

    if not images:
        failed = data.get("failed_items") or []
        detail = ""
        if failed and isinstance(failed[0], dict):
            detail = failed[0].get("error_msg") or ""
            if failed[0].get("err_code"):
                detail = f"{detail} [{failed[0]['err_code']}]".strip()
        raise MediaError(502, detail or "upstream returned no image", payload=data)

    return {"images": images, "raw": data}


# ---------------------------------------------------------------------------
# video
# ---------------------------------------------------------------------------
def normalize_video_model(model: str | None) -> str:
    if not model:
        return DEFAULT_VIDEO_MODEL
    lowered = model.strip().lower()
    for candidate in VIDEO_MODELS:
        if candidate.lower() == lowered:
            return candidate
    # Accept the short aliases the docs use.
    if lowered in {"h3", "h3.0", "minimax-h3"}:
        return "MiniMax-H3"
    if lowered in {"h3-max", "h3max", "minimax-h3-max", "h3-max-fast"}:
        return "MiniMax-H3-Max"
    if lowered in {"h2.3", "hailuo", "hailuo-2.3", "minimax-hailuo-2.3"}:
        return "MiniMax-Hailuo-2.3"
    raise MediaError(
        400,
        f"unknown video model {model!r}; use one of {', '.join(VIDEO_MODELS)} "
        "(H3: 4-15s, 768P/2K; H3-Max: 5-15s, 480P/768P; Hailuo-2.3: 6/10s, 768P/1080P)",
    )


async def video_submit(payload: dict, account: str | None = None) -> tuple[dict, str]:
    """Submit a video task; returns ``(response, account that owns the task)``."""
    return await call_ex("submit_video_generation", payload, VIDEO_SUBMIT_TIMEOUT, account)


async def video_query(task_id: str, model: str, account: str | None = None) -> dict:
    """Query one task. Deliberately sends no output path — the server treats one
    as a delivery request and answers 503 when it cannot stage a local file.

    ``account`` must be the one that submitted the task: any other account
    answers ``404 task was not found or does not belong to the current caller``.
    """
    return await call(
        "query_video_generation", {"task_id": task_id, "model": model},
        VIDEO_QUERY_TIMEOUT, account,
    )


# ---------------------------------------------------------------------------
# speech / music
# ---------------------------------------------------------------------------
async def speech(
    *,
    text: str,
    voice_id: str | None = None,
    speed: float | None = None,
    volume: float | None = None,
    pitch: int | None = None,
    emotion: str | None = None,
) -> str:
    payload: dict[str, Any] = {"text": text}
    if voice_id:
        payload["voice_id"] = voice_id
    if speed is not None:
        payload["speed"] = speed
    if volume is not None:
        payload["volume"] = volume
    if pitch is not None:
        payload["pitch"] = pitch
    if emotion:
        payload["emotion"] = emotion

    data = await call("synthesize_speech", payload, SPEECH_TIMEOUT)
    url = data.get("output_url")
    if not url:
        raise MediaError(502, "upstream returned no audio", payload=data)
    return url


async def music(
    *,
    prompt: str,
    lyrics: str | None = None,
    sample_rate: int | None = None,
    bitrate: int | None = None,
    audio_format: str | None = None,
    n: int = 1,
) -> dict:
    requests: list[dict[str, Any]] = []
    extension = (audio_format or "mp3").lower()
    for index in range(max(1, n)):
        item: dict[str, Any] = {"prompt": prompt, "output_file": f"slot_{index}.{extension}"}
        if lyrics:
            item["lyrics"] = lyrics
        if sample_rate:
            item["sample_rate"] = sample_rate
        if bitrate:
            item["bitrate"] = bitrate
        if audio_format:
            item["format"] = audio_format
        requests.append(item)

    data = await call("batch_text_to_music", {"requests": requests}, MUSIC_TIMEOUT)

    tracks: list[dict[str, Any]] = []
    for item in data.get("success_items") or []:
        url = item.get("output_url")
        if url:
            tracks.append({"url": url})
    if not tracks:
        failed = data.get("failed_items") or []
        detail = ""
        if failed and isinstance(failed[0], dict):
            detail = failed[0].get("error_msg") or ""
        raise MediaError(502, detail or "upstream returned no audio", payload=data)
    return {"tracks": tracks, "raw": data}


async def voices() -> dict:
    return await call("get_voice_list", {}, VOICES_TIMEOUT)


async def download(url: str, timeout: float = 120.0) -> bytes:
    """Fetch a generated asset so we can stream it back or inline it as base64."""
    await upstream.startup()
    client = upstream.client
    try:
        response = await client.get(url, timeout=timeout, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 - network layer
        raise MediaError(502, f"asset download failed: {exc}") from exc
    try:
        if response.status_code != 200:
            raise MediaError(502, f"asset download failed with HTTP {response.status_code}")
        return response.content
    finally:
        await response.aclose()
