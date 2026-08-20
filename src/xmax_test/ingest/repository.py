"""Ingest repository facade."""

from __future__ import annotations

from typing import Any

from ..errors import NotFoundError


class IngestRepository:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def imported(self, import_request_id: str, source_hash: str) -> dict[str, Any] | None:
        return self._repository.get_result_import(import_request_id, source_hash)

    def record_import(
        self, import_request_id: str, source_hash: str, payload: dict[str, Any]
    ) -> None:
        self._repository.save_result_import(import_request_id, source_hash, payload)

    def run(self, run_id: str) -> dict[str, Any]:
        return self._repository.get_run(run_id)

    def runs_of_batch(self, run_batch_id: str) -> list[dict[str, Any]]:
        return self._repository.list_runs(run_batch_id=run_batch_id)
