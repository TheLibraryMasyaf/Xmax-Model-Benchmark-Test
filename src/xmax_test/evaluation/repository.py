"""Evaluation repository facade."""

from __future__ import annotations

from typing import Any

from ..errors import NotFoundError


class EvaluationRepository:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def result(self, evaluation_id: str) -> dict[str, Any]:
        return self._repository.get_evaluation_result(evaluation_id)

    def results(self, evaluation_batch_id: str | None = None) -> list[dict[str, Any]]:
        return self._repository.list_evaluation_results(
            evaluation_batch_id=evaluation_batch_id
        )

    def judgments(self, evaluation_id: str) -> list[dict[str, Any]]:
        return self._repository.list_judgments(evaluation_id)

    def run(self, run_id: str) -> dict[str, Any]:
        return self._repository.get_run(run_id)

    def runs(self, run_batch_id: str) -> list[dict[str, Any]]:
        return self._repository.list_runs(run_batch_id=run_batch_id)

    def save_batch(self, manifest: dict[str, Any]) -> None:
        self._repository.save_batch_manifest(manifest)
