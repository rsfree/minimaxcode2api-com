"""Runtime configuration, read once from the environment.

Deliberately dependency-free (plain ``os.environ``) so the container has no
extra moving parts and every knob is visible in ``.env.example``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REGION_ACCOUNT_BASE = {
    "en": "https://account.minimax.io",
    "cn": "https://account.minimax.cn",
}

REGION_LLM_BASE = {
    "en": "https://agent.minimax.io/mavis/api/v1/llm/v1",
    "cn": "https://agent.minimax.cn/mavis/api/v1/llm/v1",
}

# The multimodal tools (image / video / speech / music / ASR) live on the same
# managed host but under the desktop gateway's MCP proxy, not the LLM path.
REGION_MCP_BASE = {
    "en": "https://agent.minimax.io/mavis/api/v1/mcp",
    "cn": "https://agent.minimax.cn/mavis/api/v1/mcp",
}

# The managed desktop channel authenticates with the OAuth access token in
# `Authorization` and a fixed placeholder in `x-api-key`. Both must be present:
# the upstream rejects a missing `x-api-key` with "token is required".
API_KEY_PLACEHOLDER = "sk-xxx"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Knobs the console may change without recreating the container.
#
# Everything here is read through ``settings.<name>`` at the moment it is used,
# so assigning the attribute is the whole "apply" step. What is *not* here
# (region, base_url, proxy, API keys, body limit, port) is baked into a client,
# a route table or the auth header of every request — changing those at runtime
# would leave half the process on the old value, which is worse than a restart.
EDITABLE: dict[str, dict] = {
    "retry_attempts": {
        "kind": "int", "min": 0, "max": 50,
        "note": "一次调用最多重试几次（换号或同号重试）。0 = 只试一次",
    },
    "request_timeout": {
        "kind": "float", "min": 5, "max": 3600,
        "note": "上游超时（秒），改完立即重建连接池",
    },
    "default_max_tokens": {
        "kind": "int", "min": 1, "max": 64000,
        "note": "客户端没给 max_tokens 时的兜底",
    },
    "upstream_transport": {
        "kind": "choice", "choices": ["auto", "httpx", "curl"],
        "note": "出站传输。auto = httpx 失败后回落 curl",
    },
    "pool_watch": {
        "kind": "float", "min": 0, "max": 3600,
        "note": "号池目录扫描间隔（秒），0 关闭",
    },
    "probe_auto": {
        "kind": "bool",
        "note": "面板探针自动刷新",
    },
    "probe_interval": {
        "kind": "float", "min": 0, "max": 86400,
        "note": "探针间隔（秒），0 = 只手动",
    },
    "signin_auto": {
        "kind": "bool",
        "note": "每日自动签到",
    },
    "signin_interval_hours": {
        "kind": "float", "min": 1, "max": 168,
        "note": "签到巡检间隔（小时）",
    },
    "metrics_retention_days": {
        "kind": "float", "min": 0.5, "max": 365,
        "note": "请求历史保留天数",
    },
    "expose_retired_models": {
        "kind": "bool",
        "note": "在 /v1/models 里展示已下线模型",
    },
}


def _coerce(spec: dict, value: object) -> object:
    """Turn a JSON/query value into the field's type, or explain why not."""
    kind = spec["kind"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "开"}:
            return True
        if text in {"0", "false", "no", "off", "关"}:
            return False
        raise ValueError("期望 true / false")
    if kind == "choice":
        text = str(value).strip().lower()
        choices = spec["choices"]
        if text not in choices:
            raise ValueError("只能是 " + " / ".join(choices))
        return text
    cast = int if kind == "int" else float
    try:
        number = cast(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else cast(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("期望整数" if kind == "int" else "期望数字") from None
    low, high = spec["min"], spec["max"]
    if not low <= number <= high:
        raise ValueError(f"范围 {low}..{high}")
    return number


@dataclass
class Settings:
    region: str = field(default_factory=lambda: _env("MINIMAX_REGION", "en").lower())

    # Upstream LLM endpoint (Anthropic Messages wire format).
    base_url: str = field(default_factory=lambda: _env("MINIMAX_BASE_URL"))
    mcp_base: str = field(default_factory=lambda: _env("MINIMAX_MCP_BASE"))
    account_base: str = field(default_factory=lambda: _env("MINIMAX_ACCOUNT_BASE"))
    request_timeout: float = field(default_factory=lambda: float(_env("UPSTREAM_TIMEOUT", "600") or 600))

    # Egress. Required on networks whose IP gets a 307 to the legacy CN origin.
    upstream_proxy: str = field(default_factory=lambda: _env("UPSTREAM_PROXY"))
    # httpx | curl | auto. ``curl`` is for egress proxies that reset Python's
    # TLS handshake but accept the system curl (Windows schannel).
    upstream_transport: str = field(default_factory=lambda: _env("UPSTREAM_TRANSPORT", "auto").lower())

    # Credentials, in priority order.
    access_token: str = field(default_factory=lambda: _env("MINIMAX_ACCESS_TOKEN"))
    refresh_token: str = field(default_factory=lambda: _env("MINIMAX_REFRESH_TOKEN"))
    auth_file: str = field(default_factory=lambda: _env("MINIMAX_AUTH_FILE"))
    token_store: str = field(default_factory=lambda: _env("TOKEN_STORE", "/data/token.json"))
    # Directory of ``*.json`` account files (or a single JSON file / array).
    token_pool: str = field(default_factory=lambda: _env("TOKEN_POOL"))
    # Seconds between directory scans. New/removed account files are merged into
    # the live pool without a restart; 0 turns the watcher off.
    pool_watch: float = field(default_factory=lambda: float(_env("TOKEN_POOL_WATCH", "10") or 10))

    # Daily check-in. The reward is the only free top-up; claiming it from the
    # gateway keeps a pool alive without anyone remembering to click.
    signin_auto: bool = field(default_factory=lambda: _env_bool("SIGNIN_AUTO", True))
    signin_timezone: str = field(default_factory=lambda: _env("SIGNIN_TIMEZONE", "Asia/Shanghai"))
    signin_interval_hours: float = field(
        default_factory=lambda: float(_env("SIGNIN_INTERVAL_HOURS", "6") or 6)
    )
    # Console probes (sign-in panel + credit ledger per account). One GET per
    # account per sweep, so the interval is minutes, not seconds. 0 = manual only.
    probe_auto: bool = field(default_factory=lambda: _env_bool("PROBE_AUTO", True))
    probe_interval: float = field(
        default_factory=lambda: float(_env("PROBE_INTERVAL", "300") or 300)
    )

    # Request history. Empty = /data/metrics.db in the container, data/metrics.db
    # elsewhere. Set to "off" to keep the old memory-only behaviour.
    metrics_db: str = field(default_factory=lambda: _env("METRICS_DB", ""))
    metrics_retention_days: float = field(
        default_factory=lambda: float(_env("METRICS_RETENTION_DAYS", "3") or 3)
    )

    # Gateway side.
    api_keys: list[str] = field(default_factory=list)
    default_max_tokens: int = field(default_factory=lambda: _env_int("DEFAULT_MAX_TOKENS", 8192))
    request_body_limit: int = field(default_factory=lambda: _env_int("REQUEST_BODY_LIMIT", 64 * 1024 * 1024))
    expose_retired_models: bool = field(default_factory=lambda: _env_bool("EXPOSE_RETIRED_MODELS", False))

    # How many times one caller request may be retried before the error goes
    # back. The unit is an attempt, not an account: a retry normally means
    # "park this account, take the next", but when the pool has no untried
    # account left a transient failure (5xx, connection reset) reuses the last
    # one instead of giving up. 0 = one attempt, no retry.
    retry_attempts: int = field(default_factory=lambda: _env_int("RETRY_ATTEMPTS", 3))

    def __post_init__(self) -> None:
        self.region = self.region if self.region in REGION_ACCOUNT_BASE else "en"
        self.base_url = (self.base_url or REGION_LLM_BASE[self.region]).rstrip("/")
        self.account_base = (self.account_base or REGION_ACCOUNT_BASE[self.region]).rstrip("/")
        raw_keys = _env("GATEWAY_API_KEY")
        self.api_keys = [part.strip() for part in raw_keys.replace("\n", ",").split(",") if part.strip()]
        # Value from .env/defaults, before any runtime overlay. "Reset this knob"
        # means "go back to this".
        self._baseline: dict[str, object] = {key: getattr(self, key) for key in EDITABLE}
        self._overrides: dict[str, object] = {}

    @property
    def token_endpoint(self) -> str:
        return f"{self.account_base}/oauth2/token"

    @property
    def device_endpoint(self) -> str:
        return f"{self.account_base}/oauth2/device/code"

    @property
    def messages_url(self) -> str:
        return f"{self.base_url}/messages"

    @property
    def mcp_url(self) -> str:
        """Base URL of the desktop MCP proxy (image / video / speech tools)."""
        if self.mcp_base:
            return self.mcp_base.rstrip("/")
        if self.base_url.endswith("/llm/v1"):
            return f"{self.base_url[: -len('/llm/v1')]}/mcp"
        return REGION_MCP_BASE[self.region]

    def default_auth_file(self) -> Path | None:
        """Locate the desktop client's credential file for the active region."""
        if self.auth_file:
            return Path(self.auth_file).expanduser()
        try:
            home = Path.home()
        except (RuntimeError, KeyError):
            return None
        candidate = (
            home
            / ".minimax"
            / "auth"
            / "prod"
            / self.region
            / "mcode-public"
            / "auth.json"
        )
        return candidate if candidate.exists() else None

    # -- runtime overrides -------------------------------------------------
    def runtime_path(self) -> Path:
        """Where the console persists what it changed.

        ``/data`` is the volume that survives a container recreate; on a laptop
        it is the repository's ``data/``. ``RUNTIME_CONFIG`` overrides both.
        """
        raw = _env("RUNTIME_CONFIG")
        if raw:
            return Path(raw)
        root = Path("/data")
        return (root if root.is_dir() else Path("data")) / "runtime.json"

    def load_runtime(self) -> list[str]:
        """Re-apply the last saved overrides. Returns notes for the operator."""
        path = self.runtime_path()
        try:
            raw = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            return [f"{path} 读取失败（{type(exc).__name__}: {exc}），本次按 .env 运行"]
        if not isinstance(raw, dict):
            return [f"{path} 顶层不是对象，已忽略"]
        applied, rejected = self.apply_runtime(raw, persist=False)
        notes = [f"{path} 里 {key} 未生效：{why}" for key, why in rejected]
        if applied:
            notes.append(f"从 {path} 恢复了 {len(applied)} 项运行配置")
        return notes

    def apply_runtime(self, updates: dict, persist: bool = True) -> tuple[dict, list[tuple[str, str]]]:
        """Validate and assign. Returns ``(applied, [(key, why_rejected)])``.

        ``None`` or ``""`` means "go back to the .env value", which is the only
        way to undo an override without editing the file by hand.
        """
        applied: dict[str, object] = {}
        rejected: list[tuple[str, str]] = []
        for key, value in (updates or {}).items():
            spec = EDITABLE.get(key)
            if spec is None:
                rejected.append((key, "不可运行期修改（改 .env 后重建容器）"))
                continue
            if value is None or value == "":
                self._overrides.pop(key, None)
                setattr(self, key, self._baseline[key])
                applied[key] = self._baseline[key]
                continue
            try:
                coerced = _coerce(spec, value)
            except ValueError as exc:
                rejected.append((key, str(exc)))
                continue
            setattr(self, key, coerced)
            self._overrides[key] = coerced
            applied[key] = coerced
        if persist and applied:
            try:
                self._save_runtime()
            except OSError as exc:
                rejected.append(("runtime.json", f"写入失败：{exc}"))
        return applied, rejected

    def _save_runtime(self) -> None:
        path = self.runtime_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self._overrides, ensure_ascii=False, indent=2) + "\n", "utf-8")
        tmp.replace(path)

    def editable_state(self) -> dict:
        """Everything the console needs to draw the form."""
        out: dict[str, dict] = {}
        for key, spec in EDITABLE.items():
            row = {
                "value": getattr(self, key),
                "baseline": self._baseline[key],
                "source": "runtime" if key in self._overrides else "env",
                "note": spec.get("note", ""),
                "kind": spec["kind"],
            }
            for extra in ("min", "max", "choices"):
                if extra in spec:
                    row[extra] = spec[extra]
            out[key] = row
        return out


settings = Settings()

# Notes from replaying the saved overrides; the lifespan logs them, and
# /admin/config shows them, so a broken runtime.json is visible instead of silent.
RUNTIME_NOTES: list[str] = settings.load_runtime()
