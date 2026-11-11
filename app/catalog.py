"""Model catalogue for the managed MiniMax Code channel.

Values are taken from the desktop client's shipped catalogue
(``models-dev-catalog.json``) and its ``config.yaml`` provider block. Live
availability was verified against the upstream on 2026-11-11; the two retired
ids are kept only for documentation and can be exposed with
``EXPOSE_RETIRED_MODELS=1``.
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
    retired: bool = False

    def to_openai(self) -> dict:
        return {
            "id": self.id,
            "object": "model",
            "created": 1762732800,
            "owned_by": self.owned_by,
        }

    def to_meta(self) -> dict:
        return {
            "id": self.id,
            "object": "model",
            "owned_by": self.owned_by,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output,
            "capabilities": {
                "vision": self.vision,
                "tool_calls": True,
                "reasoning": True,
                "thinking": self.thinking,
            },
        }


MODELS: tuple[Model, ...] = (
    Model(
        id="MiniMax-M3.1-Flash",
        owned_by="minimax",
        context_window=1_000_000,
        max_output=524_288,
        vision=True,
        thinking="forced_on",
    ),
    Model(
        id="MiniMax-M3",
        owned_by="minimax",
        context_window=1_000_000,
        max_output=524_288,
        vision=True,
        thinking="switchable",
    ),
    Model(
        id="MiniMax-M2.7",
        owned_by="minimax",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
    ),
    Model(
        id="MiniMax-M2.7-highspeed",
        owned_by="minimax",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
    ),
    Model(
        id="MiniMax-M2.5",
        owned_by="minimax",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
    ),
    Model(
        id="MiniMax-M2.5-highspeed",
        owned_by="minimax",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
    ),
    Model(
        id="MiniMax-M2",
        owned_by="minimax",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
        retired=True,
    ),
    Model(
        id="MiniMax-M2.1",
        owned_by="minimax",
        context_window=204_800,
        max_output=131_072,
        vision=False,
        thinking="switchable",
        retired=True,
    ),
)

DEFAULT_MODEL = "MiniMax-M3.1-Flash"

_BY_ID = {model.id: model for model in MODELS}
_BY_LOWER = {model.id.lower(): model for model in MODELS}

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
