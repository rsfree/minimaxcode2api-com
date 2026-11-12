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


class Upstream:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

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
            "upstream ready base_url=%s proxy=%s region=%s",
            settings.base_url,
            settings.upstream_proxy or "-",
            settings.region,
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

    async def _send(self, payload: dict, stream: bool) -> httpx.Response:
        await self.startup()
        token = await token_manager.get()

        attempt = 0
        while True:
            attempt += 1
            request = self.client.build_request(
                "POST",
                settings.messages_url,
                json=payload,
                headers=_headers(token),
            )
            response = await self.client.send(request, stream=stream)

            if response.status_code in {301, 302, 307, 308}:
                location = response.headers.get("location", "?")
                if stream:
                    await response.aclose()
                else:
                    await response.aread()
                    await response.aclose()
                raise UpstreamError(
                    502,
                    REDIRECT_HINT.format(
                        location=location, region=settings.region, base_url=settings.base_url
                    ),
                )

            if response.status_code == 401 and attempt == 1:
                if stream:
                    await response.aclose()
                else:
                    await response.aread()
                    await response.aclose()
                log.warning("upstream rejected the token; refreshing once")
                try:
                    token = await token_manager.get(force=True)
                except CredentialError as exc:
                    raise UpstreamError(502, f"credential refresh failed: {exc}") from exc
                continue

            return response

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

    async def messages_stream(self, payload: dict) -> httpx.Response:
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
        token = await token_manager.get()
        url = f"{settings.mcp_url}/{tool}"

        attempt = 0
        while True:
            attempt += 1
            request = self.client.build_request(
                "POST",
                url,
                json=payload,
                headers=_mcp_headers(token),
                timeout=timeout,
            )
            response = await self.client.send(request)
            body = await response.aread()
            await response.aclose()

            if response.status_code in {301, 302, 307, 308}:
                raise UpstreamError(
                    502,
                    REDIRECT_HINT.format(
                        location=response.headers.get("location", "?"),
                        region=settings.region,
                        base_url=settings.base_url,
                    ),
                )

            if response.status_code == 401 and attempt == 1:
                log.warning("mcp %s rejected the token; refreshing once", tool)
                try:
                    token = await token_manager.get(force=True)
                except CredentialError as exc:
                    raise UpstreamError(502, f"credential refresh failed: {exc}") from exc
                continue

            if response.status_code != 200:
                raise _error_from(response.status_code, body)

            import json

            try:
                decoded = json.loads(body)
            except ValueError as exc:  # pragma: no cover - upstream contract change
                raise UpstreamError(502, f"upstream returned non-JSON: {body[:300]!r}") from exc
            if not isinstance(decoded, dict):
                raise UpstreamError(502, f"unexpected upstream payload: {body[:300]!r}")
            return decoded


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
