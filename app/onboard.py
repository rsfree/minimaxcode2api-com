"""Import accounts into the pool at runtime — the console's "＋ 添加账号".

The manual path is a shell command on the host::

    python -m app.provision --file accounts.txt --out /data/tokens

That is fine for one operator with SSH, and useless for everyone else. The pool
already hot-reloads on directory changes, so the only missing piece was a way to
*mint* tokens from the browser. This module runs the same flow as the CLI
(password login → device authorise → token exchange) as a background job, writes
one token file per account into ``TOKEN_POOL``, and lets the pool watcher pick
them up within seconds.

Two input modes, same pipeline:

* ``password`` — ``email password`` per line, the original path;
* ``cookie`` — ``label cookie`` per line, where the cookie is a browser session
  copied out of DevTools. No password, and it also works for accounts whose
  password nobody kept. The cookie is stored in the record, so a rejected refresh
  token can be re-minted later without a human.

Deliberate constraints:

* **One job at a time.** The account host rate-limits the login endpoint; ten
  parallel logins from one IP look exactly like credential stuffing. The same
  applies to the device-authorise step a cookie import drives.
* **Passwords never persist.** They are held in memory for the length of the
  job and dropped; the token file keeps access/refresh tokens only. A session
  cookie *is* written, because re-minting needs it — see the module docstring in
  ``account_api`` for why that is the only durable piece.
* **Bounded batch.** A pasted list is capped, because a 2000-line paste is a
  mistake far more often than it is a plan.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from pathlib import Path

from .account_api import AccountError, provision, provision_with_cookies
from .auth import token_manager
from .config import settings
from .provision import pool_dir, record_path, write_record

log = logging.getLogger("minimax.onboard")

MAX_BATCH = 200
DEFAULT_CONCURRENCY = int(os.environ.get("PROVISION_CONCURRENCY", "3") or 3)
START_GAP = 0.4          # seconds between task starts, on top of the semaphore
ACCOUNT_TIMEOUT = 120.0  # one account: login + device code + approve + exchange

MODE_PASSWORD = "password"
MODE_COOKIE = "cookie"

# "email<sep>password", where sep is whitespace, comma, semicolon or colon. The
# email half must contain an @ so that a stray prose line is not misread.
LINE_RE = re.compile(r"^\s*([^\s,;:]+@[^\s,;:]+)[\s,;:](\s*)(\S.*?)\s*$")


def parse_accounts(text: str, mode: str = MODE_PASSWORD) -> tuple[list[tuple[str, str]], list[str]]:
    """Split pasted text into ``(label, secret)`` pairs plus unparsable lines."""
    if mode == MODE_COOKIE:
        return _parse_cookies(text)
    rows: list[tuple[str, str]] = []
    bad: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE_RE.match(line)
        if not match:
            bad.append(line[:80])
            continue
        email = match.group(1).strip().lower()
        password = match.group(3).strip()
        if not password or email in seen:
            if not password:
                bad.append(line[:80])
            continue
        seen.add(email)
        rows.append((email, password))
    return rows, bad


def _parse_cookies(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """``label<whitespace>cookie`` — split on the *first* run of whitespace only.

    A Cookie header is full of spaces and semicolons, so anything cleverer than
    "label, then the rest of the line" cuts valid sessions in half. A line whose
    label is the word ``cookie`` is almost certainly a header pasted without its
    label, and saying so beats importing an account named "Cookie:".
    """
    rows: list[tuple[str, str]] = []
    bad: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            bad.append(line[:80])
            continue
        label = parts[0].strip().lower().rstrip(":")
        cookie = parts[1].strip()
        if label in {"cookie", "set-cookie", "session"} or "=" not in cookie:
            bad.append(line[:80])
            continue
        if label in seen:
            continue
        seen.add(label)
        # Mailboxes fold to lower case so a re-import lands on the same file the
        # password path would have written; free-form labels keep their spelling.
        rows.append((label if "@" in label else parts[0].strip(), cookie))
    return rows, bad


class Importer:
    """Runs one import job and remembers how the last one went."""

    def __init__(self) -> None:
        self._job: dict | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # -- state -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    def snapshot(self) -> dict | None:
        if not self._job:
            return None
        job = self._job
        return {
            "id": job["id"],
            "state": job["state"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "total": job["total"],
            "done": job["done"],
            "ok": job["ok"],
            "failed": job["failed"],
            "updated": job["updated"],
            "concurrency": job["concurrency"],
            "pool_dir": job["pool_dir"],
            "mode": job.get("mode", MODE_PASSWORD),
            "invalid": list(job["invalid"]),
            "rows": [dict(row) for row in job["rows"]],
        }

    # -- entry point -------------------------------------------------------
    async def start(self, text: str, concurrency: int | None = None,
                    mode: str = MODE_PASSWORD) -> dict:
        mode = (mode or MODE_PASSWORD).strip().lower()
        if mode not in {MODE_PASSWORD, MODE_COOKIE}:
            raise ValueError(f"未知导入方式：{mode}（只能是 password 或 cookie）")
        accounts, invalid = parse_accounts(text, mode)
        if not accounts:
            raise ValueError(
                "没有解析出「邮箱 Cookie」行，检查一下格式（每行一个，邮箱在前，Cookie 请求头在后）"
                if mode == MODE_COOKIE else
                "没有解析出「邮箱 密码」行，检查一下格式（每行一个，用空格或 Tab 分隔）"
            )
        if len(accounts) > MAX_BATCH:
            raise ValueError(f"一次最多 {MAX_BATCH} 个账号，这批有 {len(accounts)} 个")

        async with self._lock:
            if self.running:
                raise RuntimeError("已有导入任务在跑，等它结束再提交")
            workers = max(1, min(int(concurrency or DEFAULT_CONCURRENCY), 8))
            job = {
                "id": uuid.uuid4().hex[:8],
                "state": "running",
                "started_at": time.time(),
                "finished_at": None,
                "total": len(accounts),
                "done": 0,
                "ok": 0,
                "failed": 0,
                "updated": 0,
                "concurrency": workers,
                "pool_dir": str(pool_dir()),
                "mode": mode,
                "invalid": invalid,
                "rows": [
                    {"label": label, "email": label, "state": "pending",
                     "error": None, "file": None, "updated": False}
                    for label, _ in accounts
                ],
            }
            self._job = job
            self._task = asyncio.create_task(self._run(job, accounts, workers))
            log.info("import job %s started: %d account(s), %d worker(s), mode=%s",
                     job["id"], len(accounts), workers, mode)
            return self.snapshot()  # type: ignore[return-value]

    # -- worker ------------------------------------------------------------
    async def _run(self, job: dict, accounts: list[tuple[str, str]], workers: int) -> None:
        sem = asyncio.Semaphore(workers)
        directory = pool_dir()
        proxy = None  # the account host shares the gateway's egress; no proxy here
        mode = job.get("mode", MODE_PASSWORD)
        region = settings.region

        async def mint(label: str, secret: str) -> dict:
            # Keyword arguments on purpose: this is the seam tests substitute, and
            # a positional call would break every existing fake the moment one
            # more parameter is added.
            if mode == MODE_COOKIE:
                return await asyncio.to_thread(
                    provision_with_cookies, secret, email=label, region=region, proxy=proxy
                )
            return await asyncio.to_thread(provision, label, secret, proxy=proxy, region=region)

        async def one(row: dict, label: str, secret: str) -> None:
            async with sem:
                row["state"] = "running"
                target = record_path(directory, label)
                existed = await asyncio.to_thread(target.exists)
                try:
                    record = await asyncio.wait_for(mint(label, secret), timeout=ACCOUNT_TIMEOUT)
                    path = await asyncio.to_thread(write_record, directory, record)
                    row["state"] = "updated" if existed else "ok"
                    row["file"] = path.name
                    row["updated"] = existed
                    job["ok"] += 1
                    if existed:
                        job["updated"] += 1
                    # Surface the account immediately instead of waiting for the
                    # watcher tick; both paths are idempotent.
                    await token_manager.reload()
                except asyncio.TimeoutError:
                    row["state"] = "fail"
                    row["error"] = f"超时（{int(ACCOUNT_TIMEOUT)}s）"
                    job["failed"] += 1
                except AccountError as exc:
                    row["state"] = "fail"
                    row["error"] = _reason(exc)
                    job["failed"] += 1
                except Exception as exc:  # noqa: BLE001 - one account must not sink the batch
                    row["state"] = "fail"
                    row["error"] = f"{type(exc).__name__}: {exc}"[:160]
                    job["failed"] += 1
                finally:
                    job["done"] += 1

        tasks = []
        for row, (label, secret) in zip(job["rows"], accounts):
            task = asyncio.create_task(one(row, label, secret))
            tasks.append(task)
            await asyncio.sleep(START_GAP)
        try:
            await asyncio.gather(*tasks)
        finally:
            await token_manager.reload()
            job["state"] = "done"
            job["finished_at"] = time.time()
            log.info(
                "import job %s finished: %d ok (%d refreshed), %d failed in %.0fs",
                job["id"], job["ok"], job["updated"], job["failed"], job["finished_at"] - job["started_at"],
            )


def _reason(exc: AccountError) -> str:
    """Turn an account-service error into one operator-readable line."""
    text = str(exc).strip() or "未知错误"
    lowered = text.lower()
    # Cookie-mode failures get their own wording: "登录被拒" sends an operator
    # looking for a password that this path never used.
    if "session 没被识别" in text or "session 被拒绝" in text:
        return f"Cookie 无效或已过期：{text[:140]}"
    if "cookie 里没有解析出" in text:
        return "Cookie 里没有 name=value，检查是否复制了完整请求头"
    if "产品接口用不了" in text:
        return f"换到了 token 但用不了：{text[:140]}"
    if "password" in lowered or "auth" in lowered or "登录" in text:
        return f"登录被拒：{text[:120]}"
    if "no authorisable group" in lowered:
        return "账号没有可授权的分组（可能被封或未开通）"
    if exc.code is not None:
        return f"[{exc.code}] {text[:120]}"
    return text[:140]


importer = Importer()
