"""Pipeline repository facade.

Locates stage runs, batches and their artifacts by stable IDs and hashes.
Single-stage execution never depends on the previous process's memory or temp
directory names.
"""

from __future__ import annotations

from typing import Any

from ..errors import NotFoundError


class PipelineRepository:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def stage_run(self, stage_run_id: str) -> dict[str, Any]:
        return self._repository.get_stage_run(stage_run_id)

    def batch(self, entity_type: str, batch_id: str) -> dict[str, Any]:
        return self._repository.get_batch_manifest(entity_type, batch_id)

    def batches_produced_by(self, stage_run_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self._repository.list_batch_manifests()
            if item.get("producer_stage_run_id") == stage_run_id
        ]

    def resumable(
        self, stage: str, input_hash: str, config_hash: str, producer_version: str
    ) -> dict[str, Any] | None:
        return self._repository.find_stage_run(
            stage, input_hash, config_hash, producer_version
        )

    def batch_items(self, entity_type: str, batch_id: str) -> list[dict[str, Any]]:
        """Load the item payloads behind a batch.

        ``run_batch`` items are generation runs, ``evaluation_batch`` items are
        evaluation results, and other batches fall back to raw item IDs.
        """

        manifest = self.batch(entity_type, batch_id)
        if entity_type == "run_batch":
            runs = []
            for item_id in manifest["item_ids"]:
                try:
                    runs.append(self._repository.get_run(item_id))
                except NotFoundError:
                    continue
            return runs
        if entity_type == "evaluation_batch":
            results = []
            for item_id in manifest["item_ids"]:
                try:
                    results.append(self._repository.get_evaluation_result(item_id))
                except NotFoundError:
                    continue
            return results
        return manifest.get("item_ids", [])

    def run_batch(self, batch_id: str) -> list[dict[str, Any]]:
        return self.batch_items("run_batch", batch_id)

    def evaluation_batch(self, batch_id: str) -> list[dict[str, Any]]:
        return self.batch_items("evaluation_batch", batch_id)
