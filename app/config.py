"""Runtime configuration, read once from the environment.

Deliberately dependency-free (plain ``os.environ``) so the container has no
extra moving parts and every knob is visible in ``.env.example``.
"""

from __future__ import annotations

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

    # Gateway side.
    api_keys: list[str] = field(default_factory=list)
    default_max_tokens: int = field(default_factory=lambda: _env_int("DEFAULT_MAX_TOKENS", 8192))
    request_body_limit: int = field(default_factory=lambda: _env_int("REQUEST_BODY_LIMIT", 64 * 1024 * 1024))
    expose_retired_models: bool = field(default_factory=lambda: _env_bool("EXPOSE_RETIRED_MODELS", False))

    def __post_init__(self) -> None:
        self.region = self.region if self.region in REGION_ACCOUNT_BASE else "en"
        self.base_url = (self.base_url or REGION_LLM_BASE[self.region]).rstrip("/")
        self.account_base = (self.account_base or REGION_ACCOUNT_BASE[self.region]).rstrip("/")
        raw_keys = _env("GATEWAY_API_KEY")
        self.api_keys = [part.strip() for part in raw_keys.replace("\n", ",").split(",") if part.strip()]

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


settings = Settings()
