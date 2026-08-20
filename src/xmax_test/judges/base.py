"""Judge plugin boundary used by CV and non-Codex adapters."""

from __future__ import annotations

from typing import Any, Protocol


class JudgePlugin(Protocol):
    def manifest(self) -> dict[str, Any]:
        """Return a versioned manifest matching judge-manifest.schema.json."""

    def evaluate(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Return judgments matching judgment.schema.json."""


class TrainerPlugin(Protocol):
    def train(self, dataset_uri: str, base_version: str | None = None) -> dict[str, Any]:
        """Create a challenger artifact; it must not mutate the champion."""

    def validate(self, artifact_uri: str, holdout_uri: str) -> dict[str, Any]:
        """Return a validation report used by the release gate."""

