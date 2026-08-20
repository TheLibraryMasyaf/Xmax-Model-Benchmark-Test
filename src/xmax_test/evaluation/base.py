"""Evaluation preprocessor and fusion boundaries."""

from __future__ import annotations

from typing import Any, Protocol


class MediaPreprocessor(Protocol):
    def build(self, request: dict[str, Any]) -> dict[str, Any]:
        """Build versioned frames, event windows, tracks and review sheets."""


class JudgmentFusion(Protocol):
    def fuse(
        self,
        benchmark: dict[str, Any],
        scenario_pack: dict[str, Any],
        test_case: dict[str, Any],
        judgments: list[dict[str, Any]],
        runtime_facts: dict[str, Any],
    ) -> dict[str, Any]:
        """Return an EvaluationResult without reading media or inventing weights."""
