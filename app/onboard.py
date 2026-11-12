"""Import accounts into the pool at runtime — the console's "＋ 添加账号".

The manual path is a shell command on the host::

    python -m app.provision --file accounts.txt --out /data/tokens

That is fine for one operator with SSH, and useless for everyone else. The pool
already hot-reloads on directory changes, so the only missing piece was a way to
*mint* tokens from the browser. This module runs the same flow as the CLI
(password login → device authorise → token exchange) as a background job, writes
one token file per account into ``TOKEN_POOL``, and lets the pool watcher pick
them up within seconds.

Deliberate constraints:

* **One job at a time.** The account host rate-limits the login endpoint; ten
  parallel logins from one IP look exactly like credential stuffing.
* **Passwords never persist.** They are held in memory for the length of the
  job and dropped; the token file keeps access/refresh tokens only.
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

from .account_api import AccountError, provision
from .auth import token_manager
from .provision import pool_dir, record_path, write_record

log = logging.getLogger("minimax.onboard")

MAX_BATCH = 200
DEFAULT_CONCURRENCY = int(os.environ.get("PROVISION_CONCURRENCY", "3") or 3)
START_GAP = 0.4          # seconds between task starts, on top of the semaphore
ACCOUNT_TIMEOUT = 120.0  # one account: login + device code + approve + exchange

# "email<sep>password", where sep is whitespace, comma, semicolon or colon. The
# email half must contain an @ so that a stray prose line is not misread.
LINE_RE = re.compile(r"^\s*([^\s,;:]+@[^\s,;:]+)[\s,;:](\s*)(\S.*?)\s*$")


def parse_accounts(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Split pasted text into ``(email, password)`` pairs plus unparsable lines."""
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
            "invalid": list(job["invalid"]),
            "rows": [dict(row) for row in job["rows"]],
        }

    # -- entry point -------------------------------------------------------
    async def start(self, text: str, concurrency: int | None = None) -> dict:
        accounts, invalid = parse_accounts(text)
        if not accounts:
            raise ValueError("没有解析出「邮箱 密码」行，检查一下格式（每行一个，用空格或 Tab 分隔）")
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
                "invalid": invalid,
                "rows": [
                    {"email": email, "state": "pending", "error": None, "file": None, "updated": False}
                    for email, _ in accounts
                ],
            }
            self._job = job
            self._task = asyncio.create_task(self._run(job, accounts, workers))
            log.info("import job %s started: %d account(s), %d worker(s)", job["id"], len(accounts), workers)
            return self.snapshot()  # type: ignore[return-value]

    # -- worker ------------------------------------------------------------
    async def _run(self, job: dict, accounts: list[tuple[str, str]], workers: int) -> None:
        sem = asyncio.Semaphore(workers)
        directory = pool_dir()
        proxy = None  # the account host shares the gateway's egress; no proxy here

        async def one(row: dict, email: str, password: str) -> None:
            async with sem:
                row["state"] = "running"
                target = record_path(directory, email)
                existed = await asyncio.to_thread(target.exists)
                try:
                    record = await asyncio.wait_for(
                        asyncio.to_thread(provision, email, password, proxy), timeout=ACCOUNT_TIMEOUT
                    )
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
        for row, (email, password) in zip(job["rows"], accounts):
            task = asyncio.create_task(one(row, email, password))
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
    if "password" in lowered or "auth" in lowered or "登录" in text:
        return f"登录被拒：{text[:120]}"
    if "no authorisable group" in lowered:
        return "账号没有可授权的分组（可能被封或未开通）"
    if exc.code is not None:
        return f"[{exc.code}] {text[:120]}"
    return text[:140]


importer = Importer()
