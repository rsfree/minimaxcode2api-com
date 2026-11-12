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


class UpstreamError(RuntimeError):
    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload


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
        self._force_curl = settings.upstream_transport == "curl"

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

    async def _dispatch(
        self,
        url: str,
        payload: dict,
        headers: dict[str, str],
        stream: bool,
        timeout: float | None = None,
    ) -> UpstreamResponse:
        """POST through httpx, falling back to curl when the egress rejects OpenSSL."""
        if not self._force_curl:
            try:
                kwargs = {"timeout": timeout} if timeout else {}
                request = self.client.build_request("POST", url, json=payload, headers=headers, **kwargs)
                response = await self.client.send(request, stream=stream)
                return UpstreamResponse(httpx_response=response)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                if settings.upstream_transport == "httpx":
                    raise
                log.warning(
                    "httpx egress failed (%s); switching to the curl transport", type(exc).__name__
                )
                self._force_curl = True
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

        for _ in range(attempts + 1):
            try:
                token = await token_manager.acquire(exclude=tried)
            except CredentialError as exc:
                raise UpstreamError(502, f"credential refresh failed: {exc}") from exc
            tried.add(token.access_token or token.label)

            response = await self._dispatch(
                settings.messages_url, payload, _headers(token.access_token), stream
            )

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
                # An account may simply lack the requested model. Try the next
                # account before surfacing the error; only give up when every
                # account has been tried.
                body = await response.aread()
                await response.aclose()
                err = _error_from(400, body)
                if "unknown model" in err.message.lower() and len(tried) < attempts:
                    log.warning(
                        "account %s lacks this model; trying next", token.label or token.short
                    )
                    last = err
                    continue
                raise err

            if response.status_code in {401, 402, 403, 429}:
                body = await response.aread()
                await response.aclose()
                last = _error_from(response.status_code, body)

                # A 401 may just be a rotated token: refresh once, then keep it.
                if response.status_code == 401 and await token_manager.renew(token):
                    retry = await self._dispatch(
                        settings.messages_url, payload, _headers(token.access_token), stream
                    )
                    if retry.status_code == 200:
                        return retry
                    body = await retry.aread()
                    await retry.aclose()
                    last = _error_from(retry.status_code, body)

                token_manager.park(token, response.status_code, last.message)
                log.warning(
                    "account %s failed (%s); trying next", token.label or token.short, response.status_code
                )
                continue

            return response

        raise last or UpstreamError(502, "no account could serve the request")

    async def messages(self, payload: dict) -> dict:
        response = await self._send(payload, stream=False)
        body = await response.aread()
        await response.aclose()
        if response.status_code != 200:
            raise _error_from(response.status_code, body)
        try:
            return httpx.Response(200, content=body).json()
        except ValueError as exc:  # pragma: no cover - upstream contract change
            raise UpstreamError(502, f"upstream returned non-JSON: {body[:300]!r}") from exc

    async def messages_stream(self, payload: dict) -> UpstreamResponse:
        response = await self._send(payload, stream=True)
        if response.status_code != 200:
            body = await response.aread()
            await response.aclose()
            raise _error_from(response.status_code, body)
        return response

    async def mcp(self, tool: str, payload: dict, timeout: float = 120.0) -> dict:
        """POST a desktop MCP tool call and return the decoded JSON body.

        Generation tools answer ``200`` with a business ``code`` inside the
        envelope, so a 200 here does not mean success — ``media.call`` owns that
        check.
        """
        await self.startup()
        url = f"{settings.mcp_url}/{tool}"

        tried: set[str] = set()
        last: UpstreamError | None = None
        attempts = max(1, len(getattr(token_manager, "_tokens", []) or [1]))

        for _ in range(attempts + 1):
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
            return decoded

        raise last or UpstreamError(502, "no account could serve the request")


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
