"""Test-plan builder boundary."""

from __future__ import annotations

from typing import Any, Protocol


class TestPlanBuilder(Protocol):
    def preview(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return counts, exclusions, duration and cost without side effects."""

    def build(self, request: dict[str, Any]) -> dict[str, Any]:
        """Build a deterministic test plan matching test-plan.schema.json."""

