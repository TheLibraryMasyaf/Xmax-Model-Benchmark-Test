"""Stable MLLM provider boundary shared by Judges and human normalization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class MlmmResponse:
    """Raw provider response plus the parsed JSON payload."""

    payload: Any
    raw_text: str
    provider_id: str
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class MlmmProvider(Protocol):
    """Provider API; implementations may use a CLI, HTTP API or local service."""

    @property
    def provider_id(self) -> str: ...

    def complete_json(
        self,
        *,
        prompt: str,
        image_paths: list[str],
        output_schema: dict[str, Any],
        media_inputs: list[dict[str, Any]] | None = None,
    ) -> MlmmResponse: ...
