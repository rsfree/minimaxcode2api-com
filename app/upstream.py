"""HTTP transport to the managed MiniMax Messages endpoint.

Two things this layer owns, because they are easy to get wrong:

* **Headers.** The channel needs ``Authorization: Bearer <oauth token>`` *and*
  a placeholder ``x-api-key``. Omitting the placeholder returns
  ``{"code":401,"message":"token is required"}``.
* **Region routing.** Requests from a mainland IP are answered with
  ``307 Temporary Redirect`` to the legacy ``agent.minimaxi.com`` origin, which
  rejects an ``en`` token. Because a redirect drops ``Authorization``, the
  symptom is a confusing 401. We surface that as a configuration error instead.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from . import curl_transport
from .auth import CredentialError, token_manager
from .config import API_KEY_PLACEHOLDER, settings

log = logging.getLogger("minimax.upstream")

REDIRECT_HINT = (
    "upstream answered 307 to {location}. This network is geo-redirected to the "
    "legacy CN origin, which rejects a {region} token. Set UPSTREAM_PROXY to an "
    "egress that reaches {base_url} directly, or switch MINIMAX_REGION."
)

# How long one httpx egress failure keeps the automatic fallback on curl. Short
# enough that a blip cannot define the process's behaviour for hours, long enough
# that a genuinely filtered egress is not re-probed on every request.
CURL_LATCH_SECONDS = 300.0

# Generation tools are entitlement-gated per account: the same call returns
# ``code 503 "temporarily unavailable"`` on an account without the feature and
# succeeds on a sibling. Only creation tools rotate — for
# ``query_video_generation`` a 503 genuinely means "file not staged yet" and
# the caller has to keep polling the same task.
ROTATABLE_CODES = {503}
ROTATABLE_TOOLS = {
    "image_synthesize",
    "submit_video_generation",
    "synthesize_speech",
    "batch_text_to_music",
}


class UpstreamError(RuntimeError):
    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload
        # Filled in by ``_send`` so the console can attribute a failure to the
        # account that produced it instead of showing a bare "-".
        self.account = ""


# A 400 is usually the caller's fault, but these messages mean the *account*
# is empty. Retrying on the next account is the difference between "that one
#号 is spent" and "the whole gateway is down" — observed as a ~1-in-8 failure
# before this branch existed.
QUOTA_HINTS = (
    "not enough credit", "insufficient balance", "insufficient credit",
    "credit not enough", "quota exceeded", "1008",
)


def _quota_exhausted(message: str) -> bool:
    text = (message or "").lower()
    return any(hint in text for hint in QUOTA_HINTS)


def _headers(token: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "accept": "application/json",
        # We relay bytes ourselves, so ask for an uncompressed body and let
        # httpx decode anything the edge compresses anyway.
        "accept-encoding": "identity",
        "anthropic-version": "2023-06-01",
        "authorization": f"Bearer {token}",
        "x-api-key": API_KEY_PLACEHOLDER,
        "user-agent": "undici",
    }


def _mcp_headers(token: str) -> dict[str, str]:
    """Headers for the desktop MCP proxy (image / video / speech tools).

    The proxy wants the desktop client's user agent and nothing else — sending
    ``x-api-key`` or ``anthropic-version`` here is harmless but pointless, and
    ``MiniMaxAgent`` is what the client itself sends.
    """
    return {
        "content-type": "application/json",
        "accept": "application/json",
        "accept-encoding": "identity",
        "authorization": f"Bearer {token}",
        "user-agent": "MiniMaxAgent",
    }


class UpstreamResponse:
    """Uniform view over an httpx response and a curl stream."""

    def __init__(self, httpx_response=None, curl_stream=None) -> None:
        self._h = httpx_response
        self._c = curl_stream
        # Set by ``_send`` once an account actually served the request; the
        # console attributes calls to it.
        self.account = ""

    @property
    def status_code(self) -> int:
        return self._h.status_code if self._h is not None else self._c.status

    @property
    def headers(self):
        return self._h.headers if self._h is not None else self._c.headers

    async def aread(self) -> bytes:
        if self._h is not None:
            return await self._h.aread()
        return await self._c.read()

    async def aclose(self) -> None:
        if self._h is not None:
            await self._h.aclose()
        elif self._c is not None:
            await self._c.close()

    async def aiter_lines(self):
        if self._h is not None:
            async for line in self._h.aiter_lines():
                yield line
        else:
            async for raw in self._c.lines():
                yield raw.decode("utf-8", "ignore").rstrip("\r\n")

    async def aiter_bytes(self):
        if self._h is not None:
            async for chunk in self._h.aiter_bytes():
                yield chunk
        else:
            stdout = self._c.proc.stdout
            while True:
                chunk = await stdout.read(16384)
                if not chunk:
                    break
                yield chunk


class Upstream:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # ``UPSTREAM_TRANSPORT=curl`` is a deliberate choice; the automatic
        # fallback is a *latch with an expiry*, not a switch. See ``_prefer_httpx``.
        self._force_curl = settings.upstream_transport == "curl"
        self._curl_until = 0.0

    def _latch_curl(self) -> None:
        self._curl_until = time.monotonic() + CURL_LATCH_SECONDS

    def _prefer_httpx(self) -> bool:
        """True when the next POST should go out over httpx.

        One ``ConnectError`` used to set a permanent flag: from that moment the
        process spoke curl only — a transport that (before bodies moved to
        stdin) could not carry more than 128 KiB. A single blip therefore
        degraded every later request, invisibly, until the next restart. The
        latch now expires, so a transient outage costs one failed connect per
        ``CURL_LATCH_SECONDS`` instead of the rest of the uptime.
        """
        if settings.upstream_transport == "curl":
            return False
        if settings.upstream_transport == "httpx":
            return True
        if not self._curl_until:
            return True
        if time.monotonic() < self._curl_until:
            return False
        log.info("curl fallback expired; probing httpx again")
        self._curl_until = 0.0
        return True

    def transport_state(self) -> str:
        """One line for the diagnostics panel: which wire is actually in use."""
        if settings.upstream_transport == "curl":
            return "curl (forced by UPSTREAM_TRANSPORT)"
        if settings.upstream_transport == "httpx":
            return "httpx (forced by UPSTREAM_TRANSPORT)"
        left = self._curl_until - time.monotonic()
        if self._curl_until and left > 0:
            return f"curl (fallback, {int(left)}s left; httpx failed earlier)"
        return "httpx"

    async def startup(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=30.0),
            proxy=settings.upstream_proxy or None,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        log.info(
            "upstream ready base_url=%s proxy=%s region=%s transport=%s",
            settings.base_url,
            settings.upstream_proxy or "-",
            settings.region,
            "curl" if self._force_curl else settings.upstream_transport,
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("upstream client not started")
        return self._client

    async def _dispatch_attributed(
        self, url: str, payload: dict, headers: dict[str, str], stream: bool, account: str
    ) -> UpstreamResponse:
        """``_dispatch`` with failures turned into an attributable 502.

        A transport that cannot even start (no ``curl`` binary, exec limits,
        forced-httpx connect errors) used to escape as an unhandled ``OSError``
        and reach the caller as a bodyless 500 with ``account: "-"``. It is not
        that account's fault and retrying the pool will not help, but the row
        has to name where it broke.
        """
        try:
            return await self._dispatch(url, payload, headers, stream)
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            err = UpstreamError(502, f"transport {type(exc).__name__}: {exc}"[:400])
            err.account = account
            raise err from exc

    async def _dispatch(
        self,
        url: str,
        payload: dict,
        headers: dict[str, str],
        stream: bool,
        timeout: float | None = None,
    ) -> UpstreamResponse:
        """POST through httpx, falling back to curl when the egress rejects OpenSSL."""
        if self._prefer_httpx():
            try:
                kwargs = {"timeout": timeout} if timeout else {}
                request = self.client.build_request("POST", url, json=payload, headers=headers, **kwargs)
                response = await self.client.send(request, stream=stream)
                return UpstreamResponse(httpx_response=response)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                if settings.upstream_transport == "httpx":
                    raise
                log.warning(
                    "httpx egress failed (%s); using curl for the next %ds",
                    type(exc).__name__, int(CURL_LATCH_SECONDS),
                )
                self._latch_curl()
        curl_stream = await curl_transport.open_post(
            url,
            headers,
            payload,
            timeout or settings.request_timeout,
            settings.upstream_proxy or None,
        )
        return UpstreamResponse(curl_stream=curl_stream)

    async def _send(self, payload: dict, stream: bool) -> UpstreamResponse:
        await self.startup()

        tried: set[str] = set()
        last: UpstreamError | None = None
        attempts = max(1, len(getattr(token_manager, "_tokens", []) or [1]))
        current = ""
        model_misses = 0

        for _ in range(attempts + 1):
            try:
                token = await token_manager.acquire(exclude=tried)
            except CredentialError as exc:
                # Nothing left to try. Report what actually went wrong: a bad
                # model name used to surface here as "out of quota", because the
                # extra lap ran the pool dry and then blamed the balance.
                if last is not None and not _quota_exhausted(last.message):
                    last.account = last.account or current
                    raise last from exc
                # 503, not 502: nothing is wrong with the upstream, the pool itself
                # has nothing to spend. The message says when the reset lands —
                # but the pool-level detail (which mailbox has no refresh token,
                # what the token endpoint said) stays in this log line: the
                # caller gets a reason, never an account identity.
                log.warning("no usable account: %s", exc)
                raise UpstreamError(503, f"no usable account: {exc}") from exc
            tried.add(token.access_token or token.label)
            current = token.label or token.short

            token.in_flight += 1
            try:
                response = await self._dispatch_attributed(
                    settings.messages_url, payload, _headers(token.access_token), stream, current
                )
            finally:
                token.in_flight -= 1

            if response.status_code in {301, 302, 307, 308}:
                location = response.headers.get("location", "?")
                await response.aclose()
                raise UpstreamError(
                    502,
                    REDIRECT_HINT.format(
                        location=location, region=settings.region, base_url=settings.base_url
                    ),
                )

            if response.status_code == 400:
                # An account may simply lack the requested model, or be out of
                # credits. Both are per-account conditions, so try the next
                # account before surfacing the error; only give up when every
                # account has been tried.
                body = await response.aread()
                await response.aclose()
                err = _error_from(400, body)
                # Name the account on the error itself: raising here skips the
                # attribution below, so the console showed "-" for the one row
                # an operator actually needs to read.
                err.account = current
                retriable = (
                    "unknown model" in err.message.lower() or _quota_exhausted(err.message)
                )
                if retriable and len(tried) < attempts:
                    if _quota_exhausted(err.message):
                        # The daily allowance resets at local midnight, not in
                        # fifteen minutes — park until then and let the sign-in
                        # sweep lift the park once it has topped the account up.
                        token_manager.park(token, 402, err.message, next_day=True)
                        token_manager.note_failure(token, err.message)
                        log.warning(
                            "account %s is out of credits (%s); parked until the daily reset",
                            token.label or token.short, err.message,
                        )
                    else:
                        # A model name is global, not per-account: if three
                        # accounts do not know it, the request is wrong and
                        # walking the rest of the pool just wastes round trips.
                        model_misses += 1
                        log.warning(
                            "account %s lacks this model; trying next",
                            token.label or token.short,
                        )
                        if model_misses >= 3:
                            raise err
                    last = err
                    continue
                raise err

            if response.status_code == 0 or response.status_code >= 500:
                # The request never reached a model: the connection died, or the
                # edge answered 5xx with no body. Neither is the caller's fault
                # nor that account's, so walk the pool instead of handing back a
                # 502 on the first hiccup — a 30-account pool used to surface
                # every transport blip as a hard failure.
                body = await response.aread()
                await response.aclose()
                last = _error_from(response.status_code, body)
                last.account = current
                log.warning(
                    "account %s hit a transport failure (%s); trying next",
                    token.label or token.short, response.status_code,
                )
                continue

            if response.status_code in {401, 402, 403, 429}:
                body = await response.aread()
                await response.aclose()
                last = _error_from(response.status_code, body)
                last.account = current

                # A 401 may just be a rotated token: refresh once, then keep it.
                if response.status_code == 401 and await token_manager.renew(token):
                    retry = await self._dispatch_attributed(
                        settings.messages_url, payload, _headers(token.access_token), stream, current
                    )
                    if retry.status_code == 200:
                        retry.account = token.label or token.short
                        token_manager.note_success(token)
                        return retry
                    body = await retry.aread()
                    await retry.aclose()
                    last = _error_from(retry.status_code, body)
                    last.account = current

                token_manager.park(
                    token, response.status_code, last.message,
                    next_day=response.status_code == 402,
                )
                token_manager.note_failure(token, last.message)
                log.warning(
                    "account %s failed (%s); trying next", token.label or token.short, response.status_code
                )
                continue

            response.account = token.label or token.short
            if response.status_code == 200:
                token_manager.note_success(token)
            return response

        if last is not None:
            last.account = last.account or current
            raise last
        raise UpstreamError(502, "no account could serve the request")

    async def messages(self, payload: dict) -> dict:
        body, _ = await self.messages_ex(payload)
        return body

    async def messages_ex(self, payload: dict) -> tuple[dict, str]:
        """Same as :meth:`messages`, plus the account label that served it."""
        response = await self._send(payload, stream=False)
        account = response.account
        body = await response.aread()
        await response.aclose()
        if response.status_code != 200:
            err = _error_from(response.status_code, body)
            # Without this the console shows "-" for the account that produced
            # the failure, which is the one field that decides what to do next.
            err.account = response.account
            raise err
        try:
            return httpx.Response(200, content=body).json(), account
        except ValueError as exc:  # pragma: no cover - upstream contract change
            raise UpstreamError(502, f"upstream returned non-JSON: {body[:300]!r}") from exc

    async def messages_stream(self, payload: dict) -> UpstreamResponse:
        response = await self._send(payload, stream=True)
        if response.status_code != 200:
            body = await response.aread()
            await response.aclose()
            err = _error_from(response.status_code, body)
            err.account = response.account
            raise err
        return response

    async def mcp(self, tool: str, payload: dict, timeout: float = 120.0,
                  account: str | None = None) -> dict:
        """POST a desktop MCP tool call and return the decoded JSON body.

        Generation tools answer ``200`` with a business ``code`` inside the
        envelope, so a 200 here does not mean success — ``media.call`` owns that
        check.
        """
        body, _ = await self.mcp_ex(tool, payload, timeout, account)
        return body

    async def mcp_ex(self, tool: str, payload: dict, timeout: float = 120.0,
                     account: str | None = None) -> tuple[dict, str]:
        """Same as :meth:`mcp`, but also reports which account served it.

        ``account`` pins the call to one pooled account — needed by the async
        video flow, whose ``task_id`` is only visible to the account that
        submitted it. Any other account answers
        ``404 task was not found or does not belong to the current caller``.
        """
        await self.startup()
        url = f"{settings.mcp_url}/{tool}"

        tried: set[str] = set()
        last: UpstreamError | None = None
        attempts = max(1, len(getattr(token_manager, "_tokens", []) or [1]))
        pinned = token_manager.find(account) if account else None

        for _ in range(attempts + 1):
            token = None
            if pinned is not None:
                key = pinned.access_token or pinned.label
                if key not in tried and pinned.access_token:
                    token = pinned
            if token is None:
                try:
                    token = await token_manager.acquire(exclude=tried)
                except CredentialError as exc:
                    raise UpstreamError(502, f"credential refresh failed: {exc}") from exc
            tried.add(token.access_token or token.label)

            response = await self._dispatch(url, payload, _mcp_headers(token.access_token), False, timeout)
            body = await response.aread()
            status = response.status_code
            location = response.headers.get("location", "?")
            await response.aclose()

            if status in {301, 302, 307, 308}:
                raise UpstreamError(
                    502,
                    REDIRECT_HINT.format(
                        location=location, region=settings.region, base_url=settings.base_url
                    ),
                )

            if status in {401, 402, 403, 429}:
                last = _error_from(status, body)
                if status == 401 and await token_manager.renew(token):
                    retry = await self._dispatch(url, payload, _mcp_headers(token.access_token), False, timeout)
                    body = await retry.aread()
                    status = retry.status_code
                    await retry.aclose()
                if status != 200:
                    token_manager.park(token, status, last.message if last else f"HTTP {status}")
                    log.warning("mcp %s: account %s failed (%s)", tool, token.label or token.short, status)
                    continue

            if status != 200:
                raise _error_from(status, body)

            import json

            try:
                decoded = json.loads(body)
            except ValueError as exc:  # pragma: no cover - upstream contract change
                raise UpstreamError(502, f"upstream returned non-JSON: {body[:300]!r}") from exc
            if not isinstance(decoded, dict):
                raise UpstreamError(502, f"unexpected upstream payload: {body[:300]!r}")

            # Entitlement is per account: a generation tool answers 200 with
            # code 503 ("temporarily unavailable") on an account that is not
            # allowed to use it, while a sibling account succeeds. Rotating here
            # is what makes video work on a pool where only one号 has access.
            code = decoded.get("code")
            if code in ROTATABLE_CODES and tool in ROTATABLE_TOOLS and len(tried) < attempts:
                last = UpstreamError(
                    int(code), decoded.get("message") or f"{tool} unavailable on this account"
                )
                log.warning(
                    "mcp %s: account %s cannot use this tool (%s), trying the next",
                    tool, token.label or token.short, code,
                )
                continue
            return decoded, token.access_token or token.label

        if last is not None:
            last.account = last.account or current
            raise last
        raise UpstreamError(502, "no account could serve the request")


def _error_from(status: int, body: bytes) -> UpstreamError:
    import json

    text = body.decode("utf-8", errors="ignore")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return UpstreamError(status, text[:500] or f"upstream HTTP {status}")

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or text
            return UpstreamError(status, str(message), payload)
        if isinstance(error, str):
            return UpstreamError(status, error, payload)
        message = payload.get("message") or payload.get("errorReason") or text
        return UpstreamError(status, str(message), payload)
    return UpstreamError(status, text[:500], payload)


upstream = Upstream()
