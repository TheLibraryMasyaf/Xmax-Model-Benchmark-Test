"""Reconcile: read-back verification of Feishu projection.

Detects missing, duplicate, conflicting and orphan records against the local
fact store. Never writes; dry-run semantics are implicit.
"""

from __future__ import annotations

from typing import Any


class ReconcileService:
    def __init__(self, client: Any, config: dict[str, Any], repository: Any) -> None:
        self._client = client
        self._config = config
        self._repository = repository

    def reconcile(
        self,
        run_batch_id: str | None = None,
        run_ids: list[str] | None = None,
        evaluations: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        app_token = self._config["base"]["app_token"]
        table_id = self._config["tables"]["case_data"]
        projection = self._config["field_projection"]["case_data"]

        remote = self._page_all(app_token, table_id)
        remote_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in remote:
            fields = record.get("fields", {})
            key = (
                str(fields.get(projection["case_number"], "")),
                str(fields.get(projection["model_version"], "")),
            )
            remote_by_key.setdefault(key, []).append(record)

        if run_ids is not None:
            local_runs = [self._repository.get_run(run_id) for run_id in run_ids]
        else:
            local_runs = self._repository.list_runs(run_batch_id=run_batch_id)
        local_by_key = {
            (run.get("case_number", ""), run.get("model_id", "")): run for run in local_runs
        }

        scoped = run_ids is not None or run_batch_id is not None
        if scoped:
            # A stage-level read-back verifies only the records just synced;
            # unrelated rows in the shared Case table are not orphans.
            remote_by_key = {
                key: records
                for key, records in remote_by_key.items()
                if key in local_by_key
            }

        missing = sorted(set(local_by_key) - set(remote_by_key))
        duplicates = {f"{k[0]}+{k[1]}": len(v) for k, v in remote_by_key.items() if len(v) > 1}
        orphan_keys = (
            [] if scoped else sorted(set(remote_by_key) - set(local_by_key))
        )

        conflicts: list[dict[str, Any]] = []
        for key, run in local_by_key.items():
            remote_records = remote_by_key.get(key)
            if not remote_records:
                continue
            remote_fields = remote_records[0].get("fields", {})
            # Score verification is only meaningful when the caller carries
            # the exact EvaluationResult selection used by sync. Never guess
            # by reading the newest local result.
            if evaluations is None:
                continue
            expected_score = self._expected_score(run, evaluations.get(run["run_id"]))
            stored = remote_fields.get(projection["score_percent"])
            if expected_score is None and stored is not None:
                conflicts.append(
                    {
                        "case_number": key[0],
                        "model_version": key[1],
                        "local_percent": None,
                        "remote_0_1": stored,
                        "reason": "unselected evaluation score must be blank",
                    }
                )
            elif expected_score is not None and stored is None:
                conflicts.append(
                    {
                        "case_number": key[0],
                        "model_version": key[1],
                        "local_percent": expected_score,
                        "remote_0_1": None,
                        "reason": "selected evaluation score is missing",
                    }
                )
            elif expected_score is not None and stored is not None:
                # remote is 0-1; local is 0-100
                local_scaled = round(expected_score / 100.0, 4)
                if abs(float(stored) - local_scaled) > 0.001:
                    conflicts.append(
                        {
                            "case_number": key[0],
                            "model_version": key[1],
                            "local_percent": expected_score,
                            "remote_0_1": stored,
                        }
                    )

        return {
            "remote_record_count": len(remote),
            "local_run_count": len(local_runs),
            "missing": [f"{k[0]}+{k[1]}" for k in missing],
            "duplicates": duplicates,
            "orphans": [f"{k[0]}+{k[1]}" for k in orphan_keys],
            "conflicts": conflicts,
            "ok": not missing and not duplicates and not conflicts and not orphan_keys,
        }

    def _expected_score(
        self, run: dict[str, Any], evaluation: dict[str, Any] | None
    ) -> float | None:
        if run.get("status") != "completed":
            return float(self._config["case_score"]["failed_run_value"])
        if evaluation is None:
            return None
        return evaluation.get("case_score_percent")

    def _page_all(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            page = self._client.list_records(app_token, table_id, page_token=page_token)
            records.extend(page.get("records", []))
            if not page.get("has_more"):
                break
            page_token = page.get("page_token")
        return records
