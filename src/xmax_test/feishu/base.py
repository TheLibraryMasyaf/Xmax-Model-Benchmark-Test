"""Feishu synchronization adapter boundary."""

from __future__ import annotations

from typing import Any, Protocol


class FeishuSyncAdapter(Protocol):
    def upsert(self, entity_type: str, entity: dict[str, Any]) -> dict[str, Any]:
        """Idempotently synchronize one business entity."""

    def verify(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        """Read back and compare critical fields and attachments."""

    def reconcile(self, entity_type: str | None = None) -> dict[str, Any]:
        """Report missing, duplicate, orphan and conflicting records."""

