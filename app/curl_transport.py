"""curl-backed transport for egress proxies that reject non-native TLS.

Some local accelerators (the ``go_wails`` family, for instance) inspect the TLS
ClientHello and reset anything that is not a browser or Windows schannel. Python's
OpenSSL cannot look like either, so those requests die with ``WinError 10054``
before a single byte of HTTP is exchanged — while ``curl.exe`` on Windows goes
through because schannel is what the filter expects.

This module posts JSON through the ``curl`` binary instead. It is selected with
``UPSTREAM_TRANSPORT=curl``; ``httpx`` stays the default because it is faster and
supports connection pooling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import AsyncIterator, Iterable

log = logging.getLogger("minimax.curl")

IS_WINDOWS = sys.platform.startswith("win")


def _base_args(url: str, headers: dict[str, str], timeout: float, proxy: str | None, form: bool) -> list[str]:
    args = ["curl", "-sS", "-N", "-D", "-", "--http1.1", "-m", str(max(1, int(timeout)))]
    if IS_WINDOWS:
        # Windows schannel checks revocation over the proxy and fails when the
        # CRL endpoint is unreachable; the handshake itself is still valid.
        args.append("--ssl-no-revoke")
    if proxy:
        args += ["-x", proxy]
    else:
        args += ["--noproxy", "*"]
    args += ["-X", "POST"]
    if not form:
        args += ["-H", "content-type: application/json"]
    for key, value in headers.items():
        args += ["-H", f"{key}: {value}"]
    return args


async def _read_head(stream: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    """Consume the status line and headers that ``-D -`` writes to stdout."""
    status = 0
    headers: dict[str, str] = {}
    while True:
        try:
            raw = await stream.readline()
        except (asyncio.IncompleteReadError, ValueError):
            break
        if not raw:
            break
        line = raw.decode("iso-8859-1").rstrip("\r\n")
        if not line:
            break
        if line.startswith("HTTP/"):
            parts = line.split(" ", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
            headers.clear()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return status, headers


class CurlStream:
    def __init__(self, proc: asyncio.subprocess.Process, status: int, headers: dict[str, str]) -> None:
        self.proc = proc
        self.status = status
        self.headers = headers

    async def read(self) -> bytes:
        return await self.proc.stdout.read()

    async def lines(self) -> AsyncIterator[bytes]:
        assert self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            yield line

    async def close(self) -> None:
        if self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:  # pragma: no cover
                pass
        await self.proc.wait()


async def open_post(
    url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: float = 600.0,
    proxy: str | None = None,
) -> CurlStream:
    args = _base_args(url, headers, timeout, proxy, form=False)
    args += ["--data-binary", json.dumps(payload, ensure_ascii=False), url]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    status, resp_headers = await _read_head(proc.stdout)
    return CurlStream(proc, status, resp_headers)


async def open_form(
    url: str,
    form: dict[str, str],
    timeout: float = 60.0,
    proxy: str | None = None,
    headers: dict[str, str] | None = None,
) -> CurlStream:
    args = _base_args(url, headers or {}, timeout, proxy, form=True)
    for key, value in form.items():
        args += ["--data-urlencode", f"{key}={value}"]
    args.append(url)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    status, resp_headers = await _read_head(proc.stdout)
    return CurlStream(proc, status, resp_headers)


async def post_form(
    url: str,
    form: dict[str, str],
    timeout: float = 60.0,
    proxy: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    stream = await open_form(url, form, timeout, proxy, headers)
    try:
        body = await stream.read()
        code = await stream.proc.wait()
        if stream.status == 0 and code != 0:
            stderr = b""
            if stream.proc.stderr is not None:
                stderr = await stream.proc.stderr.read()
            raise RuntimeError(f"curl failed (exit {code}): {stderr.decode('utf-8', 'ignore')[:300]}")
        return stream.status, body
    finally:
        await stream.close()


async def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: float = 600.0,
    proxy: str | None = None,
) -> tuple[int, bytes]:
    stream = await open_post(url, headers, payload, timeout, proxy)
    try:
        body = await stream.read()
        code = await stream.proc.wait()
        if stream.status == 0 and code != 0:
            stderr = b""
            if stream.proc.stderr is not None:
                stderr = await stream.proc.stderr.read()
            raise RuntimeError(f"curl failed (exit {code}): {stderr.decode('utf-8', 'ignore')[:300]}")
        return stream.status, body
    finally:
        await stream.close()
