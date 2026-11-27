"""Map this channel's MCP failures onto the canonical image taxonomy.

The taxonomy itself is ``app/gateway_errors.py`` + ``app/error_map.json``,
vendored from the ``image-upstream-error-mapping`` skill. It knows eight
public HTTP image APIs; this channel is not one of them, so the adapter below
does two jobs the stock table cannot:

1. **Rewrite the MCP envelope into a shape ``classify()`` reads.** A tool call
   answers ``200 {"code": 0, "success_items": [...], "failed_items": [...]}``.
   Neither ``data`` nor ``images`` appears anywhere, so the stock artifact
   counter sees zero images and would call every success a failure. The
   successful items are re-emitted as ``data[].url`` and the failures as
   ``data[].error`` so the counters work unchanged.

2. **Decide when the body is worth classifying at all.** Classification is
   driven by a *failure signal* -- a non-2xx status, a business ``code`` other
   than 0, a non-empty ``failed_items``, or a failed job state. Without this
   guard, a speech or music payload that simply has no image in it would be
   read as "accepted then produced nothing" and reported as
   ``generation_failed``.

Why ``minimax-mcp`` and not the stock ``minimax`` provider: the official error
table reads ``base_resp.status_code``, and on this channel that field is *always*
0 -- even on the ``{"code": 404, "message": "video task was not found"}`` reply
captured from a probe. Keying on it would classify every business failure as a
success. The in-house provider block keys on ``code`` instead; see
``error_map.json`` for the probe transcript.

The outward shape stays what callers already parse (HTTP status, integer
``code``, ``type``); the canonical verdict rides alongside as ``canonical``.
"""

from __future__ import annotations

from typing import Any

from . import gateway_errors as ge
from .redact import scrub

PROVIDER = "minimax-mcp"

#: Which phase of a request a tool represents. Getting this wrong is the classic
#: way to turn a finished, already-billed job into a second submission.
_STAGE_BY_TOOL = {
    "image_synthesize": "submit",
    "submit_video_generation": "accept",
    "query_video_generation": "poll",
    "synthesize_speech": "submit",
    "batch_text_to_music": "submit",
    "get_voice_list": "submit",
    "upload_to_cdn": "submit",
    "get_upload_url": "submit",
}

#: Job states that mean the upstream accepted the work and then lost it.
_FAILED_JOB_STATES = {"failed", "error", "canceled", "cancelled", "expired"}

#: Envelope codes that mean "no error". ``200`` shows up in some tools where the
#: code field mirrors the HTTP status instead of using 0.
_OK_CODES = {0, 200, "0", "200", "", None}


def stage_for(tool: str) -> str:
    return _STAGE_BY_TOOL.get(tool, "submit")


def _code_failed(code: Any) -> bool:
    return code not in _OK_CODES


def normalise(data: dict[str, Any]) -> dict[str, Any]:
    """Rewrite an MCP envelope into the ``data[]`` shape ``classify()`` reads."""
    out: dict[str, Any] = {
        "code": data.get("code", 0),
        "message": data.get("message") or "",
    }
    successes = data.get("success_items")
    failures = data.get("failed_items")
    if isinstance(successes, list) or isinstance(failures, list):
        rows: list[dict[str, Any]] = []
        for item in successes or []:
            if not isinstance(item, dict):
                continue
            url = item.get("output_url") or item.get("url") or item.get("video_url")
            if url:
                rows.append({"url": url})
        for item in failures or []:
            if not isinstance(item, dict):
                continue
            rows.append(
                {"error": {"code": item.get("err_code"), "message": item.get("error_msg") or ""}}
            )
        out["data"] = rows
        # The stock extractor collects message text from a top-level ``errors``
        # array -- that is what the message rules match against. Copy the
        # failure reasons there without letting them reach the caller.
        if failures:
            out["errors"] = [
                {"code": item.get("err_code"), "message": item.get("error_msg") or ""}
                for item in failures
                if isinstance(item, dict)
            ]
    return out


