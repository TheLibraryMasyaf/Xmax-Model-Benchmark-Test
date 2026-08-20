"""Human-signal normalization and learning route boundaries."""

from __future__ import annotations

from typing import Any, Protocol


class HumanSignalNormalizer(Protocol):
    def normalize(
        self, raw_signal: dict[str, Any], benchmark: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a structured signal without mutating the raw source."""


class LearningRouter(Protocol):
    def route(self, signal: dict[str, Any]) -> list[dict[str, Any]]:
        """Create CV, MLLM or fusion learning candidates."""

