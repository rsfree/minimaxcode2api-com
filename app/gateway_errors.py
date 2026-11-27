"""Upstream image-generation error -> canonical OpenAI-shaped error.

Copy next to ``error_map.json`` and import it. No third-party dependencies.

Why this is a separate module from the chat one
-----------------------------------------------
Image APIs do not report failure the way chat APIs do. Measured across eight
vendors (see ``references/provider-matrix.md``):

* HTTP 200 is the *normal* carrier of failure. MiniMax returns
  ``base_resp.status_code=1008`` with 200; Hunyuan returns
  ``JobStatusCode="4"`` with 200; Gemini returns
  ``finishReason=IMAGE_SAFETY`` with 200; Stability returns
  ``finish_reason=CONTENT_FILTERED`` with 200 and zero artifacts.
* One request has up to four phases -- submit, accept, poll, fetch -- and the
  same status code means different things in each. A 404 while fetching the
  result is an expired link, not a missing model.
* The job is non-idempotent and expensive. A retry after a 5xx can bill a
  second generation, so the chat rule "retry 5xx once" is wrong here unless the
  phase proves nothing was accepted.

Design rules:

* Judge on ``<phase, status, code, type, body-status, artifact-count>``. The
  HTTP status alone decides nothing.
* Empty artifacts at a phase that promises an image is a failure even with 200.
* Partial delivery is *not* an error. Rendering it as one discards images the
  operator already paid for and invites a duplicate, also-paid retry.
* The caller's ``message`` comes from a template. Upstream text never enters it.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error_map.json")

with open(_MAP_PATH, encoding="utf-8") as _fh:
    _SPEC = json.load(_fh)

CANONICAL: dict[str, dict[str, Any]] = _SPEC["canonical"]
HINTS: dict[str, list[str]] = _SPEC["hints"]
GENERIC_RULES: list[dict[str, Any]] = _SPEC["generic_rules"]
PROVIDERS: dict[str, dict[str, Any]] = _SPEC["providers"]

RAW_BODY_CAP = 800
PAYLOAD_CAP = 4096
LONG_STRING_CAP = 400

#: Phases of an image request. Passing the wrong one is the most common way a
#: correct mapping table produces a wrong verdict, so the value is explicit.
PHASES = ("submit", "accept", "poll", "result")

_ALIASES: dict[str, str] = {}
for _name, _spec in PROVIDERS.items():
    _ALIASES[_name] = _name
    for _alias in _spec.get("aliases", []):
        _ALIASES[_alias.lower()] = _name

#: A rule that uses a key this module does not implement would be silently
#: ignored, which is worse than a crash. Fail at import instead.
_RULE_KEYS = {
    "id",
    "canonical",
    "status",
    "stage",
    "code_exact",
    "code_prefix",
    "code_regex",
    "type_exact",
    "message_contains",
    "message_regex",
    "hint_any",
    "body_status",
    "body_int",
    "finish_reason",
    "block_reason",
    "requires_error",
    "requires_empty_artifact",
    "billed",
    "policy",
}


def _validate_rules() -> None:
    for owner, rules in (
        [("generic", GENERIC_RULES)]
        + [(name, spec.get("rules", [])) for name, spec in PROVIDERS.items()]
    ):
        for rule in rules:
            unknown = set(rule) - _RULE_KEYS
            if unknown:
                raise RuntimeError(f"rule {owner}/{rule.get('id')} uses unknown keys {unknown}")
            if rule["canonical"] not in CANONICAL:
                raise RuntimeError(f"rule {owner}/{rule.get('id')} names unknown canonical")


_validate_rules()


def resolve_provider(name: str) -> str:
    key = (name or "").strip().lower()
    return _ALIASES.get(key, key)


# ---------------------------------------------------------------------------
# payload access
# ---------------------------------------------------------------------------
def _dig(payload: Any, path: str) -> Any:
    node = payload
    for part in path.split("."):
        if isinstance(node, Mapping):
            node = node.get(part)
        elif isinstance(node, (list, tuple)) and part.isdigit():
            index = int(part)
            node = node[index] if 0 <= index < len(node) else None
        else:
            return None
        if node is None:
            return None
    return node


_FAILED_STATES = {"FAILED", "FAILURE", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "TIMED_OUT"}
_LIVE_STATES = {"PENDING", "QUEUED", "IN_QUEUE", "RUNNING", "IN_PROGRESS", "PROCESSING", "STARTING"}
_OK_STATES = {"SUCCEEDED", "SUCCESS", "COMPLETED", "DONE", "FINISHED"}
#: Hunyuan uses digits where everyone else uses words.
_NUMERIC_STATES = {"1": "PENDING", "2": "RUNNING", "3": "RUNNING", "4": "FAILED", "5": "SUCCEEDED"}

_BLOCKED_FINISH = {
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "PROHIBITED_CONTENT",
    "IMAGE_RECITATION",
    "RECITATION",
    "SAFETY",
    "BLOCKLIST",
    "SPII",
    "CONTENT_FILTERED",
}
_NO_IMAGE_FINISH = {"NO_IMAGE", "NOIMAGE"}


@dataclass
class Features:
    codes: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    job_status: str = ""
    int_status: int | None = None
    finish_reasons: list[str] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    artifact_count: int | None = None
    item_errors: int = 0
    task_metrics_failed: int = 0
    has_error: bool = False
    success_flag: bool | None = None
    param: str = ""

    @property
    def text(self) -> str:
        return " | ".join(self.messages)[:RAW_BODY_CAP]


def _status_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return _NUMERIC_STATES.get(str(int(value)), "")
    text = str(value).strip().upper()
    if text in _FAILED_STATES or text in _LIVE_STATES or text in _OK_STATES:
        return text
    return _NUMERIC_STATES.get(text, "")


def _collect_strings(node: Any, keys: set[str], depth: int = 0, out: list[str] | None = None) -> list[str]:
    """Every string value under any of ``keys``, at any depth."""
    if out is None:
        out = []
    if depth > 5:
        return out
    if isinstance(node, Mapping):
        for key, value in node.items():
            if str(key).lower() in keys:
                if isinstance(value, str) and value.strip():
                    out.append(value.strip())
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    out.append(str(value))
            _collect_strings(value, keys, depth + 1, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_strings(item, keys, depth + 1, out)
    return out


def _count_artifacts(payload: Any) -> int | None:
    """How many usable images this body carries.

    ``None`` means "this body does not speak about artifacts at all" (a submit
    acknowledgement), which must not be read as zero.
    """
    carriers = (
        "data",
        "artifacts",
        "images",
        "output.results",
        "output.images",
        "Response.ResultImage",
        "data.image_urls",
        "image_urls",
    )
    for path in carriers:
        node = _dig(payload, path)
        if isinstance(node, list):
            count = 0
            payload_keys = ("url", "b64_json", "base64", "image_url", "imageUrl", "inlineData", "inline_data")
            for item in node:
                if isinstance(item, Mapping):
                    if any(item.get(k) for k in payload_keys):
                        count += 1
                    elif any(k in item for k in payload_keys):
                        # Present but empty: a placeholder, not an image.
                        continue
                    elif item.get("error") or item.get("code"):
                        continue
                    else:
                        # Metadata-only entry in providers that return such slots.
                        count += 1
                elif isinstance(item, str) and item.strip():
                    count += 1
            return count
    candidates = _dig(payload, "candidates")
    if isinstance(candidates, list):
        count = 0
        for candidate in candidates:
            parts = _dig(candidate, "content.parts")
            if isinstance(parts, list):
                count += sum(
                    1
                    for part in parts
                    if isinstance(part, Mapping) and (part.get("inlineData") or part.get("inline_data"))
                )
        return count
    return None


def _count_item_errors(payload: Any) -> int:
    total = 0
    for path in ("data", "output.results", "results", "artifacts", "images"):
        node = _dig(payload, path)
        if not isinstance(node, list):
            continue
        for item in node:
            if not isinstance(item, Mapping):
                continue
            err = item.get("error")
            if isinstance(err, Mapping) and err:
                total += 1
            elif isinstance(err, str) and err.strip():
                total += 1
            elif item.get("success") is False:
                total += 1
    return total


def extract(payload: Any, body_text: str = "") -> Features:
    f = Features()
    if not isinstance(payload, (Mapping, list)):
        if body_text.strip():
            # HTML error page or a plain-text banner. Operator-only.
            f.messages.append(body_text.strip()[:RAW_BODY_CAP])
        return f

    err = _dig(payload, "error")
    if isinstance(err, Mapping) and any(err.values()):
        f.has_error = True

    # Alibaba's anti-bot envelope is a bare ``{"ret": [...]}`` with no error
    # member at all. Normalise it to a code so it cannot fall through to
    # "carried no image".
    ret = _dig(payload, "ret")
    if isinstance(ret, list) and ret:
        f.codes.append("x5sec")
        f.messages.append(json.dumps(ret, ensure_ascii=False)[:200])

    for path in (
        "error.code",
        "error.status",
        "error.type",
        "code",
        "error_code",
        "err_code",
        "errcode",
        "Response.Error.Code",
        "Response.JobErrorCode",
        "JobErrorCode",
        "output.code",
        "data.code",
        "name",
    ):
        value = _dig(payload, path)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text and text.lower() not in ("0", "200", "success", "ok", "none", "null"):
                f.codes.append(text)
    f.codes.extend(_collect_strings(_dig(payload, "errors"), {"name"}))

    for path in ("error.type", "type"):
        value = _dig(payload, path)
        if isinstance(value, str) and value.strip():
            f.types.append(value.strip())

    for path in (
        "error.message",
        "error.msg",
        "message",
        "msg",
        "detail",
        "output.message",
        "data.details",
        "data.detail",
        "JobErrorMsg",
        "Response.JobErrorMsg",
        "error",
        "Response.Error.Message",
        "base_resp.status_msg",
    ):
        value = _dig(payload, path)
        if isinstance(value, str) and value.strip():
            f.messages.append(value.strip()[:RAW_BODY_CAP])
        elif isinstance(value, list):
            for item in value[:4]:
                if isinstance(item, str):
                    f.messages.append(item[:200])
                elif isinstance(item, Mapping):
                    text = item.get("msg") or item.get("message")
                    if isinstance(text, str):
                        f.messages.append(text[:200])
    f.messages.extend(_collect_strings(_dig(payload, "errors"), {"message"}))

    for path in ("output.task_status", "task_status", "Response.JobStatusCode", "JobStatusCode"):
        state = _status_string(_dig(payload, path))
        if state:
            f.job_status = state
            break
    if not f.job_status:
        # Only trust a bare ``status`` when it looks like a job state; chat
        # responses and image results carry unrelated ``status`` fields.
        f.job_status = _status_string(_dig(payload, "status"))

    raw_int = _dig(payload, "base_resp.status_code")
    if raw_int is None:
        raw_int = _dig(payload, "base_resp.status_code")
    if isinstance(raw_int, (int, float)) and not isinstance(raw_int, bool):
        f.int_status = int(raw_int)

    for path in ("promptFeedback.blockReason", "prompt_feedback.block_reason"):
        value = _dig(payload, path)
        if isinstance(value, str) and value.strip():
            f.block_reasons.append(value.strip().upper())

    f.finish_reasons.extend(x.upper() for x in _collect_strings(payload, {"finishreason", "finish_reason", "finishreasonreasons"}))
    f.finish_reasons = list(dict.fromkeys(f.finish_reasons))
    f.block_reasons = list(dict.fromkeys(f.block_reasons))

    f.artifact_count = _count_artifacts(payload)
    f.item_errors = _count_item_errors(payload)

    failed_metric = _dig(payload, "output.task_metrics.FAILED")
    if isinstance(failed_metric, (int, float)) and not isinstance(failed_metric, bool):
        f.task_metrics_failed = int(failed_metric)

    param = _dig(payload, "error.param")
    if isinstance(param, str) and param.strip():
        f.param = param.strip()[:80]

    success = _dig(payload, "success")
    if success is None:
        success = _dig(payload, "data.success")
    if isinstance(success, bool):
        f.success_flag = success
    return f


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _status_hit(rule: dict[str, Any], status: int) -> bool:
    wanted = rule.get("status")
    if wanted is None:
        return True
    values = _as_list(wanted)
    return "*" in values or status in values


def _stage_hit(rule: dict[str, Any], stage: str) -> bool:
    wanted = rule.get("stage")
    if wanted is None:
        return True
    return stage in [str(x) for x in _as_list(wanted)]


def _code_hit(rule: dict[str, Any], f: Features) -> bool:
    exact = [str(x).lower() for x in _as_list(rule.get("code_exact"))]
    prefix = [str(x).lower() for x in _as_list(rule.get("code_prefix"))]
    regex = _as_list(rule.get("code_regex"))
    type_exact = [str(x).lower() for x in _as_list(rule.get("type_exact"))]

    if exact or prefix or regex:
        pool = [c.lower() for c in f.codes] + [t.lower() for t in f.types]
        hit = False
        for value in pool:
            if value in exact or any(value.startswith(p) for p in prefix):
                hit = True
                break
            if any(re.search(p, value, re.I) for p in regex):
                hit = True
                break
        if not hit:
            return False

    if type_exact:
        if not any(t.lower() in type_exact for t in f.types):
            return False
    return True


def _message_hit(rule: dict[str, Any], f: Features) -> bool:
    text = f.text
    low = text.lower()
    needles = [str(x).lower() for x in _as_list(rule.get("message_contains"))]
    if needles and not any(n in low for n in needles):
        return False
    for pattern in _as_list(rule.get("message_regex")):
        if not re.search(pattern, text, re.I):
            return False
    for group in _as_list(rule.get("hint_any")):
        needles = HINTS.get(str(group), [])
        if not any(n.lower() in low for n in needles):
            return False
    return True


def _body_status_hit(rule: dict[str, Any], f: Features) -> bool:
    wanted = rule.get("body_status")
    if wanted is None:
        return True
    if not f.job_status:
        return False
    values = [str(x).upper() for x in _as_list(wanted)]
    # A rule that says FAILED must not match CANCELED by accident.
    return f.job_status in values


def _body_int_hit(rule: dict[str, Any], f: Features) -> bool:
    spec = rule.get("body_int")
    if spec is None:
        return True
    if f.int_status is None:
        return False
    values = [int(x) for x in _as_list(spec.get("in"))]
    return f.int_status in values


def _list_hit(rule: dict[str, Any], key: str, values: list[str]) -> bool:
    wanted = rule.get(key)
    if wanted is None:
        return True
    targets = [str(x).upper() for x in _as_list(wanted)]
    return any(value in targets for value in values)


def _match(rule: dict[str, Any], status: int, stage: str, f: Features) -> bool:
    if not _status_hit(rule, status):
        return False
    if not _stage_hit(rule, stage):
        return False
    if rule.get("requires_error") and not f.has_error:
        return False
    # Strict: a body that never mentions artifacts (a submit acknowledgement)
    # must not satisfy "produced no image".
    if rule.get("requires_empty_artifact") and f.artifact_count != 0:
        return False
    return (
        _code_hit(rule, f)
        and _message_hit(rule, f)
        and _body_status_hit(rule, f)
        and _body_int_hit(rule, f)
        and _list_hit(rule, "finish_reason", f.finish_reasons)
        and _list_hit(rule, "block_reason", f.block_reasons)
    )


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    provider: str
    canonical: str
    rule_id: str
    confidence: str
    status: int
    stage: str = "submit"
    raw: Features = field(default_factory=Features)
    retry_after: int | None = None
    param: str = ""
    billed: str = "maybe"
    policy_override: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> dict[str, Any]:
        return CANONICAL[self.canonical]

    @property
    def http(self) -> int:
        return int(self.spec["http"])

    @property
    def type(self) -> str:
        return str(self.spec["type"])

    @property
    def code(self) -> str:
        return self.canonical

    @property
    def retry(self) -> str:
        return str(self.spec["retry"])

    @property
    def partial(self) -> bool:
        return bool(self.spec.get("not_an_error"))


def parse_retry_after(value: str | None) -> int | None:
    """Normalise ``Retry-After`` to whole seconds, capped at 900."""
    if not value:
        return None
    value = value.strip().rstrip("sS").strip()
    try:
        seconds = int(float(value))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = int((when - datetime.now(timezone.utc)).total_seconds())
    return max(0, min(seconds, 900))


def _billed(stage: str, status: int, canonical: str, rule: dict[str, Any]) -> str:
    if rule.get("billed"):
        return str(rule["billed"])
    if canonical in ("content_policy_violation", "invalid_request_error", "invalid_image",
                     "request_too_large", "authentication_error", "permission_denied",
                     "model_not_found", "task_not_found", "insufficient_quota"):
        # Rejected before or instead of a billed generation. fal is the known
        # exception (a 422 can still be billed); its rule says so explicitly.
        return "no" if status < 500 else "maybe"
    if stage in ("poll", "result"):
        return "yes"
    if canonical == "generation_failed":
        return "yes"
    return "maybe"


def _is_failure(status: int, stage: str, f: Features) -> bool:
    if status >= 400:
        return True
    if f.job_status in _FAILED_STATES:
        return True
    if f.int_status not in (None, 0):
        return True
    if f.block_reasons:
        return True
    if any(x in _BLOCKED_FINISH or x in _NO_IMAGE_FINISH for x in f.finish_reasons):
        return True
    if f.has_error:
        return True
    # An image phase that promises images and produced none is a failure --
    # except at ``accept``, where a task id is the whole payload.
    if stage == "accept":
        return False
    if f.artifact_count == 0:
        return True
    # A 200 that carries an error-shaped code and no URL is a refusal in the
    # vendor's own envelope (qwen's reverse-engineered web dialect answers
    # ``{"success": false, "data": {"code": "RateLimited"}}`` with 200).
    if f.success_flag is False:
        return True
    if f.codes and f.artifact_count is None:
        return True
    return False


def _is_partial(f: Features, n: int | None) -> bool:
    delivered = f.artifact_count or 0
    if delivered <= 0:
        return False
    if f.task_metrics_failed > 0:
        return True
    if f.item_errors > 0:
        return True
    if n and n > 1 and delivered < n:
        return True
    return False


def classify(
    provider: str,
    status: int,
    payload: Any = None,
    body_text: str = "",
    *,
    stage: str = "submit",
    n: int | None = 1,
    retry_after_header: str | None = None,
) -> Decision | None:
    """Map one upstream reply to a canonical semantic.

    Returns ``None`` for a clean success. Returns a decision with
    ``partial == True`` when some images were delivered -- the caller keeps the
    images and must not render an error body.

    ``status == 0`` means the transport died before a status line existed.
    ``stage`` must be one of ``PHASES``; ``accept`` is an async submit that
    returns only a task id, where zero artifacts is the expected shape.
    """
    if stage not in PHASES:
        raise ValueError(f"stage must be one of {PHASES}, got {stage!r}")

    name = resolve_provider(provider)
    f = extract(payload, body_text)
    if status == 0:
        d = Decision(name, "upstream_timeout", "transport.no_status", "field", 0, stage, f, billed="no")
        return d

    if not _is_failure(status, stage, f):
        if _is_partial(f, n):
            return Decision(name, "partial_success", "partial.artifacts", "field", status, stage, f, billed="yes")
        return None

    spec = PROVIDERS.get(name, {})
    ordered = [(rule, True) for rule in spec.get("rules", [])] + [
        (rule, False) for rule in GENERIC_RULES
    ]

    matched: dict[str, Any] | None = None
    for rule, is_provider_rule in ordered:
        if _match(rule, status, stage, f):
            matched = rule
            confidence = spec.get("confidence", "generic") if is_provider_rule else "generic"
            break
    if matched is None:
        matched = {"id": "fallback", "canonical": "unknown_upstream_error"}
        confidence = "none"

    decision = Decision(
        provider=name,
        canonical=str(matched["canonical"]),
        rule_id=str(matched["id"]),
        confidence=str(confidence),
        status=status,
        stage=stage,
        raw=f,
        param=f.param,
        billed=_billed(stage, status, str(matched["canonical"]), matched),
        policy_override=dict(matched.get("policy") or {}),
    )

    if decision.retry == "after_retry_after":
        decision.retry_after = (
            parse_retry_after(retry_after_header) or decision.spec["retry_after_default"]
        )
    return decision


# ---------------------------------------------------------------------------
# two views
# ---------------------------------------------------------------------------
def new_request_id() -> str:
    return "req_" + secrets.token_hex(4)


def to_caller(decision: Decision, request_id: str | None = None) -> dict[str, Any]:
    """The only shape that may leave the process."""
    if decision.partial:
        raise ValueError(
            "partial_success must not be rendered as an error body: the delivered "
            "images are already billed and the caller would retry them"
        )
    rid = request_id or new_request_id()
    message = str(decision.spec["message"]).format(
        retry_after=decision.retry_after or 0, request_id=rid
    )
    error: dict[str, Any] = {
        "message": message,
        "type": decision.type,
        "code": decision.code,
        "request_id": rid,
    }
    if decision.param:
        error["param"] = decision.param
    return {"error": error}


def sanitize_url(url: str) -> str:
    """Drop everything after ``?`` and any userinfo.

    Image result links are signed: Hunyuan's COS link carries ``q-ak=``,
    Aliyun's OSS link carries ``OSSAccessKeyId=``, fal's CDN link carries a
    token. A host plus a path is enough to debug; the query is the credential.
    """
    if not isinstance(url, str) or not url:
        return ""
    if url.startswith("data:"):
        head, _, payload = url.partition(",")
        return f"{head};<{len(payload)} chars redacted>"
    match = re.match(r"^(https?)://(?:[^/@]+@)?([^/?#]+)([^?#]*)", url, re.I)
    if not match:
        return "<unparseable url>"
    scheme, host, path = match.groups()
    return f"{scheme}://{host}{path}?<redacted>"


_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


def _primary_code(codes: list[str]) -> str:
    """The most informative code for a human reading one log line.

    Google puts a numeric ``error.code`` (402) next to a string
    ``error.status`` (RESOURCE_EXHAUSTED); the number alone tells an operator
    nothing.
    """
    for code in codes:
        if not code.replace(".", "", 1).isdigit():
            return code
    return codes[0] if codes else ""


def _sanitize_text(text: str) -> str:
    """Strip query strings from every URL embedded in free text.

    An upstream error message routinely quotes the URL it failed on
    ("Timeout while downloading url=..."), and result URLs are signed. The
    operator needs the sentence, not the credential.
    """
    return _URL_IN_TEXT.sub(lambda m: sanitize_url(m.group(0)), text or "")


def _redact_node(node: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "<depth>"
    if isinstance(node, Mapping):
        return {k: _redact_node(v, depth + 1) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_redact_node(v, depth + 1) for v in node]
    if isinstance(node, str):
        if re.match(r"^https?://", node, re.I):
            return sanitize_url(node)
        if len(node) > LONG_STRING_CAP:
            digest = hashlib.sha256(node.encode("utf-8", "replace")).hexdigest()[:16]
            return {"_long_string": len(node), "_sha256": digest}
    return node


def _keep_payload(payload: Any, limit: int = PAYLOAD_CAP) -> Any:
    if not isinstance(payload, (Mapping, list)):
        return None
    redacted = _redact_node(payload)
    try:
        if len(json.dumps(redacted, ensure_ascii=False, default=str)) <= limit:
            return redacted
    except (TypeError, ValueError):
        return None
    keys = list(payload)[:20] if isinstance(payload, Mapping) else None
    return {"_truncated": True, "_keys": keys}


def to_operator(
    decision: Decision,
    request_id: str,
    *,
    channel_id: int | str | None = None,
    account_label: str = "",
    payload: Any = None,
    requested_n: int | None = None,
) -> dict[str, Any]:
    """Both views in one row, plus the facts that decide the retry."""
    return {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
        "request_id": request_id,
        "provider": decision.provider,
        "channel_id": channel_id,
        "account_label": account_label,
        "stage": decision.stage,
        "raw_status": decision.status,
        "raw_body_status": decision.raw.job_status,
        "raw_code": _primary_code(decision.raw.codes),
        "raw_codes": decision.raw.codes[:5],
        "raw_type": decision.raw.types[0] if decision.raw.types else "",
        "raw_message": _sanitize_text(decision.raw.text)[:RAW_BODY_CAP],
        "artifact_count": decision.raw.artifact_count,
        "requested_n": requested_n,
        "partial": decision.partial,
        "canonical": decision.canonical,
        "rule_id": decision.rule_id,
        "confidence": decision.confidence,
        "caller_status": decision.http,
        "caller_code": decision.code,
        "retry_after": decision.retry_after,
        "billed": decision.billed,
        "raw_payload": _keep_payload(payload),
    }


# ---------------------------------------------------------------------------
# retry / rotation policy (internal; never derived from the caller status)
# ---------------------------------------------------------------------------
_POLICY: dict[str, dict[str, Any]] = {
    "invalid_request_error": {"same_account_retry": 0, "rotate": False, "cooldown_s": 0, "channel_penalty": False},
    "invalid_image": {"same_account_retry": 0, "rotate": False, "cooldown_s": 0, "channel_penalty": False},
    # Rotating accounts to dodge moderation is how a whole pool gets flagged;
    # the same prompt is refused by every key of the same vendor.
    "content_policy_violation": {"same_account_retry": 0, "rotate": False, "cooldown_s": 0, "channel_penalty": False},
    "request_too_large": {"same_account_retry": 0, "rotate": False, "cooldown_s": 0, "channel_penalty": False},
    "authentication_error": {"same_account_retry": 0, "rotate": True, "cooldown_s": 3600, "channel_penalty": False},
    "permission_denied": {"same_account_retry": 0, "rotate": True, "cooldown_s": 0, "channel_penalty": False},
    "model_not_found": {"same_account_retry": 0, "rotate": False, "cooldown_s": 0, "channel_penalty": False},
    "task_not_found": {"same_account_retry": 0, "rotate": False, "cooldown_s": 0, "channel_penalty": False},
    "rate_limit_exceeded": {"same_account_retry": 0, "rotate": True, "cooldown_s": 60, "channel_penalty": False},
    "insufficient_quota": {"same_account_retry": 0, "rotate": True, "cooldown_s": 86400, "channel_penalty": False},
    # Accepted-then-failed: a retry is a second paid generation, so it is the
    # caller's decision, not the gateway's.
    "generation_failed": {"same_account_retry": 0, "rotate": True, "cooldown_s": 0, "channel_penalty": True},
    "result_unavailable": {"same_account_retry": 0, "rotate": False, "cooldown_s": 0, "channel_penalty": False},
    "server_is_overloaded": {"same_account_retry": 1, "rotate": True, "cooldown_s": 15, "channel_penalty": True},
    "api_error": {"same_account_retry": 1, "rotate": True, "cooldown_s": 0, "channel_penalty": True},
    "upstream_timeout": {"same_account_retry": 1, "rotate": True, "cooldown_s": 0, "channel_penalty": True},
    "unknown_upstream_error": {"same_account_retry": 0, "rotate": True, "cooldown_s": 0, "channel_penalty": True},
    "partial_success": {"same_account_retry": 0, "rotate": False, "cooldown_s": 0, "channel_penalty": False},
}


def retry_policy(decision: Decision) -> dict[str, Any]:
    """What the *gateway* does. Never read the caller-facing status here.

    A 5xx at ``submit`` may mean nothing was accepted; the same 5xx at ``poll``
    means a job exists and is likely billed. The phase changes the answer, so
    it is applied on top of the canonical default.
    """
    policy = dict(_POLICY[decision.canonical])
    policy.update(decision.policy_override)
    if decision.canonical == "rate_limit_exceeded" and decision.retry_after:
        policy["cooldown_s"] = max(policy["cooldown_s"], decision.retry_after)
    if decision.stage == "poll" and decision.canonical in ("api_error", "upstream_timeout", "server_is_overloaded"):
        # The job already exists; re-submitting would start (and bill) a new one.
        policy["same_account_retry"] = 0
        policy["resubmit"] = False
    else:
        policy["resubmit"] = decision.canonical in ("api_error", "upstream_timeout", "server_is_overloaded") and decision.stage == "submit"
    if decision.billed == "yes" and decision.stage != "result" and policy["same_account_retry"] > 0:
        # A billed attempt means a retry is a second generation. At ``result``
        # the image already exists, so a re-download is free -- `billed` stays
        # "yes" for accounting, but it must not block the retry.
        policy["same_account_retry"] = 0
    if decision.stage == "result":
        # A CDN/storage hiccup while fetching an already-generated image says
        # nothing about the generation channel; tripping its breaker would
        # retire a healthy channel over a bucket.
        policy["channel_penalty"] = False
    return policy


# ---------------------------------------------------------------------------
# leak guard
# ---------------------------------------------------------------------------
_LEAK_PATTERNS = [
    r"[\w.%+-]+@[\w-]+\.[\w.]{2,}",
    r"(?i)\bbearer\s+\S{8,}",
    r"\beyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]{4,}",
    r"(?i)\b(sk|ak|apikey|api_key)[-_][A-Za-z0-9][A-Za-z0-9_-]{6,}",
    r"(?i)\b(AKID|LTAI|ASIA)[A-Za-z0-9]{8,}",
    r"(?i)(q-ak|q-signature|OSSAccessKeyId|X-Amz-Credential|X-Tos-Credential|Signature|token|access_token)=",
    r"(?i)\b(org|proj|acct|account|uin)[-_:=][A-Za-z0-9]{4,}",
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    r"(?i)https?://[\w.-]+",
    r"(?i)\b(balance|credit|quota)\D{0,8}\d",
    r"data:image/[\w.+-]+;base64,[A-Za-z0-9+/=]{16,}",
]


def leaks(text: str, known_values: Iterable[str] = ()) -> list[str]:
    """Return every reason ``text`` must not be shown to a caller."""
    reasons: list[str] = []
    for pattern in _LEAK_PATTERNS:
        match = re.search(pattern, text or "")
        if match:
            reasons.append(f"pattern {pattern!r} matched {match.group(0)[:60]!r}")
    for value in known_values:
        if value and value in (text or ""):
            reasons.append(f"known value {value[:4]}... present")
    return reasons


def assert_clean(text: str, known_values: Iterable[str] = ()) -> None:
    reasons = leaks(text, known_values)
    if reasons:
        raise AssertionError("caller-facing text leaks: " + "; ".join(reasons))


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------
class GatewayError(Exception):
    """Raise from the adapter; the handler renders both views."""

    def __init__(self, decision: Decision, request_id: str | None = None) -> None:
        if decision.partial:
            raise ValueError("partial success is not an exception; return the images")
        self.decision = decision
        self.request_id = request_id or new_request_id()
        super().__init__(f"{decision.canonical} via {decision.rule_id} ({decision.stage})")

    @property
    def status_code(self) -> int:
        return self.decision.http

    @property
    def caller_body(self) -> dict[str, Any]:
        return to_caller(self.decision, self.request_id)

    @property
    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.decision.retry_after:
            headers["retry-after"] = str(self.decision.retry_after)
        return headers


def from_response(
    provider: str,
    status: int,
    headers: Mapping[str, str] | None = None,
    payload: Any = None,
    body_text: str = "",
    *,
    stage: str = "submit",
    n: int | None = 1,
) -> GatewayError | None:
    """Build the exception straight from an httpx/aiohttp response."""
    retry_after = None
    if headers:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
    decision = classify(
        provider, status, payload, body_text, stage=stage, n=n, retry_after_header=retry_after
    )
    if decision is None or decision.partial:
        return None
    return GatewayError(decision)


if __name__ == "__main__":  # tiny manual probe
    demo = classify(
        "qwen",
        200,
        {"output": {"task_status": "FAILED", "code": "DataInspectionFailed",
                    "message": "内容安全警告：输入数据可能包含不适当的内容！"}},
        stage="poll",
    )
    print(json.dumps(to_caller(demo), indent=2, ensure_ascii=False))
    print(json.dumps(to_operator(demo, "req_demo0001", requested_n=1), indent=2, ensure_ascii=False))