def classify_tool(
    tool: str,
    status: int,
    data: Any,
    requested_n: int | None = None,
    *,
    force: bool = False,
) -> ge.Decision | None:
    """Return the canonical decision for one tool reply, or ``None`` for success.

    ``force`` is for the caller that already knows the reply is unusable (an
    image job that came back with no items at all): the signals are absent
    precisely because the body is empty.
    """
    payload = data if isinstance(data, dict) else {}
    stage = stage_for(tool)

    if not force:
        job = str(payload.get("status") or payload.get("task_status") or "").lower()
        signal = (
            status >= 400
            or _code_failed(payload.get("code"))
            or bool(payload.get("failed_items"))
            or job in _FAILED_JOB_STATES
        )
        if not signal:
            return None

    return ge.classify(
        PROVIDER, status, normalise(payload), stage=stage, n=requested_n
    )


def legacy_type(status: int) -> str:
    """The ``error.type`` this channel returns for a given outward status."""
    if status == 402:
        return "insufficient_quota"
    if status == 400:
        return "invalid_request_error"
    return "upstream_error"


#: The taxonomy is written for image jobs; this channel also serves video. Two
#: templates name the artifact, so they are reworded for the video tools rather
#: than being leaked into a poll a caller is reading about a video task.
_VIDEO_TOOLS = {"submit_video_generation", "query_video_generation"}

_VIDEO_MESSAGES = {
    "task_not_found": "the video task was not found; the task id is wrong, "
    "or it belongs to another account, or it has expired",
    "result_unavailable": "the video finished rendering but the file could not be "
    "prepared for delivery; keep polling the same task id instead of resubmitting",
}


def caller_error(
    decision: ge.Decision,
    legacy_status: int,
    legacy_code: Any,
    tool: str = "",
) -> dict[str, Any]:
    """Render the caller-facing error: template text plus the canonical verdict.

    Upstream text never appears here -- ``to_caller`` fills the message from the
    taxonomy template, which is the only reason a mailbox or a plan name cannot
    leak through this path.
    """
    envelope = ge.to_caller(decision)
    error = envelope["error"]
    if tool in _VIDEO_TOOLS and decision.canonical in _VIDEO_MESSAGES:
        error["message"] = _VIDEO_MESSAGES[decision.canonical]
    error["type"] = legacy_type(legacy_status)
    error["code"] = legacy_code if legacy_code is not None else legacy_status
    error["canonical"] = decision.canonical
    return envelope


def operator_row(
    decision: ge.Decision,
    tool: str,
    account: str = "",
    payload: Any = None,
    requested_n: int | None = None,
) -> dict[str, Any]:
    """One log line carrying both views (skill contract: upstream facts stay in)."""
    row = ge.to_operator(
        decision,
        ge.new_request_id(),
        account_label=account,
        payload=payload,
        requested_n=requested_n,
    )
    row["tool"] = tool
    return row


def operator_summary(row: dict[str, Any]) -> str:
    """Compact single-line form for the request log."""
    text = (
        f"{row['tool']} {row['canonical']} (rule {row['rule_id']}, stage {row['stage']}, "
        f"raw {row['raw_status']}/{row['raw_code'] or '-'}, billed={row['billed']})"
    )
    return scrub(text)


def retryable_with_same_input(decision: ge.Decision | None) -> bool:
    """May the gateway retry *this job* without changing the request?

    A moderation refusal is the case that matters: retrying it burns two
    upstream calls and ~20 s to receive the same answer, which is what a
    caller-side retry storm is built on. ``generation_failed`` is retried only
    by :func:`retryable_after_reencode`, because a retry with identical bytes
    cannot succeed either.
    """
    if decision is None:
        return False
    return ge.retry_policy(decision)["same_account_retry"] > 0 and decision.billed != "yes"


def retryable_after_reencode(decision: ge.Decision | None) -> bool:
    """May we re-send the *same job* with re-encoded reference images?

    Deliberately narrower than the policy table. ``generation_failed`` is
    ``billed=yes`` there, and re-submitting identical bytes would be a second
    paid generation; but re-encoding the references produces a different
    request, which is what the measured EXIF/ICC failures need. A moderation
    refusal, a quota shortfall or a transport 5xx is excluded: no re-save of
    the pixels changes any of them, and each retry costs a full generation
    round-trip.
    """
    if decision is None:
        # An unclassified 502 has no evidence behind a retry. The old code
        # retried on the status alone, which is how one refused prompt became
        # twenty billed calls.
        return False
    return decision.canonical in {"generation_failed", "unknown_upstream_error"}
