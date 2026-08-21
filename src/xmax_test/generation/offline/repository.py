"""Offline generation run persistence.

Creates runs, appends raw events, updates derived status, guards duplicate
submissions and supports resume from repository state.
"""

from __future__ import annotations

from typing import Any


class OfflineRunRepository:
    def __init__(self, repository: Any, artifacts: Any) -> None:
        self._repository = repository
        self._artifacts = artifacts

    def create_run(self, run: dict[str, Any]) -> None:
        self._repository.create_run(run)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._repository.get_run(run_id)

    def append_event(
        self,
        run_id: str,
        event: str,
        *,
        payload: dict[str, Any] | None = None,
        external_key: str | None = None,
    ) -> None:
        self._repository.append_event(run_id, event, payload=payload, external_key=external_key)

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        metrics: dict[str, Any] | None = None,
        result_asset_id: str | None = None,
        raw_events_uri: str | None = None,
    ) -> dict[str, Any]:
        return self._repository.update_run_status(
            run_id,
            status,
            metrics=metrics,
            result_asset_id=result_asset_id,
            raw_events_uri=raw_events_uri,
        )

    def save_raw_events(self, run_id: str, events: list[dict[str, Any]]) -> str | None:
        if not events:
            return None
        import json

        data = json.dumps(events, ensure_ascii=False, indent=2).encode("utf-8")
        result = self._artifacts.put_bytes("runs", f"{run_id}/events.jsonl", data)
        return result["uri"]

    def completed_run_for_case(self, case_id: str, model_id: str) -> dict[str, Any] | None:
        for run in self._repository.list_runs(case_id=case_id, status="completed"):
            if run.get("model_id") == model_id:
                return run
        return None

    def submitted_external_task(self, external_task_id: str) -> dict[str, Any] | None:
        for run in self._repository.list_runs():
            if run.get("metrics", {}).get("external_task_id") == external_task_id:
                return run
        return None

    def save_batch(self, manifest: dict[str, Any]) -> None:
        self._repository.save_batch_manifest(manifest)

    def list_runs(self, run_batch_id: str | None = None) -> list[dict[str, Any]]:
        return self._repository.list_runs(run_batch_id=run_batch_id)

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self._repository.get_asset(asset_id)

    def upsert_asset(self, asset: dict[str, Any]) -> dict[str, Any]:
        return self._repository.upsert_asset(asset)
