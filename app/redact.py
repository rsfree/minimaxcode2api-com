"""Strip account identity out of anything that leaves the gateway.

The request log is internal: it may name the account that failed, because that
is the only field that tells an operator what to do next. An API response is
not internal. A caller who sees

    no usable account: account aav2dfsmiw@mail.giie.eu.org has no refresh token

learns which mailbox is in the pool, that it is broken, and — with a couple of
guesses — what the rest of the pool looks like. Nothing about that helps them
retry the request.

So two rules:

* Messages this code writes never interpolate an account label (see
  ``upstream._send``); the detail goes to the log line instead.
* Everything else — upstream text, a token endpoint body echoed back, a
  traceback that found its way into a string — passes through :func:`scrub`
  on the way out. The unscrubbed text is already recorded in ``metrics``.
"""

from __future__ import annotations

import re

# Local part, @, domain, dot, tld. Deliberately loose: hiding a real address
# costs nothing, missing one leaks a mailbox.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")

# "Bearer abc.def-ghi" — the credential itself, not the scheme.
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}")

# JWT-shaped: three dot-separated base64url runs, always starting with eyJ.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}")

# Opaque secrets: long hex/base64url runs (access tokens, refresh tokens, ids).
_OPAQUE = re.compile(r"\b(?=[A-Za-z0-9_\-]*[0-9])[A-Za-z0-9_\-]{32,}\b")

# ``refresh_token=abc`` / ``access_token: abc`` in an echoed form or JSON body.
_NAMED = re.compile(
    r"(?i)\b(refresh_token|access_token|id_token|client_secret|authorization)"
    r"(\s*[=:]\s*)(\"?)[^\s\",}]{6,}\3"
)

ACCOUNT = "<account>"
SECRET = "<redacted>"


def _known_values() -> list[str]:
    """Account labels and token prefixes currently in the pool, longest first.

    Regexes cannot catch everything: ``Token.short`` is ``access_token[:12]``,
    which is a 12-character prefix of a live credential and looks like any other
    word. Replacing the actual values is the only reliable way to keep them out
    of a response.
    """
    try:
        from .auth import token_manager
    except Exception:  # pragma: no cover - import cycle during shutdown
        return []
    values: set[str] = set()
    for token in list(getattr(token_manager, "_tokens", None) or []):
        access = getattr(token, "access_token", "") or ""
        for value in (getattr(token, "label", "") or "", access,
                      getattr(token, "refresh_token", "") or ""):
            if len(value) >= 6:
                values.add(value)
        if len(access) >= 12:
            values.add(access[:12])
    return sorted(values, key=len, reverse=True)


def scrub(text: str) -> str:
    """Return ``text`` with addresses and credentials replaced by placeholders."""
    if not text:
        return text
    out = _EMAIL.sub(ACCOUNT, text)
    out = _BEARER.sub(lambda m: f"{m.group(1)} {SECRET}", out)
    out = _JWT.sub(SECRET, out)
    out = _NAMED.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{SECRET}{m.group(3)}", out)
    out = _OPAQUE.sub(SECRET, out)
    for value in _known_values():
        if value in out:
            # An address keeps its placeholder spelling; a credential does not
            # need to advertise that it was a token at all.
            out = out.replace(value, ACCOUNT if "@" in value else SECRET)
    return out


def leaks(text: str) -> bool:
    """True when ``text`` still carries something that must not ship."""
    if not text:
        return False
    if (
        _EMAIL.search(text)
        or _BEARER.search(text)
        or _JWT.search(text)
        or _OPAQUE.search(text)
    ):
        return True
    return any(value in text for value in _known_values())
