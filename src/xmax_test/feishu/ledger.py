"""Sync ledger.

Idempotent upsert tracking per (entity_type, entity_id, destination) with
payload hashes and attempt history. Resume is driven by the ledger, never by
in-memory state.
"""

from __future__ import annotations

from typing import Any

from ..hashing import content_hash


class SyncLedger:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def payload_hash(self, payload: dict[str, Any]) -> str:
        return content_hash(payload)

    def begin(
        self,
        entity_type: str,
        entity_id: str,
        destination: str,
        payload_hash: str,
    ) -> dict[str, Any]:
        self._repository.upsert_sync_ledger(
            entity_type,
            entity_id,
            destination,
            payload_hash=payload_hash,
            sync_status="pending",
        )
        return self.entry(entity_type, entity_id, destination)

    def entry(self, entity_type: str, entity_id: str, destination: str) -> dict[str, Any]:
        for item in self._repository.list_sync_ledger(entity_type=entity_type):
            if item["entity_id"] == entity_id and item["destination"] == destination:
                return item
        raise KeyError(f"no ledger entry for {entity_type}/{entity_id}@{destination}")

    def mark_synced(
        self,
        entity_type: str,
        entity_id: str,
        destination: str,
        feishu_record_id: str | None = None,
    ) -> None:
        self._repository.update_sync_ledger(
            entity_type,
            entity_id,
            destination,
            sync_status="synced",
            feishu_record_id=feishu_record_id,
        )

    def mark_error(self, entity_type: str, entity_id: str, destination: str, error: str) -> None:
        self._repository.update_sync_ledger(
            entity_type,
            entity_id,
            destination,
            sync_status="error",
            last_error=error,
            increment_attempts=True,
        )

    def pending(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        return self._repository.list_sync_ledger(entity_type=entity_type, sync_status="pending")

    def pending_for_entity(self, entity_type: str, entity_id: str) -> bool:
        return any(
            item["entity_id"] == entity_id and item["sync_status"] == "pending"
            for item in self._repository.list_sync_ledger(entity_type=entity_type)
        )
