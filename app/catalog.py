"""Model catalogue for the managed MiniMax Code channel.

The authoritative source is the channel's own provider config::

    GET https://agent.minimax.io/mavis/api/v1/models
    -> providers[0].config.whitelist / model_order / models

At the time of writing it whitelists exactly ``MiniMax-M3.1-Flash`` and
``MiniMax-M3``, both 512K context / 128K output, text + image + video input.
The M2.5/M2.7 family is no longer in that whitelist but the upstream still
serves it, so it stays listed (``legacy=True``) to keep existing clients
working. Retired ids (M2, M2.1) answer with an empty completion and are only
exposed with ``EXPOSE_RETIRED_MODELS=1``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import settings


@dataclass(frozen=True)
class Model:
    id: str
    owned_by: str
    context_window: int
    max_output: int
    vision: bool
    thinking: str
    modalities: tuple[str, ...] = ("text",)
    legacy: bool = False
    retired: bool = False
    kind: str = "chat"

    @property
    def type(self) -> str:
        return self.kind

    def to_openai(self) -> dict:
        body = {
            "id": self.id,
            "object": "model",
            "created": 1762732800,
            "owned_by": self.owned_by,
            "type": self.kind,
            "capabilities": {
                "vision": self.vision,
                "tool_calls": self.kind == "chat",
                "reasoning": self.kind == "chat",
                "thinking": self.thinking,
            },
            "modalities": list(self.modalities),
            "legacy": self.legacy,
        }
        if self.context_window:
            body["context_length"] = self.context_window
            body["max_output_tokens"] = self.max_output
        return body

    def to_meta(self) -> dict:
        return self.to_openai()


_CHAT = dict(owned_by="minimax", kind="chat")

MODELS: tuple[Model, ...] = (
    Model(
        id="MiniMax-M3.1-Flash",
        context_window=512_000,
        max_output=128_000,
        vision=True,
        thinking="forced_on",
        modalities=("text", "image", "video"),
        **_CHAT,
    ),
    Model(
        id="MiniMax-M3",
        context_window=512_000,
        max_output=128_000,
        vision=True,
        thinking="switchable",
        modalities=("text", "image", "video"),
        **_CHAT,
    ),
    Model(
        id="MiniMax-M2.7",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
        legacy=True,
        **_CHAT,
    ),
    Model(
        id="MiniMax-M2.7-highspeed",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
        legacy=True,
        **_CHAT,
    ),
    Model(
        id="MiniMax-M2.5",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
        legacy=True,
        **_CHAT,
    ),
    Model(
        id="MiniMax-M2.5-highspeed",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
        legacy=True,
        **_CHAT,
    ),
    Model(
        id="MiniMax-M2",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
        legacy=True,
        **_CHAT,
    ),
    Model(
        id="MiniMax-M2.1",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
        legacy=True,
        **_CHAT,
    ),
)

# Multimodal models are not chat models: they belong to their own endpoints.
# They are part of /v1/models because clients enumerate one list, and the
# `type` field keeps them apart from the chat picker.
MEDIA_MODELS: tuple[Model, ...] = (
    Model(
        id="minimax-image",
        owned_by="minimax",
        context_window=0,
        max_output=0,
        vision=False,
        thinking="off",
        kind="image",
    ),
    Model(
        id="minimax-speech",
        owned_by="minimax",
        context_window=0,
        max_output=0,
        vision=False,
        thinking="off",
        kind="audio",
    ),
    Model(
        id="minimax-music",
        owned_by="minimax",
        context_window=0,
        max_output=0,
        vision=False,
        thinking="off",
        kind="audio",
    ),
    Model(
        id="MiniMax-H3",
        owned_by="minimax",
        context_window=0,
        max_output=0,
        vision=True,
        thinking="off",
        kind="video",
    ),
    Model(
        id="MiniMax-Hailuo-2.3",
        owned_by="minimax",
        context_window=0,
        max_output=0,
        vision=True,
        thinking="off",
        kind="video",
    ),
)

DEFAULT_MODEL = "MiniMax-M3.1-Flash"

_BY_ID = {model.id: model for model in (*MODELS, *MEDIA_MODELS)}
_BY_LOWER = {model.id.lower(): model for model in (*MODELS, *MEDIA_MODELS)}

# Friendly aliases so existing clients keep working after a model retires.
ALIASES = {
    "m3": "MiniMax-M3",
    "m3.1": "MiniMax-M3.1-Flash",
    "m3.1-flash": "MiniMax-M3.1-Flash",
    "m2.7": "MiniMax-M2.7",
    "m2.5": "MiniMax-M2.5",
    "minimax": DEFAULT_MODEL,
    "default": DEFAULT_MODEL,
}


def visible_models() -> list[Model]:
    if settings.expose_retired_models:
        return list(MODELS)
    return [model for model in MODELS if not model.retired]


def visible_media_models() -> list[Model]:
    return list(MEDIA_MODELS)


def resolve_model(name: str | None) -> str:
    """Map a client-supplied model name onto a real upstream id."""
    if not name:
        return DEFAULT_MODEL
    candidate = name.strip()
    if candidate in _BY_ID:
        return candidate
    lowered = candidate.lower()
    if lowered in _BY_LOWER:
        return _BY_LOWER[lowered].id
    if lowered in ALIASES:
        return ALIASES[lowered]
    # Unknown ids are forwarded unchanged: the upstream gives a precise error,
    # and this keeps newly released models usable without a gateway rebuild.
    return candidate


def get_model(name: str) -> Model | None:
    return _BY_ID.get(resolve_model(name))
