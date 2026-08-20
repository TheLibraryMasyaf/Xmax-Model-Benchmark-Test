"""Shared offline/realtime generation adapter boundary."""

from __future__ import annotations

from typing import Any, Protocol


class GenerationAdapter(Protocol):
    mode: str

    def prepare(self, case: dict[str, Any]) -> dict[str, Any]:
        """Resolve validated assets and mode-specific settings."""

    def execute(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """Create one append-only GenerationRun."""

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Request cancellation without deleting run history."""

