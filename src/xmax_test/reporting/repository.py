"""Report repository facade."""

from __future__ import annotations

from typing import Any


class ReportRepository:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def runs_for_model(self, model_version: str) -> list[dict[str, Any]]:
        return self._repository.list_runs(model_id=model_version)

    def evaluation_for_run(self, run_id: str) -> dict[str, Any] | None:
        results = self._repository.list_evaluation_results(run_id=run_id)
        return results[0] if results else None

    def case(self, case_id: str) -> dict[str, Any]:
        return self._repository.get_test_case(case_id)

    def batch_manifest(self, entity_type: str, batch_id: str) -> dict[str, Any]:
        return self._repository.get_batch_manifest(entity_type, batch_id)
