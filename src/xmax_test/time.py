"""Injectable UTC clock.

All timestamps are UTC, RFC 3339 and carry a trailing ``Z``. The clock can be
replaced with a fake in tests to keep hashes and manifests deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> str:
        """Return the current UTC RFC 3339 timestamp ending with ``Z``."""


class SystemClock:
    def now(self) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FixedClock:
    """Deterministic clock for tests and reproducible dry-runs."""

    def __init__(self, fixed: str = "2026-08-20T00:00:00Z") -> None:
        self._fixed = fixed

    def now(self) -> str:
        return self._fixed

    def advance(self, seconds: float) -> None:
        value = datetime.fromisoformat(self._fixed.replace("Z", "+00:00")) + timedelta(
            seconds=seconds
        )
        self._fixed = value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now() -> str:
    """Convenience default that does not require a clock instance."""

    return SystemClock().now()
