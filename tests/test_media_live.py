"""Live checks for the multimodal endpoints against a running gateway.

Not part of CI: the paid calls burn real credits. Free metadata endpoints always
run; generation calls need ``MEDIA_LIVE=1``.

    GATEWAY=http://127.0.0.1:8080 GATEWAY_KEY=sk-local python tests/test_media_live.py
    MEDIA_LIVE=1 GATEWAY=... GATEWAY_KEY=... python tests/test_media_live.py

Costs at 2026-11 rates: image ~ tens of credits, TTS a few, music ~ hundreds,
a Hailuo 6s video 320. Video is off unless ``MEDIA_LIVE_VIDEO=1`` because the
channel currently renders but never delivers the file.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

GATEWAY = os.environ.get("GATEWAY", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("GATEWAY_KEY", "")
PAID = os.environ.get("MEDIA_LIVE") == "1"
VIDEO = os.environ.get("MEDIA_LIVE_VIDEO") == "1"

# The gateway is local in most runs, and an ambient HTTP_PROXY would otherwise
# turn these into absolute-form proxied requests.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(method: str, path: str, body: dict | None = None, timeout: float = 300.0):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(GATEWAY + path, data=data, method=method)
    request.add_header("authorization", f"Bearer {KEY}")
    if data:
        request.add_header("content-type", "application/json")
    with _OPENER.open(request, timeout=timeout) as response:
        raw = response.read()
        status = response.status
        mime = response.headers.get("content-type", "")
    return status, mime, raw


def check_models() -> None:
    status, _, raw = _call("GET", "/v1/media/models", timeout=20)
    assert status == 200, status
    kinds = {item["id"] for item in json.loads(raw)["data"]}
    assert kinds == {"image", "video", "speech", "music"}, kinds
    print(f"media models     -> {sorted(kinds)}")


def check_voices() -> None:
    status, _, raw = _call("GET", "/v1/audio/voices", timeout=30)
    assert status == 200, status
    voices = json.loads(raw)["data"]
    assert len(voices) > 100, len(voices)
    print(f"voices           -> {len(voices)} voices, first={voices[0]['voice_id']}")


def check_image() -> None:
    status, _, raw = _call(
        "POST", "/v1/images/generations",
        {"prompt": "a red apple on a white table", "size": "1024x1024"},
    )
    assert status == 200, raw[:300]
    url = json.loads(raw)["data"][0]["url"]
    assert url.startswith("https://"), url
    print(f"image            -> {url[:88]}")


def check_speech() -> None:
    status, mime, raw = _call(
        "POST", "/v1/audio/speech",
        {"input": "live check of the speech endpoint", "voice": "English_expressive_narrator"},
        timeout=180,
    )
    assert status == 200, raw[:300]
    assert mime.startswith("audio/"), mime
    assert raw[:3] in {b"ID3", b"\xff\xfb", b"\xff\xf3"}, raw[:4]
    print(f"speech           -> {len(raw)} bytes, {mime}")


def check_music() -> None:
    status, _, raw = _call(
        "POST", "/v1/music/generations",
        {"prompt": "calm piano solo, 8 seconds", "format": "mp3"},
        timeout=400,
    )
    assert status == 200, raw[:300]
    url = json.loads(raw)["data"][0]["url"]
    print(f"music            -> {url[:88]}")


def check_video() -> None:
    status, _, raw = _call(
        "POST", "/v1/videos/generations",
        {"model": "MiniMax-Hailuo-2.3", "prompt": "a cat on grass", "duration": 6,
         "resolution": "768P"},
        timeout=180,
    )
    assert status == 202, raw[:300]
    task_id = json.loads(raw)["id"]
    print(f"video submit     -> task {task_id}")

    deadline = time.time() + 420
    last = ""
    while time.time() < deadline:
        status, _, raw = _call(
            "GET", f"/v1/videos/generations/{task_id}?model=MiniMax-Hailuo-2.3", timeout=90
        )
        payload = json.loads(raw)
        last = payload.get("status") or (payload.get("error") or {}).get("message", "")
        if payload.get("video_url"):
            print(f"video            -> {payload['video_url'][:80]}")
            return
        if payload.get("status") == "failed":
            raise AssertionError(payload)
        time.sleep(20)
    print(f"video            -> rendered but not delivered ({last[:60]})")


def main() -> int:
    checks = [check_models, check_voices]
    if PAID:
        checks += [check_image, check_speech, check_music]
    if VIDEO:
        checks.append(check_video)

    failures = 0
    for check in checks:
        try:
            check()
        except (AssertionError, urllib.error.HTTPError, OSError) as exc:
            failures += 1
            print(f"FAIL {check.__name__}: {exc}")
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    if not PAID:
        print("(generation calls skipped; set MEDIA_LIVE=1 to run them)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
