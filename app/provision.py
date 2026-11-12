"""Provision the account pool from email/password credentials.

The managed MiniMax Code channel mints an OAuth token per account. This module
turns a list of accounts into the JSON files ``TOKEN_POOL`` reads, so the gateway
can rotate between them.

Usage::

    # one account
    python -m app.provision --email someone@mail.example --password 'secret'

    # a file of "email<tab>password" or "email password" lines
    python -m app.provision --file accounts.txt

    # refresh every stored token now
    python -m app.provision --refresh

Environment::

    TOKEN_POOL     directory to write (default ./data/tokens)
    UPSTREAM_PROXY egress proxy, required on geo-redirected networks
    PROVISION_DELAY  seconds between accounts (default 1.5)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from secrets import token_hex

from .account_api import AccountError, provision, refresh
from .config import settings


def pool_dir() -> Path:
    return Path(settings.token_pool or "data/tokens").expanduser()


def record_path(directory: Path, email: str) -> Path:
    """Where one account's token file lives. Shared with the console importer."""
    stem = (email or "account").split("@")[0]
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)
    return directory / f"{stem}.json"


def write_record(directory: Path, record: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = record_path(directory, record.get("email") or record.get("username") or "")
    payload = {k: v for k, v in record.items() if k != "password"}
    # Atomic swap: the pool watcher stats mtimes, and a half-written file would
    # make it drop the account on the next reload. The temp name is unique
    # because the gateway writes rotated tokens into the same directory.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{token_hex(4)}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


# Back-compat aliases for the CLI paths below.
_pool_dir = pool_dir
_write = write_record


def _read_accounts(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split(None, 1)
        if len(parts) < 2:
            continue
        rows.append((parts[0], parts[1].strip()))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision MiniMax Code accounts into the gateway pool")
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument("--file", help="list of email/password lines")
    parser.add_argument("--refresh", action="store_true", help="refresh every stored token")
    parser.add_argument("--out", help="pool directory (default TOKEN_POOL or ./data/tokens)")
    parser.add_argument("--proxy", default=settings.upstream_proxy or None)
    args = parser.parse_args(argv)

    directory = Path(args.out).expanduser() if args.out else _pool_dir()
    delay = float(os.environ.get("PROVISION_DELAY", "1.5"))

    if args.refresh:
        files = sorted(p for p in directory.glob("*.json") if p.name != "index.json")
        if not files:
            print(f"no token files in {directory}", file=sys.stderr)
            return 2
        ok = 0
        for path in files:
            record = json.loads(path.read_text(encoding="utf-8"))
            label = record.get("email") or path.stem
            try:
                updated = refresh(record, proxy=args.proxy)
            except AccountError as exc:
                print(f"  FAIL {label}: {exc}")
                continue
            path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
            ok += 1
            print(f"  refreshed {label}")
            time.sleep(delay)
        print(f"\n{ok}/{len(files)} refreshed")
        return 0 if ok else 1

    accounts: list[tuple[str, str]] = []
    if args.file:
        accounts += _read_accounts(Path(args.file))
    if args.email and args.password:
        accounts.append((args.email, args.password))
    if not accounts:
        parser.error("give --email/--password, --file, or --refresh")

    ok = 0
    for index, (email, password) in enumerate(accounts, 1):
        print(f"[{index}/{len(accounts)}] {email}", end="", flush=True)
        try:
            record = provision(email, password, proxy=args.proxy)
        except AccountError as exc:
            print(f"  FAIL {exc}")
            continue
        path = _write(directory, record)
        ok += 1
        print(f"  OK  {path.name}  access={record['accessToken'][:14]}…")
        time.sleep(delay)

    print(f"\n{ok}/{len(accounts)} provisioned into {directory}")
    print(f"Run the gateway with TOKEN_POOL={directory}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
