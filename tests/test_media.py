"""Offline checks for the multimodal translation layer (no network, CI-safe)."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import media  # noqa: E402
from app.media import MediaError  # noqa: E402


def test_size_to_params() -> None:
    assert media.size_to_params(None) == ("1:1", "1K")
    assert media.size_to_params("1024x1024") == ("1:1", "1K")
    # 1.83 MP sits under the 2.1 MP bucket boundary, so it stays at 1K even
    # though the ratio snaps to 16:9. The bucket is decided by area, not by the
    # longer edge -- see the README table.
    assert media.size_to_params("1792x1024") == ("16:9", "1K")
    assert media.size_to_params("1024x1792") == ("9:16", "1K")
    assert media.size_to_params("2048x2048") == ("1:1", "2K")
    assert media.size_to_params("4096x4096") == ("1:1", "4K")
    # An unparseable size is rejected, not silently rounded to 1:1: the caller
    # would otherwise get a different image than the one it asked for.
    try:
        media.size_to_params("garbage")
    except media.MediaError as exc:
        assert exc.status == 400
    else:  # pragma: no cover
        raise AssertionError("an invalid size must be rejected")


def test_video_model_aliases() -> None:
    assert media.normalize_video_model(None) == media.DEFAULT_VIDEO_MODEL
    assert media.normalize_video_model("minimax-h3") == "MiniMax-H3"
    assert media.normalize_video_model("H3.0") == "MiniMax-H3"
    assert media.normalize_video_model("hailuo") == "MiniMax-Hailuo-2.3"
    assert media.normalize_video_model("MiniMax-Hailuo-2.3") == "MiniMax-Hailuo-2.3"
    try:
        media.normalize_video_model("sora")
    except MediaError as exc:
        assert exc.status == 400
    else:  # pragma: no cover
        raise AssertionError("unknown model should be rejected")


def test_status_mapping() -> None:
    assert media._status_for(402) == 402
    assert media._status_for(503) == 503
    assert media._status_for(401) == 401
    assert media._status_for(2069) == 502


def test_media_error_exposes_task_status() -> None:
    exc = MediaError(503, "not ready", code=503, payload={"status": "succeeded"})
    assert exc.status_field == "succeeded"
    plain = MediaError(502, "boom")
    assert plain.status_field is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{'all passed' if not failures else f'{failures} failed'}")
    raise SystemExit(1 if failures else 0)
