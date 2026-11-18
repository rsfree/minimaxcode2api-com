"""Turn a pasted browser session into something curl can replay.

Why a cookie is worth supporting at all: the account host mints an OAuth token in
four steps, and only the middle two carry a human credential —

    POST /oauth2/device/code          no auth
    GET  /oauth2/device/authorize     cookie session   <-- needs a logged-in jar
    POST /oauth2/device/authorize     cookie session + body signature
    POST /oauth2/token                device_code + PKCE verifier

The password login that ``account_api.Session.login`` performs exists *only* to
fill that jar. So a jar pasted out of a logged-in browser is a strictly more
general way in than an email/password pair: it works for accounts that signed up
with Google / Apple / an email code, and it needs nothing from the owner beyond
an open tab. That is the whole reason this module exists.

Three input shapes are accepted, because three tools produce them:

* ``Cookie: a=b; c=d`` — DevTools → Network → any request → Request Headers;
* ``[{"name": "a", "value": "b"}, …]`` — a Cookie-Editor / EditThisCookie export;
* a Netscape ``cookies.txt`` — what ``curl -c`` and most extensions write.

Nothing here touches the network or the filesystem: :func:`parse` is pure, so the
console, the CLI and the importer all agree on what a valid paste looks like.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

# A full browser header for one site is a few hundred bytes. The cap is here so a
# pasted HAR file (megabytes) is refused with a sentence instead of being written
# into every token file and then into every request line.
MAX_INPUT = 32_768
MAX_PAIRS = 64
MAX_VALUE = 4096

# Cookie names are a token per RFC 6265. Anything else is junk from a partial
# paste, and writing it into a jar would make curl send a malformed header.
_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _add(pairs: dict[str, str], name: str, value: str) -> None:
    name = (name or "").strip().strip('"')
    value = (value or "").strip().strip('"')
    if not _NAME.match(name) or not value:
        return
    if len(value) > MAX_VALUE or _CONTROL.search(value):
        return
    if len(pairs) < MAX_PAIRS or name in pairs:
        pairs[name] = value


def _from_json(text: str) -> dict[str, str]:
    """``[{name, value}]`` (Cookie-Editor), ``{name: value}``, or ``{cookies: […]}}``."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    pairs: dict[str, str] = {}
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str):
                _add(pairs, key, value)
        return pairs
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("Name") or item.get("key")
            value = item.get("value") or item.get("Value")
            if isinstance(name, str) and isinstance(value, str):
                _add(pairs, name, value)
    return pairs


def parse(raw: str) -> dict[str, str]:
    """Split any supported paste into ``{name: value}``.

    Returns an empty dict for input that carries no cookie at all, so the caller
    can say "this paste has nothing usable" instead of silently importing an
    account with an empty jar.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    if len(text) > MAX_INPUT:
        raise ValueError(f"cookie 太长了（上限 {MAX_INPUT} 字符，别整份 HAR 贴进来）")

    head = text.lstrip()
    if head[:1] in "[{":
        pairs = _from_json(head)
        if pairs:
            return pairs

    pairs = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("cookie:"):
            line = line[len("cookie:"):].strip()
        if line.startswith("#"):
            # curl marks httpOnly entries this way; everything else after a '#'
            # is a comment (including the file's own header lines).
            if not line.startswith("#HttpOnly_"):
                continue
            line = line[len("#HttpOnly_"):]
        fields = line.split("\t")
        if len(fields) >= 7:
            # Netscape jar: domain, include_subdomains, path, secure, expiry,
            # name, value. The domain column is dropped on purpose — the jar is
            # rewritten for the host we are actually about to talk to.
            _add(pairs, fields[-2], fields[-1])
            continue
        for part in line.split(";"):
            name, sep, value = part.partition("=")
            if sep:
                _add(pairs, name, value)
    return pairs


def header(pairs: dict[str, str]) -> str:
    """The ``Cookie:`` header value, in the order the pairs were read."""
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def domain_for(base_url: str) -> str:
    """``.minimax.io`` for ``https://account.minimax.io``.

    The session is set on the account host but the *product* host is a sibling
    (``agent.minimax.io``), and a host-only cookie would not travel. Deriving the
    registrable suffix is what makes one pasted jar usable for both.
    """
    host = (urlparse(base_url).hostname or "").strip(".")
    labels = [part for part in host.split(".") if part]
    if not labels:
        return host
    # An IP literal has no registrable suffix — taking the last two labels would
    # produce ".0.0.1" for 127.0.0.1 and the jar would never match, which reads as
    # "session invalid" instead of "wrong domain".
    if ":" in host or (len(labels) == 4 and all(part.isdigit() for part in labels)):
        return host
    if len(labels) < 2:
        return host
    return "." + ".".join(labels[-2:])


def jar(pairs: dict[str, str], domain: str, expires_at: int = 0, secure: bool = True) -> str:
    """A Netscape cookie file curl can read with ``-b``.

    ``expires_at`` 0 means a session cookie, which is exactly right: the browser
    session this came from is already a session cookie, and pretending otherwise
    would let it outlive a logout the operator performed upstream.

    ``secure`` must match the scheme being talked to — curl silently drops a
    secure cookie on a plain-http request, which looks exactly like an invalid
    session. In production the account host is https, so the default is the safe
    one: the cookie is never sent over a downgraded connection.
    """
    flag = "TRUE" if secure else "FALSE"
    lines = ["# Netscape HTTP Cookie File", ""]
    for name, value in pairs.items():
        lines.append("\t".join([domain, "TRUE", "/", flag, str(int(expires_at)), name, value]))
    return "\n".join(lines) + "\n"


def mask(pairs: dict[str, str]) -> str:
    """A log-safe description: names only, never values."""
    names = sorted(pairs)
    if not names:
        return "no cookie"
    shown = ", ".join(names[:6])
    more = f" (+{len(names) - 6} more)" if len(names) > 6 else ""
    return f"{len(names)} cookie(s): {shown}{more}"
